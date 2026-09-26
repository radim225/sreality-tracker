#!/usr/bin/env python3
"""Dvě sledované oblasti (Vysočany + Jinonice, od 26. 9. 2026).

Hlídá hlavně to, co by se pokazilo potichu: Jinonice v mediánu Vysočan (a tím
v odhadu nájmu bytu Pod Harfou), přidání oblasti ohlášené jako stovky
„nových" inzerátů, a týdenní zápis označený jako „změnila se konfigurace",
přestože se na Vysočanech nic nezměnilo.

Run: python3 test_areas.py
"""
import json
import sys

import market
import pool
import scrape

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:58} {got!r}")


# --- kam co patří -------------------------------------------------------- #
check("Pod Harfou = Vysočany", scrape.area_of(50.10444, 14.50650), "vysocany")
check("Nové Butovice = Jinonice", scrape.area_of(50.0508, 14.3521), "jinonice")
check("Radlice = Jinonice", scrape.area_of(50.0577, 14.3887), "jinonice")
check("Anděl mimo obě", scrape.area_of(50.0710, 14.4030), None)
check("Černý Most mimo obě", scrape.area_of(50.1075, 14.5760), None)
check("bez GPS se nezahazuje", scrape.in_watched_area(None, None), True)

item = scrape.assign_area({"lat": 50.0508, "lon": 14.3521})
check("vzdálenost od středu vlastní oblasti", item["dist_km"] < 2, True)
check("bez GPS rozhodne čtvrť", scrape.assign_area({"city_part": "Košíře"})["area"], "jinonice")
check("bez GPS i čtvrti = domácí", scrape.assign_area({})["area"], "vysocany")
check("starý záznam bez area = Vysočany", scrape.listing_area({}), "vysocany")
check("popisky oblastí se shodují s pool.AREA_LABELS",
      sorted(pool.AREA_LABELS), sorted(scrape.AREAS))
check("čtvrti Jinonic jsou ve sweepu",
      all(w in scrape.SEARCH_WARDS for w in scrape.AREAS["jinonice"]["wards"]), True)

# --- fingerprint --------------------------------------------------------- #
old_cfg = {  # tvar fingerprintu před 26. 9.
    "center": [50.0995, 14.49], "radius_km": 3.0,
    "dispositions": sorted(scrape.DISPOSITION_CODES.values()),
    "wards": sorted(scrape.AREAS["vysocany"]["wards"]),
    "idnes_wards": sorted(scrape.sources.IDNES_WARDS_BY_AREA["vysocany"]),
}
new_cfg = json.loads(json.dumps(scrape.config_fingerprint()))
check("přidání Jinonic = jen nová oblast", scrape.areas_added_only(old_cfg, new_cfg), {"jinonice"})
check("stejná konfigurace = nic nového", scrape.areas_added_only(new_cfg, new_cfg), set())
check("změna dispozic není 'jen nová oblast'",
      scrape.areas_added_only(dict(old_cfg, dispositions=["1+kk"]), new_cfg), set())
check("změna poloměru Vysočan není 'jen nová oblast'",
      scrape.areas_added_only(dict(old_cfg, radius_km=2.0), new_cfg), set())
check("home_config vypadá jako fingerprint před 26. 9.",
      scrape.home_config(new_cfg), old_cfg)

# --- diff: Jinonice se při zapnutí neohlásí jako nové --------------------- #
prev = {"config": old_cfg, "comparables": [
    {"id": 1, "area": "vysocany", "price_czk": 20000, "total_czk": 20000}]}
curr = {"config": new_cfg, "comparables": [
    {"id": 1, "area": "vysocany", "price_czk": 19000, "total_czk": 19000},
    {"id": 2, "area": "vysocany", "price_czk": 18000, "total_czk": 18000},
    {"id": 3, "area": "jinonice", "price_czk": 25000, "total_czk": 25000}]}
changes = scrape.diff_snapshots(prev, curr)
check("nový byt na Vysočanech se ohlásí", [c["id"] for c in changes["new_listings"]], [2])
check("změna ceny na Vysočanech se ohlásí", [c["id"] for c in changes["price_changes"]], [1])
check("není to plný rebaseline", changes.get("config_changed"), None)
check("Jinonice označeny jako baseline", changes.get("baselined_areas"), ["jinonice"])

# --- statistiky se nemíchají --------------------------------------------- #
flats = []
for i, (area, v) in enumerate([("vysocany", 100)] * 5 + [("jinonice", 300)] * 5):
    flats.append({"id": i, "area": area, "transaction_type": "prodej", "disposition": "2+kk",
                  "price_czk_per_sqm": v + i})
scrape.rank_deals(flats)
check("výhodnost se měří proti mediánu vlastní oblasti",
      max(abs(f["deal_pct"]) for f in flats) < 5, True)

garages = [
    {"id": 1, "transaction_type": "pronajem", "price_czk": 2000, "garage_slug": "garaze"},
    {"id": 2, "transaction_type": "pronajem", "price_czk": 5000, "garage_slug": "garaze",
     "area": "jinonice"},
]
check("garážový medián domácí oblasti bez Jinonic",
      scrape.compute_garage_stats(garages)["pronajem"]["median_czk"], 2000)
check("garážový medián Jinonic",
      scrape.compute_garage_stats(garages, area="jinonice")["pronajem"]["median_czk"], 5000)

# --- pool: odhad a zápis čtou jen Vysočany ------------------------------- #
recs = {
    "1": {"id": "1", "last_seen": "2026-09-20T00:00:00Z", "first_seen": "2026-09-19T00:00:00Z"},
    "2": {"id": "2", "last_seen": "2026-09-20T00:00:00Z", "first_seen": "2026-09-19T00:00:00Z",
          "area": "jinonice"},
}
now = "2026-09-26T00:00:00Z"
check("okno poolu = jen Vysočany", [r["id"] for r in pool.window(recs, now=now)], ["1"])
check("okno Jinonic", [r["id"] for r in pool.window(recs, now=now, area="jinonice")], ["2"])
check("okno všech", len(pool.window(recs, now=now, area=None)), 2)
arrived, _ = market.period_movement(recs, "2026-09-18T00:00:00Z", now)
check("přírůstky v zápisu = jen Vysočany", [r["id"] for r in arrived], ["1"])

# --- kontakty z veřejného výstupu ---------------------------------------- #
items = [{"description": "Volejte 724 223 828 nebo pište na jan@firma.cz. Cena 8 990 000 Kč.",
          "seller_name": "RK Honzík honzik@honzik.cz"}]
scrape.scrub_contacts(items)
check("telefon pryč, cena zůstává",
      items[0]["description"], "Volejte [telefon skryt] nebo pište na [e-mail skryt]. Cena 8 990 000 Kč.")
check("e-mail ze jména kanceláře pryč", items[0]["seller_name"], "RK Honzík")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
