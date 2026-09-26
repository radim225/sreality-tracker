"""Adresa inzerátu: kde ten byt nebo ta garáž doopravdy je.

Radim chce u inzerátů -- hlavně u garáží a u zmizelých -- vědět adresu, nebo
aspoň nejlepší odhad. Sreality číslo domu často nedá, a u zmizelého inzerátu
už se nedá dohledat nic, takže adresu je potřeba zapsat, dokud inzerát žije.

Zdroje v pořadí důvěry:
  1. Sreality samo (`estate.locality`): entityType "address" znamená, že bod
     míří na konkrétní budovu. S číslem domu je to přesná adresa.
  2. Číslo domu v popisu inzerátu („Na Krocínce 12", „č. p. 1234/5") -- jen
     když sedí ulice z inzerátu, nebo je to výslovně číslo popisné. Popis
     plný čísel („2+kk", „3. patro", „cena 12 000 Kč") je past, proto přísně.
  3. Reverzní geokódování bodu ze Sreality přes OpenStreetMap Nominatim. To je
     ODHAD: když Sreality zná jen ulici, jeho bod je často střed ulice a
     „nejbližší dům" je tip. Radim byl na falešnou přesnost upozorněn a zvolil
     si ji -- proto to štítek i poznámka musí říkat nahlas.
  4. Jinak jen ulice a čtvrť.

Geokódování je příloha, ne jádro: nic tady nesmí shodit běh scrapu. Každá
chyba se spolkne, zapíše do cache jako neúspěch a zkusí se znovu až za týden.

Nominatim je zdarma za podmínek (usage policy): nejvýš 1 dotaz za sekundu,
popisný User-Agent, výsledky cachovat. Proto cache navždy a strop dotazů na
běh -- první běh doplní část, další běhy zbytek.
"""
import json
import math
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    # requests místo urllib ze stejného důvodu jako v notify.py: urllib tu
    # nemá kořenové certifikáty a každý dotaz by skončil na SSL chybě.
    import requests
except Exception:  # noqa: BLE001 -- bez requests prostě bez sítě
    requests = None

CACHE_PATH = Path(__file__).parent / "geocode_cache.json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "sreality-tracker/1.0 (+https://github.com/radim225/sreality-tracker)"
TIMEOUT_S = 15
# Policy říká max 1/s; 1,1 s je rezerva na nepřesnost hodin a sítě.
MIN_INTERVAL_S = 1.1
# Neúspěch (timeout, 5xx, nic v okolí) se nezkouší každý běh znovu -- to by
# byl přesně ten dotaz navíc, který policy nechce -- ale ani navždy.
RETRY_AFTER = timedelta(days=7)
# Dál než 300 m od bodu už to není „nejbližší dům", ale jiné místo. Na
# periferii Nominatim klidně vrátí budovu přes pole.
MAX_DIST_M = 300


def _env_int(name, default):
    """Prázdná proměnná (GitHub Actions ji tak předá, když secret/var chybí)
    znamená výchozí hodnotu, ne pád na int('')."""
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


MAX_LOOKUPS = _env_int("GEOCODE_MAX_LOOKUPS", 60)


def _disabled():
    return (os.environ.get("GEOCODE_DISABLED") or "").strip() not in ("", "0")


def _now():
    return datetime.now(timezone.utc)


def _stamp(dt=None):
    return (dt or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- souřadnice ------------------------------------------------------------ #

def _coord(value, limit):
    """Float v rozsahu, jinak None. Souřadnice jdou až do href na dashboardu,
    takže sem nesmí projít nic jiného než číslo (ani NaN, ani řetězec)."""
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or abs(num) > limit:
        return None
    return num


def _latlon(lat, lon):
    la, lo = _coord(lat, 90), _coord(lon, 180)
    return (la, lo) if la is not None and lo is not None else None


def cache_key(lat, lon):
    # 5 desetinných míst ≈ 1 m: stejný bod ze dvou běhů je jeden klíč, a
    # dva různé domy nikdy nesplynou.
    return f"{lat:.5f},{lon:.5f}"


def _dist_m(la1, lo1, la2, lo2):
    la1, lo1, la2, lo2 = map(math.radians, (la1, lo1, la2, lo2))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(min(1.0, h)))


# --- cache ------------------------------------------------------------------ #

def load_cache(path=None):
    """Poškozená nebo chybějící cache = prázdná cache. Přijdeme jen o pár
    dotazů navíc, ne o běh."""
    try:
        data = json.loads(Path(path or CACHE_PATH).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_cache(cache, path=None):
    """Seřazené klíče a indent=1, aby diff v gitu ukázal jen nové body."""
    try:
        Path(path or CACHE_PATH).write_text(
            json.dumps(cache, sort_keys=True, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# --- Nominatim -------------------------------------------------------------- #

_last_call = [0.0]


def _sleep(seconds):
    time.sleep(seconds)


# Jistič: po 429/403 už tenhle běh na Nominatim nesahá. Politika služby
# říká přestat, když nás omezuje -- zkoušet dalších 59 bodů by bylo přesně
# to „heavy use", kvůli kterému by nás zablokovali.
_tripped = [False]


def _http_get(url, params):
    """Jediné místo, které sahá na síť -- testy ho nahrazují."""
    response = requests.get(url, params=params, timeout=TIMEOUT_S,
                            headers={"User-Agent": USER_AGENT,
                                     "Accept-Language": "cs"})
    if response.status_code in (403, 429):
        _tripped[0] = True
    response.raise_for_status()
    return response.json()


def _parse(payload, lat, lon):
    """Z odpovědi jen to, co potřebujeme -- surová odpověď má kilobajty
    (licence, bounding box, OSM id) a cache by zbytečně bobtnala."""
    if not isinstance(payload, dict) or payload.get("error"):
        return {"error": str((payload or {}).get("error") or "prázdná odpověď")[:120]}
    rl, ro = _latlon(payload.get("lat"), payload.get("lon"))  or (None, None)
    if rl is None:
        return {"error": "odpověď bez souřadnic"}
    dist = round(_dist_m(lat, lon, rl, ro))
    if dist > MAX_DIST_M:
        return {"error": f"nejbližší výsledek {dist} m od bodu"}
    addr = payload.get("address") or {}
    out = {
        "road": addr.get("road") or addr.get("pedestrian") or addr.get("footway"),
        "house_number": addr.get("house_number"),
        "suburb": (addr.get("suburb") or addr.get("quarter")
                   or addr.get("city_district") or addr.get("neighbourhood")),
        "postcode": addr.get("postcode"),
        "distance_m": dist,
    }
    return {k: (str(v)[:80] if isinstance(v, str) else v)
            for k, v in out.items() if v not in (None, "")}


def _fresh(entry):
    if not isinstance(entry, dict):
        return False
    if "error" not in entry:
        return True
    try:
        at = datetime.strptime(entry.get("at", ""), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return _now() - at.replace(tzinfo=timezone.utc) < RETRY_AFTER


def reverse(lat, lon, cache, allow_live=True):
    """Adresa nejbližší k bodu. Vrací (záznam nebo None, zda šel živý dotaz).

    Záznam je dict s road/house_number/suburb/postcode/distance_m, nebo None,
    když nic není (neúspěch, zakázaná síť, vyčerpaný strop)."""
    ll = _latlon(lat, lon)
    if not ll:
        return None, False
    lat, lon = ll
    key = cache_key(lat, lon)
    entry = cache.get(key)
    if _fresh(entry):
        return (None if "error" in entry else entry), False
    if not allow_live or _disabled() or requests is None or _tripped[0]:
        return None, False
    try:
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            _sleep(wait)
        _last_call[0] = time.monotonic()
        payload = _http_get(NOMINATIM_URL, {
            "format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
            "zoom": 18, "addressdetails": 1, "accept-language": "cs"})
        result = _parse(payload, lat, lon)
    except Exception as exc:  # noqa: BLE001 -- geokódování nesmí shodit běh
        result = {"error": f"{type(exc).__name__}: {exc}"[:160]}
    result["at"] = _stamp()
    cache[key] = result
    return (None if "error" in result else result), True


# --- číslo domu z popisu ---------------------------------------------------- #

def _fold(text):
    """Bez diakritiky a malými písmeny, ZNAK ZA ZNAK -- indexy sedí na
    původní text, takže číslo lze vyříznout z originálu."""
    out = []
    for ch in text:
        base = [c for c in unicodedata.normalize("NFKD", ch)
                if not unicodedata.combining(c)]
        low = (base[0] if len(base) == 1 else ch).lower()
        out.append(low if len(low) == 1 else ch)
    return "".join(out)


# Číslo domu: 12, 12a, 1234/5, 1197/60b. Nic delšího -- pětimístné „číslo"
# je spíš cena nebo PSČ.
_NUM = r"(\d{1,4}(?:\s?/\s?\d{1,4})?[a-z]?)(?![\d/a-z])"
# Výslovné číslo popisné. „č." samo NESTAČÍ -- „tram č. 12", „bus č. 136".
_CP = re.compile(r"(?<![a-z])(?:c\.\s?p\.|cp\.|cislo popisne|c\.\s?pop\.)\s*" + _NUM)
# Co za číslem znamená, že to číslo domu není: plocha, cena, patro, dispozice,
# tisíce oddělené mezerou („12 000"), pořadí („3. patro").
_NOT_HOUSE = re.compile(
    r"\s*(?:m2|m²|m\b|kc|czk|,-|%|\+|\.\s*(?:patr|podl|np|pp)|\.\d|\s\d{3}\b|"
    r"\s?(?:patr|podl|np|pp|min|km|let|rok|mil|tis|kus|mist|stani))")


def house_number_from_text(description, street):
    """Číslo domu z popisu, nebo None. Radši nic než špatné číslo: vymyšlená
    adresa je horší než žádná, protože vypadá věrohodně."""
    if not description or not isinstance(description, str):
        return None
    text = _fold(description)
    for m in _CP.finditer(text):
        tail = text[m.end():m.end() + 12]
        if not _NOT_HOUSE.match(tail):
            return _clean(description[m.start(1):m.end(1)])
    if not street or not isinstance(street, str) or len(street.strip()) < 3:
        return None
    name = re.escape(_fold(street.strip()))
    name = name.replace(r"\ ", r"\s+")
    pattern = re.compile(r"(?<![a-z])" + name + r",?\s+(?:c\.\s?)?" + _NUM)
    for m in pattern.finditer(text):
        tail = text[m.end():m.end() + 12]
        if _NOT_HOUSE.match(tail):
            continue
        return _clean(description[m.start(1):m.end(1)])
    return None


def _clean(num):
    return re.sub(r"\s+", "", num).lower()


# --- anotace ---------------------------------------------------------------- #

def _join(*parts):
    return ", ".join(p for p in parts if p)


def _street_part(street, number):
    return " ".join(p for p in (street, number) if p)


def annotate(items, cache, budget=None):
    """Doplní item["address"] podle pořadí důvěry. Vrací počet živých dotazů.

    Na položce čte: lat, lon, street, house_number, locality_entity,
    city_part, description. Zapisuje jen klíč "address" -- nic jiného nemění.
    Strop `budget` platí jen pro živé dotazy; zásahy do cache jsou zdarma."""
    budget = MAX_LOOKUPS if budget is None else budget
    live = 0
    for item in items or []:
        try:
            if (item.get("address") or {}).get("precision") == "exact":
                continue
            address = _address_for(item, cache, allow_live=live < budget)
            if address.pop("_live", False):
                live += 1
            if address.get("text"):
                item["address"] = address
        except Exception:  # noqa: BLE001 -- jedna divná položka nezastaví zbytek
            continue
    return live


def _address_for(item, cache, allow_live):
    street = (item.get("street") or "").strip() or None
    part = (item.get("city_part") or "").strip() or None
    number = str(item.get("house_number") or "").strip() or None
    entity = item.get("locality_entity")

    # 1) Sreality ukazuje na budovu a zná číslo.
    if entity == "address" and number:
        return {"text": _join(_street_part(street, number), part),
                "precision": "exact", "source": "sreality",
                "note": "adresa přímo ze Sreality"}

    # 2) Číslo v popisu.
    found = house_number_from_text(item.get("description"), street)
    if found:
        text = _street_part(street, found) if street else f"č. p. {found}"
        return {"text": _join(text, part), "precision": "text", "source": "popis",
                "note": "číslo domu uvedené v popisu inzerátu"}

    # 3) Nejbližší adresa k bodu Sreality.
    hit, was_live = reverse(item.get("lat"), item.get("lon"), cache, allow_live)
    if hit and hit.get("house_number") and hit.get("road"):
        if entity == "address":
            note = ("Sreality bod míří na budovu, číslo doplněno z OpenStreetMap "
                    "— odhad")
        else:
            note = "nejbližší adresa k bodu, který Sreality uvádí — odhad"
        return {"text": _join(_street_part(hit["road"], hit["house_number"]),
                              hit.get("suburb") or part),
                "precision": "estimate", "source": "nominatim",
                "note": f"{note} ({hit.get('distance_m', '?')} m od bodu)",
                "distance_m": hit.get("distance_m"), "_live": was_live}

    # 4) Jen ulice.
    if street:
        return {"text": _join(street, part), "precision": "street",
                "source": "sreality-ulice",
                "note": "Sreality uvádí jen ulici, číslo domu neznámé",
                "_live": was_live}
    if hit and hit.get("road"):
        return {"text": _join(hit["road"], hit.get("suburb") or part),
                "precision": "street", "source": "nominatim",
                "note": "jen ulice nejbližší k bodu Sreality — odhad",
                "_live": was_live}
    return {"_live": was_live}


# --- odkazy na mapu --------------------------------------------------------- #

def map_links(lat, lon):
    """Odkazy na mapu a pohled z ulice, nebo None pro neplatný bod.

    Souřadnice se validují jako čísla a formátují na 6 desetinných míst
    (≈ 10 cm), takže do href se nedostane nic jiného než číslice, tečka a
    minus.

    "panorama": Mapy.cz NEMÁ zdokumentovaný odkaz „panorama u bodu" --
    jejich URL s pano=1 potřebuje pid konkrétního snímku a x/y o něm
    nerozhodují (zjistit pid jde jen přes jejich JS API). Proto odkaz
    otevře bod ve větším přiblížení, odkud je panorama o jedno kliknutí.
    Skutečný pohled z ulice rovnou otevře jen "google_street"."""
    ll = _latlon(lat, lon)
    if not ll:
        return None
    la, lo = f"{ll[0]:.6f}", f"{ll[1]:.6f}"
    return {
        "mapy": f"https://mapy.cz/zakladni?source=coor&id={lo}%2C{la}&x={lo}&y={la}&z=18",
        "panorama": f"https://mapy.cz/zakladni?source=coor&id={lo}%2C{la}&x={lo}&y={la}&z=19",
        "google_street": ("https://www.google.com/maps/@?api=1&map_action=pano"
                          f"&viewpoint={la},{lo}"),
    }
