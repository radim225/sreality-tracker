#!/usr/bin/env python3
"""Remove a sreality.cz listing from tracked.json so scrape.py stops live-tracking it.
Accepts either a listing URL or a bare numeric id.
Idempotent: removing an id that is not tracked is a no-op."""
import json
import re
import sys
from pathlib import Path
from listing_url import listing_id

TRACKED_PATH = Path(__file__).parent / "tracked.json"
ID_RE = re.compile(r"/(\d+)/?$")


def main():
    if len(sys.argv) != 2:
        print("Usage: remove_tracked.py <sreality.cz listing URL | listing id>", file=sys.stderr)
        sys.exit(1)
    arg = sys.argv[1].strip()
    try:
        parsed_id = listing_id(arg, allow_id=True)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    tracked = json.loads(TRACKED_PATH.read_text()) if TRACKED_PATH.exists() else []
    kept = [t for t in tracked if t.get("id") != parsed_id]
    if len(kept) == len(tracked):
        print(f"Listing {parsed_id} is not tracked (nothing to remove)", file=sys.stderr)
        return

    TRACKED_PATH.write_text(json.dumps(kept, ensure_ascii=False, indent=2) + "\n")
    print(f"Removed listing {parsed_id} from tracked.json", file=sys.stderr)


if __name__ == "__main__":
    main()
