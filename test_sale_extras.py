#!/usr/bin/env python3
"""Tests for what a sale advert prices next to the flat, and for the three
things that went wrong on the one listing Radim actually watches.

The Pod Harfou flat (id 2864615500) is the whole reason this file exists. It is
a contract-assignment sale, and on the dashboard it read: transaction type
"Rent", price "Nájem (net) 6 750 000 Kč", area "—", no sign that it had dropped
from 6 950 000, and no mention of the 800 000 Kč garage space its own
description offers. Four separate defects, one advert.

Run: python3 test_sale_extras.py
"""
import sys

from scrape import (
    area_from_title,
    attach_sale_extras,
    backfill_missing_areas,
    build_tracked_item,
    flag_transaction_mismatch,
    parse_sale_extras,
    price_histories,
)

HARFA = (
    "Nabízíme k prodeji byt 1+kk o ploše 29,6 m2 s balkonem 4,6 m2 v pátém patře. "
    "Prodej probíhá formou postoupení smlouvy o budoucí kupní smlouvě u developera. "
    "Nyní hradíte pouze odstupné ve výši 1 948 080,- zbytek kupní ceny je možné "
    "hradit prostřednictvím hypotečního úvěru až po dokončení domu. "
    "K bytu je možné přikoupit nadrozměrné garážové stání včetně komory přístupné "
    "z tohoto stání o výměře 2 m2 a to za 800 000,- Kč."
)

# (label, description, price_czk, expected [(label, amounts)])
EXTRA_CASES = [
    ("Harfa: garage+box and the assignment fee", HARFA, 6_750_000, [
        ("odstupné (splatné nyní)", [1_948_080]),
        ("garážové stání + komora", [800_000]),
    ]),
    # "Garážové stání" contains "stání", so without the de-duplication rule
    # every garage row would read "garážové stání + parkovací stání".
    ("garage does not also claim the parking label",
     "K bytu se navíc dokupuje vyhrazené garážové stání za 700 000 Kč.", 11_790_000,
     [("garážové stání", [700_000])]),
    ("a bare list line still counts",
     "Sklep: 170 000 Kč", 18_500_000, [("sklep", [170_000])]),
    ("cellar sold by the m² is not a cellar price",
     "K bytu je třeba dokoupit sklep (cena 67 200 Kč/m2).", 19_373_005, []),
    # The advert's own per-m² price, mentioning both words in passing.
    ("the flat's unit price is not an extra",
     "Cena za m2 užitné plochy vychází bez garáže a sklepu na 184.000,-kč.",
     8_499_000, []),
    # Which number belongs to which extra is exactly the guess the fee parser
    # was taught to stop making, so all of them are kept and the row says so.
    ("two extras in one sentence keep both amounts",
     "K jednotce lze dokoupit sklep o velikosti 2,6 m2 za 199.000 Kč a podzemní "
     "garážové stání za cenu 550.000 Kč.", 8_250_000,
     [("garážové stání + sklep", [199_000, 550_000])]),
    ("the flat's own price is never an extra",
     "Garážové stání je v ceně 6 750 000 Kč.", 6_750_000, []),
    ("no amount, no row",
     "K bytu náleží garážové stání a sklep.", 9_000_000, []),
    ("a monthly garage rent is far below the floor",
     "Garážové stání 2.500 Kč měsíčně.", 9_000_000, []),
]

TITLE_CASES = [
    ("Prodej bytu 1+kk 30 m²", 30.0),
    ("Pronájem bytu 2+kk 34 m2", 34.0),
    ("Prodej bytu 3+1 78,5 m²", 78.5),
    ("Prodej bytu 1+kk", None),
]

TX_CASES = [
    ("a 6.75M rent is a sale filed wrong",
     {"transaction_type": "pronajem", "price_czk": 6_750_000}, True),
    ("a 350k sale is a rent filed wrong",
     {"transaction_type": "prodej", "price_czk": 120_000}, True),
    ("an ordinary rent is left alone",
     {"transaction_type": "pronajem", "price_czk": 21_900}, False),
    ("an ordinary sale is left alone",
     {"transaction_type": "prodej", "price_czk": 6_750_000}, False),
    ("no price, no verdict",
     {"transaction_type": "prodej", "price_czk": None}, False),
]


def main():
    failures = []

    for label, text, price, want in EXTRA_CASES:
        got = [(e["label"], e["amounts"]) for e in parse_sale_extras(text, price)]
        ok = got == want
        if not ok:
            failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"{'PASS' if ok else 'FAIL'}  {label:48} {got!r}")

    # The sentence has to travel with the number: the row is a hover away from
    # the advert's own words, which is the only way to check it without leaving
    # the page.
    extras = parse_sale_extras(HARFA, 6_750_000)
    ok = all("Kč" in e["text"] or "," in e["text"] for e in extras) and \
        "přikoupit" in [e for e in extras if e["kind"] == "extra"][0]["text"]
    if not ok:
        failures.append("the advert's sentence is not carried on the row")
    print(f"{'PASS' if ok else 'FAIL'}  {'each row carries the advert sentence':48} {ok}")

    # A rental's garage is priced by the month and belongs to parking_state.
    rentals = [{"transaction_type": "pronajem", "price_czk": 21_900, "description": HARFA}]
    attach_sale_extras(rentals)
    ok = rentals[0]["sale_extras"] == []
    if not ok:
        failures.append("a rental was given sale extras")
    print(f"{'PASS' if ok else 'FAIL'}  {'rentals get no sale extras':48} {ok}")

    for title, want in TITLE_CASES:
        got = area_from_title(title)
        ok = got == want
        if not ok:
            failures.append(f"area_from_title({title!r}): got {got!r}, want {want!r}")
        print(f"{'PASS' if ok else 'FAIL'}  {('area from ' + repr(title))[:48]:48} {got!r}")

    # The whole point: with no floorArea the flat had no Kč/m² and could not be
    # compared with anything on its own street.
    listings = [{
        "id": 2864615500, "title": "Prodej bytu 1+kk 30 m²",
        "transaction_type": "prodej", "price_czk": 6_750_000, "floor_area_sqm": None,
    }]
    backfill_missing_areas(listings)
    ok = (listings[0]["floor_area_sqm"] == 30.0
          and listings[0]["floor_area_source"] == "title"
          and listings[0]["price_czk_per_sqm"] == 225_000)
    if not ok:
        failures.append(f"backfill did not fill area and Kč/m²: {listings[0]!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {'missing area comes from the title':48} {ok}")

    measured = [{"title": "Prodej bytu 1+kk 30 m²", "floor_area_sqm": 29.6}]
    backfill_missing_areas(measured)
    ok = measured[0]["floor_area_sqm"] == 29.6 and measured[0]["floor_area_source"] == "field"
    if not ok:
        failures.append("the title overwrote a measured area")
    print(f"{'PASS' if ok else 'FAIL'}  {'a stated area is never overwritten':48} {ok}")

    for label, listing, want_flag in TX_CASES:
        flag_transaction_mismatch([listing])
        got = bool(listing["tx_suspect"])
        ok = got == want_flag
        if not ok:
            failures.append(f"{label}: got {listing['tx_suspect']!r}")
        if want_flag and not listing.get("exclude_from_stats"):
            ok = False
            failures.append(f"{label}: flagged but still in the statistics")
        print(f"{'PASS' if ok else 'FAIL'}  {label:48} {listing['tx_suspect']!r}")

    # The defect that started all of this: the dashboard row was built with
    # "pronajem" hard-coded, so a sale was priced, labelled and totalled as rent.
    item = build_tracked_item(
        {"id": 1, "transaction_type": "prodej", "rent_czk": 6_750_000,
         "title": "Prodej bytu 1+kk 30 m²"},
        {"tracked_price_changes": [], "price_changes": [], "new_listings": []},
    )
    ok = item["transaction_type"] == "prodej"
    if not ok:
        failures.append(f"tracked sale still rendered as {item['transaction_type']!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {'a tracked sale stays a sale':48} {item['transaction_type']!r}")

    # One price is not a history; the first point survives truncation, because
    # "what did it originally ask?" is the question the badge answers.
    long_path = [{"at": f"2026-01-{d:02d}", "price_czk": 9_000_000 - d} for d in range(1, 21)]
    got = price_histories({
        "a": {"id": "a", "price_history": [{"at": "2026-06-28", "price_czk": 6_950_000},
                                           {"at": "2026-09-02", "price_czk": 6_750_000}]},
        "b": {"id": "b", "price_history": [{"at": "2026-06-28", "price_czk": 6_950_000}]},
        "c": {"id": "c", "price_history": []},
        "d": {"id": "d", "price_history": long_path},
    })
    ok = (set(got) == {"a", "d"}
          and len(got["a"]) == 2
          and len(got["d"]) == 12
          and got["d"][0] == {"at": "2026-01-01", "price_czk": 8_999_999}
          and got["d"][-1]["at"] == "2026-01-20")
    if not ok:
        failures.append(f"price_histories: {got!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {'only real paths, first point kept':48} {ok}")

    # A cut middle must not render as one ordinary step under a caption that
    # promises every move is listed.
    ok = got["d"][1].get("after_gap") is True and not any(
        p.get("after_gap") for p in got["a"]
    )
    if not ok:
        failures.append(f"the truncation is not marked: {got['d'][:2]!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {'a truncated path says so':48} {ok}")

    # An override is Radim's number. Left carrying the backfill's source, the
    # page would tell him his own correction came off the advert's title.
    from scrape import apply_overrides
    corrected = [{"id": 7, "title": "Prodej bytu 1+kk 30 m²", "transaction_type": "prodej",
                  "price_czk": 6_750_000, "floor_area_sqm": None}]
    backfill_missing_areas(corrected)
    apply_overrides(corrected, {"7": {"id": "7", "floor_area_sqm": 29.6}})
    ok = (corrected[0]["floor_area_sqm"] == 29.6
          and corrected[0]["floor_area_source"] == "override")
    if not ok:
        failures.append(f"override area still credited to {corrected[0].get('floor_area_source')!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {'an override is not credited to the title':48} {ok}")

    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("all sale-extras cases pass")


if __name__ == "__main__":
    main()
