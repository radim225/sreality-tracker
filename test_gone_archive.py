#!/usr/bin/env python3
"""Archiv zmizelých inzerátů a párování znovuvložených.

Radim chce u zmizelého bytu vidět fotku, popis a adresu -- stránka na portálu
je v tu chvíli už 404. A když makléř tentýž byt vloží znovu pod novým id, má
to být vidět jako jeden byt, ne jako „zmizelo" + „nové". Testy hlídají, že
archiv nepřepisuje data zmizení, nepouští do gitu kontakty a nepáruje
stejné byty v jednom komplexu jen podle ceny a plochy.

Run: python3 test_gone_archive.py
"""
import copy
import sys
import tempfile
from pathlib import Path

import gone_archive as ga

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:52} {got!r}")


DESC = ("Nabízíme k dlouhodobému pronájmu světlý byt 2+kk s balkonem v ulici "
        "Kolmá v Praze 9. Byt se nachází ve třetím patře cihlového domu s "
        "výtahem, kuchyňská linka je vybavena myčkou a lednicí, v koupelně je "
        "sprchový kout a pračka. K bytu náleží sklep. Volné od října.")
OTHER = ("Pronájem nového bytu v projektu Pod Harfou. Dispozice 2+kk, "
         "orientace do dvora, podlahové vytápění, rekuperace, francouzské "
         "okno. Součástí nájmu je parkovací stání v garáži domu, sklep a "
         "připojení k internetu. Prohlídky po domluvě s makléřem.")


def flat(i, **kw):
    base = {"id": i, "url": f"https://x/{i}", "source": "sreality", "title": "Pronájem bytu 2+kk 54 m²",
            "transaction_type": "pronajem", "disposition": "2+kk", "floor_area_sqm": 54.0,
            "price_czk": 24500, "total_czk": 29000, "street": "Kolmá", "lat": 50.1, "lon": 14.5,
            "description": DESC, "seller_name": "RK Test", "images": [f"img{n}" for n in range(8)],
            "thumb": "img0", "phone": "777 000 111", "cost_of_living_raw": "x"}
    base.update(kw)
    return base


# --- add_gone ------------------------------------------------------------ #
arch = {}
pool = {"1": {"first_seen": "2026-08-01T10:00:00Z"}}
n = ga.add_gone(arch, [flat(1, removed_since="2026-09-01T08:00:00Z")], "2026-09-01T08:00:00Z", pool)
check("add_gone přidá záznam", n, 1)
e = arch["1"]
check("gone_at z removed_since", e["gone_at"], "2026-09-01T08:00:00Z")
check("first_seen z poolu", e["first_seen"], "2026-08-01T10:00:00Z")
check("last_price_czk", e["last_price_czk"], 24500)
check("max 5 fotek", len(e["images"]), 5)
check("pole mimo PREVIEW_FIELDS se neukládají", "phone" in e or "cost_of_living_raw" in e, False)

n = ga.add_gone(arch, [flat(1, removed_since="2026-09-05T08:00:00Z", price_czk=23000)],
                "2026-09-05T08:00:00Z", pool)
check("druhé volání nic nepřidá", n, 0)
check("gone_at se nepřepíše", arch["1"]["gone_at"], "2026-09-01T08:00:00Z")
check("jen jeden záznam", len(arch), 1)

n = ga.add_gone(arch, [{**flat(2), "first_seen": "2026-08-20T00:00:00Z"}], "2026-09-02T00:00:00Z")
check("bez removed_since -> at", arch["2"]["gone_at"], "2026-09-02T00:00:00Z")
check("bez poolu first_seen ze záznamu", arch["2"]["first_seen"], "2026-08-20T00:00:00Z")

# --- kontakty z popisu ven ----------------------------------------------- #
raw = ("Krásný byt. Tel.: 724 223 828, případně +420 771 285 172 či na mail "
       "k.novak@example.cz. Nájem 24 500 Kč, kauce 49 000 Kč, cena 8 990 000 Kč.\n775966386")
clean = ga.clean_description(raw)
check("telefon 3-3-3 pryč", "724 223 828" in clean, False)
check("telefon +420 pryč", "771 285 172" in clean, False)
check("telefon bez mezer pryč", "775966386" in clean, False)
check("e-mail pryč", "@" in clean, False)
check("ceny zůstávají", all(s in clean for s in ("24 500", "49 000", "8 990 000")), True)
long = ga.clean_description("slovo " * 400)
check("popis zkrácen na 1200 (+…)", len(long) <= ga.MAX_DESCRIPTION + 1, True)
a2 = {}
ga.add_gone(a2, [flat(9, description="Volejte 606 123 456 nebo pis@byt.cz")], "2026-09-01T00:00:00Z")
a3 = {}
ga.add_gone(a3, [flat(8, seller_name="Realitní kancelář  Honzík honzik@example.cz")], "x")
check("e-mail pryč i ze jména prodejce", a3["8"]["seller_name"], "Realitní kancelář Honzík")
check("add_gone ukládá očištěný popis", "606" in a2["9"]["description"] or "@" in a2["9"]["description"], False)

# --- prune ------------------------------------------------------------- #
pa = {"old": {"gone_at": "2026-01-01T00:00:00Z"}, "new": {"gone_at": "2026-09-01T00:00:00Z"},
      "bad": {"gone_at": "nesmysl"}}
dropped = ga.prune(pa, "2026-09-26T00:00:00Z")
check("prune vrací vyřazená id", dropped, ["old"])
check("prune nechá čerstvé a nečitelné", sorted(pa), ["bad", "new"])

# --- mark_returned ------------------------------------------------------- #
ma = {"1": {"gone_at": "2026-09-01T00:00:00Z"}, "2": {"gone_at": "2026-09-01T00:00:00Z"}}
check("mark_returned vrací id", ga.mark_returned(ma, [1, 99], at="2026-09-10T00:00:00Z"), ["1"])
check("záznam zůstává", "1" in ma, True)
check("returned_at nastaven", ma["1"].get("returned_at"), "2026-09-10T00:00:00Z")
check("opakovaně se neoznačí", ga.mark_returned(ma, [1], at="2026-09-11T00:00:00Z"), [])
check("vrácený mimo dashboard", [r["id"] for r in ga.dashboard_rows(
    {k: {**v, "id": k} for k, v in ma.items()}, "2026-09-12T00:00:00Z")], ["2"])
# Vrátil se a pak zmizel znovu: nová epizoda, stará se neztratí.
ra = {}
ga.add_gone(ra, [flat(5, removed_since="2026-09-01T00:00:00Z")], "x")
ga.mark_returned(ra, [5], at="2026-09-03T00:00:00Z")
check("staré zmizení po návratu nic nezaloží",
      ga.add_gone(ra, [flat(5, removed_since="2026-09-01T00:00:00Z")], "x"), 0)
check("znovu zmizelý se počítá", ga.add_gone(ra, [flat(5, removed_since="2026-09-08T00:00:00Z")], "x"), 1)
check("nové gone_at", ra["5"]["gone_at"], "2026-09-08T00:00:00Z")
check("stará epizoda zapsaná", ra["5"]["episodes"],
      [{"gone_at": "2026-09-01T00:00:00Z", "returned_at": "2026-09-03T00:00:00Z"}])

# --- link_relists -------------------------------------------------------- #
NOW = "2026-09-10T00:00:00Z"
la = {}
ga.add_gone(la, [flat(100, removed_since="2026-09-01T00:00:00Z", first_seen="2026-08-01T00:00:00Z"),
                 flat(200, removed_since="2026-09-01T00:00:00Z", first_seen="2026-08-01T00:00:00Z",
                      street="Pod Harfou", description=OTHER, lat=50.2, lon=14.6)], "x")
live = [
    # Tentýž text, nové id, o 300 Kč levnější -> znovuvložení.
    flat(101, price_czk=24200, first_seen="2026-09-03T00:00:00Z"),
    # Stejná cena, plocha, ulice, poloha jako 200 -- ale jiný popis (jiná
    # jednotka ve stejném komplexu). Tentýž prodejce -- ani to na „maybe"
    # nestačí, když se popisy shodují pod 0,3.
    flat(201, street="Pod Harfou", lat=50.2, lon=14.6,
         first_seen="2026-09-03T00:00:00Z",
         description=("Byt 2+kk v přízemí se zahrádkou orientovanou na jih, "
                      "nově zrekonstruovaná koupelna s vanou, velká šatna, kuchyň "
                      "bez spotřebičů, vhodné pro rodinu se psem, dostupné ihned.")),
]
links = ga.link_relists(la, live, NOW)
check("spáruje stejný popis", sorted(links), ["101"])
check("relisted_as u starého", la["100"]["relisted_as"]["id"], 101)
check("verdikt same", la["100"]["relisted_as"]["verdict"], "same")
check("link_record ukazuje na starý", links["101"]["id"], 100)
check("jiný popis ve stejném komplexu nespárován", "relisted_as" in la["200"], False)

again = ga.link_relists(la, live + [flat(102, price_czk=24300, first_seen="2026-09-04T00:00:00Z")], NOW)
check("druhý běh nic nového", again, {})
check("relisted_as se nepřepíše", la["100"]["relisted_as"]["id"], 101)

# Nové id se objevilo 50 dní po zmizení -> mimo okno.
ta = {}
ga.add_gone(ta, [flat(300, removed_since="2026-07-01T00:00:00Z", first_seen="2026-06-01T00:00:00Z")], "x")
late = [flat(301, first_seen="2026-08-20T00:00:00Z")]
check("nový >45 dní po zmizení nespárován", ga.link_relists(ta, late, "2026-08-10T00:00:00Z"), {})
check("starý mimo okno se ani nezkouší", ga.link_relists(ta, late, "2026-09-10T00:00:00Z"), {})

# Kontakt v živém popisu nesmí shodu rozbít: archiv má očištěný text.
ca = {}
ga.add_gone(ca, [flat(400, removed_since="2026-09-01T00:00:00Z", first_seen="2026-08-01T00:00:00Z",
                      description=DESC + " Volejte 724 223 828.")], "x")
check("shoda i s telefonem v popisu", sorted(ga.link_relists(
    ca, [flat(401, first_seen="2026-09-02T00:00:00Z", description=DESC + " Volejte 724 223 828.")], NOW)),
    ["401"])

# Řetěz A -> B -> C, B mezitím zmizel taky.
ch = {}
ga.add_gone(ch, [flat(500, removed_since="2026-09-01T00:00:00Z", first_seen="2026-08-01T00:00:00Z"),
                 flat(501, removed_since="2026-09-05T00:00:00Z", first_seen="2026-09-02T00:00:00Z")], "x")
cands = [copy.deepcopy(ch["501"]), flat(502, first_seen="2026-09-06T00:00:00Z")]
ga.link_relists(ch, cands, NOW)
check("řetěz: A -> B", ch["500"]["relisted_as"]["id"], 501)
check("řetěz: B -> C", ch["501"]["relisted_as"]["id"], 502)
check("řetěz: B zná předchůdce", ch["501"]["relist_of"]["id"], 500)

# --- relist_map ---------------------------------------------------------- #
rm = ga.relist_map(la)
check("relist_map obnoví páry", sorted(rm), ["101"])
check("relist_map = link_relists", rm["101"], links["101"])
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "a.json"
    ga.save_archive(la, p)
    check("relist_map po uložení a načtení", ga.relist_map(ga.load_archive(p)), rm)
    check("načtení chybějícího = {}", ga.load_archive(Path(d) / "neni.json"), {})

# --- dashboard_rows ------------------------------------------------------ #
da = {}
ga.add_gone(da, [flat(1, removed_since="2026-09-01T00:00:00Z"),
                 flat(2, removed_since="2026-09-09T00:00:00Z"),
                 flat(3, removed_since="2026-09-05T00:00:00Z"),
                 flat(4, removed_since="2026-07-01T00:00:00Z")], "x")
rows = ga.dashboard_rows(da, "2026-09-10T00:00:00Z")
check("nejnovější první, staré mimo 30 dní", [r["id"] for r in rows], [2, 3, 1])
check("bez popisu a galerie", any("description" in r or "images" in r for r in rows), False)
check("poslední cena v řádku", rows[0]["last_price_czk"], 24500)
check("chybějící area = vysocany", rows[0]["area"], "vysocany")

print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("all passed")
