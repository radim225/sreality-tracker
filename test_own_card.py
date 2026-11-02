#!/usr/bin/env python3
"""Testy karty „Tvůj byt" a karty s frontou poplatků.

Karta drží osobní čísla a fronta zobrazuje cizí text z inzerátů na
veřejné stránce. Obojí je místo, kde tichá chyba stojí buď špatné rozhodnutí,
nebo díru v escapování.

Čísla tady jsou syntetická. Reálné částky sem nepatří: repo je veřejné a
raw.githubusercontent.com servíruje git, i když Pages testy vynechá.

Spuštění: python3 test_own_card.py
"""
import os
import sys

# Jediný zdroj částek. Asserty níž jsou vztahy mezi nimi, ať změna konstanty
# nerozbije test a ať tu nezůstane natvrdo opsané číslo.
PRICE = 4_200_000
EXTRA = {"garaz": 350_000, "komora": 90_000}
DEPOSITS = 800_000

# Konstanty se čtou při importu scrape, ne při volání. Přiřazení, ne
# setdefault: shell s reálným OWN_* by jinak test načetl osobní číslo.
os.environ["OWN_PRICE_CZK"] = str(PRICE)
os.environ["OWN_EXTRA_PRICES_CZK"] = ",".join(
    f"{name}={amount}" for name, amount in EXTRA.items()
)
os.environ["OWN_DEPOSITS_CZK"] = str(DEPOSITS)

import scrape  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:44} {got!r}")


# --- rozpad na jednotky ------------------------------------------------- #
units, total = scrape.own_units()
expected_total = PRICE + sum(EXTRA.values())
check("celková cena všech tří jednotek", total, expected_total)
check("jednotky v pořadí byt, garáž, komora", [u for u, _ in units], ["byt", "garaz", "komora"])

# --- chybějící vlastní kapitál ------------------------------------------ #
gap = scrape.own_equity_gap()
ltv = scrape.OWN_LTV_PCT
check("zálohy jsou menší než cena bytu", DEPOSITS < PRICE, True)
check("banka půjčí při nastaveném LTV", gap["bank_lends_czk"], round(total * ltv / 100))
check("vlastních je potřeba", gap["own_needed_czk"], round(total * (1 - ltv / 100)))
check("chybí doplnit", gap["gap_czk"], gap["own_needed_czk"] - DEPOSITS)

# LTV je knoflík, ne konstanta -- při nižším LTV chybí víc.
#
# Mezera je pořád (potřebné vlastní − zaplacené zálohy). Finep appka k tomu
# samému dochází jako (Σ doplatků − strop banky). Dvě nezávislé definice,
# stejný vztah -- kdyby se rozešly, jedna z appek počítá špatně.
scrape.OWN_LTV_PCT = 70
gap70 = scrape.own_equity_gap()
check("při 70 % LTV chybí víc", gap70["gap_czk"] > gap["gap_czk"], True)
check("při 70 % pořád platí mezera", gap70["gap_czk"], gap70["own_needed_czk"] - DEPOSITS)
scrape.OWN_LTV_PCT = 80

# --- hrubý výnos --------------------------------------------------------- #
estimate = {"profiles": {"nezarizeny": {"rent": {"median": 18000}}}}
garage_rent = 2000
y = scrape.own_gross_yield(estimate, garage_rent)
check("výnos bytu", y["rows"][0]["yield_pct"], round(18000 * 12 / PRICE * 100, 2))
check("výnos garáže", y["rows"][1]["yield_pct"], round(garage_rent * 12 / EXTRA["garaz"] * 100, 2))
check("výnos celku počítá s celou cenou", y["total"]["price_czk"], expected_total)
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

# --- degradace: chybějící secrety nesmí nic shodit ---------------------- #
# Karta drží osobní čísla, která nemusí být nastavená. Chybějící řádek je
# správná odpověď; pád scrapu kvůli nenastavenému secretu by nebyl.
saved = (scrape.OWN_EXTRA_PRICES, scrape.OWN_DEPOSITS_CZK)
scrape.OWN_EXTRA_PRICES, scrape.OWN_DEPOSITS_CZK = {}, None
check("bez rozpadu jen byt", scrape.own_units()[1], PRICE)
check("bez záloh žádný chybějící kapitál", scrape.own_equity_gap(), None)
scrape.OWN_EXTRA_PRICES, scrape.OWN_DEPOSITS_CZK = saved

# --- veřejný dashboard vs. soukromý renderer ---------------------------- #
# Helper pořád umí kartu (lokální / testy). Do Pages HTML nesmí.
own = {
    "own_czk_per_sqm": 140_000, "band_median": 200000, "band_count": 10,
    "disposition_median": 190000, "disposition_count": 20,
    "p10": 150000, "p90": 250000, "low": 140000, "high": 300000, "cheaper_pct": 40,
}
private_html = scrape.render_own_property_card(own)
check("soukromý renderer pořád emituje ownCard", "id=\"ownCard\"" in private_html, True)
try:
    scrape.reject_own_finance_in_public_html(private_html)
    failures.append("tripwire nepustil ownCard")
    print("FAIL  tripwire chytí ownCard")
except RuntimeError:
    check("tripwire chytí ownCard", True, True)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
