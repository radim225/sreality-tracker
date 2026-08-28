#!/usr/bin/env python3
"""Stránkování: co se stane, když výsledků během sweepu ubude.

27. 8. spadl celý běh na `search page 5 of pronajem/Libeň: no __NEXT_DATA__
(HTTP 404)`. Nebylo na tom nic rozbitého — Libeň se mezi první a pátou
stránkou zmenšila a stránka, na kterou kód sáhl, přestala existovat.
Sreality na stránku za koncem odpovídá 404, ne prázdným seznamem.

Rozlišení, na kterém to stojí:
  404 na straně 1        = hledání je rozbité, padat (jinak by se celý ward
                           ohlásil jako odstraněný)
  404 dál, něco přečteno = došly výsledky, korektně skončit

Run: python3 test_search_paging.py
"""
import sys

import scrape

failures = []


def check(label, ok):
    if not ok:
        failures.append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


def page_data(page):
    """Stránka s vlastním offsetem — kód má ochranu proti opakovanému offsetu
    (server přestal posouvat), takže mock musí offsety posouvat, jinak by
    netestoval to, co si myslí."""
    return {
        "pagination": {"total": 100, "limit": 22, "offset": (page - 1) * 22},
        "results": [
            {"id": page, "name": "Pronájem bytu 1+kk 30 m²",
             "categorySubCb": {"value": 2}, "locality": {}, "images": [], "priceCzk": 20000}
        ],
    }


def fake_fetch(pages):
    """pages: {strana: (next_data|None, status)}"""
    def _fetch(url, params=None, parse_on_404=False):
        return pages[params["strana"]]
    return _fetch


def run(pages):
    orig_fetch, orig_query = scrape.fetch_next_data, scrape.get_query_data
    scrape.fetch_next_data = fake_fetch(pages)
    scrape.get_query_data = lambda nd, key: (nd, None)
    try:
        return scrape.search_ward("Libeň", "pronajem"), None
    except scrape.TransientFetchError as exc:
        return None, exc
    finally:
        scrape.fetch_next_data, scrape.get_query_data = orig_fetch, orig_query


# --- 404 za koncem po přečtených stránkách: korektní konec --------------- #
found, err = run({1: (page_data(1), 200), 2: (page_data(2), 200), 3: (None, 404)})
check("404 na straně 3 po dvou přečtených nespadne", err is None)
check("vrátí, co stihl přečíst", found is not None and len(found) == 2)

# --- 404 hned na první straně: skutečná chyba, musí spadnout ------------- #
found, err = run({1: (None, 404)})
check("404 na straně 1 spadne", err is not None)
check("chyba pojmenuje stránku", err is not None and "page 1" in str(err))

# --- jiná chyba než 404 spadne i dál v pořadí ---------------------------- #
found, err = run({1: (page_data(1), 200), 2: (None, 500)})
check("HTTP 500 na straně 2 spadne", err is not None)

# --- normální dočtení do konce ------------------------------------------- #
short = {"pagination": {"total": 2, "limit": 22, "offset": 0}, "results": page_data(1)["results"]}
found, err = run({1: (short, 200)})
check("jedna stránka pokrývající celý total projde", err is None and len(found) == 1)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print(f"všech {4 + 2} kontrol prošlo")
