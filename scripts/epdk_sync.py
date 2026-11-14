#!/usr/bin/env python3
"""EVAsist · EPDK şarj istasyonu gece senkronu.

EPDK'nın resmi servisinden (apigateway.epdk.gov.tr/sarjIstasyonlari) tüm istasyonları TEK istekle çeker,
sadeleştirir ve Supabase'deki charging_stations / charging_connectors tablolarına yazar.

EPDK servisi hakkında bilinmesi gerekenler:
  - Gövdeli GET bekler ({} = filtresiz). Gövdesiz GET 400, POST 404 döner.
  - Kota çok dardır (yaklaşık günde bir tam indirme). Bu yüzden her çalıştırmada tek istek atılır,
    asla tekrar denenmez. 429 (kota) veya EPDK kesintisinde mevcut veri olduğu gibi korunur.
  - "sarjIstasyonuIsletmecisi" alanı kişi adı içerebildiği için (KVKK) hiç alınmaz.

Ortam değişkenleri (GitHub > Settings > Secrets and variables > Actions):
  SUPABASE_URL               https://<proje>.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  Supabase secret / service_role anahtarı (asla uygulamaya konmaz)

Kullanım:
  python3 scripts/epdk_sync.py                         # EPDK'dan çek, Supabase'e yaz
  python3 scripts/epdk_sync.py --dry-run               # EPDK'dan çek, sadece özet yaz (Supabase'e dokunmaz)
  python3 scripts/epdk_sync.py --from-file raw.json    # EPDK'ya gitmeden kayıtlı yanıttan çalış
  python3 scripts/epdk_sync.py --save-raw raw.json     # ham EPDK yanıtını dosyaya da kaydet
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

EPDK_URL = "https://apigateway.epdk.gov.tr/sarjIstasyonlari"
USER_AGENT = "EVAsist-epdk-sync/1.0 (+https://evasist.com)"
BATCH_SIZE = 1000
MIN_STATIONS = 5000  # bundan az kayıt gelirse yanıt şüphelidir; hiçbir şey yazılmaz

PROVINCES = {
    "ADANA", "ADIYAMAN", "AFYONKARAHİSAR", "AĞRI", "AKSARAY", "AMASYA", "ANKARA", "ANTALYA", "ARDAHAN",
    "ARTVİN", "AYDIN", "BALIKESİR", "BARTIN", "BATMAN", "BAYBURT", "BİLECİK", "BİNGÖL", "BİTLİS", "BOLU",
    "BURDUR", "BURSA", "ÇANAKKALE", "ÇANKIRI", "ÇORUM", "DENİZLİ", "DİYARBAKIR", "DÜZCE", "EDİRNE",
    "ELAZIĞ", "ERZİNCAN", "ERZURUM", "ESKİŞEHİR", "GAZİANTEP", "GİRESUN", "GÜMÜŞHANE", "HAKKARİ", "HATAY",
    "IĞDIR", "ISPARTA", "İSTANBUL", "İZMİR", "KAHRAMANMARAŞ", "KARABÜK", "KARAMAN", "KARS", "KASTAMONU",
    "KAYSERİ", "KIRIKKALE", "KIRKLARELİ", "KIRŞEHİR", "KİLİS", "KOCAELİ", "KONYA", "KÜTAHYA", "MALATYA",
    "MANİSA", "MARDİN", "MERSİN", "MUĞLA", "MUŞ", "NEVŞEHİR", "NİĞDE", "ORDU", "OSMANİYE", "RİZE",
    "SAKARYA", "SAMSUN", "SİİRT", "SİNOP", "SİVAS", "ŞANLIURFA", "ŞIRNAK", "TEKİRDAĞ", "TOKAT", "TRABZON",
    "TUNCELİ", "UŞAK", "VAN", "YALOVA", "YOZGAT", "ZONGULDAK",
}

_ADDR_TAIL = re.compile(r"([^/]+?)\s*/\s*([^/]+?)\s*$")


class QuotaOrOutage(Exception):
    """EPDK kotası doldu ya da servis geçici olarak yanıt vermiyor: rutin, ertesi gece tekrar denenir."""


def tr_lower(text: str) -> str:
    return " ".join(text.replace("İ", "i").replace("I", "ı").lower().split())


def tr_upper(text: str) -> str:
    return " ".join(text.replace("i", "İ").replace("ı", "I").upper().split())


def clean(text) -> str | None:
    if text is None:
        return None
    value = " ".join(str(text).split())
    return value or None


def number(value) -> float | None:
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def split_city(address: str | None) -> tuple[str | None, str | None]:
    """EPDK adresleri genelde '... Arnavutköy / İSTANBUL' ile biter. (ilçe, il) döner."""
    match = _ADDR_TAIL.search(address or "")
    if not match:
        return None, None
    city = tr_upper(match.group(2))
    if city not in PROVINCES:
        return None, None
    words = match.group(1).split()
    district = words[-1] if words else None
    return district, city


def classify_socket(sock: dict) -> dict | None:
    no = clean(sock.get("soketNo"))
    if not no:
        return None
    kind = (sock.get("soketTuru") or "").strip().upper()
    current = (sock.get("soketTipi") or "").strip().upper()
    if current not in ("AC", "DC"):
        current = "DC" if kind.startswith("DC") else "AC" if kind.startswith("AC") else None
    if current is None:
        return None
    if "CCS" in kind:
        standard = "CCS"
    elif "TYPE2" in kind or "TYPE_2" in kind:
        standard = "TYPE2"
    elif "CHADEMO" in kind:
        standard = "CHADEMO"
    else:
        standard = "OTHER"
    kw = number(sock.get("soketGucu"))
    return {"no": no, "type": current, "standard": standard, "kw": kw if kw and kw > 0 else None}


def transform(payload) -> list[dict]:
    records = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise RuntimeError(f"Beklenmeyen EPDK yanıtı: {str(payload)[:300]}")

    rows, seen = [], set()
    for rec in records:
        no = clean(rec.get("sarjIstasyonuNo"))
        lat, lng = number(rec.get("enlem")), number(rec.get("boylam"))
        if not no or no in seen or lat is None or lng is None:
            continue
        if not (35 <= lat <= 43 and 25 <= lng <= 45):
            continue
        seen.add(no)
        brand = clean(rec.get("marka"))
        address = clean(rec.get("adres"))
        district, city = split_city(address)
        sockets, socket_nos = [], set()
        for raw_socket in rec.get("soketler") or []:
            sock = classify_socket(raw_socket)
            if sock and sock["no"] not in socket_nos:
                socket_nos.add(sock["no"])
                sockets.append(sock)
        rows.append({
            "no": no,
            "name": clean(rec.get("sarjIstasyonuAdi")) or no,
            "brand": brand,
            "brand_key": tr_lower(brand) if brand else None,
            "network_operator": clean(rec.get("sarjAgiIsletmecisiUnvan")),
            "license_no": clean(rec.get("sarjAgiIsletmecisiLisansNo")),
            "address": address,
            "city": city,
            "district": district,
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "is_public": (rec.get("hizmetSekli") or "").strip().upper() == "HALKA_ACIK",
            "is_green": (rec.get("yesilSarjIstasyonuMu") or "").strip().upper() == "EVET",
            "sockets": sockets,
            # "sarjIstasyonuIsletmecisi" bilerek alınmaz (KVKK).
        })
    return rows


def fetch_epdk() -> bytes:
    request = urllib.request.Request(
        EPDK_URL,
        data=b"{}",
        method="GET",
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        body = error.read()[:300]
        if error.code == 429 or error.code >= 500:
            raise QuotaOrOutage(f"EPDK HTTP {error.code}: {body!r}") from error
        raise RuntimeError(f"EPDK HTTP {error.code}: {body!r}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise QuotaOrOutage(f"EPDK'ya ulaşılamadı: {error}") from error


class Supabase:
    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1/rpc/"
        self.headers = {"apikey": key, "Content-Type": "application/json", "Accept": "application/json"}
        # Eski tip (JWT) service_role anahtarı Authorization başlığıyla da gönderilir;
        # yeni tip "sb_secret_..." anahtarlar yalnızca apikey başlığıyla çalışır.
        if not key.startswith("sb_"):
            self.headers["Authorization"] = f"Bearer {key}"

    def rpc(self, name: str, params: dict):
        data = json.dumps(params, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base + name, data=data, method="POST", headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Supabase {name} HTTP {error.code}: {error.read()[:500]!r}") from error
        return json.loads(raw) if raw else None


def summary(rows: list[dict]) -> str:
    public = sum(1 for r in rows if r["is_public"])
    sockets = sum(len(r["sockets"]) for r in rows)
    with_city = sum(1 for r in rows if r["city"])
    return (f"{len(rows)} istasyon ({public} halka açık), {sockets} soket, "
            f"il bilgisi çıkarılan: {with_city}")


def main() -> int:
    parser = argparse.ArgumentParser(description="EPDK şarj istasyonu senkronu")
    parser.add_argument("--from-file", type=Path, help="EPDK'ya gitmeden kayıtlı yanıtı kullan")
    parser.add_argument("--save-raw", type=Path, help="Ham EPDK yanıtını bu dosyaya da kaydet")
    parser.add_argument("--dry-run", action="store_true", help="Supabase'e yazma, sadece özet göster")
    args = parser.parse_args()

    try:
        raw = args.from_file.read_bytes() if args.from_file else fetch_epdk()
    except QuotaOrOutage as error:
        print(f"::warning::EPDK verisi bu gece alınamadı, mevcut veri korunuyor. {error}")
        return 0

    if args.save_raw:
        args.save_raw.write_bytes(raw)

    rows = transform(json.loads(raw))
    print("EPDK:", summary(rows))
    if len(rows) < MIN_STATIONS:
        print(f"::error::Yalnızca {len(rows)} istasyon geldi (en az {MIN_STATIONS} bekleniyordu). Hiçbir şey yazılmadı.")
        return 1

    if args.dry_run:
        print("Deneme modu: Supabase'e yazılmadı. Örnek kayıt:")
        print(json.dumps(rows[0], ensure_ascii=False, indent=2))
        return 0

    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("::error::SUPABASE_URL ve SUPABASE_SERVICE_ROLE_KEY tanımlı değil.")
        return 1

    db = Supabase(url, key)
    try:
        run_id = db.rpc("epdk_start_run", {})
    except Exception as error:  # noqa: BLE001
        print(f"::error::Supabase'e bağlanılamadı (URL veya anahtar yanlış olabilir): {error}")
        return 1
    print(f"Senkron no: {run_id}")
    try:
        written = 0
        for start in range(0, len(rows), BATCH_SIZE):
            batch = rows[start:start + BATCH_SIZE]
            written += db.rpc("epdk_upsert_batch", {"p_run_id": run_id, "p_rows": batch}) or 0
            print(f"  {min(start + BATCH_SIZE, len(rows))} / {len(rows)} işlendi")
        result = db.rpc("epdk_finish_run", {"p_run_id": run_id})
    except Exception as error:  # noqa: BLE001 - kayıt başarısız işaretlenir, iş hata verir
        try:
            db.rpc("epdk_fail_run", {"p_run_id": run_id, "p_note": str(error)})
        finally:
            print(f"::error::Senkron başarısız: {error}")
        return 1

    print(f"Sonuç: {json.dumps(result, ensure_ascii=False)}")
    if not result or result.get("status") != "success":
        print("::error::Senkron tamamlanamadı; ayrıntı epdk_sync_runs tablosunda.")
        return 1
    print(f"Tamamlandı: {result['stations']} istasyon, {result['connectors']} soket, "
          f"{result['deactivated']} istasyon pasif yapıldı.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
