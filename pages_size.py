#!/usr/bin/env python3
"""Kolik z repa publikuje GitHub Pages a jestli se vejde do limitu.

Pages staví web Jekyllem z kořene repa: bere všechno kromě cest začínajících
tečkou nebo podtržítkem a kromě `exclude:` v _config.yml. Publikovaný web smí
mít nejvýš 1 GB (GitHub Pages limits). 27. 9. 2026 měl ~991 MB, z toho
snapshots/ 956 MB, a každý běh přidával ~7 MB -- nasazení dashboardu by za pár
dní přestalo projít a scrape by o tom nevěděl.

    python3 pages_size.py   # vypíše velikost; nad WARN_BYTES ::warning:: pro Actions

Nikdy nekončí chybou: selhání tady nesmí shodit běh, jen ho ohlásit.
"""
import fnmatch
import os
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "_config.yml"
LIMIT_BYTES = 1_000_000_000
# Varování s rezervou: ~0,8 GB je při dnešním tempu týdny, ne hodiny.
WARN_BYTES = 800_000_000


def excluded_entries(path=None):
    """Položky `exclude:` z _config.yml. Jen blokový seznam („  - x"), jiný
    YAML tu není -- vlastní čtení, ať CI nepotřebuje PyYAML."""
    path = Path(path or CONFIG_PATH)
    if not path.exists():
        return []
    entries, inside = [], False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        if not line.startswith((" ", "-")):
            inside = line.strip() == "exclude:"
            continue
        if inside and line.lstrip().startswith("- "):
            entries.append(line.lstrip()[2:].strip().strip("'\""))
    return entries


def is_excluded(rel_path, entries):
    """Jako Jekyll: shoda celé cesty, jejího adresáře nebo glob vzoru."""
    rel = rel_path.replace(os.sep, "/").removeprefix("./")
    for entry in entries:
        e = entry.rstrip("/")
        if rel == e or rel.startswith(e + "/") or fnmatch.fnmatch(rel, entry):
            return True
    return False


def published_bytes(root=None, entries=None):
    """Odhad velikosti publikovaného webu v bajtech (bez vykreslení Markdownu,
    ten je zanedbatelný). Ověřeno proti `jekyll build` 3.10: 35,1 MB proti
    35 106 621 B skutečně vystavěných."""
    root = Path(root or ROOT)
    entries = excluded_entries(root / "_config.yml") if entries is None else entries
    total = 0
    for dirpath, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        dirs[:] = [d for d in dirs
                   if not d.startswith((".", "_"))
                   and not is_excluded(os.path.join(rel_dir, d), entries)]
        for name in files:
            rel = os.path.join(rel_dir, name)
            if name.startswith((".", "_")) or is_excluded(rel, entries):
                continue
            total += os.path.getsize(os.path.join(dirpath, name))
    return total


def main():
    try:
        size = published_bytes()
    except OSError as exc:
        print(f"::warning::velikost Pages nezměřena: {exc}")
        return
    print(f"GitHub Pages publikuje ~{size / 1e6:.1f} MB (limit {LIMIT_BYTES / 1e9:.0f} GB)")
    if size > WARN_BYTES:
        print(f"::warning::GitHub Pages web má ~{size / 1e6:.0f} MB, limit je "
              f"{LIMIT_BYTES / 1e9:.0f} GB -- doplň exclude v _config.yml")


if __name__ == "__main__":
    main()
