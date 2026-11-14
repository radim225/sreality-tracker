#!/usr/bin/env python3
"""Plné snapshoty v GitHub Releases místo v repu (#17, Radim 14. 11. 2026).

Do 14. 11. se každý běh commitoval do `snapshots/` (~10 MB, 6× denně). Repo
rostlo o ~60 MB denně, Action klonovala celou historii a snapshoty čte jen
ruční přehrávání (backfill_pool.py, backfill_gone_archive.py). Teď:

* každý běh nahraje svůj snapshot gzipovaný (~2 MB) do release
  `snapshots-RRRR-MM` (jeden měsíc = jeden release, jeden soubor = jeden běh,
  granularita zůstává stejná jako dřív);
* když upload selže, workflow snapshot commitne postaru (`git add -f`), aby se
  nic neztratilo -- tichá ztráta je horší než větší repo;
* `restore` je stáhne zpět do `snapshots/`, ověří SHA-256 proti digestu, který
  GitHub u assetu uvádí, a rozbalí.

    python3 snapshots_release.py upload snapshots/snapshot-…json   # Actions
    python3 snapshots_release.py restore [--month 2026-10]          # lokálně

Potřebuje `gh` CLI (v Actions s GH_TOKEN, lokálně `gh auth login`).
"""
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent
SNAPSHOTS_DIR = ROOT / "snapshots"
TAG_PREFIX = "snapshots-"
NAME_RE = re.compile(r"^snapshot-(\d{4})(\d{2})\d{2}T\d{6}Z\.json$")
UPLOAD_ATTEMPTS = 3


def repo():
    return os.environ.get("GITHUB_REPOSITORY") or "radim225/sreality-tracker"


def tag_for(filename):
    """snapshot-20261114T181245Z.json -> snapshots-2026-11. Jiný název = chyba:
    soubor, který neznáme, do archivu nepatří."""
    m = NAME_RE.match(Path(filename).name)
    if not m:
        raise ValueError(f"neznámý název snapshotu: {filename}")
    return f"{TAG_PREFIX}{m.group(1)}-{m.group(2)}"


def asset_name(filename):
    return Path(filename).name + ".gz"


def gzip_to(src, dst):
    # mtime=0: stejný snapshot dá vždy stejný .gz (stejný SHA-256).
    with open(src, "rb") as f_in, open(dst, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as f_out:
            shutil.copyfileobj(f_in, f_out)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def gh(*args, check=True):
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def release_assets(tag):
    """{název: {"size", "digest"}} nebo None, když release neexistuje."""
    res = gh("api", f"repos/{repo()}/releases/tags/{tag}", check=False)
    if res.returncode != 0:
        return None
    data = json.loads(res.stdout)
    return {a["name"]: {"size": a.get("size"), "digest": a.get("digest")} for a in data.get("assets", [])}


def ensure_release(tag):
    if release_assets(tag) is not None:
        return
    month = tag[len(TAG_PREFIX):]
    gh("release", "create", tag, "--repo", repo(), "--title", f"Snapshoty {month}",
       "--notes", "Archiv plných snapshotů Sreality trackeru (gzip, jeden soubor = jeden běh). "
                  "Obnova: python snapshots_release.py restore",
       "--latest=false")


def upload(path):
    """Nahraje jeden snapshot. True = je v release a sedí SHA-256."""
    path = Path(path)
    tag = tag_for(path.name)
    with tempfile.TemporaryDirectory() as tmp:
        gz = Path(tmp) / asset_name(path.name)
        gzip_to(path, gz)
        want = "sha256:" + sha256_of(gz)
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            try:
                ensure_release(tag)
                assets = release_assets(tag) or {}
                if assets.get(gz.name, {}).get("digest") == want:
                    print(f"{gz.name} už v {tag} je (stejný SHA-256)")
                    return True
                gh("release", "upload", tag, str(gz), "--repo", repo(), "--clobber")
                got = (release_assets(tag) or {}).get(gz.name, {}).get("digest")
                if got == want:
                    print(f"Snapshot {gz.name} → release {tag} ({gz.stat().st_size // 1024} kB, SHA-256 ověřen)")
                    return True
                print(f"::warning::{gz.name}: digest po uploadu nesedí ({got} ≠ {want})", file=sys.stderr)
            except subprocess.CalledProcessError as exc:
                print(f"::warning::upload {gz.name} pokus {attempt}: {exc.stderr.strip()[:300]}", file=sys.stderr)
            time.sleep(5 * attempt)
    return False


def restore(month=None, dest=SNAPSHOTS_DIR):
    """Stáhne archiv do `dest`. Vrací (staženo, přeskočeno, chyb)."""
    res = gh("release", "list", "--repo", repo(), "--limit", "500", "--json", "tagName")
    tags = sorted(r["tagName"] for r in json.loads(res.stdout) if r["tagName"].startswith(TAG_PREFIX))
    if month:
        tags = [t for t in tags if t == f"{TAG_PREFIX}{month}"]
    dest = Path(dest)
    dest.mkdir(exist_ok=True)
    done = skipped = errors = 0
    for tag in tags:
        assets = release_assets(tag) or {}
        missing = [n for n in sorted(assets) if n.endswith(".json.gz") and not (dest / n[:-3]).exists()]
        skipped += len(assets) - len(missing)
        if not missing:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            gh("release", "download", tag, "--repo", repo(), "--dir", tmp, "--pattern", "*.json.gz")
            for name in missing:
                gz = Path(tmp) / name
                want = assets[name].get("digest")
                if not gz.exists() or (want and want != "sha256:" + sha256_of(gz)):
                    print(f"CHYBA {name}: chybí nebo nesedí SHA-256", file=sys.stderr)
                    errors += 1
                    continue
                with gzip.open(gz, "rb") as f_in, open(dest / name[:-3], "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                done += 1
        print(f"{tag}: staženo {len(missing)}", file=sys.stderr)
    return done, skipped, errors


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("upload", "restore"):
        raise SystemExit(__doc__)
    if argv[0] == "upload":
        ok = all(upload(p) for p in argv[1:])
        return 0 if ok and argv[1:] else 1
    month = argv[argv.index("--month") + 1] if "--month" in argv else None
    done, skipped, errors = restore(month)
    print(f"Hotovo: {done} staženo, {skipped} už bylo v snapshots/, {errors} chyb")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
