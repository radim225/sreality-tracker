#!/usr/bin/env python3
"""Ceníky developerů kolem U Kříže (Jinonice / Radlice) -- denní snímek.

Radim (27. 9. 2026): developeři ceny prodaných jednotek z ceníku mažou (nebo
jednotku z ceníku rovnou vyhodí), takže cenu musíme mít uloženou dřív, než
zmizí. Tady je jeden parser na projekt a společná logika snímku a rozdílu.

Kde co leží:
  developers/<projekt>.json   aktuální stav projektu (jednotky + kdy se co
                              naposledy změnilo, poslední známá cena)
  developers/history.jsonl    append-only deník událostí: nová jednotka,
                              změna ceny, změna stavu, zmizení, návrat,
                              skrytí ceny, baseline projektu

Pravidla, která drží zbytek repa, platí i tady:
  * Nepřítomnost ≠ prodáno, dokud zdroj není zdravý. Parser, který spadne nebo
    vrátí 0 jednotek, NEZMĚNÍ stav projektu -- jen se zapíše varování. Stejně
    tak náhlý propad počtu jednotek o víc než polovinu (rozbitá stránka se
    tváří jako „všechno se prodalo").
  * První běh projektu je tichá baseline: uloží se, ale nic se nehlásí.
  * Jeden projekt, který selže, nezastaví ostatní.
  * Žádný jazykový model, žádné placené API; jeden GET za projekt a den.

Parsery čtou jen veřejné stránky ceníku bez přihlášení a bez obcházení
ochran (robots.txt ověřen 27. 9. 2026; viz PROJECTS[*]["access_note"]).
Projekty, které pokryté nejsou, a proč, jsou v NOT_COVERED.

Spuštění:
  python3 developers.py              stáhne, uloží, pošle alert
  python3 developers.py --dry-run    stáhne, uloží, alert jen vypíše
  python3 developers.py --no-save    stáhne a vypíše souhrn, nic nezapíše
"""
import argparse
import html
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "developers"
HISTORY_FILE = "history.jsonl"
PAGES_URL = "https://radim225.github.io/sreality-tracker/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json",
    "Accept-Language": "cs,en;q=0.8",
}
TIMEOUT_S = 40

# Pokles počtu jednotek o víc než tenhle podíl proti minulému stavu = stránka
# je nejspíš rozbitá, ne vyprodaná. Stav se nemění, zapíše se varování. Pod
# MIN_UNITS_FOR_DROP_GUARD jednotek se neuplatní (3 → 1 je normální konec).
MAX_DROP_SHARE = 0.5
MIN_UNITS_FOR_DROP_GUARD = 6

STATUSES = ("volny", "predrezervace", "rezervace", "prodano", "jine")
STATUS_LABELS = {"volny": "volný", "predrezervace": "předrezervace", "rezervace": "rezervace",
                 "prodano": "prodáno", "jine": "jiný stav", "zmizelo": "zmizelo z ceníku"}
# Dispozice, které Radima zajímají nejvíc -- v alertu první a podrobně.
FOCUS_DISPOSITIONS = ("4+kk", "5+kk")
MAX_ALERT_DETAIL_LINES = 25
MAX_ALERT_OTHER_LINES = 12


# --- Pomocné: čísla, texty, stavy ------------------------------------------- #
def _fold(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def num(v):
    """Číslo z čehokoli, co ceníky píšou: 14645000, "14 645 000 Kč", "29,80",
    "55,9", "" / "-" / None -> None. Nula je u ceny „neuvedeno" (U Komína píše
    u rezervovaných „0"), u plochy „žádná" -- volající rozhodne."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("\xa0", " ").replace("m²", "").replace("Kč", "").strip()
    s = s.replace(" ", "")
    if not s or s in ("-", "–", "—"):
        return None
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def price(v):
    x = num(v)
    return int(round(x)) if x else None


def area(v):
    x = num(v)
    return round(x, 2) if x else None


def disposition(v):
    """„4+KK" / „4+kk" / „1,5+kk" / „1/2+kk" -> malými písmeny, bez mezer."""
    s = str(v or "").strip().lower().replace(" ", "")
    return s or None


def status_of(raw):
    """Stav ceníku na jednu z STATUSES. Syrový text se ukládá vedle."""
    f = _fold(raw).replace("-", "").replace(" ", "")
    if not f:
        return "jine"
    if f.startswith(("voln", "dostup", "available")):
        return "volny"
    if ("pred" in f and "rezerv" in f) or "vjednani" in f:
        return "predrezervace"
    if "rezerv" in f or f.startswith("reserved"):
        return "rezervace"
    if f.startswith(("prod", "sold")) or "nenaprodej" in f:
        return "prodano"
    return "jine"


def floor_from_np(v):
    """„1. NP" / „02 NP" / „02_NP" / "3" -> 1 / 2 / 3 (číslo nadzemního
    podlaží, stejně jako `floor_number` na Sreality: ověřeno 27. 9. na
    U Komína, kde inzerát 1+kk 29 m² za 8 381 250 Kč má patro 1 a ceník
    tentýž byt B1.1 v „1. NP")."""
    m = re.search(r"-?\d+", str(v or ""))
    return int(m.group(0)) if m else None


def _get(url):
    import requests
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT_S)
    r.raise_for_status()
    # Weby bez charsetu v hlavičce (Apache u etapy H) by requests dekódoval
    # jako latin-1 a z „PRODÁNO" udělal mojibake.
    r.encoding = "utf-8"
    return r.text


def _next_flight(html_text):
    """Obsah Next.js App Routeru (self.__next_f.push) jako jeden řetězec."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html_text, re.S)
    if not chunks:
        raise ValueError("stránka nemá self.__next_f -- změnil se web?")
    return "".join(json.loads('"' + c + '"') for c in chunks)


def _json_after(text, marker):
    i = text.find(marker)
    if i < 0:
        raise ValueError(f"v datech chybí {marker!r}")
    value, _ = json.JSONDecoder().raw_decode(text, i + len(marker))
    return value


def _cells(row_html):
    return [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)]


def unit(uid, **kw):
    """Jednotný tvar jednotky. Chybějící hodnota = None, nikdy 0."""
    rec = {"id": str(uid)}
    rec.update({k: v for k, v in kw.items() if v is not None and v != ""})
    return rec


# --- Parsery ----------------------------------------------------------------- #
def parse_waltrovka_f(text):
    """Nová Waltrovka etapa F (Penta). Next.js + Strapi, pole `defaultData`
    přímo v HTML. Rezervované a prodané jednotky z ceníku MIZÍ -- zmizení je
    tady jediný signál prodeje, proto `gone_means` u projektu.
    `updatedAt` se přepisuje při každém importu (27. 9. měly všechny jednotky
    dnešní čas), takže jako datum změny ceny nepoužitelné -- neukládá se."""
    data = _json_after(_next_flight(text), '"defaultData":')
    out = []
    for d in data:
        a = d.get("attributes") or {}
        if not a.get("Oznaceni"):
            continue
        patro = a.get("Patro")
        out.append(unit(
            a["Oznaceni"],
            building=a.get("Budova"),
            floor=(int(patro) + 1) if isinstance(patro, (int, float)) else None,
            floor_raw=None if patro is None else f"patro {patro}",
            disposition=disposition(a.get("Dispozice")),
            area_sqm=area(a.get("Plocha_uzitna")),
            area_interior_sqm=area(a.get("Plocha_vnitrni")),
            balcony_sqm=area(a.get("Plocha_balkon")),
            terrace_sqm=area(a.get("Plocha_terasa")),
            loggia_sqm=area(a.get("Plocha_lodzie")),
            garden_sqm=area(a.get("Plocha_zahradka")),
            price_czk=price(a.get("Cena")),
            price_excl_vat_czk=price(a.get("Cena_bez_dph")),
            price_with_extras_czk=price(a.get("Cena_prislusenstvi")),
            extras=a.get("Prislusenstvi"),
            status=status_of(a.get("Dostupnost")),
            status_raw=a.get("Dostupnost"),
            source_created_at=a.get("createdAt"),
        ))
    return out


def _js_single_quoted_json(text, var):
    m = re.search(r"var " + re.escape(var) + r"\s*=\s*JSON\.parse\('(.*?)'\);", text, re.S)
    if not m:
        raise ValueError(f"chybí var {var} = JSON.parse(...)")
    return json.loads(m.group(1).replace("\\'", "'"))


def parse_waltrovka_h(text):
    """Nová Waltrovka etapa H (Penta, zkolaudováno 2024). `var flats =
    JSON.parse('...')` v HTML; ceny zůstávají i u prodaných + katastrální
    identifikátory (parcela, číslo budovy, číslo jednotky dle prohlášení
    vlastníka, spoluvlastnický podíl). Patro na webu není -- odvozuje se
    z označení (H2.105: stovky = patro od přízemí, jako u etapy F, kde
    F10.001 = patro 0 a F10.101 = patro 1), proto `floor_derived`."""
    flats = _js_single_quoted_json(text, "flats")
    out = []
    for key, f in flats.items():
        uid = f.get("flat_internal_id") or key
        m = re.search(r"\.(\d)\d\d$", uid)
        floor = int(m.group(1)) + 1 if m else None
        disc = price(f.get("flat_discount_vat"))
        before = price(f.get("flat_price_before_discount_vat"))
        cad = {k: v for k, v in {
            "parcela": f.get("parcel_number"),
            "budova": f.get("flat_building"),
            "jednotka_prohlaseni": f.get("flat_number_owners_declaration"),
            "podil": f.get("flat_number_ownership_share"),
        }.items() if v}
        out.append(unit(
            uid,
            building=uid.split(".")[0],
            floor=floor,
            floor_derived=True if floor is not None else None,
            disposition=disposition(f.get("flat_disposition")),
            area_sqm=area(f.get("flat_area")),
            area_living_sqm=area(f.get("flat_area_living")),
            area_brutto_sqm=area(f.get("flat_area_brutto")),
            balcony_sqm=area(f.get("flat_area_balcony")),
            terrace_sqm=area(f.get("flat_area_terrace")),
            loggia_sqm=area(f.get("flat_area_loggia")),
            garden_sqm=area(f.get("flat_area_garden")),
            price_czk=price(f.get("flat_price")),
            price_excl_vat_czk=price(f.get("flat_price_without_vat")),
            price_before_discount_czk=before if before and before != price(f.get("flat_price")) else None,
            discount_czk=disc,
            price_with_extras_czk=price(f.get("associatedunits_totalprice_vat")),
            extras=f.get("associatedunits_internal_ids"),
            status=status_of(f.get("flat_status")),
            status_raw=f.get("flat_status"),
            cadastre=cad or None,
        ))
    return out


# byty_stav na waltrovka-showcase: na webu 27. 9. všech 694 jednotek „PRODÁNO"
# a všechny mají stav 3. Jiné kódy jsme neviděli -- neuhodnutý kód jde jako
# „jine" se syrovou hodnotou, ne jako odhad.
SHOWCASE_STATUS = {3: "prodano"}


def parse_waltrovka_showcase(text):
    """Rezidence Waltrovka (Penta, dokončeno 2017). JSON endpoint
    /ajax/apartments/, robots.txt ho nezakazuje. Ceny prodaných jsou už
    smazané („-"), takže tu je jen stav, plochy a patro."""
    data = json.loads(text)
    out = []
    for d in data:
        if not d.get("product_name"):
            continue
        st = d.get("byty_stav")
        out.append(unit(
            d["product_name"],
            building=d.get("byty_budova"),
            floor=d.get("byty_podlazi") if isinstance(d.get("byty_podlazi"), int) else None,
            kind_raw=d.get("byty_category_name"),
            disposition=disposition(d.get("byty_dispozice")),
            area_sqm=area(d.get("byty_plocha")),
            balcony_sqm=area(d.get("byty_plocha_balkon")),
            terrace_sqm=area(d.get("byty_plocha_terasa")),
            loggia_sqm=area(d.get("byty_plocha_lodzie")),
            garden_sqm=area(d.get("byty_plocha_zahrada")),
            land_sqm=area(d.get("byty_plocha_pozemek")),
            price_czk=price(d.get("byty_cena_sd")),
            price_excl_vat_czk=price(d.get("byty_cena_bd")),
            status=SHOWCASE_STATUS.get(st, "jine"),
            status_raw=str(st),
        ))
    return out


def parse_hutmanka(text):
    """Rezidence Hutmanka (Na Hutmance 525/6; prodej Svoboda & Williams).
    Next.js, pole `apartments` v datech stránky. Prodaným a rezervovaným web
    cenu maže (price: null) -- přesně to, proč se snímá denně."""
    big = _next_flight(text)
    # První výskyt `"apartments":` jsou popisky sloupců (slovník překladů),
    # data jsou pole objektů.
    i = big.find('"apartments":[{')
    if i < 0:
        raise ValueError('v datech chybí "apartments":[{')
    data, _ = json.JSONDecoder().raw_decode(big, i + len('"apartments":'))
    out = []
    for a in data:
        if not a.get("unitId"):
            continue
        out.append(unit(
            a["unitId"],
            building=a.get("building"),
            floor=floor_from_np(a.get("floor")),
            floor_raw=a.get("floor"),
            disposition=disposition(a.get("disposition")),
            area_sqm=area(a.get("livableArea")),
            balcony_sqm=area(a.get("balconyArea")),
            terrace_sqm=area(a.get("terraceArea")),
            garden_sqm=area(a.get("gardenArea")),
            area_total_sqm=area(a.get("totalArea")),
            price_czk=price(a.get("price")),
            status=status_of(a.get("status")),
            status_raw=a.get("status"),
            source_updated_at=a.get("updatedAt"),
        ))
    return out


def parse_semerinka(text):
    """Semerínka (Crestyl; dokončeno konec 2025). Serverová tabulka
    `tr.b-offers__item` s JSON v data-item + buňky (terasa, stav). Cenu mají
    jen volné; předrezervace i prodané ji mají prázdnou. Kód stavu z data-item
    (0 volný, 1 předrezervace, 2/4/6 web píše „Prodáno") se ukládá syrový."""
    rows = re.findall(r"<tr class=\"b-offers__item[^>]*data-item='([^']*)'[^>]*>(.*?)</tr>",
                      text, re.S)
    out = []
    for item, body in rows:
        d = json.loads(html.unescape(item))
        cells = dict(re.findall(r'<td class="b-offers__(\w+)"[^>]*>(.*?)</td>', body, re.S))
        clean = {k: re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", v))).strip()
                 for k, v in cells.items()}
        state = clean.get("state") or ""
        out.append(unit(
            d.get("no") or clean.get("name"),
            building=clean.get("building") or d.get("building"),
            floor=floor_from_np(d.get("floor")),
            floor_raw=d.get("floor"),
            disposition=disposition(clean.get("disposition")),
            area_sqm=area(d.get("size")),
            terrace_sqm=area(clean.get("terrace")),
            price_czk=price(d.get("price")),
            status=status_of(state),
            status_raw=f"{state} ({d.get('status')})",
        ))
    return out


UKOMINA_HEADER = ["Podlaží", "Jednotka", "Dispozice", "Užitné m²", "Terasa", "Zahrada",
                  "Balkón", "Stav", "Cena"]


def parse_ukomina(text):
    """Rezidence U Komína (Red Group). Web na Frameru: tabulka je v HTML, ale
    bez tříd, jen proud textů. Bere se proto po devíticích „N. NP | B1.1 |
    dispozice | m² | terasa | zahrada | balkón | stav | cena" -- a jen pokud
    stránka má přesně tuhle hlavičku v tomhle pořadí (jinak chyba, ne tichý
    posun sloupců). Rezervovaným web cenu maže („-" nebo „0")."""
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", text, flags=re.S)
    toks = [html.unescape(x).strip() for x in re.split(r"<[^>]+>", t)]
    toks = [re.sub(r"\s+", " ", x) for x in toks if x.strip()]
    joined = "|".join(toks)
    if "|".join(UKOMINA_HEADER) not in joined:
        raise ValueError("hlavička ceníku U Komína se změnila")
    out, seen = [], set()
    for i, x in enumerate(toks):
        if not re.fullmatch(r"\d+\. NP", x) or i + 8 >= len(toks):
            continue
        row = toks[i:i + 9]
        if not re.fullmatch(r"[A-Z]\d+\.\d+", row[1]) or row[1] in seen:
            continue
        seen.add(row[1])
        out.append(unit(
            row[1],
            building=re.match(r"[A-Z]+", row[1]).group(0),
            floor=floor_from_np(row[0]),
            floor_raw=row[0],
            disposition=disposition(row[2]),
            area_sqm=area(row[3]),
            terrace_sqm=area(row[4]),
            garden_sqm=area(row[5]),
            balcony_sqm=area(row[6]),
            price_czk=price(row[8]),
            status=status_of(row[7]),
            status_raw=row[7],
        ))
    return out


PROKOPSKA_HEADER = ["Č. bytu", "Podlaží", "Dispozice", "Plocha [m²]",
                    "Plocha příslušenství [m²]", "Cena bez DPH", "Cena s DPH", "Stav"]


def parse_prokopska(text):
    """Rezidence Prokopská vyhlídka (Sekyra Group; výstavba, 2027). Stránka
    /jednotky je serverová tabulka (Nette). Rezervovaným web cenu maže."""
    head = re.search(r"<thead.*?</thead>", text, re.S)
    if not head or _cells(head.group(0))[:8] != PROKOPSKA_HEADER:
        raise ValueError("hlavička tabulky Prokopské vyhlídky se změnila")
    body = text[text.find("<tbody"):text.find("</tbody>")]
    out = []
    for row in re.findall(r"<tr.*?</tr>", body, re.S):
        c = _cells(row)
        if len(c) < 8 or not c[0]:
            continue
        m = re.search(r'href="/jednotky/(\d+)"', row)
        out.append(unit(
            c[0],
            building=c[0].split(".")[0],
            floor=floor_from_np(c[1]),
            disposition=disposition(c[2]),
            area_sqm=area(c[3]),
            accessory_area_sqm=area(c[4]),
            price_excl_vat_czk=price(c[5]),
            price_czk=price(c[6]),
            status=status_of(c[7]),
            status_raw=c[7],
            source_id=m.group(1) if m else None,
        ))
    return out


# --- Projekty ---------------------------------------------------------------- #
# kind: stejné třídy jako novostavby.py (dokoncena = kolaudace 2020+,
# vystavba = ve výstavbě / projekt, starsi = dřív). Podle nich se vybírají
# srovnatelné inzeráty ze Sreality -- dokončené se nemíchají s výstavbou.
# lat/lon: bod projektu (Nominatim 27. 9.), pro párování s inzeráty (≤ 250 m).
PROJECTS = {
    "nova-waltrovka-f": {
        "name": "Nová Waltrovka – etapa F", "developer": "Penta Real Estate",
        "url": "https://novawaltrovka.cz/cenik/", "parser": parse_waltrovka_f,
        "kind": "vystavba", "kind_note": "dokončení Q1 2027",
        "lat": 50.05727, "lon": 14.38245, "location_note": "nám. Augustina Bubníka",
        "gone_means": "rezervováno/prodáno (web prodané z ceníku vyřazuje)",
        "access_note": "robots.txt neexistuje (404); podmínky zakazují jen zásah do obsahu webu",
    },
    "nova-waltrovka-h": {
        "name": "Nová Waltrovka – etapa H", "developer": "Penta Real Estate",
        "url": "https://www.etapah.novawaltrovka.cz/bydleni", "parser": parse_waltrovka_h,
        "kind": "dokoncena", "kind_note": "H1/H2 zkolaudováno 2024",
        "lat": 50.05727, "lon": 14.38245, "location_note": "Nová Waltrovka (přesná poloha domu neověřena)",
        "access_note": "robots.txt neexistuje (404)",
    },
    "rezidence-waltrovka": {
        "name": "Rezidence Waltrovka", "developer": "Penta Real Estate",
        "url": "https://www.waltrovka-showcase.website/ajax/apartments/",
        "parser": parse_waltrovka_showcase,
        "kind": "starsi", "kind_note": "dokončeno 2017, vyprodáno",
        "lat": 50.0563, "lon": 14.3752, "location_note": "Kačírkova",
        "access_note": "robots.txt /ajax/ nezakazuje; ceny už smazané, jen stav",
        "prices_wiped": True,
    },
    "rezidence-hutmanka": {
        "name": "Rezidence Hutmanka", "developer": "Svoboda & Williams (prodej)",
        "url": "https://www.rezidencehutmanka.cz/cenik", "parser": parse_hutmanka,
        "kind": "vystavba", "kind_note": "dokončení Q4 2027",
        "lat": 50.05590, "lon": 14.36784, "location_note": "Na Hutmance 525/6",
        "access_note": "robots.txt: Allow /",
    },
    "semerinka": {
        "name": "Semerínka", "developer": "Crestyl",
        "url": "https://www.semerinka.cz/cenik", "parser": parse_semerinka,
        "kind": "dokoncena", "kind_note": "dokončení konec 2025, předání Q1 2026",
        "lat": 50.05547, "lon": 14.38247, "location_note": "U Komína (OSM „Semerínka“)",
        "access_note": "robots.txt: prázdné Disallow",
    },
    "rezidence-u-komina": {
        "name": "Rezidence U Komína", "developer": "Red Group",
        "url": "https://www.ukomina.cz/cen%C3%ADk", "parser": parse_ukomina,
        "kind": "vystavba", "kind_note": "nastěhování do konce 2027",
        "lat": 50.05563, "lon": 14.38254, "location_note": "U Komína",
        "access_note": "robots.txt: Allow /",
    },
    "prokopska-vyhlidka": {
        "name": "Rezidence Prokopská vyhlídka", "developer": "Sekyra Group",
        "url": "https://www.rezidenceprokopskavyhlidka.cz/jednotky", "parser": parse_prokopska,
        "kind": "vystavba", "kind_note": "dokončení 2027",
        "lat": 50.0482, "lon": 14.3588,
        "location_note": "mezi Radlickou a Novoveskou (přibližně, u Radlické 355)",
        "access_note": "robots.txt zakazuje jen /admin/",
    },
}

NOT_COVERED = {
    "Rezidence Radlická vyhlídka (BEMETT)":
        "ceník na premiovebydleni.cz je za anti-bot výzvou (JS nastaví cookie "
        "bot_verified a stránku znovu načte) -- obcházet ji nebudeme",
    "Panorama Jinonice (CREDITAS)":
        "v přípravě, veřejný ceník po jednotkách zatím neexistuje",
    "Jinonický Dvůr (CREDITAS)":
        "vyprodáno, web přesměrovává na katalog novostavby.com -- ceník už není",
    "náměstí Augustina Bubníka":
        "není samostatný projekt, je to Nová Waltrovka (etapa F je pokrytá)",
}


# --- Stav a rozdíl ----------------------------------------------------------- #
# Pole jednotky, která se berou z posledního zdravého stažení (vše kromě
# sledovacích polí níže).
TRACK_FIELDS = ("first_seen", "last_seen", "last_change_at", "gone_at", "returned_at",
                "last_price_czk", "last_price_at", "price_hidden_at", "baseline")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_live(u):
    return not u.get("gone_at")


def _event(at, slug, u, typ, old=None, new=None):
    ev = {"at": at, "project": slug, "unit": u["id"], "type": typ,
          "disposition": u.get("disposition"), "area_sqm": u.get("area_sqm")}
    if old is not None:
        ev["old"] = old
    if new is not None:
        ev["new"] = new
    lp = u.get("price_czk") or u.get("last_price_czk")
    if lp:
        ev["price_czk"] = lp
    return ev


def health_problem(prev_units, fetched):
    """Důvod, proč tomuhle stažení nevěřit, nebo None."""
    if not fetched:
        return "parser vrátil 0 jednotek"
    live_before = sum(1 for u in (prev_units or {}).values() if is_live(u))
    if (live_before >= MIN_UNITS_FOR_DROP_GUARD
            and len(fetched) < live_before * (1 - MAX_DROP_SHARE)):
        return (f"počet jednotek spadl z {live_before} na {len(fetched)} -- "
                "spíš rozbitá stránka než výprodej")
    ids = [u["id"] for u in fetched]
    if len(set(ids)) != len(ids):
        return "duplicitní označení jednotek -- parser čte špatně"
    return None


def merge(prev_state, fetched, at, slug):
    """Nový stav projektu + seznam událostí. Čisté, bez sítě a disku.

    prev_state None = první běh projektu: tichá baseline, jediná událost
    „baseline" (bez alertu). Jinak:
      new           jednotka, která v ceníku dřív nebyla
      price         změna ceny (obě strany známé)
      price_hidden  cena zmizela (typicky při rezervaci/prodeji); poslední
                    známá cena zůstává v `last_price_czk`
      price_shown   cena se znovu objevila (bez předchozí ceny = jen informace)
      status        změna stavu (volný -> rezervace ...)
      gone          jednotka z ceníku zmizela (zdroj je zdravý)
      returned      zmizelá jednotka je zpátky
    """
    prev_units = (prev_state or {}).get("units") or {}
    baseline = prev_state is None
    units, events = {}, []
    for f in fetched:
        uid = f["id"]
        old = prev_units.get(uid)
        u = dict(f)
        if old is None:
            u["first_seen"] = at
            u["last_change_at"] = at
            if baseline:
                u["baseline"] = True
            else:
                events.append(_event(at, slug, u, "new", new=u.get("status")))
        else:
            for k in TRACK_FIELDS:
                if old.get(k) is not None:
                    u[k] = old[k]
            changed = False
            if old.get("gone_at"):
                u.pop("gone_at", None)
                u["returned_at"] = at
                events.append(_event(at, slug, u, "returned", new=u.get("status")))
                changed = True
            op, np_ = old.get("price_czk"), u.get("price_czk")
            if op and np_ and op != np_:
                events.append(_event(at, slug, u, "price", old=op, new=np_))
                changed = True
            elif op and not np_:
                events.append(_event(at, slug, u, "price_hidden", old=op))
                u["price_hidden_at"] = at
                changed = True
            elif np_ and not op:
                events.append(_event(at, slug, u, "price_shown", old=old.get("last_price_czk"), new=np_))
                u.pop("price_hidden_at", None)
                changed = True
            if old.get("status") != u.get("status"):
                events.append(_event(at, slug, u, "status", old=old.get("status"), new=u.get("status")))
                changed = True
            if changed:
                u["last_change_at"] = at
        if u.get("price_czk"):
            if u.get("last_price_czk") != u["price_czk"]:
                u["last_price_at"] = at
            u["last_price_czk"] = u["price_czk"]
        u["last_seen"] = at
        units[uid] = u
    for uid, old in prev_units.items():
        if uid in units:
            continue
        u = dict(old)
        if is_live(old):
            u["gone_at"] = at
            u["last_change_at"] = at
            events.append(_event(at, slug, u, "gone", old=old.get("status")))
        units[uid] = u
    if baseline:
        events = [{"at": at, "project": slug, "type": "baseline", "units": len(units)}]
    return {k: units[k] for k in sorted(units)}, events


def project_meta(slug):
    p = PROJECTS[slug]
    return {k: v for k, v in p.items() if k != "parser"}


def update_project(slug, prev_state, fetched, error, at):
    """(nový stav, události, varování). Chyba nebo nezdravé stažení = starý
    stav beze změny (jen čas a důvod posledního selhání), žádné události."""
    meta = project_meta(slug)
    if error is None:
        error = health_problem((prev_state or {}).get("units"), fetched)
    if error is not None:
        state = dict(prev_state or {"units": {}})
        state.update({"project": slug, **meta, "last_error": error, "last_error_at": at})
        return state, [], f"{slug}: {error} -- stav ponechán"
    units, events = merge(prev_state, fetched, at, slug)
    state = {"project": slug, **meta,
             "baseline_at": (prev_state or {}).get("baseline_at") or at,
             "fetched_at": at, "last_ok_at": at, "units": units}
    return state, events, None


# --- Disk -------------------------------------------------------------------- #
def load_state(slug, data_dir=DATA_DIR):
    p = Path(data_dir) / f"{slug}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(slug, state, data_dir=DATA_DIR):
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    # Stav, který nikdy nebyl zdravý (první běh spadl), se neukládá: soubor
    # bez jednotek by příští běh vzal jako existující stav a nová data by
    # hlásil jako „nové jednotky" místo tiché baseline.
    if not state.get("units") and not state.get("baseline_at"):
        return
    tmp = d / f".{slug}.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=False) + "\n",
                   encoding="utf-8")
    tmp.replace(d / f"{slug}.json")


def append_history(events, data_dir=DATA_DIR):
    if not events:
        return
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / HISTORY_FILE, "a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")


def load_all(data_dir=DATA_DIR):
    """{slug: state} pro všechny uložené projekty (i ty, co už v PROJECTS
    nejsou -- data se nemažou)."""
    out = {}
    d = Path(data_dir)
    if not d.exists():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"::warning::developers/{p.name} nejde přečíst: {exc}", file=sys.stderr)
    return out


def load_history(data_dir=DATA_DIR):
    p = Path(data_dir) / HISTORY_FILE
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


# --- Alert ------------------------------------------------------------------- #
ALERT_TYPES = ("status", "price", "gone", "returned", "new", "price_hidden")


def _czk(v):
    if not v:
        return "—"
    return f"{int(v):,}".replace(",", " ") + " Kč"


def _mil(v):
    return f"{v / 1e6:.2f}".replace(".", ",") + " M" if v else "—"


def _event_line(e, states):
    st = states.get(e["project"]) or {}
    u = (st.get("units") or {}).get(e["unit"]) or {}
    area_ = u.get("area_sqm") or e.get("area_sqm")
    head = f"{html.escape(e['unit'])} {html.escape(e.get('disposition') or '?')}"
    if area_:
        head += f" {str(area_).replace('.', ',')} m²"
    t = e["type"]
    if t == "price":
        pct = (e["new"] - e["old"]) / e["old"] * 100
        what = f"💰 {_czk(e['old'])} → {_czk(e['new'])} ({pct:+.1f} %)".replace(".", ",", 1)
    elif t == "status":
        what = (f"🔁 {STATUS_LABELS.get(e.get('old'), e.get('old'))} → "
                f"{STATUS_LABELS.get(e.get('new'), e.get('new'))}")
        if e.get("price_czk"):
            what += f" · cena {_czk(e['price_czk'])}"
    elif t == "gone":
        meaning = st.get("gone_means") or "zmizelo z ceníku"
        what = f"❌ {html.escape(meaning)} · naposledy {_czk(e.get('price_czk'))}"
    elif t == "returned":
        what = f"↩️ zpět v ceníku ({STATUS_LABELS.get(e.get('new'), e.get('new'))})"
    elif t == "price_hidden":
        what = f"🙈 cena skryta · naposledy {_czk(e.get('old'))}"
    else:
        what = f"🆕 nová ({STATUS_LABELS.get(e.get('new'), e.get('new'))}) · {_czk(e.get('price_czk'))}"
    return f"{head} · {what}"


def build_alert(events, states, dashboard_url=PAGES_URL):
    """Jedna seskupená zpráva za běh, nebo None. 4+kk a 5+kk podrobně a
    první, ostatní stručně (do MAX_ALERT_OTHER_LINES) a zbytek jako počty.
    Baseline se nehlásí nikdy (merge ji z událostí jednotek nevyrábí)."""
    evs = [e for e in events if e.get("type") in ALERT_TYPES and e.get("unit")]
    # Skrytí ceny jde v jednom běhu skoro vždy spolu se změnou stavu té
    # jednotky -- hlásí se jen, když stav zůstal (cena zmizela „potichu").
    with_status = {(e["project"], e["unit"]) for e in evs if e["type"] == "status"}
    evs = [e for e in evs if not (e["type"] == "price_hidden"
                                  and (e["project"], e["unit"]) in with_status)]
    if not evs:
        return None
    order = {t: i for i, t in enumerate(ALERT_TYPES)}
    evs.sort(key=lambda e: (e["project"], order[e["type"]], e["unit"]))
    focus = [e for e in evs if e.get("disposition") in FOCUS_DISPOSITIONS]
    other = [e for e in evs if e.get("disposition") not in FOCUS_DISPOSITIONS]
    lines = ["<b>🏗️ Ceníky developerů · U Kříže</b>"]

    def name(slug):
        return html.escape((states.get(slug) or {}).get("name") or PROJECTS.get(slug, {}).get("name") or slug)

    if focus:
        lines.append("<b>4+kk / 5+kk</b>")
        last = None
        for e in focus[:MAX_ALERT_DETAIL_LINES]:
            if e["project"] != last:
                lines.append(f"<i>{name(e['project'])}</i>")
                last = e["project"]
            lines.append(_event_line(e, states))
        if len(focus) > MAX_ALERT_DETAIL_LINES:
            lines.append(f"… a dalších {len(focus) - MAX_ALERT_DETAIL_LINES}")
    if other:
        lines.append("<b>Ostatní dispozice</b>")
        last = None
        for e in other[:MAX_ALERT_OTHER_LINES]:
            if e["project"] != last:
                lines.append(f"<i>{name(e['project'])}</i>")
                last = e["project"]
            lines.append(_event_line(e, states))
        rest = other[MAX_ALERT_OTHER_LINES:]
        if rest:
            counts = {}
            for e in rest:
                counts[e["type"]] = counts.get(e["type"], 0) + 1
            label = {"status": "změn stavu", "price": "změn ceny", "gone": "zmizelých",
                     "returned": "vrácených", "new": "nových", "price_hidden": "skrytých cen"}
            lines.append("… navíc " + ", ".join(f"{n} {label[t]}" for t, n in counts.items()))
    if dashboard_url:
        lines.append(f'<a href="{html.escape(dashboard_url, quote=True)}">dashboard</a>')
    return "\n".join(lines)


def send_alert(events, states, *, dry_run=False):
    """Nikdy nevyhodí výjimku -- alert nesmí shodit uložení dat."""
    try:
        text = build_alert(events, states)
        if not text:
            print("Ceníky: žádná změna k nahlášení.", file=sys.stderr)
            return None
        import notify
        return notify.send_text(text, dry_run=dry_run, what="Alert ceníků")
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::alert ceníků se neodeslal: {exc}", file=sys.stderr)
        return None


# --- Běh --------------------------------------------------------------------- #
def fetch_project(slug, getter=_get):
    """(jednotky, chyba). Nikdy nevyhodí."""
    p = PROJECTS[slug]
    try:
        return p["parser"](getter(p["url"])), None
    except Exception as exc:  # noqa: BLE001 -- jeden web nesmí shodit ostatní
        return [], f"{type(exc).__name__}: {str(exc)[:200]}"


def run(slugs=None, *, getter=_get, data_dir=DATA_DIR, at=None, save=True):
    """Stáhne projekty, sloučí se stavem, uloží. Vrací (states, events, warnings)."""
    at = at or now_iso()
    states, all_events, warnings = {}, [], []
    for slug in slugs or PROJECTS:
        prev = load_state(slug, data_dir)
        fetched, err = fetch_project(slug, getter)
        state, events, warn = update_project(slug, prev, fetched, err, at)
        states[slug] = state
        all_events += events
        if warn:
            warnings.append(warn)
            print(f"::warning::ceník {warn}", file=sys.stderr)
        else:
            counts = {}
            for u in state["units"].values():
                if is_live(u):
                    counts[u.get("status")] = counts.get(u.get("status"), 0) + 1
            print(f"{slug}: {sum(counts.values())} jednotek {counts}, "
                  f"{len([e for e in events if e['type'] != 'baseline'])} událostí"
                  + (" (baseline)" if prev is None else ""), file=sys.stderr)
        if save:
            save_state(slug, state, data_dir)
    if save:
        append_history(all_events, data_dir)
    return states, all_events, warnings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="alert jen vypsat")
    ap.add_argument("--no-save", action="store_true", help="nic nezapisovat")
    ap.add_argument("--project", action="append", help="jen tenhle projekt (lze opakovat)")
    args = ap.parse_args(argv)
    for s in args.project or []:
        if s not in PROJECTS:
            ap.error(f"neznámý projekt {s}; známé: {', '.join(PROJECTS)}")
    states, events, warnings = run(args.project, save=not args.no_save)
    if not args.no_save or args.dry_run:
        send_alert(events, states, dry_run=args.dry_run or args.no_save)
    # Selhání jednoho webu není selhání běhu (ostatní se uložily); všechny
    # naráz ano -- to je spíš síť nebo náš kód a má to být vidět červeně.
    if warnings and len(warnings) == len(args.project or PROJECTS):
        print("::error::žádný ceník se nepodařilo stáhnout", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
