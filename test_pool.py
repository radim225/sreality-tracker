#!/usr/bin/env python3
"""Tests for the durable pool.

The pool is the thing every number downstream is computed from, and its
failures are the quiet kind: a price path that silently keeps only the latest
price makes "how many times did it come down" answer zero forever, and a
`gone_at` set from a missing search result turns a live flat into a statistic
about removals. Both of those have already happened once in this project in
another form, so they get tests.

Run: python3 test_pool.py
"""
import sys
from datetime import datetime, timedelta, timezone

import pool

FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}\n        got  {got!r}\n        want {want!r}")
        FAILURES.append(label)


def snap(at, comparables):
    return {"generated_at": at, "comparables": comparables, "config": {"radius_km": 3.0}}


def listing(lid, price, **extra):
    base = {
        "id": lid,
        "url": f"https://example.test/{lid}",
        "source": "sreality",
        "transaction_type": "pronajem",
        "disposition": "1+kk",
        "floor_area_sqm": 30.0,
        "price_czk": price,
        "total_czk": price + 3000,
        "fees_czk": 3000,
        "fees_missing": False,
        "price_czk_per_sqm": round((price + 3000) / 30.0),
    }
    base.update(extra)
    return base


# --- price path ---------------------------------------------------------- #
p = {}
pool.update_from_snapshot(p, snap("2026-08-01T00:00:00Z", [listing(1, 20000)]))
pool.update_from_snapshot(p, snap("2026-08-08T00:00:00Z", [listing(1, 20000)]))
pool.update_from_snapshot(p, snap("2026-08-15T00:00:00Z", [listing(1, 19000)]))
pool.update_from_snapshot(p, snap("2026-08-22T00:00:00Z", [listing(1, 18000)]))
rec = p["1"]

check("last price wins (D4)", rec["price_czk"], 18000)
check("unchanged price adds no history entry", len(rec["price_history"]), 3)
check("first_seen is the first sighting", rec["first_seen"], "2026-08-01T00:00:00Z")
check("last_seen is the latest sighting", rec["last_seen"], "2026-08-22T00:00:00Z")
check("two drops counted", pool.price_drops(rec)[0], 2)
check("total drop in percent", pool.price_drops(rec)[1], -10.0)

# A retrospective median must read the price of the day, not today's price --
# otherwise every past week gets recomputed with the latest number and the
# whole trend flattens itself.
check("price on 8 Aug", pool.price_at(rec, "2026-08-08")[0], 20000)
check("price on 16 Aug", pool.price_at(rec, "2026-08-16")[0], 19000)
check("price before first entry falls back to first", pool.price_at(rec, "2026-07-01")[0], 20000)
check("per_sqm_at uses the all-in total for rentals",
      pool.per_sqm_at(rec, "2026-08-08"), round(23000 / 30.0))
check("rent_per_sqm_at is the bare rent",
      pool.rent_per_sqm_at(rec, "2026-08-08"), round(20000 / 30.0))

# --- removals ------------------------------------------------------------ #
p2 = {}
pool.update_from_snapshot(p2, snap("2026-08-01T00:00:00Z", [listing(2, 20000)]))
# Absent from the next snapshot -- which says nothing at all on its own.
pool.update_from_snapshot(p2, snap("2026-08-02T00:00:00Z", []))
check("absence alone never sets gone_at (N-7)", p2["2"]["gone_at"], None)

pool.update_from_snapshot(p2, snap("2026-08-03T00:00:00Z", []),
                          changes={"newly_inactive": [{"id": 2}]})
check("confirmed removal sets gone_at", p2["2"]["gone_at"], "2026-08-03T00:00:00Z")
check("last asking price kept at removal", p2["2"]["gone_last_price_czk"], 20000)

pool.update_from_snapshot(p2, snap("2026-08-04T00:00:00Z", [listing(2, 20000)]))
check("a listing that answers again clears gone_at", p2["2"]["gone_at"], None)
check("but the fact it was called gone is kept",
      p2["2"]["resurrected_at"], "2026-08-04T00:00:00Z")

# --- window -------------------------------------------------------------- #
now = datetime(2026, 8, 24, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


p3 = {}
pool.update_from_snapshot(p3, snap(iso(now - timedelta(days=5)), [listing(10, 20000)]))
pool.update_from_snapshot(p3, snap(iso(now - timedelta(days=40)), [listing(11, 21000)]))
check("recent record is in the window", len(pool.window(p3, now=iso(now))), 1)
check("old record stays in the pool", len(p3), 2)
check("moving the window back finds the old record",
      [r["id"] for r in pool.window(p3, end=iso(now - timedelta(days=38)))], ["11"])

# --- the n<8 floor ------------------------------------------------------- #
check("median of seven is refused (N-3)", pool.quantiles(range(7))["median"], None)
check("...and says why", pool.quantiles(range(7))["too_small"], True)
check("median of eight is allowed", pool.quantiles(range(8))["median"], 4)
check("quantiles ignore None", pool.quantiles([1, None, 2, 3, 4, 5, 6, 7, 8])["n"], 8)

# --- days on market ------------------------------------------------------ #
rec_since = {"since": "2026-08-01", "gone_at": None}
check("days on market from the portal's own date",
      pool.days_on_market(rec_since, now=iso(now)), 23)
check("a gone listing stops counting at removal",
      pool.days_on_market({"since": "2026-08-01", "gone_at": "2026-08-10T00:00:00Z"},
                          now=iso(now)), 9)
check("no `since` means no answer, not a guess",
      pool.days_on_market({"first_seen": iso(now)}, now=iso(now)), None)

# --- config fingerprint -------------------------------------------------- #
state = {"config_changes": []}
check("first config is a baseline, not a change",
      pool.note_config(state, {"radius_km": 3.0}, "2026-08-01T00:00:00Z"), False)
check("the same config again is not a change",
      pool.note_config(state, {"radius_km": 3.0}, "2026-08-02T00:00:00Z"), False)
check("a different config is",
      pool.note_config(state, {"radius_km": 5.0}, "2026-08-03T00:00:00Z"), True)
check("and the report can find it in a period",
      pool.config_changed_between(state, "2026-08-02T00:00:00Z", "2026-08-04T00:00:00Z"), True)
check("...but not outside it",
      pool.config_changed_between(state, "2026-08-05T00:00:00Z", "2026-08-06T00:00:00Z"), False)

print()
if FAILURES:
    print(f"{len(FAILURES)} case(s) failed: {', '.join(FAILURES)}")
    sys.exit(1)
print("all pool cases pass")
