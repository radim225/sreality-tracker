#!/usr/bin/env python3
"""The durable pool of adverts: one record per listing ever seen.

The snapshot answers "what is on the market right now", which is the wrong
question for an estimate. Only ~60 rentals of the relevant kind are live at any
moment, but 168 passed through the area in the last 30 days -- and a median of
60 swings ±3-6 % week to week purely on sample noise. So every advert that was
ever seen keeps a record here, with its price path, and the statistics read a
30-day window over the pool instead of the live set.

Records are never deleted. Falling out of the 30-day window only means a record
stops feeding the current medians; its price path stays, because that is what
the monthly summary and the trend are built from.

Sharded by the month a listing was first seen. One 6-7 MB file rewritten every
8 h would be a poor fit for a git-committed store; sharding keeps the churn in
the current month's file, where it belongs.
"""
import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
POOL_DIR = ROOT / "pool"
STATE_PATH = POOL_DIR / "state.json"

# D3: the window the current statistics read. Wide enough that the sample stops
# being noise (168 vs 60 adverts), narrow enough to still describe today.
WINDOW_DAYS = 30

# Copied verbatim from the snapshot record. Everything here is either identity,
# something the statistics filter on, or something the report has to be able to
# show without going back to the portal (the advert may be gone by then).
POOL_FIELDS = (
    # identity
    "url", "source", "title",
    # classification
    "transaction_type", "disposition", "floor_area_sqm", "street", "locality",
    "city_part", "lat", "lon", "dist_km", "pod_harfou",
    # price
    "price_czk", "fees_czk", "fees_missing", "fees_source", "electricity_czk",
    "electricity_estimated", "total_czk", "price_czk_per_sqm", "price_old_czk",
    "deal_pct", "deal_outlier",
    # attributes (see scrape.enrich_comparable)
    "building_condition", "building_condition_name", "is_new_building",
    "building_type", "building_type_name", "energy_rating", "furnished",
    "commission_czk", "tenant_not_pay_commission", "no_commission",
    "cellar", "cellar_area_sqm", "garage", "garage_count", "parking",
    # The three-state view of the same thing. `garage`/`parking` stay because
    # they are what the portal states; these two are what we concluded from it,
    # and only `parking_state` distinguishes "has one, price unknown" from
    # "has one, costs X". Nothing in market.py reads them yet -- they are here
    # so the history exists when something does.
    "parking_state", "parking_price_czk",
    "parking_lots", "balcony", "balcony_area_sqm", "loggia", "loggia_area_sqm",
    "terrace", "terrace_area_sqm", "floor_number", "floors_total", "elevator",
    "ownership", "ownership_name", "views", "edited",
    # times supplied by the portal
    "since",
)


def parse_ts(value):
    """Accepts both the snapshot's "...Z" timestamps and bare "YYYY-MM-DD"
    dates (which is how Sreality states `since`). Returns an aware UTC datetime,
    or None for anything unparseable -- a malformed date must not take a run
    down, it just means that record can't answer time questions."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _day(value):
    dt = parse_ts(value)
    return dt.strftime("%Y-%m-%d") if dt else None


def _shard(first_seen):
    dt = parse_ts(first_seen)
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m")


def load_pool():
    """id (as str) -> record. Ids are ints on Sreality and strings elsewhere;
    JSON keys are always strings, so the pool settles on strings throughout."""
    pool = {}
    if not POOL_DIR.exists():
        return pool
    for path in sorted(POOL_DIR.glob("*.json")):
        if path.name in ("state.json", "weekly.json"):
            continue
        try:
            shard = json.loads(path.read_text())
        except json.JSONDecodeError:
            raise SystemExit(f"pool shard {path.name} is not valid JSON")
        for rec_id, rec in shard.items():
            pool[str(rec_id)] = rec
    return pool


def save_pool(pool):
    POOL_DIR.mkdir(exist_ok=True)
    shards = {}
    for rec_id, rec in pool.items():
        shards.setdefault(_shard(rec.get("first_seen")), {})[rec_id] = rec
    for name, shard in shards.items():
        path = POOL_DIR / f"{name}.json"
        ordered = {k: shard[k] for k in sorted(shard)}
        payload = json.dumps(ordered, ensure_ascii=False, indent=1)
        # Only write when the content actually changed: an untouched month
        # otherwise shows up in every commit as a no-op diff.
        if not path.exists() or path.read_text() != payload:
            path.write_text(payload)
    return sorted(shards)


def load_state():
    if not STATE_PATH.exists():
        return {"config_changes": [], "filter_weeks": {}, "hard_filters_since": None}
    state = json.loads(STATE_PATH.read_text())
    state.setdefault("config_changes", [])
    state.setdefault("filter_weeks", {})
    state.setdefault("hard_filters_since", None)
    return state


def save_state(state):
    POOL_DIR.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def _snapshot_view(comp):
    rec = {}
    for field in POOL_FIELDS:
        if field in comp:
            rec[field] = comp[field]
    return rec


def record_price(rec, at_day, price_czk, total_czk):
    """Put one price sighting into the advert's path.

    Sorted rather than appended, because snapshots do not always arrive in
    order: re-running the backfill after production has moved on -- the obvious
    way to fold in snapshots the pool missed -- replays older ones. An
    out-of-order path breaks `price_at`, which walks the list assuming it is
    sorted, and `price_drops`, which reads its ends.

    Repeats collapse, so the path stays what it claims to be: the points where
    the price actually moved. Returns True when the path gained a point."""
    if price_czk is None or not at_day:
        return False
    history = rec.setdefault("price_history", [])
    before = len(history)
    history.append({"at": at_day, "price_czk": price_czk, "total_czk": total_czk})
    history.sort(key=lambda entry: entry.get("at") or "")
    collapsed = []
    for entry in history:
        if collapsed and collapsed[-1].get("price_czk") == entry.get("price_czk"):
            continue
        collapsed.append(entry)
    rec["price_history"] = collapsed
    return len(collapsed) > before


def update_from_snapshot(pool, snapshot, changes=None, at=None):
    """Fold one snapshot into the pool. Safe to replay in any order, so the
    backfill can be re-run over an archive the pool has partly seen.

    `changes["newly_inactive"]` is the only thing that sets `gone_at`. Absence
    from a snapshot means nothing on its own -- the ward sweep walks ~60
    paginated pages that shift underneath it, and listings routinely slip
    between page boundaries for a run. Only a confirmed 404 counts (N-7)."""
    at = at or snapshot.get("generated_at")
    at_day = _day(at)
    counts = {"new": 0, "updated": 0, "repriced": 0, "gone": 0, "resurrected": 0}

    for comp in snapshot.get("comparables", []):
        rec_id = str(comp.get("id"))
        if not rec_id or rec_id == "None":
            continue
        view = _snapshot_view(comp)
        rec = pool.get(rec_id)
        if rec is None:
            rec = {
                "id": rec_id,
                "first_seen": at,
                "last_seen": at,
                "gone_at": None,
                "price_history": [],
                **view,
            }
            record_price(rec, at_day, rec.get("price_czk"), rec.get("total_czk"))
            pool[rec_id] = rec
            counts["new"] += 1
            continue

        # D4: the record carries the LAST seen price, so a snapshot older than
        # the one already folded in must not overwrite it. It may still fill a
        # gap -- an attribute nobody had read yet -- but never replace an answer
        # a newer snapshot already gave.
        newer = not rec.get("last_seen") or at >= rec["last_seen"]
        if newer:
            rec.update(view)
            rec["last_seen"] = at
            if at < (rec.get("first_seen") or at):
                rec["first_seen"] = at
        else:
            for key, value in view.items():
                if rec.get(key) is None:
                    rec[key] = value
            if at < (rec.get("first_seen") or at):
                rec["first_seen"] = at

        if record_price(rec, at_day, view.get("price_czk"), view.get("total_czk")):
            counts["repriced"] += 1

        if rec.get("gone_at") and newer:
            # It answered again, so the earlier removal was wrong or the advert
            # was re-posted. Say so on the record rather than quietly dropping
            # the fact that we once called it gone. Only a NEWER sighting counts
            # -- an older snapshot showing it alive says nothing about now.
            rec["resurrected_at"] = at
            rec["gone_at"] = None
            counts["resurrected"] += 1
        counts["updated"] += 1

    for comp in (changes or {}).get("newly_inactive", []):
        rec = pool.get(str(comp.get("id")))
        if rec is not None and not rec.get("gone_at"):
            rec["gone_at"] = at
            rec["gone_last_price_czk"] = rec.get("price_czk")
            rec["gone_last_total_czk"] = rec.get("total_czk")
            counts["gone"] += 1

    return counts


def price_at(rec, when):
    """The price this advert was asking on a given date.

    The record itself only carries the latest price (D4), so a retrospective
    median -- last week's, or week 27's -- would otherwise be computed from
    today's numbers. Returns (price_czk, total_czk); either may be None."""
    target = parse_ts(when)
    history = rec.get("price_history") or []
    if not history or target is None:
        return rec.get("price_czk"), rec.get("total_czk")
    chosen = None
    for entry in history:
        entry_at = parse_ts(entry.get("at"))
        if entry_at is None or entry_at <= target:
            chosen = entry
        else:
            break
    if chosen is None:
        # The window opens before the advert's first recorded price. Its first
        # known price is the closest honest answer.
        chosen = history[0]
    return chosen.get("price_czk"), chosen.get("total_czk")


def per_sqm_at(rec, when):
    """Kč/m² as of a date, on the same footing as `price_czk_per_sqm`: all-in
    total for rentals, purchase price for sales."""
    sqm = rec.get("floor_area_sqm")
    if not sqm:
        return None
    price, total = price_at(rec, when)
    value = total if rec.get("transaction_type") == "pronajem" else price
    if value is None:
        value = price
    return round(value / sqm) if value else None


def rent_per_sqm_at(rec, when):
    """Bare rent per m² -- what actually lands on the landlord, as opposed to
    what the tenant pays (§3.5). Sales have no such split, so None."""
    if rec.get("transaction_type") != "pronajem":
        return None
    sqm = rec.get("floor_area_sqm")
    if not sqm:
        return None
    price, _total = price_at(rec, when)
    return round(price / sqm) if price else None


def records_of(pool):
    """Callers hold the pool either as the id->record map or as an already
    filtered list. Both are legitimate, so every consumer accepts both."""
    return list(pool.values()) if isinstance(pool, dict) else list(pool)


def window(pool, days=WINDOW_DAYS, now=None, end=None):
    """Records last seen inside the window (R-4.2).

    `end` moves the window back in time so a past week can be recomputed on
    exactly the definition used live -- that is what makes the weekly series
    comparable with itself instead of with whatever happened to be online."""
    end_dt = parse_ts(end) or parse_ts(now) or datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    out = []
    for rec in records_of(pool):
        seen = parse_ts(rec.get("last_seen"))
        if seen is None:
            continue
        if start_dt <= seen <= end_dt:
            out.append(rec)
        elif seen > end_dt:
            # Seen after the window closes -- but if it was already around
            # before the window opened it was on the market during it, so it
            # belongs in a retrospective view.
            first = parse_ts(rec.get("first_seen"))
            if first is not None and first <= end_dt:
                out.append(rec)
    return out


def days_on_market(rec, now=None):
    """From the portal's own `since` date (R-6.3).

    Never from our own first/last_seen: the search set fluctuates run to run,
    which made the median "visibility" come out at 4 days -- an artefact of
    unstable pagination, not how long anything was actually for sale."""
    since = parse_ts(rec.get("since"))
    if since is None:
        return None
    end = parse_ts(rec.get("gone_at")) or parse_ts(now) or datetime.now(timezone.utc)
    return max(0, (end - since).days)


def price_drops(rec):
    """(count of decreases, total drop in %) over the advert's whole path."""
    history = [h for h in (rec.get("price_history") or []) if h.get("price_czk")]
    if len(history) < 2:
        return 0, 0.0
    drops = sum(
        1 for a, b in zip(history, history[1:])
        if b["price_czk"] < a["price_czk"]
    )
    first, last = history[0]["price_czk"], history[-1]["price_czk"]
    pct = (last - first) / first * 100 if first else 0.0
    return drops, round(pct, 1)


def quantiles(values):
    """p25 / median / p75 with an explicit n. Deliberately returns None rather
    than a number when the sample is under 8 (N-3): a percentile out of three
    adverts gets read as a fact, and the precedent for that already exists in
    this repo."""
    values = sorted(v for v in values if v is not None)
    n = len(values)
    if n < 8:
        return {"n": n, "median": None, "p25": None, "p75": None,
                "min": None, "max": None, "too_small": True}

    def pct(q):
        return values[min(int(n * q), n - 1)]

    return {
        "n": n,
        "median": round(statistics.median(values)),
        "p25": pct(0.25),
        "p75": pct(0.75),
        "min": values[0],
        "max": values[-1],
        "too_small": False,
    }


def note_config(state, config, at):
    """Remember when the shape of the search changed (R-6.5).

    A week that contains one of these cannot be read as market movement: the
    last widening pulled in ~1300 adverts that had been on the market for
    months. The report has to be able to say so rather than draw a step."""
    fingerprint = json.dumps(config, ensure_ascii=False, sort_keys=True)
    changes = state.setdefault("config_changes", [])
    if changes and changes[-1].get("fingerprint") == fingerprint:
        return False
    changes.append({"at": at, "fingerprint": fingerprint})
    return len(changes) > 1


def config_changed_between(state, start, end):
    start_dt, end_dt = parse_ts(start), parse_ts(end)
    for change in state.get("config_changes", [])[1:]:
        at = parse_ts(change.get("at"))
        if at and start_dt and end_dt and start_dt <= at <= end_dt:
            return True
    return False
