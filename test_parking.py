#!/usr/bin/env python3
"""Tests for the three parking states.

Radim's rule: an advert that HAS a garage but does not price it is not a free
garage. The quoted rent most likely already covers it and the advert does not
say, so it contributes nothing to what a space costs. Booking those as zero
would have invented ~135 free garages and pulled the median down -- which is
the entire reason this is three states and not two.

Run: python3 test_parking.py
"""
import sys

from scrape import PARKING_STATES, find_parking_price, parking_price_stats, parking_state

PRICE_CASES = [
    # --- the amount is the space's -------------------------------------- #
    ("priced in the note", "Garážové stání 2.500 Kč/měs", 2500),
    ("possibility of renting", "možnost pronájmu garáže za 2.000 Kč měsíčně", 2000),
    ("bare name, price next clause", "Parkovací stání | 1.800 Kč", 1800),

    # --- the amount belongs to something else --------------------------- #
    # The service charge standing nearer the fee word than the garage word.
    # This is the mis-read that cost eight adverts of one agency template
    # 1 000 Kč each on their all-in total.
    ("fee wins when it is nearer", "Garáž v domě, poplatky za služby 3.500 Kč", None),
    ("rent is not the parking price", "Nájem 17.500 Kč, garáž v ceně", None),
    # A year is not a price. Before dates were stripped this produced parking
    # "prices" as high as 19 000 Kč.
    ("year is not a price", "Garážové stání, kolaudace 2024", None),
    ("date is not a price", "Garáž k dispozici od 1. 9. 2026", None),
    # An inclusive phrase prices nothing -- it is the unpriced case.
    ("included prices nothing", "Nájem včetně garážového stání", None),
    # ... but a negated one is not inclusive, and still names no price.
    ("negation is not inclusion", "Parkovací stání není zahrnuto v ceně nájmu", None),
]

STATE_CASES = [
    ("no garage at all", {"garage": False, "parking": False}, "none", None),
    ("garage, no price stated", {"garage": True, "description": "K bytu náleží garáž."},
     "unpriced", None),
    ("garage with a price", {"garage": True, "description": "Garážové stání 2.500 Kč/měs"},
     "priced", 2500),
    ("parking flag alone counts", {"parking": True, "description": "Parkování v domě."},
     "unpriced", None),
]


def main():
    failures = []
    for label, text, want in PRICE_CASES:
        got = find_parking_price(text)
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"{'PASS' if got == want else 'FAIL'}  {label:38} {got!r}")

    for label, comp, want_state, want_price in STATE_CASES:
        state, price = parking_state(comp)
        if state not in PARKING_STATES:
            failures.append(f"{label}: {state!r} is not one of PARKING_STATES")
        if (state, price) != (want_state, want_price):
            failures.append(
                f"{label}: got {(state, price)!r}, want {(want_state, want_price)!r}"
            )
        print(f"{'PASS' if (state, price) == (want_state, want_price) else 'FAIL'}  "
              f"{label:38} {state} {price!r}")

    # The load-bearing assertion: unpriced adverts must not reach the median.
    stats = parking_price_stats([
        {"transaction_type": "pronajem", "parking_state": "priced", "parking_price_czk": 2000},
        {"transaction_type": "pronajem", "parking_state": "priced", "parking_price_czk": 2500},
        {"transaction_type": "pronajem", "parking_state": "unpriced", "parking_price_czk": None},
        {"transaction_type": "pronajem", "parking_state": "unpriced", "parking_price_czk": None},
        {"transaction_type": "pronajem", "parking_state": "none", "parking_price_czk": None},
        # A sale is not a monthly parking rent, whatever it says.
        {"transaction_type": "prodej", "parking_state": "priced", "parking_price_czk": 500000},
    ])
    ok = stats["n"] == 2 and stats["median_czk"] == 2250 and stats["n_unpriced"] == 2
    if not ok:
        failures.append(f"unpriced or sales leaked into the median: {stats}")
    print(f"{'PASS' if ok else 'FAIL'}  {'unpriced stay out of the median':38} {stats}")

    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print(f"all {len(PRICE_CASES) + len(STATE_CASES) + 1} checks pass")


if __name__ == "__main__":
    main()
