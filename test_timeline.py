#!/usr/bin/env python3
"""Vývoj cen a doba na trhu (timeline.py): příprava dat a agregace.

Hlídá hlavně poctivost vůči datům: zombie pryč, živé inzeráty se nemíchají do
„dní do zmizení", znovu vložený inzerát není zmizení, začátek z prvního běhu
sledování je neznámý, zmizení před ověřováním se nepočítá. JS na stránce
počítá totéž; ověření proti stránce je v jsdom (mimo CI).

Offline, bez sítě. Run: python3 test_timeline.py
"""
import json
import sys

import timeline as T

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:60} {got!r}")


def day(s):
    return T.day_of(s)


NOW = "2026-09-27T12:00:00Z"
STATE = {"config_changes": [{"at": "2026-06-28T19:00:00Z"}, {"at": "2026-08-23T19:00:00Z"}]}


def rec(rid, **kw):
    base = {"id": rid, "source": "sreality", "transaction_type": "pronajem", "disposition": "2+kk",
            "floor_area_sqm": 50.0, "city_part": "Vysočany", "first_seen": "2026-09-01T08:00:00Z",
            "last_seen": "2026-09-27T10:00:00Z", "gone_at": None,
            "price_history": [{"at": "2026-09-01", "price_czk": 25000}]}
    base.update(kw)
    return base


# Kotva: sreality inzerát v Jinonicích z prvního dne -> určuje baseline, ne Vysočany.
records = [
    # 1 živý, začátek ze Sreality
    rec("live1", since="2026-08-20"),
    # 2 ověřeně zmizelý, začátek ze Sreality
    rec("gone1", since="2026-09-01", gone_at="2026-09-11T06:00:00Z", last_seen="2026-09-11T06:00:00Z"),
    # 3 zombie -> pryč celý
    rec("zombie", source="idnes", gone_at="2026-09-26T20:00:00Z", gone_stale=True, first_seen="2026-08-24T08:00:00Z"),
    # 4 znovu vložený: předchůdce zmizel, nástupce ho odkazuje
    rec("old", since="2026-08-25", gone_at="2026-09-05T06:00:00Z", last_seen="2026-09-05T06:00:00Z",
        first_seen="2026-08-25T10:00:00Z"),
    rec("new", since="2026-09-06", first_seen="2026-09-06T10:00:00Z",
        relist_of={"id": "old", "verdict": "same"}),
    # 5 bez since, první výskyt v prvních 48 h po rozšíření sběru -> začátek neznámý
    rec("base", source="bezrealitky", first_seen="2026-08-24T08:00:00Z"),
    # 6 bez since, viděli jsme ho přijít -> začátek známý
    rec("arr", source="bezrealitky", first_seen="2026-09-10T08:00:00Z", gone_at="2026-09-20T08:00:00Z",
        last_seen="2026-09-20T08:00:00Z"),
    # 7 zmizel před ověřováním -> lost
    rec("early", since="2026-08-01", first_seen="2026-08-01T08:00:00Z", gone_at="2026-08-21T08:00:00Z",
        last_seen="2026-08-21T08:00:00Z"),
    # 8 přestal chodit, nikdo neověřil -> lost
    rec("lost", since="2026-09-01", last_seen="2026-09-15T08:00:00Z"),
    # 9 chyba v datech: pronájem přehozený na prodej uprostřed cesty
    rec("swap", since="2026-09-01", price_history=[{"at": "2026-09-01", "price_czk": 20000},
                                                   {"at": "2026-09-10", "price_czk": 8_500_000},
                                                   {"at": "2026-09-12", "price_czk": 19000}]),
    # 10 bez plochy -> vynechán
    rec("nosqm", floor_area_sqm=None),
    # bezrealitky kotva pro baseline (první výskyt portálu v oblasti)
    rec("bzanchor", source="bezrealitky", first_seen="2026-07-05T08:00:00Z", last_seen="2026-07-06T08:00:00Z"),
]
live_ids = {"live1", "new", "base", "swap", "nosqm"}
archive = {"old": {"relisted_as": {"id": "new", "verdict": "same"}}}
rows, wards, skipped = T.build_rows(records, live_ids, archive, STATE, None, NOW)
by = {}
kept = [r for r in records if not r.get("gone_stale") and r.get("floor_area_sqm")]
for r, row in zip(kept, rows):
    by[r["id"]] = row
check("zombie counted as skipped", skipped["zombie"], 1)
check("no-sqm counted as skipped", skipped["no_sqm"], 1)
check("rows kept", len(rows), len(kept))

S = T.STATUS
check("live status", by["live1"][9], S["live"])
check("gone status", by["gone1"][9], S["gone"])
check("relisted predecessor status", by["old"][9], S["relisted"])
check("gone before tracking -> lost", by["early"][9], S["lost"])
check("unverified absence -> lost", by["lost"][9], S["lost"])
check("lost censored at last_seen", by["lost"][8], day("2026-09-15"))
check("live end = now", by["live1"][8], day(NOW))
check("start from since", by["live1"][5], day("2026-08-20"))
check("start known with since", by["live1"][6], 1)
check("relist successor inherits start", by["new"][5], day("2026-08-25"))
check("relist successor inherits obs_first", by["new"][7], day("2026-08-25"))
check("baseline without since -> unknown start", by["base"][6], 0)
check("seen arriving without since -> known", by["arr"][6], 1)
check("insane price point dropped", by["swap"][10], [day("2026-09-01"), 200, day("2026-09-12"), 190])

# ---- exits: jen ověřená, jen se známým začátkem, žádné živé
track = day(T.TRACK_FROM_DEFAULT["vysocany"])
ex = sorted(T.exits(rows, track))
check("exits = gone1 + arr only", ex, [(day("2026-09-11"), 10), (day("2026-09-20"), 10)])

# ---- survival: ruční výpočet
# jednotky (vstup, konec, událost): live1 (entry 12->?, ...). Stačí ověřit
# medián a počet událostí na malém čistém vzorku:
mini = [
    [0, 0, 1, 2, 50, 100, 1, 100, 110, S["gone"], [100, 250]],   # zmizel po 10
    [0, 0, 1, 2, 50, 100, 1, 100, 120, S["gone"], [100, 250]],   # po 20
    [0, 0, 1, 2, 50, 100, 1, 100, 130, S["live"], [100, 250]],   # živý 30 (cenzura)
    [0, 0, 1, 2, 50, 100, 1, 100, 140, S["gone"], [100, 250]],   # po 40
]
sv = T.survival(mini, 100, min_n=1)
curve = [(t, round(s, 4), r) for t, s, r in sv["curve"]]
check("KM curve", curve, [(0, 1.0, None), (10, 0.75, 4), (20, 0.5, 3), (40, 0.0, 1)])
check("KM median", sv["median"], 20)
check("KM events (live not an event)", sv["events"], 3)
check("KM stops under min_n", len(T.survival(mini, 100, min_n=3)["curve"]), 3)
# Zpožděný vstup: inzerát starý 50 dní při startu sledování se v riziku
# neobjeví dřív než ve věku 50.
late = [[0, 0, 1, 2, 50, 0, 1, 50, 60, S["gone"], [0, 250]]] + mini
sv2 = T.survival(late, 100, min_n=1)
check("delayed entry: not at risk before entry age", sv2["curve"][1], (10, 0.75, 4))
# Předchůdce znovuvložení do KM nevstupuje.
check("relisted not a KM unit", T.survival([[0, 0, 1, 2, 50, 100, 1, 100, 110, S["relisted"], [100, 1]]], 100, 1)["units"], 0)

# ---- týdenní mediány
w0 = T.week_start(day("2026-09-07"))  # pondělí 7. 9.
check("week_start is Monday", T.day_label(w0), "7. 9.")
wk_rows = [
    [0, 0, 1, 2, 50, w0, 1, w0, w0 + 20, S["live"], [w0, 200, w0 + 10, 180]],  # 20000 -> 18000
    [0, 0, 1, 2, 40, w0, 1, w0, w0 + 20, S["live"], [w0, 240]],
    [0, 0, 1, 2, 60, w0, 1, w0, w0 + 3, S["gone"], [w0, 300]],                # jen první týden
]
wk = T.weekly(wk_rows, [w0, w0 + 7], w0 + 20)
check("week1 n", wk[0]["n"], 3)
check("week1 median price", wk[0]["price_med"], 24000)
check("week1 median Kč/m²", wk[0]["sqm_med"], 500)
check("week2 n (gone one dropped)", wk[1]["n"], 2)
check("week2 uses end-of-week price", wk[1]["price_med"], (18000 + 24000) / 2)
check("quantile type 7", T.quantile([1, 2, 3, 4], 0.25), 1.75)

# ---- zlevnění (regrese: cena bodu je p[i+1], ne den)
cuts = T.cuts(wk_rows + [[0, 0, 1, 2, 50, w0, 1, w0, w0 + 20, S["live"], [w0, 200, w0 + 2, 50]]], [w0, w0 + 7])
check("cuts per week", [c["n"] for c in cuts], [0, 1])
check("cut depth", round(cuts[1]["med_pct"], 1), 10.0)

# ---- filtr
flt = {"area": 0, "tx": 1, "wards": None, "disps": {2}, "sqm_min": 45, "sqm_max": 55}
check("filter by sqm/disp", [T.matches(r, flt) for r in wk_rows], [True, False, False])
check("filter tx", T.matches(wk_rows[0], dict(flt, tx=0)), False)

# ---- stránka: nic nerozbije <script>, prázdný payload = žádná karta
check("no card without payload", T.card_html(None), "")
evil = {"rows": [], "wards": ["</script><img src=x onerror=alert(1)>"], "track_from": [None, None, None]}
safe = json.dumps(evil, ensure_ascii=False).replace("<", "\\u003c")
js = T.page_js(safe)
check("payload placeholder replaced", "__TL_JSON__" in js, False)
check("no raw </script> from data", "</script>" in js, False)
check("wards rendered via textContent, not innerHTML", "innerHTML" in T._JS, False)

# ---- novostavby jako oblast U Kříže
nov = T.novostavby_records([{"id": 1, "transaction_type": "prodej", "disposition": "4+kk",
                             "floor_area_sqm": 120, "price_czk": 20_000_000, "first_seen": NOW,
                             "since": "2026-07-28", "last_seen": NOW,
                             "price_history": [{"at": NOW, "price_czk": 20_000_000}]},
                            {"id": 2, "out_of_scope": True}])
check("novostavby mapped to ukrize", [(r["area"], r["id"]) for r in nov], [("ukrize", "nov-1")])
payload = T.build_payload([], {"generated_at": NOW, "comparables": [],
                               "novostavby": [{"id": 1, "transaction_type": "prodej", "disposition": "4+kk",
                                               "floor_area_sqm": 120, "price_czk": 20_000_000,
                                               "first_seen": NOW, "since": "2026-07-28", "last_seen": NOW,
                                               "price_history": [{"at": NOW, "price_czk": 20_000_000}]}],
                               "novostavby_meta": {"baseline_at": NOW}}, {}, {})
r0 = payload["rows"][0]
check("novostavba row: area, live, since start", (r0[0], r0[9], r0[5], r0[6]), (2, S["live"], day("2026-07-28"), 1))
check("U Kříže tracked from baseline", payload["track_from"][2], day(NOW))

if failures:
    print(f"\n{len(failures)} FAILED")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("\nALL PASS")
