#!/usr/bin/env python3
"""Týdenní segmenty trhu. Jen agregace, nic osobního.

Počítá se z poolu a nic nestahuje. Veřejný JSON i karta na dashboardu obsahují
výhradně součty, mediány a příznaky po segmentech. Žádné id inzerátu, adresa,
splátka, výše úvěru, pokrytí nájmu ani konkrétní byt. Modul alerty neposílá —
příznaky si přečte pondělní souhrn.

Segment s méně než MIN_N aktivními inzeráty má ``malo_dat`` a z většího posunu
ani z krizové triády nikdy nevzejde.

Spuštění nad lokálním poolem (bez scrapu, bez odeslání):

    python segments.py
    python segments.py --week 2026-W41 --output /tmp/weekly_segments.json
"""
import argparse
import html
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import deal_basis
import market
import pool as poolmod

ROOT = Path(__file__).parent
OUTPUT_NAME = "weekly_segments.json"
OUTPUT_PATH = ROOT / OUTPUT_NAME

SCHEMA = "sreality-tracker.weekly-segments.v1"
MIN_N = 20
# Stejná podlaha jako pool.quantiles: medián z hrstky inzerátů se nepublikuje.
MEDIAN_MIN_N = 8
# Konec srpna 2026 změnil pokrytí. Týden, který skončil před 1. 9., do mediánu
# Kč/m² nepatří. Srovnání počtu aktivních proti takovému týdnu taky ne.
COVERAGE_FROM = datetime(2026, 9, 1, tzinfo=timezone.utc)
SERIES_WEEKS = 10  # hlášený týden + 9 předchozích (8týdenní průměr + rezerva)

MEDIAN_WW_PCT = 3.0
MEDIAN_VS_4W_PCT = 5.0
DISCOUNT_SHARE_PP = 5.0
DISCOUNT_SHARE_WEEKS = 8
AVG_DISCOUNT_PCT = 3.0
DURATION_VS_4W_PCT = 20.0
DURATION_WEEKS = 4
ACTIVE_COUNT_PCT = 15.0
CRISIS_MIN_SEGMENTS = 2

SMALL_DISPS = {"1+kk", "1+1", "2+kk"}
MEDIUM_DISPS = {"2+1", "3+kk"}
# Garsonka je v inzerci totéž co malý byt bez oddělené místnosti.
SMALL_ALIASES = {"garsonka", "garsoniéra", "garsoniera", "garsonier"}
_DISP_RE = re.compile(r"^(\d+)\+(kk|\d+)$")

TXS = (("prodej", "Prodej"), ("pronajem", "Pronájem"))
AXES = (
    ("typ_domu", "typ", (
        ("novostavba", "Novostavba"),
        ("starsi_cihla", "Starší cihla"),
        ("panel", "Panel"),
        ("neurceno", "Neurčeno"),
    )),
    ("velikost", "velikost", (
        ("maly", "Malý"),
        ("stredni", "Střední"),
        ("velky", "Velký"),
        ("neurceno", "Neurčeno"),
    )),
    ("lokalita", "lokalita", (
        ("praha9", "Vysočany a okolí (P9, Libeň, Karlín, Žižkov)"),
        ("zbytek", "Jinonice a další sledované oblasti (ne celá Praha)"),
    )),
    ("reference", "reference", (
        ("novostavba_1kk_25_35_praha9",
         "Referenční: novostavba 1+kk 25–35 m², Vysočany a okolí (P9, Libeň, Karlín, Žižkov)"),
    )),
)
REFERENCE_LO, REFERENCE_HI = 25.0, 35.0

# Veřejný dokument smí mít jen tyto klíče. Cokoli navíc (řádek inzerátu,
# osobní pole) je chyba, ne tichý průchod.
TOP_KEYS = {
    "schema", "generated_at", "week", "week_start", "week_end", "as_of",
    "coverage_from", "min_n", "personal_data", "note", "definitions",
    "thresholds", "krizovy_signal", "krizovy_signal_tydny", "krizove_segmenty",
    "vetsi_posun_n", "segment_count", "segments",
}
SEGMENT_KEYS = {
    "segment_id", "transaction", "axis", "key", "label", "n", "malo_dat",
    "active_count", "same_listings_n", "same_listings_avg_change_pct",
    "same_listings_avg_discount_pct", "price_cut_share_pct", "price_cut_n",
    "price_cut_base_n", "median_days_on_market", "days_on_market_n",
    "median_kc_m2", "median_kc_m2_n", "median_kc_m2_note", "coverage_stable",
    "coverage_note", "vetsi_posun", "vetsi_posun_duvody", "nevyhodnoceno",
    "triada", "series",
}
SERIES_KEYS = {
    "week", "n", "active_count", "same_listings_n", "same_listings_avg_change_pct",
    "same_listings_avg_discount_pct", "price_cut_share_pct", "price_cut_n",
    "price_cut_base_n", "median_days_on_market", "days_on_market_n",
    "median_kc_m2", "median_kc_m2_n", "median_kc_m2_note",
}
REASON_KEYS = {"code", "text"}
DEFINITIONS = {
    "typ_domu": (
        "Novostavba, panel a starší cihla jsou třídy deal_basis.building_class "
        "(pole house_class). Starší cihla = třída starsi: cihla, skelet, smíšená, "
        "kamenná, montovaná i dřevostavba, pokud to není novostavba ani panel. "
        "Samostatný koš jen pro cihlu data nemají. Bez třídy (ani štítek, ani typ, "
        "ani is_new_building) = neurčeno; do cihly se to nedomýšlí."
    ),
    "velikost": (
        "Malý: 1+kk, 1+1 nebo 2+kk (a garsonka) a plocha chybí nebo je ≤ 55 m². "
        "Střední: 2+1 a 3+kk, dokud plocha není nad 75 m²; také malá dispozice "
        "s plochou nad 55 m² a do 75 m². Velký: dispozice 3+1 a větší, nebo "
        "jakákoli dispozice s plochou nad 75 m². Přesně 55 m² je ještě malý, "
        "přesně 75 m² ještě není velké jen kvůli ploše. Neznámá dispozice se "
        "řadí jen podle plochy; bez dispozice i bez plochy = neurčeno."
    ),
    "lokalita": (
        "Vysočany a okolí = oblast vysocany. Není to administrativní Praha 9: "
        "kruh bere Vysočany, Libeň, Karlín, Žižkov, Hloubětín, Hrdlořezy a "
        "Malešice a na okraji i sousední čtvrtě. Záznam bez area patří sem, "
        "stejně jako ve zbytku trackeru. Druhý koš není zbytek Prahy. Jsou to "
        "jen Jinonice a další sledované oblasti (Jinonice, Radlice, Košíře, "
        "Stodůlky, Hlubočepy a jejich okraj). Čtvrtě se nedělí: část inzerátů "
        "nemá city_part a kruh přetahuje i cizí čtvrť, takže řez podle názvu "
        "čtvrti by lhal. Tracker má dvě oblasti."
    ),
    "reference": (
        "Novostavba, dispozice 1+kk, plocha 25–35 m² včetně, oblast Vysočany "
        "a okolí (P9, Libeň, Karlín, Žižkov a sousední čtvrtě v tom kruhu). "
        "Prodej a pronájem zvlášť. Je to pásmo trhu, ne konkrétní byt."
    ),
    "aktivni": (
        "Stejná množina jako sale_count a rent_count ve snapshotu: inzerát je "
        "v posledním sběru (last_seen je okamžik toho snapshotu), má Kč/m², "
        "není exclude_from_stats, nemá area_mismatch a není uklizená zombie. "
        "Inzerát, který ze snapshotu vypadl a nemá potvrzené gone_at, se "
        "nepočítá. Dřív stačilo last_seen od začátku týdne, řada proto jen "
        "rostla. Starší týdny berou inzerát viděný ještě v neděli nebo později "
        "(last_seen >= konec týdne), s first_seen do konce týdne a bez gone_at "
        "před koncem. Hlášený týden je poslední uzavřený a jeho počet je počet "
        "ve snapshotu, včetně inzerátů přibylých až v rozpracovaném týdnu. "
        "stats.sale_count je jen Vysočany a okolí; Jinonice mají vlastní "
        "sale_count v area_stats. Součet obou lokalit je součet obou."
    ),
    "stejne_inzeraty": (
        "Průnik aktivních inzerátů na konci tohoto a minulého týdne. Změna ceny "
        "je průměr (cena teď − cena minule) / cena minule. Pronájem bere celkovou "
        "cenu, prodej cenu bytu. Průměrná sleva je průměr kladných slev, beze "
        "slevy se počítá nula. Kdo nemá cenu známou k oběma koncům, v průniku "
        "není. Pronájem bez poplatků a cenový outlier do cen nevstupují."
    ),
    "podil_slev": (
        "Podíl aktivních inzerátů s použitelnou cenou, jejichž cena na konci "
        "týdne je nižší než na konci minulého. Kdo se objevil až tento týden, "
        "srovnává se s první cenou v tomto týdnu."
    ),
    "kc_m2": (
        "Medián Kč/m² aktivních inzerátů s použitelnou cenou, i s n. Týden, "
        "který končí před 1. 9. 2026, se neuvádí (změna pokrytí na konci srpna). "
        "Medián z méně než 8 inzerátů se neuvádí."
    ),
    "doba": (
        "Medián dnů na trhu podle portálového since, ke konci týdne. Pod 8 "
        "inzerátů se medián neuvádí."
    ),
    "malo_dat": (
        "Aktivních inzerátů méně než 20. Větší posun ani krizová triáda z "
        "takového segmentu nevzejde. Tenhle modul alerty neposílá."
    ),
    "pokryti": (
        "Poznámka pokrytí se pořád počítá: týden před 1. 9. 2026, změna "
        "konfigurace sběru, nová oblast nebo nový zdroj. Příznak skoku počtu "
        "aktivních se z ní nespouští. Je vypnutý, dokud se počet neověří proti "
        "snapshotu. Metrika active_count v souhrnu zůstává."
    ),
    "krize": (
        "Krizový signál: v jedné ose a jednom typu obchodu (typ domu, velikost "
        "nebo lokalita — ne referenční pásmo) jsou aspoň dva segmenty v triádě, "
        "a totéž platí i předchozí týden. Triáda = roste podíl slev, roste "
        "medián doby na trhu a klesá cena stejných inzerátů. Osy se nesčítají, "
        "aby jeden koš viděný z několika řezů nebyl dvěma segmenty."
    ),
}
THRESHOLDS = {
    "min_n": MIN_N,
    "median_min_n": MEDIAN_MIN_N,
    "median_ww_pct": MEDIAN_WW_PCT,
    "median_vs_4w_pct": MEDIAN_VS_4W_PCT,
    "discount_share_pp": DISCOUNT_SHARE_PP,
    "discount_share_weeks": DISCOUNT_SHARE_WEEKS,
    "avg_discount_pct": AVG_DISCOUNT_PCT,
    "avg_discount_compare": "gt",
    "duration_vs_4w_pct": DURATION_VS_4W_PCT,
    "duration_weeks": DURATION_WEEKS,
    "active_count_pct": ACTIVE_COUNT_PCT,
    # Metrika zůstává. Příznak je vypnutý, dokud se počet neověří proti snapshotu.
    "active_count_flag": False,
    "krizovy_min_segments": CRISIS_MIN_SEGMENTS,
    "krizovy_weeks": 2,
    "coverage_from": "2026-09-01",
}

FORBIDDEN_KEYS = {
    "id", "url", "street", "title", "description", "price_history", "seller",
    "seller_name", "phone", "email", "mortgage", "annuity", "anuita", "own",
    "owncard", "own_card", "ltv", "payment_czk", "loan", "hypoindex",
}
FORBIDDEN_SUBSTRINGS = (
    "owncard", "709/r", "mortgage", "anuita", "hypoindex",
    "own_price", "own_ltv", "own_deposit", "own_extra", "m0rtgage",
)


def _r1(value):
    return None if value is None else round(float(value), 1)


def _norm_disp(value):
    text = (value or "").strip().lower().replace(" ", "")
    if text in SMALL_ALIASES:
        return "1+kk"
    return text


def _sqm(rec):
    raw = rec.get("floor_area_sqm")
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _large_disp(disp):
    match = _DISP_RE.fullmatch(disp or "")
    if not match:
        return False
    rooms = int(match.group(1))
    rest = match.group(2)
    if rooms >= 4:
        return True
    return rooms == 3 and rest != "kk"


def size_band(rec):
    """Malý / střední / velký / neurčeno. První pravidlo, které sedí, vyhrává.

    Hrany jsou v DEFINITIONS['velikost']: plocha nad 75 m² je velká i u 2+kk,
    3+1 je velké i pod 75 m², 2+kk mezi 55 a 75 m² je střední.
    """
    disp = _norm_disp(rec.get("disposition"))
    sqm = _sqm(rec)
    if sqm is not None and sqm > 75:
        return "velky"
    if _large_disp(disp):
        return "velky"
    if disp in MEDIUM_DISPS:
        return "stredni"
    if disp in SMALL_DISPS:
        if sqm is None or sqm <= 55:
            return "maly"
        return "stredni"
    if sqm is None:
        return "neurceno"
    if sqm <= 55:
        return "maly"
    return "stredni"


def house_segment(rec, now_year):
    """Novostavba / starší cihla / panel / neurčeno.

    Uložená house_class má přednost (počítala se i z popisu). Jinak
    deal_basis.building_class z toho, co pool drží. is_new_building doplní
    novostavbu jen když z polí třída nevypadne — ne aby přebila panel.
    """
    stored = rec.get("house_class")
    if stored in ("novostavba", "panel", "starsi"):
        cls = stored
    else:
        cls, _src = deal_basis.building_class(rec, now_year)
        if cls is None and rec.get("is_new_building") is True:
            cls = "novostavba"
    return {"novostavba": "novostavba", "panel": "panel", "starsi": "starsi_cihla"}.get(
        cls, "neurceno"
    )


def location_band(rec):
    """Oblast vysocany (včetně záznamu bez area) vs. ostatní sledované oblasti.

    Klíč praha9 není administrativní Praha 9. Štítek říká, které čtvrtě v
    kruhu leží. Druhý klíč není celá Praha, jen Jinonice a další sledované.
    """
    return "praha9" if poolmod.area_of(rec) == poolmod.HOME_AREA else "zbytek"


def in_reference(rec, now_year):
    sqm = _sqm(rec)
    return (
        house_segment(rec, now_year) == "novostavba"
        and _norm_disp(rec.get("disposition")) == "1+kk"
        and sqm is not None
        and REFERENCE_LO <= sqm <= REFERENCE_HI
        and location_band(rec) == "praha9"
    )


def catalog():
    """Stabilní seznam segmentů. Prázdný koš v JSON zůstane, ať job zná schéma."""
    rows = []
    for tx, tx_label in TXS:
        for axis, slug, keys in AXES:
            for key, label in keys:
                rows.append({
                    "segment_id": f"{tx}:{slug}:{key}",
                    "transaction": tx,
                    "axis": axis,
                    "key": key,
                    "label": f"{tx_label} · {label}",
                })
    return rows


def segment_ids_for(rec, now_year):
    tx = rec.get("transaction_type")
    if tx not in ("prodej", "pronajem"):
        return []
    ids = [
        f"{tx}:typ:{house_segment(rec, now_year)}",
        f"{tx}:velikost:{size_band(rec)}",
        f"{tx}:lokalita:{location_band(rec)}",
    ]
    if in_reference(rec, now_year):
        ids.append(f"{tx}:reference:novostavba_1kk_25_35_praha9")
    return ids


def _in_stats(rec):
    """Řádek, který compute_stats pustí do sale_count / rent_count.

    Bez Kč/m², s exclude_from_stats nebo s area_mismatch ve statistice není.
    Aktivní počet má být tahle množina, ne širší pool.
    """
    if rec.get("exclude_from_stats") or rec.get("gone_stale") or rec.get("area_mismatch"):
        return False
    if rec.get("transaction_type") not in ("prodej", "pronajem"):
        return False
    return bool(rec.get("price_czk_per_sqm"))


def _observation_instant(records, as_of_dt):
    """Okamžik posledního sběru, který as_of už vidí.

    Po update_from_snapshot mají inzeráty ve snapshotu last_seen rovné
    generated_at a ostatní starší. To je celá živá množina, i když se segmenty
    spouští o chvíli později a as_of je „teď“.
    """
    latest = None
    for rec in records:
        seen = poolmod.parse_ts(rec.get("last_seen"))
        if seen is None or seen > as_of_dt:
            continue
        if latest is None or seen > latest:
            latest = seen
    return latest


def is_active(rec, start, end, live_at, snapshot_week):
    """V nabídce. Hlášený týden = přesně snapshot. Starší týden = viděn v neděli.

    Viz DEFINITIONS['aktivni']. snapshot_week je jen poslední uzavřený týden:
    tam se počet musí rovnat sale_count / rent_count, ne součtu všeho, co
    pool ještě neoznačil jako zmizelé.
    """
    if not _in_stats(rec):
        return False
    seen = poolmod.parse_ts(rec.get("last_seen"))
    if seen is None:
        return False
    gone = poolmod.parse_ts(rec.get("gone_at"))
    if snapshot_week:
        # Ve snapshotu je přesně to, co má last_seen rovné poslednímu sběru.
        # gone_at se tu nečte: sale_count ho taky nečte, a řádek ve snapshotu
        # už v nabídce je.
        return live_at is not None and seen == live_at
    first = poolmod.parse_ts(rec.get("first_seen"))
    if first is None or first > end:
        return False
    if gone is not None and gone <= end:
        return False
    # Neděle včetně. last_seen >= začátek týdne nechávalo v týdnu i inzerát,
    # který jsme viděli v pondělí a do neděle už v nabídce nebyl, a hlavně
    # každý pozdější sběr — řada pak jen rostla.
    return seen >= end


def closed_week(as_of_dt, week=None):
    """Poslední ISO týden, který vůči as_of už skončil.

    Vyžádaný týden, který ještě běží, se nepoužije. Pondělní souhrn by jinak
    hlásil rozpracovaný týden. Konec týdne je neděle 23:59:59 UTC; týden je
    uzavřený, teprve když je as_of přísně po tom okamžiku.
    """
    if week:
        _start, end = market.week_bounds(week)
        if as_of_dt > end:
            return week
    key = market.previous_week_key(market.iso_week_key(as_of_dt))
    while True:
        _start, end = market.week_bounds(key)
        if as_of_dt > end:
            return key
        key = market.previous_week_key(key)


def _history(rec):
    rows = [h for h in (rec.get("price_history") or []) if h.get("at")]
    rows.sort(key=lambda h: h.get("at") or "")
    return rows


def _money(rec, entry):
    """Částka, kterou segment srovnává. None = do cen nepatří.

    Pronájem bez poplatků a cenový outlier by medián i slevu táhly špatným
    směrem; v počtu aktivních zůstávají.
    """
    if not entry or rec.get("deal_outlier"):
        return None
    if rec.get("transaction_type") == "pronajem":
        if rec.get("fees_missing"):
            return None
        total = entry.get("total_czk")
        value = total if total else entry.get("price_czk")
    else:
        value = entry.get("price_czk")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def asking_known_at(rec, when):
    """Cena jen když historie má bod v ten den nebo dřív.

    pool.price_at by jinak dosadil první pozdější cenu a inzerát, který
    minulý týden ještě neměl záznam, by vypadal jako beze změny.
    """
    target = poolmod.parse_ts(when)
    if target is None:
        return None
    chosen = None
    for entry in _history(rec):
        at = poolmod.parse_ts(entry.get("at"))
        if at is None:
            continue
        if at <= target:
            chosen = entry
        else:
            break
    return _money(rec, chosen)


def _first_price_during(rec, start, end):
    for entry in _history(rec):
        at = poolmod.parse_ts(entry.get("at"))
        if at and start <= at <= end:
            return _money(rec, entry)
    return None


def _median(values):
    stats = poolmod.quantiles(values)
    return stats["median"], stats["n"], ("too_small" if stats["too_small"] else None)


def _blank_metrics(week_key):
    return {
        "week": week_key,
        "n": 0,
        "active_count": 0,
        "same_listings_n": 0,
        "same_listings_avg_change_pct": None,
        "same_listings_avg_discount_pct": None,
        "price_cut_share_pct": None,
        "price_cut_n": 0,
        "price_cut_base_n": 0,
        "median_days_on_market": None,
        "days_on_market_n": 0,
        "median_kc_m2": None,
        "median_kc_m2_n": 0,
        "median_kc_m2_note": None,
        "_ids": set(),
    }


def _metrics_for_group(records, week_key, prev_records):
    start, end = market.week_bounds(week_key)
    _prev_start, prev_end = market.week_bounds(market.previous_week_key(week_key))
    out = _blank_metrics(week_key)
    out["n"] = len(records)
    out["active_count"] = len(records)
    out["_ids"] = {str(r.get("id")) for r in records}

    changes, discounts = [], []
    prev_by_id = {str(r.get("id")): r for r in prev_records}
    cut_n, cut_base = 0, 0
    per_sqm, days = [], []
    for rec in records:
        rid = str(rec.get("id"))
        now_px = asking_known_at(rec, end)
        if rid in prev_by_id:
            prev_px = asking_known_at(rec, prev_end)
            if now_px and prev_px:
                pct = (now_px - prev_px) / prev_px * 100
                changes.append(pct)
                discounts.append(max(0.0, -pct))
        if now_px:
            ref = asking_known_at(rec, prev_end)
            if ref is None:
                first_seen = poolmod.parse_ts(rec.get("first_seen"))
                if first_seen and first_seen > prev_end:
                    ref = _first_price_during(rec, start, end)
            if ref:
                cut_base += 1
                if now_px < ref:
                    cut_n += 1
        sqm_px = None if (rec.get("deal_outlier") or (
            rec.get("transaction_type") == "pronajem" and rec.get("fees_missing")
        )) else poolmod.per_sqm_at(rec, end)
        # per_sqm_at dosadí první pozdější cenu. Bereme jen cenu známou k konci týdne.
        if sqm_px and asking_known_at(rec, end):
            per_sqm.append(sqm_px)
        dom = poolmod.days_on_market(rec, end)
        if dom is not None:
            days.append(dom)

    out["same_listings_n"] = len(changes)
    out["same_listings_avg_change_pct"] = _r1(statistics.fmean(changes)) if changes else None
    out["same_listings_avg_discount_pct"] = _r1(statistics.fmean(discounts)) if discounts else None
    out["price_cut_n"] = cut_n
    out["price_cut_base_n"] = cut_base
    out["price_cut_share_pct"] = _r1(cut_n / cut_base * 100) if cut_base else None
    med_days, n_days, _note = _median(days)
    out["median_days_on_market"] = med_days
    out["days_on_market_n"] = n_days
    if end < COVERAGE_FROM:
        out["median_kc_m2"] = None
        out["median_kc_m2_n"] = len(per_sqm)
        out["median_kc_m2_note"] = "pred_koncem_srpna_2026"
    else:
        med_px, n_px, note = _median(per_sqm)
        out["median_kc_m2"] = med_px
        out["median_kc_m2_n"] = n_px
        out["median_kc_m2_note"] = note
    return out


def _earliest(records, key_fn):
    first = {}
    for rec in records:
        key = key_fn(rec)
        seen = poolmod.parse_ts(rec.get("first_seen"))
        if seen and (key not in first or seen < first[key]):
            first[key] = seen
    return first


def coverage_note(state, week_key, current, previous, source_first, area_first):
    """None = pokrytí je pro změnu počtu stabilní. Jinak kód důvodu."""
    _start, end = market.week_bounds(week_key)
    prev_key = market.previous_week_key(week_key)
    prev_start, prev_end = market.week_bounds(prev_key)
    if end < COVERAGE_FROM or prev_end < COVERAGE_FROM:
        return "pred_koncem_srpna_2026"
    if poolmod.config_changed_between(state or {}, prev_start, end):
        return "zmena_konfigurace_sberu"
    area_since = (state or {}).get("area_since") or {}
    areas = {poolmod.area_of(r) for r in list(current) + list(previous)}
    for area in areas:
        started = poolmod.parse_ts(area_since.get(area)) or area_first.get(area)
        if started and prev_start < started <= end:
            return "nova_oblast"
    sources = {(r.get("source") or "sreality") for r in list(current) + list(previous)}
    for src in sources:
        started = source_first.get(src)
        if started and prev_start < started <= end:
            return "novy_zdroj"
    return None


def _reason(code, text):
    return {"code": code, "text": text}


def _prev_points(series_by_week, week_key, n):
    out, cursor = [], week_key
    for _ in range(n):
        cursor = market.previous_week_key(cursor)
        point = series_by_week.get(cursor)
        if point is None:
            return None
        out.append(point)
    return out


def _flags(point, series_by_week, coverage):
    """Větší posun a důvody, proč se některé prahy nehodnotily.

    ``malo_dat`` nevrací žádný větší posun — ani kdyby procenta křičela.
    """
    if point["n"] < MIN_N:
        return [], [_reason("malo_dat", f"n={point['n']} < {MIN_N}, příznaky se nepočítají")]
    fired, skipped = [], []
    week = point["week"]

    def need(points, code, what):
        if not points:
            skipped.append(_reason(code, f"chybí historie pro {what}"))
            return False
        return True

    prev = _prev_points(series_by_week, week, 1)
    if need(prev, "median_ww", "týdenní medián"):
        earlier = prev[0]
        if (point["median_kc_m2"] is None or earlier["median_kc_m2"] is None
                or point["median_kc_m2_n"] < MIN_N or earlier["median_kc_m2_n"] < MIN_N):
            why = point["median_kc_m2_note"] or earlier["median_kc_m2_note"] or "malo_dat"
            skipped.append(_reason("median_ww", f"medián Kč/m² se nesrovnává ({why})"))
        else:
            move = _r1((point["median_kc_m2"] - earlier["median_kc_m2"]) / earlier["median_kc_m2"] * 100)
            if move is not None and abs(move) >= MEDIAN_WW_PCT:
                fired.append(_reason(
                    "median_ww",
                    f"medián Kč/m² se týden na týden posunul o {move} % (práh {MEDIAN_WW_PCT:g} %)",
                ))

    prev4 = _prev_points(series_by_week, week, DURATION_WEEKS)
    if need(prev4, "median_vs_4w", "průměr 4 týdnů"):
        usable = [p["median_kc_m2"] for p in prev4
                  if p["median_kc_m2"] and p["median_kc_m2_n"] >= MIN_N]
        if len(usable) < DURATION_WEEKS or point["median_kc_m2"] is None or point["median_kc_m2_n"] < MIN_N:
            skipped.append(_reason("median_vs_4w", "chybí 4 týdny s mediánem a n≥20"))
        else:
            avg = statistics.fmean(usable)
            move = _r1((point["median_kc_m2"] - avg) / avg * 100) if avg else None
            if move is not None and abs(move) >= MEDIAN_VS_4W_PCT:
                fired.append(_reason(
                    "median_vs_4w",
                    f"medián Kč/m² je o {move} % jinde než průměr 4 týdnů (práh {MEDIAN_VS_4W_PCT:g} %)",
                ))

    prev8 = _prev_points(series_by_week, week, DISCOUNT_SHARE_WEEKS)
    if need(prev8, "discount_share_vs_8w", "průměr podílu slev"):
        usable = [p["price_cut_share_pct"] for p in prev8
                  if p["price_cut_share_pct"] is not None and p["price_cut_base_n"] >= MIN_N]
        if (len(usable) < DISCOUNT_SHARE_WEEKS or point["price_cut_share_pct"] is None
                or point["price_cut_base_n"] < MIN_N):
            skipped.append(_reason("discount_share_vs_8w", "chybí 8 týdnů s podílem slev a n≥20"))
        else:
            delta = _r1(point["price_cut_share_pct"] - statistics.fmean(usable))
            if delta is not None and delta >= DISCOUNT_SHARE_PP:
                fired.append(_reason(
                    "discount_share_vs_8w",
                    f"podíl slev je o {delta} p. b. nad průměrem 8 týdnů (práh {DISCOUNT_SHARE_PP:g})",
                ))

    if point["same_listings_n"] < MIN_N or point["same_listings_avg_discount_pct"] is None:
        skipped.append(_reason("avg_discount_same", "průnik stejných inzerátů nemá n≥20"))
    elif point["same_listings_avg_discount_pct"] > AVG_DISCOUNT_PCT:
        fired.append(_reason(
            "avg_discount_same",
            f"průměrná sleva stejných inzerátů je {point['same_listings_avg_discount_pct']} % "
            f"(práh nad {AVG_DISCOUNT_PCT:g} %)",
        ))

    if need(prev4, "duration_vs_4w", "průměr doby na trhu"):
        usable = [p["median_days_on_market"] for p in prev4
                  if p["median_days_on_market"] is not None and p["days_on_market_n"] >= MIN_N]
        if (len(usable) < DURATION_WEEKS or point["median_days_on_market"] is None
                or point["days_on_market_n"] < MIN_N):
            skipped.append(_reason("duration_vs_4w", "chybí 4 týdny s mediánem doby a n≥20"))
        else:
            avg = statistics.fmean(usable)
            move = _r1((point["median_days_on_market"] - avg) / avg * 100) if avg else None
            if move is not None and move >= DURATION_VS_4W_PCT:
                fired.append(_reason(
                    "duration_vs_4w",
                    f"medián doby na trhu je o {move} % nad průměrem 4 týdnů (práh +{DURATION_VS_4W_PCT:g} %)",
                ))

    # Příznak je vypnutý schválně, i když by práh vyšel a pokrytí bylo stabilní.
    # Kontrola pokrytí skok o desítky procent nechytila (coverage_note prázdné).
    # Metrika active_count v bodu zůstává. Až se počet ověří proti snapshotu,
    # práh ACTIVE_COUNT_PCT se sem vrátí — do té doby se ticho nezapíše jako úspěch.
    skipped.append(_reason(
        "active_count",
        "příznak počtu aktivních je vypnutý, dokud se počet neověří proti snapshotu",
    ))
    return fired, skipped


def _triada(cur, prev):
    if not cur or not prev:
        return False
    if cur["n"] < MIN_N or prev["n"] < MIN_N:
        return False
    if (cur["price_cut_share_pct"] is None or prev["price_cut_share_pct"] is None
            or cur["price_cut_base_n"] < MIN_N or prev["price_cut_base_n"] < MIN_N):
        return False
    if not cur["price_cut_share_pct"] > prev["price_cut_share_pct"]:
        return False
    if (cur["median_days_on_market"] is None or prev["median_days_on_market"] is None
            or cur["days_on_market_n"] < MIN_N or prev["days_on_market_n"] < MIN_N):
        return False
    if not cur["median_days_on_market"] > prev["median_days_on_market"]:
        return False
    change = cur["same_listings_avg_change_pct"]
    if cur["same_listings_n"] < MIN_N or change is None or not change < 0:
        return False
    return True


def _crisis(segments_by_id, week_key):
    """Segmenty, které v tom týdnu tvoří dvojici uvnitř jedné osy a transakce."""
    groups = {}
    for seg in segments_by_id.values():
        if seg["axis"] == "reference":
            continue
        by_week = {p["week"]: p for p in seg["series"]}
        prev = market.previous_week_key(week_key)
        if _triada(by_week.get(week_key), by_week.get(prev)):
            groups.setdefault((seg["transaction"], seg["axis"]), []).append(seg["segment_id"])
    chosen = []
    for ids in groups.values():
        if len(ids) >= CRISIS_MIN_SEGMENTS:
            chosen.extend(ids)
    return sorted(set(chosen))


def _public_point(point):
    return {key: point[key] for key in SERIES_KEYS}


def build_summary(records, state=None, as_of=None, week=None):
    """Agregovaný souhrn uzavřeného týdne. Záznamy se do výsledku nekopírují."""
    state = state or {}
    as_of_dt = poolmod.parse_ts(as_of) or datetime.now(timezone.utc)
    week_key = closed_week(as_of_dt, week)
    now_year = as_of_dt.year
    records = poolmod.records_of(records)
    source_first = _earliest(records, lambda r: r.get("source") or "sreality")
    area_first = _earliest(records, poolmod.area_of)

    weeks = []
    cursor = week_key
    for _ in range(SERIES_WEEKS):
        weeks.append(cursor)
        cursor = market.previous_week_key(cursor)
    weeks.reverse()

    # Aktivní množina po týdnech, jednou. Segmenty se z ní jen třídí.
    # Hlášený týden se bere ze snapshotu (poslední sběr). Starší týdny z toho,
    # kdo byl vidět ještě v jejich neděli — jinak řada jen roste.
    live_at = _observation_instant(records, as_of_dt)
    active = {}
    for key in weeks:
        start, end = market.week_bounds(key)
        active[key] = [
            r for r in records
            if is_active(r, start, end, live_at, snapshot_week=(key == week_key))
        ]

    grouped = {row["segment_id"]: {key: [] for key in weeks} for row in catalog()}
    for key in weeks:
        for rec in active[key]:
            for sid in segment_ids_for(rec, now_year):
                grouped[sid][key].append(rec)

    raw_series = {}
    for row in catalog():
        sid = row["segment_id"]
        series = []
        for key in weeks:
            prev_key = market.previous_week_key(key)
            prev_recs = grouped[sid].get(prev_key, [])
            series.append(_metrics_for_group(grouped[sid][key], key, prev_recs))
        raw_series[sid] = series

    segments = []
    for row in catalog():
        series = raw_series[row["segment_id"]]
        by_week = {p["week"]: p for p in series}
        point = by_week[week_key]
        prev_recs = grouped[row["segment_id"]].get(market.previous_week_key(week_key), [])
        note = coverage_note(
            state, week_key, grouped[row["segment_id"]][week_key], prev_recs,
            source_first, area_first,
        )
        fired, skipped = _flags(point, by_week, note)
        public_series = [_public_point(p) for p in series]
        segments.append({
            **row,
            "n": point["n"],
            "malo_dat": point["n"] < MIN_N,
            "active_count": point["active_count"],
            "same_listings_n": point["same_listings_n"],
            "same_listings_avg_change_pct": point["same_listings_avg_change_pct"],
            "same_listings_avg_discount_pct": point["same_listings_avg_discount_pct"],
            "price_cut_share_pct": point["price_cut_share_pct"],
            "price_cut_n": point["price_cut_n"],
            "price_cut_base_n": point["price_cut_base_n"],
            "median_days_on_market": point["median_days_on_market"],
            "days_on_market_n": point["days_on_market_n"],
            "median_kc_m2": point["median_kc_m2"],
            "median_kc_m2_n": point["median_kc_m2_n"],
            "median_kc_m2_note": point["median_kc_m2_note"],
            "coverage_stable": note is None,
            "coverage_note": note,
            "vetsi_posun": bool(fired),
            "vetsi_posun_duvody": fired,
            "nevyhodnoceno": skipped,
            "triada": False,
            "series": public_series,
        })

    by_id = {s["segment_id"]: s for s in segments}
    prev_week = market.previous_week_key(week_key)
    for seg in segments:
        if seg["axis"] == "reference":
            continue
        by_week = {p["week"]: p for p in seg["series"]}
        seg["triada"] = _triada(by_week.get(week_key), by_week.get(prev_week))
    crisis_now = _crisis(by_id, week_key)
    crisis_prev = _crisis(by_id, prev_week)
    krizovy = bool(crisis_now) and bool(crisis_prev)

    start, end = market.week_bounds(week_key)
    return {
        "schema": SCHEMA,
        "generated_at": as_of_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "week": week_key,
        "week_start": start.strftime("%Y-%m-%d"),
        "week_end": end.strftime("%Y-%m-%d"),
        "as_of": as_of_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coverage_from": "2026-09-01",
        "min_n": MIN_N,
        "personal_data": False,
        "note": (
            "Jen agregované segmenty trhu. Žádné id inzerátu, žádná adresa, "
            "žádná osobní čísla. Alerty se odsud neposílají."
        ),
        "definitions": DEFINITIONS,
        "thresholds": THRESHOLDS,
        "krizovy_signal": krizovy,
        "krizovy_signal_tydny": [prev_week, week_key] if krizovy else [],
        "krizove_segmenty": {prev_week: crisis_prev, week_key: crisis_now} if krizovy else {},
        "vetsi_posun_n": sum(1 for s in segments if s["vetsi_posun"]),
        "segment_count": len(segments),
        "segments": segments,
    }


def assert_public(obj, path="$"):
    """Veřejný strom smí obsahovat jen agregaci. Osobní klíč nebo text spadne."""
    if isinstance(obj, dict):
        if path == "$":
            extra = set(obj) - TOP_KEYS
            missing = TOP_KEYS - set(obj)
            if extra or missing:
                raise ValueError(f"schéma souhrnu: navíc {sorted(extra)} chybí {sorted(missing)}")
        elif path.count(".") == 1 and path.startswith("$.segments"):
            pass
        for key, value in obj.items():
            if not isinstance(key, str):
                raise ValueError(f"{path}: klíč není text")
            low = key.lower()
            if low in FORBIDDEN_KEYS or any(s in low for s in ("mortgage", "anuita", "owncard", "hypoindex")):
                raise ValueError(f"{path}.{key}: zakázaný klíč")
            assert_public(value, f"{path}.{key}")
        return
    if isinstance(obj, list):
        for i, value in enumerate(obj):
            assert_public(value, f"{path}[{i}]")
        return
    if isinstance(obj, str):
        if len(obj) > 900:
            raise ValueError(f"{path}: text je delší než agregace")
        low = obj.lower()
        for token in FORBIDDEN_SUBSTRINGS:
            if token in low:
                raise ValueError(f"{path}: zakázaný text {token!r}")
        return
    if obj is None or isinstance(obj, (int, float, bool)):
        return
    raise ValueError(f"{path}: typ {type(obj).__name__} do veřejného JSON nepatří")


def _check_segment_shape(summary):
    for seg in summary["segments"]:
        extra = set(seg) - SEGMENT_KEYS
        missing = SEGMENT_KEYS - set(seg)
        if extra or missing:
            raise ValueError(f"{seg.get('segment_id')}: navíc {sorted(extra)} chybí {sorted(missing)}")
        for point in seg["series"]:
            if set(point) != SERIES_KEYS:
                raise ValueError(f"{seg['segment_id']}: série má {sorted(set(point) ^ SERIES_KEYS)}")
        for bucket in ("vetsi_posun_duvody", "nevyhodnoceno"):
            for reason in seg[bucket]:
                if set(reason) != REASON_KEYS:
                    raise ValueError(f"{seg['segment_id']}: důvod má {sorted(reason)}")
    if summary["personal_data"] is not False:
        raise ValueError("personal_data musí být false")


def publish(records, state=None, as_of=None, week=None, path=None):
    """Spočítá souhrn a zapíše ho, jen když projde kontrolou veřejného tvaru."""
    summary = build_summary(records, state=state, as_of=as_of, week=week)
    assert_public(summary)
    _check_segment_shape(summary)
    target = Path(path) if path else OUTPUT_PATH
    payload = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    low = payload.lower()
    for token in FORBIDDEN_SUBSTRINGS:
        if token in low:
            raise RuntimeError(f"veřejný JSON obsahuje zakázaný text {token!r}")
    target.write_text(payload, encoding="utf-8")
    return summary


def load_public(path=None):
    target = Path(path) if path else OUTPUT_PATH
    if not target.exists():
        return None
    data = json.loads(target.read_text(encoding="utf-8"))
    assert_public(data)
    _check_segment_shape(data)
    return data


def _fmt_pct(value, signed=False):
    if value is None:
        return "—"
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.1f}".replace(".", ",") + " %"


def _fmt_int(value):
    if value is None:
        return "—"
    return f"{int(round(value)):,}".replace(",", " ")


def card_html(summary):
    """Karta dashboardu. Čísla jsou z agregace, texty jsou naše a stejně se escapují."""
    if not summary:
        return ""
    rows = []
    current_group = None
    for seg in summary.get("segments") or []:
        group = (seg.get("transaction"), seg.get("axis"))
        if group != current_group:
            current_group = group
            axis = {"typ_domu": "Typ domu", "velikost": "Velikost",
                    "lokalita": "Lokalita", "reference": "Referenční pásmo"}.get(
                seg.get("axis"), seg.get("axis") or "")
            tx = {"prodej": "Prodej", "pronajem": "Pronájem"}.get(seg.get("transaction"), "")
            rows.append(
                f'<tr><td colspan="8"><b>{html.escape(tx)} · {html.escape(axis)}</b></td></tr>'
            )
        if seg.get("malo_dat"):
            flag = '<span class="badge approx">málo dat</span>'
        elif seg.get("vetsi_posun"):
            texts = "; ".join(r.get("text") or "" for r in seg.get("vetsi_posun_duvody") or [])
            flag = f'<span class="badge bad">větší posun</span> <span class="hint">{html.escape(texts)}</span>'
        else:
            flag = "—"
        kc_note = seg.get("median_kc_m2_note")
        if seg.get("median_kc_m2") is None and kc_note == "pred_koncem_srpna_2026":
            kc = "— (před zářím 2026)"
        elif seg.get("median_kc_m2") is None:
            kc = f"— (n={_fmt_int(seg.get('median_kc_m2_n'))})"
        else:
            kc = f"{_fmt_int(seg.get('median_kc_m2'))} <span class=\"hint\">n={_fmt_int(seg.get('median_kc_m2_n'))}</span>"
        change = _fmt_pct(seg.get("same_listings_avg_change_pct"), signed=True)
        same_n = seg.get("same_listings_n") or 0
        rows.append(
            "<tr>"
            f"<td>{html.escape(seg.get('label') or '')}</td>"
            f"<td>{_fmt_int(seg.get('active_count'))}</td>"
            f"<td>{change} <span class=\"hint\">n={same_n}</span></td>"
            f"<td>{_fmt_pct(seg.get('same_listings_avg_discount_pct'))}</td>"
            f"<td>{_fmt_pct(seg.get('price_cut_share_pct'))}</td>"
            f"<td>{_fmt_int(seg.get('median_days_on_market'))}</td>"
            f"<td>{kc}</td>"
            f"<td>{flag}</td>"
            "</tr>"
        )
    if summary.get("krizovy_signal"):
        banner = (
            '<p class="hint" style="color:#f88;"><b>Krizový signál.</b> '
            "Aspoň dva segmenty jedné osy mají dva týdny po sobě zároveň rostoucí "
            "podíl slev, delší dobu na trhu a klesající cenu stejných inzerátů.</p>"
        )
    else:
        banner = '<p class="hint">Krizový signál tento týden není.</p>'
    week = html.escape(str(summary.get("week") or ""))
    start = html.escape(str(summary.get("week_start") or ""))
    end = html.escape(str(summary.get("week_end") or ""))
    body = "\n".join(rows)
    return f"""<div class="card" id="segmentCard">
  <h2 style="margin-top:0;font-size:1rem;">📉 Týdenní segmenty — {week}</h2>
  <p class="hint">Uzavřený týden {start} – {end}. Jen součty po segmentech, žádné jednotlivé inzeráty.
     Pod {MIN_N} aktivními je „málo dat“ a příznak se nespouští. Medián Kč/m² před 1. 9. 2026 se neuvádí.</p>
  {banner}
  <div class="est-scroll">
  <table class="est-table">
    <thead><tr>
      <th>Segment</th><th>Aktivní</th><th>Stejné inzeráty</th><th>Prům. sleva</th>
      <th>Podíl slev</th><th>Dny na trhu</th><th>Kč/m²</th><th>Příznak</th>
    </tr></thead>
    <tbody>
    {body}
    </tbody>
  </table>
  </div>
  <p class="hint">Větší posun (stačí jedno, jen při n≥{MIN_N}): medián Kč/m² ±{MEDIAN_WW_PCT:g} % t/t nebo
     ±{MEDIAN_VS_4W_PCT:g} % proti průměru 4 týdnů; podíl slev o {DISCOUNT_SHARE_PP:g} p. b. nad průměrem
     8 týdnů; průměrná sleva stejných inzerátů nad {AVG_DISCOUNT_PCT:g} %; medián doby na trhu
     +{DURATION_VS_4W_PCT:g} % proti 4 týdnům. Počet aktivních se v tabulce ukazuje,
     jeho příznak je vypnutý, dokud počet nesedí se snapshotem.
     Strojově: <code>weekly_segments.json</code>.</p>
</div>"""


def dashboard_card(path=None):
    """Karta pro render dashboardu. Chybějící nebo závadný soubor kartu vynechá."""
    try:
        summary = load_public(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"::warning::týdenní segmenty se na dashboard nedostaly: {exc}", file=sys.stderr)
        return ""
    return card_html(summary)


def stamp_live_snapshot(records, snapshot):
    """Srovná příznaky se snapshotem, ze kterého se počítá sale_count.

    Pool se nezapisuje. Volající má předat kopie: funkce mění předané
    slovníky. `apply_overrides` na poolu umí smazat exclude_from_stats,
    který ve snapshotu nechal nesoulad typu obchodu, a starší shard nemá
    area_mismatch. Bez téhle kopie by počet o pár inzerátů minul
    sale_count. Inzerát, který ve snapshotu je, není zombie.
    """
    by_id = {}
    for comp in snapshot.get("comparables") or []:
        cid = comp.get("id")
        if cid is not None:
            by_id[str(cid)] = comp
    for rec in records:
        comp = by_id.get(str(rec.get("id")))
        if comp is None:
            continue
        rec["exclude_from_stats"] = bool(comp.get("exclude_from_stats"))
        rec["price_czk_per_sqm"] = comp.get("price_czk_per_sqm")
        if comp.get("area"):
            rec["area"] = comp["area"]
        if comp.get("area_mismatch"):
            rec["area_mismatch"] = True
        else:
            rec.pop("area_mismatch", None)
        rec.pop("gone_stale", None)
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description="Týdenní segmenty trhu (jen agregace, bez alertů).")
    parser.add_argument("--as-of", help="ISO čas, vůči kterému se bere uzavřený týden")
    parser.add_argument("--week", help="ISO týden, např. 2026-W41 (jinak poslední uzavřený)")
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument(
        "--snapshot",
        default=str(ROOT / "latest_snapshot.json"),
        help="Snapshot, ze kterého se doplní area_mismatch (soubor se nemění)",
    )
    args = parser.parse_args(argv)
    records = poolmod.records_of(poolmod.load_pool())
    snap_path = Path(args.snapshot)
    if snap_path.exists():
        stamp_live_snapshot(records, json.loads(snap_path.read_text(encoding="utf-8")))
    summary = publish(
        records, poolmod.load_state(),
        as_of=args.as_of, week=args.week, path=args.output,
    )
    print(
        f"{summary['week']}: {summary['segment_count']} segmentů, "
        f"větší posun {summary['vetsi_posun_n']}, "
        f"krizový signál {'ano' if summary['krizovy_signal'] else 'ne'} → {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
