#!/usr/bin/env python3
"""Novostavby 4+kk / 5+kk u U Kříže: parsování, nadmnožina, zmizení jen po
404, baseline jako dolní mez, dny na trhu, tichá změna konfigurace, alert.

Bez vnější sítě: HTTP je podvržené nebo běží na lokálním testovacím serveru.

Run: python3 test_novostavby.py
"""
import re
import sys
import tempfile
import time
from pathlib import Path

import notify
import novostavby as nov
import scrape

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:52} {got!r}")


def result(id_, code=8, name="Prodej bytu 4+kk 100 m²", price=20_000_000, lat=50.0550, lon=14.3680,
           street="Na Hutmance", sub_name="4+kk"):
    return {"id": id_, "name": name, "priceCzk": price, "categorySubCb": {"name": sub_name, "value": code},
            "locality": {"street": street, "cityPart": "Jinonice", "city": "Praha",
                         "latitude": lat, "longitude": lon}, "images": []}


# --- parsování -------------------------------------------------------------- #
r = scrape.parse_novostavba(result(1, sub_name="4+kt"), "prodej")   # anglická odpověď
check("dispozice z kódu, ne z anglického názvu", r["disposition"], "4+kk")
check("5+kk = kód 10", scrape.parse_novostavba(result(2, code=10), "pronajem")["disposition"], "5+kk")
check("plocha z titulku", r["floor_area_sqm"], 100.0)
check("Kč/m² dopočtené", r["price_czk_per_sqm"], 200_000)
check("km od U Kříže", r["km"], nov.km_from_center(50.0550, 14.3680))
check("odkaz na detail bytu", r["url"], "https://www.sreality.cz/detail/prodej/byt/4%2Bkk/x/1")
r0 = scrape.parse_novostavba(result(3, price=0), "prodej")
check("cena 0 = na dotaz (None), žádné Kč/m²", (r0["price_czk"], r0["price_czk_per_sqm"]), (None, None))
check("kódy dispozic", nov.DISPOSITIONS, {8: "4+kk", 10: "5+kk"})


# --- hledání: filtr musí platit, prázdno je 404 s tělem -------------------- #
def fake_page(results, key_extra=None, total=None):
    key = {"buildingCondition": [6], "categorySubCb": [8, 10], "localityEntityType": "ward"}
    key.update(key_extra or {})
    data = {"pagination": {"total": len(results) if total is None else total, "limit": 22, "offset": 0},
            "results": results}
    return {"props": {"pageProps": {"dehydratedState": {"queries": [
        {"queryKey": ["estatesSearch", key], "state": {"status": "success", "data": data}}]}}}}


orig_fetch = scrape.fetch_next_data
orig_sleep = time.sleep
time.sleep = lambda s: None
try:
    scrape.fetch_next_data = lambda url, params=None, parse_on_404=False: (
        fake_page([result(10), result(11, code=9, name="Prodej bytu 4+1 90 m²")]), 200)
    got = scrape.search_ward_novostavby("Jinonice", "prodej")
    check("4+1 z výsledku vypadne", [g["id"] for g in got], [10])

    scrape.fetch_next_data = lambda url, params=None, parse_on_404=False: (fake_page([], total=0), 404)
    check("prázdná čtvrť (404 s tělem) = []", scrape.search_ward_novostavby("Radlice", "pronajem"), [])

    sent_params = []

    def no_stav(url, params=None, parse_on_404=False):
        sent_params.append(dict(params or {}))
        return fake_page([result(10)], {"buildingCondition": None}), 200
    scrape.fetch_next_data = no_stav
    check("bez filtru stavu: výsledek projde", [g["id"] for g in scrape.search_ward_novostavby("Jinonice", "prodej")], [10])
    check("filtr stavu se na server neposílá", "stav" in sent_params[0], False)

    scrape.fetch_next_data = lambda url, params=None, parse_on_404=False: (
        fake_page([result(10)], {"categorySubCb": None}), 200)
    try:
        scrape.search_ward_novostavby("Jinonice", "prodej")
        check("ignorovaný filtr dispozice = chyba", "no error", "TransientFetchError")
    except scrape.TransientFetchError:
        check("ignorovaný filtr dispozice = chyba", "TransientFetchError", "TransientFetchError")
finally:
    scrape.fetch_next_data = orig_fetch


# --- nadmnožina ------------------------------------------------------------- #
check("1,9 km je v nadmnožině", nov.in_superset({"km": 1.9}), True)
check("2,5 km není", nov.in_superset({"km": 2.5}), False)
check("bez GPS není", nov.in_superset({"km": None}), False)
check("výchozí kruh pokrývá Novou Waltrovku (1,09 km)",
      nov.haversine_km(50.05720, 14.38183, *nov.CENTER) <= nov.DEFAULT_RADIUS_KM, True)
check("všechna Radimova místa uvnitř výchozího kruhu",
      all(nov.haversine_km(p["lat"], p["lon"], *nov.CENTER) <= nov.DEFAULT_RADIUS_KM for p in nov.LANDMARKS), True)
check("ulice bez diakritiky/velikosti", nov.named_place({"street": "U kříže"}), "U Kříže")
check("u parku Waltrovka", nov.named_place({"street": "Kačírkova", "lat": 50.0567, "lon": 14.3775}), "Waltrovka")
check("Na Hutmance nic", nov.named_place({"street": "Na Hutmance", "lat": 50.0550, "lon": 14.3680}), None)


# --- sloučení: baseline, zmizení jen po 404 --------------------------------- #
T0, T1, T2 = "2026-09-01T00:00:00Z", "2026-09-05T00:00:00Z", "2026-09-11T00:00:00Z"
a = dict(scrape.parse_novostavba(result(100), "prodej"))
b = dict(scrape.parse_novostavba(result(101, code=10), "prodej"))
recs, ev = nov.merge(None, [a, b], T0, baseline=True)
check("baseline: žádné události", ev, [])
check("baseline: záznamy označené", [x["baseline"] for x in recs], [True, True])

# Běh 2: b chybí v hledání, detail žije -> zůstává živý.
recs2, ev2 = nov.merge(recs, [a], T1, baseline=False, verify=lambda rec: "live")
rb = next(x for x in recs2 if x["id"] == 101)
check("chybí v hledání + detail žije = živý", (rb["gone_at"], rb.get("missing_from_search")), (None, T1))
check("…a žádná událost", ev2, [])
# Nejistota (5xx/timeout) totéž.
recs2u, ev2u = nov.merge(recs, [a], T1, baseline=False, verify=lambda rec: "unknown")
check("nejistota = živý", next(x for x in recs2u if x["id"] == 101)["gone_at"], None)
# Bez ověření (strop vyčerpán) se nic neprohlásí za zmizelé.
recs2c, _ = nov.merge(recs, [a], T1, baseline=False, verify=lambda rec: "gone", max_checks=0)
check("strop ověření 0 = nikdo zmizelý", next(x for x in recs2c if x["id"] == 101)["gone_at"], None)

# Běh 3: detail b vrací 404 -> zmizel.
recs3, ev3 = nov.merge(recs2, [a], T2, baseline=False, verify=lambda rec: "gone")
rb3 = next(x for x in recs3 if x["id"] == 101)
check("404 = zmizel", rb3["gone_at"], T2)
check("událost gone", [e["kind"] for e in ev3], ["gone"])
check("dny na trhu first_seen→gone_at", rb3["days_on_market"], 10)
dom = nov.days_on_market(rb3, "2026-10-01T00:00:00Z")
check("baseline = dolní mez", (dom["days"], dom["lower_bound"], dom["live"]), (10, True, False))
# Zmizelý se znovu neověřuje a gone_at se nepřepisuje.
recs4, ev4 = nov.merge(recs3, [a], "2026-09-12T00:00:00Z", baseline=False,
                       verify=lambda rec: (_ for _ in ()).throw(AssertionError("neověřovat")))
check("gone_at drží, bez ověření", next(x for x in recs4 if x["id"] == 101)["gone_at"], T2)
check("…bez události", ev4, [])

# Nový inzerát po baseline: event new, baseline False, dny od prvního výskytu.
c = dict(scrape.parse_novostavba(result(102), "pronajem"))
recs5, ev5 = nov.merge(recs4, [a, c], "2026-09-12T00:00:00Z", baseline=False)
rc = next(x for x in recs5 if x["id"] == 102)
check("nový po baseline", ([e["kind"] for e in ev5], rc["baseline"]), (["new"], False))
dom = nov.days_on_market(rc, "2026-09-15T12:00:00Z")
check("živý: dny do teď, ne dolní mez", (dom["days"], dom["lower_bound"], dom["live"]), (4, False, True))
check("dny se zaokrouhlují jako v JS (3,5 → 4)", nov.days_between("2026-09-01T00:00:00Z", "2026-09-04T12:00:00Z"), 4)

# Změna ceny živého -> událost price s oběma cenami, historie roste.
a2 = dict(a, price_czk=19_000_000)
recs6, ev6 = nov.merge(recs5, [a2, c], "2026-09-13T00:00:00Z", baseline=False)
check("zlevnění = událost price", [(e["kind"], e["old_price"], e["rec"]["price_czk"]) for e in ev6],
      [("price", 20_000_000, 19_000_000)])
check("historie ceny", len(next(x for x in recs6 if x["id"] == 100)["price_history"]), 2)
check("baseline příznak se nese", next(x for x in recs6 if x["id"] == 100)["baseline"], True)

# Cena na dotaz nesmí smazat poslední známou cenu pro příští alert.
unknown = dict(a2, price_czk=None, price_czk_per_sqm=None)
recs_unknown, ev_unknown = nov.merge(recs6, [unknown], "2026-09-14T00:00:00Z", baseline=False)
check("na dotaz = bez cenového alertu", ev_unknown, [])
back = dict(a2, price_czk=18_000_000)
_, ev_back = nov.merge(recs_unknown, [back], "2026-09-15T00:00:00Z", baseline=False)
check("návrat ceny porovná poslední známou", [(e["old_price"], e["rec"]["price_czk"]) for e in ev_back],
      [(19_000_000, 18_000_000)])

# Návrat po potvrzeném 404 je nová nabídka a nový úsek dní na trhu.
returned, ev_returned = nov.merge(recs3, [a, b], "2026-09-21T00:00:00Z", baseline=False)
rb_returned = next(x for x in returned if x["id"] == 101)
check("návrat po 404 = alert", [e["kind"] for e in ev_returned], ["returned"])
check("návrat = dny od opětovného výskytu", nov.days_on_market(rb_returned, "2026-09-25T00:00:00Z")["days"], 4)
still_returned, _ = nov.merge(returned, [a, b], "2026-09-25T00:00:00Z", baseline=False)
check("další běh zachová začátek obnovené nabídky",
      nov.days_on_market(next(x for x in still_returned if x["id"] == 101), "2026-09-25T00:00:00Z")["days"], 4)

# Detail žije, ale už to není novostavba -> left_filter, ne zmizení.
recs7, ev7 = nov.merge(recs6, [a2], "2026-09-14T00:00:00Z", baseline=False, verify=lambda rec: "left_filter")
rc7 = next(x for x in recs7 if x["id"] == 102)
check("left_filter není zmizení", (rc7["gone_at"], bool(rc7.get("left_filter_at")), ev7), (None, True, []))


# --- verify_novostavba: jen 404 je gone ------------------------------------- #
orig_detail = scrape._novostavba_detail
try:
    scrape._novostavba_detail = lambda rec: (None, 404)
    check("detail 404 → gone", scrape.verify_novostavba({"url": "https://x"}), "gone")

    def boom(rec):
        raise scrape.TransientFetchError("HTTP 503 after 4 attempts")
    scrape._novostavba_detail = boom
    check("5xx po retry → unknown", scrape.verify_novostavba({"url": "https://x"}), "unknown")
    scrape._novostavba_detail = lambda rec: (None, 403)
    check("jiný status → unknown", scrape.verify_novostavba({"url": "https://x"}), "unknown")
    scrape._novostavba_detail = lambda rec: ({"params": {"buildingCondition": {"name": "Very good", "value": 1}},
                                              "categorySubCb": {"value": 8}}, 200)
    check("žije, stav 1 → live (stav se třídí, nefiltruje)", scrape.verify_novostavba({"url": "https://x"}), "live")
    scrape._novostavba_detail = lambda rec: ({"params": {}, "categorySubCb": {"value": 9}}, 200)
    check("žije, ale 4+1 → left_filter", scrape.verify_novostavba({"url": "https://x"}), "left_filter")
    scrape._novostavba_detail = lambda rec: ({"params": {"buildingCondition": {"name": "Novostavba", "value": 6}},
                                              "categorySubCb": {"value": 10}}, 200)
    check("žije, novostavba 5+kk → live", scrape.verify_novostavba({"url": "https://x"}), "live")
finally:
    scrape._novostavba_detail = orig_detail


# --- fetch_novostavby: první běh a změna konfigurace jsou tiché ------------- #
def run_fetch(prev_snapshot, found):
    o_search, o_enrich = scrape.search_ward_novostavby, scrape.enrich_novostavba
    o_verify = scrape.verify_novostavba
    calls = {"n": 0}

    def fake_search(ward, tx):
        calls["n"] += 1
        return [dict(x) for x in found if x["transaction_type"] == tx] if ward == "Jinonice" else []
    scrape.search_ward_novostavby = fake_search
    scrape.enrich_novostavba = lambda rec: "ok"
    scrape.verify_novostavba = lambda rec: "live"
    try:
        return scrape.fetch_novostavby(prev_snapshot), calls["n"]
    finally:
        scrape.search_ward_novostavby, scrape.enrich_novostavba = o_search, o_enrich
        scrape.verify_novostavba = o_verify


far = scrape.parse_novostavba(result(200, lat=50.0541, lon=14.4100), "prodej")      # ~3 km
found = [scrape.parse_novostavba(result(i), "prodej") for i in range(300, 305)] + [far]
(recs_a, ev_a, meta_a), n_req = run_fetch(None, found)
check("první běh: tichá baseline", (ev_a, meta_a["baseline_run"]), ([], True))
check("mimo nadmnožinu se neukládá", sorted(r["id"] for r in recs_a), [300, 301, 302, 303, 304])
check("hledání = čtvrti × transakce", n_req, len(nov.WARDS) * len(nov.TRANSACTIONS))

snap = {"novostavby": recs_a, "novostavby_config": nov.fingerprint(), "novostavby_meta": meta_a}
more = found + [scrape.parse_novostavba(result(i), "prodej") for i in range(400, 403)]
(recs_b, ev_b, meta_b), _ = run_fetch(snap, more)
check("stejná konfigurace: 3 nové = 3 události", sorted(e["rec"]["id"] for e in ev_b), [400, 401, 402])

old_cfg = dict(nov.fingerprint(), superset_km=1.5)
snap_changed = dict(snap, novostavby_config=old_cfg)
(recs_c, ev_c, meta_c), _ = run_fetch(snap_changed, more)
check("změna konfigurace: žádná záplava", ev_c, [])
check("…nové dostanou baseline", all(r["baseline"] for r in recs_c if r["id"] >= 400), True)
check("…a meta to zaznamená", (meta_c["baseline_run"], bool(meta_c["config_changed_at"])), (True, True))
check("baseline_at se drží z prvního běhu", meta_c["baseline_at"], meta_a["baseline_at"])

(recs_null, ev_null, meta_null), _ = run_fetch(dict(snap, novostavby_config=None), found)
check("null konfigurace ze starého snapshotu = tichá baseline",
      (len(recs_null), ev_null, meta_null["baseline_run"]), (5, [], True))

old_center = nov.CENTER
try:
    nov.CENTER = (50.0600, 14.3700)
    (recentered, _, _), _ = run_fetch(snap, found)
    check("změna středu přepočítá uložené km",
          next(r for r in recentered if r["id"] == 300)["km"],
          nov.km_from_center(found[0]["lat"], found[0]["lon"]))
finally:
    nov.CENTER = old_center

# Přechod ze serverového filtru „novostavba" na třídění (27. 9.) je změna
# konfigurace: otisk z doby filtru (condition 6, bez classifier) -> tichá baseline.
filter_era = {k: v for k, v in nov.fingerprint().items() if k not in ("classifier", "new_from_year")}
filter_era["condition"] = 6
(recs_d, ev_d, meta_d), _ = run_fetch(dict(snap, novostavby_config=filter_era), more)
check("přechod z filtru na třídění: tichý", (ev_d, meta_d["baseline_run"]), ([], True))


# Detaily: nové první, strop drží, zbytek počká (a nemá typ, takže do alertu nesmí).
def run_fetch_detail(prev_snapshot, found, cap):
    o_search, o_detail, o_cap = scrape.search_ward_novostavby, scrape._novostavba_detail, \
        scrape.MAX_NOVOSTAVBY_DETAIL_FETCHES
    reads = []

    def fake_detail(rec):
        reads.append(rec["id"])
        return {"params": {"buildingCondition": {"name": "Novostavba", "value": 6},
                           "acceptanceYear": 2024}, "categorySubCb": {"value": 8}}, 200
    scrape.search_ward_novostavby = lambda ward, tx: (
        [dict(x) for x in found if x["transaction_type"] == tx] if ward == "Jinonice" else [])
    scrape._novostavba_detail = fake_detail
    scrape.MAX_NOVOSTAVBY_DETAIL_FETCHES = cap
    try:
        return scrape.fetch_novostavby(prev_snapshot), reads
    finally:
        scrape.search_ward_novostavby, scrape._novostavba_detail = o_search, o_detail
        scrape.MAX_NOVOSTAVBY_DETAIL_FETCHES = o_cap


five = [scrape.parse_novostavba(result(i), "prodej") for i in range(800, 805)]
(rd, _e, md), reads = run_fetch_detail(None, five, cap=3)
check("strop detailů", (len(reads), md["detail_deferred"]), (3, 2))
check("přečtené mají typ, odložené neurčeno",
      sorted(r["kind"] for r in rd), ["dokoncena"] * 3 + ["neurceno"] * 2)
snap_d = {"novostavby": rd, "novostavby_config": nov.fingerprint(), "novostavby_meta": md}
(rd2, ev_d2, _m), reads2 = run_fetch_detail(snap_d, five, cap=60)
check("další běh dočte jen zbylé 2", len(reads2), 2)
(rd3, _e3, _m3), reads3 = run_fetch_detail({"novostavby": rd2, "novostavby_config": nov.fingerprint(),
                                            "novostavby_meta": md}, five, cap=60)
check("ustálený stav: 0 detailů", len(reads3), 0)

empty_snap = {"novostavby": [], "novostavby_config": nov.fingerprint(), "novostavby_meta": {}}
(pending, first_events, _), _ = run_fetch_detail(empty_snap, five, cap=3)
check("nové bez detailu zatím bez alertu", len(nov.alert_events(first_events)), 3)
(completed, delayed_events, _), _ = run_fetch_detail(
    {"novostavby": pending, "novostavby_config": nov.fingerprint(), "novostavby_meta": {}}, five, cap=60)
check("odložené nové po dočtení alertují", sorted(e["rec"]["id"] for e in nov.alert_events(delayed_events)),
      [803, 804])


# --- statistika ------------------------------------------------------------- #
S = [
    {"id": 1, "transaction_type": "prodej", "disposition": "4+kk", "price_czk": 20_000_000,
     "price_czk_per_sqm": 200_000, "lat": 50.0545, "lon": 14.3680, "first_seen": T0},
    {"id": 2, "transaction_type": "prodej", "disposition": "4+kk", "price_czk": 24_000_000,
     "price_czk_per_sqm": 240_000, "lat": 50.0545, "lon": 14.3680, "first_seen": T0},
    {"id": 3, "transaction_type": "prodej", "disposition": "4+kk", "price_czk": 18_000_000,
     "lat": 50.0545, "lon": 14.3680, "first_seen": T0, "gone_at": T2, "baseline": True},
    # mimo výchozí kruh:
    {"id": 4, "transaction_type": "prodej", "disposition": "4+kk", "price_czk": 99_000_000,
     "lat": 50.0541, "lon": 14.3950, "first_seen": T0},
]
st = nov.compute_stats(S, T2, kinds=None)["prodej_4+kk"]
check("živé jen v kruhu, bez zmizelých", st["live_n"], 2)
check("medián ceny", st["median_price_czk"], 22_000_000)
check("zmizelé: n, dny, dolní mez, poslední cena",
      (st["gone_n"], st["median_days_to_gone"], st["days_lower_bound"], st["gone_last_prices_czk"]),
      (1, 10, True, [18_000_000]))
check("prázdná skupina", nov.compute_stats(S, T2, kinds=None)["pronajem_5+kk"]["live_n"], 0)
S[0]["kind"], S[1]["kind"] = "dokoncena", "starsi"
check("výchozí statistika jen z dokončených", nov.compute_stats(S, T2)["prodej_4+kk"]["live_n"], 1)


# --- klasifikace ------------------------------------------------------------ #
def cls(**kw):
    return nov.classify(dict({"detail_read": True}, **kw), "2026-09-27T00:00:00Z")


# Skutečné případy z 27. 9. (do 2 km od U Kříže):
check("U Kříže: dobrý, kolaudace 2000 → starší", cls(building_condition=2, acceptance_year=2000),
      ("starsi", "kolaudace 2000", False))
check("Kohoutových: velmi dobrý, bez roku, popis „novostavba“ → starší s poznámkou",
      cls(building_condition=1, desc_mentions_new=True),
      ("starsi", "stav: velmi dobrý, rok neuveden · popis zmiňuje novostavbu", False))
check("Bochovská: panel, velmi dobrý, bez roku → starší", cls(building_condition=1)[0], "starsi")
check("Na Hutmance: štítek novostavba bez roku → dokončená, rok neuveden",
      cls(building_condition=6), ("dokoncena", "stav: novostavba, rok neuveden", True))
check("U Komína: novostavba, kolaudace 2025 → dokončená", cls(building_condition=6, acceptance_year=2025),
      ("dokoncena", "kolaudace 2025", False))
check("Naskové: štítek novostavba, kolaudace 2019 → starší", cls(building_condition=6, acceptance_year=2019),
      ("starsi", "kolaudace 2019 (štítek novostavba)", False))
check("Radlická: kolaudace 2029 → výstavba", cls(building_condition=6, acceptance_year=2029),
      ("vystavba", "kolaudace plánována 2029", False))
check("ve výstavbě", cls(building_condition=4), ("vystavba", "stav: ve výstavbě", False))
check("projekt", cls(building_condition=5)[0], "vystavba")
check("velmi dobrý + kolaudace 2022 → dokončená", cls(building_condition=1, acceptance_year=2022)[0], "dokoncena")
check("rekonstrukce 2025 se nepočítá", cls(building_condition=1, reconstruction_year=2025),
      ("starsi", "stav: velmi dobrý, rok neuveden · rekonstrukce 2025", False))
check("nesmyslný rok se ignoruje", cls(building_condition=1, acceptance_year=20225)[0], "starsi")
check("bez detailu = neurčeno", nov.classify({}, T0)[0], "neurceno")

# Detail: nový hned, starší verze znovu, jinak po týdnu.
check("detail: nový", nov.needs_detail({}, None, T0), True)
check("detail: stará verze", nov.needs_detail({}, {"detail_read": True, "detail_read_at": T0}, T0), True)
fresh = {"detail_read": True, "detail_read_at": T0, "detail_version": nov.DETAIL_VERSION}
check("detail: čerstvý ne", nov.needs_detail({}, fresh, T1), False)
check("detail: po 7 dnech ano", nov.needs_detail({}, fresh, "2026-09-08T00:00:00Z"), True)

# Čerstvý detail přebíjí i svým None; nečerstvý záznam si nese staré.
old = dict(scrape.parse_novostavba(result(700), "prodej"), detail_read=True, building_condition=6,
           acceptance_year=2025, detail_version=2, detail_read_at=T0)
m1, _ = nov.merge(None, [old], T0, baseline=True)
check("merge nastaví typ", (m1[0]["kind"], m1[0]["kind_reason"]), ("dokoncena", "kolaudace 2025"))
m2, _ = nov.merge(m1, [dict(scrape.parse_novostavba(result(700), "prodej"))], T1, baseline=False)
check("bez nového detailu se typ nese", m2[0]["kind"], "dokoncena")
re_read = dict(scrape.parse_novostavba(result(700), "prodej"), detail_read=True, detail_fresh=True,
               building_condition=4, acceptance_year=None, detail_version=2, detail_read_at=T1)
m3, ev3b = nov.merge(m2, [re_read], T2, baseline=False)
check("čerstvý detail přebije rok i stav", (m3[0]["kind"], m3[0].get("acceptance_year")), ("vystavba", None))
check("změna typu se zaznamená, nehlásí", (m3[0]["kind_before"], m3[0]["kind_changed_at"], ev3b),
      ("dokoncena", T2, []))
check("detail_fresh se neukládá", "detail_fresh" in m3[0], False)


# --- alert ------------------------------------------------------------------ #
ev_rec = dict(scrape.parse_novostavba(result(500, street="U kříže <b>x</b>"), "prodej"),
              first_seen=T0, baseline=False, detail_read=True, building_condition=6, acceptance_year=2023)
ev_rec["named_place"] = nov.named_place(ev_rec)
ev_rec["kind"], ev_rec["kind_reason"], _ = nov.classify(ev_rec, T2)
gone_rec = dict(ev_rec, id=501, gone_at=T2, baseline=True, street="Bochovská")
far_rec = dict(ev_rec, id=502, lat=50.0541, lon=14.3950, km=1.9)
text = nov.build_alert([{"kind": "new", "rec": ev_rec}, {"kind": "gone", "rec": gone_rec},
                        {"kind": "new", "rec": far_rec}], T2, "https://example.github.io/x/")
check("ulice escapovaná", "<b>x</b>" in text, False)
check("řádek nese typ", "[dokončená · kolaudace 2023]" in text, True)
U = "https://www.sreality.cz/detail/prodej/byt/4%2Bkk/x/"
old_rec = dict(ev_rec, id=503, url=U + "503", kind="starsi", kind_reason="kolaudace 2000")
bld_rec = dict(ev_rec, id=504, url=U + "504", kind="vystavba", kind_reason="stav: ve výstavbě")
unk_rec = dict(ev_rec, id=505, url=U + "505", kind="neurceno")
t2 = nov.build_alert([{"kind": "new", "rec": r} for r in (old_rec, bld_rec, unk_rec)], T2)
check("starší ani neověřené se nehlásí, výstavba ano",
      ("/x/503" in t2, "/x/505" in t2, "/x/504" in t2, "[výstavba · stav: ve výstavbě]" in t2),
      (False, False, True, True))
check("jen starší = žádná zpráva", nov.build_alert([{"kind": "new", "rec": old_rec}], T2), None)
check("nový s odkazem", '🆕 4+kk prodej' in text and 'href="https://www.sreality.cz/detail/prodej/byt/4%2Bkk/x/500"' in text, True)
check("zmizelý s dny (≥ u baseline)", "na trhu ≥ 10 d" in text, True)
check("mimo alertový kruh se nehlásí", "/x/502" in text, False)
check("Kč/m² a km v řádku", "200 000 Kč/m²" in text and "km" in text, True)
check("nic k hlášení = None", nov.build_alert([], T2), None)
check("jen mimo kruh = None", nov.build_alert([{"kind": "new", "rec": far_rec}], T2), None)
check("návrat je srozumitelný v alertu",
      "znovu v nabídce" in nov.build_alert([{"kind": "returned", "rec": ev_rec}], T2), True)

with tempfile.TemporaryDirectory() as tmp:
    pending_path = Path(tmp) / "alert.txt"
    check("příprava vytvoří alert", nov.stage_alert([{"kind": "new", "rec": ev_rec}], T2, pending_path), True)
    check("příprava neodesílá", pending_path.exists(), True)
    check("odeslání připraveného alertu jen dry-run",
          nov.send_staged_alert(pending_path, dry_run=True), "dry-run")
    nov.stage_alert([], T2, pending_path)
    check("tichý běh odstraní starý alert", pending_path.exists(), False)

workflow = (Path(__file__).parent / ".github/workflows/scrape.yml").read_text()
check("produkční odeslání následuje až po pushi",
      workflow.index("run: python send_novostavby_alert.py") > workflow.index("git push"), True)

# Dry-run neposílá a vrací "dry-run"; selhání kanálu neshodí běh.
orig_send = notify.send_telegram
sent = []
notify.send_telegram = lambda t: sent.append(t) or True
check("dry-run", nov.send_alert([{"kind": "new", "rec": ev_rec}], T2, dry_run=True), "dry-run")
check("dry-run nic neposlal", sent, [])


def refuse(t):
    raise notify.NotifyError("Telegram odmítl")
notify.send_telegram = refuse
check("odmítnutí kanálem → None, bez výjimky", nov.send_alert([{"kind": "new", "rec": ev_rec}], T2), None)
notify.send_telegram = lambda t: False
orig_ntfy = notify.send_ntfy
notify.send_ntfy = lambda t: False
check("bez secrets → None", nov.send_alert([{"kind": "new", "rec": ev_rec}], T2), None)
notify.send_telegram, notify.send_ntfy = orig_send, orig_ntfy


# --- stránka ---------------------------------------------------------------- #
check("kolekce nikdy neběžela = žádná karta", nov.card_html(None), "")
card = nov.card_html([], "2026-09-27T09:00:00Z")
check("karta: mapa, posuvník, reset, tabulka",
      all(x in card for x in ('id="novMap"', 'id="novR"', 'id="novReset"', 'id="tblNov"')), True)
check("karta říká, že alerty jsou pro výchozí kruh", "Alerty chodí pro výchozí okruh 1,2 km" in card, True)
# Radim 27. 9.: kruh se mění jen v režimu úprav a ukládá se tlačítkem, aby ho
# zoom nebo klik do mapy nezměnil omylem. Chování ověřeno v headless Chromiu;
# tady jen, že se výchozí stav stránky nevrátí k „vždy upravitelné".
check("karta: Upravit polohu / Uložit / Zrušit",
      all(x in card for x in ('id="novEdit"', 'id="novSave"', 'id="novCancel"')), True)
check("posuvník je bez režimu úprav zamčený", bool(re.search(r'<input type="range" id="novR"[^>]*\bdisabled\b', card)), True)
check("klik do mapy mimo úpravy nic nedělá", 'NM.on("click", e => {\n      if (!editing) return;' in nov.page_js("null"), True)
check("✚ se bez úprav netáhne", "draggable: false" in nov.page_js("null") and "dragging.enable()" in nov.page_js("null"), True)
check("hidden vyhraje nad display tlačítek", ".nov-ctl [hidden] { display: none !important; }" in nov.CSS, True)
pp = nov.page_payload([dict(ev_rec, url="javascript:alert(1)"), dict(ev_rec, id=9, out_of_scope=True)], T2)
check("payload: bez out_of_scope, bez ne-https URL", ([r["id"] for r in pp["records"]], "url" in pp["records"][0]),
      ([500], False))
pp2 = nov.page_payload([dict(ev_rec, since="<svg onload=x>"), dict(ev_rec, id=8, since="2026-09-09")], T2)
check("payload: since jen jako YYYY-MM-DD", [r.get("since") for r in pp2["records"]], [None, "2026-09-09"])

# Plocha z detailu (titulek bez m²) se nese i do dalších běhů -- detail se
# podruhé nečte (review 27. 9.).
no_area = dict(scrape.parse_novostavba(result(600, name="Prodej bytu 4+kk"), "prodej"))
enriched = dict(no_area, floor_area_sqm=110.0, price_czk_per_sqm=181818, detail_area_sqm=110, detail_read=True)
m1, _ = nov.merge(None, [enriched], T0, baseline=True)
m2, _ = nov.merge(m1, [dict(no_area)], T1, baseline=False)
check("plocha z detailu přežije další běh", (m2[0]["floor_area_sqm"], m2[0]["price_czk_per_sqm"]),
      (110.0, 181818))

js = nov.page_js(scrape.script_json(pp))
check("JS: payload vložen, žádný placeholder", "__NOV_JSON__" in js, False)
check("script_json nenechá '<' v datech", "<b>" in scrape.script_json(pp), False)

# --- dny na trhu podle data Sreality (27. 9.) -------------------------------- #
# Baseline z 27. 9. psala u všech „≥ 0 d". Když detail uvádí `since`, počítá se
# od něj (a není to dolní mez); ≥ jen tam, kde datum Sreality chybí.
BL = "2026-09-27T14:26:01Z"
bl_since = {"first_seen": BL, "baseline": True, "since": "2026-04-27"}
d = nov.days_on_market(bl_since, "2026-09-28T00:00:00Z")
check("since dřív než baseline: dny od since, ne dolní mez", (d["days"], d["lower_bound"], d["source"]),
      (154, False, "sreality"))
d = nov.days_on_market({"first_seen": BL, "baseline": True}, "2026-09-28T00:00:00Z")
check("bez since: baseline = dolní mez", (d["days"], d["lower_bound"], d["source"]), (0, True, "baseline"))
d = nov.days_on_market({"first_seen": BL, "baseline": True, "since": "2026-02-30"}, "2026-09-28T00:00:00Z")
check("nesmyslné since se ignoruje", (d["lower_bound"], d["source"]), (True, "baseline"))
d = nov.days_on_market({"first_seen": BL, "baseline": True, "since": "<img>"}, "2026-09-28T00:00:00Z")
check("ne-datum since se ignoruje", d["source"], "baseline")
# since POZDĚJI než náš první výskyt (inzerát smazán a vložen znovu, Sreality
# datum vynulovala): platí dřívější důkaz, tedy náš first_seen.
d = nov.days_on_market({"first_seen": "2026-09-01T00:00:00Z", "baseline": False, "since": "2026-09-20",
                        "gone_at": "2026-09-21T00:00:00Z"}, "2026-09-28T00:00:00Z")
check("since po first_seen: počítá se od first_seen", (d["days"], d["source"], d["lower_bound"]), (20, "ours", False))
S2 = [dict(S[2], kind="dokoncena", since="2026-08-22"),        # zmizel T2, baseline, since známé
      dict(S[0], id=11, kind="dokoncena", exclude_from_stats=True)]
st2 = nov.compute_stats(S2, T2)["prodej_4+kk"]
check("statistika: do zmizení od since, bez ≥", (st2["median_days_to_gone"], st2["days_lower_bound"]), (20, False))
check("statistika: „mimo statistiku“ se nepočítá", st2["live_n"], 0)
gone_since = dict(gone_rec, since="2026-08-22")
t3 = nov.build_alert([{"kind": "gone", "rec": gone_since}], T2)
check("alert: dny podle Sreality", "na trhu 20 d (podle Sreality)" in t3, True)

# --- detail inzerátu: popis, fotky, prodejce (27. 9.) ----------------------- #
orig_detail = scrape._novostavba_detail
try:
    scrape._novostavba_detail = lambda rec: ({
        "params": {"buildingCondition": {"name": "Novostavba", "value": 6}, "acceptanceYear": 2024,
                   "since": "2026-04-27", "floorNumber": 3, "floors": 6},
        "categorySubCb": {"value": 8},
        "description": "Krásný byt u Waltrovky. Volejte 724 223 828 nebo pis@makler.cz.",
        "seller": {"name": "Makléř s.r.o. info@makler.cz"},
        "images": [{"url": "//d18-a.sdn.cz/d_18/c_img_A/abc.jpeg"}],
    }, 200)
    er = dict(scrape.parse_novostavba(result(900), "prodej"))
    check("enrich: verdikt", scrape.enrich_novostavba(er), "ok")
    check("enrich: popis uložen bez kontaktů",
          ("724 223 828" in er["description"], "pis@makler.cz" in er["description"], "Waltrovky" in er["description"]),
          (False, False, True))
    check("enrich: prodejce bez e-mailu", er["seller_name"], "Makléř s.r.o.")
    check("enrich: fotky, patro, verze", (len(er["images"]) >= 1, er["images"][0].startswith("https://"),
                                          er["floor_number"], er["floors_total"], er["detail_version"]),
          (True, True, 3, 6, nov.DETAIL_VERSION))
finally:
    scrape._novostavba_detail = orig_detail

# Nová pole se k uloženým záznamům dostanou přes DETAIL_VERSION v rámci
# běžného stropu -- bez baseline, bez alertů.
old_v = [dict(r, detail_version=2) for r in rd3]
(rv, ev_v, mv), reads_v = run_fetch_detail({"novostavby": old_v, "novostavby_config": nov.fingerprint(),
                                            "novostavby_meta": md}, five, cap=3)
check("verze 2 → přečíst znovu, strop drží", (len(reads_v), mv["detail_deferred"]), (3, 2))
check("…žádné události ani baseline", (ev_v, mv["baseline_run"]), ([], False))
check("…odložené si nesou starý typ", sorted(r["kind"] for r in rv), ["dokoncena"] * 5)
(rv2, _e, _m), reads_v2 = run_fetch_detail({"novostavby": rv, "novostavby_config": nov.fingerprint(),
                                            "novostavby_meta": md}, five, cap=60)
check("…další běh dočte zbylé 2", len(reads_v2), 2)
check("pole detailu se nesou dál (merge)", all(k in nov.DETAIL_FIELDS for k in
      ("description", "images", "seller_name", "floor_number")), True)
m_d1, _ = nov.merge(None, [dict(er, detail_read_at=T0)], T0, baseline=True)
m_d2, _ = nov.merge(m_d1, [dict(scrape.parse_novostavba(result(900), "prodej"))], T1, baseline=False)
check("popis a fotky přežijí běh bez detailu", (bool(m_d2[0].get("description")), bool(m_d2[0].get("images"))),
      (True, True))

# --- stránka: detail, opravy, since ----------------------------------------- #
pr = dict(ev_rec, id=950, description="Tel. 724 223 828", seller_name="X y@z.cz",
          images=["https://d18-a.sdn.cz/a.jpeg", "javascript:alert(1)"], since="2026-04-27",
          baseline=True, first_seen=BL)
before = dict(pr)
pp3 = nov.page_payload([pr], "2026-09-28T00:00:00Z",
                       {"950": {"id": "950", "floor_area_sqm": 125.0, "exclude_from_stats": True, "note": "ověřeno"}})
p0 = pp3["records"][0]
check("payload: popis bez telefonu, prodejce bez e-mailu",
      ("724" in p0["description"], p0["seller_name"]), (False, "X"))
check("payload: jen https fotky", p0["images"], ["https://d18-a.sdn.cz/a.jpeg"])
check("payload: začátek podle Sreality", (p0["market_start"], p0["market_source"], p0["days_lower_bound"]),
      ("2026-04-27T00:00:00Z", "sreality", False))
check("payload: oprava plochy → Kč/m² přepočtené",
      (p0["floor_area_sqm"], p0["price_czk_per_sqm"], p0["floor_area_source"]), (125.0, 160_000, "override"))
check("payload: mimo statistiku + poznámka", (p0["exclude_from_stats"], p0["override_note"]), (True, "ověřeno"))
check("oprava nemění uložený záznam", pr, before)
check("bez oprav payload beze změny plochy", nov.page_payload([pr], T2)["records"][0]["floor_area_sqm"],
      pr["floor_area_sqm"])
card2 = nov.card_html([], None)
check("karta: poznámka o datu Sreality a vynulování", "podle Sreality" in card2 and "vynuluje" in card2, True)
js2 = nov.page_js(scrape.script_json(pp3))
check("JS: klik na řádek přes data-nid, ne inline id", ('data-nid="${escapeHtml(String(r.id))}"' in js2,
                                                        "onclick=\"openNov" in js2), (True, False))


# --- dokončení z popisu (27. 9. večer) --------------------------------------- #
cfd = nov.completion_from_description
check("Q4/2027", cfd("Předpokládaný termín dokončení Q4/2027.  Přehlednou dispozici"),
      {"year": 2027, "month": 10, "text": "dokončení Q4/2027"})
check("předpokládaná kolaudace 2027", cfd("Předpokládaná kolaudace 2027.")["year"], 2027)
check("nastěhování v roce 2028", cfd("Nastěhování v roce 2028.")["text"], "Nastěhování v roce 2028")
check("na jaře 2028 (Leitzova)", cfd("byty budou dokončeny k nastěhování na jaře 2028.  V ceně")["month"], 3)
check("rok před slovem ve stejné větě", cfd("V roce 2027 bude dům dokončen.")["year"], 2027)
check("pronájem: nastěhování = volnost bytu, ne dokončení",
      cfd("Nastěhování v roce 2027.", rental=True), None)
check("nastěhování od přesného data se nebere", cfd("Byt je volný, nastěhování od 1.11.2027."), None)
check("pronájem: kolaudace 2027 platí dál", cfd("Předpokládaná kolaudace 2027.", rental=True)["year"], 2027)
check("dům je aktuálně ve výstavbě", cfd("Dům je aktuálně ve výstavbě."), {"year": None, "month": None, "text": "ve výstavbě"})
for label, txt in [("rekonstrukce 2027", "Plánovaná rekonstrukce 2027."), ("sleva do 2027", "Sleva platí do 2027."),
                   ("při koupi do 30. 9. 2026", "Při koupi do 30. 9. 2026 s nabídkou stání."),
                   ("rok v jiné větě", "Dům byl dokončen. Sleva platí do 2027."),
                   ("škola ve výstavbě v okolí", "V okolí je ve výstavbě nová škola."),
                   ("volný od 1.10.2026", "Z roku 2020. Byt je volný od 1.10.2026."),
                   ("dokončení bez roku", "Doplatek až po dokončení stavby a kolaudaci projektu.")]:
    check(f"nic: {label}", cfd(txt), None)
NOW = "2026-09-27T00:00:00Z"
check("štítek novostavba + popis Q4/2027 → výstavba",
      cls(building_condition=6, desc_completion={"year": 2027, "month": 10, "text": "dokončení Q4/2027"}),
      ("vystavba", "popis: dokončení Q4/2027", False))
check("…letos s pozdějším čtvrtletím → výstavba",
      cls(building_condition=6, desc_completion={"year": 2026, "month": 10, "text": "dokončení Q4/2026"})[0], "vystavba")
check("…letos, dřívější čtvrtletí → dokončená",
      cls(building_condition=6, desc_completion={"year": 2026, "month": 7, "text": "dokončení Q3/2026"})[0], "dokoncena")
check("…letos bez období → dokončená",
      cls(building_condition=6, desc_completion={"year": 2026, "month": None, "text": "kolaudace 2026"})[0], "dokoncena")
check("…minulý rok nic nepřeklápí",
      cls(building_condition=6, desc_completion={"year": 2021, "month": None, "text": "kolaudace 2021"})[0], "dokoncena")
check("…„ve výstavbě“ bez roku → výstavba",
      cls(building_condition=6, desc_completion={"year": None, "month": None, "text": "ve výstavbě"})[1], "popis: ve výstavbě")
check("rok kolaudace v datech má přednost před popisem",
      cls(building_condition=6, acceptance_year=2024,
          desc_completion={"year": 2027, "month": 10, "text": "dokončení Q4/2027"})[0], "dokoncena")
check("jen štítek novostavba se popisem překlápí (velmi dobrý ne)",
      cls(building_condition=1, desc_completion={"year": 2027, "month": 10, "text": "dokončení Q4/2027"})[0], "starsi")
check("desc_completion se nese mezi běhy", "desc_completion" in nov.DETAIL_FIELDS, True)
check("otisk konfigurace se nemění (bez baseline)", nov.fingerprint()["classifier"], 1)
# Přetřídění dokončená → výstavba u sledovaného inzerátu se zaznamená, nehlásí.
hut = dict(scrape.parse_novostavba(result(960), "prodej"), detail_read=True, building_condition=6,
           detail_version=nov.DETAIL_VERSION, detail_read_at=T0)
h1, _ = nov.merge(None, [hut], T0, baseline=True)
check("před: dokončená", h1[0]["kind"], "dokoncena")
h2, evh = nov.merge(h1, [dict(hut, detail_fresh=True, desc_completion={"year": 2027, "month": 10,
                                                                         "text": "dokončení Q4/2027"})], T1, baseline=False)
check("po: výstavba, zaznamenáno, bez události", (h2[0]["kind"], h2[0]["kind_before"], evh), ("vystavba", "dokoncena", []))
orig_detail = scrape._novostavba_detail
try:
    scrape._novostavba_detail = lambda rec: ({
        "params": {"buildingCondition": {"name": "Novostavba", "value": 6}}, "categorySubCb": {"value": 8},
        "description": "Předpokládaný termín dokončení Q4/2027. Volejte 724 223 828."}, 200)
    eh = dict(scrape.parse_novostavba(result(961), "prodej"))
    scrape.enrich_novostavba(eh)
    check("enrich uloží desc_completion", eh["desc_completion"], {"year": 2027, "month": 10, "text": "dokončení Q4/2027"})
finally:
    scrape._novostavba_detail = orig_detail
check("JS: submitOverride najde novostavbu", "window.novItem" in nov.page_js("null"), True)


# --- celkový limit na request (fetch_next_data) ----------------------------- #
# Lokální server na 127.0.0.1: jedna cesta odpoví hned, druhá posílá tělo po
# kouscích -- každý kousek pod read timeoutem, dohromady přes limit. Přesně
# ten případ, který `timeout=` v requests nechytí.
import http.server
import threading

import requests

PAGE = ('<html><script id="__NEXT_DATA__" type="application/json">{"ok": "ž"}</script></html>').encode()
_wait = threading.Event().wait      # time.sleep je v testu vypnutý


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/missing"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if self.path.startswith("/slow"):
            self.send_header("Content-Length", str(len(PAGE) + 40))
            self.end_headers()
            try:
                for _ in range(40):          # 40 × 0,1 s = 4 s, limit 1 s
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    _wait(0.1)
                self.wfile.write(PAGE)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
orig_deadline, orig_backoff = scrape.REQUEST_DEADLINE_S, scrape.RETRY_BACKOFF_SECONDS
try:
    scrape.REQUEST_DEADLINE_S = 1
    scrape.RETRY_BACKOFF_SECONDS = ()
    check("rychlá stránka: stejné chování", scrape.fetch_next_data(base + "/ok"), ({"ok": "ž"}, 200))
    check("404 beze změny", scrape.fetch_next_data(base + "/missing"), (None, 404))
    t0 = time.monotonic()
    try:
        scrape.fetch_next_data(base + "/slow")
        check("pomalé tělo nad limit = přechodná chyba", "no error", "TransientFetchError")
    except scrape.TransientFetchError as exc:
        check("pomalé tělo nad limit = přechodná chyba", "celkový limit" in str(exc), True)
    check("…a skončí brzy po limitu, ne za 4 s", time.monotonic() - t0 < 2.5, True)
    try:
        scrape._get_with_deadline(base + "/slow", deadline_s=0.3, session=requests.Session())
        check("_get_with_deadline vyhodí requests.Timeout", "no error", "Timeout")
    except requests.Timeout:
        check("_get_with_deadline vyhodí requests.Timeout", "Timeout", "Timeout")
finally:
    scrape.REQUEST_DEADLINE_S, scrape.RETRY_BACKOFF_SECONDS = orig_deadline, orig_backoff
    srv.shutdown()

time.sleep = orig_sleep
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
