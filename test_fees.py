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
    # The clause splitter must NOT break on an abbreviation followed by a
    # lowercase word. Its own pattern says so, but re.I used to cancel that
    # rule -- with the flag on, [a-zá-ž] and [A-ZÁ-Ž] both match either case.
    # "vč. TV a internetu" split in two, which never changed this fee but made
    # it look like it came from the next clause.
    ("abbreviation inside a fee clause", None,
     "Poplatky za společné služby vč. TV a internetu a záloh na energie 6.500 Kč/měs.",
     6500, None),
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

    # --- optional extras are not the service charge ---------------------- #
    # A garage/parking space offered "za příplatek" is a separate thing the
    # tenant may or may not take. Booking it as the monthly fee understates the
    # real fee and, worse, lands in the all-in total the estimate compares on.
    # The description path already ignored these (no fee keyword, and it
    # requires one); costOfLiving does not require a keyword, so that is where
    # they got in.
    ("garage alone in field", "Garážové stání za příplatek 2.500 Kč", None, None, None),
    # Order is what made this bite: the garage clause comes first, claims the
    # fee slot, and the real fee two clauses later can no longer overwrite it.
    ("garage before the fee", "Garážové stání 2.500 Kč, poplatky 3.500 Kč", None, 3500, None),
    ("parking before the fee", "Parkovací stání 1.800 Kč, zálohy 4.000 Kč", None, 4000, None),
    ("cellar before the fee", "Sklep 500 Kč, poplatky 3.200 Kč", None, 3200, None),
    # The keyword clause has no amount of its own and looks ahead one clause --
    # which must not be allowed to land on an extra either.
    ("lookahead onto a garage", "Poplatky za služby, garážové stání 2.500 Kč",
     None, None, None),
    ("garage in description", None,
     "Možnost pronájmu garážového stání za 2.500 Kč měsíčně.", None, None),
    # The mirror image, and the reason the rule is confined to costOfLiving: in
    # a description these words describe the flat, and the amount next to them
    # is the real fee. Measured on live adverts, this shape is far commoner
    # than a priced extra written into prose.
    ("cellar named in a fee lookahead", None,
     "Poplatky za služby, sklep a úklid činí 4.700 Kč", 4700, None),
    ("cellar as an amenity, fee elsewhere", None,
     "K bytu náleží také sklep o velikosti 2 m². Poplatky za služby 3.900 Kč", 3900, None),
    # Must NOT regress: the extra is named INSIDE a real fee clause, so the
    # clause still carries the fee. Skipping it here would lose a good answer.
    ("cellar inside the fee clause", None,
     "Poplatky za služby včetně sklepa 3.500 Kč", 3500, None),
    ("garage included in the fee", "Poplatky včetně garážového stání 4.200 Kč", None, 4200, None),
    # Live shape, eight adverts of one agency template. The sentence ends in a
    # parenthesised amount, so the old letters-only lookbehind never split it;
    # the clause then carried a fee keyword and two amounts, and the multi-tier
    # rule picked the cheaper one -- which was the parking, not the fee.
    ("parenthesised parking price, then the fee", None,
     "K bytu náleží sklep a parkovací stání v podzemní garáži (2 500 Kč). "
     "Poplatky cca 3 500 Kč měsíčně, elektřina se převádí na nájemce.", 3500, None),
    # The same split must not fire on an abbreviation, which is what the
    # letters-only lookbehind was protecting.
    ("abbreviation is still not a sentence end", None,
     "Poplatky 3.500 Kč vč. vody a tepla", 3500, None),
    # A digit ending a sentence splits too -- previously it did not.
    ("sentence ending in a bare number", None,
     "Parkovací stání stojí 2.000. Poplatky za služby činí 4.100 Kč.", 4100, None),

    # --- found by measuring against labelled adverts, not by reading code --- #
    # "záloha" says a payment is an advance, not what for. A clause whose only
    # fee word is that generic one, next to an explicit "energie", is about
    # electricity and must not claim the fee slot.
    ("generic záloha next to energie is electricity", None,
     "Nájem 18.000 Kč, poplatky 3.200 Kč, záloha na energie 1.500 Kč", 3200, None),
    # Both costs in ONE clause, no persons mentioned: the cheapest-wins rule was
    # written for tier tables and has no business here. The amount nearest a
    # specific fee word wins instead.
    ("two different costs, one clause", None,
     "Poplatky jsou 4750 Kč a zálohy na energie 1150 kč.", 4750, None),
    # ...but a real tier table must still resolve to the single occupant.
    ("tier table still takes the cheapest", None,
     "Záloha na služby 2.500 Kč pro 1 osobu, 3.500 Kč pro 2 osoby", 2500, None),
    # "paušál" is how a third of agencies write the service charge.
    ("paušál is a fee word", None,
     "Celková cena: 22.000,-Kč + 3.500,-Kč (paušál) + 2.200,-Kč (záloha na energie)",
     3500, 22000),
    # An all-in total that is NOT the advertised rent says nothing about the
    # advertised rent. Reading it as "fees included" made the services free.
    ("all-in total that is not the rent", None,
     "Celková cena včetně paušálních poplatků a internetu je 48.000 Kč.", None, 34000),
    ("all-in total that IS the rent", None,
     "Celková cena včetně paušálních poplatků a internetu je 34.000 Kč.", 0, 34000),
    # Unchanged when the rent is unknown -- there is nothing to compare against.
    ("all-inclusive without a known rent", None,
     "Celková cena za užívání včetně poplatků je 35.000,- Kč/měsíc.", 0, None),
]


def main():
    failures = []
    for label, col, desc, want, rent in CASES:
        fee, source, _electricity, unsure = parse(col, desc, rent)
        status = "PASS" if fee == want else "FAIL"
        if fee != want:
            failures.append(f"{label}: got {fee!r} (source {source!r}), want {want!r}")
        flag = f" unsure={','.join(unsure)}" if unsure else ""
        print(f"{status}  {label:26} fee={fee!r:>7} source={source!r}{flag}")
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
