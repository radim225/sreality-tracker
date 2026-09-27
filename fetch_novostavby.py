#!/usr/bin/env python3
"""Doběhne JEN kolekci novostaveb (bez celého scrape, ~15–60 requestů) a
zapíše ji do snapshotu. Na lokální ověření a na dry-run alertu.

    python3 fetch_novostavby.py SNAPSHOT.json            # alert jen vypíše
    python3 fetch_novostavby.py SNAPSHOT.json --send     # alert opravdu pošle
    python3 render_latest.py SNAPSHOT.json               # karta z výsledku

Snapshot se zadává vždy výslovně: zápis do latest_snapshot.json by v
pracovní kopii nechal ručně vyrobená data, která nemají co dělat v commitu
(ten soubor commituje jen Action).
"""
import argparse
import json
from pathlib import Path

import novostavby
import report
import scrape


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("snapshot", type=Path, help="snapshot k načtení a přepsání")
    ap.add_argument("--send", action="store_true", help="alert opravdu odeslat (jinak dry-run)")
    args = ap.parse_args()

    snapshot = json.loads(args.snapshot.read_text()) if args.snapshot.exists() else {}
    records, events, meta = scrape.fetch_novostavby(snapshot)
    snapshot["novostavby"] = records
    snapshot["novostavby_config"] = novostavby.fingerprint()
    snapshot["novostavby_meta"] = meta
    now = meta["last_run_at"]
    snapshot["novostavby_stats"] = novostavby.compute_stats(records, now)
    args.snapshot.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    print(f"Zapsáno do {args.snapshot}")
    channel = novostavby.send_alert(events, now, dry_run=not args.send, dashboard_url=report.PAGES_URL)
    print(f"Alert: {channel or 'nic k odeslání (nebo žádný kanál)'} · událostí {len(events)}")


if __name__ == "__main__":
    main()
