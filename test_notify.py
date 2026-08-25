#!/usr/bin/env python3
"""Tests for the phone message.

Three things have to hold every week, and none of them fail loudly:

* the message always opens into the detail (R-8.3) -- an earlier plain-text
  conversion stripped the hrefs and left the word "dashboard" pointing nowhere;
* the mortgage appears only when it is configured, and never anywhere else
  (R-5.6, R-10.1);
* a quiet week still produces a message (R-8.4), because silence and a broken
  pipeline look identical from a phone.

Run: python3 test_notify.py
"""
import sys

import market
import notify

FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}\n        got  {got!r}\n        want {want!r}")
        FAILURES.append(label)


def rental(rid, per_sqm, sqm=30.0, fee=3000, **extra):
    total = round(per_sqm * sqm)
    rec = {
        "id": str(rid), "transaction_type": "pronajem", "disposition": "1+kk",
        "floor_area_sqm": sqm, "price_czk": total - fee, "total_czk": total,
        "fees_czk": fee, "fees_missing": False,
        "price_czk_per_sqm": round(total / sqm),
        "last_seen": "2026-08-24T00:00:00Z", "first_seen": "2026-08-01T00:00:00Z",
        "price_history": [{"at": "2026-08-01", "price_czk": total - fee, "total_czk": total}],
    }
    rec.update(extra)
    return rec


records = [rental(i, 700, furnished="ne" if i % 2 else "ano",
                  is_new_building=bool(i % 3)) for i in range(20)]
estimate = market.rent_estimate(records, as_of="2026-08-24T00:00:00Z", state={},
                                week_key="2026-W34")
META = {
    "week": "2026-W34",
    "verdict": "quiet",
    "reasons": [],
    "estimate": estimate,
    "noise": {"pct": 3.0},
    "trend_4w": None,
    "arrived_similar_n": 3,
    "left_similar_n": 1,
    "config_changed": False,
}

# --- the link is not optional -------------------------------------------- #
plain = notify.to_plain(notify.build_message(META))
check("the report link survives the plain-text conversion",
      "reports/2026-W34.md" in plain, True)
check("...and so does the dashboard link",
      "github.io/sreality-tracker" in plain, True)
check("no markup leaks into the plain text", "<b>" in plain or "<a " in plain, False)
check("the first URL is the write-up itself",
      notify.first_url(notify.build_message(META)).endswith("reports/2026-W34.md"), True)

# --- a quiet week still says something (R-8.4) --------------------------- #
quiet = notify.build_message(META)
check("a quiet week produces a message", bool(quiet.strip()), True)
check("...and names itself quiet on the first line",
      "Klidný týden" in quiet.splitlines()[0], True)

loud = notify.build_message({**META, "verdict": "important",
                             "reasons": ["nájemní úroveň se posunula"]})
check("an important week says so instead",
      "Důležitý týden" in loud.splitlines()[0], True)
check("...and gives the reason", "nájemní úroveň" in loud, True)

# --- the mortgage is opt-in and stays out of everything else ------------- #
check("no payment configured means no coverage line",
      "pokryje" in notify.build_message(META), False)
with_payment = notify.build_message(META, 30000)
check("a configured payment produces one", "pokryje" in with_payment, True)
check("...computed on the bare rent, not the all-in total",
      f"{estimate['profiles']['zarizeny']['rent']['median']:,}".replace(",", " ")
      in with_payment, True)

# --- the payment can be derived from the loan terms ---------------------- #
import os  # noqa: E402  -- only this block touches the environment

os.environ.pop("MORTGAGE_PAYMENT_CZK", None)
os.environ.update({"MORTGAGE_PRINCIPAL_CZK": "5700000", "MORTGAGE_RATE_PCT": "4.7",
                   "MORTGAGE_YEARS": "30"})
payment = notify.mortgage_payment()
check("annuity from principal/rate/years", 29000 < payment < 30000, True)
os.environ["MORTGAGE_PAYMENT_CZK"] = "31000"
check("an explicit payment wins over the terms", notify.mortgage_payment(), 31000)
for key in ("MORTGAGE_PAYMENT_CZK", "MORTGAGE_PRINCIPAL_CZK", "MORTGAGE_RATE_PCT",
            "MORTGAGE_YEARS"):
    os.environ.pop(key, None)
check("nothing configured means no payment", notify.mortgage_payment(), None)

# --- the monthly summary has to reach the phone too ---------------------- #
# Until this existed the summary was written to reports/ and nothing sent it,
# so the one artefact whose whole job is to stop a missed week costing the
# month was the one Radim never saw.
MONTHLY = {
    "month": "2026-07",
    "period": ("01. 07. 2026", "31. 07. 2026"),
    "estimate": estimate,
    "rent_start": {"median": 500, "n": 40}, "rent_end": {"median": 515, "n": 44},
    "rent_move": 3.0,
    "sale_start": {"median": 210000, "n": 30}, "sale_end": {"median": 212000, "n": 31},
    "sale_move": 1.0,
    "sample_shifted": False,
    "arrived_n": 120, "left_n": 90,
    "arrived_similar_n": 7, "left_similar_n": 4,
    "sale_dynamics": {"days_on_market": {"median": 41}, "repriced_share_pct": 18.0},
    "config_changed": False,
}
monthly = notify.build_monthly_message(MONTHLY)
check("the monthly summary names the month on the first line",
      "2026-07" in monthly.splitlines()[0], True)
mplain = notify.to_plain(monthly)
check("the monthly link points at the summary, not a week",
      "reports/2026-07-souhrn.md" in mplain, True)
check("no markup leaks out of the monthly message",
      "<b>" in mplain or "<a " in mplain, False)
check("the monthly message carries the movement counts",
      "120" in mplain and "90" in mplain, True)

# N-5: a move we caused ourselves is not a market move, monthly or weekly.
contaminated = notify.to_plain(
    notify.build_monthly_message({**MONTHLY, "config_changed": True}))
check("a config change suppresses the monthly percentage",
      "3,0 %" in contaminated or "+3.0" in contaminated, False)
check("...and says why instead", "konfigurace" in contaminated, True)
shifted = notify.to_plain(
    notify.build_monthly_message({**MONTHLY, "sample_shifted": True}))
check("a shifted sample suppresses it too", "srovnatelný" in shifted, True)

# The two messages must not be interchangeable -- a renamed field must fail
# loudly rather than render a monthly summary as a week.
try:
    notify.notify(MONTHLY, dry_run=True, kind="ctvrtletni")
    check("an unknown notification kind is refused", "no error", "NotifyError")
except notify.NotifyError:
    check("an unknown notification kind is refused", True, True)

# --- an unconfigured channel warns, it does not crash the run ------------ #
for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC", "NTFY_TOKEN"):
    os.environ.pop(key, None)
check("no channel configured returns None rather than raising",
      notify.notify(META), None)

# --- a public ntfy topic is refused -------------------------------------- #
os.environ["NTFY_TOPIC"] = "nejaky-verejny-topic"
try:
    notify.send_ntfy("x")
    check("a tokenless ntfy topic is refused (R-8.1)", "no error", "NotifyError")
except notify.NotifyError:
    check("a tokenless ntfy topic is refused (R-8.1)", True, True)
os.environ.pop("NTFY_TOPIC", None)

print()
if FAILURES:
    print(f"{len(FAILURES)} case(s) failed: {', '.join(FAILURES)}")
    sys.exit(1)
print("all notify cases pass")
