#!/usr/bin/env python3
"""Deterministic statistics over the pool: what the flat rents for, and how the
area is moving.

Every number in the weekly report is produced here, in Python. Nothing in this
module talks to a model and nothing downstream is allowed to recompute a figure
-- an optional interpretation layer may comment on these numbers, never make
them (D9, R-9.2).

Two things this module is careful about, both learned the expensive way:

* A median from a handful of adverts reads as a fact. Anything under 8 comes
  back as None with `too_small` set, and the report says so instead of printing
  a number (N-3).
* Filtering buys accuracy the sample cannot deliver. Across every filter cut
  the median moves ±5 %, while the spread inside each cut is ±35 %. So the
  estimate runs on a wide base with named, data-derived factors (D2), and the
  factors are only ever applied for the two attributes that actually separate:
  furnishing and building condition (§3.3). Balcony, cellar, garage and floor
  get no factor at all -- on n≈20 the balcony cut even came out negative,
  because flats with balconies happened to be bigger (N-2).
"""
import statistics
from datetime import datetime, timedelta, timezone

import pool as poolmod
from pool import parse_ts, per_sqm_at, quantiles, rent_per_sqm_at

# The flat the estimate is for. Deliberately no unit number and no project name
# -- this file is in a public repo (D11, R-10.2). Size band rather than a bare
# disposition because Kč/m² falls with size even inside 1+kk.
REFERENCE = {
    "disposition": "1+kk",
    "floor_area_sqm": 29.6,
    "size_band_sqm": (25.0, 35.0),
    "is_new_building": True,
}

# R-5.7: how big the hard-filtered pool has to get, and for how long, before the
# estimate stops needing factors at all.
HARD_FILTER_MIN_N = 30
HARD_FILTER_WEEKS = 4

# Attributes the sample provably cannot separate. Named in the report on
# purpose: "we don't adjust for a balcony" is a finding, not an omission.
NOT_SEPARABLE = ("balkon", "sklep", "garáž/stání", "patro", "orientace")

# Horizons the report leads with (R-6.1). Week-on-week is deliberately absent:
# with a 30-day window two neighbouring weeks share ~75 % of their sample, so
# the delta is small before the market gets a say.
TREND_HORIZONS = (4, 12)
# A horizon of N weeks compares two endpoints, so it needs N+1 points in the
# series. Asking for exactly max(TREND_HORIZONS) gave a series whose oldest
# week was one short, and the 12-week trend then came back None every single
# time -- silently, as "série je zatím kratší".
SERIES_WEEKS = max(TREND_HORIZONS) + 1


def iso_week_key(when):
    dt = parse_ts(when) or datetime.now(timezone.utc)
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(key):
    """Monday 00:00 UTC to the last instant of Sunday, for an ISO week key."""
    year, week = key.split("-W")
    start = datetime.fromisocalendar(int(year), int(week), 1).replace(tzinfo=timezone.utc)
    return start, start + timedelta(days=7) - timedelta(seconds=1)


def previous_week_key(key):
    start, _ = week_bounds(key)
    return iso_week_key(start - timedelta(days=1))


def month_key(when):
    dt = parse_ts(when) or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def previous_month_key(key):
    year, month = (int(x) for x in key.split("-"))
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def month_bounds(key):
    year, month = (int(x) for x in key.split("-"))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return start, datetime(end_year, end_month, 1, tzinfo=timezone.utc) - timedelta(seconds=1)


# ---------------------------------------------------------------- rent estimate

def base_eligible(rec):
    """R-5.1. Excludes adverts whose fee is unknown on purpose: their all-in
    total is missing a real cost, so they read as cheap and drag the base down
    (N-4, the same reasoning `rank_deals` already runs on)."""
    if rec.get("transaction_type") != "pronajem":
        return False
    if rec.get("disposition") != REFERENCE["disposition"]:
        return False
    sqm = rec.get("floor_area_sqm")
    lo, hi = REFERENCE["size_band_sqm"]
    if sqm is None or not (lo <= sqm <= hi):
        return False
    if rec.get("fees_missing"):
        return False
    if rec.get("deal_outlier"):
        return False
    return True


def hard_filter_eligible(rec):
    """The narrow cut D2 wants to switch to eventually: new build, no agency
    commission -- Radim's own situation, no factors needed."""
    return (
        base_eligible(rec)
        and rec.get("is_new_building") is True
        and rec.get("no_commission") is True
    )


def _factor(records, group_pred, base_median, as_of, label):
    """One attribute's level relative to the whole base.

    Reported as a multiplicative factor rather than an additive premium so the
    two factors compose without either of them having to know about the other.
    `contrast_pct` is the same attribute stated the way §3.3 states it -- group
    against its complement -- because that is the number that is intuitive to
    read, even though it is not the one that gets applied."""
    group = [r for r in records if group_pred(r) is True]
    rest = [r for r in records if group_pred(r) is False]
    group_stats = quantiles([per_sqm_at(r, as_of) for r in group])
    rest_stats = quantiles([per_sqm_at(r, as_of) for r in rest])
    out = {
        "label": label,
        "n": group_stats["n"],
        "median": group_stats["median"],
        "complement_n": rest_stats["n"],
        "complement_median": rest_stats["median"],
        "factor": 1.0,
        "contrast_pct": None,
        "usable": False,
        "reason": None,
    }
    if group_stats["too_small"] or not base_median:
        out["reason"] = f"vzorek {group_stats['n']} < 8, faktor se nepoužije"
        return out
    out["factor"] = round(group_stats["median"] / base_median, 4)
    out["usable"] = True
    if not rest_stats["too_small"] and rest_stats["median"]:
        out["contrast_pct"] = round(
            (group_stats["median"] - rest_stats["median"]) / rest_stats["median"] * 100, 1
        )
    else:
        out["reason"] = (
            f"protivzorek {rest_stats['n']} < 8, kontrast se neuvádí"
        )
    return out


def _scaled(stats, factor, sqm):
    """Turn a Kč/m² spread into monthly koruna for the reference flat."""
    def one(value):
        return round(value * factor * sqm) if value else None
    return {
        "median": one(stats["median"]),
        "p25": one(stats["p25"]),
        "p75": one(stats["p75"]),
        "per_sqm": round(stats["median"] * factor) if stats["median"] else None,
    }


def hard_filter_progress(state, records, week_key):
    """R-5.7. Records this week's filtered sample size and reports whether the
    switch condition (n >= 30 for four consecutive weeks) is met."""
    weeks = state.setdefault("filter_weeks", {})
    weeks[week_key] = len([r for r in records if hard_filter_eligible(r)])
    # Only the run of weeks ending at the current one counts; a gap resets it,
    # because a missed week is not evidence the sample held.
    keys, cursor = [], week_key
    for _ in range(HARD_FILTER_WEEKS):
        if cursor not in weeks:
            break
        keys.append(cursor)
        cursor = previous_week_key(cursor)
    streak = [weeks[k] for k in keys]
    ready = (
        len(streak) == HARD_FILTER_WEEKS
        and all(n >= HARD_FILTER_MIN_N for n in streak)
    )
    return {
        "n_this_week": weeks[week_key],
        "streak": list(reversed(streak)),
        "weeks_required": HARD_FILTER_WEEKS,
        "min_n": HARD_FILTER_MIN_N,
        "ready": ready,
        "active": bool(state.get("hard_filters_since")),
        "since": state.get("hard_filters_since"),
    }


def rent_estimate(pool_records, as_of=None, state=None, week_key=None, allow_switch=True):
    """§5. Two profiles, two numbers each, always with a spread.

    `allow_switch=False` makes the call read-only with respect to the mode: the
    dashboard recomputes this every 8 h, and the switch to hard filters has to
    be announced in a report, not happen quietly on a render (R-5.7)."""
    as_of = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sqm = REFERENCE["floor_area_sqm"]
    state = state if state is not None else {}
    week_key = week_key or iso_week_key(as_of)

    records = [r for r in pool_records if base_eligible(r)]
    progress = hard_filter_progress(state, records, week_key)
    switched_now = False
    if progress["ready"] and not progress["active"] and allow_switch:
        state["hard_filters_since"] = as_of
        progress["active"] = True
        progress["since"] = as_of
        switched_now = True

    mode = "hard_filters" if progress["active"] else "base_and_factors"
    if mode == "hard_filters":
        # D2's end state: the sample carries the filters itself, so the factors
        # are thrown away rather than layered on top of an already-narrow cut.
        records = [r for r in records if hard_filter_eligible(r)]

    total_stats = quantiles([per_sqm_at(r, as_of) for r in records])
    rent_stats = quantiles([rent_per_sqm_at(r, as_of) for r in records])

    factors = {}
    if mode == "base_and_factors":
        factors["novostavba"] = _factor(
            records, lambda r: r.get("is_new_building"), total_stats["median"], as_of,
            "novostavba",
        )
        factors["zarizeny"] = _factor(
            records, lambda r: (True if r.get("furnished") == "ano"
                               else False if r.get("furnished") == "ne" else None),
            total_stats["median"], as_of, "zařízený",
        )
        factors["nezarizeny"] = _factor(
            records, lambda r: (True if r.get("furnished") == "ne"
                               else False if r.get("furnished") == "ano" else None),
            total_stats["median"], as_of, "nezařízený",
        )

    def direct_check(furnished_value):
        """The reference profile's own median, computed straight from the
        adverts that match it on both attributes.

        The factor model estimates each attribute against the whole base and
        then multiplies, which double-counts whatever the two share -- new
        builds in this area are also more often furnished. So the direct median
        is shown next to the modelled figure whenever the subgroup is big
        enough to state one. When they disagree, that disagreement is the
        honest width of the answer."""
        subset = [
            r for r in records
            if r.get("is_new_building") is True and r.get("furnished") == furnished_value
        ]
        total = quantiles([per_sqm_at(r, as_of) for r in subset])
        rent = quantiles([rent_per_sqm_at(r, as_of) for r in subset])
        if total["too_small"]:
            return {"n": total["n"], "available": False}
        return {
            "n": total["n"],
            "available": True,
            "total": _scaled(total, 1.0, sqm),
            "rent": _scaled(rent, 1.0, sqm),
        }

    def profile(name, furnished_value):
        if mode == "hard_filters":
            subset = [r for r in records if r.get("furnished") == furnished_value]
            sub_total = quantiles([per_sqm_at(r, as_of) for r in subset])
            sub_rent = quantiles([rent_per_sqm_at(r, as_of) for r in subset])
            if not sub_total["too_small"]:
                return {
                    "name": name, "factor": 1.0, "basis": "přímý medián podskupiny",
                    "n": sub_total["n"],
                    "total": _scaled(sub_total, 1.0, sqm),
                    "rent": _scaled(sub_rent, 1.0, sqm),
                }
            return {
                "name": name, "factor": 1.0,
                "basis": f"podskupina má jen {sub_total['n']} inzerátů, použit filtrovaný základ",
                "n": total_stats["n"],
                "total": _scaled(total_stats, 1.0, sqm),
                "rent": _scaled(rent_stats, 1.0, sqm),
            }
        applied = 1.0
        parts = []
        for key in ("novostavba", "zarizeny" if furnished_value == "ano" else "nezarizeny"):
            f = factors.get(key)
            if f and f["usable"]:
                applied *= f["factor"]
                # Czech decimal comma: this string is shown to a reader, not parsed.
                parts.append(f"{f['label']} ×{f['factor']:.3f}".replace(".", ","))
            elif f:
                parts.append(f"{f['label']} nepoužit ({f['reason']})")
        return {
            "name": name,
            "factor": round(applied, 4),
            "basis": " · ".join(parts) or "bez faktorů",
            "n": total_stats["n"],
            "total": _scaled(total_stats, applied, sqm),
            "rent": _scaled(rent_stats, applied, sqm),
        }

    profiles = {
        "zarizeny": profile("zařízený", "ano"),
        "nezarizeny": profile("nezařízený", "ne"),
    }
    for key, value in (("zarizeny", "ano"), ("nezarizeny", "ne")):
        profiles[key]["direct"] = direct_check(value)
    furnished_delta = None
    a, b = profiles["zarizeny"]["rent"]["median"], profiles["nezarizeny"]["rent"]["median"]
    if a and b:
        furnished_delta = round((a - b) / b * 100, 1)

    return {
        "as_of": as_of,
        "mode": mode,
        "switched_now": switched_now,
        "window_days": poolmod.WINDOW_DAYS,
        "reference": dict(REFERENCE),
        "base_total_per_sqm": total_stats,
        "base_rent_per_sqm": rent_stats,
        "factors": factors,
        "profiles": profiles,
        "furnished_delta_pct": furnished_delta,
        "not_separable": list(NOT_SEPARABLE),
        "hard_filters": progress,
        # §3.5: the gap between the two is service charges and utilities passing
        # through the landlord, not income. Stated so it can never be read as
        # margin.
        "passthrough_czk": (
            profiles["zarizeny"]["total"]["median"] - profiles["zarizeny"]["rent"]["median"]
            if profiles["zarizeny"]["total"]["median"] and profiles["zarizeny"]["rent"]["median"]
            else None
        ),
    }


def mortgage_coverage(estimate, payment_czk):
    """R-5.6. Private: this never reaches the public dashboard or the archive.

    The payment comes from the environment rather than the source, so the
    figure stays out of a public repo (R-10.1)."""
    if not payment_czk:
        return None
    out = {"payment_czk": payment_czk, "profiles": {}}
    for key, prof in estimate["profiles"].items():
        income = prof["rent"]["median"]
        if not income:
            continue
        out["profiles"][key] = {
            "name": prof["name"],
            "income_czk": income,
            "covered_pct": round(income / payment_czk * 100),
            "missing_czk": max(0, payment_czk - income),
        }
    return out


# --------------------------------------------------------------- market metrics

def level(records, tx, as_of, disposition=None):
    values = [
        per_sqm_at(r, as_of) for r in records
        if r.get("transaction_type") == tx
        and not r.get("deal_outlier")
        and (disposition is None or r.get("disposition") == disposition)
        and not (tx == "pronajem" and r.get("fees_missing"))
    ]
    return quantiles(values)


def weekly_series(all_records, weeks=12, as_of=None, tx="pronajem", disposition=None):
    """The last N ISO weeks, each computed on the same 30-day window definition
    used live -- so the series is comparable with itself rather than with
    whatever happened to be online on a given day.

    Two neighbouring weeks share ~75 % of a 30-day window by construction, so
    the week-on-week delta is small for arithmetic reasons before the market
    gets a say. That is exactly why the report leads with the 4- and 12-week
    move instead (§3.6)."""
    end_key = iso_week_key(as_of)
    series, cursor = [], end_key
    for _ in range(weeks):
        _start, end = week_bounds(cursor)
        window = poolmod.window(all_records, end=end)
        stats = level(window, tx, end, disposition)
        series.append({
            "week": cursor,
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "median": stats["median"],
            "p25": stats["p25"],
            "p75": stats["p75"],
            "n": stats["n"],
        })
        cursor = previous_week_key(cursor)
    return list(reversed(series))


def trend(series, weeks_back, state=None):
    """Change over N weeks, in percent, with both endpoints named so the report
    can show what it compared.

    The endpoint is found by counting calendar weeks back, not by counting
    entries: a week the pool could not fill (n=0 before the archive starts)
    would otherwise shift the horizon and quietly turn "4 weeks" into six.

    `config_contaminated` says a change to our own search fell inside the span.
    The two largest jumps in the whole series to date were exactly that, and
    reporting one as market movement is the thing this project has already
    learned not to do (N-5)."""
    by_week = {p["week"]: p for p in series}
    points = [p for p in series if p["median"]]
    if len(points) < 2:
        return None
    latest = points[-1]
    target = latest["week"]
    for _ in range(weeks_back):
        target = previous_week_key(target)
    earlier = by_week.get(target)
    if not earlier or not earlier["median"]:
        return None
    span_start, _ = week_bounds(earlier["week"])
    _span, span_end = week_bounds(latest["week"])
    contaminated = None
    if state is not None:
        contaminated = poolmod.config_changed_between(state, span_start, span_end)
    # A trend across a sample that doubled is not a trend. The archive's early
    # weeks are thin by construction (the pool was still filling), and without
    # this the ramp-up reads as the market rising ~11 %/month.
    from_n, to_n = earlier.get("n") or 0, latest.get("n") or 0
    sample_shifted = bool(from_n and to_n) and (
        to_n > from_n * 2 or to_n < from_n / 2
    )
    return {
        "weeks": weeks_back,
        "from_week": earlier["week"],
        "from": earlier["median"],
        "from_n": from_n,
        "to_week": latest["week"],
        "to": latest["median"],
        "to_n": to_n,
        "pct": round((latest["median"] - earlier["median"]) / earlier["median"] * 100, 1),
        "config_contaminated": contaminated,
        "sample_shifted": sample_shifted,
    }


def noise_band(series):
    """R-6.2: the threshold is derived, not chosen.

    Week-on-week moves in this series are dominated by resampling, so the
    typical absolute weekly move IS the noise floor. p75 of |Δ| is used rather
    than the mean so a couple of genuine jumps don't inflate the band that is
    supposed to detect them."""
    deltas = []
    points = [p for p in series if p["median"]]
    for a, b in zip(points, points[1:]):
        deltas.append(abs((b["median"] - a["median"]) / a["median"] * 100))
    if len(deltas) < 6:
        return {"pct": None, "n": len(deltas),
                "note": "série je kratší než 6 týdnů, práh se zatím odvodit nedá"}
    deltas.sort()
    p75 = deltas[min(int(len(deltas) * 0.75), len(deltas) - 1)]
    return {
        "pct": round(p75, 1),
        "n": len(deltas),
        "median_delta": round(statistics.median(deltas), 1),
        "max_delta": round(deltas[-1], 1),
        "note": "p75 absolutních týdenních změn v dostupné sérii",
    }


def above_noise(change_pct, band):
    if change_pct is None or band.get("pct") is None:
        return None
    return abs(change_pct) > band["pct"]


def sale_dynamics(records, as_of=None):
    """R-6.3 / R-6.4. Everything here describes adverts, never transactions.

    A disappeared advert is a disappeared advert: the portals do not publish a
    realised price and a new build stays registered to the developer until
    handover, so there is nothing to look up either (§3.4). The only honest
    label is "last asking price when it vanished" (N-1)."""
    sales = [r for r in records if r.get("transaction_type") == "prodej"]
    live = [r for r in sales if not r.get("gone_at")]
    gone = [r for r in sales if r.get("gone_at")]

    dom = [d for d in (poolmod.days_on_market(r, as_of) for r in live) if d is not None]
    dom_gone = [d for d in (poolmod.days_on_market(r, as_of) for r in gone) if d is not None]

    drops = [poolmod.price_drops(r) for r in sales]
    repriced = [(c, p) for c, p in drops if c > 0]

    return {
        "live_n": len(live),
        "gone_n": len(gone),
        "since_coverage_pct": (
            round(len([r for r in sales if r.get("since")]) / len(sales) * 100) if sales else 0
        ),
        "days_on_market": quantiles(dom),
        "days_on_market_gone": quantiles(dom_gone),
        "repriced_n": len(repriced),
        "repriced_share_pct": round(len(repriced) / len(sales) * 100) if sales else 0,
        # Only the adverts that ended lower than they started. A listing that
        # cut once and then raised its price net-rose; folding its |change| into
        # a "discount depth" would report an increase as a discount.
        "drop_depth": quantiles([-p for _c, p in repriced if p < 0]),
        "gone_last_asking": quantiles(
            [r.get("gone_last_price_czk") for r in gone if r.get("gone_last_price_czk")]
        ),
    }


def similar_to_reference(rec):
    """Adverts close enough to the reference flat to be worth a direct link in
    the weekly report (R-7.3). Deliberately looser than the estimate's base --
    a fee-less advert is still worth looking at, it just doesn't get to move
    the median."""
    if rec.get("transaction_type") != "pronajem":
        return False
    if rec.get("disposition") != REFERENCE["disposition"]:
        return False
    sqm = rec.get("floor_area_sqm")
    lo, hi = REFERENCE["size_band_sqm"]
    return sqm is not None and lo <= sqm <= hi


def period_movement(all_records, start, end):
    """What arrived and what left between two instants -- the two halves R-6.4
    insists on keeping apart."""
    start_dt, end_dt = parse_ts(start), parse_ts(end)
    arrived, left = [], []
    for rec in poolmod.records_of(all_records):
        first = parse_ts(rec.get("first_seen"))
        if first and start_dt <= first <= end_dt:
            arrived.append(rec)
        gone = parse_ts(rec.get("gone_at"))
        if gone and start_dt <= gone <= end_dt:
            left.append(rec)
    return arrived, left
