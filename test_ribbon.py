#!/usr/bin/env python3
"""Ribbon: výběr žhavých nabídek a bezpečnost toho, co jde na veřejnou stránku.

Radim chtěl nahoře „žhavé nabídky" -- testy hlídají hlavně, že každý důvod
na kartě má oporu v datech (žádné „pod mediánem" u pronájmu bez poplatků,
žádná „sleva" z přehozeného pronájmu na prodej) a že cizí text nerozbije
stránku.

Offline, bez sítě. Run: python3 test_ribbon.py
"""
import json
import os
import tempfile
import re
import shutil
import subprocess
import sys

import ribbon

failures = []
# Náhled se píše do dočasné složky -- pevná cesta z vývojového stroje na CI
# runneru neexistuje a shodila celý běh (26. 9.).
SCRATCH = os.environ.get("RIBBON_PREVIEW_DIR") or tempfile.mkdtemp(prefix="ribbon-")
NOW = "2026-09-26T19:00:00Z"


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:52} {got!r}")


def flat(id, **kw):
    base = {"id": id, "title": f"Byt {id}", "url": f"https://www.sreality.cz/detail/{id}",
            "thumb": "https://img.example/x.jpg", "transaction_type": "prodej",
            "disposition": "2+kk", "floor_area_sqm": 50.0, "price_czk": 6_000_000 + sum(map(ord, str(id))) % 1000,
            "total_czk": None, "deal_pct": 0, "deal_ok": False,
            "deal_outlier": False, "fees_missing": False, "exclude_from_stats": False}
    base.update(kw)
    return base


def ids(out):
    return [it["id"] for it in out]


def by_id(out, id):
    return next((it for it in out if it["id"] == id), None)


# --- výhodné nabídky ---------------------------------------------------- #
out = ribbon.hot_offers([flat(1, deal_pct=-12, deal_ok=True)], [], [], NOW)
check("deal_ok se vybere", ids(out), [1])
check("důvod říká odchylku a dispozici", out[0]["reason"], "−12 % pod mediánem 2+kk")
check("kind deal", out[0]["kind"], "deal")

# Odlehlé (podíl, dražba, překlep v ploše) a ručně vyřazené nejsou nabídky,
# které jde koupit za uvedenou cenu -- ani se zlevněním.
drop_ev = lambda id, old, at="2026-09-26T10:00:00Z": {
    "at": at, "kind": "price_change", "id": id, "old_price_czk": old, "new_price_czk": None}
out = ribbon.hot_offers(
    [flat(2, deal_pct=-60, deal_outlier=True, price_czk=3_000_000),
     flat(3, deal_pct=-20, deal_ok=True, exclude_from_stats=True, price_czk=3_000_000)],
    [drop_ev(2, 3_500_000), drop_ev(3, 3_500_000)], [], NOW)
check("odlehlý ani vyřazený se nevybere", ids(out), [])

# Pronájem bez poplatků: celková cena je podhodnocená, tvrzení „pod
# mediánem" se nesmí objevit -- ani jako vedlejší důvod u zlevnění.
nofee = flat(4, transaction_type="pronajem", price_czk=18_000, total_czk=19_500,
             deal_pct=-25, deal_ok=False, fees_missing=True)
check("pronájem bez poplatků sám o sobě ne",
      ids(ribbon.hot_offers([nofee], [], [], NOW)), [])
out = ribbon.hot_offers([nofee], [drop_ev(4, 20_000)], [], NOW)
check("...se zlevněním ano", ids(out), [4])
check("...ale bez tvrzení o mediánu", "medián" in out[0]["reason"], False)
check("...a nájem snížen, ne zlevněno", out[0]["reason"], "nájem snížen o 10 % (z 20 000 Kč)")
check("...a deal_pct se neposílá", out[0]["deal_pct"], None)
check("...fees_missing jde do karty (hvězdička)", out[0]["fees_missing"], True)

# --- zlevnění ----------------------------------------------------------- #
f = flat(5, price_czk=9_000_000)
check("zlevnění 10 % za 9 h", by_id(ribbon.hot_offers([f], [drop_ev(5, 10_000_000)], [], NOW), 5)["kind"], "drop")
check("zlevnění starší než 72 h ne",
      ids(ribbon.hot_offers([f], [drop_ev(5, 10_000_000, "2026-09-23T10:00:00Z")], [], NOW)), [])
check("zlevnění o 2 % ne",
      ids(ribbon.hot_offers([f], [drop_ev(5, 9_180_000)], [], NOW)), [])
# Skutečný případ z historie: 18 000 → 8 500 000 je přehozený typ obchodu.
check("sleva přes 50 % je chyba dat",
      ids(ribbon.hot_offers([flat(6, price_czk=18_000)], [drop_ev(6, 8_500_000)], [], NOW)), [])
check("když zase zdražil, sleva neplatí",
      ids(ribbon.hot_offers([flat(7, price_czk=10_500_000)], [drop_ev(7, 10_000_000)], [], NOW)), [])
out = ribbon.hot_offers([flat(8, price_czk=18_000_000)],
                        [drop_ev(8, 20_000_000, "2026-09-25T10:00:00Z"),
                         drop_ev(8, 19_000_000, "2026-09-26T10:00:00Z")], [], NOW)
check("dvě zlevnění v okně = jedno od první ceny", out[0]["reason"],
      "zlevněno o 10 % (z 20 000 000 Kč)")
check("string id z jiného portálu se spáruje",
      ids(ribbon.hot_offers([flat("idnes-ab'c\"", price_czk=9_000_000)],
                            [drop_ev("idnes-ab'c\"", 10_000_000)], [], NOW)), ["idnes-ab'c\""])

# --- nové --------------------------------------------------------------- #
new_ev = lambda id, at="2026-09-26T12:00:00Z": {"at": at, "kind": "new", "id": id, "item": {}}
out = ribbon.hot_offers([flat(9, deal_pct=-6)], [new_ev(9)], [], NOW)
check("nové a 6 % pod mediánem", out[0]["reason"], "nové dnes · −6 % pod mediánem 2+kk")
check("nové a jen 2 % pod mediánem ne",
      ids(ribbon.hot_offers([flat(10, deal_pct=-2)], [new_ev(10)], [], NOW)), [])
check("nové před 50 h ne",
      ids(ribbon.hot_offers([flat(11, deal_pct=-6)], [new_ev(11, "2026-09-24T17:00:00Z")], [], NOW)), [])

# --- znovu vložené ------------------------------------------------------ #
rel = lambda verdict, price: {"id": 99, "verdict": verdict, "price_czk": price,
                              "first_seen": "2026-08-01", "listed_since": "2026-08-01",
                              "url": "https://www.sreality.cz/detail/99"}
out = ribbon.hot_offers([flat(12, price_czk=5_400_000, relist_of=rel("same", 6_000_000))], [], [], NOW)
check("znovu vloženo levněji", out[0]["kind"], "relist")
check("...s dřívější cenou", out[0]["reason"], "znovu vloženo o 10 % levněji (dřív 6 000 000 Kč)")
check("verdikt maybe není fakt",
      ids(ribbon.hot_offers([flat(13, price_czk=5_400_000, relist_of=rel("maybe", 6_000_000))], [], [], NOW)), [])

# --- garáže ------------------------------------------------------------- #
def garage(id, price, **kw):
    g = {"id": id, "title": f"Stání {id}", "url": f"https://www.sreality.cz/g/{id}",
         "transaction_type": "pronajem", "garage_kind": "Garážové stání", "price_czk": price,
         "usable_area_sqm": 12.0 + id % 7, "street": "X", "gone_at": None,
         "price_old_czk": None, "features": []}
    g.update(kw)
    return g

gs = [garage(100 + i, 2000 + 100 * i, price_old_czk=3000 + 100 * i) for i in range(6)]
out = ribbon.hot_offers([], [], gs, NOW)
check("nejvýš tři garáže", len(out), 3)
check("garáž se neotevírá v modalu", all(it["is_garage"] for it in out), True)
moto = garage(200, 500, features=["⚠ jen pro motocykl"])
few = [garage(300 + i, 2500) for i in range(3)] + [moto]
check("motorka ani skupina pod 4 nejsou nejlevnější",
      ids(ribbon.hot_offers([], [], few, NOW)), [])
four = [garage(400 + i, 2000 + 100 * i) for i in range(4)]
out = ribbon.hot_offers([], [], four + [garage(410, 900, gone_at="2026-09-20T00:00:00Z")], NOW)
check("nejlevnější ze čtyř; zmizelá se nepočítá", ids(out), [400])
check("...s důvodem", out[0]["reason"], "nejlevnější stání k pronájmu v oblasti (z 4)")

# --- diverzifikace, dedupe, determinismus ------------------------------- #
vys = [flat(1000 + i, deal_pct=-30, deal_ok=True, floor_area_sqm=40 + i) for i in range(20)]
jin = [flat(2000 + i, deal_pct=-10, deal_ok=True, area="jinonice", floor_area_sqm=40 + i) for i in range(10)]
out = ribbon.hot_offers(vys + jin, [], [], NOW)
areas = [it["area"] for it in out]
check("limit 14", len(out), 14)
check("Vysočany nejvýš 60 % (9 ze 14)", areas.count("vysocany"), 9)
out = ribbon.hot_offers(vys + jin[:2], [], [], NOW)
check("málo Jinonic: volná místa doplní Vysočany", [it["area"] for it in out].count("vysocany"), 12)
check("chybějící area = vysocany", ribbon.hot_offers(vys[:1], [], [], NOW)[0]["area"], "vysocany")
dup = [flat(3000, deal_pct=-15, deal_ok=True, price_czk=5_000_000),
       flat(3001, deal_pct=-15, deal_ok=True, price_czk=5_000_000)]
check("tentýž byt dvakrát jen jednou", len(ribbon.hot_offers(dup, [], [], NOW)), 1)
check("stejná data, stejný výběr",
      ids(ribbon.hot_offers(vys + jin, [], [], NOW)) == ids(ribbon.hot_offers(list(reversed(vys + jin)), [], [], NOW)), True)

# --- bezpečnost dat ----------------------------------------------------- #
evil = flat(4000, deal_pct=-20, deal_ok=True, url="javascript:alert(1)", thumb="http://x/y.jpg",
            title='</script><img src=x onerror=alert(1)>')
out = ribbon.hot_offers([evil], [], [], NOW)
check("javascript: URL zahozena", out[0]["url"], None)
check("http thumb zahozen", out[0]["thumb"], None)
json.dumps(out)  # musí jít serializovat bez výjimky

nav = ribbon.ribbon_html([("dealsCard", "Nejlepší"), ('x"><script>', "<b>zlé</b>")])
check("ribbon_html escapuje id", '"><script>' in nav, False)
check("ribbon_html escapuje popisek", "<b>zlé</b>" in nav, False)
check("ribbon_html má lepící nav i pás", ("ribNav" in nav, "hotList" in nav), (True, True))
js = ribbon.ribbon_js()
check("JS bez inline handlerů", "onclick=" in js or "onerror=" in js, False)
check("JS bez nenahrazeného placeholderu", "__RIB_" in js, False)
check("CSS exportuje --rib", "--rib" in js and "--hdr" in ribbon.ribbon_css(), True)

# --- kouřový test stránky ----------------------------------------------- #
page = ribbon.preview_html(out + ribbon.hot_offers(vys[:3], [], gs, NOW))
check("titulek z dat nezavře <script>", "</script><img" in page, False)
os.makedirs(SCRATCH, exist_ok=True)
with open(os.path.join(SCRATCH, "ribbon_preview.html"), "w", encoding="utf-8") as fh:
    fh.write(page)
node = shutil.which("node")
if node:
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    path = os.path.join(SCRATCH, "ribbon_preview.js")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(scripts))
    res = subprocess.run([node, "--check", path], capture_output=True, text=True)
    check("node --check na JS stránky", res.returncode, 0)
    if res.returncode:
        print(res.stderr)
else:
    print("SKIP  node není k dispozici, JS se nekontroluje")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
