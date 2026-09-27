#!/usr/bin/env python3
"""Co GitHub Pages publikuje (_config.yml, pages_size.py).

Hlídá dvě chyby, které by nikdo neviděl hned: snapshots/ zase na webu (limit
1 GB, nasazení by přestalo projít), a naopak exclude, který by z webu vyřadil
soubor, který stránka za běhu čte nebo na který odkazuje README -- dashboard by
se nasadil a tiše by mu chyběla data.

Run: python3 test_pages.py
"""
import re
import sys
import tempfile
from pathlib import Path

import pages_size

ROOT = Path(__file__).parent
failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:58} {got!r}")


entries = pages_size.excluded_entries()
check("_config.yml vyřazuje snapshots/", "snapshots/" in entries, True)
check("snímek není na webu",
      pages_size.is_excluded("snapshots/snapshot-20260927T142759Z.json", entries), True)

# Co stránka čte za běhu: fetch("soubor") v šabloně dashboardu.
fetched = sorted(set(re.findall(r'fetch\("([^":/]+)"', (ROOT / "scrape.py").read_text(encoding="utf-8"))))
check("dashboard něco za běhu čte (regex nezastaral)", "gone_archive.json" in fetched, True)
for name in fetched:
    check(f"čtené za běhu zůstává: {name}", pages_size.is_excluded(name, entries), False)

# Na co odkazuje README přes Pages.
readme = (ROOT / "README.md").read_text(encoding="utf-8")
linked = sorted(set(re.findall(r"github\.io/sreality-tracker/([^)\s>]+)", readme)))
for name in linked:
    check(f"odkaz z README zůstává: {name}", pages_size.is_excluded(name, entries), False)
for name in ("index.html", "dashboard.html"):
    check(f"stránka zůstává: {name}", pages_size.is_excluded(name, entries), False)

# Pravidla shody jako Jekyll.
check("adresář bez lomítka", pages_size.is_excluded("pool/2026-09.json", ["pool"]), True)
check("prefix jména není adresář",
      pages_size.is_excluded("snapshots_old.json", ["snapshots/"]), False)
check("glob", pages_size.is_excluded("changes_log.jsonl", ["*.jsonl"]), True)

# Čtení _config.yml: jen blok exclude, komentáře pryč.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "_config.yml").write_text(
        "# komentář\ntitle: x\nexclude:\n  - snapshots/  # velké\n  - \"*.log\"\ninclude:\n  - .well-known\n",
        encoding="utf-8")
    check("exclude z YAML", pages_size.excluded_entries(root / "_config.yml"), ["snapshots/", "*.log"])
    check("bez _config.yml nic", pages_size.excluded_entries(root / "chybi.yml"), [])

    # Velikost: tečka/podtržítko a exclude se nepočítají.
    (root / "index.html").write_bytes(b"x" * 10)
    (root / "snapshots").mkdir()
    (root / "snapshots" / "a.json").write_bytes(b"x" * 100)
    (root / ".git").mkdir()
    (root / ".git" / "pack").write_bytes(b"x" * 1000)
    (root / "run.log").write_bytes(b"x" * 7)
    check("velikost jen publikovaného", pages_size.published_bytes(root), 10)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
