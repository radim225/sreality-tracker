#!/usr/bin/env python3
"""Add a sreality.cz listing URL to tracked.json so scrape.py live-tracks it.
Idempotent: re-adding a URL whose id is already tracked is a no-op."""
import json
import re
import sys
from pathlib import Path
from listing_url import listing_id

TRACKED_PATH = Path(__file__).parent / "tracked.json"
ID_RE = re.compile(r"/(\d+)/?$")


def main():
    if len(sys.argv) != 2:
        print("Usage: add_tracked.py <sreality.cz listing URL>", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1].strip()
    try:
        parsed_id = listing_id(url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    tracked = json.loads(TRACKED_PATH.read_text()) if TRACKED_PATH.exists() else []
    if any(t["id"] == parsed_id for t in tracked):
        print(f"Listing {parsed_id} is already tracked", file=sys.stderr)
        return

    tracked.append({"id": parsed_id, "url": url})
    TRACKED_PATH.write_text(json.dumps(tracked, ensure_ascii=False, indent=2) + "\n")
    print(f"Added listing {parsed_id} to tracked.json", file=sys.stderr)


if __name__ == "__main__":
    main()
