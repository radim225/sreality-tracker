#!/usr/bin/env python3
"""Tests for the rent estimate and the market metrics.

These are the numbers Radim will act on, and every failure mode here is silent:
a factor applied to the wrong base, a trend horizon that quietly slipped two
weeks, a noise threshold picked instead of derived, a premium introduced for an
attribute the sample cannot separate. None of them raise -- they just produce a
plausible wrong number.

Run: python3 test_market.py
"""
import sys

import json

import market

FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}\n        got  {got!r}\n        want {want!r}")
        FAILURES.append(label)


def rental(rid, per_sqm, sqm=30.0, fee=3000, **extra):
    """A rental whose all-in Kč/m² is exactly `per_sqm`."""
    total = round(per_sqm * sqm)
    rec = {
        "id": str(rid),
        "url": f"https://example.test/{rid}",
        "transaction_type": "pronajem",
        "disposition": "1+kk",
        "floor_area_sqm": sqm,
        "price_czk": total - fee,
        "total_czk": total,
        "fees_czk": fee,
        "fees_missing": False,
        "price_czk_per_sqm": round(total / sqm),
        "first_seen": "2026-08-01T00:00:00Z",
        "last_seen": "2026-08-24T00:00:00Z",
        "price_history": [{"at": "2026-08-01", "price_czk": total - fee, "total_czk": total}],
    }
    rec.update(extra)
    return rec


AS_OF = "2026-08-24T00:00:00Z"

# --- what gets into the base -------------------------------------------- #
check("rental with a known fee is eligible", market.base_eligible(rental(1, 700)), True)
check("unknown fee is excluded (N-4)",
      market.base_eligible(rental(2, 700, fees_missing=True)), False)
check("outside the size band is excluded",
      market.base_eligible(rental(3, 700, sqm=45.0)), False)
check("a different disposition is excluded",
      market.base_eligible(rental(4, 700, disposition="2+kk")), False)
check("a not-a-price outlier is excluded",
      market.base_eligible(rental(5, 700, deal_outlier=True)), False)
check("excluded-from-stats is not eligible",
      market.base_eligible(rental(7, 700, exclude_from_stats=True)), False)
check("a sale is not a rental", market.base_eligible(rental(6, 700, transaction_type="prodej")),
      False)

# --- the n<8 floor propagates ------------------------------------------- #
small = [rental(i, 700) for i in range(5)]
est = market.rent_estimate(small, as_of=AS_OF, state={}, week_key="2026-W34")
check("estimate from five adverts publishes no median",
      est["profiles"]["zarizeny"]["rent"]["median"], None)
check("...and says the sample is too small",
      est["base_total_per_sqm"]["too_small"], True)

# --- factors ------------------------------------------------------------- #
# Ten unfurnished at 600, ten furnished at 700 -- so furnished is +16.7 % on
# the complement and the whole-sample median sits at 650.
records = (
    [rental(f"u{i}", 600, furnished="ne", is_new_building=False) for i in range(10)]
    + [rental(f"f{i}", 700, furnished="ano", is_new_building=True) for i in range(10)]
)
est = market.rent_estimate(records, as_of=AS_OF, state={}, week_key="2026-W34")
check("base is the whole sample", est["base_total_per_sqm"]["median"], 650)
check("furnished contrast is measured, not assumed",
      est["factors"]["zarizeny"]["contrast_pct"], 16.7)
check("furnished factor is relative to the base",
      est["factors"]["zarizeny"]["factor"], round(700 / 650, 4))
check("unfurnished factor likewise",
      est["factors"]["nezarizeny"]["factor"], round(600 / 650, 4))
check("both factors report their n", est["factors"]["zarizeny"]["n"], 10)

# Only furnishing and building condition are ever allowed a factor (N-2).
check("no factor for anything else",
      sorted(est["factors"]), ["nezarizeny", "novostavba", "zarizeny"])
check("...and the report names what it cannot separate",
      "balkon" in est["not_separable"], True)

# --- a factor the sample cannot support is refused ----------------------- #
thin = (
    [rental(f"u{i}", 600, furnished="ne") for i in range(10)]
    + [rental(f"f{i}", 700, furnished="ano") for i in range(3)]
)
est_thin = market.rent_estimate(thin, as_of=AS_OF, state={}, week_key="2026-W34")
check("a three-advert group gets no factor",
      est_thin["factors"]["zarizeny"]["usable"], False)
check("...and the profile falls back to the plain base",
      est_thin["profiles"]["zarizeny"]["factor"], 1.0)

# --- two numbers, never one (R-5.4) -------------------------------------- #
prof = est["profiles"]["zarizeny"]
check("bare rent is below the all-in total",
      prof["rent"]["median"] < prof["total"]["median"], True)
check("the spread is always there", prof["rent"]["p25"] is not None, True)

# --- the model is shown against the data it models ----------------------- #
# Every factor is measured against the whole base and then multiplied, so what
# the two attributes share is counted twice. The subgroup's own median is the
# cross-check that makes that visible instead of hidden.
check("the direct median is offered when the subgroup carries one",
      est["profiles"]["zarizeny"]["direct"]["available"], True)
check("...from the adverts that match on both attributes",
      est["profiles"]["zarizeny"]["direct"]["n"], 10)
check("...and refused when it does not",
      est["profiles"]["nezarizeny"]["direct"]["available"], False)
check("a refused cross-check still says how few there were",
      est["profiles"]["nezarizeny"]["direct"]["n"], 0)

# --- hard filter switch (R-5.7) ------------------------------------------ #
state = {}
wide = [rental(f"h{i}", 700, furnished="ano", is_new_building=True, no_commission=True)
        for i in range(35)]
for week in ("2026-W31", "2026-W32", "2026-W33"):
    market.rent_estimate(wide, as_of=AS_OF, state=state, week_key=week)
check("three good weeks are not enough", state.get("hard_filters_since"), None)
est4 = market.rent_estimate(wide, as_of=AS_OF, state=state, week_key="2026-W34")
check("the fourth consecutive week switches", est4["mode"], "hard_filters")
check("...and the switch is announced, not silent", est4["switched_now"], True)
check("...and it is written down", bool(state["hard_filters_since"]), True)

state2 = {}
for week in ("2026-W31", "2026-W32", "2026-W33", "2026-W34"):
    market.rent_estimate(wide, as_of=AS_OF, state=state2, week_key=week, allow_switch=False)
check("a read-only call never flips the mode", state2.get("hard_filters_since"), None)

# A gap in the streak resets it: a week we did not measure is not a week that held.
state3 = {"filter_weeks": {"2026-W30": 40, "2026-W32": 40, "2026-W33": 40}}
progress = market.hard_filter_progress(state3, wide, "2026-W34")
check("a missing week breaks the streak", progress["ready"], False)

# --- trend --------------------------------------------------------------- #
series = [
    {"week": "2026-W23", "median": None, "n": 0},
    {"week": "2026-W24", "median": None, "n": 0},
    {"week": "2026-W30", "median": 500, "n": 50},
    {"week": "2026-W31", "median": 510, "n": 50},
    {"week": "2026-W32", "median": 520, "n": 50},
    {"week": "2026-W33", "median": 530, "n": 50},
    {"week": "2026-W34", "median": 550, "n": 50},
]
t = market.trend(series, 4)
check("4 weeks back means four calendar weeks", t["from_week"], "2026-W30")
check("...not four populated entries", t["pct"], 10.0)
check("an endpoint the pool never filled gives no trend",
      market.trend(series, 11), None)

cfg_state = {"config_changes": [
    {"at": "2026-01-01T00:00:00Z", "fingerprint": "a"},
    {"at": "2026-08-19T00:00:00Z", "fingerprint": "b"},
]}
check("a config change inside the span is flagged (N-5)",
      market.trend(series, 4, cfg_state)["config_contaminated"], True)
early_cfg = {"config_changes": [
    {"at": "2026-01-01T00:00:00Z", "fingerprint": "a"},
    {"at": "2026-05-01T00:00:00Z", "fingerprint": "b"},
]}
check("...and a change before the span is not",
      market.trend(series, 4, early_cfg)["config_contaminated"], False)

# A horizon whose two ends hold very different sample sizes is comparing two
# different populations. The archive's own early weeks do exactly this: the pool
# was still filling, and the ramp-up reads as the market rising ~11 %/month.
ramp = [
    {"week": "2026-W30", "median": 500, "n": 40},
    {"week": "2026-W31", "median": 510, "n": 90},
    {"week": "2026-W32", "median": 520, "n": 150},
    {"week": "2026-W33", "median": 530, "n": 300},
    {"week": "2026-W34", "median": 550, "n": 460},
]
check("a sample that grew tenfold is flagged",
      market.trend(ramp, 4)["sample_shifted"], True)
check("a steady sample is not", market.trend(series, 4)["sample_shifted"], False)
check("both ends are reported so the reader can see it",
      (market.trend(ramp, 4)["from_n"], market.trend(ramp, 4)["to_n"]), (40, 460))

# A horizon of N weeks compares two endpoints, so the series needs N+1 points.
# Requesting exactly max(TREND_HORIZONS) left the 12-week trend one week short
# and it returned None every time -- reported as "the series is still shorter",
# which reads as a fact about the data rather than an off-by-one.
full = [{"week": f"2026-W{20 + i:02d}", "median": 500 + i, "n": 50}
        for i in range(market.SERIES_WEEKS)]
check("the series is long enough for the longest horizon",
      market.SERIES_WEEKS, max(market.TREND_HORIZONS) + 1)
for horizon in market.TREND_HORIZONS:
    check(f"a {horizon}-week trend is computable from a full series",
          market.trend(full, horizon) is not None, True)
check("the longest horizon reaches the oldest point",
      market.trend(full, max(market.TREND_HORIZONS))["from_week"], full[0]["week"])

# --- noise band ---------------------------------------------------------- #
# Under six week-on-week deltas the threshold is not derivable, and R-6.2 says
# it must be derived rather than chosen -- so it comes back as "unknown" and the
# report says so instead of falling back to a number somebody picked.
check("a five-week series yields no threshold",
      market.noise_band(series)["pct"], None)
flat = [{"week": f"2026-W{20 + i}", "median": 500, "n": 50} for i in range(10)]
check("a flat series has a zero threshold", market.noise_band(flat)["pct"], 0.0)
wobbly = [{"week": f"2026-W{20 + i}", "median": 500 + (25 if i % 2 else 0), "n": 50}
          for i in range(10)]
band = market.noise_band(wobbly)
check("the threshold is derived from the data", band["pct"] is not None, True)
check("a move inside the band is not news", market.above_noise(0.1, band), False)
check("a move outside it is", market.above_noise(50.0, band), True)
check("no threshold means no verdict", market.above_noise(50.0, {"pct": None}), None)

# --- sale dynamics never claims a sale (N-1) ----------------------------- #
sales = [
    {"id": "s1", "transaction_type": "prodej", "since": "2026-07-01",
     "gone_at": "2026-08-01T00:00:00Z", "price_czk": 8_000_000,
     "gone_last_price_czk": 8_000_000, "price_history": [
         {"at": "2026-07-01", "price_czk": 9_000_000},
         {"at": "2026-07-20", "price_czk": 8_000_000}]},
    {"id": "s2", "transaction_type": "prodej", "since": "2026-08-01", "gone_at": None,
     "price_czk": 7_000_000, "price_history": [{"at": "2026-08-01", "price_czk": 7_000_000}]},
]
dyn = market.sale_dynamics(sales, AS_OF)
check("live and gone are counted apart (R-6.4)", (dyn["live_n"], dyn["gone_n"]), (1, 1))
check("a discount is counted", dyn["repriced_n"], 1)
check("the removal keeps only the last asking price",
      dyn["gone_last_asking"]["n"], 1)

# A listing whose price rose overall must not be counted as a discount depth.
rose = [{"id": "s3", "transaction_type": "prodej", "since": "2026-07-01", "gone_at": None,
         "price_czk": 9_500_000, "price_history": [
             {"at": "2026-07-01", "price_czk": 9_000_000},
             {"at": "2026-07-10", "price_czk": 8_800_000},
             {"at": "2026-07-20", "price_czk": 9_500_000}]}]
check("a net increase is not a discount depth",
      market.sale_dynamics(rose, AS_OF)["drop_depth"]["n"], 0)

# --- mortgage coverage stays out of the public numbers ------------------- #
est_cov = market.rent_estimate(records, as_of=AS_OF, state={}, week_key="2026-W34")
check("no payment configured means no coverage block",
      market.mortgage_coverage(est_cov, None), None)
cov = market.mortgage_coverage(est_cov, 30000)
check("coverage is a percentage of the bare rent, not the total",
      cov["profiles"]["zarizeny"]["covered_pct"],
      round(est_cov["profiles"]["zarizeny"]["rent"]["median"] / 30000 * 100))
# The estimate is what the public dashboard card renders from, so the coverage
# must live outside it entirely rather than being filtered out at render time.
public = json.dumps(est_cov, ensure_ascii=False).lower()
for word in ("hypot", "splátk", "mortgage", "pokryje", "payment"):
    check(f"the estimate carries no {word!r}", word in public, False)

print()
if FAILURES:
    print(f"{len(FAILURES)} case(s) failed: {', '.join(FAILURES)}")
    sys.exit(1)
print("all market cases pass")
