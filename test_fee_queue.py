#!/usr/bin/env python3
"""Tests for the "don't guess, ask Radim" queue.

The parser used to resolve every ambiguity silently. A wrong fee is worse than
a missing one -- it moves the all-in median without leaving a trace -- so the
four shapes below now store `unknown` and queue the advert instead.

The reasons are as much the product as the flag: a reason that keeps turning
out harmless is one to retire, and that argument can only be had if the queue
says WHY.

Run: python3 test_fee_queue.py
"""
import sys

from scrape import (
    FEE_AMBIGUITY_REASONS,
    build_fee_review_queue,
    extract_fees_and_electricity as parse,
)

# (label, costOfLiving, description, rent, expected reasons)
CASES = [
    # --- the four shapes that queue ------------------------------------- #
    ("lookahead into next clause", None,
     "zúčtovatelná záloha na služby, která činí 2.500 Kč při jedné osobě",
     None, {"lookahead"}),
    ("two different costs in one clause", None,
     "Poplatky jsou 4750 Kč a zálohy na energie 1150 kč", None,
     {"multiple_candidates"}),
    ("person tier with both amounts in one clause", None,
     "Poplatky 2 500 Kč pro 1 osobu a 3 500 Kč pro 2 osoby", None,
     {"person_tier"}),
    ("included with nothing to corroborate it", None,
     "Nájemné je uvedeno včetně poplatků za služby", None,
     {"included_without_amount"}),

    # --- the shapes that must NOT queue --------------------------------- #
    # A bare integer in the structured field has nothing to guess about. This
    # is the single most common path; queueing it would bury the real cases.
    ("clean int field", "3400", None, None, set()),
    ("plain fee clause", None, "Poplatky za služby 3.500 Kč měsíčně", None, set()),
    # No fee stated anywhere is a finding, not a guess. Roughly half the
    # rentals in the area are like this and they are already excluded from the
    # estimate by `fees_missing`.
    ("no fee anywhere", None, "Krásný byt v cihlovém domě.", None, set()),
    # The all-in total that is NOT the rent is already correctly rejected, and
    # rejection is an answer -- it must not also queue.
    # Same advert as the new case in test_fees.py, from the other side: it must
    # not be queued either. The fee is right there in the same clause; only the
    # re.I bug made it look like a lookahead.
    ("abbreviation is not a lookahead", None,
     "Poplatky za společné služby vč. TV a internetu a záloh na energie 6.500 Kč/měs.",
     None, set()),
    ("all-in total that is not the rent", None,
     "Celková cena včetně paušálních poplatků je 48 000 Kč", 34000, set()),
]


def main():
    failures = []
    for label, col, desc, rent, want in CASES:
        _fee, _src, _elec, why = parse(col, desc, rent)
        got = set(why)
        unknown = got - FEE_AMBIGUITY_REASONS
        if unknown:
            failures.append(f"{label}: reason not in FEE_AMBIGUITY_REASONS: {unknown}")
        if got != want:
            failures.append(f"{label}: got {got or '{}'}, want {want or '{}'}")
        status = "PASS" if got == want and not unknown else "FAIL"
        print(f"{status}  {label:42} {','.join(sorted(got)) or '-'}")

    # A queued advert must store unknown, not the guess -- and must remember
    # what the guess would have been, so a reason can be argued about later.
    fee, src, _elec, why = parse(None, "Poplatky jsou 4750 Kč a zálohy na energie 1150 kč", None)
    if not why:
        failures.append("ambiguous clause did not flag at all")
    if fee is None:
        failures.append("the parser should still compute what it WOULD have said")

    # Sales carry no service charge, so they never reach the queue however
    # their prose reads. 30 of 56 early entries were sales before this.
    queue = build_fee_review_queue([
        {"id": 1, "transaction_type": "prodej", "fee_unsure": ["included_without_amount"]},
        {"id": 2, "transaction_type": "pronajem", "fee_unsure": ["lookahead"]},
        {"id": 3, "transaction_type": "pronajem", "fee_unsure": []},
    ])
    if [q["id"] for q in queue] != [2]:
        failures.append(f"queue should hold only the flagged rental, got {[q['id'] for q in queue]}")
    print(f"PASS  {'queue holds only flagged rentals':42} n={len(queue)}")

    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print(f"all {len(CASES) + 2} checks pass")


if __name__ == "__main__":
    main()
