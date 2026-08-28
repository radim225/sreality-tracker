#!/usr/bin/env python3
"""Ověří, že odkazy na garáže z posledního snapshotu opravdu vedou na inzerát.

Proč vlastní test: Sreality používá pro týž typ jiný slug ve vyhledávání
(`garazova-stani`) a jiný v detailu (`garazove-stani`). Odkaz se špatným
slugem vrací 404, což se nijak neprojeví — data jsou v pořádku, statistiky
sedí, jen každý odkaz na dashboardu vede na chybovou stránku. Přesně to se
28. 8. stalo a našel to Radim kliknutím, ne test.

Sahá na síť, proto NENÍ v CI před scrapem — pouští se ručně:
    python3 test_garage_links.py [kolik]
"""
import json
import pathlib
import sys

# Stejná session jako scrape.py: urllib tu nemá kořenové certifikáty a každý
# request by skončil na SSL chybě, kterou by bylo snadné splést s 404.
import scrape

SNAP = pathlib.Path(__file__).parent / "latest_snapshot.json"


def status(url):
    try:
        return scrape.SESSION.head(url, timeout=15, allow_redirects=True).status_code
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}"


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    garages = (json.loads(SNAP.read_text()).get("garages") or [])
    if not garages:
        print("Ve snapshotu nejsou žádné garáže — není co ověřovat.")
        return

    # Vzorek napříč oběma kategoriemi a oběma typy transakce, ne prvních N:
    # kdyby byl slug špatně jen u jedné kategorie, prvních N by to minulo.
    buckets = {}
    for g in garages:
        buckets.setdefault((g.get("garage_slug"), g.get("transaction_type")), []).append(g)
    sample = []
    per_bucket = max(1, limit // max(1, len(buckets)))
    for items in buckets.values():
        sample += items[:per_bucket]

    bad = []
    for g in sample:
        # URL se přegeneruje AKTUÁLNÍM kódem, ne bere ze snapshotu: testujeme
        # dnešní pravidlo, ne odkaz, který tam zbyl z minulého běhu.
        url = scrape.parse_garage(
            {"id": g["id"], "name": g.get("title"), "locality": {}, "images": [],
             "categorySubCb": {"name": g.get("garage_kind"), "value": None}},
            g["transaction_type"], g["garage_slug"],
        )["url"]
        g = {**g, "url": url}
        code = status(g["url"])
        ok = code == 200
        if not ok:
            bad.append((g["id"], code, g["url"]))
        print(f"{'PASS' if ok else 'FAIL'}  {code}  {g.get('garage_kind')}/{g['transaction_type']}  {g['url']}")

    print()
    if bad:
        print(f"{len(bad)} z {len(sample)} odkazů nevede na inzerát:")
        for i, code, url in bad:
            print(f"  {i}: HTTP {code}")
        print("\nZkontroluj GARAGE_DETAIL_SLUG ve scrape.py — Sreality má jiný slug")
        print("pro hledání a jiný pro detail.")
        sys.exit(1)
    print(f"všech {len(sample)} odkazů vede na inzerát")


if __name__ == "__main__":
    main()
