#!/usr/bin/env python3
"""Adresa inzerátu: číslo z popisu, odhad z OpenStreetMap, odkazy na mapu.

Radim chce u garáží a zmizelých inzerátů vědět, kde přesně jsou. Byl
upozorněn, že odhad z bodu Sreality může ukázat na vedlejší dům, a zvolil si
ho -- tyhle testy hlídají, že odhad je vždycky označený jako odhad, že popis
plný čísel nevyrobí falešnou adresu a že geokódování nikdy neshodí běh.

Offline: síť je nahrazená, nic se neposílá na Nominatim.
Run: python3 test_geocode.py
"""
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

os.environ["GEOCODE_DISABLED"] = "1"  # pojistka: ani omylem na síť
import geocode  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:52} {got!r}")


hn = geocode.house_number_from_text

# --- číslo domu z popisu ------------------------------------------------ #
# Texty jsou ve stylu skutečných inzerátů z okruhu.
check("ulice + číslo", hn("Nabízíme k pronájmu garáž v ulici Na Krocínce 12, Praha 9.",
                          "Na Krocínce"), "12")
check("ulice + popisné/orientační",
      hn("Nabízíme k pronájmu stání na adrese Poděbradská 1197/60", "Poděbradská"), "1197/60")
check("číslo s písmenem", hn("Garáž v domě Kolbenova 3a, Vysočany.", "Kolbenova"), "3a")
check("ulice bez diakritiky v popisu", hn("stani v dome Podebradska 56", "Poděbradská"), "56")
check("ulice velkými", hn("ADRESA: POD HARFOU 9, PRAHA 9", "Pod Harfou"), "9")
check("č. p.", hn("Garáž je součástí domu č. p. 1234 na sídlišti.", None), "1234")
check("čp.1234/5 bez mezery", hn("Řadová garáž u domu čp.1234/5.", "Jinonická"), "1234/5")
check("číslo popisné", hn("dům číslo popisné 780, klidná lokalita", None), "780")
check("číslo na konci věty", hn("Stání najdete v ulici Kolbenova 38.", "Kolbenova"), "38")

# Pasti: čísla, která číslem domu nejsou. Každá z nich by vyrobila adresu,
# která vypadá věrohodně a je špatně.
check("dispozice 2+kk", hn("Pronájem bytu 2+kk, Kolbenova 2+kk po rekonstrukci", "Kolbenova"), None)
check("patro", hn("Byt ve 3. patře, Kolbenova, 3. patro s výtahem", "Kolbenova"), None)
check("plocha", hn("Garáž Kolbenova 70 m2, suchá", "Kolbenova"), None)
check("plocha s m²", hn("Pod Harfou 18 m² stání", "Pod Harfou"), None)
check("cena s mezerou tisíců", hn("Na Krocínce 12 000 Kč měsíčně", "Na Krocínce"), None)
check("cena bez ulice", hn("cena 12 000 Kč včetně poplatků", "Na Krocínce"), None)
check("Praha 9 není číslo domu", hn("Kolbenova, Praha 9 - Vysočany", "Kolbenova"), None)
check("Praha 9 bez ulice", hn("Garáž Praha 9, volná ihned", None), None)
check("rok kolaudace", hn("Novostavba z roku 2019, Kolbenova, kolaudace 2019", "Kolbenova"), None)
check("tram č. 12 není č. p.", hn("Zastávka tram č. 12 přímo u domu.", "Kolbenova"), None)
check("jiná ulice se nebere", hn("Blízko domu Sokolovská 120.", "Kolbenova"), None)
check("ulice jako část slova", hn("Nová Kolbenova 5 je jinde", "Nová"), None)
check("pětimístné číslo ne", hn("Kolbenova 19000 Praha", "Kolbenova"), None)
check("minuty pěšky", hn("Kolbenova 5 min od metra", "Kolbenova"), None)
check("prázdný popis", hn(None, "Kolbenova"), None)
check("bez ulice a bez č. p.", hn("Garáž na adrese Kolbenova 3", None), None)

# --- Nominatim: odpověď, cache, stropy --------------------------------- #
PT = (50.10444, 14.50650)
# Zkrácená skutečná odpověď Nominatim pro bod Pod Harfou (26. 9. 2026).
REAL = {"place_id": 132672028, "licence": "Data © OpenStreetMap contributors",
        "lat": "50.1044451", "lon": "14.5064956", "category": "place", "type": "house",
        "display_name": "1066/9, Pod Harfou, Vysočany, Praha 9, obvod Praha 9, Praha, 190 00, Česko",
        "address": {"house_number": "1066/9", "road": "Pod Harfou", "suburb": "Vysočany",
                    "district": "obvod Praha 9", "city": "Praha", "postcode": "190 00",
                    "country": "Česko", "country_code": "cz"},
        "boundingbox": ["50.1043951", "50.1044951", "14.5064456", "14.5065456"]}

calls, sleeps = [], []
responses = []


def fake_get(url, params):
    calls.append((url, dict(params)))
    r = responses.pop(0) if responses else REAL
    if isinstance(r, Exception):
        raise r
    return r


geocode._http_get = fake_get
geocode._sleep = lambda s: sleeps.append(s)
os.environ["GEOCODE_DISABLED"] = ""

cache = {}
hit, live = geocode.reverse(*PT, cache)
check("živý dotaz", live, True)
check("adresa z odpovědi", (hit["road"], hit["house_number"], hit["suburb"], hit["postcode"]),
      ("Pod Harfou", "1066/9", "Vysočany", "190 00"))
check("vzdálenost od bodu v m", hit["distance_m"] <= 5, True)
# Cache nesmí nést surovou odpověď -- licence, bbox, display_name.
check("cache bez surové odpovědi",
      sorted(cache["50.10444,14.50650"]),
      ["at", "distance_m", "house_number", "postcode", "road", "suburb"])
check("parametry dotazu",
      {k: calls[0][1][k] for k in ("format", "zoom", "addressdetails", "accept-language")},
      {"format": "jsonv2", "zoom": 18, "addressdetails": 1, "accept-language": "cs"})

hit2, live2 = geocode.reverse(*PT, cache)
check("druhý dotaz z cache, bez sítě", (live2, len(calls), hit2["road"]), (False, 1, "Pod Harfou"))

# Policy: max 1 dotaz za sekundu.
geocode.reverse(50.1, 14.5, cache)
check("mezi živými dotazy se čeká", bool(sleeps) and sleeps[-1] > 0.9, True)

# Výsledek daleko od bodu není „nejbližší dům".
far = dict(REAL, lat="50.1100", lon="14.5065")
responses.append(far)
hit3, _ = geocode.reverse(50.2, 14.6, cache)
far_entry = cache["50.20000,14.60000"]
check("výsledek přes 300 m se zahodí", hit3, None)
check("...a zapíše jako neúspěch", "error" in far_entry, True)
far_entry["error"] = "x"
geocode.reverse(50.2, 14.6, cache)
check("neúspěch se týden nezkouší znovu", len([c for c in calls if c[1]["lat"] == "50.200000"]), 1)
old = geocode._now() - timedelta(days=8)
far_entry["at"] = geocode._stamp(old)
responses.append(REAL)
geocode.reverse(50.2, 14.6, cache)
check("po týdnu ano", len([c for c in calls if c[1]["lat"] == "50.200000"]), 2)

# Chyba sítě nesmí vyletět ven.
responses.append(TimeoutError("read timed out"))
hit4, live4 = geocode.reverse(50.3, 14.7, cache)
check("timeout se spolkne", (hit4, live4, "error" in cache["50.30000,14.70000"]), (None, True, True))
responses.append(ValueError("Expecting value"))  # 200 s HTML místo JSON
check("rozbitá odpověď se spolkne", geocode.reverse(50.31, 14.7, cache)[0], None)
responses.append({"error": "Unable to geocode"})
check("Nominatim 'error' = neúspěch", geocode.reverse(50.32, 14.7, cache)[0], None)

os.environ["GEOCODE_DISABLED"] = "1"
n = len(calls)
check("GEOCODE_DISABLED = žádná síť", (geocode.reverse(50.4, 14.7, cache), len(calls)),
      ((None, False), n))
check("...ale cache platí dál", geocode.reverse(*PT, cache)[0]["road"], "Pod Harfou")
os.environ["GEOCODE_DISABLED"] = ""

check("nesmyslné souřadnice", geocode.reverse("abc", None, cache), (None, False))
check("NaN", geocode.reverse(float("nan"), 14.5, cache), (None, False))

# Prázdná proměnná z GitHub Actions = výchozí hodnota, ne pád.
os.environ["X_TEST_INT"] = "  "
check("prázdná env = default", geocode._env_int("X_TEST_INT", 60), 60)
os.environ["X_TEST_INT"] = "abc"
check("nečíselná env = default", geocode._env_int("X_TEST_INT", 60), 60)
os.environ["X_TEST_INT"] = "5"
check("číselná env", geocode._env_int("X_TEST_INT", 60), 5)

# --- annotate: pořadí důvěry -------------------------------------------- #
exact = {"lat": 50.1, "lon": 14.5, "street": "Kolbenova", "house_number": "38",
         "locality_entity": "address", "city_part": "Vysočany",
         "description": "Na Krocínce 12"}
from_text = {"lat": 50.11, "lon": 14.51, "street": "Na Krocínce", "locality_entity": "street",
             "city_part": "Vysočany", "description": "Garáž v ulici Na Krocínce 12, suchá."}
estimate = {"lat": PT[0], "lon": PT[1], "street": "Pod Harfou", "locality_entity": "street",
            "city_part": "Vysočany", "description": "Stání 2+kk, 3. patro, 70 m2"}
street_only = {"lat": 50.5, "lon": 14.9, "street": "Jinonická", "locality_entity": "street",
               "city_part": "Jinonice", "description": ""}
no_gps = {"street": "Kolbenova", "city_part": "Vysočany"}
nothing = {"description": "Garáž"}
done = {"lat": 50.1, "lon": 14.5,
        "address": {"text": "X 1", "precision": "exact", "source": "sreality", "note": ""}}
items = [exact, from_text, estimate, street_only, no_gps, nothing, done]

check("žádná nespotřebovaná odpověď", responses, [])
cache = {"50.50000,14.90000": {"error": "nic", "at": geocode._stamp()}}
calls.clear()
live = geocode.annotate(items, cache, budget=5)
check("přesná ze Sreality", (exact["address"]["text"], exact["address"]["precision"],
                             exact["address"]["source"]),
      ("Kolbenova 38, Vysočany", "exact", "sreality"))
check("přesná nepotřebuje síť ani popis", exact["address"]["text"].startswith("Kolbenova"), True)
check("z popisu", (from_text["address"]["text"], from_text["address"]["precision"],
                   from_text["address"]["source"]),
      ("Na Krocínce 12, Vysočany", "text", "popis"))
check("odhad z OSM", (estimate["address"]["text"], estimate["address"]["precision"],
                      estimate["address"]["source"]),
      ("Pod Harfou 1066/9, Vysočany", "estimate", "nominatim"))
# Tohle je jádro Radimova rozhodnutí: odhad se smí ukázat, jen když to říká.
check("odhad říká, že je odhad", "odhad" in estimate["address"]["note"], True)
check("jen ulice", (street_only["address"]["text"], street_only["address"]["precision"],
                    street_only["address"]["source"]),
      ("Jinonická, Jinonice", "street", "sreality-ulice"))
check("bez GPS aspoň ulice", no_gps["address"]["precision"], "street")
check("bez čehokoli nic", "address" in nothing, False)
check("hotová přesná se nepřepisuje", done["address"]["text"], "X 1")
check("živé dotazy spočítané", (live, len(calls)), (1, 1))

# Strop platí jen pro živé dotazy.
many = [{"lat": 49.0 + i / 100, "lon": 15.0, "street": "Ulice"} for i in range(5)]
many.append({"lat": PT[0], "lon": PT[1], "street": "Pod Harfou"})  # v cache
calls.clear()
live = geocode.annotate(many, cache, budget=2)
check("strop živých dotazů", (live, len(calls)), (2, 2))
check("cache zásah i po vyčerpání stropu", many[-1]["address"]["precision"], "estimate")
check("po stropu aspoň ulice", many[4]["address"]["precision"], "street")

# Rozbitá položka nezastaví ostatní.
weird = [None, {"lat": "x"}, {"street": "Kolbenova"}]
check("divná položka se přeskočí", geocode.annotate(weird, {}, budget=1), 0)
check("...a zbytek se zpracuje", weird[2]["address"]["precision"], "street")

# --- cache na disku ------------------------------------------------------ #
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "c.json"
    geocode.save_cache({"b": {"road": "Žitná"}, "a": {}}, path)
    raw = path.read_text(encoding="utf-8")
    check("seřazené klíče", raw.index('"a"') < raw.index('"b"'), True)
    check("čeština bez escapů", "Žitná" in raw, True)
    check("načtení zpět", geocode.load_cache(path)["b"]["road"], "Žitná")
    path.write_text("{rozbité", encoding="utf-8")
    check("rozbitá cache = prázdná", geocode.load_cache(path), {})
    check("chybějící cache = prázdná", geocode.load_cache(Path(tmp) / "nic.json"), {})

# --- odkazy na mapu ------------------------------------------------------ #
links = geocode.map_links(50.10444, 14.5065)
check("mapy.cz: lon,lat", links["mapy"],
      "https://mapy.cz/zakladni?source=coor&id=14.506500%2C50.104440"
      "&x=14.506500&y=50.104440&z=18")
check("google street view: lat,lon", links["google_street"],
      "https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=50.104440,14.506500")
check("panorama míří na bod", "x=14.506500&y=50.104440" in links["panorama"], True)
check("řetězec s číslem projde jako číslo", geocode.map_links("50.1", "14.5")["mapy"].count("50.100000"), 2)
# Nic jiného než číslo se do href nedostane.
check("injekce v souřadnici", geocode.map_links('50.1" onclick="x', 14.5), None)
check("None", geocode.map_links(None, 14.5), None)
check("mimo rozsah", geocode.map_links(95, 14.5), None)
check("nekonečno", geocode.map_links(float("inf"), 14.5), None)
check("bool není souřadnice", geocode.map_links(True, 14.5), None)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
