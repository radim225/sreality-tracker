#!/usr/bin/env python3
"""Add a listing URL to tracked.json.

Sreality, Bezrealitky and iDNES are accepted. Bezrealitky and iDNES are not
fetched here — scrape.py pairs the id with the comparison row it already has.
Idempotent: re-adding a URL whose id is already tracked is a no-op.
"""
import json
import sys
from pathlib import Path

from listing_url import listing_id, order_tracked

TRACKED_PATH = Path(__file__).parent / "tracked.json"


def add_tracked_url(tracked, url):
    """Return (entries, parsed_id, added). Does not write and does not fetch."""
    parsed_id = listing_id(url)
    if any(t.get("id") == parsed_id for t in tracked):
        return tracked, parsed_id, False
    return order_tracked([*tracked, {"id": parsed_id, "url": url}]), parsed_id, True


def main():
    if len(sys.argv) != 2:
        print("Usage: add_tracked.py <Sreality | Bezrealitky | iDNES listing URL>", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1].strip()
    try:
        parsed_id = listing_id(url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    tracked = json.loads(TRACKED_PATH.read_text()) if TRACKED_PATH.exists() else []
    updated, parsed_id, added = add_tracked_url(tracked, url)
    if not added:
        print(f"Listing {parsed_id} is already tracked", file=sys.stderr)
        return

    TRACKED_PATH.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
    print(f"Added listing {parsed_id} to tracked.json", file=sys.stderr)


if __name__ == "__main__":
    main()
