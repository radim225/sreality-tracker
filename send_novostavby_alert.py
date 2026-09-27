#!/usr/bin/env python3
"""Odešle připravený alert až po úspěšném pushi snapshotu."""

import os
from pathlib import Path

import novostavby


def main():
    path = os.environ.get("NOVOSTAVBY_ALERT_PATH")
    if not path:
        raise SystemExit("NOVOSTAVBY_ALERT_PATH není nastavená")
    novostavby.send_staged_alert(Path(path),
                                dry_run=os.environ.get("NOVOSTAVBY_ALERT_DRY_RUN") == "1")


if __name__ == "__main__":
    main()
