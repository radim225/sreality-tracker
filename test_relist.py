#!/usr/bin/env python3
"""Znovu vložené inzeráty a páry pronájem/prodej (relist.py).

Každý případ je stavěný podle skutečného nálezu z dat 26. 9. 2026: garáž
Na Krocínce smazaná a vložená znovu týž den, deset stejných stání v komplexu
Pod Harfou, dva byty jednoho makléře v Jeseniově se šablonovým popisem, a
tentýž byt na Sreality a iDNES souběžně celé týdny.

Run: python3 test_relist.py
"""
import sys

import relist
import scrape

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:58} {got!r}")


TEXT = ("Nabízíme k prodeji garážové stání v podzemní garáži bytového domu v ulici Na Krocínce. "
        "Stání má plochu 14 m2, vjezd na dálkové ovládání, v garáži je osvětlení a mytí vozu. "
        "Dům je v klidné části Vysočan, pět minut od metra.")
OTHER = ("Pronájem parkovacího stání v novostavbě, stání je v prvním podzemním podlaží, "
         "šíře vhodná i pro SUV, možnost uskladnění pneumatik, k dispozici ihned po podpisu smlouvy.")

old = dict(id=1, transaction_type="prodej", garage_slug="garaze", price_czk=599000,
           usable_area_sqm=14.0, lat=50.1, lon=14.5, street="Na Krocínce",
           description=TEXT, seller_name="REMAX Partner",
           first_seen="2026-08-28T10:00:00Z", gone_at="2026-09-18T10:00:00Z")
new = dict(old, id=2, first_seen="2026-09-18T09:00:00Z", gone_at=None)

check("smazaná a týž den vložená garáž = same", relist.judge(old, new)[0], "same")
check("stejné id není znovuvložení", relist.judge(old, dict(new, id=1)), None)
check("cena o 15 % jinde neprojde", relist.judge(old, dict(new, price_czk=700000)), None)
check("cena o 5 % jinde projde", relist.judge(old, dict(new, price_czk=629000))[0], "same")
check("plocha o 2 m² jinde neprojde", relist.judge(old, dict(new, usable_area_sqm=16.0)), None)
check("200 m daleko neprojde", relist.judge(old, dict(new, lat=50.1018)), None)
check("pronájem vs prodej neprojde", relist.judge(old, dict(new, transaction_type="pronajem")), None)
# Makléři tutéž věc zařadí jednou jako garáž, jindy jako stání.
check("garáž vs garážové stání je jeden druh",
      relist.judge(old, dict(new, garage_slug="garazova-stani"))[0], "same")

# Komplex s identickými stáními: vše sedí kromě popisu -> nic.
check("stejná cena/plocha/místo, jiný popis, jiný prodejce = nic",
      relist.judge(old, dict(new, description=OTHER, seller_name="Jiná RK")), None)
check("jiný popis, stejný prodejce = nic (text se liší, ne chybí)",
      relist.judge(old, dict(new, description=OTHER)), None)
check("popis chybí, stejný prodejce = maybe",
      relist.judge(old, dict(new, description=None))[0], "maybe")
check("popis chybí, jiný prodejce = nic",
      relist.judge(old, dict(new, description=None, seller_name="Jiná RK")), None)

# Časování.
check("nový 60 dní po zmizení = nic",
      relist.judge(old, dict(new, first_seen="2026-11-17T10:00:00Z")), None)
check("souběh 20 dní = dvojí inzerce, ne znovuvložení",
      relist.judge(old, dict(new, first_seen="2026-08-29T10:00:00Z")), None)
check("souběh pár hodin = znovuvložení",
      relist.judge(old, dict(new, first_seen="2026-09-17T20:00:00Z"))[0], "same")
check("starý ještě nezmizel = nic", relist.judge(dict(old, gone_at=None), new), None)

# Byty: patro a remíza podle blízkosti.
flat = dict(id=10, transaction_type="pronajem", disposition="2+kk", price_czk=29000,
            floor_area_sqm=61.0, floor_number=1, lat=50.1, lon=14.5, street="Jeseniova",
            description=TEXT, seller_name="RK", first_seen="2026-09-01T00:00:00Z",
            gone_at="2026-09-22T00:00:00Z")
check("jiné patro = jiný byt", relist.judge(flat, dict(flat, id=11, floor_number=3,
                                                         first_seen="2026-09-21T00:00:00Z", gone_at=None)), None)
flat_b = dict(flat, id=20, price_czk=30000, floor_area_sqm=62.0, floor_number=None)
new_a = dict(flat, id=12, floor_number=None, first_seen="2026-09-21T00:00:00Z", gone_at=None)
new_b = dict(flat_b, id=21, first_seen="2026-09-21T00:00:00Z", gone_at=None)
pairs = {p[0]["id"]: p[1]["id"] for p in relist.match([flat, flat_b], [new_b, new_a])}
check("šablonový popis: páruje se podle ceny a plochy, ne křížem", pairs, {10: 12, 20: 21})

# Řetěz A -> B -> C nese nejstarší datum nabídky.
a = dict(old, id="A", first_seen="2026-08-01T00:00:00Z", gone_at="2026-08-20T00:00:00Z")
b = dict(old, id="B", first_seen="2026-08-20T00:00:00Z", gone_at="2026-09-10T00:00:00Z",
         relist_of=relist.link_record(a, "same", 1.0))
check("řetěz: listed_since z A", relist.link_record(b, "same", 1.0)["listed_since"],
      "2026-08-01T00:00:00Z")

# Pronájem + prodej od téhož prodejce (Olgy Havlové, CENTURY 21 Dream).
sale = dict(id=100, transaction_type="prodej", garage_slug="garaze", usable_area_sqm=14.0,
            lat=50.088589, lon=14.484219, seller_name="CENTURY 21 Dream", price_czk=1160000)
rent = dict(sale, id=101, transaction_type="pronajem", garage_slug="garazova-stani", price_czk=2500)
check("pár pronájem/prodej od téhož prodejce",
      [(s["id"], r["id"]) for s, r in relist.rent_sale_pairs([sale, rent])], [(100, 101)])
check("jiný prodejce = žádný pár",
      relist.rent_sale_pairs([sale, dict(rent, seller_name="REMAX")]), [])
check("bez prodejce = žádný pár",
      relist.rent_sale_pairs([dict(sale, seller_name=None), dict(rent, seller_name=None)]), [])

# Integrace do garáží: link_garage_relists je idempotentní a označí obě strany.
garages = [dict(old), dict(new)]
scrape.link_garage_relists(garages)
scrape.link_garage_relists(garages)
check("nový nese relist_of", garages[1].get("relist_of", {}).get("id"), 1)
check("starý nese relisted_as", garages[0].get("relisted_as", {}).get("id"), 2)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
