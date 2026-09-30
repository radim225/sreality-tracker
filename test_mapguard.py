#!/usr/bin/env python3
"""Mapy nekradou kolečko myši (mapguard.py).

Chování (kolečko posouvá stránku, Ctrl/⌘ + kolečko nebo klik do mapy
přibližuje) ověřuje headless Chromium; tady se hlídá, co by se rozbilo
potichu při úpravě stránky:
- ochrana musí být ve skriptu PŘED vznikem první mapy, jinak ji hák
  L.Map.addInitHook mine a mapa zase krade kolečko;
- posluchač musí být capture (dřív než Leaflet) a passive (nesmí blokovat
  posun stránky);
- CSS nápovědy musí na stránce být.

Run: python3 test_mapguard.py
"""
import re
import sys

import mapguard
import scrape

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:58} {got!r}")


js = mapguard.JS
check("hák na všechny mapy", "L.Map.addInitHook" in js, True)
check("wheel posluchač capture + passive", "{ capture: true, passive: true }" in js, True)
check("Ctrl i ⌘ (metaKey) propustí zoom", "ev.ctrlKey || ev.metaKey" in js, True)
check("bez Leafletu nic nespadne", 'typeof L === "undefined"' in js, True)
check("nápověda česky", "Ctrl + kolečkem" in js and "⌘ + kolečkem" in js, True)

src = open(scrape.__file__, encoding="utf-8").read()
check("CSS nápovědy ve stylech stránky", "{mapguard.CSS}" in src, True)
check("JS na začátku skriptu stránky", "js = mapguard.JS + (" in src, True)
# Mapy vznikají až v js_template a v modulech připojených za něj.
check("mapy vznikají až po mapguard (ne v hlavičce)",
      bool(re.search(r"L\.map\(", src.split("js_template = r\"\"\"")[0])), False)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
