#!/usr/bin/env python3
"""Garáže: charakteristika z popisu, historie zmizelých, izolace od bytů.

Radim si na dashboardu otevřel dvě garážová stání a obě daly 404, a u zbytku
nepoznal, co vlastně jsou -- 245 tisíc a 3 miliony vedle sebe bez vysvětlení.
Tyhle testy hlídají obojí.

Run: python3 test_garages.py
"""
import sys

import scrape

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:46} {got!r}")


# --- charakteristika z popisu ------------------------------------------- #
# Všechny texty jsou zkrácené kusy skutečných inzerátů z okruhu.
check("podlaží se čte i bez slova podzemní",
      scrape.garage_features("v podzemní garáži v -3. podlaží, plocha 10,3 m2"),
      ["-3. podlaží"])
check("1. PP", scrape.garage_features("stání umístěné v 1. PP moderní novostavby"), ["1. PP"])
check("1. podzemní podlaží se normalizuje na PP",
      scrape.garage_features("garáž v 1. podzemním podlaží bytového domu")[0], "1. PP")
check("parklift", scrape.garage_features("stání je v sytému parklift"), ["parklift / plošina"])

# Stání jen pro motorku stojí zlomek ceny a v mediánu vypadá jako výhodná
# koupě -- tohle je hlavní důvod, proč se popis vůbec čte.
check("jen motorka varuje",
      "⚠ jen pro motocykl" in scrape.garage_features("Parkovací místo pro motocykl v garáži"), True)
# ...ale garáž, kam se vejde auto I motorka, je normální garáž.
check("auto i motorka nevaruje",
      scrape.garage_features("parkování pro Váš vůz, motorku nebo uskladnění"), [])

# Konkrétnější štítek vyhrává, aby si dvojice neodporovaly.
check("řadová vytlačí samostatnou",
      scrape.garage_features("samostatnou družstevní řadovou garáž v oploceném areálu"),
      ["uzavřené parkoviště", "řadová garáž", "družstevní"])
check("číslo podlaží vytlačí obecné podzemí",
      scrape.garage_features("v podzemní garáži, 1. PP"), ["1. PP"])

# Vymyslet charakteristiku je horší než ji neuvést.
check("nic rozpoznatelného = prázdné",
      scrape.garage_features("Nabízíme k pronájmu stání na adrese Poděbradská 1197/60"), [])
check("prázdný popis", scrape.garage_features(None), [])

# --- historie: zmizelé nepočítat do statistik --------------------------- #
sample = [
    {"id": 1, "transaction_type": "prodej", "price_czk": 500000, "garage_slug": "garaze"},
    {"id": 2, "transaction_type": "prodej", "price_czk": 900000, "garage_slug": "garaze"},
    # Zmizelý inzerát: v historii ano, v mediánu ne. Jinak by se dnešní ceny
    # mísily s tím, co bylo v nabídce před měsícem.
    {"id": 3, "transaction_type": "prodej", "price_czk": 3000000, "garage_slug": "garaze",
     "gone_at": "2026-08-20T00:00:00Z"},
]
check("aktivní vylučuje zmizelé", [g["id"] for g in scrape.active_garages(sample)], [1, 2])
stats = scrape.compute_garage_stats(sample)
check("medián počítá jen z aktivních", stats["prodej"]["n"], 2)
check("zmizelý netáhne medián nahoru", stats["prodej"]["median_czk"], 700000)

# --- URL: slug pro detail je jiný než pro hledání ------------------------ #
def url_for(slug):
    return scrape.parse_garage(
        {"id": 42, "name": "x", "locality": {}, "images": [], "categorySubCb": {}},
        "prodej", slug)["url"]

check("garážové stání má detailní slug", "garazove-stani" in url_for("garazova-stani"), True)
check("hledací slug se do detailu nedostane", "garazova-stani" in url_for("garazova-stani"), False)
check("garáž má slug garaz", url_for("garaze").endswith("/ostatni/garaz/x/42"), True)

# --- karta: historie se ukáže a nelže o významu -------------------------- #
base = dict(garage_kind="Garáž", garage_slug="garaze", transaction_type="prodej",
            price_czk=600000, first_seen="2026-08-01T00:00:00Z", street="Budilova",
            city_part="Libeň", usable_area_sqm=13.0, features=[], url="https://x")
card = scrape.garage_card(
    [dict(base, id=1, gone_at=None),
     dict(base, id=2, gone_at="2026-08-28T10:00:00Z", street="Jandova")],
    {"pronajem": {"n": 0}, "prodej": {"n": 1, "median_czk": 600000,
                                      "min_czk": 600000, "max_czk": 600000}})
check("zmizelý je v kartě", "Jandova" in card, True)
check("s datem zmizení", "2026-08-28" in card, True)
check("a s datem prvního výskytu", "2026-08-01" in card, True)
# "Zmizel" není totéž co "prodal se" -- N-7, stejná lekce jako u bytů.
check("karta neříká, že se prodal", "Neznamená to, že se prodal" in card, True)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
