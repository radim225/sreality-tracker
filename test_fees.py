#!/usr/bin/env python3
"""Regression tests for the monthly-fee parser.

Every case here is a shape that actually appeared in a live advert. The fee is
the number Radim reads first, and the failures are silent -- a mis-parse doesn't
raise, it just quietly shows the rent (or a deposit) as the service charge and
skews the all-in Kč/m² the whole dashboard ranks on. So the parser gets tests
even though nothing else in the repo does.

Run: python3 test_fees.py
"""
import sys

from scrape import extract_fees_and_electricity as parse

# (label, costOfLiving field, description, expected fee, rent if known)
CASES = [
    # --- formats that used to fail -------------------------------------- #
    # Pipe-separated agency one-liner. "Jistina" is a deposit synonym; without
    # it in the exclusion list the deposit was read as the fee.
    ("iDNES pipe list", None,
     "Nájem 17.500,- | Poplatky 3.500,- | Jistina 21.000,- | Provize 15.000,-", 3500, None),
    ("iDNES plus form", None,
     "+ 5.500 Kč paušální poplatky + převod elektřiny. Kauce ve výši 45.000 Kč "
     "včetně provize RK", 5500, None),
    # A markdown bullet list where one clause mentioned "Kauce", which used to
    # void the whole clause and with it the fee. Multi-person tiers resolve to
    # the cheapest (single occupant).
    ("markdown tiers + kauce", None,
     "###  Nájemné a poplatky  * **Nájemné: 19 900 Kč / měsíc** * Záloha na služby:   "
     "* **2 500 Kč / měsíc pro 1 osobu**   * **3 500 Kč / měsíc pro 2 osoby**  "
     "**Celkové měsíční náklady:** * 1 osoba: **22 400 Kč** **Kauce:** 22 400 Kč", 2500, None),
    # The sentence comma splits the keyword away from its amount, so the fee
    # clause has to look ahead one clause.
    ("comma split lookahead", None,
     "k němuž je nutné připočítat zúčtovatelnou zálohu na služby, která činí 2.500 Kč "
     "při jedné osobě, 3.500 Kč při dvou osobách", 2500, None),

    # --- formats that already worked, kept so they keep working ---------- #
    ("clean int field", "3400", None, 3400, None),
    ("field phrase", "+ poplatky 3.400 Kč + el. energie + vratná kauce + provize RK",
     None, 3400, None),
    # Portals inject zero-width joiners / nbsp into price markup.
    ("nbsp/zwj digits", None, "Poplatky 3‍ 500‍ Kč měsíčně", 3500, None),

    # --- things that must NOT be read as a fee --------------------------- #
    ("deposit only", None, "Kauce ve výši 45.000 Kč, provize RK 20.000 Kč", None, None),
    ("rent-sized number", None, "Poplatky za služby: 19 900 Kč", None, None),
    ("no fee info", None, "Byt 2+kk o výměře 53 m² ve 4. patře, rok výstavby 2018.", None, None),
    ("service word, no amount", None,
     "V bezprostředním okolí se nachází OC Galerie Harfa a další obchody, služby, "
     "restaurace", None, None),
    # The rent is routinely restated inside the very field that carries the fee,
    # and on a cheap flat it is small enough to pass for a service charge.
    ("cheap rent in pipe note", "Nájem 12.500,- | Poplatky 3.500,-", None, 3500, 12500),
    ("rent restated, no fee", "Nájem 12.500,-", None, None, 12500),
    ("rent word alone", None, "Měsíční nájemné je 13.000 Kč.", None, 13000),
    ("fee and rent together", "Poplatky 2.800 Kč, nájemné 9.900 Kč", None, 2800, 9900),

    # --- "the rent is already all-in" is an answer, not a gap ------------ #
    ("all-inclusive", None,
     "Celková cena za užívaní včetně paušálních poplatků, energií a internetu "
     "je 35.000,- Kč/měsíc.", 0, None),
]


def main():
    failures = []
    for label, col, desc, want, rent in CASES:
        fee, source, _electricity = parse(col, desc, rent)
        status = "PASS" if fee == want else "FAIL"
        if fee != want:
            failures.append(f"{label}: got {fee!r} (source {source!r}), want {want!r}")
        print(f"{status}  {label:26} fee={fee!r:>7} source={source!r}")
    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print("  " + f)
        return 1
    print(f"all {len(CASES)} cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
