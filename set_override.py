#!/usr/bin/env python3
"""Write one listing override into overrides.json.

The payload is JSON: {"id": "...", optional floor_area_sqm, fees_czk,
exclude_from_stats, note}. Missing fields mean "leave the parser's value".
Replacing an existing id overwrites that record; it does not merge field-by-field
so the JSON you send is the whole override.

Reads OVERRIDE_SET when no argument is given — workflow_dispatch JSON has quotes
that a shell argv would mangle.
"""
import json
import os
import sys

import scrape


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("OVERRIDE_SET", "")
    raw = (raw or "").strip()
    if not raw:
        print("Usage: set_override.py '<json>'", file=sys.stderr)
        sys.exit(1)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"override_set is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(payload, dict):
        print("override_set must be a JSON object", file=sys.stderr)
        sys.exit(1)
    try:
        rec = scrape.upsert_override(payload)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(f"Set override for listing {rec['id']}", file=sys.stderr)


if __name__ == "__main__":
    main()
