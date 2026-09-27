#!/usr/bin/env python3
"""Novostavby 4+kk / 5+kk u U Kříže: parsování, nadmnožina, zmizení jen po
404, baseline jako dolní mez, dny na trhu, tichá změna konfigurace, alert.

Bez sítě: všechno HTTP je podvržené. Nic se nezapisuje na disk.

Run: python3 test_novostavby.py
"""
import sys
import time

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

    scrape.fetch_next_data = lambda url, params=None, parse_on_404=False: (
        fake_page([result(10)], {"buildingCondition": None}), 200)
    try:
        scrape.search_ward_novostavby("Jinonice", "prodej")
        check("ignorovaný filtr stav = chyba", "no error", "TransientFetchError")
    except scrape.TransientFetchError:
        check("ignorovaný filtr stav = chyba", "TransientFetchError", "TransientFetchError")
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
    check("žije, stav 1 → left_filter", scrape.verify_novostavba({"url": "https://x"}), "left_filter")
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
st = nov.compute_stats(S, T2)["prodej_4+kk"]
check("živé jen v kruhu, bez zmizelých", st["live_n"], 2)
check("medián ceny", st["median_price_czk"], 22_000_000)
check("zmizelé: n, dny, dolní mez, poslední cena",
      (st["gone_n"], st["median_days_to_gone"], st["days_lower_bound"], st["gone_last_prices_czk"]),
      (1, 10, True, [18_000_000]))
check("prázdná skupina", nov.compute_stats(S, T2)["pronajem_5+kk"]["live_n"], 0)


# --- alert ------------------------------------------------------------------ #
ev_rec = dict(scrape.parse_novostavba(result(500, street="U kříže <b>x</b>"), "prodej"),
              first_seen=T0, baseline=False)
ev_rec["named_place"] = nov.named_place(ev_rec)
gone_rec = dict(ev_rec, id=501, gone_at=T2, baseline=True, street="Bochovská")
far_rec = dict(ev_rec, id=502, lat=50.0541, lon=14.3950, km=1.9)
text = nov.build_alert([{"kind": "new", "rec": ev_rec}, {"kind": "gone", "rec": gone_rec},
                        {"kind": "new", "rec": far_rec}], T2, "https://example.github.io/x/")
check("ulice escapovaná", "<b>x</b>" in text, False)
check("nový s odkazem", '🆕 4+kk prodej' in text and 'href="https://www.sreality.cz/detail/prodej/byt/4%2Bkk/x/500"' in text, True)
check("zmizelý s dny (≥ u baseline)", "na trhu ≥ 10 d" in text, True)
check("mimo alertový kruh se nehlásí", "/x/502" in text, False)
check("Kč/m² a km v řádku", "200 000 Kč/m²" in text and "km" in text, True)
check("nic k hlášení = None", nov.build_alert([], T2), None)
check("jen mimo kruh = None", nov.build_alert([{"kind": "new", "rec": far_rec}], T2), None)

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

time.sleep = orig_sleep
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
