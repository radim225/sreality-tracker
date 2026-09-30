#!/usr/bin/env python3
"""S čím se inzerát srovnává: třída domu a plocha bytu.

Radim (30. 9.): výhodnost se má počítat proti mediánu stejné lokality,
dispozice A typu domu -- panelák za 158 tis. Kč/m² není „výhodná" novostavba.
A Kč/m² z plochy, kterou portál nafoukl o terasu, není cena bytu.

Rozhodnutí (Radim, 30. 9.):
- 3 třídy domu: novostavba / panel / ostatní starší. Novostavba = štítek
  stavu Novostavba, Ve výstavbě nebo Projekt, nebo kolaudace >= 2015.
- Plocha: když rozdíl portál vs. popis vysvětlí terasa, balkon, lodžie,
  sklep, zahrada nebo stání, Kč/m² se počítá z plochy bytu a stránka to
  řekne („portál 76 m² vč. terasy 29 m²"). Nevysvětlený rozpor jde mimo
  medián i výhodné nabídky, s varováním.
- Typ domu neznámý -> zkusit popis; když ani ten nic neřekne (nebo je
  skupina menší než MIN_GROUP), srovnává se se širokým mediánem dispozice
  a text to přizná.

Modul nic nestahuje a nesahá na scrape.py; volá ho scrape.py.
"""
import itertools
import re

NEW_FROM_YEAR = 2015
CLASSES = ("novostavba", "panel", "starsi")
CLASS_LABEL = {"novostavba": "novostavba", "panel": "panel", "starsi": "starší"}
# Sreality buildingCondition: 6 Novostavba, 8 Ve výstavbě, 10 Projekt (čísla
# ověřená proti building_condition_name v latest_snapshot.json 30. 9.).
NEW_CONDITION_NAMES = {"Novostavba", "Ve výstavbě", "Projekt"}
PANEL_TYPE_NAMES = {"Panelová"}

# --------------------------------------------------------------------------- #
# Třída domu
# --------------------------------------------------------------------------- #
_YEAR_BUILT_RE = re.compile(
    r"\b(?:z\s+roku|postaven\w*\s+(?:v\s+)?(?:roce\s+)?|kolaudac\w*\s+(?:v\s+)?(?:roce\s+)?"
    r"|zkolaudov\w*\s+(?:v\s+)?(?:roce\s+)?|dokončen\w*\s+(?:v\s+)?(?:roce\s+)?"
    r"|rok\s+(?:výstavby|kolaudace|dokončení)\s*:?)\s*((?:18|19|20)\d{2})\b",
    re.I,
)
# „po rekonstrukci z roku 2019" je rok rekonstrukce, ne domu -- i když mezi
# nimi stojí „včetně elektřiny a instalatérských prací".
_RECON_BEFORE_RE = re.compile(r"(rekonstr|renovac|modernizac|zateplen)\w*[^.;!?\n]{0,70}$", re.I)
# Holé „z roku" jen o domě: „kotle Protherm z roku 2025" není rok stavby.
_BUILDING_NOUN_RE = re.compile(r"(\bbyt\b|\bbytu\b|dům|domu|domě|dom\b|budov|novostavb|projekt|rezidenc|stavb|výstavb)\w*[^.;!?\n]{0,40}$", re.I)
_PANEL_RE = re.compile(r"\bpanel(?:ov\w+\s+(?:dům|domě|domu|dom|budov\w*)|ák\w*)\b", re.I)
_NEW_RE = re.compile(r"\bnovostavb\w*|\bnově\s+postaven\w*|\bnový\s+(?:bytový\s+)?dům\b", re.I)
# „bydlí se jinak než v novostavbě" je srovnání, ne popis domu.
_COMPARISON_BEFORE_RE = re.compile(r"\b(?:než|jako|oproti|rozdíl\s+od|ne)\s+(?:\w+\s+)?$", re.I)


def _mentions(rx, text):
    for m in rx.finditer(text or ""):
        if not _COMPARISON_BEFORE_RE.search(text[max(0, m.start() - 25):m.start()]):
            return True
    return False


def year_built_from_text(text, now_year, allow_future=True):
    """Rok stavby/kolaudace z popisu, nebo None. Rok rekonstrukce se nebere,
    holé „z roku" jen tehdy, když věta mluví o domě."""
    for m in _YEAR_BUILT_RE.finditer(text or ""):
        before = text[max(0, m.start() - 100):m.start()]
        if _RECON_BEFORE_RE.search(before):
            continue
        if m.group(0).lower().startswith("z") and not _BUILDING_NOUN_RE.search(before):
            continue
        year = int(m.group(1))
        if 1850 <= year <= now_year + (5 if allow_future else 0):
            return year
    return None


def building_class(listing, now_year):
    """(třída, zdroj) -- třída z CLASSES nebo None, když se nedá říct.

    Pořadí: štítek stavu (Sreality) > rok kolaudace (detail) > typ stavby
    (Sreality) > popis. Cihla/skelet/smíšená bez štítku a bez roku je
    „starší" -- to je dohodnutá definice třídy, ne tvrzení o stáří domu."""
    cond = listing.get("building_condition_name")
    if cond in NEW_CONDITION_NAMES:
        return "novostavba", "stav"
    year = listing.get("acceptance_year")
    if isinstance(year, int) and 1850 <= year <= now_year + 5:
        return ("novostavba" if year >= NEW_FROM_YEAR else
                "panel" if listing.get("building_type_name") in PANEL_TYPE_NAMES else
                "starsi"), "kolaudace"
    text = listing.get("description") or ""
    # U bytu „Po/V rekonstrukci" je budoucí „dokončení 2027" konec rekonstrukce.
    recon = "rekonstr" in (cond or "").lower()
    desc_year = year_built_from_text(text, now_year, allow_future=not recon)
    if desc_year is not None and desc_year >= NEW_FROM_YEAR:
        return "novostavba", "popis"
    btype = listing.get("building_type_name")
    if btype in PANEL_TYPE_NAMES:
        return "panel", "typ"
    if btype:
        return "starsi", "typ"
    # Bez parametrů (iDNES, Bezrealitky, nedočtený detail): jen popis. Rok
    # stavby před NEW_FROM_YEAR má přednost před slovem „novostavba".
    panel = _mentions(_PANEL_RE, text)
    if desc_year is not None:
        return ("panel" if panel else "starsi"), "popis"
    if _mentions(_NEW_RE, text):
        return "novostavba", "popis"
    if panel:
        return "panel", "popis"
    return None, None


# --------------------------------------------------------------------------- #
# Plocha bytu z popisu
# --------------------------------------------------------------------------- #
_NUM = r"(\d{1,3}(?:[,.]\d{1,2})?)\s*m(?:²|2|\^2)(?![\w/])"
# Pády: plocha/ploše/plochou, výměra/výměře, rozloha/rozloze, velikost(i).
_AREA_KW = r"(?:plo(?:cha|chy|chou|chu|še)|výmě[rř]\w*|rozlo[hz]\w*|velikost\w*|obytn\w+\s+prostor\w*)"
_FLAT_KW_RE = re.compile(
    r"\b(?:(?:podlahov|užitn|obytn|čist|celkov)\w+\s+)?" + _AREA_KW + r"(?!\w)[^0-9.;!?\n]{0,25}?" + _NUM,
    re.I,
)
# „byt 3+1 (111 m²)", „byt 53,2 m²"
_FLAT_DIRECT_RE = re.compile(
    r"\bbyt(?:u)?\s+(?:\d\+(?:kk|\d)\s*)?(?:\(\s*)?" + _NUM, re.I)
# Plocha, která NENÍ plochou bytu: místnost, příloha, dům, pozemek.
_NOT_FLAT_RE = re.compile(
    r"(pokoj|ložnic|kuchy|koupel|chodb|předsí|sklep|sklad|kój|koje|teras|balk|lod[žz]i|zahr[aá]d"
    r"|pozem|garáž|stání|parkov|komor|šatn|domu\b|budov|objekt|pracovn|obýv|dětsk|toalet|\bwc\b"
    r"|místnost|galeri|podkrov|půd|patro\b|spaní|spací|atelié|dvor)",
    re.I,
)
_CLAUSE_SPLIT_RE = re.compile(r"[.;!?\n•*–]\s|\s-\s")
# Holé „velikost 21 m²" bez přívlastku je plochou bytu jen tehdy, když věta
# mluví o bytě -- „Má příjemnou velikost cca 21 m²" je pokoj z minulé věty.
_QUALIFIED_RE = re.compile(r"^(?:podlahov|užitn|obytn|čist|celkov)", re.I)
_FLAT_NOUN_RE = re.compile(r"\b(?:byt\w*|bytov\w+\s+jednotk\w*|jednotk\w*|mezonet\w*|loft\w*|garsoniér\w*)\b", re.I)
# Plocha bytu mimo tohle pásmo kolem portálu je jiný údaj (pokoj, zahrada).
MIN_FLAT_SHARE, MAX_FLAT_SHARE = 0.45, 2.0

EXTRA_KINDS = {
    "terasa": r"teras\w*",
    "balkon": r"balk[oó]n\w*",
    "lodžie": r"lod[žz]i\w*",
    "sklep": r"sklep\w*|sklepní\s+kój\w*|kój\w*|komor\w*",
    "zahrada": r"(?:před)?zahr[aá]d\w*",
    "stání": r"garážov\w+\s+stání\w*|parkovací\w*\s+stání\w*|stání\w*|parkovací\w*\s+míst\w*",
}
EXTRA_LABEL = {"terasa": "terasy", "balkon": "balkonu", "lodžie": "lodžie", "sklep": "sklepa",
               "zahrada": "zahrady", "stání": "stání"}
# „terasa 29 m²", „lodžie o výměře 17,1 m²" i obráceně „7 m² zasklené lodžie".
_EXTRA_RE = {k: re.compile(r"\b(?:" + v + r")(?!\w)[^0-9.;!?\n]{0,45}?" + _NUM, re.I)
             for k, v in EXTRA_KINDS.items()}
# Mezi číslem a přílohou nejvýš jedno přídavné jméno („zasklené"), ne spojka:
# v „sklep 6 m² a garážové stání" těch 6 m² stání není.
_EXTRA_AFTER_RE = {k: re.compile(_NUM + r"\s*(?:\w{4,}\s+)?(?:" + v + r")(?!\w)", re.I)
                   for k, v in EXTRA_KINDS.items()}
_PARAM_EXTRAS = (("terasa", "terrace_area_sqm"), ("balkon", "balcony_area_sqm"),
                 ("lodžie", "loggia_area_sqm"), ("sklep", "cellar_area_sqm"))

# Kdy jsou dvě plochy „stejné". Portál zaokrouhluje na celé m² a developeři
# uvádějí podlahovou plochu, kde portál má užitnou -- 5-7 % rozdílu je běžné
# (30. 9.: 14 z 22 ručně prošlých „rozporů" bylo právě tohle). Hranice je
# práh výhodné nabídky (scrape.DEAL_THRESHOLD_PCT = 8): menší rozdíl sám
# nabídku neudělá ani nezkazí, takže není důvod inzerát vyřazovat.
SAME_ABS_SQM, SAME_REL = 2.0, 0.08
# Kdy „byt + přílohy" sedí na portál. Přísněji: se čtyřmi přílohami se do
# volné tolerance trefí skoro cokoli (87 + 5,6 + 2 ≈ 97 u 5 %).
FIT_ABS_SQM, FIT_REL = 1.0, 0.02
MAX_EXTRAS = 4


def _f(x):
    return float(x.replace(",", "."))


def _same(a, b):
    return abs(a - b) <= max(SAME_ABS_SQM, SAME_REL * max(a, b))


def _fits(a, b):
    return abs(a - b) <= max(FIT_ABS_SQM, FIT_REL * max(a, b))


def flat_area_candidates(text):
    """Plochy, které popis uvádí jako plochu bytu, v pořadí výskytu."""
    out = []
    text = text or ""
    for rx in (_FLAT_KW_RE, _FLAT_DIRECT_RE):
        for m in rx.finditer(text):
            before = _CLAUSE_SPLIT_RE.split(text[max(0, m.start() - 80):m.start()])[-1]
            if _NOT_FLAT_RE.search(before[-45:]) or _NOT_FLAT_RE.search(m.group(0)):
                continue
            if (rx is _FLAT_KW_RE and not _QUALIFIED_RE.match(m.group(0))
                    and not _FLAT_NOUN_RE.search(before)):
                continue
            v = _f(m.group(1))
            if 10 <= v <= 400:
                out.append((m.start(), v))
    return [v for _, v in sorted(out)]


def extras_from(listing, text):
    """[(druh, m²)] -- přílohy z popisu i z parametrů portálu, bez duplicit."""
    seen, out, used = set(), [], set()
    for table in (_EXTRA_RE, _EXTRA_AFTER_RE):
        for kind, rx in table.items():
            for m in rx.finditer(text or ""):
                # Jedno číslo v textu patří jen jedné příloze.
                if m.start(1) in used:
                    continue
                v = _f(m.group(1))
                if 0.5 <= v <= 300 and (kind, v) not in seen:
                    seen.add((kind, v))
                    used.add(m.start(1))
                    out.append((kind, v))
    for kind, key in _PARAM_EXTRAS:
        v = listing.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            v = float(v)
            # Parametr je často zaokrouhlený údaj z popisu (29 vs 29,4).
            if not any(k == kind and abs(x - v) < 1 for k, x in out):
                seen.add((kind, v))
                out.append((kind, v))
    return out


def _explain(big, small, extras):
    """Nejmenší sada příloh (max. jedna hodnota od druhu), pro kterou
    small + přílohy ≈ big. None, když žádná nesedí."""
    for r in range(1, min(MAX_EXTRAS, len(extras)) + 1):
        for combo in itertools.combinations(extras, r):
            if len({k for k, _ in combo}) < r:
                continue
            if _fits(small + sum(v for _, v in combo), big):
                return list(combo)
    return None


def check_area(listing):
    """Porovná plochu z portálu s plochou bytu z popisu.

    Vrací None (popis plochu neuvádí nebo není s čím srovnat), nebo dict:
      status  ok        -- popis uvádí stejnou plochu
              corrected -- portál = byt + přílohy; flat_sqm je plocha bytu
              desc_incl -- popis = byt + přílohy, portál už je byt; beze změny
              mismatch  -- rozdíl nic nevysvětluje
      portal_sqm, flat_sqm, extras [(druh, m²)]"""
    portal = listing.get("floor_area_sqm")
    text = listing.get("description")
    if not portal or not text:
        return None
    cands = [v for v in flat_area_candidates(text)
             if MIN_FLAT_SHARE * portal <= v <= MAX_FLAT_SHARE * portal]
    if not cands:
        return None
    extras = extras_from(listing, text)
    base = {"portal_sqm": float(portal)}
    # Nejdřív „portál = byt + přílohy": popis často uvádí celkovou plochu
    # i plochu bytu („celková 59 m² (byt 53,2 m² + balkon 5,8 m²)").
    for v in cands:
        if v < portal and not _same(v, portal):
            combo = _explain(portal, v, extras)
            if combo:
                return {**base, "status": "corrected", "flat_sqm": v, "extras": combo}
    if any(_same(v, portal) for v in cands):
        return {**base, "status": "ok", "flat_sqm": float(portal), "extras": []}
    for v in cands:
        if v > portal:
            combo = _explain(v, portal, extras)
            if combo:
                return {**base, "status": "desc_incl", "flat_sqm": float(portal), "extras": combo}
    return {**base, "status": "mismatch", "flat_sqm": cands[0], "extras": []}


def fmt_sqm(v):
    """71.15 -> „71,15", 43.0 -> „43" -- tak, jak to píše inzerát."""
    return (f"{v:.2f}".rstrip("0").rstrip(".")).replace(".", ",")


def area_note(res):
    """Text pro stránku, česky."""
    if res["status"] == "corrected":
        parts = ", ".join(f"{EXTRA_LABEL[k]} {fmt_sqm(v)} m²" for k, v in res["extras"])
        return (f"portál {fmt_sqm(res['portal_sqm'])} m² vč. {parts}; "
                f"Kč/m² z plochy bytu {fmt_sqm(res['flat_sqm'])} m²")
    if res["status"] == "mismatch":
        return (f"plocha nesedí: portál {fmt_sqm(res['portal_sqm'])} m², "
                f"popis {fmt_sqm(res['flat_sqm'])} m² — mimo medián a výhodné nabídky")
    return None


AREA_FIELDS = ("floor_area_portal_sqm", "area_note", "area_mismatch")


def restore_portal_area(listing):
    """Vrátí plochu z portálu, pokud ji minulý běh opravil z popisu.

    floor_area_sqm se nese keší do dalšího běhu (ENRICHED_FIELDS), takže
    opravená plocha by se jinak příště tvářila jako údaj portálu -- a
    kontrola by ji porovnávala sama se sebou. Vrací True, když něco vrátila
    (volající pak přepočítá Kč/m²)."""
    portal = listing.pop("floor_area_portal_sqm", None)
    listing.pop("area_note", None)
    listing.pop("area_mismatch", None)
    if listing.get("floor_area_source") in ("popis", "override"):
        listing.pop("floor_area_source", None)
    if not portal:
        return False
    # Bezrealitky a iDNES berou plochu z karty hledání každý běh znovu, keš
    # nese jen Kč/m². Tam je přenesená hodnota jen značka „minule opraveno"
    # a čerstvou plochu nepřepisuje -- starší údaj by jinak vyhrál navždy.
    if listing.get("source") in (None, "sreality"):
        listing["floor_area_sqm"] = portal
    return True


def apply_area_check(listing):
    """Na místě: opraví plochu, nebo označí rozpor. Vrací status nebo None.
    Plochu z titulku ani ruční opravu nepřepisuje (override vyhrává)."""
    if listing.get("floor_area_source") in ("title", "override"):
        return None
    res = check_area(listing)
    if not res:
        return None
    if res["status"] == "corrected":
        listing["floor_area_portal_sqm"] = res["portal_sqm"]
        listing["floor_area_sqm"] = res["flat_sqm"]
        listing["floor_area_source"] = "popis"
        listing["area_note"] = area_note(res)
    elif res["status"] == "mismatch":
        listing["area_mismatch"] = True
        listing["area_note"] = area_note(res)
    return res["status"]


def house_select_html():
    """Výběr „Typ domu" -- stejný v tabulce, Nejlepších nabídkách i v pásu
    žhavých nabídek. Všechny tři drží jeden stav (window.HOUSE_FILTER), změna
    v kterémkoli přepne i ostatní."""
    return ('<select class="house-filter" aria-label="Typ domu">'
            '<option value="">Všechny typy domu</option>'
            '<option value="novostavba">Novostavba (od 2015)</option>'
            '<option value="panel">Panel</option>'
            '<option value="starsi">Starší (cihla, skelet…)</option>'
            '<option value="neurceno">Typ domu neurčen</option>'
            '</select>')
