#!/usr/bin/env python3
"""Týdenní segmenty: řezy, prahy, málo dat, krize a zákaz osobních dat.

Run: python3 test_segments.py
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import market
import segments

FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}\n        got  {got!r}\n        want {want!r}")
        FAILURES.append(label)


def codes(seg):
    return [r["code"] for r in seg["vetsi_posun_duvody"]]


def skipped(seg):
    return [r["code"] for r in seg["nevyhodnoceno"]]


def one(summary, segment_id):
    return next(s for s in summary["segments"] if s["segment_id"] == segment_id)


# --- velikost --------------------------------------------------------------- #
check("2+kk 55 m² je malý", segments.size_band({"disposition": "2+kk", "floor_area_sqm": 55}), "maly")
check("2+kk těsně nad 55 je střední", segments.size_band({"disposition": "2+kk", "floor_area_sqm": 55.01}), "stredni")
check("2+kk přesně 75 ještě není velké jen plochou",
      segments.size_band({"disposition": "2+kk", "floor_area_sqm": 75}), "stredni")
check("2+kk nad 75 je velké", segments.size_band({"disposition": "2+kk", "floor_area_sqm": 75.01}), "velky")
check("2+kk bez plochy je malý", segments.size_band({"disposition": "2+kk"}), "maly")
check("3+kk 40 m² zůstává střední", segments.size_band({"disposition": "3+kk", "floor_area_sqm": 40}), "stredni")
check("3+kk nad 75 je velké", segments.size_band({"disposition": "3+kk", "floor_area_sqm": 80}), "velky")
check("3+1 40 m² je velké (3+1 a víc)", segments.size_band({"disposition": "3+1", "floor_area_sqm": 40}), "velky")
check("4+kk je velké", segments.size_band({"disposition": "4+kk", "floor_area_sqm": 70}), "velky")
check("1+1 je malý", segments.size_band({"disposition": "1+1", "floor_area_sqm": 40}), "maly")
check("garsonka se bere jako malá dispozice", segments.size_band({"disposition": "Garsonka", "floor_area_sqm": 28}), "maly")
check("bez dispozice, 40 m² = malý", segments.size_band({"floor_area_sqm": 40}), "maly")
check("bez dispozice, 60 m² = střední", segments.size_band({"floor_area_sqm": 60}), "stredni")
check("bez dispozice, 90 m² = velký", segments.size_band({"floor_area_sqm": 90}), "velky")
check("bez dispozice i plochy = neurčeno", segments.size_band({}), "neurceno")

# --- typ domu a lokalita ---------------------------------------------------- #
Y = 2026
check("uložená třída má přednost",
      segments.house_segment({"house_class": "panel", "building_condition_name": "Novostavba"}, Y), "panel")
check("štítek Novostavba",
      segments.house_segment({"building_condition_name": "Novostavba"}, Y), "novostavba")
check("panel", segments.house_segment({"building_type_name": "Panelová"}, Y), "panel")
check("cihla = starší cihla", segments.house_segment({"building_type_name": "Cihlová"}, Y), "starsi_cihla")
check("skelet patří do starší cihly (třída starsi)",
      segments.house_segment({"building_type_name": "Skeletová"}, Y), "starsi_cihla")
check("bez typu = neurčeno", segments.house_segment({}, Y), "neurceno")
check("is_new_building doplní novostavbu, jen když třída chybí",
      segments.house_segment({"is_new_building": True}, Y), "novostavba")
check("vysocany = klíč praha9", segments.location_band({"area": "vysocany"}), "praha9")
check("chybějící area = klíč praha9", segments.location_band({}), "praha9")
check("jinonice = klíč zbytek", segments.location_band({"area": "jinonice"}), "zbytek")
_loc = {row["key"]: row["label"] for row in segments.catalog()
        if row["transaction"] == "prodej" and row["axis"] == "lokalita"}
check("štítek oblasti říká Vysočany, Libeň, Karlín a Žižkov",
      "Karlín" in _loc["praha9"] and "Žižkov" in _loc["praha9"] and "Vysočany" in _loc["praha9"], True)
check("štítek oblasti netvrdí, že je to Praha 9", "Praha 9" in _loc["praha9"], False)
check("druhý štítek říká Jinonice a že to není celá Praha",
      "Jinonice" in _loc["zbytek"] and "ne celá Praha" in _loc["zbytek"], True)
check("druhý štítek netvrdí zbytek Prahy", "Zbytek Prahy" in _loc["zbytek"], False)


def ref(**kw):
    base = {"disposition": "1+kk", "floor_area_sqm": 30, "house_class": "novostavba", "area": "vysocany"}
    base.update(kw)
    return base


check("reference 25 m²", segments.in_reference(ref(floor_area_sqm=25), Y), True)
check("reference 35 m²", segments.in_reference(ref(floor_area_sqm=35), Y), True)
check("reference 24,9 m² ne", segments.in_reference(ref(floor_area_sqm=24.9), Y), False)
check("reference 35,1 m² ne", segments.in_reference(ref(floor_area_sqm=35.1), Y), False)
check("reference jiná dispozice ne", segments.in_reference(ref(disposition="2+kk"), Y), False)
check("reference panel ne", segments.in_reference(ref(house_class="panel"), Y), False)
check("reference Jinonice ne", segments.in_reference(ref(area="jinonice"), Y), False)

# --- metriky ---------------------------------------------------------------- #
AS_OF = "2026-10-14T12:00:00Z"
WEEK = "2026-W41"  # 5.–11. 10. 2026, uzavřený týden vůči AS_OF


def listing(i, price=5_000_000, history=None, **kw):
    rec = {
        "id": f"lst-{i}",
        "transaction_type": "prodej",
        "disposition": "2+kk",
        "floor_area_sqm": 50,
        "house_class": "panel",
        "area": "vysocany",
        "source": "sreality",
        "first_seen": "2026-06-01T00:00:00Z",
        "last_seen": AS_OF,
        "since": "2026-01-15",
        "price_czk": price,
        "price_history": history or [{"at": "2026-06-01", "price_czk": price}],
        "gone_at": None,
    }
    rec.update(kw)
    if "price_czk_per_sqm" not in kw and rec.get("price_czk") and rec.get("floor_area_sqm"):
        rec["price_czk_per_sqm"] = round(rec["price_czk"] / rec["floor_area_sqm"])
    return rec


def summary_of(records, state=None, week=WEEK, as_of=AS_OF):
    return segments.build_summary(records, state=state, as_of=as_of, week=week)


panel = [listing(i) for i in range(25)]
# +3,2 % až v hlášeném týdnu. Medián se pohne o 3,2 (práh t/t), ne o 5 proti 4 týdnům.
for rec in panel:
    rec["price_czk"] = 5_160_000
    rec["price_history"] = [
        {"at": "2026-06-01", "price_czk": 5_000_000},
        {"at": "2026-10-05", "price_czk": 5_160_000},
    ]
ww = one(summary_of(panel), "prodej:typ:panel")
check("t/t medián ≥3 % je větší posun", "median_ww" in codes(ww), True)
check("3,2 % není 5 % proti 4 týdnům", "median_vs_4w" in codes(ww), False)
check("segment má 25 aktivních", ww["active_count"], 25)
check("průnik stejných inzerátů je 25", ww["same_listings_n"], 25)
check("průměrná změna stejných inzerátů je +3,2 %", ww["same_listings_avg_change_pct"], 3.2)
check("beze slevy je průměrná sleva 0", ww["same_listings_avg_discount_pct"], 0.0)

big = []
for i in range(25):
    big.append(listing(f"b{i}", price=5_300_000, history=[
        {"at": "2026-06-01", "price_czk": 5_000_000},
        {"at": "2026-10-05", "price_czk": 5_300_000},
    ]))
both = one(summary_of(big), "prodej:typ:panel")
check("+6 % je i proti průměru 4 týdnů", "median_vs_4w" in codes(both), True)

# Sleva na části vzorku: medián stojí, průměrná sleva přesáhne 3 %.
# Jen dva týdny v nabídce, ať osmitýdenní podíl slev nemá z čeho počítat.
mixed = []
for i in range(25):
    price = 4_000_000 if i < 8 else 5_000_000
    history = [{"at": "2026-09-28", "price_czk": 5_000_000}]
    if i < 8:
        history.append({"at": "2026-10-05", "price_czk": 4_000_000})
    mixed.append(listing(
        f"m{i}", price=price, history=history,
        first_seen="2026-09-28T00:00:00Z",
    ))
disc = one(summary_of(mixed), "prodej:typ:panel")
check("průměrná sleva stejných inzerátů > 3 %", "avg_discount_same" in codes(disc), True)
check("medián se přitom nehýbe (většina beze změny)", "median_ww" in codes(disc), False)
check("osmitýdenní podíl se bez historie nehlásí", "discount_share_vs_8w" in skipped(disc), True)

# Podíl slev +10 p. b. proti osmi týdnům nul.
share_rows = []
for i in range(25):
    history = [{"at": "2026-06-01", "price_czk": 5_000_000}]
    price = 5_000_000
    if i < 5:
        history.append({"at": "2026-10-05", "price_czk": 4_500_000})
        price = 4_500_000
    share_rows.append(listing(f"s{i}", price=price, history=history))
share = one(summary_of(share_rows), "prodej:typ:panel")
check("podíl slev je 20 %", share["price_cut_share_pct"], 20.0)
check("20 p. b. nad osmi týdny nul je větší posun", "discount_share_vs_8w" in codes(share), True)

few = [listing(f"f{i}", price=1_000_000, history=[
    {"at": "2026-06-01", "price_czk": 2_000_000},
    {"at": "2026-10-05", "price_czk": 1_000_000},
]) for i in range(19)]
small = one(summary_of(few), "prodej:typ:panel")
check("19 inzerátů je málo dat", small["malo_dat"], True)
check("málo dat nespustí větší posun", small["vetsi_posun"], False)
check("důvod je zapsaný, ne tichý", "malo_dat" in skipped(small), True)

# Počet aktivních +20 %, pokrytí stabilní (oblast i zdroj jsou v datech od června).
grown = [listing(f"g{i}") for i in range(25)]
for i in range(5):
    grown.append(listing(
        f"n{i}", first_seen="2026-10-05T00:00:00Z",
        price_history=[{"at": "2026-10-05", "price_czk": 5_000_000}],
    ))
grown_seg = one(summary_of(grown), "prodej:typ:panel")
check("skok počtu příznak nespustí", "active_count" in codes(grown_seg), False)
check("vypnutí příznaku je zapsané", "active_count" in skipped(grown_seg), True)
check("důvod říká, že je příznak vypnutý",
      "vypnutý" in next(r["text"] for r in grown_seg["nevyhodnoceno"] if r["code"] == "active_count"), True)
check("metrika počtu zůstává", grown_seg["active_count"], 30)
check("pokrytí je stabilní", grown_seg["coverage_stable"], True)

state_change = {"config_changes": [
    {"at": "2026-01-01T00:00:00Z", "fingerprint": "zaklad"},
    {"at": "2026-10-06T00:00:00Z", "fingerprint": "jina"},
]}
changed = one(summary_of(grown, state=state_change), "prodej:typ:panel")
check("změna konfigurace počet aktivních nehlásí", "active_count" in codes(changed), False)
check("důvod je zapsaný", "active_count" in skipped(changed), True)
check("poznámka pokrytí", changed["coverage_note"], "zmena_konfigurace_sberu")

# Inzerát viděný uprostřed týdne, ale už ne v posledním sběru, v hlášeném
# týdnu není. V předchozím ještě ano — řada proto nemusí jen růst.
ghost_rows = [listing(f"live{i}") for i in range(25)]
ghost_rows.append(listing("ghost", last_seen="2026-10-07T08:00:00Z"))
ghost = one(summary_of(ghost_rows), "prodej:typ:panel")
ghost_by_week = {p["week"]: p["n"] for p in ghost["series"]}
check("bez gone_at, ale mimo snapshot, se nepočítá", ghost["active_count"], 25)
check("v předchozím týdnu ještě v nabídce byl", ghost_by_week["2026-W40"], 26)

dropped = ghost_rows + [
    listing("mm", area_mismatch=True),
    listing("noprice", price_czk_per_sqm=None),
    listing("excl", exclude_from_stats=True),
]
check("rozpor plochy, chybějící Kč/m² a vyřazení se nepočítají",
      one(summary_of(dropped), "prodej:typ:panel")["active_count"], 25)

# Pool po apply_overrides umí mít exclude False, snapshot pořád True.
stale = [listing("keep")]
stale.append(listing("cleared", exclude_from_stats=False))
segments.stamp_live_snapshot(stale, {"comparables": [
    {"id": "lst-keep", "price_czk_per_sqm": stale[0]["price_czk_per_sqm"], "area": "vysocany"},
    {"id": "lst-cleared", "exclude_from_stats": True, "price_czk_per_sqm": 1, "area": "vysocany"},
]})
check("razítko snapshotu vrátí vyřazení, které pool smazal",
      one(summary_of(stale), "prodej:typ:panel")["active_count"], 1)

# Uzavřený týden. 14. 10. 2026 je středa W42, W41 skončil 11. 10.
check("středa 14. 10. hlásí W41",
      segments.build_summary([listing(1)], as_of="2026-10-14T12:00:00Z")["week"], "2026-W41")
check("středa 7. 10. (W41 ještě běží) hlásí W40",
      segments.build_summary([listing(1)], as_of="2026-10-07T12:00:00Z")["week"], "2026-W40")
check("neděle 11. 10. v poledne ještě není W41",
      segments.build_summary([listing(1)], as_of="2026-10-11T12:00:00Z")["week"], "2026-W40")
check("pondělí 12. 10. 00:00 už má W41 uzavřený",
      segments.build_summary([listing(1)], as_of="2026-10-12T00:00:00Z")["week"], "2026-W41")
check("vyžádaný rozpracovaný týden se nebere",
      segments.build_summary([listing(1)], as_of="2026-10-14T12:00:00Z", week="2026-W42")["week"],
      "2026-W41")

# Před koncem srpna se medián Kč/m² neuvádí a počet se nesrovnává.
august = [listing(f"a{i}") for i in range(25)]
aug = one(summary_of(august, week="2026-W35"), "prodej:typ:panel")
check("před zářím 2026 není medián Kč/m²", aug["median_kc_m2"], None)
check("důvod je změna pokrytí", aug["median_kc_m2_note"], "pred_koncem_srpna_2026")
check("n mediánu se stejně řekne", aug["median_kc_m2_n"] >= 20, True)
check("počet aktivních se proti srpnu nehlásí", "active_count" in codes(aug), False)
check("srpnové pokrytí není stabilní", aug["coverage_note"], "pred_koncem_srpna_2026")

# Doba na trhu: čtyři týdny s mediánem 10 dní, pak 40.
def cohort(prefix, week_key, dom_days, n=25, gone_at=None, last_seen=None):
    start, end = market.week_bounds(week_key)
    # since je půlnoc; (konec týdne − since).days má vyjít dom_days.
    since = (end.date() - timedelta(days=dom_days)).isoformat()
    rows = []
    for i in range(n):
        rows.append(listing(
            f"{prefix}{i}",
            first_seen=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            last_seen=(last_seen or end).strftime("%Y-%m-%dT%H:%M:%SZ"),
            gone_at=None if gone_at is None else gone_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            since=since,
            price_history=[{"at": start.strftime("%Y-%m-%d"), "price_czk": 5_000_000}],
        ))
    return rows


dur_rows = []
for back, key in enumerate(("2026-W37", "2026-W38", "2026-W39", "2026-W40")):
    # Skončí na začátku následujícího týdne, ať v dalším týdnu už není aktivní.
    nxt_start, _ = market.week_bounds({
        "2026-W37": "2026-W38", "2026-W38": "2026-W39",
        "2026-W39": "2026-W40", "2026-W40": "2026-W41",
    }[key])
    dur_rows += cohort(f"d{back}-", key, 10, gone_at=nxt_start)
dur_rows += cohort(
    "now-", "2026-W41", 40,
    last_seen=datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc),
)
dur = one(summary_of(dur_rows), "prodej:typ:panel")
check("medián doby vyskočil na 40", dur["median_days_on_market"], 40)
check("doba +20 % proti 4 týdnům", "duration_vs_4w" in codes(dur), True)

# Pronájem bez poplatků je v počtu, ne v ceně. Prodej do pronájmu nepatří.
rents = []
for i in range(25):
    total = 30_000
    rents.append(listing(
        f"r{i}", transaction_type="pronajem", price_czk=total, floor_area_sqm=30,
        price_history=[{"at": "2026-06-01", "price_czk": total, "total_czk": total}],
        fees_missing=i < 5,
    ))
rents.append(listing("sale-only"))
rent_seg = one(summary_of(rents), "pronajem:typ:panel")
sale_seg = one(summary_of(rents), "prodej:typ:panel")
check("pronájem počítá i inzeráty bez poplatků", rent_seg["active_count"], 25)
check("do ceny pronájmu jdou jen ty s poplatky", rent_seg["median_kc_m2_n"], 20)
check("prodej se s pronájmem nemíchá", sale_seg["active_count"], 1)

# --- krize ------------------------------------------------------------------ #
def shock(prefix, house, cut_from):
    """25 inzerátů. V týdnech od cut_from jich přibývá se slevou 5 %."""
    # cut_from: {week_start_date: how many listings drop that week}
    rows = []
    for i in range(25):
        price = 1_000_000
        history = [{"at": "2026-06-01", "price_czk": price}]
        for day, n_cut in cut_from:
            if i < n_cut:
                price = int(price * 0.95)
                history.append({"at": day, "price_czk": price})
        rows.append(listing(
            f"{prefix}{i}", house_class=house, price_czk=price, price_history=history,
        ))
    return rows


cuts = (("2026-09-21", 4), ("2026-09-28", 8), ("2026-10-05", 16))
crisis_rows = shock("p", "panel", cuts) + shock("c", "starsi", cuts)
crisis = summary_of(crisis_rows)
check("dva typy dva týdny = krizový signál", crisis["krizovy_signal"], True)
check("v signálu jsou oba týdny", crisis["krizovy_signal_tydny"], ["2026-W40", "2026-W41"])
check("panel je v triádě", one(crisis, "prodej:typ:panel")["triada"], True)
check("starší cihla je v triádě", one(crisis, "prodej:typ:starsi_cihla")["triada"], True)

lone = summary_of(shock("p", "panel", cuts) + shock("c", "starsi", ()))
check("jeden typ nestačí, i když ho vidí víc os", lone["krizovy_signal"], False)

one_week = summary_of(shock("p", "panel", (("2026-10-05", 16),)) + shock("c", "starsi", (("2026-10-05", 16),)))
check("dva segmenty v jen jednom týdnu nejsou krize", one_week["krizovy_signal"], False)

thin = summary_of(shock("p", "panel", cuts) + shock("c", "starsi", cuts)[:10])
check("druhý segment pod 20 se do krize nepočítá", thin["krizovy_signal"], False)
check("ten druhý je málo dat", one(thin, "prodej:typ:starsi_cihla")["malo_dat"], True)

# --- veřejný výstup --------------------------------------------------------- #
secret = listing(
    "x",
    id="secret-listing-xyz",
    url="https://secret.example/listing",
    street="Tajna ulice",
    title="tajny titulek",
    description="tajny popis bytu",
)
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "weekly_segments.json"
    published = segments.publish([secret] + panel[:24], as_of=AS_OF, week=WEEK, path=path)
    text = path.read_text(encoding="utf-8")
    loaded = json.loads(text)
    check("soubor je to, co publish vrátil", loaded["week"], published["week"])
    for token in ("secret-listing-xyz", "secret.example", "Tajna ulice", "tajny titulek",
                  "tajny popis", "ownCard", "709/R", "hypoindex", "MORTGAGE"):
        check(f"veřejný JSON neobsahuje {token}", token in text, False)
    check("personal_data je false", loaded["personal_data"], False)
    check("schéma", loaded["schema"], segments.SCHEMA)

try:
    segments.assert_public({"note": "sazba hypoindex"}, path="$.note")
    check("assert_public pustí zakázaný text", False, True)
except ValueError:
    check("assert_public zastaví zakázaný text", True, True)

html = segments.card_html({
    "week": "2026-W41",
    "week_start": "2026-10-05",
    "week_end": "2026-10-11",
    "krizovy_signal": False,
    "segments": [{
        "transaction": "prodej",
        "axis": "typ_domu",
        "label": "<script>alert(1)</script>",
        "active_count": 3,
        "same_listings_n": 0,
        "same_listings_avg_change_pct": None,
        "same_listings_avg_discount_pct": None,
        "price_cut_share_pct": None,
        "median_days_on_market": None,
        "median_kc_m2": None,
        "median_kc_m2_n": 0,
        "median_kc_m2_note": None,
        "malo_dat": True,
        "vetsi_posun": False,
        "vetsi_posun_duvody": [],
    }],
})
check("karta má své id", 'id="segmentCard"' in html, True)
check("popisek se escapuje", "<script>" in html, False)
check("escapovaný popisek v kartě je", "&lt;script&gt;" in html, True)
check("chybějící soubor = prázdná karta", segments.dashboard_card("/tmp/segments-chybi-22e4.json"), "")

workflow = Path(".github/workflows/scrape.yml").read_text(encoding="utf-8")
check("workflow commituje weekly_segments.json", "weekly_segments.json" in workflow, True)
check("testy ho spouštějí", "python test_segments.py" in workflow, True)
check("stráž hlídá segmentový JSON", "personal data leaked into weekly_segments.json" in workflow, True)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED:")
    for item in FAILURES:
        print(f"  {item}")
    sys.exit(1)
print("všechny kontroly prošly")
