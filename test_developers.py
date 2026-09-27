#!/usr/bin/env python3
"""Ceníky developerů: parsery na uložených (zkrácených) stránkách z 27. 9.
2026, rozdíl stavů, pojistka proti „všechno zmizelo", tichá baseline, alert,
párování s inzeráty Sreality a srovnatelná sada, karta.

Bez sítě. Na disk jen do dočasného adresáře.

Run: python3 test_developers.py
"""
import json
import sys
import tempfile
from pathlib import Path

import developers as dev
import developers_card as card
import developers_compare as cmp_

failures = []
FIX = Path(__file__).resolve().parent / "fixtures" / "developers"


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:58} {got!r}")


def fx(name):
    return (FIX / name).read_text(encoding="utf-8")


def by_id(units):
    return {u["id"]: u for u in units}


# --- parsery ---------------------------------------------------------------- #
u = by_id(dev.parse_waltrovka_f(fx("waltrovka_f.html")))
check("F: 3 jednotky", len(u), 3)
f1 = u["F10.001"]
check("F: cena s DPH", f1["price_czk"], 18076700)
check("F: cena bez DPH", f1["price_excl_vat_czk"], 16139911)
check("F: cena s příslušenstvím", f1["price_with_extras_czk"], 19387700)
check("F: patro 0 = 1. NP", f1["floor"], 1)
check("F: zahrádka a terasa", (f1["garden_sqm"], f1["terrace_sqm"]), (87.7, 32.8))
check("F: nulový balkon se neukládá", "balcony_sqm" in f1, False)
check("F: Předrezervace -> predrezervace",
      sorted(x["status"] for x in u.values()), ["predrezervace", "volny", "volny"])

u = by_id(dev.parse_waltrovka_h(fx("waltrovka_h.html")))
h = u["H2.105"]
check("H: katastr (parcela, budova, jednotka)",
      (h["cadastre"]["parcela"], h["cadastre"]["budova"], h["cadastre"]["jednotka_prohlaseni"]),
      ("977/18", "1059", "47"))
check("H: PRODÁNO (unicode escape) -> prodano", h["status"], "prodano")
check("H: cena prodaného zůstává", h["price_czk"], 6626281)
check("H: patro odvozené z označení", (h["floor"], h["floor_derived"]), (2, True))

u = dev.parse_waltrovka_showcase(fx("waltrovka_showcase.json"))
check("showcase: 2 jednotky, stav 3 = prodáno", [x["status"] for x in u], ["prodano", "prodano"])
check("showcase: smazaná cena „-“ = žádná cena", [x.get("price_czk") for x in u], [None, None])

u = by_id(dev.parse_hutmanka(fx("hutmanka.html")))
check("Hutmanka: slovník popisků se nebere jako data", len(u), 3)
check("Hutmanka: stavy", sorted(x["status"] for x in u.values()), ["prodano", "rezervace", "volny"])
check("Hutmanka: prodaný nemá cenu", [x.get("price_czk") for x in u.values() if x["status"] == "prodano"], [None])
v = [x for x in u.values() if x["status"] == "volny"][0]
check("Hutmanka: 4+kk s cenou a patrem z „1. NP“", (v["disposition"], bool(v["price_czk"]), v["floor"]),
      ("4+kk", True, 1))

u = dev.parse_semerinka(fx("semerinka.html"))
check("Semerínka: stavy", [x["status"] for x in u], ["volny", "predrezervace", "prodano"])
check("Semerínka: dispozice malými", u[0]["disposition"], "3+kk")
check("Semerínka: patro z „02 NP“", u[0]["floor"], 2)
check("Semerínka: jen volný má cenu", [bool(x.get("price_czk")) for x in u], [True, False, False])

u = dev.parse_ukomina(fx("ukomina.html"))
check("U Komína: duplicitní responzivní řádek se nepočítá", [x["id"] for x in u], ["B1.1", "B1.3", "B7.4", "B2.1"])
check("U Komína: „0“ i „-“ = bez ceny", [x.get("price_czk") for x in u], [8381250, None, 23989500, None])
check("U Komína: plocha z „29,3 m²“", u[0]["area_sqm"], 29.3)
check("U Komína: balkón „-“ = nic", "balcony_sqm" in u[0], False)
try:
    dev.parse_ukomina(fx("ukomina.html").replace("Užitné m²", "Plocha"))
    check("U Komína: jiná hlavička = chyba", "no error", "ValueError")
except ValueError:
    check("U Komína: jiná hlavička = chyba", "ValueError", "ValueError")

u = dev.parse_prokopska(fx("prokopska.html"))
check("Prokopská: stavy", [x["status"] for x in u], ["volny", "rezervace", "predrezervace"])
check("Prokopská: cena s i bez DPH", (u[0]["price_czk"], bool(u[0]["price_excl_vat_czk"])), (7367000, True))
check("Prokopská: rezervovaný bez ceny", u[1].get("price_czk"), None)

check("status_of: V jednání", dev.status_of("V jednání"), "predrezervace")
check("status_of: Před-rezervovaný", dev.status_of("Před-rezervovaný"), "predrezervace")
check("status_of: neznámé", dev.status_of("Odložený"), "jine")
check("num: „14 645 000 Kč“", dev.num("14\xa0645 000 Kč"), 14645000.0)


# --- rozdíl a baseline ------------------------------------------------------ #
def U(uid, price=10_000_000, status="volny", disp="4+kk", area=100.0, floor=2):
    return dev.unit(uid, price_czk=price, status=status, disposition=disp, area_sqm=area, floor=floor)


T0, T1, T2 = "2026-09-27T05:30:00Z", "2026-09-28T05:30:00Z", "2026-09-29T05:30:00Z"
st0, ev0, w0 = dev.update_project("rezidence-hutmanka", None, [U("a"), U("b"), U("c", disp="2+kk", area=50)], None, T0)
check("baseline: jediná událost typu baseline", [e["type"] for e in ev0], ["baseline"])
check("baseline: jednotky označené", all(x.get("baseline") for x in st0["units"].values()), True)
check("baseline: žádný alert", dev.build_alert(ev0, {"rezidence-hutmanka": st0}), None)

fetched = [U("a", price=9_500_000), U("b", price=None, status="rezervace"), U("d", status="volny")]
st1, ev1, w1 = dev.update_project("rezidence-hutmanka", st0, fetched, None, T1)
types = sorted((e["unit"], e["type"]) for e in ev1)
check("rozdíl: cena, skrytí ceny + stav, nová, zmizelá",
      types, [("a", "price"), ("b", "price_hidden"), ("b", "status"), ("c", "gone"), ("d", "new")])
check("rozdíl: poslední známá cena zůstává", st1["units"]["b"]["last_price_czk"], 10_000_000)
check("rozdíl: zmizelá jednotka zůstává v datech s gone_at", st1["units"]["c"]["gone_at"], T1)
check("rozdíl: first_seen se nese", st1["units"]["a"]["first_seen"], T0)
pe = [e for e in ev1 if e["type"] == "price"][0]
check("rozdíl: stará → nová cena", (pe["old"], pe["new"]), (10_000_000, 9_500_000))

st2, ev2, _ = dev.update_project("rezidence-hutmanka", st1, fetched + [U("c", disp="2+kk", area=50)], None, T2)
check("návrat zmizelé jednotky", [(e["unit"], e["type"]) for e in ev2], [("c", "returned")])
check("…a gone_at je pryč", "gone_at" in st2["units"]["c"], False)

# --- pojistka: 0 jednotek / chyba / propad ---------------------------------- #
for label, fetched_, err in (("0 jednotek", [], None), ("výjimka parseru", [], "ValueError: x")):
    st, ev, warn = dev.update_project("rezidence-hutmanka", st1, fetched_, err, T2)
    check(f"{label}: žádné události", ev, [])
    check(f"{label}: jednotky beze změny", st["units"], st1["units"])
    check(f"{label}: varování", bool(warn), True)
big = {"units": {f"u{i}": U(f"u{i}") for i in range(10)}}
st, ev, warn = dev.update_project("rezidence-hutmanka", big, [U("u1"), U("u2")], None, T1)
check("propad 10 -> 2 = nezdravé, nic nezmizí", (ev, "propad" in (warn or "") or "spadl" in (warn or "")), ([], True))
st, ev, warn = dev.update_project("rezidence-hutmanka", big, [U(f"u{i}") for i in range(9)], None, T1)
check("10 -> 9 je normální prodej", [e["type"] for e in ev], ["gone"])

# run() na disk: jeden web spadne, ostatní se uloží; první běh bez dat nic nezapíše
with tempfile.TemporaryDirectory() as tmp:
    pages = {dev.PROJECTS["rezidence-hutmanka"]["url"]: fx("hutmanka.html")}

    def getter(url):
        if url in pages:
            return pages[url]
        raise OSError("síť nedostupná")

    states, events, warns = dev.run(["rezidence-hutmanka", "semerinka"], getter=getter, data_dir=tmp, at=T0)
    files = sorted(p.name for p in Path(tmp).iterdir())
    check("run: selhání jednoho webu nezastaví druhý", files, ["history.jsonl", "rezidence-hutmanka.json"])
    check("run: varování jen pro selhaný", len(warns), 1)
    hist = dev.load_history(tmp)
    check("run: historie = baseline", [e["type"] for e in hist], ["baseline"])
    # druhý běh: Hutmanka vrátí 0 jednotek -> stav zůstane
    pages[dev.PROJECTS["rezidence-hutmanka"]["url"]] = "<html></html>"
    states, events, warns = dev.run(["rezidence-hutmanka"], getter=getter, data_dir=tmp, at=T1)
    saved = dev.load_state("rezidence-hutmanka", tmp)
    check("run: rozbitá stránka nezmění uložené jednotky", len(saved["units"]), 3)
    check("run: …a nic nezmizí", any(x.get("gone_at") for x in saved["units"].values()), False)
    check("run: …ale chyba je zapsaná", bool(saved.get("last_error")), True)

# --- alert ------------------------------------------------------------------ #
states = {"rezidence-hutmanka": st1}
text = dev.build_alert(ev1, states)
check("alert: 4+kk první", text.split("\n")[1], "<b>4+kk / 5+kk</b>")
check("alert: skrytá cena se stavem se nehlásí dvakrát", text.count("🙈"), 0)
check("alert: změna ceny", "10 000 000 Kč → 9 500 000 Kč" in text, True)
check("alert: ostatní dispozice zvlášť", "<b>Ostatní dispozice</b>" in text, True)
evil = [{"at": T1, "project": "x<b>", "unit": "<script>", "type": "new", "new": "volny",
         "disposition": "4+kk", "price_czk": 1}]
t = dev.build_alert(evil, {"x<b>": {"name": "<img src=x>", "units": {}}})
check("alert: escapování", ("<script>" in t, "<img" in t), (False, False))
sent = dev.send_alert(ev1, states, dry_run=True)
check("alert: dry-run nic neodešle", sent, "dry-run")


# --- párování a srovnatelná sada ------------------------------------------- #
def proj(slug, units, kind="vystavba", lat=50.0559, lon=14.3678):
    return {"project": slug, "name": slug, "kind": kind, "lat": lat, "lon": lon,
            "units": {x["id"]: x for x in units}}


def L(id_, disp="4+kk", area=100, price=20_000_000, floor=None, tx="prodej", lat=50.0560, lon=14.3680,
      kind="vystavba"):
    return {"id": id_, "src": "t", "tx": tx, "disposition": disp, "area_sqm": float(area),
            "price_czk": price, "per_sqm": round(price / area), "floor": floor, "lat": lat, "lon": lon,
            "kind": kind, "street": "Na Hutmance", "url": "https://www.sreality.cz/x"}


P = {"hut": proj("hut", [U("1.1.2", price=28_252_000, area=100.31, floor=1),
                         U("1.2.2", price=24_112_000, area=100.31, floor=2),
                         U("2.1.1", price=13_412_000, disp="2+kk", area=43.05, floor=1)])}
links, un = cmp_.match_listings([L("s1", area=43, disp="2+kk", price=13_000_000, floor=1)], P)
check("párování: jediný kandidát -> pár s důvodem", list(links), [("hut", "2.1.1")])
lk = links[("hut", "2.1.1")][0]
check("párování: rozdíl Kč a %", (lk["diff_czk"], lk["diff_pct"]), (-412_000, -3.1))
check("párování: tier pravidla", lk["tier"], "pravidla")
links, un = cmp_.match_listings([L("s2", area=100, price=21_000_000)], P)
check("párování: dva kandidáti bez patra = nespárováno, počet", (links, un), ({}, {"s2": 2}))
links, un = cmp_.match_listings([L("s3", area=100, price=24_112_000)], P)
check("párování: shoda ceny vybere uvnitř pravidel", (list(links), links[("hut", "1.2.2")][0]["tier"]),
      ([("hut", "1.2.2")], "cena"))
links, un = cmp_.match_listings([L("s4", area=100, price=21_000_000, floor=2)], P)
check("párování: patro rozhodne", list(links), [("hut", "1.2.2")])
links, un = cmp_.match_listings([L("s5", area=100, floor=3)], P)
check("párování: jiné patro = nic", (links, un), ({}, {"s5": 0}))
links, un = cmp_.match_listings([L("s6", area=104.5, floor=1)], P)
check("párování: plocha o 4,2 m² / 4 % mimo", links, {})
links, un = cmp_.match_listings([L("s7", area=103.2, floor=1)], P)
check("párování: plocha ±3 m² stačí", list(links), [("hut", "1.1.2")])
links, un = cmp_.match_listings([L("s8", area=100, floor=1, lat=50.0590)], P)
check("párování: 350 m od projektu = nic", links, {})
links, un = cmp_.match_listings([L("s9", disp="3+kk", area=100, floor=1)], P)
check("párování: jiná dispozice = nic", links, {})

snap = {"generated_at": T1,
        "novostavby": [{"id": 1, "transaction_type": "pronajem", "disposition": "4+kk", "price_czk": 90_000,
                        "floor_area_sqm": 100, "lat": 50.056, "lon": 14.368, "kind": "vystavba"}],
        "comparables": [{"id": 2, "transaction_type": "prodej", "disposition": "2+kk", "price_czk": 9_000_000,
                         "floor_area_sqm": 50, "lat": 50.05, "lon": 14.37, "building_condition": 4,
                         "active": True},
                        {"id": 3, "transaction_type": "prodej", "disposition": "2+kk", "price_czk": 9_000_000,
                         "floor_area_sqm": 50, "lat": 50.05, "lon": 14.37, "building_condition": 1,
                         "active": True}]}
ls = cmp_.sreality_listings(snap)
check("Sreality: pronájem se do srovnání nedostane", [x["id"] for x in ls], ["2", "3"])
check("Sreality: druh comparables ze stavu budovy", [x["kind"] for x in ls], ["vystavba", "starsi"])
check("Sreality: pronájem 4+kk z novostaveb není ani kandidát",
      [x for x in ls if x["disposition"] == "4+kk"], [])

unit_ = U("x", price=10_000_000, disp="2+kk", area=50)  # 200 000 Kč/m²
mk = [L(f"v{i}", disp="2+kk", area=50, price=9_000_000, kind="vystavba", lat=50.0541, lon=14.3674) for i in range(3)]
mk += [L(f"d{i}", disp="2+kk", area=50, price=12_000_000, kind="dokoncena", lat=50.0541, lon=14.3674) for i in range(5)]
mk += [L("far", disp="2+kk", area=50, price=1_000_000, kind="vystavba", lat=50.08, lon=14.3674)]
mk += [L("big", disp="2+kk", area=70, price=1_000_000, kind="vystavba", lat=50.0541, lon=14.3674)]
b = cmp_.benchmark(unit_, "vystavba", mk)
check("benchmark: jen stejný druh, v kruhu, plocha ±15 %", (b["n"], b["median"], b["low"]), (3, 180_000, False))
check("benchmark: odchylka jednotky", b["dev_pct"], 11.1)
b = cmp_.benchmark(unit_, "vystavba", mk, exclude_ids={"v0"})
check("benchmark: n < 3 = málo srovnatelných, žádná odchylka", (b["n"], b["low"], "dev_pct" in b), (2, True, False))
check("benchmark: smíšené číslo zvlášť a označené", (b["mixed"]["n"], b["mixed"]["median"]), (7, 240_000))
check("benchmark: dokončené se nemíchá do výstavby",
      cmp_.benchmark(unit_, "dokoncena", mk)["median"], 240_000)
mk.append(L("old", disp="2+kk", area=50, price=5_000_000, kind="starsi", lat=50.0541, lon=14.3674))
b = cmp_.benchmark(unit_, "vystavba", mk, exclude_ids={"v0", "v1"})
check("benchmark: starší ani ve smíšeném", b["mixed"]["n"], 6)

# druh inzerátu přebije projekt (Hutmanka má na Sreality štítek novostavba)
ll = cmp_.apply_project_kinds([L("k1", area=100, kind="dokoncena")], P)
check("druh inzerátu z projektu, původní se pamatuje", (ll[0]["kind"], ll[0]["kind_sreality"]),
      ("vystavba", "dokoncena"))

# --- karta ------------------------------------------------------------------ #
with tempfile.TemporaryDirectory() as tmp:
    s = {"project": "rezidence-hutmanka", "name": "Hut<script>", "kind": "vystavba",
         "lat": 50.0559, "lon": 14.3678, "url": "javascript:alert(1)",
         "units": {"</script><b>": U("</script><b>")}, "baseline_at": T0}
    dev.save_state("rezidence-hutmanka", s, tmp)
    dev.append_history([{"at": T1, "project": "rezidence-hutmanka", "unit": "</script><b>", "type": "status",
                         "old": "volny", "new": "rezervace", "disposition": "4+kk", "price_czk": 1}], tmp)
    out = card.card_html({"generated_at": T1}, data_dir=tmp)
    check("karta: vyrenderuje se", 'id="devCard"' in out, True)
    import html as _html
    import re as _re
    check("karta: žádný <script> v kartě", "<script" in out.lower(), False)
    attr = _re.search(r'data-json="([^"]*)"', out).group(1)
    check("karta: data v atributu bez „<“ a uvozovek", ("<" in attr, '"' in attr), (False, False))
    data = json.loads(_html.unescape(attr))
    check("karta: nedávno rezervované", data["recent"][0]["new"], "rezervace")
    check("karta: škodlivé id zůstává jen v datech", data["units"][0]["id"], "</script><b>")
    check("karta: prázdný adresář = žádná karta", card.card_html({}, data_dir=tmp + "/nic"), "")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
