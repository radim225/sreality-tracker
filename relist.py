"""Znovu vložené inzeráty: smazaný inzerát, který se vrátil pod novým id.

Makléři inzerát běžně stáhnou a vloží znovu -- nové id, nové datum, tentýž
byt nebo tatáž garáž. Bez spárování to na dashboardu vypadá jako „zmizelo"
a „nové" zároveň, a z historie ceny zmizí všechno před novým vložením.

Pravidla jsou přísná (Radim, 26. 9. 2026: „pokud sedí popis a cena cca"):
  * stejný typ obchodu a stejný druh (dispozice; garáž a stání jsou jedno),
  * cena ±10 %, plocha ±1 m² (u bytů ±3 %),
  * poloha do 150 m, nebo stejná ulice, když jeden z nich nemá GPS,
  * stejné patro, když ho oba uvádějí,
  * nový se objevil nejpozději 45 dní po zmizení starého a nejdřív 2 dny
    před ním (delší souběh = tentýž byt na dvou portálech, ne znovuvložení),
  * A ZÁROVEŇ se shoduje popis (podobnost textu ≥ 0,5).

Poslední podmínka je ta podstatná. V komplexu Pod Harfou je deset stání se
stejnou plochou a cenou -- bez popisu by se spárovala napříč. Když popis
chybí nebo se shoduje jen zčásti (0,3–0,5), ale sedí prodejce, je výsledek
„možná totéž": zobrazí se jako náznak, nic se neslučuje.

Deterministické, bez LLM, bez sítě. Nic se tady nemaže ani nepřepisuje --
výstupem je jen seznam párů; co s nimi, rozhoduje volající.
"""
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone

MAX_PRICE_DIFF = 0.10
MAX_DIST_KM = 0.15
MAX_GAP_DAYS = 45
MAX_OVERLAP_DAYS = 2
SAME_TEXT = 0.5
MAYBE_TEXT = 0.3
# Popis kratší než tohle je „Garáž k pronájmu, volné ihned" -- takový text se
# shoduje s každým druhým inzerátem a o identitě nic neříká.
MIN_WORDS = 12


def _ts(value):
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _km(a, b):
    la1, lo1, la2, lo2 = a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")
    if None in (la1, lo1, la2, lo2):
        return None
    la1, lo1, la2, lo2 = map(math.radians, (la1, lo1, la2, lo2))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


def _words(text):
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", text)


def text_similarity(a, b):
    """Jaccard na trojicích slov. None, když se nemá co porovnat -- to je
    jiná odpověď než 0 („popisy se liší")."""
    wa, wb = _words(a)[:250], _words(b)[:250]
    if len(wa) < MIN_WORDS or len(wb) < MIN_WORDS:
        return None
    sa = {tuple(wa[i:i + 3]) for i in range(len(wa) - 2)}
    sb = {tuple(wb[i:i + 3]) for i in range(len(wb) - 2)}
    return round(len(sa & sb) / len(sa | sb), 2)


def kind(item):
    """Dispozice u bytu. U garáží jedna třída: makléři tutéž věc zařazují
    jednou jako „garáž" a jindy jako „garážové stání" (Olgy Havlové: stejné
    stání, pronájem jako stání, prodej jako garáž), takže rozlišovat by znamenalo
    párovat podle toho, jak kdo klikl ve formuláři."""
    return "garaz" if item.get("garage_slug") else item.get("disposition")


def area_sqm(item):
    return item.get("usable_area_sqm") or item.get("floor_area_sqm")


def seller_key(item):
    name = " ".join(_words(item.get("seller_name")))
    return name or None


def same_seller(a, b):
    ka, kb = seller_key(a), seller_key(b)
    return bool(ka and ka == kb)


def close_enough(old, new):
    """Tvrdé podmínky, bez popisu. Všechny musí platit."""
    if old.get("transaction_type") != new.get("transaction_type"):
        return False
    if not kind(old) or kind(old) != kind(new):
        return False
    po, pn = old.get("price_czk"), new.get("price_czk")
    if not po or not pn or abs(pn - po) / po > MAX_PRICE_DIFF:
        return False
    ao, an = area_sqm(old), area_sqm(new)
    if not ao or not an:
        return False
    tolerance = 1.0 if old.get("garage_slug") else max(1.0, 0.03 * ao)
    if abs(an - ao) > tolerance:
        return False
    # Jiné patro je jiný byt, i když je text z téže šablony (Jeseniova: dva
    # byty jednoho makléře, 61 a 62 m², 1. a 3. patro, popis shodný na 0,92).
    fo, fn = old.get("floor_number"), new.get("floor_number")
    if fo is not None and fn is not None and fo != fn:
        return False
    d = _km(old, new)
    if d is not None:
        return d <= MAX_DIST_KM
    street = (old.get("street") or "").strip().lower()
    return bool(street) and street == (new.get("street") or "").strip().lower()


def timing_ok(old, new):
    """Nový se objevil po starém a nejpozději 45 dní po jeho zmizení. Starý
    musí být pryč: dva živé zároveň nejsou znovuvložení, ale dvojí inzerce."""
    gone = _ts(old.get("gone_at"))
    first_old, first_new = _ts(old.get("first_seen")), _ts(new.get("first_seen"))
    if gone is None or first_new is None:
        return False
    if first_old is not None and first_new <= first_old:
        return False
    # Souběh delší než pár dní = dvojí inzerce, ne znovuvložení. Backfill to
    # naměřil: 17 párů Sreality ↔ iDNES/Bezrealitky žilo souběžně 3 až 81 dní
    # (tentýž byt na dvou portálech), kdežto skutečná znovuvložení se
    # překrývají o hodiny -- nový je venku, starý se potvrdí jako pryč o
    # jeden až dva běhy později.
    if first_new < gone - timedelta(days=MAX_OVERLAP_DAYS):
        return False
    if new.get("gone_at") and _ts(new["gone_at"]) and _ts(new["gone_at"]) <= gone:
        return False
    return first_new <= gone + timedelta(days=MAX_GAP_DAYS)


def judge(old, new):
    """("same" | "maybe", podobnost) nebo None."""
    if str(old.get("id")) == str(new.get("id")):
        return None
    if not close_enough(old, new) or not timing_ok(old, new):
        return None
    sim = text_similarity(old.get("description"), new.get("description"))
    if sim is not None and sim >= SAME_TEXT:
        return "same", sim
    if same_seller(old, new) and (sim is None or sim >= MAYBE_TEXT):
        return "maybe", sim
    return None


def _closeness(old, new):
    po, pn = old.get("price_czk") or 0, new.get("price_czk") or 0
    ao, an = area_sqm(old) or 0, area_sqm(new) or 0
    return (abs(pn - po) / po if po else 1) + (abs(an - ao) / ao if ao else 1)


def match(gone, candidates):
    """Páry (starý, nový, verdikt, podobnost), každý inzerát nejvýš v jednom.

    Hladově od nejjistějšího: „same" před „maybe", pak podle podobnosti
    popisu. Kdyby se jeden starý hodil ke dvěma novým, vyhraje ten, jehož
    text sedí víc -- a druhý zůstane nespárovaný, ne přiřazený naslepo."""
    pairs = []
    for old in gone:
        for new in candidates:
            verdict = judge(old, new)
            if verdict:
                pairs.append((old, new, verdict[0], verdict[1]))
    # Při shodě textu rozhoduje, který kandidát je tomu starému blíž cenou a
    # plochou -- šablonový popis jednoho makléře sedí na všechny jeho byty
    # v domě stejně, a bez tohohle se dva sousední byty spárovaly křížem.
    pairs.sort(key=lambda p: (p[2] != "same", -(p[3] or 0), _closeness(p[0], p[1])))
    used_old, used_new, out = set(), set(), []
    for old, new, verdict, sim in pairs:
        if str(old["id"]) in used_old or str(new["id"]) in used_new:
            continue
        used_old.add(str(old["id"]))
        used_new.add(str(new["id"]))
        out.append((old, new, verdict, sim))
    return out


def link_record(old, verdict, sim):
    """Co si nový inzerát pamatuje o tom, který nahradil."""
    return {
        "id": old.get("id"),
        "verdict": verdict,
        "text_similarity": sim,
        "url": old.get("url"),
        "first_seen": old.get("first_seen"),
        "gone_at": old.get("gone_at"),
        "price_czk": old.get("price_czk"),
        # Když byl sám znovuvložením, řetěz se neztratí: nejstarší datum
        # nabídky se nese dál.
        "listed_since": (old.get("relist_of") or {}).get("listed_since") or old.get("first_seen"),
    }


# ----------------------------------------------------------------------------
# Tentýž prodejce nabízí totéž k pronájmu i k prodeji
# ----------------------------------------------------------------------------
PAIR_DIST_KM = 0.1


def rent_sale_pairs(items):
    """(prodej, pronájem) pro garáže, které tentýž prodejce nabízí oběma
    způsoby: stejný prodejce, plocha ±1 m², do 100 m (nebo
    stejná ulice bez GPS). Každá strana nejvýš v jednom páru -- když sedí víc
    kandidátů, vyhraje nejbližší."""
    sales = [g for g in items if g.get("transaction_type") == "prodej"]
    rents = [g for g in items if g.get("transaction_type") == "pronajem"]
    cands = []
    for s in sales:
        for r in rents:
            if not same_seller(s, r) or kind(s) != kind(r):
                continue
            a_s, a_r = area_sqm(s), area_sqm(r)
            if not a_s or not a_r or abs(a_s - a_r) > 1.0:
                continue
            d = _km(s, r)
            if d is None:
                st = (s.get("street") or "").strip().lower()
                if not st or st != (r.get("street") or "").strip().lower():
                    continue
                d = 0.0
            elif d > PAIR_DIST_KM:
                continue
            cands.append((d, s, r))
    cands.sort(key=lambda c: c[0])
    used, out = set(), []
    for _d, s, r in cands:
        if str(s["id"]) in used or str(r["id"]) in used:
            continue
        used.update((str(s["id"]), str(r["id"])))
        out.append((s, r))
    return out
