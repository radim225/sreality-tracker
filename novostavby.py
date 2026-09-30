"""Novostavby 4+kk / 5+kk kolem U Kříže (Jinonice, park Waltrovka).

Radim (27. 9. 2026): „přidej trackování na Sreality u nových bytů u parku
Waltrovka Jinonice a u Kříže, novostavby 4+kk, 5+kk, když se prodávají a
pronajímají — za kolik a jak dlouho trvá, než zmizí. Nová sekce. Ještě ulice
Kohoutových a Bochovská, a Na Pomezí. Cca u Kříže poloměr kilometr, aby tam
vše spadalo. A abych v appce viděl přesně ten okruh a mohl si ho změnit."

Samostatná kolekce po vzoru garáží, ne rozšíření `comparables`: 4+kk a 5+kk
v DISPOSITION_CODES nejsou, a kdyby se sem dostaly, vstoupily by do mediánů
bytů, do poolu i do odhadu nájmu. Vlastní klíč ve snapshotu (`novostavby`),
vlastní karta, vlastní alert.

Rozdělení práce:
  * scrape.py sbírá (hledání po čtvrtích, detail nového inzerátu a pak jednou
    za týden, ověření zmizení přes 404) -- potřebuje jeho HTTP vrstvu s retry.
  * tady je všechno ostatní a je to čisté: sloučení s minulým během, baseline,
    třídění dokončené/výstavba/starší, dny na trhu, statistika, text alertu,
    karta a JS. Testovatelné bez sítě.

Modul záměrně neimportuje scrape.py (scrape.py importuje jeho).

Sbírá se NADMNOŽINA (SUPERSET_KM kolem U Kříže); kruh na stránce je jen
filtr zobrazení, který si Radim táhne a mění. Alerty ale chodí pro pevný
výchozí kruh -- server localStorage prohlížeče nevidí.
"""
import html
import math
import re
import statistics
import sys
import unicodedata
from datetime import datetime

import gone_archive
import photo_archive

# --- Kde --------------------------------------------------------------------- #
# Geokódováno 27. 9. 2026 přes Nominatim (OSM), včetně geometrie ulic, aby šlo
# říct, jak daleko je NEJVZDÁLENĚJŠÍ bod každého místa, ne jen jeho střed.
# Vzdálenosti od středu (U Kříže):
#   U Kříže (celá ulice)            0.00–0.20 km
#   Kohoutových                     0.16 (nejdál 0.24)
#   Bochovská                       0.19 (nejdál 0.30)
#   Na Pomezí                       0.47 (nejdál 0.89 -- severní konec v Košířích)
#   Park Waltrovka                  0.76 (nejdál 0.87)
#   Waltrovka (čtvrť, OSM bod)      0.62
#   Nová Waltrovka (nová zástavba)  1.09   <- jediné, co se do 1 km nevejde
# Radim řekl „cca kilometr, aby tam vše spadalo" -- kilometr by uřízl Novou
# Waltrovku, tedy právě tu novou zástavbu u parku. Výchozí poloměr je proto
# 1,2 km: pokryje ji i s rezervou ~100 m na půdorys domů (GPS inzerátu je dům,
# ne bod čtvrti). Víc ne -- ve 1,2 km už leží náměstí Augustina Bubníka.
CENTER = (50.0541, 14.3674)           # U Kříže, úsek Waltrovka/Jinonice
CENTER_LABEL = "U Kříže"
DEFAULT_RADIUS_KM = 1.2
# Alerty chodí pro tenhle kruh a jen pro něj. Jedna konstanta vedle výchozího
# kruhu, ať se nerozjedou: posuvník na stránce alerty nemění.
ALERT_CENTER = CENTER
ALERT_RADIUS_KM = DEFAULT_RADIUS_KM
# Co se sbírá: všechno do 2 km. Posuvník na stránce jde do tohohle maxima;
# za přerušovanou hranicí data nejsou. 2 km stojí 14 stránek hledání za běh
# (7 čtvrtí × prodej/pronájem, v každé ≤ 22 výsledků) -- levné.
SUPERSET_KM = 2.0
# Čtvrti, kterých se 2km kruh dotýká. Ověřeno 27. 9. proti Sreality: všech sedm
# jmen se rozpozná jako „městská část …" (localityEntityType ward). Smíchov a
# Motol dnes do 2 km nepřispívají ničím, ale kruh jejich okraje protíná a
# stojí dohromady 4 requesty.
WARDS = ["Jinonice", "Radlice", "Košíře", "Stodůlky", "Hlubočepy", "Smíchov", "Motol"]
TRANSACTIONS = ("prodej", "pronajem")

# --- Co ---------------------------------------------------------------------- #
# categorySubCb kódy Sreality, ověřené živě 27. 9. (estatesFilterPage):
#   8 = 4+kk, 9 = 4+1, 10 = 5+kk, 11 = 5+1, 12 = 6 a více, 16 = atypický.
# Dispozice se bere VÝHRADNĚ z kódu, nikdy z názvu (Sreality občas odpoví
# anglicky a „4+kk" pak přijde jako „4+kt").
DISPOSITIONS = {8: "4+kk", 10: "5+kk"}
VELIKOST = "4+kk,5+kk"
# --- Co je „nové" ------------------------------------------------------------ #
# Do 27. 9. odpoledne se filtrovalo na serveru štítkem „Novostavba"
# (stav=novostavby). Ukázalo se, že štítek lže oběma směry: Kohoutových 5+kk
# píše v popisu „novostavba", ale štítek má „Velmi dobrý"; Naskové 4+kk má
# štítek „Novostavba" a kolaudaci 2019. Radim: „ideálně dostavěné v roce 2020+
# a možnost vidět filtr. Novostavba z roku 2000 opravdu není. Klidně i ve
# výstavbě, ale hlavně už ty dokončené, které se tváří jako novostavba."
#
# Sbírá se proto KAŽDÝ 4+kk/5+kk v nadmnožině a z detailu se určí `kind`:
#   dokoncena  rok kolaudace ≥ 2020 (a ne v budoucnu), nebo štítek Novostavba
#              bez uvedeného roku (`year_unknown`, „rok neuveden")
#   vystavba   štítek „Ve výstavbě" / „Projekt", nebo kolaudace v budoucnu
#   starsi     všechno ostatní -- kolaudace < 2020 (i se štítkem Novostavba),
#              „velmi dobrý"/„dobrý"/panel bez roku ≥ 2020
#
# Rok: Sreality má v detailu jen dvě roková pole (ověřeno 27. 9. na všech 39
# inzerátech 4+kk/5+kk do 2 km): `acceptanceYear` = rok kolaudace a
# `reconstructionYear` = rok rekonstrukce. „Rok výstavby" jako pole neexistuje.
# Rekonstrukce se za novost NEPOČÍTÁ (Průchova: „velmi dobrý", rekonstrukce
# 2025 -- starý dům). Kolaudace 2029 u Radlické se štítkem Novostavba je
# plánovaná -- dům stojí teprve ve výstavbě.
# Kódy buildingCondition (estatesFilterPage): 1 velmi dobrý, 2 dobrý,
# 3 špatný, 4 ve výstavbě, 5 projekt, 6 novostavba, 7 k demolici,
# 8 před rekonstrukcí, 9 po rekonstrukci, 10 v rekonstrukci.
NEW_FROM_YEAR = 2020
CONDITION_NEW = 6
CONDITIONS_BUILDING = {4: "ve výstavbě", 5: "projekt"}
CONDITION_LABELS = {1: "velmi dobrý", 2: "dobrý", 3: "špatný", 4: "ve výstavbě", 5: "projekt",
                    6: "novostavba", 7: "k demolici", 8: "před rekonstrukcí",
                    9: "po rekonstrukci", 10: "v rekonstrukci"}
KINDS = ("dokoncena", "vystavba", "starsi")
KIND_LABELS = {"dokoncena": "dokončená 2020+", "vystavba": "ve výstavbě / projekt",
               "starsi": "starší"}
# Alert jen pro tyhle; starší byty Radima nezajímají.
ALERT_KINDS = ("dokoncena", "vystavba")
# Verze klasifikace -- je v otisku konfigurace, takže její změna proběhne
# jako tichá baseline, ne jako záplava „nových" inzerátů.
#
# 27. 9. večer: štítek „novostavba" bez roku + budoucí dokončení v popisu =
# výstavba (desc_completion). Verze ZÁMĚRNĚ zůstává 1: přetřídění už
# sledovaného inzerátu se jen zaznamená (kind_changed_at), nikdy nehlásí, a
# obě dotčené třídy (dokončená, výstavba) jsou v ALERT_KINDS -- záplava
# nehrozí. Zvýšení by naopak udělalo z příštího běhu tichou baseline a
# spolklo by skutečně nové inzeráty z toho běhu.
CLASSIFIER_VERSION = 1

# Pojmenovaná místa -- špendlíky na mapě a vlajka u řádku tabulky.
LANDMARKS = [
    {"name": "U Kříže", "lat": 50.0541, "lon": 14.3674},
    {"name": "Kohoutových", "lat": 50.05350, "lon": 14.36530},
    {"name": "Bochovská", "lat": 50.05273, "lon": 14.36587},
    {"name": "Na Pomezí", "lat": 50.05825, "lon": 14.36592},
    {"name": "Park Waltrovka", "lat": 50.05756, "lon": 14.37658},
    {"name": "Nová Waltrovka", "lat": 50.05720, "lon": 14.38183},
]
# Ulice se porovnávají bez diakritiky a velikosti písmen: Sreality píše
# „U kříže" i „Na pomezí".
NAMED_STREETS = ("U Kříže", "Kohoutových", "Bochovská", "Na Pomezí")
# Waltrovka nemá jednu ulici; inzerát do 300 m od parku / Nové Waltrovky nebo
# s „Waltrovka" v popisu (např. „Rezidence Waltrovka", Kačírkova) se označí.
WALTROVKA_POINTS = [(50.05756, 14.37658), (50.05720, 14.38183), (50.05742, 14.37434)]
WALTROVKA_NEAR_KM = 0.3

MAX_ALERT_LINES = 25


# --- Pomocné ----------------------------------------------------------------- #
def haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    la1, lo1, la2, lo2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(a))


def km_from_center(lat, lon):
    d = haversine_km(lat, lon, CENTER[0], CENTER[1])
    return round(d, 2) if d is not None else None


def in_superset(rec):
    km = rec.get("km")
    return km is not None and km <= SUPERSET_KM


def _fold(s):
    """Malá písmena bez diakritiky -- „U kříže" == „U Kříže"."""
    s =unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


_NAMED = {_fold(s): s for s in NAMED_STREETS}


def named_place(rec):
    """Které z Radimových míst inzerát je, nebo None."""
    street = _NAMED.get(_fold(rec.get("street")))
    if street:
        return street
    if rec.get("mentions_waltrovka"):
        return "Waltrovka"
    lat, lon = rec.get("lat"), rec.get("lon")
    for p in WALTROVKA_POINTS:
        d = haversine_km(lat, lon, p[0], p[1])
        if d is not None and d <= WALTROVKA_NEAR_KM:
            return "Waltrovka"
    return None


def fingerprint():
    """Tvar toho, CO se sbírá. Změní-li se (jiné čtvrti, širší nadmnožina,
    další dispozice), běh proběhne jako tichá baseline: nové záznamy dostanou
    `baseline: true` a žádný alert neodejde -- naše změna konfigurace se nesmí
    hlásit jako pohyb trhu. Výchozí/alertový poloměr sem nepatří: nemění, co
    se sbírá, jen co se zobrazuje."""
    return {
        "center": list(CENTER),
        "superset_km": SUPERSET_KM,
        "dispositions": sorted(DISPOSITIONS.values()),
        # Od 27. 9. se sbírá bez filtru stavu a třídí se až tady; změna
        # pravidel třídění mění, co je „nové", takže patří do otisku.
        "condition": None,
        "classifier": CLASSIFIER_VERSION,
        "new_from_year": NEW_FROM_YEAR,
        "wards": sorted(WARDS),
        "transactions": sorted(TRANSACTIONS),
    }


def _year(v, now_year):
    """Rok z detailu, nebo None. Sreality občas vrátí nesmysl (0, 20225)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = int(v)
    return v if 1800 <= v <= now_year + 15 else None


def classify(rec, now):
    """(kind, reason, year_unknown) podle detailu. Záznam bez načteného detailu
    je „neurceno" -- hádat z titulku nejde a do alertu nesmí."""
    if not rec.get("detail_read"):
        return "neurceno", "detail zatím nenačten", False
    t = parse_iso(now)
    now_year = t.year if t else datetime.now().year
    cond = rec.get("building_condition")
    cond_txt = CONDITION_LABELS.get(cond)
    year = _year(rec.get("acceptance_year"), now_year)
    recon = _year(rec.get("reconstruction_year"), now_year)
    extra = []
    if recon:
        extra.append(f"rekonstrukce {recon}")
    if rec.get("desc_mentions_new") and cond != CONDITION_NEW:
        extra.append("popis zmiňuje novostavbu")
    tail = (" · " + ", ".join(extra)) if extra else ""

    if cond in CONDITIONS_BUILDING:
        reason = f"stav: {cond_txt}" + (f", kolaudace {year}" if year else "")
        return "vystavba", reason + tail, False
    if year is not None:
        if year > now_year:
            return "vystavba", f"kolaudace plánována {year}" + tail, False
        if year >= NEW_FROM_YEAR:
            return "dokoncena", f"kolaudace {year}" + tail, False
        label = " (štítek novostavba)" if cond == CONDITION_NEW else ""
        return "starsi", f"kolaudace {year}{label}" + tail, False
    if cond == CONDITION_NEW:
        # Štítek „novostavba" bez roku kolaudace, ale popis říká, že dům
        # teprve stojí (Na Hutmance 27. 9.: „Předpokládaný termín dokončení
        # Q4/2027"). Popis je tu jediný zdroj data -- a mluví o budoucnosti.
        comp = rec.get("desc_completion")
        if isinstance(comp, dict) and comp.get("text") and (
                comp.get("year") is None or completion_is_future(comp, t)):
            return "vystavba", f"popis: {comp['text']}" + tail, False
        return "dokoncena", "stav: novostavba, rok neuveden" + tail, True
    return "starsi", f"stav: {cond_txt or 'neuveden'}, rok neuveden" + tail, False


# --- Dokončení z popisu ------------------------------------------------------ #
# Konzervativně: rok se bere JEN v téže větě hned za (nebo těsně před)
# slovem o dokončení/kolaudaci/nastěhování -- ne jakýkoli rok v textu
# („rekonstrukce 2027", „sleva do 2027" ani „při koupi do 30. 9. 2026" nic
# neznamenají). Rozhoduje až classify() podle data běhu, takže uložená
# zmínka „Q4/2027" sama zestárne v dokončenou, až to období přijde.
_COMPLETION_KW = re.compile(
    r"\b(?:dokonč\w*|zkolaudov\w*|kolaudac\w*|kolaudov\w*|nastěhov\w*|"
    r"předání\s+(?:bytu|bytů|jednotek|klíčů)|předán\w*\s+(?:bytu|bytů|klíčů))", re.I)
# „Dům je ve výstavbě" -- bez roku, ale s podmětem nebo příslovcem „teď";
# holé „ve výstavbě" (škola v okolí, metro) se nepočítá.
_UNDER_CONSTRUCTION = re.compile(
    r"\b(?:(?:dům|budova|projekt|objekt|stavba|rezidence|byt)\s+(?:je\s+)?"
    r"(?:(?:aktuálně|momentálně|nyní|teprve|stále|právě)\s+)?ve\s+výstavbě"
    r"|(?:aktuálně|momentálně|nyní|teprve|v\s+současné\s+době)\s+ve\s+výstavbě)", re.I)
_SENTENCE_END = re.compile(r"[.!?]\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ])|\n")
_DAY_DATE = re.compile(r"(?<!\d)\d{1,2}\.\s*\d{1,2}\.\s*20\d\d")
_YEAR = re.compile(r"(?<!\d)(20[2-4]\d)(?!\d)")
_MONTH_STEMS = [("led", 1), ("únor", 2), ("břez", 3), ("dub", 4), ("květ", 5), ("červenc", 7),
                ("červn", 6), ("červen", 6), ("srp", 8), ("září", 9), ("říj", 10),
                ("listopad", 11), ("prosin", 12),
                ("jař", 3), ("jar", 3), ("lét", 6), ("léto", 6), ("podzim", 9), ("zim", 12)]


def _period_month(before):
    """Měsíc (první měsíc období) z textu těsně před rokem, nebo None."""
    tail = before[-25:].lower()
    m = re.search(r"q\s*([1-4])\s*[/.\-]?\s*(?:roku\s+)?$", tail)
    if m:
        return (int(m.group(1)) - 1) * 3 + 1
    m = re.search(r"([1-4])\.\s*(?:čtvrtlet\w*|kvartál\w*)\s*(?:roku\s+)?$", tail)
    if m:
        return (int(m.group(1)) - 1) * 3 + 1
    m = re.search(r"(?<!\d)(\d{1,2})\s*[./]\s*$", tail)
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(1))
    m = re.search(r"(\w+)\s+(?:roku\s+|r\.\s*)?$", tail)
    if m:
        word = m.group(1)
        for stem, month in _MONTH_STEMS:
            if word.startswith(stem):
                return month
    return None


def completion_from_description(text, rental=False):
    """Nejpozdější zmínka o dokončení v popisu: {"year", "month", "text"},
    {"year": None, "month": None, "text": "ve výstavbě"} pro „dům je ve
    výstavbě" bez roku, nebo None. Nezávislé na datu -- ukládá se k záznamu.

    „Nastěhování" je u pronájmu termín volnosti bytu („nastěhování od
    1.11.2026"), ne dokončení domu -- u pronájmu se proto nebere vůbec a
    jinde ne s přesným datem dne."""
    text = str(text or "")
    found = []
    for kw in _COMPLETION_KW.finditer(text):
        move_in = kw.group(0).lower().startswith("nastěhov")
        if move_in and rental:
            continue
        after = text[kw.end():kw.end() + 60]
        cut = _SENTENCE_END.search(after)
        if cut:
            after = after[:cut.start()]
        if move_in and _DAY_DATE.search(after):
            continue
        y = _YEAR.search(after)
        if y:
            start, end = kw.start(), kw.end() + y.end()
            before = after[:y.start()]
        else:
            # „v roce 2027 bude dokončen": rok těsně před slovem, v téže větě.
            pre_start = max(0, kw.start() - 40)
            pre = text[pre_start:kw.start()]
            cuts = list(_SENTENCE_END.finditer(pre))
            if cuts:
                pre_start += cuts[-1].end()
                pre = text[pre_start:kw.start()]
            ys = list(_YEAR.finditer(pre))
            if not ys:
                continue
            y = ys[-1]
            before = pre[:y.start()]
            start, end = pre_start + y.start(), kw.end()
        year = int(y.group(1))
        snippet = " ".join(text[start:end].split())
        if len(snippet) > 60:
            snippet = f"{kw.group(0)} … {year}"
        found.append({"year": year, "month": _period_month(before), "text": snippet})
    if found:
        return max(found, key=lambda c: (c["year"], c["month"] or 0))
    if _UNDER_CONSTRUCTION.search(text):
        return {"year": None, "month": None, "text": "ve výstavbě"}
    return None


def completion_is_future(comp, t):
    """Leží zmínka po datu běhu `t`? Rok > letošní, nebo letos s pozdějším
    měsícem/čtvrtletím. Letošní rok bez období ani minulé roky nic nepřeklápí."""
    if t is None or not isinstance(comp.get("year"), int):
        return False
    if comp["year"] > t.year:
        return True
    month = comp.get("month")
    return comp["year"] == t.year and isinstance(month, int) and month > t.month


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def days_between(a, b):
    """Celé dny, zaokrouhlené stejně jako daysBetween() v JS (Math.round)."""
    t1, t2 = parse_iso(a), parse_iso(b)
    if t1 is None or t2 is None:
        return None
    return max(0, math.floor((t2 - t1).total_seconds() / 86400 + 0.5))


def is_live(rec):
    return not rec.get("gone_at") and not rec.get("left_filter_at")


_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def since_iso(rec):
    """`since` (datum vložení podle Sreality, „2026-04-27") jako ISO půlnoc
    UTC, nebo None. Nic jiného než tvar YYYY-MM-DD se nebere -- je to
    scrapovaný řetězec."""
    m = _DAY_RE.match(str(rec.get("since") or ""))
    if not m:
        return None
    try:
        datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T00:00:00Z"


def market_start(rec):
    """(start, source) -- odkdy se počítají dny na trhu.

    Po potvrzeném zmizení a návratu začíná nový souvislý úsek `returned_at`.
    Radim (27. 9.): „≥ 0 d" u všech řádků po baseline nic neříká. Sreality
    v detailu uvádí vlastní datum vložení (`since`); je to nejlepší, co máme,
    i když se může vynulovat, když makléř inzerát smaže a vloží znovu (pak je
    číslo spíš podhodnocené). Bere se DŘÍVĚJŠÍ z `since` a našeho prvního
    výskytu -- oba jsou důkaz, že inzerát v tu chvíli žil.
      "sreality"  start je `since`
      "ours"      start je náš first_seen a inzerát jsme viděli přibýt
      "baseline"  start je first_seen z baseline -- skutečné stáří neznáme,
                  číslo je jen dolní mez („≥")"""
    # Po potvrzeném 404 začíná nový souvislý úsek nabídky. Staré `since`
    # může Sreality zachovat, ale nedokazuje dostupnost během výpadku.
    if rec.get("returned_at"):
        return rec["returned_at"], "ours"
    first = rec.get("first_seen")
    since = since_iso(rec)
    t_first, t_since = parse_iso(first), parse_iso(since)
    if t_since is not None and (t_first is None or t_since <= t_first):
        return since, "sreality"
    return first, ("baseline" if rec.get("baseline") else "ours")


def days_on_market(rec, now):
    """{"days", "lower_bound", "live", "source"}.

    Od market_start() do gone_at u zmizelého, do teď u živého. Dolní mez
    (`lower_bound`, stránka píše „≥ N d") jen tehdy, když start je baseline
    a Sreality datum neuvádí."""
    live = is_live(rec)
    end = rec.get("gone_at") if rec.get("gone_at") else now
    start, source = market_start(rec)
    return {
        "days": days_between(start, end),
        "lower_bound": source == "baseline",
        "live": live,
        "source": source,
    }


# --- Sloučení s minulým během ------------------------------------------------ #
# Pole, která přišla z detailu a nesou se dál, dokud se detail nepřečte znovu
# (scrape.py ho čte u nového inzerátu a pak jednou za DETAIL_TTL_DAYS).
DETAIL_FIELDS = ("since", "detail_area_sqm", "mentions_waltrovka", "address_exact",
                 "building_condition", "acceptance_year", "reconstruction_year",
                 "desc_mentions_new", "detail_read", "detail_read_at", "detail_version",
                 "description", "images", "seller_name", "floor_number", "floors_total",
                 "desc_completion")
# 2 = rok kolaudace/rekonstrukce a zmínka „novostavba" v popisu (27. 9.).
# 3 = popis, fotky, prodejce a patro pro detail inzerátu na stránce, a
#     `desc_completion` (plánované dokončení z popisu, viz classify) (27. 9.).
#     Záznamy z verze 2 se tím jednou přečtou znovu -- v rámci běžného stropu
#     MAX_NOVOSTAVBY_DETAIL_FETCHES (60; dnes 39 záznamů, tedy jeden běh).
#     Otisk konfigurace se nemění, takže žádná baseline ani alerty: opětovné
#     čtení detailu mění nanejvýš typ, a ten se jen zaznamená.
DETAIL_VERSION = 3
# Stav stavby se mění (výstavba -> hotovo, prodejce opraví rok), takže živý
# inzerát se jednou za týden přečte znovu. ~40 inzerátů / 42 běhů týdně = ~1
# detail za běh.
DETAIL_TTL_DAYS = 7


def needs_detail(rec, prev, now):
    """Číst detail teď? Nový, starší verze detailu, nebo po DETAIL_TTL_DAYS."""
    src = prev or {}
    if not src.get("detail_read") or (src.get("detail_version") or 1) < DETAIL_VERSION:
        return True
    age = days_between(src.get("detail_read_at"), now)
    return age is None or age >= DETAIL_TTL_DAYS


def merge(prev_records, seen, now, *, baseline, verify=None, max_checks=30):
    """Sloučí záznamy z minulého běhu s tím, co našlo hledání teď.

    `seen`     -- záznamy z hledání (už odfiltrované na nadmnožinu).
    `baseline` -- tichý běh (první, nebo po změně konfigurace): nové záznamy
                  dostanou baseline: true a žádné události nevzniknou.
    `verify`   -- funkce(rec) -> "gone" | "live" | "left_filter" | "unknown",
                  ptá se detailu inzerátu. Zmizelý je jen „gone" (= 404).
                  Absence ve výsledcích hledání sama nic neznamená.

    Vrací (records, events). Události: {"kind": "new"|"gone"|"price", "rec",
    "old_price"?}."""
    prev_by_id = {str(r["id"]): r for r in (prev_records or [])}
    seen_ids = set()
    out, events = [], []

    for rec in seen:
        rid = str(rec["id"])
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        prev = prev_by_id.get(rid)
        rec = dict(rec)
        if prev:
            rec["first_seen"] = prev.get("first_seen") or now
            rec["baseline"] = bool(prev.get("baseline"))
            history = list(prev.get("price_history") or [])
            # Čerstvě přečtený detail je autorita -- i jeho None (prodejce
            # rok smazal) má přebít starou hodnotu.
            if not rec.get("detail_fresh"):
                for k in DETAIL_FIELDS:
                    if rec.get(k) is None and prev.get(k) is not None:
                        rec[k] = prev[k]
            for k in ("kind_changed_at", "kind_before"):
                if prev.get(k) is not None:
                    rec[k] = prev[k]
            if prev.get("kind") and prev.get("kind") != "neurceno":
                rec["_prev_kind"] = prev["kind"]
            # Plocha z detailu (titulek ji nemá) -- detail se čte jen jednou,
            # takže bez tohohle by m² a Kč/m² od druhého běhu zmizely.
            area = rec.get("detail_area_sqm")
            if not rec.get("floor_area_sqm") and isinstance(area, (int, float)) and area > 0:
                rec["floor_area_sqm"] = float(area)
                if rec.get("price_czk") and not rec.get("price_czk_per_sqm"):
                    rec["price_czk_per_sqm"] = round(rec["price_czk"] / area)
            old = prev.get("price_czk") or prev.get("last_known_price_czk")
            new = rec.get("price_czk")
            if new:
                rec["last_known_price_czk"] = new
            elif old:
                rec["last_known_price_czk"] = old
            if new != old and new:
                history.append({"at": now, "price_czk": new})
                rec["price_old_czk"] = old
                if old and is_live(prev) and not baseline:
                    events.append({"kind": "price", "rec": rec, "old_price": old})
            else:
                rec["price_old_czk"] = prev.get("price_old_czk")
            rec["price_history"] = history
            if prev.get("gone_at"):
                rec["returned_at"] = now
                rec["gone_before_at"] = prev["gone_at"]
                if not baseline:
                    events.append({"kind": "returned", "rec": rec})
            elif prev.get("returned_at"):
                rec["returned_at"] = prev["returned_at"]
                rec["gone_before_at"] = prev.get("gone_before_at")
        else:
            rec["first_seen"] = now
            rec["baseline"] = bool(baseline)
            rec["price_history"] = [{"at": now, "price_czk": rec.get("price_czk")}] \
                if rec.get("price_czk") else []
            if not baseline:
                events.append({"kind": "new", "rec": rec})
        rec["last_seen"] = now
        rec["gone_at"] = None
        rec.pop("missing_from_search", None)
        rec.pop("left_filter_at", None)
        rec.pop("out_of_scope", None)
        out.append(rec)

    checks = 0
    for rid, prev in prev_by_id.items():
        if rid in seen_ids:
            continue
        rec = dict(prev)
        if rec.get("gone_at") or rec.get("left_filter_at"):
            out.append(rec)      # už rozhodnuto, nic se neověřuje znovu
            continue
        if not in_superset(rec):
            # Mimo to, co teď sbíráme (zúžená konfigurace). Nemaže se a
            # neprohlašuje za zmizelé -- jen se přestane sledovat.
            rec["out_of_scope"] = True
            out.append(rec)
            continue
        verdict = "unknown"
        if verify is not None and checks < max_checks:
            checks += 1
            try:
                verdict = verify(rec)
            except Exception as exc:  # noqa: BLE001 -- nejistota = nechat živý
                print(f"  novostavba {rid}: ověření selhalo ({exc})", file=sys.stderr)
                verdict = "unknown"
        if verdict == "gone":
            rec["gone_at"] = now
            rec.pop("missing_from_search", None)
            rec["days_on_market"] = days_on_market(rec, now)["days"]
            if not baseline:
                events.append({"kind": "gone", "rec": rec})
        elif verdict == "left_filter":
            # Inzerát žije, ale už není 4+kk/5+kk (prodejce změnil
            # dispozici). Není to zmizení a do dní na trhu nepatří.
            rec["left_filter_at"] = now
            rec.pop("missing_from_search", None)
        else:
            rec["missing_from_search"] = rec.get("missing_from_search") or now
        out.append(rec)

    for rec in out:
        if rec.get("gone_at"):
            rec["days_on_market"] = days_on_market(rec, now)["days"]
        else:
            rec.pop("days_on_market", None)
        rec["named_place"] = named_place(rec)
        kind, reason, year_unknown = classify(rec, now)
        prev_kind = rec.pop("_prev_kind", None)
        if prev_kind and kind != "neurceno" and kind != prev_kind:
            # Zaznamená se, ale nehlásí: výstavba -> dokončená je změna štítku
            # u inzerátu, který Radim už viděl, ne nová nabídka.
            rec["kind_changed_at"] = now
            rec["kind_before"] = prev_kind
        rec["kind"], rec["kind_reason"], rec["year_unknown"] = kind, reason, year_unknown
        prev = prev_by_id.get(str(rec["id"]))
        if (prev and prev.get("kind") == "neurceno" and not prev.get("detail_read")
                and not prev.get("baseline") and not baseline and kind in ALERT_KINDS
                and is_live(rec)):
            # První výskyt už byl zapsaný, ale strop detailů nedovolil
            # rozhodnout typ. Zpráva se pošle teprve teď, při prvním ověření;
            # případná změna ceny patří do téhož oznámení o nové nabídce.
            events = [e for e in events if e["rec"]["id"] != rec["id"] or e["kind"] != "price"]
            events.append({"kind": "new", "rec": rec})
        rec.pop("detail_fresh", None)
    out.sort(key=lambda r: (r.get("gone_at") is not None, r.get("transaction_type") or "",
                            r.get("disposition") or "", r.get("km") or 0))
    return out, events


# --- Statistika -------------------------------------------------------------- #
def in_circle(rec, center, radius_km):
    d = haversine_km(rec.get("lat"), rec.get("lon"), center[0], center[1])
    return d is not None and d <= radius_km + 1e-9


def compute_stats(records, now, center=CENTER, radius_km=DEFAULT_RADIUS_KM, kinds=("dokoncena",)):
    """Po transakci × dispozici, jen uvnitř kruhu a jen zvolených typů. Stejný
    výpočet dělá JS na stránce pro kruh a typy, které si Radim nastaví; tady
    je pro výchozí nastavení (dokončené 2020+, 1,2 km) -- do snapshotu a pro
    testy. `kinds=None` = všechny."""
    out = {}
    recs = [r for r in records if not r.get("out_of_scope") and in_circle(r, center, radius_km)
            and (kinds is None or r.get("kind") in kinds) and not r.get("exclude_from_stats")]
    for tx in TRANSACTIONS:
        for disp in DISPOSITIONS.values():
            group = [r for r in recs if r.get("transaction_type") == tx and r.get("disposition") == disp]
            live = [r for r in group if is_live(r)]
            gone = [r for r in group if r.get("gone_at")]
            prices = sorted(r["price_czk"] for r in live if r.get("price_czk"))
            per_sqm = sorted(r["price_czk_per_sqm"] for r in live if r.get("price_czk_per_sqm"))
            gone_dom = [days_on_market(r, now) for r in gone]
            gone_days = [d["days"] for d in gone_dom if d["days"] is not None]
            out[f"{tx}_{disp}"] = {
                "live_n": len(live),
                "median_price_czk": round(statistics.median(prices)) if prices else None,
                "median_czk_per_sqm": round(statistics.median(per_sqm)) if per_sqm else None,
                "gone_n": len(gone),
                "median_days_to_gone": round(statistics.median(gone_days)) if gone_days else None,
                "min_days_to_gone": min(gone_days) if gone_days else None,
                "max_days_to_gone": max(gone_days) if gone_days else None,
                # Kterýkoli zmizelý bez známého začátku (baseline bez data
                # Sreality) dělá z mediánu dolní mez.
                "days_lower_bound": any(d["lower_bound"] for d in gone_dom),
                "gone_last_prices_czk": [r.get("price_czk") for r in
                                         sorted(gone, key=lambda r: r.get("gone_at") or "", reverse=True)
                                         if r.get("price_czk")][:5],
            }
    return out


# --- Alert ------------------------------------------------------------------- #
def _czk(v):
    if not isinstance(v, (int, float)) or not v:
        return "cena na dotaz"
    return f"{int(round(v)):,}".replace(",", " ") + " Kč"


def _km(v):
    return "? km" if v is None else f"{v:.1f}".replace(".", ",") + " km"


def _tx(rec):
    return "pronájem" if rec.get("transaction_type") == "pronajem" else "prodej"


def _safe_url(u):
    return u if isinstance(u, str) and u.startswith("https://") else None


def alert_events(events):
    """Jen události uvnitř výchozího (alertového) kruhu a jen dokončené 2020+
    a ve výstavbě. Starší byty ani neověřené („neurceno") se nehlásí nikdy."""
    return [e for e in events if in_circle(e["rec"], ALERT_CENTER, ALERT_RADIUS_KM)
            and e["rec"].get("kind") in ALERT_KINDS]


def kind_tag(rec):
    """Krátký štítek typu do alertu: [dokončená · kolaudace 2025]."""
    kind = rec.get("kind")
    label = {"dokoncena": "dokončená", "vystavba": "výstavba"}.get(kind, kind or "?")
    reason = rec.get("kind_reason") or ""
    return f"[{label}" + (f" · {reason.split(' · ')[0]}" if reason else "") + "]"


def build_alert(events, now, dashboard_url=None):
    """Jedna seskupená zpráva (Telegram HTML), nebo None, když není co hlásit.

    Každý scrapovaný řetězec projde html.escape -- Telegram HTML je markup a
    ulice s „<" by zprávu rozbila (nebo hůř). Kontakty tu nejsou: zpráva popis ani
    prodejce nepoužívá, jen ulici, cenu a odkaz."""
    events = alert_events(events)
    if not events:
        return None
    order = {"new": 0, "returned": 1, "price": 2, "gone": 3}
    events = sorted(events, key=lambda e: (order[e["kind"]], e["rec"].get("km") or 0))
    radius = f"{ALERT_RADIUS_KM:.1f}".replace(".", ",")
    lines = [f"<b>🏗️ Nové byty 4+kk / 5+kk · {CENTER_LABEL} ≤ {radius} km</b>"]
    for e in events[:MAX_ALERT_LINES]:
        r = e["rec"]
        unit = "/měs" if r.get("transaction_type") == "pronajem" else ""
        where = html.escape(r.get("street") or r.get("city_part") or "?")
        if r.get("named_place"):
            same = _fold(r["named_place"]) == _fold(r.get("street"))
            where += " ⭐" if same else f" ⭐{html.escape(r['named_place'])}"
        if e["kind"] == "price":
            price = f"{_czk(e.get('old_price'))} → {_czk(r.get('price_czk'))}{unit}"
            icon = "💰"
        elif e["kind"] == "gone":
            price = f"naposledy {_czk(r.get('price_czk'))}{unit}"
            icon = "❌"
        elif e["kind"] == "returned":
            price = f"znovu v nabídce · {_czk(r.get('price_czk'))}{unit}"
            icon = "🔄"
        else:
            price = f"{_czk(r.get('price_czk'))}{unit}"
            icon = "🆕"
        parts = [f"{icon} {html.escape(r.get('disposition') or '?')} {_tx(r)} {html.escape(kind_tag(r))}",
                 where, price]
        if r.get("price_czk_per_sqm"):
            parts.append(f"{_czk(r['price_czk_per_sqm'])}/m²")
        parts.append(_km(r.get("km")))
        if e["kind"] == "gone":
            dom = days_on_market(r, now)
            if dom["days"] is not None:
                src = " (podle Sreality)" if dom["source"] == "sreality" else ""
                parts.append(f"na trhu {'≥ ' if dom['lower_bound'] else ''}{dom['days']} d{src}")
        url = _safe_url(r.get("url"))
        line = " · ".join(parts)
        if url:
            line += f' · <a href="{html.escape(url, quote=True)}">odkaz</a>'
        # Detail přímo na dashboardu (#byt=<id> otevře inzerát po načtení).
        import ux
        dash = ux.dashboard_link(dashboard_url, r.get("id"))
        if dash:
            line += f' · <a href="{html.escape(dash, quote=True)}">na dashboardu</a>'
        lines.append(line)
    if len(events) > MAX_ALERT_LINES:
        lines.append(f"… a dalších {len(events) - MAX_ALERT_LINES}")
    if dashboard_url:
        lines.append(f'<a href="{html.escape(dashboard_url, quote=True)}">dashboard</a>')
    return "\n".join(lines)


def send_alert(events, now, *, dry_run=False, dashboard_url=None):
    """Pošle alert, když je co. Nikdy nevyhodí výjimku: alert je doplněk a
    jeho selhání nesmí shodit běh. Vrací kanál / "dry-run" / None."""
    try:
        text = build_alert(events, now, dashboard_url)
        if not text:
            return None
        import notify
        return notify.send_text(text, dry_run=dry_run, what="Alert novostaveb")
    except Exception as exc:  # noqa: BLE001 -- deliberate: never fail the run
        print(f"::warning::alert novostaveb se neodeslal: {exc}", file=sys.stderr)
        return None


def stage_alert(events, now, path, dashboard_url=None):
    """Připraví zprávu mimo repo; odeslat ji smí až krok po úspěšném pushi."""
    text = build_alert(events, now, dashboard_url)
    if text:
        path.write_text(text, encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    return bool(text)


def send_staged_alert(path, *, dry_run=False):
    """Odešle připravenou zprávu; volá se pouze po úspěšném pushi."""
    if not path.exists():
        return None
    try:
        import notify
        return notify.send_text(path.read_text(encoding="utf-8"), dry_run=dry_run,
                                what="Alert novostaveb")
    except Exception as exc:  # noqa: BLE001 -- doplňkový alert neblokuje scrape
        print(f"::warning::alert novostaveb se neodeslal: {exc}", file=sys.stderr)
        return None


# --- Stránka ---------------------------------------------------------------- #
PAGE_FIELDS = (
    "id", "title", "disposition", "transaction_type", "price_czk", "price_old_czk",
    "price_czk_per_sqm", "floor_area_sqm", "street", "city_part", "locality", "lat", "lon",
    "km", "url", "thumb", "first_seen", "last_seen", "gone_at", "baseline", "price_history",
    "since", "named_place", "missing_from_search", "left_filter_at", "returned_at",
    "kind", "kind_reason", "year_unknown", "kind_changed_at", "kind_before",
    # Detail inzerátu (modal): stejné pole jako u bytů, aby šly sdílet
    # pomocné funkce stránky (addressHtml, priceHistoryHtml, formulář oprav).
    "description", "images", "seller_name", "floor_number", "floors_total", "address_exact",
    "acceptance_year", "reconstruction_year", "building_condition",
    "override", "override_note", "exclude_from_stats", "floor_area_source",
)
MAX_PAGE_IMAGES = 5


def with_overrides(records, overrides):
    """Kopie záznamů s ručními opravami z overrides.json (stejný soubor a
    stejný formulář jako u bytů). Uložené záznamy se NEMĚNÍ -- oprava se
    uplatní jen na pohled (stránka, statistika), takže smazaná oprava zmizí
    hned příštím renderem a nic po ní v kolekci nezůstane.

    Z opravy se tu uplatní plocha (a z ní Kč/m²), „mimo statistiku" a
    poznámka. Poplatky se u novostaveb nikde nepočítají -- formulář je na
    stránce skrývá."""
    overrides = overrides or {}
    out = []
    for r in records or []:
        ov = overrides.get(str(r.get("id")))
        if not ov:
            out.append(r)
            continue
        r = dict(r)
        r["override"] = ov
        if ov.get("note"):
            r["override_note"] = ov["note"]
        area = ov.get("floor_area_sqm")
        if isinstance(area, (int, float)) and not isinstance(area, bool) and area > 0:
            r["floor_area_sqm"] = float(area)
            r["floor_area_source"] = "override"
            r["price_czk_per_sqm"] = round(r["price_czk"] / area) if r.get("price_czk") else None
        if ov.get("exclude_from_stats"):
            r["exclude_from_stats"] = True
        out.append(r)
    return out


def page_payload(records, generated_at, overrides=None):
    recs = []
    for r in with_overrides(records, overrides):
        if r.get("out_of_scope"):
            continue
        rec = {k: r[k] for k in PAGE_FIELDS if r.get(k) is not None}
        if not _safe_url(rec.get("url")):
            rec.pop("url", None)
        # `since` je scrapovaný řetězec a fmtDay() v JS z něj bere rok bez
        # escapování -- na stránku jen tvar YYYY-MM-DD.
        if not re.match(r"^\d{4}-\d{2}-\d{2}", str(rec.get("since") or "")):
            rec.pop("since", None)
        # Stránka je veřejná: kontakty makléřů ven i tady, ne jen při zápisu
        # snapshotu (re-render starého snapshotu je nesmí znovu zveřejnit).
        if rec.get("description"):
            rec["description"] = gone_archive.strip_contacts(str(rec["description"]))
        if rec.get("seller_name"):
            rec["seller_name"] = gone_archive.strip_seller(str(rec["seller_name"]))
        imgs = [u for u in (rec.get("images") or []) if _safe_url(u)][:MAX_PAGE_IMAGES]
        if imgs:
            rec["images"] = imgs
        else:
            rec.pop("images", None)
        # Vlastní kopie fotek (photo_archive): Sreality je po smazání
        # inzerátu stáhne z CDN, tyhle zůstanou.
        local = [p for p in photo_archive.local_photos(rec.get("id"))
                 if photo_archive.WEB_PATH_RE.match(p)]
        if local:
            rec["photos_local"] = local
        dom = days_on_market(r, generated_at)
        rec["market_start"], rec["market_source"] = market_start(r)
        rec["days_lower_bound"] = dom["lower_bound"]
        recs.append(rec)
    return {
        "records": recs,
        "center": list(CENTER),
        "center_label": CENTER_LABEL,
        "default_radius_km": DEFAULT_RADIUS_KM,
        "alert_radius_km": ALERT_RADIUS_KM,
        "superset_km": SUPERSET_KM,
        "landmarks": LANDMARKS,
        "generated_at": generated_at,
    }


def card_html(records, baseline_at=None):
    """Kostra karty; tabulky, statistiky a mapu kreslí JS (page_js), protože se
    přepočítávají z kruhu, který si Radim nastaví. None = kolekce ještě nikdy
    neběžela -> žádná karta (čip v ribbonu se pak sám schová)."""
    if records is None:
        return ""
    r_def = f"{DEFAULT_RADIUS_KM:.1f}".replace(".", ",")
    r_alert = f"{ALERT_RADIUS_KM:.1f}".replace(".", ",")
    r_sup = f"{SUPERSET_KM:.1f}".replace(".", ",")
    t = parse_iso(baseline_at)
    since = f" ({t.day}. {t.month}. {t.year})" if t is not None else ""
    return f"""<div class="card" id="novCard">
  <h2 style="margin-top:0;font-size:1rem;">🏗️ Novostavby 4+kk / 5+kk — {html.escape(CENTER_LABEL)}</h2>
  <p class="hint" style="margin:0 0 8px;">Všechny byty 4+kk a 5+kk do <b>{r_sup} km</b> od {html.escape(CENTER_LABEL)}
    (přerušovaná hranice), prodej i pronájem, roztříděné podle detailu na Sreality:
    <b>dokončené {NEW_FROM_YEAR}+</b> (rok kolaudace ≥ {NEW_FROM_YEAR}, nebo štítek „novostavba" bez roku),
    <b>ve výstavbě / projekt</b> a <b>starší</b> (kolaudace před {NEW_FROM_YEAR}, nebo bez roku a bez štítku
    novostavba; rok rekonstrukce se nepočítá). Důvod je ve sloupci Typ.
    Kruh je <b>jen filtr zobrazení</b>. Pro změnu klikni na <b>Upravit polohu</b>: pak táhni středem ✚
    (nebo klepni do mapy) a posuvníkem měň poloměr, statistika, tabulka i mapa se průběžně přepočítají.
    Uloží se až tlačítkem <b>Uložit</b>, <b>Zrušit</b> vrátí uložený kruh. Mimo úpravu klik ani zoom
    do mapy kruh nemění. Alerty chodí pro výchozí okruh {r_alert} km a jen pro dokončené
    a ve výstavbě; posuvník ani přepínače typu je nemění.</p>
  <div class="nov-ctl" id="novKinds" role="group" aria-label="Typ">
    <label class="nov-k"><input type="checkbox" data-kind="dokoncena"> Dokončené {NEW_FROM_YEAR}+</label>
    <label class="nov-k"><input type="checkbox" data-kind="vystavba"> Ve výstavbě / projekt</label>
    <label class="nov-k"><input type="checkbox" data-kind="starsi"> Starší</label>
    <label class="nov-k" id="novKindUnk" hidden><input type="checkbox" data-kind="neurceno"> Typ neověřen</label>
  </div>
  <div class="nov-ctl">
    <button type="button" class="popup-btn" id="novEdit">✎ Upravit polohu</button>
    <label class="nov-r">Poloměr <input type="range" id="novR" min="0.2" max="{SUPERSET_KM}" step="0.05" disabled>
      <b id="novRv"></b></label>
    <button type="button" class="popup-btn" id="novSave" hidden>Uložit</button>
    <button type="button" class="popup-btn" id="novCancel" hidden>Zrušit</button>
    <button type="button" class="popup-btn" id="novReset" hidden>Výchozí ({r_def} km)</button>
    <select id="novTx">
      <option value="">Prodej i pronájem</option>
      <option value="prodej">Jen prodej</option>
      <option value="pronajem">Jen pronájem</option>
    </select>
  </div>
  <div id="novWarn" class="modal-note" hidden></div>
  <div id="novMap" class="gmap"></div>
  <div class="hint">🟠 prodej · 🔵 pronájem · ⚪ zmizelo · ✚ střed kruhu · 🟣 Radimova místa ·
    bledé = mimo kruh.</div>
  <div class="est-scroll" style="margin-top:10px;"><table class="est-table" id="novStats"></table></div>
  <p class="hint">„Na trhu" a „Do zmizení" se počítají od <b>data vložení podle Sreality</b> („na Sreality od …"),
    když ho detail uvádí a je dřívější než náš první výskyt; jinak od prvního výskytu u nás. Konec je ověřené
    zmizení (detail na Sreality vrací 404; kontrola každé ~4 h). Pozor: Sreality datum vynuluje, když makléř
    inzerát smaže a vloží znovu — číslo pak může být podhodnocené. <b>≥</b> = datum Sreality chybí a inzerát byl
    v nabídce už při prvním běhu sledování{since} (nebo když jsme rozšířili sběr), skutečné stáří neznáme —
    číslo je dolní mez. Zmizení neznamená prodej. Klik na řádek (nebo „Detail" v mapě) otevře fotky, popis
    a historii ceny.</p>
  <div class="scroll" style="margin-top:8px;">
  <table id="tblNov" class="nov-list">
    <thead><tr><th>Dispozice</th><th>Typ</th><th>Transakce</th><th>Ulice</th><th>m²</th><th>Cena</th><th>Kč/m²</th>
      <th title="Vzdušná vzdálenost od {html.escape(CENTER_LABEL)}">km od {html.escape(CENTER_LABEL)}</th>
      <th>Na trhu</th><th>Stav</th><th></th></tr></thead>
    <tbody></tbody>
  </table>
  </div>
  <p class="hint">⭐ = ulice U Kříže, Kohoutových, Bochovská, Na Pomezí nebo okolí parku Waltrovka.</p>
</div>"""


CSS = """
  /* Hlavička statistiky se nelepí (viz .est-table th ve scrape.py) a netřídí. */
  #novStats th { cursor: default; }
  #tblNov tbody tr.clickable-row td { vertical-align: top; }
  .nov-ctl { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 0 0 8px; }
  /* Atribut hidden tu musí vyhrát nad display z .popup-btn / .nov-k -- jinak
     jsou tlačítka režimu úprav i čip „Typ neověřen" vidět pořád. */
  .nov-ctl [hidden] { display: none !important; }
  .nov-ctl .nov-r { display: flex; gap: 6px; align-items: center; font-size: 0.8rem; flex: 1 1 220px; }
  .nov-ctl .nov-r input { flex: 1; min-width: 100px; padding: 0; }
  .nov-lbl { background: rgba(20,22,30,.85); color: #e6d6ff; border: 1px solid #6b4fa0;
             font-size: 0.66rem; padding: 0 4px; box-shadow: none; }
  .nov-lbl::before { display: none; }
  .nov-center { color: #7CFFB2; font-size: 20px; line-height: 22px; text-align: center;
                font-weight: 700; text-shadow: 0 0 3px #000; cursor: default; }
  /* Režim úprav kruhu: je vidět, že klik do mapy teď kruh posune. */
  .nov-editing .nov-center { cursor: move; }
  .nov-editing #novMap { outline: 2px dashed #7CFFB2; outline-offset: 2px; cursor: crosshair; }
  .nov-ctl .nov-r input:disabled { opacity: 0.45; }
  #novSave { background: #1f5c3a; }
  tr.nov-out td { opacity: 0.45; }
  .nov-star { color: #fc6; }
  .nov-k { display: inline-flex; gap: 4px; align-items: center; font-size: 0.8rem;
           background: #11141b; border-radius: 12px; padding: 3px 10px; cursor: pointer; }
  .nov-k input { margin: 0; padding: 0; }
  .nov-kind { display: inline-block; font-size: 0.66rem; padding: 1px 6px; border-radius: 8px;
              white-space: nowrap; }
  .nov-kind.dokoncena { background: #1e4620; color: #8f8; }
  .nov-kind.vystavba { background: #4a3c1c; color: #fc6; }
  .nov-kind.starsi { background: #333; color: #aaa; }
  .nov-kind.neurceno { background: #2b2b2b; color: #888; }
"""


def page_js(payload_json):
    """JS karty. Vkládá se za hlavní šablonu (potřebuje escapeHtml, fmtCzk,
    fmtDay, daysBetween, safeImg, PLACEHOLDER a Leaflet `L`). `payload_json`
    musí už být escapovaný pro <script> (scrape.script_json)."""
    return r"""
// ---- Novostavby 4+kk / 5+kk kolem U Kříže -----------------------------------
// Kruh je filtr zobrazení; data jsou nadmnožina do NOV.superset_km. Všechno
// scrapované jde přes escapeHtml, URL jen https, do atributů žádná id.
(function () {
  const NOV = __NOV_JSON__;
  const card = document.getElementById("novCard");
  if (!card || !NOV) return;
  const KEY = "novCircle:v1";
  const DEF = { lat: NOV.center[0], lon: NOV.center[1], r: NOV.default_radius_km };
  const TXL = { prodej: "prodej", pronajem: "pronájem" };
  const DISPS = ["4+kk", "5+kk"];

  function hav(a1, o1, a2, o2) {
    const R = 6371, rad = Math.PI / 180;
    const x = Math.sin((a2 - a1) * rad / 2) ** 2 +
      Math.cos(a1 * rad) * Math.cos(a2 * rad) * Math.sin((o2 - o1) * rad / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(x));
  }
  function load() {
    try {
      const s = JSON.parse(localStorage.getItem(KEY) || "null");
      if (s && [s.lat, s.lon, s.r].every(v => typeof v === "number" && isFinite(v)) &&
          s.r >= 0.2 && s.r <= NOV.superset_km && hav(s.lat, s.lon, DEF.lat, DEF.lon) < 10) {
        return { lat: s.lat, lon: s.lon, r: s.r };
      }
    } catch (e) {}
    return { ...DEF };
  }
  // `circ` je kruh, podle kterého se kreslí; `saved` ten uložený. Mimo režim
  // úprav jsou stejné. Radim: zoom a klik do mapy nesmí kruh změnit omylem,
  // proto se mění jen po „Upravit polohu" a do localStorage jde až „Uložit".
  function save() {
    saved = { ...circ };
    // Výchozí kruh se neukládá: po změně výchozího v kódu ho má i uživatel,
    // který dal „Výchozí" a Uložit (stejně jako dřívější Reset).
    const isDef = saved.lat === DEF.lat && saved.lon === DEF.lon && saved.r === DEF.r;
    try {
      if (isDef) localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, JSON.stringify(saved));
    } catch (e) {}
  }
  let circ = load();
  let saved = { ...circ };
  let editing = false;

  // Typy: výchozí jen dokončené (Radim: „hlavně už ty dokončené"). Neověřené
  // (detail zatím nenačten) jsou vidět, dokud je nevypne -- nic se tiše neschová.
  const KINDS_KEY = "novKinds:v1";
  const KIND_DEF = { dokoncena: true, vystavba: false, starsi: false, neurceno: true };
  const KIND_TXT = { dokoncena: "dokončená", vystavba: "výstavba", starsi: "starší", neurceno: "neověřeno" };
  function loadKinds() {
    try {
      const s = JSON.parse(localStorage.getItem(KINDS_KEY) || "null");
      if (s && typeof s === "object") {
        const out = { ...KIND_DEF };
        for (const k of Object.keys(KIND_DEF)) if (typeof s[k] === "boolean") out[k] = s[k];
        return out;
      }
    } catch (e) {}
    return { ...KIND_DEF };
  }
  let kinds = loadKinds();
  const kindOf = r => (KIND_DEF.hasOwnProperty(r.kind) ? r.kind : "neurceno");
  const kindOk = r => !!kinds[kindOf(r)];
  const hasUnk = NOV.records.some(r => kindOf(r) === "neurceno");
  function kindBadge(r) {
    const k = kindOf(r);
    const changed = r.kind_changed_at && r.kind_before && KIND_TXT[r.kind_before]
      ? `<div class="hint">dřív ${KIND_TXT[r.kind_before]} (do ${fmtDay(r.kind_changed_at)})</div>` : "";
    return `<span class="nov-kind ${k}">${KIND_TXT[k]}</span><div class="hint">${escapeHtml(r.kind_reason || "")}</div>${changed}`;
  }

  const num = v => (typeof v === "number" && isFinite(v)) ? v : null;
  const kmTxt = v => num(v) == null ? "—" : v.toFixed(2).replace(".", ",");
  const safeUrl = u => (typeof u === "string" && /^https:\/\//.test(u)) ? u : "";
  const live = r => !r.gone_at && !r.left_filter_at;
  function inCircle(r) {
    const la = num(r.lat), lo = num(r.lon);
    return la != null && lo != null && hav(circ.lat, circ.lon, la, lo) <= circ.r + 1e-9;
  }
  function median(a) {
    if (!a.length) return null;
    const s = [...a].sort((x, y) => x - y), m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }
  // Začátek počítá server (novostavby.market_start): dřívější z data
  // vložení podle Sreality a našeho prvního výskytu. "baseline" = datum
  // Sreality chybí a inzerát byl v nabídce už při prvním běhu -> dolní mez.
  function dom(r) {
    const src = r.market_source || (r.baseline ? "baseline" : "ours");
    const d = daysBetween(r.market_start || r.first_seen, r.gone_at || null);
    return { d, lb: src === "baseline", src };
  }
  function domTxt(r) {
    const x = dom(r);
    if (x.d == null) return "—";
    return `${x.lb ? "≥ " : ""}${x.d} d`;
  }
  function domHint(r) {
    const x = dom(r);
    if (x.src === "sreality") return `podle Sreality (od ${fmtDay(r.since)})`;
    const since = r.since ? `; na Sreality od ${fmtDay(r.since)}` : "";
    if (x.src === "baseline") return `od prvního běhu sledování — dolní mez${since}`;
    return `od prvního výskytu u nás (${fmtDay(r.first_seen)})${since}`;
  }
  function txSel() { return document.getElementById("novTx").value; }
  function unit(r) { return r.transaction_type === "pronajem" ? "/měs" : ""; }

  function renderWarn() {
    const el = document.getElementById("novWarn");
    const reach = hav(circ.lat, circ.lon, DEF.lat, DEF.lon) + circ.r;
    if (reach > NOV.superset_km + 0.005) {
      el.textContent = `Kruh přesahuje sbíranou oblast. Data se sbírají jen do ${String(NOV.superset_km).replace(".", ",")} km ` +
        `od ${NOV.center_label} (přerušovaná hranice) — co leží za ní, tu chybí, i když by to na Sreality bylo.`;
      el.hidden = false;
    } else {
      el.hidden = true;
    }
    document.getElementById("novRv").textContent = circ.r.toFixed(2).replace(".", ",") + " km";
  }

  function renderStats() {
    const tx = txSel();
    // Ručně vyřazené („mimo statistiku") zůstanou v tabulce, ne v číslech.
    const recs = NOV.records.filter(r => inCircle(r) && kindOk(r) && !r.exclude_from_stats);
    const rows = [];
    for (const t of ["prodej", "pronajem"]) {
      if (tx && tx !== t) continue;
      for (const disp of DISPS) {
        const g = recs.filter(r => r.transaction_type === t && r.disposition === disp);
        const lv = g.filter(live), gone = g.filter(r => r.gone_at);
        const pr = lv.map(r => num(r.price_czk)).filter(v => v);
        const psm = lv.map(r => num(r.price_czk_per_sqm)).filter(v => v);
        const gd = gone.map(r => dom(r).d).filter(v => v != null);
        const lb = gone.some(r => dom(r).lb) ? "≥ " : "";
        const md = median(gd);
        const last = [...gone].sort((a, b) => String(b.gone_at).localeCompare(String(a.gone_at)))
          .map(r => num(r.price_czk)).filter(v => v).slice(0, 4);
        const u = t === "pronajem" ? "/měs" : "";
        rows.push(`<tr><td>${TXL[t]} ${escapeHtml(disp)}</td><td>${lv.length}</td>
          <td>${pr.length ? fmtCzk(Math.round(median(pr))) + u : "—"}</td>
          <td>${psm.length ? fmtCzk(Math.round(median(psm))) : "—"}</td>
          <td>${gone.length}</td>
          <td>${md != null ? `${lb}${Math.round(md)} d <span class="hint">(${lb}${Math.min(...gd)}–${Math.max(...gd)})</span>` : "—"}</td>
          <td>${last.length ? last.map(v => fmtCzk(v) + u).join(", ") : "—"}</td></tr>`);
      }
    }
    const on = Object.keys(KIND_DEF).filter(k => kinds[k] && (k !== "neurceno" || hasUnk)).map(k => KIND_TXT[k]);
    document.getElementById("novStats").innerHTML =
      `<caption class="hint" style="text-align:left;caption-side:top;">Typy: ${on.join(", ") || "žádný"}</caption>` +
      `<thead><tr><th></th><th>Živé</th><th>Medián ceny</th><th>Medián Kč/m²</th><th>Zmizelo</th>` +
      `<th>Do zmizení (medián, rozsah)</th><th>Poslední ceny zmizelých</th></tr></thead><tbody>${rows.join("")}</tbody>`;
  }

  function stateTxt(r) {
    if (r.gone_at) return `<span class="deal-bad">zmizel ${fmtDay(r.gone_at)}</span>`;
    if (r.left_filter_at) return `<span class="hint">už není 4+kk/5+kk (${fmtDay(r.left_filter_at)})</span>`;
    const miss = r.missing_from_search ? ` <span class="hint" title="Ve výsledcích hledání chybí, ale detail inzerátu žije">(mimo hledání)</span>` : "";
    return `<span class="deal-good">živý</span>${miss}`;
  }
  function priceTxt(r) {
    const moved = num(r.price_old_czk) && r.price_old_czk !== r.price_czk
      ? ` <span class="hint" title="Předchozí cena">(dřív ${fmtCzk(r.price_old_czk)})</span>` : "";
    return (num(r.price_czk) ? fmtCzk(r.price_czk) + unit(r) : "na dotaz") + moved;
  }
  function renderTable() {
    const tx = txSel();
    const recs = NOV.records.filter(r => (!tx || r.transaction_type === tx) && kindOk(r));
    recs.sort((a, b) => (live(b) - live(a)) || (inCircle(b) - inCircle(a)) ||
      String(a.transaction_type).localeCompare(String(b.transaction_type)) || ((a.km || 0) - (b.km || 0)));
    const rows = recs.map(r => {
      const inside = inCircle(r);
      const star = r.named_place ? ` <span class="nov-star" title="${escapeHtml(r.named_place)}">⭐</span>` : "";
      const url = safeUrl(r.url);
      // Id jen jako data-atribut (escapeHtml), klik obslouží jeden posluchač
      // na dokumentu -- žádné id v inline JS.
      return `<tr class="clickable-row${inside ? "" : " nov-out"}" data-nid="${escapeHtml(String(r.id))}">
        <td>${escapeHtml(r.disposition || "")}${overrideBadges(r)}</td>
        <td>${kindBadge(r)}</td>
        <td>${TXL[r.transaction_type] || ""}</td>
        <td>${escapeHtml(r.street || r.city_part || "—")}${star}<div class="hint">${escapeHtml(r.city_part || "")}</div></td>
        <td>${fmtArea(r)}</td>
        <td>${priceTxt(r)}</td>
        <td>${num(r.price_czk_per_sqm) ? fmtCzk(r.price_czk_per_sqm) : "—"}</td>
        <td>${kmTxt(r.km)}${inside ? "" : ' <span class="hint">mimo kruh</span>'}</td>
        <td>${domTxt(r)}<div class="hint">${domHint(r)}</div></td>
        <td>${stateTxt(r)}</td>
        <td>${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener" data-stop title="Otevřít na Sreality">↗</a>` : ""}</td>
      </tr>`;
    });
    const tb = document.querySelector("#tblNov tbody");
    tb.innerHTML = rows.length ? rows.join("")
      : `<tr><td colspan="11" class="hint">Pro zvolené typy tu zatím nic není.</td></tr>`;
  }

  // ---- Detail inzerátu: stejný modal jako u bytů (galerie, historie ceny,
  // parametry, popis, odkaz, ruční oprava) ----
  const BY_ID = new Map(NOV.records.map(r => [String(r.id), r]));
  // Jen cesty, které vyrábí photo_archive.WEB_PATH_RE -- nic jiného z dat
  // se jako src nepoužije.
  const LOCAL_PHOTO_RE = /^photos\/nov\/[0-9A-Za-z_-]{1,40}\/[0-9]\.(?:webp|jpg|png)$/;
  // Selhaná fotka: zkusit vlastní kopii, jinak ji z galerie vyndat. Prázdná
  // galerie pak řekne proč (CSS .modal-gallery:empty).
  window.novPhotoError = function (img) {
    const fb = img.getAttribute("data-fb") || "";
    img.removeAttribute("data-fb");
    if (fb && LOCAL_PHOTO_RE.test(fb)) { img.src = fb; return; }
    img.remove();
  };
  function novThumb(r) {
    const local = (r.photos_local || []).filter(p => LOCAL_PHOTO_RE.test(p));
    return (r.gone_at && local.length) ? local[0] : safeImg(r.thumb);
  }
  function novModalHtml(r) {
    // Vlastní kopie (photos/nov/…) mají přednost u zmizelých -- Sreality
    // jejich fotky smaže z CDN. U živých jsou zálohou, když odkaz selže.
    const local = (r.photos_local || []).filter(p => LOCAL_PHOTO_RE.test(p));
    const remote = (r.images && r.images.length) ? r.images : (r.thumb ? [r.thumb] : []);
    const imgs = (r.gone_at && local.length) ? local.map(p => [p, ""])
      : remote.map((u, i) => [safeImg(u), local[i] || ""]);
    const gallery = imgs.length
      ? imgs.map(([u, fb]) => `<img src="${escapeHtml(u)}" data-fb="${escapeHtml(fb)}" loading="lazy" onerror="novPhotoError(this)">`).join("")
      : `<img src="${PLACEHOLDER}">`;
    const x = dom(r);
    const goneHtml = r.gone_at ? `<div class="modal-note">❌ Už není v nabídce — zmizel ${fmtDay(r.gone_at)}
        po ${x.lb ? "≥ " : ""}${x.d ?? "?"} dnech. Fotky a popis jsou z doby, kdy inzerát žil.</div>` : "";
    const leftHtml = r.left_filter_at ? `<div class="modal-note">Inzerát žije, ale od ${fmtDay(r.left_filter_at)}
        už není 4+kk/5+kk — ze sledování vypadl.</div>` : "";
    const url = safeUrl(r.url);
    const floor = num(r.floor_number) != null ? `${r.floor_number}/${num(r.floors_total) ?? "?"}` : "—";
    const title = r.title || `${r.disposition || ""} · ${TXL[r.transaction_type] || ""}`;
    return `
      <button id="modalClose" onclick="closeModal()">&times;</button>
      <h2>${escapeHtml(title)} ${overrideBadges(r)}</h2>
      ${goneHtml}${leftHtml}
      ${r.override_note ? `<div class="modal-note">Oprava: ${escapeHtml(r.override_note)}</div>` : ""}
      <div class="modal-gallery">${gallery}</div>
      ${priceHistoryHtml(r)}
      <div class="modal-grid">
        <div><b>Cena</b>${priceTxt(r)}</div>
        <div><b>Kč/m²</b>${fmtCzk(r.price_czk_per_sqm)}</div>
        <div><b>Dispozice</b>${escapeHtml(r.disposition || "—")}</div>
        <div><b>m²</b>${fmtArea(r)}</div>
        <div><b>Patro</b>${floor}</div>
        <div><b>Transakce</b>${TXL[r.transaction_type] || "—"}</div>
        <div style="grid-column:1/-1;"><b>Typ</b>${kindBadge(r)}</div>
        <div style="grid-column:1/-1;"><b>Adresa</b>${addressHtml(r)}
          <div class="hint">${kmTxt(r.km)} km od ${escapeHtml(NOV.center_label)} · ${mapLinksHtml(r.lat, r.lon)}</div></div>
        <div><b>Na trhu</b>${domTxt(r)}<div class="hint">${domHint(r)}</div></div>
        <div><b>Stav</b>${stateTxt(r)}</div>
        <div><b>Prodejce</b>${escapeHtml(r.seller_name || "—")}</div>
      </div>
      <div class="modal-desc">${escapeHtml(r.description ||
        "Popis zatím nemáme — načte se při dalším čtení detailu (nové inzeráty hned, ostatní do týdne).")}</div>
      <div class="modal-note">U novostaveb se z ruční opravy použije plocha (a z ní Kč/m²), „mimo statistiku"
        a poznámka — v této kartě, po doběhnutí dalšího běhu.</div>
      ${overrideFormHtml(r)}
      ${url ? `<a class="modal-link" href="${escapeHtml(url)}" target="_blank" rel="noopener">${r.gone_at ? "Původní inzerát (už nejspíš 404)" : "Otevřít na Sreality"} →</a>` : ""}
      ${overrideFooterHtml(r)}`;
  }
  // Pro submitOverride (scrape.py): po uložení opravy obnoví odznak
  // „čeká na zpracování" i u novostavby.
  window.novItem = id => BY_ID.get(String(id)) || null;
  function openNov(id) {
    const r = BY_ID.get(String(id));
    if (!r) return;
    document.getElementById("modalSheet").innerHTML = novModalHtml(r);
    // Poplatky se u novostaveb nikde nepočítají -- pole by slibovalo opravu,
    // která nic nezmění. Skryté (ne smazané): saveOverride ho čte.
    const fees = document.getElementById("ovFees");
    if (fees) {
      fees.style.display = "none";
      const lbl = fees.previousElementSibling;
      if (lbl && lbl.tagName === "LABEL") lbl.style.display = "none";
    }
    document.getElementById("modalOverlay").classList.add("open");
    // Hvězda oblíbených + kopírování odkazu (ux.py), když je na stránce.
    if (typeof uxModalOpened === "function") uxModalOpened("nov", String(id));
  }
  // Pro ux.py (#byt= odkaz, hledání, oblíbené): jeden detail novostavby, ne dva.
  window.openNov = openNov;
  document.addEventListener("click", ev => {
    if (ev.target.closest("[data-stop]")) return;
    const el = ev.target.closest("[data-nid]");
    if (!el) return;
    ev.preventDefault();
    openNov(el.getAttribute("data-nid"));
  });

  let NM = null, circleL = null, centerM = null, markL = null;
  function color(r) {
    if (!live(r)) return "#9aa0aa";
    return r.transaction_type === "pronajem" ? "#7ab8ff" : "#ff9a4d";
  }
  function drawMarkers() {
    if (!NM) return;
    markL.clearLayers();
    const tx = txSel();
    for (const r of NOV.records) {
      if ((tx && r.transaction_type !== tx) || !kindOk(r)) continue;
      const la = num(r.lat), lo = num(r.lon);
      if (la == null || lo == null) continue;
      const inside = inCircle(r), lv = live(r), c = color(r);
      const m = L.circleMarker([la, lo], {
        radius: lv ? 7 : 5, color: c, weight: 2, dashArray: lv ? null : "3,3",
        fillColor: c, fillOpacity: lv ? (inside ? 0.6 : 0.15) : 0.1, opacity: inside ? 1 : 0.35,
      });
      const url = safeUrl(r.url);
      m.bindPopup(`<div style="min-width:150px;">
        <img class="popup-thumb" src="${escapeHtml(novThumb(r))}" onerror="this.src=PLACEHOLDER">
        <div style="font-weight:600;font-size:0.85rem;">${escapeHtml(r.disposition || "")} · ${TXL[r.transaction_type] || ""}</div>
        <div style="font-size:0.8rem;">${escapeHtml(r.street || "")} · ${priceTxt(r)}</div>
        <div style="font-size:0.75rem;">${stateTxt(r)} · ${kmTxt(r.km)} km</div>
        <button class="popup-btn" data-nid="${escapeHtml(String(r.id))}">Detail</button>
        ${url ? `<a class="popup-btn" href="${escapeHtml(url)}" target="_blank" rel="noopener">Sreality ↗</a>` : ""}</div>`);
      m.addTo(markL);
    }
  }
  function moveCircle(fit) {
    if (!NM) return;
    circleL.setLatLng([circ.lat, circ.lon]).setRadius(circ.r * 1000);
    centerM.setLatLng([circ.lat, circ.lon]);
    // Hranice z geometrie, ne circleL.getBounds(): to potřebuje už
    // promítnutou vrstvu a na mapě bez pohledu (první volání) spadne.
    if (fit) NM.fitBounds(L.latLng(circ.lat, circ.lon).toBounds(circ.r * 2000), { padding: [10, 10] });
  }
  function initMap() {
    const el = document.getElementById("novMap");
    if (!el || typeof L === "undefined") return;
    NM = L.map("novMap").setView([circ.lat, circ.lon], 14);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
    }).addTo(NM);
    // Sbíraná nadmnožina: pevná, přerušovaná, bez výplně a bez kliku.
    L.circle(NOV.center, { radius: NOV.superset_km * 1000, color: "#888", weight: 1.5,
      dashArray: "6,6", fill: false, interactive: false }).addTo(NM);
    circleL = L.circle([circ.lat, circ.lon], { radius: circ.r * 1000, color: "#7CFFB2",
      weight: 2, fillOpacity: 0.05, interactive: false }).addTo(NM);
    markL = L.layerGroup().addTo(NM);
    for (const p of NOV.landmarks || []) {
      L.circleMarker([p.lat, p.lon], { radius: 4, color: "#b48cff", weight: 2,
        fillColor: "#b48cff", fillOpacity: 0.9, interactive: false })
        .bindTooltip(escapeHtml(p.name), { permanent: true, direction: "right", className: "nov-lbl", offset: [6, 0] })
        .addTo(NM);
    }
    centerM = L.marker([circ.lat, circ.lon], {
      draggable: false, zIndexOffset: 1000, title: "Střed kruhu",
      icon: L.divIcon({ className: "nov-center", html: "✚", iconSize: [22, 22], iconAnchor: [11, 11] }),
    }).addTo(NM);
    centerM.on("drag", e => {
      const ll = e.target.getLatLng();
      circ.lat = ll.lat; circ.lon = ll.lng;
      circleL.setLatLng(ll);
    });
    centerM.on("dragend", () => refresh(false));
    NM.on("click", e => {
      if (!editing) return;
      circ.lat = e.latlng.lat; circ.lon = e.latlng.lng;
      refresh(false);
    });
    applyEditing();
    moveCircle(true);
    drawMarkers();
  }
  function refresh(fit) {
    renderWarn();
    renderStats();
    renderTable();
    // Mapa je doplněk tabulky: když selže (Leaflet se nenačetl, divný
    // prohlížeč), čísla a řádky už stojí.
    try { moveCircle(fit); drawMarkers(); } catch (e) { console.error(e); }
  }
  const slider = document.getElementById("novR");
  const btnEdit = document.getElementById("novEdit");
  const btnSave = document.getElementById("novSave");
  const btnCancel = document.getElementById("novCancel");
  const btnReset = document.getElementById("novReset");
  slider.value = String(circ.r);
  function applyEditing() {
    slider.disabled = !editing;
    btnEdit.hidden = editing;
    btnSave.hidden = btnCancel.hidden = btnReset.hidden = !editing;
    card.classList.toggle("nov-editing", editing);
    if (centerM && centerM.dragging) {
      if (editing) centerM.dragging.enable(); else centerM.dragging.disable();
    }
  }
  function endEditing(keep) {
    if (keep) save(); else circ = { ...saved };
    editing = false;
    slider.value = String(circ.r);
    applyEditing();
    refresh(false);
  }
  btnEdit.addEventListener("click", () => { editing = true; applyEditing(); });
  btnSave.addEventListener("click", () => endEditing(true));
  btnCancel.addEventListener("click", () => endEditing(false));
  slider.addEventListener("input", () => {
    if (!editing) return;
    const v = Number(slider.value);
    if (!isFinite(v)) return;
    circ.r = Math.min(NOV.superset_km, Math.max(0.2, v));
    refresh(false);
  });
  // Jen návrh: uloží se až tlačítkem Uložit, Zrušit ho vrátí.
  btnReset.addEventListener("click", () => {
    if (!editing) return;
    circ = { ...DEF };
    slider.value = String(circ.r);
    refresh(true);
  });
  applyEditing();
  document.getElementById("novTx").addEventListener("change", () => refresh(false));
  document.getElementById("novKindUnk").hidden = !hasUnk;
  for (const cb of document.querySelectorAll("#novKinds input[data-kind]")) {
    const k = cb.getAttribute("data-kind");
    cb.checked = !!kinds[k];
    cb.addEventListener("change", () => {
      kinds[k] = cb.checked;
      try { localStorage.setItem(KINDS_KEY, JSON.stringify(kinds)); } catch (e) {}
      refresh(false);
    });
  }
  // Karta může být při načtení sbalená: Leaflet v skrytém kontejneru má nulovou
  // velikost, takže se po rozbalení musí přeměřit a znovu vycentrovat.
  window.novOnShow = () => { if (NM) { NM.invalidateSize(); moveCircle(true); } };
  refresh(false);
  try { initMap(); } catch (e) { console.error(e); NM = null; }
})();
""".replace("__NOV_JSON__", payload_json)
