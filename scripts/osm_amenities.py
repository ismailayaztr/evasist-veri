#!/usr/bin/env python3
"""
EVAsist — istasyon çevresi olanak/marka verisi (OpenStreetMap'ten toplu hesaplama)

Girdi : docs/stations.json (EPDK istasyonları) + Türkiye OSM özeti (.osm.pbf, önceden
        osmium tags-filter ile süzülmüş olması önerilir)
Çıktı : docs/amenities.json  — her istasyon için 300 m içindeki olanaklar, zincir
        markalar ve tesis tipi.

Veri © OpenStreetMap katkıda bulunanlar, ODbL 1.0 (https://www.openstreetmap.org/copyright).
Bu dosyadan türetilen veritabanı da ODbL ile paylaşılmalıdır.
"""
import argparse, json, math, re, sys, time, unicodedata
from collections import defaultdict

import osmium

RADIUS_M = 300          # olanak ve marka arama yarıçapı
CELL = 0.01             # ızgara hücresi (derece) ~1.1 km

# ---------------------------------------------------------------- olanak kodları
# Kodlar uygulamadaki station_amenities kodlarıyla aynı.
def amenity_codes(t):
    a, s, tr, hw, le = t.get("amenity"), t.get("shop"), t.get("tourism"), t.get("highway"), t.get("leisure")
    out = []
    if a == "toilets": out.append("wc")
    if a == "place_of_worship" and t.get("religion") == "muslim": out.append("mescit")
    if a == "cafe": out.append("kafe")
    if a in ("restaurant", "food_court"): out.append("restoran")
    if a == "fast_food": out.append("fast_food")
    if s in ("supermarket", "convenience"): out.append("market")
    if s == "mall": out.append("avm")
    if a == "fuel": out.append("akaryakit")
    if tr in ("hotel", "motel"): out.append("otel")
    if hw in ("services", "rest_area"): out.append("dinlenme_tesisi")
    if a == "parking":
        fee = (t.get("fee") or "").lower()
        out.append("otopark_ucretli" if fee in ("yes", "evet") else "otopark_ucretsiz" if fee in ("no", "hayır", "hayir") else "otopark")
    if a == "car_wash": out.append("arac_yikama")
    if a == "pharmacy": out.append("eczane")
    if a == "atm" or (a == "bank" and t.get("atm") == "yes"): out.append("atm")
    if s == "tyres": out.append("lastikci")
    if le == "playground": out.append("cocuk_oyun_alani")
    if a == "hospital": out.append("hastane")
    if a in ("university", "college"): out.append("kampus")
    if s == "car": out.append("oto_bayi")
    return out

# Tesis tipi: istasyon bu tesisin içinde/yanında mı? (kod -> en fazla mesafe m)
FACILITY = {
    "avm": 150, "dinlenme_tesisi": 300, "otel": 80, "hastane": 150,
    "kampus": 200, "akaryakit": 60, "oto_bayi": 80,
}

# ---------------------------------------------------------------- zincir markalar
BRANDS = [
    # slug, görünen ad, kategori, alan adı (logo için), desen (normalize metin)
    ("starbucks", "Starbucks", "kahve", "starbucks.com.tr", r"\bstarbucks\b"),
    ("kahve_dunyasi", "Kahve Dünyası", "kahve", "kahvedunyasi.com", r"kahve dunyasi"),
    ("espressolab", "Espressolab", "kahve", "espressolab.com", r"espresso ?lab"),
    ("caffe_nero", "Caffè Nero", "kahve", "caffenero.com.tr", r"caff?e\s*nero"),
    ("gloria_jeans", "Gloria Jean's", "kahve", "gloriajeanscoffees.com.tr", r"gloria jean"),
    ("tchibo", "Tchibo", "kahve", "tchibo.com.tr", r"\btchibo\b"),
    ("simit_sarayi", "Simit Sarayı", "kahve", "simitsarayi.com", r"simit sarayi"),
    ("mcdonalds", "McDonald's", "fast_food", "mcdonalds.com.tr", r"mc ?donald"),
    ("burger_king", "Burger King", "fast_food", "burgerking.com.tr", r"burger ?king"),
    ("kfc", "KFC", "fast_food", "kfcturkiye.com", r"\bkfc\b"),
    ("popeyes", "Popeyes", "fast_food", "popeyes.com.tr", r"\bpopeyes\b"),
    ("dominos", "Domino's", "fast_food", "dominos.com.tr", r"\bdomino'?s\b"),
    ("little_caesars", "Little Caesars", "fast_food", "littlecaesars.com.tr", r"little caesar"),
    ("komagene", "Komagene", "fast_food", "komagene.com.tr", r"\bkomagene\b"),
    ("kofteci_yusuf", "Köfteci Yusuf", "restoran", "kofteciyusuf.com", r"kofteci yusuf"),
    ("tavuk_dunyasi", "Tavuk Dünyası", "restoran", "tavukdunyasi.com", r"tavuk dunyasi"),
    ("usta_donerci", "Usta Dönerci", "fast_food", "ustadonerci.com", r"usta donerci"),
    ("macrocenter", "Macrocenter", "market", "macrocenter.com.tr", r"macro ?center"),
    ("migros", "Migros", "market", "migros.com.tr", r"\bmigros\b|^m{2,3}\b"),
    ("carrefoursa", "CarrefourSA", "market", "carrefoursa.com", r"\bcarrefour"),
    ("bim", "BİM", "market", "bim.com.tr", r"^bim\b|\bbim birlesik"),
    ("a101", "A101", "market", "a101.com.tr", r"\ba ?101\b"),
    ("sok", "ŞOK", "market", "sokmarket.com.tr", r"^sok\b|\bsok market"),
    ("file", "File", "market", "file.com.tr", r"^file\b|\bfile market"),
    ("tarim_kredi", "Tarım Kredi", "market", "", r"tarim kredi"),
]
BRAND_RX = [(slug, re.compile(rx)) for slug, _, _, _, rx in BRANDS]
FOOD_SHOP = {"cafe", "restaurant", "fast_food", "food_court", "ice_cream", "bakery"}
SHOP_OK = {"supermarket", "convenience", "coffee", "bakery", "pastry", "mall", "department_store"}

TR = str.maketrans({"İ": "i", "I": "ı", "Ç": "c", "ç": "c", "Ğ": "g", "ğ": "g", "ı": "i",
                    "Ö": "o", "ö": "o", "Ş": "s", "ş": "s", "Ü": "u", "ü": "u", "â": "a", "è": "e", "é": "e"})

def norm(s):
    s = (s or "").translate(TR).lower().translate(TR)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", s)).strip()

def brand_of(t):
    a, s = t.get("amenity"), t.get("shop")
    if a not in FOOD_SHOP and s not in SHOP_OK:
        return None
    for key in ("brand", "brand:tr", "brand:en", "name", "name:tr", "name:en", "operator"):
        v = norm(t.get(key))
        if not v:
            continue
        for slug, rx in BRAND_RX:
            if rx.search(v):
                return slug
    return None

# ---------------------------------------------------------------- OSM okuma
class Collector(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.pois = []   # (lat, lon, codes, brand, name, opening_hours, osm_id)

    def _add(self, t, lat, lon, oid):
        codes = amenity_codes(t)
        b = brand_of(t)
        if not codes and not b:
            return
        self.pois.append((lat, lon, codes, b, t.get("name") or "", t.get("opening_hours") or "", oid))

    def node(self, n):
        if not n.tags or not n.location.valid():
            return
        self._add(dict(n.tags), n.location.lat, n.location.lon, "n%d" % n.id)

    def way(self, w):
        if not w.tags:
            return
        t = dict(w.tags)
        if not amenity_codes(t) and not brand_of(t):
            return
        la = lo = 0.0; k = 0
        for nd in w.nodes:
            if nd.location.valid():
                la += nd.location.lat; lo += nd.location.lon; k += 1
        if k:
            self._add(t, la / k, lo / k, "w%d" % w.id)

def haversine(la1, lo1, la2, lo2):
    r = 6371000.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--stations", default="docs/stations.json")
    ap.add_argument("--out", default="docs/amenities.json")
    args = ap.parse_args()

    t0 = time.time()
    st = json.load(open(args.stations, encoding="utf-8"))
    stations = st["stations"] if isinstance(st, dict) else st

    c = Collector()
    c.apply_file(args.pbf, locations=True, idx="flex_mem")
    print(f"OSM nesnesi: {len(c.pois)}  ({time.time()-t0:.0f} sn)")

    grid = defaultdict(list)
    for p in c.pois:
        grid[(int(p[0] // CELL), int(p[1] // CELL))].append(p)

    out = {}
    with_any = with_brand = 0
    for s in stations:
        la, lo = s.get("lat"), s.get("lng", s.get("lon"))
        if la is None or lo is None:
            continue
        gi, gj = int(la // CELL), int(lo // CELL)
        near_a, near_b, types = {}, {}, set()
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for (pla, plo, codes, b, name, oh, oid) in grid.get((gi + di, gj + dj), ()):
                    d = haversine(la, lo, pla, plo)
                    if d > RADIUS_M:
                        continue
                    d = int(round(d))
                    for code in codes:
                        if code in FACILITY and d <= FACILITY[code]:
                            types.add(code)
                        if code not in near_a or d < near_a[code]:
                            near_a[code] = d
                    if b and (b not in near_b or d < near_b[b][0]):
                        near_b[b] = (d, name, oh, oid)
        if not near_a and not near_b:
            continue
        rec = {}
        if types: rec["t"] = sorted(types)
        if near_a: rec["a"] = dict(sorted(near_a.items(), key=lambda kv: kv[1]))
        if near_b:
            rec["b"] = [[k, v[0], v[1], v[2], v[3]] for k, v in sorted(near_b.items(), key=lambda kv: kv[1][0])][:10]
            with_brand += 1
        out[str(s["id"])] = rec
        with_any += 1

    doc = {
        "v": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "OpenStreetMap",
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap katkıda bulunanlar",
        "attribution_url": "https://www.openstreetmap.org/copyright",
        "radius_m": RADIUS_M,
        "format": {
            "t": "tesis tipi (istasyon bu tesisin içinde/yanında)",
            "a": "olanak kodu -> en yakın mesafe (m)",
            "b": "[marka, mesafe m, OSM adı, açılış saatleri, osm id]",
        },
        "brands": {slug: {"name": n, "cat": cat, "domain": dom} for slug, n, cat, dom, _ in BRANDS},
        "station_count": with_any,
        "stations_with_brand": with_brand,
        "stations": out,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    print(f"İstasyon (olanak bulunan): {with_any}/{len(stations)}, markalı: {with_brand}, süre {time.time()-t0:.0f} sn")

if __name__ == "__main__":
    sys.exit(main())

