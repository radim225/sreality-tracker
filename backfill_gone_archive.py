#!/usr/bin/env python3
"""Jednorázové naplnění gone_archive.json z historie. Spouští se lokálně
před mergem, ne ve workflow.

Odkud se berou data: changes_log.jsonl zná každé zmizení („removed", s časem
a id), ale jen ve štíhlé podobě -- bez popisu a fotek. Plný záznam má
poslední snapshot, ve kterém inzerát ještě byl (v `comparables`, případně
v `pending_removal`, kde čekal na potvrzení zmizení).

Snapshotů je 336 a mají dohromady ~860 MB, takže se čtou od nejnovějšího
a každý nejvýš jednou: v jednom souboru se hledají všechna id, která v tu
chvíli ještě hledaná jsou a zmizela až po něm. Jakmile jsou všechna nalezená,
čtení končí.

Run: python3 backfill_gone_archive.py [--days 60] [--dry-run]
"""
import argparse
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

import gone_archive as ga
import pool as pool_mod
import relist

ROOT = Path(__file__).parent
LOG_PATH = ROOT / "changes_log.jsonl"
SNAPSHOTS_DIR = ROOT / "snapshots"
LATEST_PATH = ROOT / "latest_snapshot.json"


def snapshot_time(path):
    # snapshot-20260926T192118Z.json -> 2026-09-26T19:21:18Z
    s = path.stem.split("-", 1)[1]
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[9:11]}:{s[11:13]}:{s[13:15]}Z"


def last_removals(days, now):
    """{str id: čas posledního zmizení} za posledních `days` dní. Inzerát,
    který zmizel víckrát (88 id z 902), se bere podle posledního zmizení --
    to je ten stav, který má archiv popsat."""
    since = now - timedelta(days=days)
    out = {}
    with LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            ev = json.loads(line)
            if ev.get("kind") != "removed":
                continue
            at = relist._ts(ev.get("at"))
            if at is None or at < since:
                continue
            key = str(ev["id"])
            if key not in out or ev["at"] > out[key]:
                out[key] = ev["at"]
    return out


def find_records(removals):
    """{str id: plný záznam} z nejnovějšího snapshotu, který je starší nebo
    stejně starý jako zmizení a inzerát obsahuje."""
    wanted = dict(removals)
    found = {}
    paths = sorted(SNAPSHOTS_DIR.glob("snapshot-*.json"), reverse=True)
    oldest = min(removals.values()) if removals else None
    read = 0
    t0 = time.time()
    for path in paths:
        if not wanted:
            break
        when = snapshot_time(path)
        # Snapshot musí být nejpozději z okamžiku zmizení; hledají se jen id,
        # která zmizela až po něm.
        here = {k for k, at in wanted.items() if when <= at}
        if not here:
            continue
        with path.open(encoding="utf-8") as f:
            snap = json.load(f)
        read += 1
        for rec in snap.get("comparables", []) + snap.get("pending_removal", []):
            key = str(rec.get("id"))
            if key in here and key not in found:
                rec = {k: v for k, v in rec.items() if k not in ("missing_since", "removed_since")}
                found[key] = rec
                del wanted[key]
        if read % 10 == 0:
            print(f"  … {read} snapshotů ({path.name}), nalezeno {len(found)}, "
                  f"zbývá {len(wanted)}, {time.time() - t0:.0f} s", file=sys.stderr)
        if oldest and when < oldest and not here:
            break
    return found, wanted, read


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = ga._now()
    removals = last_removals(args.days, now)
    print(f"zmizení v changes_log za {args.days} dní: {len(removals)} různých id")

    found, missing, read = find_records(removals)
    print(f"plný záznam nalezen: {len(found)}, nenalezen: {len(missing)} "
          f"(přečteno {read} snapshotů)")

    pool = pool_mod.load_pool()
    archive = ga.load_archive()
    before = len(archive)
    gone = [{**rec, "removed_since": removals[key]} for key, rec in found.items()]
    added = ga.add_gone(archive, gone, ga._iso(now), pool)
    print(f"archiv: {before} -> {len(archive)} záznamů (přidáno {added})")

    with LATEST_PATH.open(encoding="utf-8") as f:
        latest = json.load(f)
    live = latest.get("comparables", [])
    returned = ga.mark_returned(archive, [c["id"] for c in live], at=latest.get("generated_at"))
    print(f"vrátilo se pod stejným id (returned_at): {len(returned)}")

    # Kandidáti: živé inzeráty (first_seen z poolu) a zároveň záznamy archivu
    # -- znovuvložený inzerát mohl sám zase zmizet. Jedno volání přes oboje,
    # aby řetěz A -> B -> C vyšel jako dva páry, ne jako A -> C.
    cands = [{**c, "first_seen": (pool.get(str(c["id"])) or {}).get("first_seen")} for c in live]
    cands = [c for c in cands if c.get("first_seen")]
    cands += [dict(e) for e in archive.values() if not e.get("returned_at")]
    # link_relists pouští ke párování jen záznamy zmizelé za posledních 45
    # dní od `now`. Ve workflow je to správně (starší už nový protějšek mít
    # nemůže), ale backfill sahá 60 dní zpět a inzerát zmizelý před 50 dny
    # se mohl znovu vložit za týden. Proto se volá jednou, s `now` = nejstarší
    # zmizení: způsobilé je tak všechno a okno 45 dní hlídá relist.timing_ok
    # mezi zmizením a objevením nového, což je ta skutečná podmínka.
    earliest = min(e["gone_at"] for e in archive.values() if e.get("gone_at"))
    links = ga.link_relists(archive, cands, earliest)
    print(f"páry znovuvložení: {len(links)} "
          f"(same {sum(1 for l in links.values() if l['verdict'] == 'same')}, "
          f"maybe {sum(1 for l in links.values() if l['verdict'] == 'maybe')})")

    by_id = {str(c["id"]): c for c in cands}
    for new_key, link in sorted(links.items(), key=lambda kv: kv[1]["gone_at"] or ""):
        old = archive[str(link["id"])]
        new = by_id.get(new_key, {})
        print(f"  {link['verdict']:5} sim={link['text_similarity']}  "
              f"{old.get('id')} ({old.get('street')}, {old.get('price_czk')} Kč, zmizel {old.get('gone_at', '')[:10]}) "
              f"-> {new.get('id')} ({new.get('street')}, {new.get('price_czk')} Kč, "
              f"od {str(new.get('first_seen', ''))[:10]}{', taky zmizel' if new_key in archive else ''})"
              f"  | {old.get('title')}")

    if args.dry_run:
        print("dry-run: nic se nezapsalo")
        return
    ga.save_archive(archive)
    size = ga.ARCHIVE_PATH.stat().st_size
    print(f"zapsáno {ga.ARCHIVE_PATH.name}: {len(archive)} záznamů, {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
