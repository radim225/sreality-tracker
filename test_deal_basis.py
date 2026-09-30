#!/usr/bin/env python3
"""Výhodnost podle typu domu a plocha bytu z popisu (deal_basis.py).

Hlídá, co by se pokazilo potichu:
- Kč/m² z plochy, kterou portál nafoukl o terasu -> byt vypadá levnější;
- pokoj, zahrada nebo „0,5 m² a garážové stání“ přečtené jako plocha bytu;
- opravená plocha, která se keší vrátí jako „údaj portálu“ a smazaný override,
  který zůstane viset;
- panelák vydávaný za výhodnou novostavbu a tichý přechod na široký medián.

Texty v testech jsou zkrácené formulace z inzerátů, na kterých se parser
30. 9. ručně ověřoval (bez kontaktů).

Run: python3 test_deal_basis.py
"""
import sys

import deal_basis as D
import ribbon
import scrape

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:62} {got!r}")


def area(desc, portal, **kw):
    return D.check_area({"floor_area_sqm": portal, "description": desc, **kw})


def status(desc, portal, **kw):
    r = area(desc, portal, **kw)
    return r and r["status"]


# --- plocha: opravy ------------------------------------------------------- #
r = area("Krásný byt 2+kk o velikosti 43 m² s prostornou terasou 29 m², "
         "garážovým stáním 13 m² a sklepem 4 m² v novostavbě.", 76)
check("portál 76 = byt 43 + terasa 29 + sklep 4", (r["status"], r["flat_sqm"]), ("corrected", 43.0))
check("...poznámka pro stránku", D.area_note(r),
      "portál 76 m² vč. terasy 29 m², sklepa 4 m²; Kč/m² z plochy bytu 43 m²")
check("byt 50 m² + lodžie 13 m² v závorce",
      area("dispozičně jako 1kk 63m2 (byt 50m2 + lodžie 13m2)", 63)["flat_sqm"], 50.0)
check("celková 59 (byt 53,2 + balkon 5,8), sklep z parametru",
      area("byt 2+kk o celkové užitné ploše 59 m² (byt 53,2 m² + balkon 5,8 m²)", 61,
           cellar_area_sqm=2)["flat_sqm"], 53.2)
check("číslo před přílohou: „74 m² + 7 m² lodžie“",
      area("Nabízím k prodeji světlý byt 3+1 o velikosti 74 m² + 7 m² zasklené lodžie.", 81)["flat_sqm"], 74.0)
check("výměře (ř) i lodžie o výměře",
      area("byt s podlahovou plochou 56,1 m². K bytu náleží zasklená lodžie o výměře 17,1 m².", 73)["flat_sqm"], 56.1)
check("parkovacím stáním (7. pád)",
      area("byt o velikosti 146,8 m² s lodžií 8,1 m², sklepem 5,7 m² a parkovacím stáním 13,8 m²", 166)["status"],
      "corrected")

# --- plocha: shoda, bez tvrzení, rozpor ---------------------------------- #
check("rozdíl pod 8 % není rozpor (72 vs 76)",
      status("Nabízíme k prodeji nový byt 3+kk o celkové výměře 76m2.", 72), "ok")
check("desc_incl: popis 38,9 = portál 31 + balkon 7,3",
      status("Byt o podlahové ploše 38,9 m² včetně balkonu 7,3 m².", 31), "desc_incl")
check("nevysvětlený rozpor 60 vs 71,15",
      status("Byt disponuje podlahovou plochou 71,15 m². Balkon: 4,52 m²", 60), "mismatch")
check("...poznámka s přesným číslem z popisu",
      D.area_note(area("Byt disponuje podlahovou plochou 71,15 m².", 60)),
      "plocha nesedí: portál 60 m², popis 71,15 m² — mimo medián a výhodné nabídky")
check("bez plochy v popisu = None", status("Pěkný byt v klidné lokalitě.", 50), None)
check("pokoj z minulé věty není byt („Má příjemnou velikost 21 m²“)",
      status("Obývací pokoj je prostorný. Má příjemnou velikost cca 21m2.", 69), None)
check("předzahrádka (á) není byt",
      status("vlastní předzahrádkou o velikosti 18,2 m²", 40), None)
check("zahrada 235 m² u bytu není plocha bytu",
      status("k bytu náleží část oplocené zahrady v osobním vlastnictví o ploše 235 m2", 97), None)
check("skladovací prostor není byt",
      status("přímý přístup z bytu do podzemního skladovacího prostoru o velikosti 18 m²", 63), None)
check("„sklep 6 m² a garážové stání“ -- 6 m² není stání",
      [e for e in D.extras_from({}, "K bytu náleží sklep o velikosti 6 m² a garážové stání.")],
      [("sklep", 6.0)])
check("předsíň 9,10 m² a komora 1,35 m² -- předsíň není příloha",
      ("sklep", 9.1) in D.extras_from({}, "předsíň 9,10 m² a komora 1,35 m²"), False)

# --- plocha: na listingu, obnova přes keš, override ------------------------ #
DESC = "Byt 2+kk o velikosti 43 m² s terasou 29 m² a sklepem 4 m²."
lst = {"id": 1, "source": "sreality", "transaction_type": "prodej", "price_czk": 8_600_000,
       "floor_area_sqm": 76.0, "description": DESC}
check("apply: corrected", D.apply_area_check(lst), "corrected")
scrape.recompute_listing_costs(lst)
check("...plocha bytu a Kč/m² z ní", (lst["floor_area_sqm"], lst["price_czk_per_sqm"]), (43.0, 200_000))
check("...portál zapamatovaný", (lst["floor_area_portal_sqm"], lst["floor_area_source"]), (76.0, "popis"))
# Další běh: floor_area_sqm přišlo z keše už opravené.
cached = {k: lst[k] for k in ("id", "source", "transaction_type", "price_czk", "floor_area_sqm",
                              "floor_area_portal_sqm", "description", "price_czk_per_sqm")}
check("restore vrátí portál (Sreality)", (scrape.restore_portal_areas([cached]), cached["floor_area_sqm"]), (1, 76.0))
check("...a přepočte Kč/m²", cached["price_czk_per_sqm"], round(8_600_000 / 76))
check("...kontrola pak opraví znovu, stejně", (D.apply_area_check(cached), cached["floor_area_sqm"]), ("corrected", 43.0))
# iDNES / Bezrealitky: plocha je čerstvá z karty, keš nese jen značku.
idn = {"id": "idnes-x", "source": "idnes", "transaction_type": "prodej", "price_czk": 7_600_000,
       "floor_area_sqm": 80.0, "floor_area_portal_sqm": 76.0, "price_czk_per_sqm": 200_000}
check("restore u iDNES nepřepíše čerstvou plochu z karty",
      (scrape.restore_portal_areas([idn]), idn["floor_area_sqm"], idn["price_czk_per_sqm"]), (1, 80.0, 95_000))
# Override: ruční plocha vyhrává a po smazání overridu se vrátí portál.
ov = {"id": 2, "source": "sreality", "transaction_type": "prodej", "price_czk": 6_000_000,
      "floor_area_sqm": 60.0, "description": "Byt disponuje podlahovou plochou 71,15 m²."}
scrape.check_flat_areas([ov])
check("rozpor před overridem", ov.get("area_mismatch"), True)
scrape.apply_overrides([ov], {"2": {"floor_area_sqm": 71.15}})
check("override zruší rozpor a zapamatuje portál",
      (ov.get("area_mismatch"), ov["floor_area_sqm"], ov["floor_area_portal_sqm"]), (None, 71.15, 60.0))
scrape.restore_portal_areas([ov])
scrape.apply_overrides([ov], {})
check("smazaný override: zpět plocha portálu", ov["floor_area_sqm"], 60.0)
title_area = {"floor_area_sqm": 50.0, "floor_area_source": "title", "description": "podlahová plocha 71 m²"}
check("plochu z titulku kontrola nepřepisuje", D.apply_area_check(title_area), None)

# --- třída domu ---------------------------------------------------------- #
Y = 2026
check("štítek Novostavba", D.building_class({"building_condition_name": "Novostavba",
                                             "building_type_name": "Panelová"}, Y), ("novostavba", "stav"))
check("Ve výstavbě / Projekt", D.building_class({"building_condition_name": "Projekt"}, Y)[0], "novostavba")
check("kolaudace 2016 (cihla, velmi dobrý)",
      D.building_class({"building_condition_name": "Velmi dobrý", "building_type_name": "Cihlová",
                        "acceptance_year": 2016}, Y), ("novostavba", "kolaudace"))
check("kolaudace 2014 = starší", D.building_class({"building_type_name": "Cihlová",
                                                   "acceptance_year": 2014}, Y)[0], "starsi")
check("panel z typu", D.building_class({"building_condition_name": "Dobrý",
                                        "building_type_name": "Panelová"}, Y), ("panel", "typ"))
check("cihla bez roku = starší", D.building_class({"building_type_name": "Cihlová"}, Y), ("starsi", "typ"))
check("popis: rok 2018 přebije typ", D.building_class({"building_type_name": "Cihlová",
                                                       "description": "v domě z roku 2018"}, Y)[0], "novostavba")
check("popis: rekonstrukce z roku 2019 není rok domu",
      D.building_class({"building_type_name": "Cihlová",
                        "description": "byt po kompletní rekonstrukci z roku 2019"}, Y)[0], "starsi")
check("„kotle z roku 2025“ není rok domu", D.building_class(
    {"building_type_name": "Cihlová", "description": "Vytápění z plynového kotle z roku 2025."}, Y)[0], "starsi")
check("rekonstrukce … instalatérských prací z roku 2024", D.building_class(
    {"building_type_name": "Cihlová", "description": "po celkové rekonstrukci včetně elektřiny a "
     "instalatérských prací z roku 2024."}, Y)[0], "starsi")
check("„Po rekonstrukci“ + dokončení 2027 = rekonstrukce", D.building_class(
    {"building_condition_name": "Po rekonstrukci", "building_type_name": "Cihlová",
     "description": "Předpokládané dokončení 2027."}, Y)[0], "starsi")
check("iDNES: „panelového domu“", D.building_class(
    {"description": "v 5. podlaží ze 6 panelového domu s výtahem"}, Y), ("panel", "popis"))
check("iDNES: „zkolaudované v roce 2015“", D.building_class(
    {"description": "v rezidenční budově zkolaudované v roce 2015"}, Y), ("novostavba", "popis"))
check("„jinak než v novostavbě“ není novostavba", D.building_class(
    {"description": "Pod šikmými stropy se bydlí jinak než v novostavbě."}, Y), (None, None))
check("popis „novostavba z roku 2008“ = starší (rok vyhrává)", D.building_class(
    {"description": "byt v novostavbě z roku 2008"}, Y)[0], "starsi")
check("nic nevíme = None", D.building_class({"description": "Pěkný byt."}, Y), (None, None))

# --- medián podle třídy, záloha na široký --------------------------------- #
def flat(i, sqm_price, cls_cond=None, btype=None, **kw):
    return {"id": i, "transaction_type": "prodej", "disposition": "2+kk", "area": "vysocany",
            "price_czk_per_sqm": sqm_price, "building_condition_name": cls_cond,
            "building_type_name": btype, **kw}

new = [flat(i, 200_000 + i * 1000, "Novostavba", "Cihlová") for i in range(5)]      # medián 202 000
panel = [flat(10 + i, 150_000 + i * 1000, "Dobrý", "Panelová") for i in range(5)]   # medián 152 000
# 145 000: proti panelům (medián 150 500) jen −4 %, proti všem typům
# (medián 162 000) by to bylo −10 % a „výhodná nabídka".
cheap_panel = flat(20, 145_000, "Dobrý", "Panelová")
old2 = [flat(30 + i, 170_000, "Dobrý", "Cihlová") for i in range(2)]                 # jen 2 -> široký
unknown = flat(40, 150_000)
mism = flat(41, 90_000, "Dobrý", "Panelová", area_mismatch=True)
rows = new + panel + [cheap_panel] + old2 + [unknown, mism]
scrape.rank_deals(rows)
check("panel proti mediánu panelů (ne všech)",
      (cheap_panel["deal_basis"], cheap_panel["deal_label"], cheap_panel["deal_pct"]),
      ("class", "2+kk · panel", round((145_000 - 150_500) / 150_500 * 100)))
check("...panel 140k NENÍ výhodný jen proto, že novostavby jsou dražší", cheap_panel["deal_ok"], False)
check("malá skupina (2 starší) -> široký medián, řečeno v labelu",
      (old2[0]["deal_basis"], old2[0]["deal_label"]), ("broad", "2+kk, všechny typy"))
check("neznámý typ -> široký medián", unknown["deal_basis"], "broad")
check("nevysvětlená plocha: žádné % ani výhodnost", (mism["deal_pct"], mism["deal_ok"]), (None, False))
check("...a nejde do mediánu panelů (n = 6, ne 7)", cheap_panel["deal_n"], 6)
check("house_class na řádku", (new[0]["house_class"], panel[0]["house_class"], unknown["house_class"]),
      ("novostavba", "panel", None))
st = scrape.compute_stats(rows)
check("compute_stats bez nevysvětlené plochy", st["sale_count"], len(rows) - 1)

# --- ribbon: žádné „pod mediánem“ u rozporu, text říká základ -------------- #
NOW = "2026-09-30T12:00:00Z"
good = flat(50, 120_000, "Dobrý", "Panelová", price_czk=5_000_000, deal_pct=-20, deal_ok=True,
            deal_label="2+kk · panel", house_class="panel")
bad = dict(good, id=51, area_mismatch=True, price_czk=5_100_000)
out = ribbon.hot_offers([good, bad], [], [], NOW)
check("ribbon: rozpor plochy se nevybírá", [it["id"] for it in out], [50])
check("ribbon: důvod říká typ domu", out[0]["reason"], "−20 % pod mediánem 2+kk · panel")
by = ribbon.hot_offers_by_class([good, dict(good, id=52, house_class=None, price_czk=4_000_000,
                                            deal_label="2+kk, všechny typy")], [], NOW)
check("ribbon po třídách: panel", [it["id"] for it in by["panel"]], [50])
check("ribbon po třídách: neurčeno", [it["id"] for it in by["neurceno"]], [52])
check("...důvod přizná všechny typy", by["neurceno"][0]["reason"], "−20 % pod mediánem 2+kk, všechny typy")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
