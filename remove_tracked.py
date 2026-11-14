#!/usr/bin/env python3
"""Remove a listing from tracked.json.

Accepts a Sreality, Bezrealitky or iDNES URL, a bare Sreality id, or a
comparison id (`bez-<id>`, `idnes-<hash>`). Idempotent: removing an id that
is not tracked is a no-op. Does not fetch.
"""
import json
import sys
from pathlib import Path

from listing_url import listing_id

TRACKED_PATH = Path(__file__).parent / "tracked.json"


def remove_tracked_id(tracked, value):
    """Return (entries, parsed_id, removed). Does not write and does not fetch."""
    parsed_id = listing_id(value, allow_id=True)
    kept = [t for t in tracked if t.get("id") != parsed_id]
    return kept, parsed_id, len(kept) != len(tracked)


def main():
    if len(sys.argv) != 2:
        print(
            "Usage: remove_tracked.py <listing URL | Sreality id | bez-<id> | idnes-<hash>>",
            file=sys.stderr,
        )
        sys.exit(1)
    arg = sys.argv[1].strip()
    try:
        parsed_id = listing_id(arg, allow_id=True)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    tracked = json.loads(TRACKED_PATH.read_text()) if TRACKED_PATH.exists() else []
    kept, parsed_id, removed = remove_tracked_id(tracked, arg)
    if not removed:
        print(f"Listing {parsed_id} is not tracked (nothing to remove)", file=sys.stderr)
        return

    TRACKED_PATH.write_text(json.dumps(kept, ensure_ascii=False, indent=2) + "\n")
    print(f"Removed listing {parsed_id} from tracked.json", file=sys.stderr)


if __name__ == "__main__":
    main()
