#!/usr/bin/env python3
"""EVAsist · EPDK şarj istasyonlarını herkese açık JSON dosyası olarak yayınlar.

Her gece GitHub Actions'ta çalışır: EPDK servisinden TEK istekle tüm istasyonları çeker, yalnız
HALKA AÇIK istasyonları sadeleştirir ve docs/stations.json + docs/meta.json dosyalarını yazar.
Bu klasör GitHub Pages ile yayınlanır; uygulama (Readdy) dosyayı oradan okur. Veritabanı gerekmez.

  python3 scripts/epdk_to_json.py --out docs                 # EPDK'dan çek, docs/ altına yaz
  python3 scripts/epdk_to_json.py --out docs --from-file raw.json   # kayıtlı yanıttan dene

EPDK kotası doluysa veya servis yanıt vermezse dosyalar DEĞİŞMEZ (dünkü veri yayında kalır).
Şüpheli derecede az istasyon gelirse hiçbir şey yazılmaz ve iş hata verir.
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from epdk_sync import MIN_STATIONS, QuotaOrOutage, fetch_epdk, transform  # noqa: E402


def to_public_json(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if not r["is_public"]:
            continue
        digits = re.sub(r"\D", "", r["no"])
        if not digits:
            continue
        grouped = Counter((s["type"], s["standard"], s["kw"]) for s in r["sockets"])
        sockets = [
            {"type": t, "standard": st, "kw": kw, "count": n}
            for (t, st, kw), n in sorted(grouped.items(), key=lambda x: (x[0][0] != "DC", -(x[0][2] or 0)))
        ]
        powers = [s["kw"] for s in r["sockets"] if s["kw"]]
        out.append({
            "id": int(digits),
            "epdk_no": r["no"],
            "name": r["name"],
            "brand": r["brand"],
            "brand_key": r["brand_key"],
            "address": r["address"],
            "city": r["city"],
            "district": r["district"],
            "lat": r["lat"],
            "lng": r["lng"],
            "green": r["is_green"],
            "has_dc": any(s["type"] == "DC" for s in r["sockets"]),
            "max_kw": max(powers) if powers else None,
            "sockets": sockets,
        })
    out.sort(key=lambda s: s["id"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="EPDK istasyonlarını JSON olarak yayınla")
    parser.add_argument("--out", type=Path, required=True, help="Çıktı klasörü (ör. docs)")
    parser.add_argument("--from-file", type=Path, help="EPDK'ya gitmeden kayıtlı yanıtı kullan")
    args = parser.parse_args()

    try:
        raw = args.from_file.read_bytes() if args.from_file else fetch_epdk()
    except QuotaOrOutage as error:
        print(f"::warning::EPDK verisi bu gece alınamadı; dünkü dosya yayında kalıyor. {error}")
        return 0

    stations = to_public_json(transform(json.loads(raw)))
    if len(stations) < MIN_STATIONS:
        print(f"::error::Yalnızca {len(stations)} halka açık istasyon geldi (en az {MIN_STATIONS} bekleniyordu). Dosyalar değiştirilmedi.")
        return 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {
        "updated_at": now,
        "source": "EPDK",
        "station_count": len(stations),
        "socket_count": sum(s["count"] for st in stations for s in st["sockets"]),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "stations.json").write_text(
        json.dumps({**meta, "stations": stations}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (args.out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Tamamlandı: {meta['station_count']} halka açık istasyon, {meta['socket_count']} soket → {args.out}/stations.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
