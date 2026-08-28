#!/usr/bin/env python3
"""Testy karty „Tvůj byt" a karty s frontou poplatků.

Karta drží Radimova osobní čísla a fronta zobrazuje cizí text z inzerátů na
veřejné stránce. Obojí je místo, kde tichá chyba stojí buď špatné rozhodnutí,
nebo díru v escapování.

Spuštění: python3 test_own_card.py
"""
import os
import sys

# Konfigurace musí být v prostředí DŘÍV, než se scrape naimportuje: konstanty
# se čtou při importu modulu, ne při volání.
os.environ.setdefault("OWN_PRICE_CZK", "6556088")
os.environ.setdefault("OWN_EXTRA_PRICES_CZK", "garaz=500000,komora=110110")
os.environ.setdefault("OWN_DEPOSITS_CZK", "1294929")

import scrape  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:44} {got!r}")


# --- rozpad na jednotky ------------------------------------------------- #
units, total = scrape.own_units()
check("celková cena všech tří jednotek", total, 7166198)
check("jednotky v pořadí byt, garáž, komora", [u for u, _ in units], ["byt", "garaz", "komora"])

# --- chybějící vlastní kapitál ------------------------------------------ #
gap = scrape.own_equity_gap()
check("banka půjčí při 80 % LTV", gap["bank_lends_czk"], 5732958)
check("vlastních je potřeba", gap["own_needed_czk"], 1433240)
check("chybí doplnit", gap["gap_czk"], 138311)

# LTV je knoflík, ne konstanta -- při nižším LTV chybí víc.
#
# 854 930 je zároveň křížová kontrola proti Finep appce, která k témuž číslu
# jde jinou cestou: tam je to (Σ doplatků − strop banky), tady
# (potřebné vlastní − zaplacené zálohy). Dvě nezávislé definice, stejný
# výsledek -- kdyby se rozešly, jedna z appek počítá špatně.
scrape.OWN_LTV_PCT = 70
check("při 70 % LTV chybí víc", scrape.own_equity_gap()["gap_czk"], 854930)
scrape.OWN_LTV_PCT = 80

# --- hrubý výnos --------------------------------------------------------- #
estimate = {"profiles": {"nezarizeny": {"rent": {"median": 18000}}}}
y = scrape.own_gross_yield(estimate, 2000)
check("výnos bytu", y["rows"][0]["yield_pct"], round(18000 * 12 / 6556088 * 100, 2))
check("výnos garáže", y["rows"][1]["yield_pct"], round(2000 * 12 / 500000 * 100, 2))
check("výnos celku počítá s celou cenou", y["total"]["price_czk"], 7166198)
check("komora je označená jako neoceněná", y["storage_unpriced"], True)

# Bez odhadu nájmu nesmí vzniknout číslo z ničeho.
check("bez odhadu nájmu žádný výnos", scrape.own_gross_yield(None, 2000), None)
check("bez mediánu garáže jen byt",
      len(scrape.own_gross_yield(estimate, None)["rows"]), 1)

# --- fronta: escapování cizího textu ------------------------------------ #
# Text inzerátu píše cizí člověk a jde na veřejnou stránku. Tenhle projekt už
# jednou platil za neescapované generované HTML.
evil = '<script>alert("xss")</script> & "uvozovky"'
card = scrape.fee_queue_card([{
    "id": 1, "url": "https://example.com/a?x=1&y=2", "title": evil,
    "why": ["lookahead"], "rent_czk": 20000, "would_have_said": 3000,
    "cost_of_living_raw": None, "price_note_raw": evil, "description": None,
}])
check("skript v titulku neprojde", "<script>alert" in card, False)
check("titulek je vidět escapovaný", "&lt;script&gt;" in card, True)
check("ampersand v URL escapován", "x=1&amp;y=2" in card, True)

# --- fronta: prázdná se nevykresluje ------------------------------------ #
check("prázdná fronta nedělá kartu", scrape.fee_queue_card([]), "")

# --- fronta: řazení po důvodech ----------------------------------------- #
card = scrape.fee_queue_card([
    {"id": 1, "url": "u", "title": "a", "why": ["lookahead"], "rent_czk": 1,
     "would_have_said": 1, "description": "x", "cost_of_living_raw": None, "price_note_raw": None},
    {"id": 2, "url": "u", "title": "b", "why": ["person_tier"], "rent_czk": 1,
     "would_have_said": 1, "description": "x", "cost_of_living_raw": None, "price_note_raw": None},
    {"id": 3, "url": "u", "title": "c", "why": ["lookahead"], "rent_czk": 1,
     "would_have_said": 1, "description": "x", "cost_of_living_raw": None, "price_note_raw": None},
])
check("nejčastější důvod je první", card.index("lookahead") < card.index("person_tier"), True)
check("počet u důvodu sedí", "(2×)" in card, True)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
