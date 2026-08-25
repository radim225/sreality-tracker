#!/usr/bin/env python3
"""Replay the snapshot archive into the pool.

Without this the pool starts empty and the estimate is worthless for a month
and the trend for three. The archive already holds ~200 snapshots going back to
July, so first seen, last seen and the whole price path can be reconstructed
for every advert that ever appeared -- which is exactly what the 30-day window
and the weekly series need.

What the replay CANNOT reconstruct, and does not pretend to:

* `since` (the portal's own insertion date). It was never stored, so days on
  market stay unknown for adverts that are already gone. Live adverts pick it
  up on the next enriched run.
* the attribute block (furnishing, building condition, commission). Same
  reason. Backfilled records carry `backfilled: true` so the gap is visible in
  the data rather than inferred from a missing key.
* removals. Absence from a snapshot is not evidence of anything (N-7), so
  `gone_at` is only ever set from confirmed removal events in the change log.

Run: python3 backfill_pool.py [--limit N] [--dry-run]
"""
import json
import sys
from pathlib import Path

import market
import pool as poolmod

ROOT = Path(__file__).parent
SNAPSHOTS_DIR = ROOT / "snapshots"
CHANGES_HISTORY = ROOT / "changes_history.json"


def snapshot_paths(limit=None):
    paths = sorted(SNAPSHOTS_DIR.glob("snapshot-*.json"))
    if limit:
        paths = paths[-limit:]
    return paths


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    dry_run = "--dry-run" in sys.argv

    pool = poolmod.load_pool()
    state = poolmod.load_state()
    paths = snapshot_paths(limit)
    if not paths:
        raise SystemExit("v snapshots/ nic není — není co přehrát")

    print(f"Přehrávám {len(paths)} snapshotů...", file=sys.stderr)
    totals = {"new": 0, "updated": 0, "repriced": 0}
    seen_configs = 0
    for i, path in enumerate(paths, 1):
        try:
            snap = json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"  {path.name}: nečitelný JSON, přeskakuji", file=sys.stderr)
            continue
        at = snap.get("generated_at")
        if not at:
            continue
        # Snapshots taken before the fingerprint existed are exactly the ones
        # from the narrow Vysočany-only search, so their absence of a config IS
        # a config: recording it as a distinct one makes the widening on 23. 8.
        # show up as the change it was. Without this the weekly series silently
        # splices a narrow area onto a wide one and the report calls the step a
        # market move (R-6.5, N-5).
        config = snap.get("config") or {"pre_fingerprint": True}
        if poolmod.note_config(state, config, at):
            seen_configs += 1
            print(f"  {at}: změna konfigurace hledání", file=sys.stderr)
        counts = poolmod.update_from_snapshot(pool, snap, changes=None, at=at)
        for key in totals:
            totals[key] += counts[key]
        # Mark what the replay could not know, once, when the record is created.
        for comp in snap.get("comparables", []):
            rec = pool.get(str(comp.get("id")))
            if rec is not None and "backfilled" not in rec:
                rec["backfilled"] = rec.get("since") is None
        if i % 25 == 0 or i == len(paths):
            print(f"  {i}/{len(paths)} · pool {len(pool)} inzerátů", file=sys.stderr)

    # Confirmed removals only. The change log is written after verify_removals()
    # has asked each listing's own page and taken a 404 for an answer, so these
    # are observations rather than absences.
    removed = 0
    if CHANGES_HISTORY.exists():
        for event in json.loads(CHANGES_HISTORY.read_text()):
            if event.get("kind") != "removed":
                continue
            rec = pool.get(str(event.get("id")))
            # The change log is applied after the replay, so an event older than
            # the record's last sighting describes a listing that came back. It
            # is not gone; saying it is would invent a removal.
            if rec is None or rec.get("gone_at"):
                continue
            if (event.get("at") or "") < (rec.get("last_seen") or ""):
                continue
            rec["gone_at"] = event.get("at")
            rec["gone_last_price_czk"] = rec.get("price_czk")
            rec["gone_last_total_czk"] = rec.get("total_czk")
            removed += 1

    print(
        f"Hotovo: {len(pool)} inzerátů v poolu "
        f"(+{totals['new']} nových, {totals['repriced']} změn ceny, "
        f"{removed} potvrzeně odstraněných, {seen_configs} změn konfigurace)",
        file=sys.stderr,
    )
    window = poolmod.window(pool)
    print(f"30denní okno: {len(window)} inzerátů", file=sys.stderr)
    rent = market.level(window, "pronajem", None)
    sale = market.level(window, "prodej", None)
    print(f"  pronájem medián {rent['median']} Kč/m² (n={rent['n']})", file=sys.stderr)
    print(f"  prodej   medián {sale['median']} Kč/m² (n={sale['n']})", file=sys.stderr)

    if dry_run:
        print("--dry-run: nic se neuložilo", file=sys.stderr)
        return
    shards = poolmod.save_pool(pool)
    poolmod.save_state(state)
    print(f"Uloženo do pool/: {', '.join(shards)}", file=sys.stderr)


if __name__ == "__main__":
    main()
