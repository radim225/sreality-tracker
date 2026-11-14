#!/usr/bin/env python3
"""Snapshoty v GitHub Releases (#17): názvy, gzip, workflow a pojistka.

Run: python3 test_snapshots_release.py
"""
import gzip
import sys
import tempfile
from pathlib import Path

import snapshots_release as sr

FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}\n        got  {got!r}\n        want {want!r}")
        FAILURES.append(label)


check("měsíční release", sr.tag_for("snapshot-20261114T181245Z.json"), "snapshots-2026-11")
check("cesta se ořízne", sr.tag_for("snapshots/snapshot-20260628T193702Z.json"), "snapshots-2026-06")
check("asset = název + .gz", sr.asset_name("snapshots/snapshot-20261114T181245Z.json"),
      "snapshot-20261114T181245Z.json.gz")
try:
    sr.tag_for("latest_snapshot.json")
    check("cizí soubor = chyba", "no error", "ValueError")
except ValueError:
    check("cizí soubor = chyba", "ValueError", "ValueError")

with tempfile.TemporaryDirectory() as tmp:
    src = Path(tmp) / "snapshot-20261114T181245Z.json"
    src.write_text('{"generated_at": "2026-11-14T18:12:45Z", "x": "ěšč"}', encoding="utf-8")
    a, b = Path(tmp) / "a.gz", Path(tmp) / "b.gz"
    sr.gzip_to(src, a)
    sr.gzip_to(src, b)
    check("gzip je bezztrátový", gzip.decompress(a.read_bytes()), src.read_bytes())
    check("gzip je deterministický (stejný SHA-256)", sr.sha256_of(a), sr.sha256_of(b))

check("upload bez souboru = chyba (exit 1)", sr.main(["upload"]), 1)

wf = Path(".github/workflows/scrape.yml").read_text(encoding="utf-8")
check("workflow nahrává snapshot do Release", "python snapshots_release.py upload" in wf, True)
check("pojistka: při selhání commit postaru", 'git add -f "$snap"' in wf, True)
check("snapshots/ se necommituje celé", "git add snapshots " in wf, False)
check("upload má token", "GH_TOKEN: ${{ github.token }}" in wf, True)
check("scrape klonuje mělce", "fetch-depth: 1" in wf and "fetch-depth: 0" not in wf, True)
check("test běží v CI", "python test_snapshots_release.py" in wf, True)
dev = Path(".github/workflows/developers.yml").read_text(encoding="utf-8")
check("ceníky klonují mělce", "fetch-depth: 1" in dev and "fetch-depth: 0" not in dev, True)

ignore = Path(".gitignore").read_text(encoding="utf-8").splitlines()
check(".gitignore drží snapshots/ mimo repo", "snapshots/" in ignore, True)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED:")
    for item in FAILURES:
        print(f"  {item}")
    sys.exit(1)
print("všechny kontroly prošly")
