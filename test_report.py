#!/usr/bin/env python3
"""Tests for the weekly write-up: the verdict, and what the text is allowed to
say.

The verdict on the first line is the whole reason the format works -- if every
week reads the same, the report gets skimmed and a real move goes past unread.
So "quiet" has to mean quiet, and "important" has to be for reasons that are
about the market rather than about us changing what we look at.

The other half is what must never appear: a disappeared advert described as a
sale (N-1), and any personal figure in a file that lives in a public repo
(R-10.1, R-10.3).

Run: python3 test_report.py
"""
import sys
from datetime import timedelta

import report

FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}\n        got  {got!r}\n        want {want!r}")
        FAILURES.append(label)


def rental(rid, per_sqm, first_seen, last_seen, sqm=30.0, fee=3000, **extra):
    total = round(per_sqm * sqm)
    rec = {
        "id": str(rid),
        "url": f"https://example.test/{rid}",
        "title": f"Pronájem bytu 1+kk {sqm:.0f} m²",
        "transaction_type": "pronajem",
        "disposition": "1+kk",
        "floor_area_sqm": sqm,
        "price_czk": total - fee,
        "total_czk": total,
        "fees_czk": fee,
        "fees_missing": False,
        "price_czk_per_sqm": round(total / sqm),
        "first_seen": first_seen,
        "last_seen": last_seen,
        "gone_at": None,
        "price_history": [{"at": first_seen[:10], "price_czk": total - fee, "total_czk": total}],
    }
    rec.update(extra)
    return rec


def steady_pool(weeks=14, per_week=20, per_sqm=700, from_week=None, then_per_sqm=None):
    """A market that does nothing at all, observed evenly for `weeks`.

    `from_week`/`then_per_sqm` re-price the adverts arriving from that week on,
    without changing how many there are -- which is the only way to test a real
    move, since a jump built by *adding* adverts trips the sample-shift guard
    and would be dismissed exactly as it should be."""
    pool = {}
    for w in range(weeks):
        week = f"2026-W{20 + w:02d}"
        start, _end = report.market.week_bounds(week)
        # Seen once, in its own week. The 30-day window then holds exactly the
        # last four weeks of arrivals, which keeps the sample size flat and the
        # arithmetic checkable by hand.
        stamp = (start + timedelta(days=2)).strftime("%Y-%m-%dT12:00:00Z")
        price = then_per_sqm if (from_week and week >= from_week and then_per_sqm) else per_sqm
        for i in range(per_week):
            rid = f"w{w}-{i}"
            pool[rid] = rental(rid, price, stamp, stamp)
    return pool


BASE_STATE = {"config_changes": [{"at": "2026-01-01T00:00:00Z", "fingerprint": "a"}],
              "filter_weeks": {}, "hard_filters_since": None}

# --- a flat market is a quiet week --------------------------------------- #
pool = steady_pool()
meta = report.build_weekly(pool, dict(BASE_STATE), "2026-W32")
check("nothing moved, so the week is quiet", meta["verdict"], "quiet")
check("...and no reason is given", meta["reasons"], [])
text = report.render_weekly(meta)
check("the first line says so", "Klidný týden" in text.splitlines()[2], True)

# --- our own config change never makes a week 'important' for market reasons #
cfg_state = dict(BASE_STATE)
cfg_state["config_changes"] = [
    {"at": "2026-01-01T00:00:00Z", "fingerprint": "a"},
    {"at": "2026-08-05T00:00:00Z", "fingerprint": "b"},
]
meta_cfg = report.build_weekly(pool, cfg_state, "2026-W32")
check("a week containing our own change is flagged", meta_cfg["verdict"], "important")
market_reasons = [r for r in meta_cfg["reasons"] if "úroveň" in r]
check("...but not as a market move (N-5)", market_reasons, [])
check("...and the text warns explicitly",
      "změnila se naše vlastní konfigurace" in report.render_weekly(meta_cfg).lower(), True)

# --- a genuine move above the noise band is important -------------------- #
moved = steady_pool(from_week="2026-W30", then_per_sqm=900)
meta_moved = report.build_weekly(moved, dict(BASE_STATE), "2026-W32")
check("a real jump is important", meta_moved["verdict"], "important")
check("...and the reason names the move",
      any("úroveň" in r for r in meta_moved["reasons"]), True)
check("...on an unchanged sample size",
      meta_moved["trend_4w"]["sample_shifted"], False)

# --- what the text may never say ----------------------------------------- #
gone = steady_pool()
gone_start, _ = report.market.week_bounds("2026-W32")
gone["sold"] = rental("sold", 700, "2026-07-01T00:00:00Z",
                      gone_start.strftime("%Y-%m-%dT12:00:00Z"),
                      gone_at=gone_start.strftime("%Y-%m-%dT13:00:00Z"),
                      gone_last_price_czk=21000, gone_last_total_czk=24000,
                      since="2026-07-01")
meta_gone = report.build_weekly(gone, dict(BASE_STATE), "2026-W32")
text_gone = report.render_weekly(meta_gone).lower()
check("a departed advert is listed", meta_gone["left_similar_n"], 1)
for forbidden in ("prodáno za", "pronajato za", "prodalo se za", "realizovaná cena"):
    check(f"the text never says {forbidden!r} (N-1)", forbidden in text_gone, False)
check("it says what it actually knows instead",
      "poslední nabídková cena" in text_gone, True)

# --- nothing personal in a public file (R-10.3) -------------------------- #
for forbidden in ("hypoték", "splátk", "pokryje", "kupní cena", "mortgage"):
    check(f"the weekly report carries no {forbidden!r}", forbidden in text_gone, False)

# --- the archive is filed under the week it describes -------------------- #
check("weekly filename is the covered week",
      report.report_path("2026-W32").name, "2026-W32.md")
check("monthly filename is the covered month",
      report.month_report_path("2026-07").name, "2026-07-souhrn.md")

# --- an unusual arrival count is a reason, once there is a baseline ------- #
state_hist = dict(BASE_STATE)
state_hist["weekly_arrived"] = {"2026-W28": 5, "2026-W29": 6, "2026-W30": 4, "2026-W31": 5}
meta_hist = report.build_weekly(pool, state_hist, "2026-W32")
check("20 arrivals against a baseline of ~10 is worth saying",
      any("mimo obvyklých" in r for r in meta_hist["reasons"]), True)

state_thin = dict(BASE_STATE)
state_thin["weekly_arrived"] = {"2026-W31": 10}
meta_thin = report.build_weekly(pool, state_thin, "2026-W32")
check("one prior week is not a baseline",
      any("mimo obvyklých" in r for r in meta_thin["reasons"]), False)

print()
if FAILURES:
    print(f"{len(FAILURES)} case(s) failed: {', '.join(FAILURES)}")
    sys.exit(1)
print("all report cases pass")
