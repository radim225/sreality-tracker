#!/usr/bin/env python3
"""Ověření zmizelých inzerátů: správně i levně.

26. 9. 2026 se ukázaly dvě věci naráz. Bezrealitky i iDNES na smazaný inzerát
odpovídají 200 („již není v nabídce"), ne 404 -- takže verify_removals mrtvé
inzeráty nikdy nepoznal a vracel je do tabulky (4 z 8 a 3 z 8 ve vzorku). A
~390 takových „zmizelých" se ověřovalo každý běh znovu: 5,5 min z 11,5.

Run: python3 test_removals.py   (offline)
"""
import json
import sys

import market
import pool
import scrape
import sources

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:60} {got!r}")


def bez_page(advert, neighbours=()):
    data = {"props": {"pageProps": {
        "origAdvert": advert,
        "apolloCache": {f"Advert:{n['id']}": n for n in neighbours},
    }}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


# --- Bezrealitky: jen origAdvert rozhoduje -------------------------------- #
check("aktivní inzerát žije",
      sources.bez_inactive(bez_page({"id": "1", "active": True, "archived": False}), "1"), False)
check("active:false = pryč",
      sources.bez_inactive(bez_page({"id": "1", "active": False, "archived": False}), "1"), True)
check("archived:true = pryč",
      sources.bez_inactive(bez_page({"id": "1", "active": True, "archived": True}), "1"), True)
check("mrtvý soused nezabije živý inzerát",
      sources.bez_inactive(bez_page({"id": "1", "active": True},
                                    [{"id": "2", "active": False}]), "1"), False)
check("cizí origAdvert = nevím",
      sources.bez_inactive(bez_page({"id": "9", "active": False}), "1"), None)
check("stránka bez dat = nevím", sources.bez_inactive("<html></html>", "1"), None)

# --- iDNES: nadpis „Nabídka již není aktivní" + datum ---------------------- #
dead = ('<p class="b-intro__title">\n\t\t\t\t\tNabídka již není aktivní\t\t\t\t</p>'
        '<p>Ode dne 21.09.2026 evidujeme nabídku jako neaktivní.<br></p>')
check("iDNES neaktivní s datem", sources.idnes_inactive(dead), (True, "2026-09-21"))
check("iDNES živý", sources.idnes_inactive('<div class="b-desc">Byt již není volný od…</div>'),
      (False, None))
# „již není" v popisu makléře neznamená smazaný inzerát.
check("fráze v popisu nestačí",
      sources.idnes_inactive("<p>Nabídka již není aktivní pro zájemce se psem</p>")[0], False)

# --- verify_removals: TTL, zombie, razítko -------------------------------- #
NOW = "2026-09-27T10:00:00Z"
asked = []


def fake_gone(comp):
    asked.append(comp["id"])
    return {"dead-zombie": (True, None), "dead-fresh": (True, "2026-09-26"),
            "sr-dead": (True, None)}.get(comp["id"], (False, None))


real = scrape.listing_is_gone
scrape.listing_is_gone = fake_gone
try:
    cands = [
        # dávná zombie: vracená do tabulky starým kódem, nikdy neověřená novým
        {"id": "dead-zombie", "source": "bezrealitky", "search_missed": True},
        # prošla ověřením novým kódem před 30 h, teď je mrtvá = čerstvá zpráva
        {"id": "dead-fresh", "source": "idnes", "search_missed": True,
         "verified_live_at": "2026-09-26T04:00:00Z"},
        # ověřená před 5 h: znovu se neptá
        {"id": "recent", "source": "idnes", "search_missed": True,
         "verified_live_at": "2026-09-27T05:00:00Z"},
        {"id": "live", "source": "bezrealitky"},
        {"id": "sr-dead", "source": "sreality"},
    ]
    changes = {"generated_at": NOW, "newly_inactive": [dict(c) for c in cands]}
    curr = {"comparables": []}
    scrape.verify_removals(changes, curr)
finally:
    scrape.listing_is_gone = real

check("ověřená do 24 h se znovu neptá", "recent" in asked, False)
check("ostatní se ptají", sorted(asked), ["dead-fresh", "dead-zombie", "live", "sr-dead"])
gone = {c["id"]: c for c in changes["newly_inactive"]}
check("potvrzeně pryč", sorted(gone), ["dead-fresh", "dead-zombie", "sr-dead"])
check("dávná zombie = stale_ghost", gone["dead-zombie"].get("stale_ghost"), True)
check("čerstvé zmizení není zombie", gone["dead-fresh"].get("stale_ghost"), None)
check("datum z iDNES", gone["dead-fresh"].get("removed_since"), "2026-09-26T00:00:00Z")
back = {c["id"]: c for c in curr["comparables"]}
check("živé se vrací do tabulky", sorted(back), ["live", "recent"])
check("živý dostane razítko ověření", back["live"].get("verified_live_at"), NOW)
check("neověřovaný si nechá staré razítko", back["recent"].get("verified_live_at"),
      "2026-09-27T05:00:00Z")

# --- zombie se nehlásí: historie ani týdenní úbytek ---------------------- #
# Nic se nezapisuje na disk: log i historie jdou do paměti.
events = []
scrape.append_changes_log = lambda evs: events.extend(evs)
scrape.load_changes_history = lambda: []


class _Sink:
    def write_text(self, *_a, **_k):
        pass


scrape.CHANGES_HISTORY_PATH = _Sink()
scrape.update_changes_history(changes)
check("historie hlásí jen čerstvé zmizení",
      sorted(e["id"] for e in events if e["kind"] == "removed"), ["dead-fresh", "sr-dead"])

recs = {}
for cid in ("dead-zombie", "dead-fresh"):
    recs[cid] = {"id": cid, "first_seen": "2026-09-01T00:00:00Z", "last_seen": "2026-09-26T00:00:00Z"}
pool.update_from_snapshot(recs, {"comparables": []}, changes, at=NOW)
check("pool zombii uzavře", recs["dead-zombie"].get("gone_at"), NOW)
check("a označí ji", recs["dead-zombie"].get("gone_stale"), True)
_, left = market.period_movement(recs, "2026-09-21T00:00:00Z", "2026-09-28T00:00:00Z")
check("týdenní úbytek bez zombie", [r["id"] for r in left], ["dead-fresh"])

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
