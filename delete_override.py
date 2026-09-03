#!/usr/bin/env python3
"""Remove one listing override from overrides.json.

Accepts a listing id (Sreality numeric, bez-…, idnes-…). Idempotent: deleting
an id that is not present is a no-op. Does not touch the listing pool.

Reads OVERRIDE_DELETE when no argument is given, matching set_override.py.
"""
import os
import sys

import scrape


def main():
    listing_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("OVERRIDE_DELETE", "")
    listing_id = (listing_id or "").strip()
    if not listing_id:
        print("Usage: delete_override.py <listing id>", file=sys.stderr)
        sys.exit(1)
    removed = scrape.drop_override(listing_id)
    if removed is None:
        print(f"Listing {listing_id} has no override (nothing to remove)", file=sys.stderr)
        return
    print(f"Removed override for listing {listing_id}", file=sys.stderr)


if __name__ == "__main__":
    main()
