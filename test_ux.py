#!/usr/bin/env python3
"""UX vrstva (ux.py): čerstvost, „co je nového", odkaz na inzerát, alert
o selhání běhu. Offline; JS se tu kontroluje jen staticky (bezpečnostní
vzory) -- chování v prohlížeči ověřuje jsdom mimo CI."""
import copy
import json
import re
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import ux

FAILURES = []


def check(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + f" {name:<60} {got!r}")
    if not ok:
        FAILURES.append(f"{name}: {got!r} != {want!r}")


# --- čerstvost -------------------------------------------------------------- #
yml = Path(".github/workflows/scrape.yml").read_text(encoding="utf-8")
cron = re.search(r'cron:\s*"0 \*/(\d+) \* \* \*"', yml)
check("CRON_EVERY_H odpovídá cronu ve workflow", int(cron.group(1)) if cron else None, ux.CRON_EVERY_H)
check("další slot po 14:24", ux.iso(ux.next_run_after("2026-09-27T14:24:59Z")), "2026-09-27T16:00:00Z")
check("přesně na slotu -> až další", ux.iso(ux.next_run_after("2026-09-27T16:00:00Z")), "2026-09-27T20:00:00Z")
check("přes půlnoc", ux.iso(ux.next_run_after("2026-09-27T23:10:00Z")), "2026-09-28T00:00:00Z")
check("neplatný čas", ux.next_run_after("nesmysl"), None)
check("prahy", [ux.freshness_level(h) for h in (0, 6, 6.01, 10, 10.01, None)],
      ["ok", "ok", "amber", "amber", "red", "red"])
f = ux.freshness("2026-09-27T14:24:59Z", "2026-09-28T01:00:00Z")
check("freshness 10,6 h = red", (f["level"], round(f["age_h"], 1)), ("red", 10.6))
check("fmt_age", [ux.fmt_age(m) for m in (3, 60, 125, 1500)],
      ["před 3 min", "před 1 h", "před 2 h", "před 1 d 1 h"])

# --- odkaz na inzerát ------------------------------------------------------- #
check("dashboard_link číslo", ux.dashboard_link("https://x.github.io/t/", 123), "https://x.github.io/t/#byt=123")
check("dashboard_link zahodí starý hash", ux.dashboard_link("https://x.github.io/t/#a", "bez-9"),
      "https://x.github.io/t/#byt=bez-9")
check("dashboard_link escapuje", ux.dashboard_link("https://x/", 'a"<b>'), "https://x/#byt=a%22%3Cb%3E")
check("dashboard_link bez base", ux.dashboard_link(None, 1), None)

# --- changes_since ---------------------------------------------------------- #
B = "2026-09-26T12:00:00Z"
facts = [
    {"s": "byt", "id": 1, "fs": "2026-09-27T01:00:00Z", "p": 100},                  # nový
    {"s": "byt", "id": 2, "fs": "2026-09-20T01:00:00Z", "g": "2026-09-27T01:00:00Z"},  # zmizel
    {"s": "byt", "id": 3, "fs": "2026-09-26T13:00:00Z", "g": "2026-09-27T01:00:00Z"},  # přišel a odešel
    {"s": "nov", "id": 4, "fs": "2026-09-27T01:00:00Z", "b": True, "p": 5},          # baseline sběru
    {"s": "byt", "id": 5, "p": 90, "pe": [["2026-09-25T00:00:00Z", 120, 100],
                                          ["2026-09-26T20:00:00Z", 100, 95],
                                          ["2026-09-27T02:00:00Z", 95, 90]]},       # 100 -> 90
    {"s": "byt", "id": 6, "p": 100, "pe": [["2026-09-27T00:00:00Z", 100, 110],
                                           ["2026-09-27T05:00:00Z", 110, 100]]},     # tam a zpět
    {"s": "garaz", "id": 7, "p": 2000, "pe": [["2026-09-27T00:00:00Z", 1800, 2000]]},  # zdražení
    {"s": "byt", "id": 8, "p": 50, "pe": [["2026-09-20T00:00:00Z", 60, 50]]},        # před baseline
    {"s": "garaz", "id": 9, "g": "2026-09-27T00:00:00Z"},                            # bez fs = starý
]
res = ux.changes_since(facts, B)
by = {i["id"]: i for i in res["items"]}
check("počty", res["counts"], {"new": 1, "down": 1, "up": 1, "gone": 2})
check("nový", by[1]["kind"], "new")
check("zmizelý viděný před baseline", by[2]["kind"], "gone")
check("objevil se a zmizel mezi návštěvami se nehlásí", 3 in by, False)
check("baseline novostavba není nová", 4 in by, False)
check("zlevnění od ceny při baseline", (by[5]["kind"], by[5]["old"], by[5]["cur"], by[5]["pct"]),
      ("down", 100, 90, -10.0))
check("tam a zpět = žádná změna", 6 in by, False)
check("zdražení", (by[7]["kind"], by[7]["pct"]), ("up", 11.1))
check("změna před baseline se nepočítá", 8 in by, False)
check("zmizelý bez first_seen", by[9]["kind"], "gone")

# --- build_payload -------------------------------------------------------- #
snap = {
    "generated_at": "2026-09-27T14:24:59Z",
    "comparables": [{"id": 1, "price_czk": 100}, {"id": "bez-2", "price_czk": 200}, {"id": 3, "price_czk": 300}],
    "tracked": [{"id": 4, "rent_czk": 15000}],
    "garages": [{"id": 10, "first_seen": "2026-07-01T00:00:00Z", "price_czk": 1},
                {"id": 11, "first_seen": "2026-09-20T00:00:00Z", "gone_at": "2026-09-27T00:00:00Z", "price_czk": 2}],
    "novostavby": [{"id": 20, "first_seen": "2026-09-27T14:26:01Z", "baseline": True, "price_czk": 9,
                    "url": "javascript:alert(1)", "thumb": "http://x/y.jpg",
                    "price_history": [{"at": "2026-09-27T14:26:01Z", "price_czk": 10},
                                      {"at": "2026-09-27T18:00:00Z", "price_czk": 9}]},
                   {"id": 21, "out_of_scope": True}],
}
first = {"1": "2026-09-27T10:00:00Z", "bez-2": "2026-06-01T00:00:00Z", "3": "2026-09-01T00:00:00+00:00"}
log = [{"at": "2026-09-27T10:00:00Z", "kind": "price_change", "id": "bez-2", "old_price_czk": 250, "new_price_czk": 200},
       {"at": "2026-07-01T10:00:00Z", "kind": "price_change", "id": 3, "old_price_czk": 1, "new_price_czk": 300},
       {"at": "2026-09-27T09:00:00Z", "kind": "new", "id": 4},
       {"at": "2026-09-27T09:00:00Z", "kind": "price_change", "id": 4, "old_price_czk": 5, "new_price_czk": 5}]
gone_rows = [{"id": 1, "gone_at": "2026-09-26T00:00:00Z", "first_seen": "2026-09-01T00:00:00Z"},
             {"id": 99, "gone_at": "2026-09-26T00:00:00Z", "first_seen": "2026-09-01T00:00:00Z", "last_price_czk": 7}]
pl = ux.build_payload(snap, gone_rows, first_seen=first, log_events=log)
fb = {(f["s"], str(f["id"])): f for f in pl["facts"]}
check("okno 30 dní", pl["window_start"], "2026-08-28T14:24:59Z")
check("nový byt s first_seen z poolu", fb[("byt", "1")].get("fs"), "2026-09-27T10:00:00Z")
check("starý byt se změnou ceny: bez fs, s pe", (fb[("byt", "bez-2")].get("fs"), fb[("byt", "bez-2")]["pe"]),
      (None, [["2026-09-27T10:00:00Z", 250, 200]]))
check("first_seen normalizovaný", fb[("byt", "3")]["fs"], "2026-09-01T00:00:00Z")
check("změna mimo okno vynechána", "pe" in fb[("byt", "3")], False)
check("sledovaný: cena z rent_czk, fs z logu 'new'", (fb[("byt", "4")]["p"], fb[("byt", "4")]["fs"]),
      (15000, "2026-09-27T09:00:00Z"))
check("živý byt se nezdvojí zmizelým řádkem", sum(1 for f in pl["facts"] if str(f["id"]) == "1"), 1)
check("zmizelý byt z archivu", (fb[("byt", "99")]["g"], fb[("byt", "99")]["p"]), ("2026-09-26T00:00:00Z", 7))
check("stará garáž mimo okno vynechána", ("garaz", "10") in fb, False)
check("zmizelá garáž", fb[("garaz", "11")]["g"], "2026-09-27T00:00:00Z")
check("novostavba: baseline + změna ceny", (fb[("nov", "20")]["b"], fb[("nov", "20")]["pe"]),
      (True, [["2026-09-27T18:00:00Z", 10, 9]]))
check("novostavba mimo scope vynechána", [n["id"] for n in pl["nov"]], [20])
check("novostavba: ne-https url/thumb pryč", ("url" in pl["nov"][0], "thumb" in pl["nov"][0]), (False, False))
import ribbon  # noqa: E402
check("práh chyby dat = ribbon", ux.DROP_MAX_PCT, ribbon.DROP_MAX_PCT)
check("prahy v payloadu", (pl["amber_h"], pl["red_h"], pl["cron_every_h"]), (6, 10, ux.CRON_EVERY_H))

# --- JS: bezpečnostní vzory ------------------------------------------------- #
js = ux.JS
check("žádný inline handler s interpolací", re.findall(r'on[a-z]+="[^"]*\$\{', js), [])
check("žádný JSON.stringify v atributu", re.findall(r'data-[a-z-]+="\$\{JSON', js), [])
check("localStorage jen přes try/catch wrappery",
      sorted(set(re.findall(r"localStorage\.\w+", js))), ["localStorage.getItem", "localStorage.setItem"])
check("wrappery mají try", "function uxGet(k) { try {" in js and "try { localStorage.setItem" in js, True)
check("payload se vkládá", "__UX_JSON__" not in ux.page_js("{}"), True)

# --- alert o selhání běhu -------------------------------------------------- #
env = {"RUN_URL": 'https://github.com/x/actions/runs/1?a="<b>', "RUN_EVENT": "schedule"}
t = ux.failure_alert_text(env)
check("alert: odkaz escapovaný", '"<b>' in t, False)
check("alert: odkaz na běh", "actions/runs/1" in t, True)
check("alert: zrušený běh", "zrušen" in ux.failure_alert_text({"JOB_STATUS": "cancelled"}), True)
import notify  # noqa: E402
with patch.object(notify, "send_telegram", Mock(return_value=True)) as st:
    check("bez secrets: nic se neposílá, exit 0", ux.send_failure_alert({}), 0)
    check("bez secrets: send nevolán", st.called, False)
    rc = ux.send_failure_alert({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c", "RUN_URL": "https://r"})
    check("se secrets: posláno přes notify.send_telegram", (rc, st.call_count), (0, 1))
with patch.object(notify, "send_telegram", Mock(side_effect=notify.NotifyError("403"))):
    check("chyba Telegramu nevyhodí, exit 0",
          ux.send_failure_alert({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}), 0)
check("workflow: krok alertu s if: failure()", "if: failure() || cancelled()" in yml
      and "python3 ux.py notify-failure || true" in yml, True)
check("workflow: test_ux v CI", "python test_ux.py" in yml, True)

# --- alert novostaveb nese odkaz na dashboard ------------------------------- #
import novostavby as nov  # noqa: E402
rec = {"id": 3622817868, "kind": "dokoncena", "lat": nov.ALERT_CENTER[0], "lon": nov.ALERT_CENTER[1], "km": 0.1,
       "disposition": "4+kk", "transaction_type": "prodej", "price_czk": 1, "street": "U kříže",
       "url": "https://www.sreality.cz/detail/prodej/byt/4+kk/x/3622817868"}
txt = nov.build_alert([{"kind": "new", "rec": rec}], "2026-09-27T14:00:00Z", "https://a.github.io/b/")
check("novostavba: odkaz #byt=", 'href="https://a.github.io/b/#byt=3622817868">na dashboardu</a>' in txt, True)
txt2 = nov.build_alert([{"kind": "new", "rec": rec}], "2026-09-27T14:00:00Z")
check("novostavba bez dashboard_url: bez odkazu", "#byt=" in txt2, False)

# --- integrace s render_dashboard -------------------------------------------- #
import scrape  # noqa: E402
snapshot = json.loads(Path("latest_snapshot.json").read_text())
history = json.loads(Path("changes_history.json").read_text())
target = Mock()
with patch.object(scrape, "DASHBOARD_PATH", target):
    scrape.render_dashboard(copy.deepcopy(snapshot), {"new": [], "removed": [], "price_changes": []},
                            snapshot["stats"], history)
doc = target.write_text.call_args.args[0]
for needle in ('id="uxFresh"', 'id="uxQ"', 'id="uxNews"', 'id="favCard"', 'data-target="favCard"',
               'id="uxSort"', "var UX = {", "uxInit();"):
    check(f"stránka obsahuje {needle}", needle in doc, True)
check("UX payload je platný JSON", bool(json.loads(re.search(r"var UX = (.*?);\nvar uxReady", doc, re.S).group(1))), True)
check("ux JS je před ribbonem", doc.index("uxInit();") < doc.index("initRibbon();"), True)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED:")
    for f in FAILURES:
        print(f"  {f}")
    sys.exit(1)
print("all ux checks pass")
