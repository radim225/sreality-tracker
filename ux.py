#!/usr/bin/env python3
"""UX vrstva dashboardu: co je nového od posledního přečtení, oblíbené,
čerstvost dat, odkaz přímo na inzerát, hledání a zlevnění v tabulce.

Radim (27. 9. 2026) chtěl dashboard, který mu po otevření řekne, co se změnilo,
a který dává zpětnou vazbu na každé kliknutí -- tichá selhání jsou v tomhle
projektu opakovaná třída chyb.

Rozdělení práce (deterministické jádro, žádné LLM):
  * Python (`build_payload`) připraví FAKTA: kdy se který inzerát poprvé
    objevil, kdy zmizel a kdy se mu měnila cena -- jen za posledních
    WINDOW_DAYS dní, aby stránka nenabobtnala. `changes_since` je referenční
    výpočet „co je nového od času X"; JS (`uxChangesSince`) je jeho přesný
    přepis a test ho porovnává s Pythonem.
  * Všechno, co patří jednomu čtenáři (kdy naposledy klikl „přečteno",
    oblíbené), žije jen v localStorage jeho prohlížeče. Stránka je veřejná
    (GitHub Pages): nic osobního se neposílá na server ani do repa. Každý
    přístup k localStorage je v try/catch a stránka bez něj funguje.

Bezpečnost: scrapované texty jdou do HTML jen přes escapeHtml, id inzerátu
se nikdy neskládá do inline handleru -- nese ho data-atribut a čte ho jeden
delegovaný posluchač (inline onclick s JSON.stringify už v repu jednou
rozbil ukládání oprav).

Modul záměrně neimportuje scrape.py (scrape importuje jeho). CLI:
`python3 ux.py notify-failure` pošle Telegram o selhaném běhu workflow.
"""
import html
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).parent
CHANGES_LOG_PATH = ROOT / "changes_log.jsonl"
REPO = "radim225/sreality-tracker"
ACTIONS_RUNS_URL = f"https://github.com/{REPO}/actions/workflows/scrape.yml"

# Zrcadlí cron v .github/workflows/scrape.yml ("0 */4 * * *"); test_ux hlídá,
# že se neroztečou.
CRON_EVERY_H = 4
# Skutečné běhy chodí nepravidelně (GitHub cron zpožďuje i o hodiny; změřeno
# z commitů 20.–27. 9.: mezery 3–9 h). Proto žlutá až po 6 h a červená po
# 10 h -- dřív by svítila skoro každý den naprázdno.
AMBER_H = 6
RED_H = 10
# Kolik dní historie změn stránka nese. Starší „poslední přečtení" dostane
# přiznanou poznámku, že výčet je jen za tohle okno.
WINDOW_DAYS = 30
# Pokles o víc než polovinu není sleva, ale chyba v datech (cena „1 Kč",
# přehozený pronájem/prodej) -- stejný práh jako ribbon.DROP_MAX_PCT.
# Řazení „největší zlevnění" takové řádky dává na konec.
DROP_MAX_PCT = 50


# --- čas -------------------------------------------------------------------- #
def parse_ts(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    try:
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def iso(dt):
    """Jeden tvar pro všechna razítka -- JS je porovnává jako řetězce."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def norm_ts(value):
    return iso(parse_ts(value))


def next_run_after(generated_at, every_h=CRON_EVERY_H):
    """Nejbližší plánovaný slot cronu (UTC násobek every_h) PO času dat.
    Je to plán, ne slib: GitHub běhy běžně posouvá."""
    t = parse_ts(generated_at)
    if t is None:
        return None
    base = t.replace(minute=0, second=0, microsecond=0)
    slot = base.replace(hour=(base.hour // every_h) * every_h)
    while slot <= t:
        slot += timedelta(hours=every_h)
    return slot


def freshness_level(age_h):
    if age_h is None:
        return "red"
    if age_h > RED_H:
        return "red"
    if age_h > AMBER_H:
        return "amber"
    return "ok"


def freshness(generated_at, now):
    t, n = parse_ts(generated_at), parse_ts(now)
    if t is None or n is None:
        return {"age_h": None, "level": "red", "next_run": None}
    age_h = max(0.0, (n - t).total_seconds() / 3600)
    return {"age_h": age_h, "level": freshness_level(age_h), "next_run": iso(next_run_after(t))}


def fmt_age(minutes):
    """„před 5 min" / „před 2 h" / „před 1 d 3 h" -- stejně jako uxAgeTxt v JS."""
    m = max(0, int(minutes))
    if m < 60:
        return f"před {m} min"
    h = m // 60
    if h < 24:
        return f"před {h} h"
    return f"před {h // 24} d {h % 24} h"


# --- odkaz na inzerát ------------------------------------------------------- #
def dashboard_link(base_url, listing_id):
    """`…/#byt=<id>` -- stránka po načtení otevře detail inzerátu."""
    if not base_url or listing_id is None:
        return None
    return base_url.split("#")[0] + "#byt=" + quote(str(listing_id), safe="-_")


# --- fakta pro „co je nového" ----------------------------------------------- #
def _load_pool_first_seen():
    try:
        import pool
        return {k: r.get("first_seen") for k, r in pool.load_pool().items()}
    except (Exception, SystemExit) as exc:  # noqa: BLE001 -- stránka se vykreslí i bez toho
        print(f"::warning::ux: pool se nenačetl, „nové“ jen z logu změn: {exc}", file=sys.stderr)
        return {}


def _load_log(since):
    out = []
    try:
        with CHANGES_LOG_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                at = norm_ts(ev.get("at"))
                if at and at >= since:
                    ev["at"] = at
                    out.append(ev)
    except OSError:
        pass
    return out


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def build_payload(snapshot, gone_rows, *, first_seen=None, log_events=None):
    """Data pro JS. `first_seen` (id -> ISO) a `log_events` jdou předat
    v testech; jinak se čtou z poolu a z changes_log.jsonl."""
    gen = norm_ts(snapshot.get("generated_at"))
    gen_dt = parse_ts(gen)
    ws = iso(gen_dt - timedelta(days=WINDOW_DAYS)) if gen_dt else "0000"
    if first_seen is None:
        first_seen = _load_pool_first_seen()
    if log_events is None:
        log_events = _load_log(ws)

    new_at, price_ev = {}, defaultdict(list)
    for ev in log_events:
        at = norm_ts(ev.get("at"))
        if not at or at < ws:
            continue
        sid = str(ev.get("id"))
        if ev.get("kind") == "new":
            new_at[sid] = min(at, new_at.get(sid, at))
        elif ev.get("kind") == "price_change":
            old, new = _num(ev.get("old_price_czk")), _num(ev.get("new_price_czk"))
            if old and new and old != new:
                price_ev[sid].append([at, old, new])

    facts = []

    def add(fact):
        fact = {k: v for k, v in fact.items() if v not in (None, [], False)}
        facts.append(fact)

    flats = {}
    for c in list(snapshot.get("comparables") or []) + list(snapshot.get("tracked") or []):
        flats.setdefault(str(c.get("id")), c)
    for sid, c in flats.items():
        fs = norm_ts(first_seen.get(sid)) or new_at.get(sid)
        fs = fs if fs and fs >= ws else None
        pe = sorted(price_ev.get(sid, []))
        if not fs and not pe:
            continue
        price = _num(c.get("price_czk"))
        if price is None:
            price = _num(c.get("rent_czk"))
        add({"s": "byt", "id": c.get("id"), "fs": fs, "p": price, "pe": pe})
    for g in gone_rows or []:
        sid = str(g.get("id"))
        gone = norm_ts(g.get("gone_at"))
        if sid in flats or not gone or gone < ws:
            continue
        add({"s": "byt", "id": g.get("id"), "fs": norm_ts(g.get("first_seen")), "g": gone,
             "p": _num(g.get("last_price_czk"))})

    for g in snapshot.get("garages") or []:
        fs, gone = norm_ts(g.get("first_seen")), norm_ts(g.get("gone_at"))
        if not ((fs and fs >= ws) or (gone and gone >= ws)):
            continue
        add({"s": "garaz", "id": g.get("id"), "fs": fs, "g": gone, "p": _num(g.get("price_czk"))})

    nov_list = []
    for r in snapshot.get("novostavby") or []:
        if r.get("out_of_scope"):
            continue
        nov_list.append(nov_for_page(r))
        fs, gone = norm_ts(r.get("first_seen")), norm_ts(r.get("gone_at"))
        pts = [(norm_ts(h.get("at")), _num(h.get("price_czk"))) for h in r.get("price_history") or []]
        pts = [p for p in pts if p[0] and p[1]]
        pe = [[pts[i][0], pts[i - 1][1], pts[i][1]] for i in range(1, len(pts))
              if pts[i][0] >= ws and pts[i][1] != pts[i - 1][1]]
        if not ((fs and fs >= ws) or (gone and gone >= ws) or pe):
            continue
        # b = baseline: byl v nabídce už při prvním běhu sběru, není „nový".
        add({"s": "nov", "id": r.get("id"), "fs": fs, "g": gone, "b": bool(r.get("baseline")),
             "p": _num(r.get("price_czk")), "pe": pe})

    return {
        "generated_at": gen,
        "window_start": ws,
        "window_days": WINDOW_DAYS,
        "facts": facts,
        "nov": nov_list,
        "cron_every_h": CRON_EVERY_H,
        "amber_h": AMBER_H,
        "red_h": RED_H,
        "drop_max_pct": DROP_MAX_PCT,
        "actions_url": ACTIONS_RUNS_URL,
    }


NOV_FIELDS = ("id", "title", "disposition", "transaction_type", "price_czk", "price_czk_per_sqm",
              "floor_area_sqm", "street", "city_part", "locality", "lat", "lon", "km", "url", "thumb",
              "first_seen", "gone_at", "kind", "kind_reason", "price_history", "since")


def nov_for_page(r):
    out = {k: r[k] for k in NOV_FIELDS if r.get(k) is not None}
    for k in ("url", "thumb"):
        if not (isinstance(out.get(k), str) and out[k].startswith("https://")):
            out.pop(k, None)
    return out


def changes_since(facts, baseline):
    """Referenční výpočet. Pro každý inzerát nejvýš jedna položka:
      * gone: zmizel po baseline a byl vidět už před ní (objevil se a zmizel
        mezi dvěma návštěvami = nikdy ho neviděl, nehlásí se),
      * new:  objevil se po baseline a žije (u novostaveb ne baseline záznam),
      * down/up: žije, starší než baseline, cena teď != cena při baseline
        (cena při baseline = „stará" cena první změny po baseline).
    JS uxChangesSince musí dát totéž -- test to porovnává."""
    items = []
    for f in facts:
        fs, g = f.get("fs"), f.get("g")
        if g:
            if g > baseline and (not fs or fs <= baseline):
                items.append({"s": f["s"], "id": f["id"], "kind": "gone", "at": g})
            continue
        if fs and fs > baseline and not f.get("b"):
            items.append({"s": f["s"], "id": f["id"], "kind": "new", "at": fs})
            continue
        after = [e for e in f.get("pe") or [] if e[0] > baseline]
        cur = f.get("p")
        if after and cur and after[0][1] and after[0][1] != cur:
            old = after[0][1]
            items.append({"s": f["s"], "id": f["id"], "kind": "down" if cur < old else "up",
                          "at": after[-1][0], "old": old, "cur": cur,
                          "pct": round((cur - old) / old * 100, 1)})
    counts = {k: sum(1 for i in items if i["kind"] == k) for k in ("new", "down", "up", "gone")}
    return {"items": items, "counts": counts}


# --- HTML / CSS --------------------------------------------------------------- #
def header_html():
    """Do <header>: čerstvost dat a hledání. Obsah plní JS."""
    return """<div class="ux-hrow">
    <span id="uxFresh" class="ux-fresh" role="status" aria-live="polite"></span>
    <div class="ux-search">
      <input id="uxQ" type="search" autocomplete="off" spellcheck="false"
             placeholder="🔎 Ulice, číslo inzerátu nebo vlož odkaz…" aria-label="Hledat inzerát">
      <div id="uxQRes" class="ux-qres" role="listbox" hidden></div>
    </div>
  </div>"""


def banner_html():
    return """<section id="uxNews" class="ux-news" hidden aria-live="polite"></section>
<div id="uxToast" class="ux-toast" role="status" aria-live="polite" hidden></div>"""


def fav_card_html():
    return """<div class="card" id="favCard">
  <h2 style="margin-top:0;font-size:1rem;">★ Oblíbené <span id="favCount" class="hint"></span></h2>
  <div id="favList"></div>
  <p class="hint">Oblíbené jsou uložené <b>jen v tomhle prohlížeči</b> (localStorage) — na telefonu a na počítači
    jsou zvlášť, nic se neposílá na server. Hvězdičku ☆ najdeš u každého řádku a v detailu inzerátu.
    U zmizelého inzerátu zůstane poslední známá cena.</p>
</div>"""


def table_controls_html():
    return """<label class="ux-sortl">Řazení
    <select id="uxSort">
      <option value="">podle sloupce (klik na hlavičku)</option>
      <option value="drop">největší zlevnění</option>
    </select></label>"""


CSS = """
  /* ---- UX vrstva (ux.py) ---- */
  .ux-hrow { display: flex; gap: 8px 12px; align-items: center; flex-wrap: wrap; margin-top: 6px; }
  .ux-fresh { font-size: 0.75rem; color: #9aa; }
  .ux-fresh.amber { color: #fc6; }
  .ux-fresh.red { color: #ff8a8a; font-weight: 600; }
  .ux-fresh a { color: inherit; text-decoration: underline; }
  .ux-search { position: relative; flex: 1 1 260px; max-width: 460px; min-width: 0; }
  .ux-search input { width: 100%; box-sizing: border-box; }
  .ux-qres { position: absolute; left: 0; right: 0; top: calc(100% + 4px); z-index: 30; background: #1b1f29;
             border: 1px solid #2a2f3a; border-radius: 8px; max-height: 60vh; overflow-y: auto;
             box-shadow: 0 6px 18px rgba(0,0,0,.5); }
  .ux-qi { display: flex; gap: 8px; align-items: center; width: 100%; background: none; border: none;
           border-bottom: 1px solid #232733; color: inherit; text-align: left; padding: 6px 8px;
           cursor: pointer; font: inherit; font-size: 0.8rem; }
  .ux-qi:hover, .ux-qi.sel { background: #22304a; }
  .ux-qi img { width: 40px; height: 32px; object-fit: cover; border-radius: 4px; flex-shrink: 0; background: #11141b; }
  .ux-qi .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ux-qi .m { font-size: 0.68rem; color: #889; }
  .ux-qmsg { padding: 8px 10px; font-size: 0.8rem; color: #ccd; }
  .ux-sec { font-size: 0.6rem; padding: 0 5px; border-radius: 6px; background: #262b36; color: #aab;
            white-space: nowrap; }
  .ux-news { margin: 12px 12px 0; padding: 10px 12px; background: #172033; border: 1px solid #25406b;
             border-radius: 10px; font-size: 0.85rem; }
  .ux-news.quiet { background: #161a22; border-color: #262a33; color: #aab; }
  .ux-news .ux-cnt { display: inline; background: none; border: none; color: #7ab8ff; cursor: pointer;
                     font: inherit; padding: 0; text-decoration: underline; }
  .ux-news .ux-cnt[aria-expanded="true"] { color: #fff; }
  .ux-news .ux-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 6px; }
  .ux-news .ux-list { margin-top: 8px; display: flex; flex-direction: column; gap: 2px; }
  .ux-li { display: flex; gap: 8px; align-items: center; background: #11141b; border: none; color: inherit;
           border-radius: 6px; padding: 5px 8px; cursor: pointer; text-align: left; font: inherit; font-size: 0.8rem; }
  .ux-li:hover { background: #1d2636; }
  .ux-li .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ux-b { display: inline-block; font-size: 0.62rem; font-weight: 700; padding: 1px 5px; border-radius: 8px;
          margin-left: 4px; white-space: nowrap; vertical-align: middle; }
  .ux-b.new { background: #152338; color: #9cc9ff; }
  .ux-b.down { background: #14301f; color: #7CFFB2; }
  .ux-b.up { background: #3a1c1c; color: #ff9a9a; }
  .ux-b.gone { background: #2b2b2b; color: #bbb; }
  .ux-star { background: none; border: none; color: #778; cursor: pointer; font-size: 1rem; line-height: 1;
             padding: 2px 4px; vertical-align: middle; }
  .ux-star.on { color: #fc6; }
  .ux-star:focus-visible { outline: 1px solid #7ab8ff; border-radius: 4px; }
  .ux-mbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 0 0 8px; }
  .ux-mbar .popup-btn { margin-top: 0; }
  .ux-mbar .popup-btn.ux-ghost { background: #262b36; color: #dde; }
  .ux-mbar .popup-btn.ux-ghost.on { background: #4a3c1c; color: #fc6; }
  .ux-copy-fallback { width: 100%; box-sizing: border-box; margin-top: 4px; }
  .ux-toast { position: fixed; left: 50%; transform: translateX(-50%);
              bottom: calc(16px + env(safe-area-inset-bottom, 0px)); z-index: 80; background: #243044;
              color: #eef; border: 1px solid #3b5277; border-radius: 10px; padding: 8px 14px; font-size: 0.85rem;
              max-width: calc(100vw - 32px); box-sizing: border-box; box-shadow: 0 6px 18px rgba(0,0,0,.5);
              display: flex; gap: 10px; align-items: center; }
  .ux-toast.err { background: #3a1c1c; border-color: #7f1d1d; color: #fdd; }
  .ux-toast button { background: none; border: none; color: #7ab8ff; cursor: pointer; font: inherit;
                     text-decoration: underline; padding: 0; }
  .fav-row { display: flex; gap: 10px; align-items: center; padding: 6px 0; border-bottom: 1px solid #232733; }
  .fav-row .fav-open { flex: 1; min-width: 0; display: flex; gap: 10px; align-items: center; background: none;
                       border: none; color: inherit; text-align: left; cursor: pointer; font: inherit; padding: 0; }
  .fav-row .fav-txt { min-width: 0; font-size: 0.82rem; }
  .fav-row .fav-t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fav-row .fav-m { font-size: 0.7rem; color: #889; }
  .fav-row.gone .fav-t { color: #999; text-decoration: line-through; }
  .ux-sortl { display: flex; align-items: center; gap: 6px; font-size: 0.85rem; }
  .pmove .ux-days { font-weight: 400; opacity: .85; }
  @media (max-width: 480px) {
    .ux-search { flex-basis: 100%; max-width: none; }
    .ux-news { font-size: 0.8rem; }
  }
"""


def page_js(payload_json):
    """JS vrstvy. Vkládá se do hlavního skriptu PŘED ribbon (ten startuje
    poslední a jeho `areachange` překreslí tabulky i s našimi odznaky).

    `payload_json` musí být escapovaný pro <script> (scrape.script_json).
    Předpoklady: escapeHtml, fmtCzk, fmtDay, daysBetween, safeImg, PLACEHOLDER,
    ALL, ALL_BY_ID, GARAGES, GARAGE_BY_ID, GONE, GONE_BY_ID, DATA, openModal,
    openGarage, openGone, render, renderPodHarfou, renderGarages, priceMove,
    manageTracked, GH_ACTIONS_URL, ribJump (ribbon). Hooky volané z šablon ve
    scrape.py (uxRowBadges, uxDropSuffix, uxModalOpened, uxModalClosed) smí
    běžet i dřív, než tenhle kód doběhne: jsou to hoistované funkce a stav
    drží ve `var`, které do inicializace vracejí prázdno."""
    return JS.replace("__UX_JSON__", payload_json)


JS = r"""
// ---- UX vrstva (ux.py): novinky, oblíbené, čerstvost, odkazy, hledání -----
var UX = __UX_JSON__;
var uxReady = false;          // do uxInit vrací hooky prázdno
var uxBaseline = null;        // ISO: data, která čtenář naposledy označil jako přečtená
var uxMarkedAt = null;        // kdy to označil (hodiny prohlížeče)
var uxStorageOk = true;
var uxByKey = new Map();      // "s:id" -> položka z uxChangesSince
var uxFavs = null;            // {"s:id": {...}}
var uxFavCorrupt = false;
var uxNovById = new Map();
var uxOpenKey = null;         // co je teď v modalu
var uxQSel = 0;

const UX_SEEN_KEY = "ux:seen:v1";
const UX_FAV_KEY = "ux:fav:v1";
const UX_SEC_LABEL = { byt: "byt", garaz: "garáž", nov: "novostavba" };

function uxGet(k) { try { return localStorage.getItem(k); } catch (e) { uxStorageOk = false; return null; } }
function uxSet(k, v) {
  try { localStorage.setItem(k, v); return true; } catch (e) { uxStorageOk = false; return false; }
}
function uxKey(s, id) { return s + ":" + String(id); }
function uxEsc(v) { return escapeHtml(v == null ? "" : String(v)); }

// ---- čas -------------------------------------------------------------------
function uxParse(iso) { const t = Date.parse(iso); return isFinite(t) ? t : null; }
function uxHm(ms) {
  const d = new Date(ms);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}
function uxWhen(ms) {
  // „dnes 14:28" / „včera 21:14" / „25. 9. 21:14" -- v čase prohlížeče.
  const d = new Date(ms), now = new Date();
  const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  if (diff === 0) return "dnes " + uxHm(ms);
  if (diff === 1) return "včera " + uxHm(ms);
  return `${d.getDate()}. ${d.getMonth() + 1}. ${uxHm(ms)}`;
}
function uxAgeTxt(minutes) {
  const m = Math.max(0, Math.floor(minutes));
  if (m < 60) return `před ${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `před ${h} h`;
  return `před ${Math.floor(h / 24)} d ${h % 24} h`;
}
function uxLevel(ageH) {
  if (ageH == null || !isFinite(ageH)) return "red";
  if (ageH > UX.red_h) return "red";
  if (ageH > UX.amber_h) return "amber";
  return "ok";
}
function uxNextRun(genMs) {
  // Slot cronu v UTC (násobek cron_every_h hodin) po čase dat. Plán, ne slib.
  const step = UX.cron_every_h * 3600000;
  return Math.floor(genMs / step) * step + step;
}

// ---- čerstvost dat ----------------------------------------------------------
function uxRenderFresh() {
  const el = document.getElementById("uxFresh");
  if (!el) return;
  const gen = uxParse(UX.generated_at);
  if (gen == null) { el.textContent = "Čas dat neznámý"; el.className = "ux-fresh red"; return; }
  const ageMin = (Date.now() - gen) / 60000;
  const lvl = uxLevel(ageMin / 60);
  const next = uxNextRun(gen);
  const nextTxt = next > Date.now() ? `další běh cca ${uxHm(next)}`
    : `další běh se čeká (plán ${uxHm(next)}, GitHub běhy posouvá)`;
  let html = `Data: ${uxEsc(uxWhen(gen))} (${uxEsc(uxAgeTxt(ageMin))}) · ${uxEsc(nextTxt)}`;
  if (lvl !== "ok") {
    html += ` · ${lvl === "red" ? "⚠ " : ""}běh se možná nepovedl — <a href="${uxEsc(UX.actions_url)}" target="_blank" rel="noopener">zkontroluj Actions</a>`;
  }
  el.innerHTML = html;
  el.className = "ux-fresh" + (lvl === "ok" ? "" : " " + lvl);
  el.title = "Čas je čas běhu scraperu, ne otevření stránky. Běhy jsou plánované každé "
    + UX.cron_every_h + " h, GitHub je ale často o hodiny posune. Žlutá po " + UX.amber_h
    + " h, červená po " + UX.red_h + " h bez nových dat.";
}

// ---- co je nového ---------------------------------------------------------
// Přesný přepis ux.changes_since (Python) -- test porovnává oba výsledky.
function uxChangesSince(facts, baseline) {
  const items = [];
  for (const f of facts) {
    const fs = f.fs, g = f.g;
    if (g) {
      if (g > baseline && (!fs || fs <= baseline)) items.push({ s: f.s, id: f.id, kind: "gone", at: g });
      continue;
    }
    if (fs && fs > baseline && !f.b) { items.push({ s: f.s, id: f.id, kind: "new", at: fs }); continue; }
    const after = (f.pe || []).filter(e => e[0] > baseline);
    const cur = f.p;
    if (after.length && cur && after[0][1] && after[0][1] !== cur) {
      const old = after[0][1];
      items.push({ s: f.s, id: f.id, kind: cur < old ? "down" : "up", at: after[after.length - 1][0],
                   old, cur, pct: Math.round((cur - old) / old * 1000) / 10 });
    }
  }
  const counts = {};
  ["new", "down", "up", "gone"].forEach(k => { counts[k] = items.filter(i => i.kind === k).length; });
  return { items, counts };
}

function uxLoadSeen() {
  const raw = uxGet(UX_SEEN_KEY);
  if (!raw) return null;
  try {
    const o = JSON.parse(raw);
    if (o && typeof o.data_at === "string" && uxParse(o.data_at) != null) return o;
  } catch (e) {}
  return null;
}

function uxMarkRead() {
  const ok = uxSet(UX_SEEN_KEY, JSON.stringify({ data_at: UX.generated_at, marked_at: new Date().toISOString() }));
  if (!ok) { uxToast("Nepodařilo se uložit — prohlížeč nepovoluje úložiště (soukromé okno?). Nic se nezměnilo.", true); return; }
  uxBaseline = UX.generated_at; uxMarkedAt = Date.now();
  uxComputeNews();
  uxRerender();
  uxRenderNews(`✓ Označeno jako přečtené (${uxHm(uxMarkedAt)}). Další novinky se ukážou po dalším běhu scraperu.`);
}

function uxComputeNews() {
  uxByKey = new Map();
  if (!uxBaseline) return { items: [], counts: { new: 0, down: 0, up: 0, gone: 0 } };
  const res = uxChangesSince(UX.facts, uxBaseline);
  res.items.forEach(i => uxByKey.set(uxKey(i.s, i.id), i));
  return res;
}

function uxPct(p) { return (p > 0 ? "+" : "−") + Math.abs(p).toFixed(1).replace(".", ",") + " %"; }

function uxBadgeHtml(i) {
  if (!i) return "";
  if (i.kind === "new") return `<span class="ux-b new" title="Objevil se od posledního přečtení">NOVÉ</span>`;
  if (i.kind === "gone") return `<span class="ux-b gone" title="Zmizel od posledního přečtení">zmizel</span>`;
  const t = `Od posledního přečtení: ${fmtCzk(i.old)} → ${fmtCzk(i.cur)}`;
  return `<span class="ux-b ${i.kind}" title="${uxEsc(t)}">${i.kind === "down" ? "↓" : "↑"} ${uxPct(i.pct)}</span>`;
}

// Hook z řádků tabulek ve scrape.py: odznak novinky + hvězdička.
function uxRowBadges(s, id) {
  if (!uxReady) return "";
  return uxBadgeHtml(uxByKey.get(uxKey(s, id))) + uxStarHtml(s, id);
}

// Hook z priceMoveBadge: „za 12 d" u zlevnění od prvního zachycení.
function uxDropSuffix(r) {
  const h = r && r.price_history;
  if (!h || !h.length || !UX) return "";
  const d = daysBetween(h[0].at, UX.generated_at);
  return d == null ? "" : ` <span class="ux-days">za ${d} d</span>`;
}

// Záznam pro zobrazení podle sekce a id: {x: data, gone: ISO|null, open: fn}.
function uxResolve(s, id) {
  const sid = String(id);
  if (s === "byt") {
    const live = ALL_BY_ID.get(sid);
    if (live) return { x: live, gone: null, open: () => openModal(live.id) };
    const g = GONE_BY_ID.get(sid);
    if (g) return { x: Object.assign({ price_czk: g.last_price_czk }, g), gone: g.gone_at, open: () => openGone(sid) };
    return null;
  }
  if (s === "garaz") {
    const g = GARAGE_BY_ID.get(sid);
    return g ? { x: g, gone: g.gone_at || null, open: () => openGarage(sid) } : null;
  }
  if (s === "nov") {
    const n = uxNovById.get(sid);
    return n ? { x: n, gone: n.gone_at || null, open: () => uxOpenNov(sid) } : null;
  }
  return null;
}

function uxPriceTxt(x) {
  if (!x) return "—";
  const v = x.transaction_type === "pronajem" ? (x.total_czk ?? x.price_czk) : x.price_czk;
  return fmtCzk(v) + (x.transaction_type === "pronajem" ? "/měs" : "");
}

function uxTitle(x, s) {
  if (!x) return "—";
  if (s === "nov") return `${x.disposition || ""} ${x.street || x.city_part || ""}`.trim() || (x.title || String(x.id));
  return x.title || String(x.id);
}

function uxItemRow(s, id, extra) {
  const r = uxResolve(s, id);
  const x = r ? r.x : null;
  const where = x ? (x.street || x.city_part || x.locality || "") : "";
  return `<button type="button" class="ux-li" data-ux-open="${uxEsc(uxKey(s, id))}">
    <span class="ux-sec">${uxEsc(UX_SEC_LABEL[s] || s)}</span>
    <span class="t">${uxEsc(uxTitle(x, s))}${where ? ` · <span class="hint">${uxEsc(where)}</span>` : ""}</span>
    <span>${extra || ""}</span></button>`;
}

var uxOpenList = "";
function uxRenderNews(note) {
  const el = document.getElementById("uxNews");
  if (!el) return;
  el.hidden = false;
  if (!uxStorageOk && !uxBaseline) {
    el.className = "ux-news quiet";
    el.innerHTML = "Prohlížeč nedovolí ukládat (soukromé okno nebo blokované úložiště) — přehled novinek a oblíbené tu proto nefungují. Zbytek stránky ano.";
    return;
  }
  const res = uxComputeNews();
  const c = res.counts;
  const total = c.new + c.down + c.up + c.gone;
  const since = uxMarkedAt ? uxWhen(uxMarkedAt) : fmtDay(uxBaseline);
  const stale = uxBaseline && uxBaseline < UX.window_start
    ? ` <span class="hint">(přečteno před víc než ${UX.window_days} dny — ukazuju jen změny za posledních ${UX.window_days} dní)</span>` : "";
  // Čeština: 1 nový / 2–4 nové / 5+ nových.
  const pl = (n, one, few, many) => n === 1 ? one : (n >= 2 && n <= 4 ? few : many);
  const btn = (k, n, word) => n
    ? `<button type="button" class="ux-cnt" data-ux-list="${k}" aria-expanded="${uxOpenList === k}">${n} ${word}</button>` : "";
  const parts = [btn("new", c.new, pl(c.new, "nový", "nové", "nových")),
                 btn("down", c.down, pl(c.down, "zlevněný", "zlevněné", "zlevněných")),
                 btn("up", c.up, pl(c.up, "zdražený", "zdražené", "zdražených")),
                 btn("gone", c.gone, pl(c.gone, "zmizel", "zmizely", "zmizelo"))].filter(Boolean);
  el.className = "ux-news" + (total ? "" : " quiet");
  let head;
  if (note) head = uxEsc(note);
  else if (total) head = `<b>Od tvé poslední návštěvy</b> (${uxEsc(since)}): ${parts.join(", ")}${stale}`;
  else head = `Od tvé poslední návštěvy (${uxEsc(since)}) se nic nezměnilo.${stale}`;
  let list = "";
  if (uxOpenList && !note) {
    const rows = res.items.filter(i => i.kind === uxOpenList)
      .sort((a, b) => a.s.localeCompare(b.s) || String(b.at).localeCompare(String(a.at)));
    list = `<div class="ux-list">${rows.slice(0, 200).map(i => uxItemRow(i.s, i.id, uxBadgeHtml(i))).join("")}</div>`
      + (rows.length > 200 ? `<div class="hint">… a dalších ${rows.length - 200}</div>` : "");
  }
  el.innerHTML = `<div>${head}</div>${list}
    <div class="ux-actions">
      ${total && !note ? '<button type="button" class="popup-btn" data-ux-act="read">Označit jako přečtené</button>' : ""}
      <span class="hint" style="margin:0;">Počítá se od chvíle, kdy jsi naposledy klikl na „označit jako přečtené“
        (uloženo jen v tomto prohlížeči). Odznaky NOVÉ / ↓ / ↑ jsou i v tabulkách.</span>
    </div>`;
}

// ---- oblíbené ---------------------------------------------------------------
function uxLoadFavs() {
  const raw = uxGet(UX_FAV_KEY);
  if (!raw) return {};
  try {
    const o = JSON.parse(raw);
    if (o && typeof o === "object" && !Array.isArray(o)) {
      const out = {};
      Object.keys(o).forEach(k => {
        const v = o[k];
        if (v && typeof v === "object" && UX_SEC_LABEL[v.s] && v.id != null) out[k] = v;
      });
      return out;
    }
  } catch (e) {}
  // Poškozená data se nezahazují potichu: syrová kopie zůstane vedle.
  uxFavCorrupt = true;
  uxSet(UX_FAV_KEY + ":poskozene", raw);
  return {};
}
function uxFavMap() { if (!uxFavs) uxFavs = uxLoadFavs(); return uxFavs; }
function uxSaveFavs() { return uxSet(UX_FAV_KEY, JSON.stringify(uxFavMap())); }

function uxStarHtml(s, id) {
  const k = uxKey(s, id), on = !!uxFavMap()[k];
  return `<button type="button" class="ux-star${on ? " on" : ""}" data-ux-fav="${uxEsc(k)}"
    title="${on ? "Odebrat z oblíbených" : "Přidat do oblíbených (jen v tomto prohlížeči)"}"
    aria-pressed="${on}">${on ? "★" : "☆"}</button>`;
}

function uxFavSnapshot(s, id) {
  const r = uxResolve(s, id), x = r ? r.x : {};
  const p = x.price_czk ?? null;
  return { s, id, t: uxTitle(x, s), p, p0: p, tx: x.transaction_type || null,
           st: x.street || x.city_part || "", th: typeof x.thumb === "string" ? x.thumb : "",
           u: typeof x.url === "string" ? x.url : "", at: new Date().toISOString(),
           seen: r && !r.gone ? UX.generated_at : null, ga: r && r.gone ? r.gone : null };
}

function uxToggleFav(k, undoEntry) {
  const favs = uxFavMap();
  const i = k.indexOf(":"), s = k.slice(0, i), id = k.slice(i + 1);
  const had = !!favs[k];
  let removed = null;
  if (had) { removed = favs[k]; delete favs[k]; }
  else {
    const r = uxResolve(s, id);
    favs[k] = undoEntry || uxFavSnapshot(s, r ? r.x.id : id);
  }
  const ok = uxSaveFavs();
  uxRefreshStars(k);
  uxRenderFavs();
  if (!ok) {
    uxToast("Uložení selhalo — prohlížeč nepovoluje úložiště. Změna vydrží jen do obnovení stránky.", true);
  } else if (had) {
    uxToast("Odebráno z oblíbených.", false, "Vrátit", () => { if (!uxFavMap()[k]) uxToggleFav(k, removed); });
  } else {
    uxToast("★ Přidáno do oblíbených — uloženo jen v tomto prohlížeči.");
  }
}

function uxRefreshStars(k) {
  const on = !!uxFavMap()[k];
  document.querySelectorAll("[data-ux-fav]").forEach(b => {
    if (b.getAttribute("data-ux-fav") !== k) return;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
    if (b.classList.contains("popup-btn")) b.textContent = on ? "★ V oblíbených" : "☆ Do oblíbených";
    else { b.textContent = on ? "★" : "☆"; b.title = on ? "Odebrat z oblíbených" : "Přidat do oblíbených (jen v tomto prohlížeči)"; }
  });
}

// Při každém načtení: živým oblíbeným obnoví poslední známou cenu a čas,
// zmizelým zapamatuje datum zmizení -- aby je šlo ukázat, i když vypadnou
// z dat stránky (zmizelé se na stránce drží 30 dní).
function uxSyncFavs() {
  const favs = uxFavMap();
  let changed = false;
  Object.keys(favs).forEach(k => {
    const f = favs[k], r = uxResolve(f.s, f.id);
    if (!r) return;
    if (!r.gone) {
      const p = r.x.price_czk ?? null;
      if (f.p !== p || f.seen !== UX.generated_at) { f.p = p; f.seen = UX.generated_at; changed = true; }
    } else if (f.ga !== r.gone) { f.ga = r.gone; changed = true; }
  });
  if (changed) uxSaveFavs();
}

function uxRenderFavs() {
  const el = document.getElementById("favList");
  if (!el) return;
  const favs = uxFavMap();
  const keys = Object.keys(favs);
  const cnt = document.getElementById("favCount");
  if (cnt) cnt.textContent = keys.length ? `(${keys.length})` : "";
  let warn = "";
  if (uxFavCorrupt) warn = `<div class="modal-note">Uložená data oblíbených v tomto prohlížeči byla poškozená, nešla přečíst.
    Syrová kopie zůstala v localStorage pod klíčem „${uxEsc(UX_FAV_KEY)}:poskozene“; začíná se s prázdným seznamem.</div>`;
  if (!uxStorageOk) warn += `<div class="modal-note">Prohlížeč nedovolí ukládat — oblíbené vydrží jen do obnovení stránky.</div>`;
  if (!keys.length) {
    el.innerHTML = warn + '<div class="hint" style="margin:0;">Zatím nic. Klikni na ☆ u inzerátu v tabulce nebo v detailu.</div>';
    return;
  }
  const rows = keys.map(k => {
    const f = favs[k], r = uxResolve(f.s, f.id);
    const x = r ? r.x : null;
    const gone = r ? r.gone : (f.ga || "unknown");
    const title = x ? uxTitle(x, f.s) : (f.t || String(f.id));
    const thumb = safeImg((x && x.thumb) || f.th);
    let state;
    if (!gone) {
      const move = f.p0 && x && x.price_czk && f.p0 !== x.price_czk
        ? ` · <span class="${x.price_czk < f.p0 ? "pm-down" : "pm-up"} pmove">${x.price_czk < f.p0 ? "↓" : "↑"} od uložení (${fmtCzk(f.p0)})</span>` : "";
      state = `${uxPriceTxt(x)}${move}`;
    } else {
      const lastP = x ? (x.price_czk ?? f.p) : f.p;
      const when = gone === "unknown"
        ? `zmizel (přesné datum neznáme; naposledy viděn ${f.seen ? fmtDay(f.seen) : "?"})`
        : `zmizel dne ${fmtDay(gone)}`;
      state = `<span class="deal-bad">${uxEsc(when)}</span> · poslední cena ${fmtCzk(lastP)}`;
    }
    const open = r ? `data-ux-open="${uxEsc(k)}"` : "";
    const portal = !r && /^https:\/\//.test(f.u || "") ? ` · <a href="${uxEsc(f.u)}" target="_blank" rel="noopener">původní inzerát</a>` : "";
    return { gone: !!gone, html: `<div class="fav-row${gone ? " gone" : ""}">
      ${uxStarHtml(f.s, f.id)}
      <button type="button" class="fav-open" ${open}${r ? "" : " disabled"}>
        <img class="thumb" src="${uxEsc(thumb)}" loading="lazy" alt="">
        <span class="fav-txt"><div class="fav-t">${uxEsc(title)}</div>
          <div class="fav-m"><span class="ux-sec">${uxEsc(UX_SEC_LABEL[f.s] || f.s)}</span>
            ${uxEsc(x ? (x.street || x.city_part || "") : (f.st || ""))} · ${state}</div></span>
      </button>${portal}</div>` };
  });
  rows.sort((a, b) => a.gone - b.gone);
  el.innerHTML = warn + rows.map(r => r.html).join("");
}

// ---- modal: hvězda, odkaz, novinka ----------------------------------------
function uxLink(id) {
  return location.origin + location.pathname + "#byt=" + encodeURIComponent(String(id));
}

// Hook z openModal / openGarage / openGone / openHistoryItem ve scrape.py.
function uxModalOpened(s, id) {
  if (!uxReady || id == null) return;
  const sheet = document.getElementById("modalSheet");
  if (!sheet || sheet.querySelector(".ux-mbar") || !uxResolve(s, id)) return;
  uxOpenKey = uxKey(s, id);
  const on = !!uxFavMap()[uxOpenKey];
  const bar = document.createElement("div");
  bar.className = "ux-mbar";
  bar.innerHTML = `<button type="button" class="popup-btn ux-ghost${on ? " on" : ""}" data-ux-fav="${uxEsc(uxOpenKey)}"
      aria-pressed="${on}">${on ? "★ V oblíbených" : "☆ Do oblíbených"}</button>
    <button type="button" class="popup-btn ux-ghost" data-ux-copy="${uxEsc(String(id))}">🔗 Kopírovat odkaz</button>
    ${uxBadgeHtml(uxByKey.get(uxOpenKey))}
    <span class="ux-mstatus hint" role="status" aria-live="polite" style="margin:0;"></span>`;
  const close = sheet.querySelector("#modalClose");
  if (close && close.nextSibling) sheet.insertBefore(bar, close.nextSibling); else sheet.prepend(bar);
  try { history.replaceState(null, "", "#byt=" + encodeURIComponent(String(id))); } catch (e) {}
}

function uxModalClosed() {
  uxOpenKey = null;
  if (/^#byt=/.test(location.hash)) {
    try { history.replaceState(null, "", location.pathname + location.search); } catch (e) {}
  }
}

async function uxCopy(id, btn) {
  const url = uxLink(id);
  const status = btn.parentElement.querySelector(".ux-mstatus");
  const say = t => { if (status) status.textContent = t; };
  try {
    if (!navigator.clipboard || !window.isSecureContext) throw new Error("clipboard");
    await navigator.clipboard.writeText(url);
    say("✓ Odkaz zkopírován.");
    uxToast("🔗 Odkaz zkopírován do schránky.");
    return;
  } catch (e) {}
  // Záloha: pole s odkazem, označené -- stačí Ctrl/⌘+C.
  let inp = btn.parentElement.querySelector(".ux-copy-fallback");
  if (!inp) {
    inp = document.createElement("input");
    inp.className = "ux-copy-fallback";
    inp.readOnly = true;
    btn.parentElement.appendChild(inp);
  }
  inp.value = url;
  inp.focus(); inp.select();
  let copied = false;
  try { copied = document.execCommand && document.execCommand("copy"); } catch (e) {}
  say(copied ? "✓ Odkaz zkopírován." : "Schránka není dostupná — odkaz je označený v poli níže, zkopíruj ho (Ctrl/⌘+C).");
}

// Novostavby nemají vlastní detail -- stačí malý: cena, typ, historie, odkazy.
function uxOpenNov(id) {
  // Plný detail z karty novostaveb (fotky, popis, opravy), když je na stránce;
  // tenhle zjednodušený jen jako záloha.
  if (typeof window.openNov === "function" && typeof window.novItem === "function"
      && window.novItem(id)) {
    window.openNov(id);
    uxModalOpened("nov", String(id));
    return;
  }
  const n = uxNovById.get(String(id));
  if (!n) return;
  const tx = n.transaction_type === "pronajem" ? "pronájem" : "prodej";
  const unit = n.transaction_type === "pronajem" ? "/měs" : "";
  const h = (n.price_history || []).filter(p => p && p.price_czk);
  const hist = h.length > 1 ? `<div class="ph-box"><div class="ph-title">📉 Historie ceny</div>
      <table class="ph-table"><tbody>${h.map(p => `<tr><td>${uxEsc(fmtDay(p.at))}</td><td>${fmtCzk(p.price_czk)}</td></tr>`).join("")}</tbody></table></div>` : "";
  const url = /^https:\/\//.test(n.url || "") ? n.url : "";
  document.getElementById("modalSheet").innerHTML = `
    <button id="modalClose" onclick="closeModal()">&times;</button>
    <h2>🏗️ ${uxEsc(n.disposition || "")} ${uxEsc(n.street || n.city_part || "")}</h2>
    ${n.gone_at ? `<div class="modal-note">❌ Už není v nabídce — zmizel ${fmtDay(n.gone_at)}.</div>` : ""}
    ${n.thumb ? `<div class="modal-gallery"><img src="${uxEsc(safeImg(n.thumb))}" loading="lazy" alt=""></div>` : ""}
    <div class="modal-grid">
      <div><b>Cena</b>${n.price_czk ? fmtCzk(n.price_czk) + unit : "na dotaz"}</div>
      <div><b>Kč/m²</b>${fmtCzk(n.price_czk_per_sqm)}</div>
      <div><b>m²</b>${uxEsc(n.floor_area_sqm ?? "—")}</div>
      <div><b>Typ</b>${uxEsc(n.kind_reason || n.kind || "—")} · ${tx}</div>
      <div style="grid-column:1/-1;"><b>Kde</b>${uxEsc([n.street, n.city_part].filter(Boolean).join(", ") || n.locality || "—")}
        <div class="hint">${mapLinksHtml(n.lat, n.lon)}</div></div>
      <div><b>V nabídce</b>${fmtDay(n.first_seen)} → ${n.gone_at ? fmtDay(n.gone_at) : "dosud"}</div>
      <div><b>Název</b>${uxEsc(n.title || "—")}</div>
    </div>
    ${hist}
    <p class="hint">Novostavba 4+kk / 5+kk — podrobnosti v kartě 🏗️ Novostavby.</p>
    ${url ? `<a class="modal-link" href="${uxEsc(url)}" target="_blank" rel="noopener">Otevřít na Sreality →</a>` : ""}`;
  document.getElementById("modalOverlay").classList.add("open");
  uxModalOpened("nov", n.id);
}

// Otevře inzerát podle id bez ohledu na sekci (byt -> garáž -> novostavba -> zmizelý).
function uxOpenById(id) {
  const sid = String(id);
  for (const s of ["byt", "garaz", "nov"]) {
    const r = uxResolve(s, sid);
    if (r) { r.open(); return true; }
  }
  return false;
}

function uxOpenHash() {
  const m = /^#byt=(.+)$/.exec(location.hash || "");
  if (!m) return;
  let id = m[1];
  try { id = decodeURIComponent(id); } catch (e) {}
  if (!uxOpenById(id)) {
    uxToast(`Inzerát ${id} na stránce není — zmizel před víc než 30 dny, nebo ho nesledujeme.`, true);
  }
}

// ---- hledání ----------------------------------------------------------------
var uxIndex = null;
function uxFold(s) {
  return String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
}
function uxNormUrl(u) {
  return String(u || "").trim().replace(/[?#].*$/, "").replace(/\/+$/, "").replace(/^https?:\/\/(www\.)?/, "").toLowerCase();
}
function uxBuildIndex() {
  const out = [], seen = new Set();
  const push = (s, x, gone) => {
    const k = uxKey(s, x.id);
    if (seen.has(k)) return;
    seen.add(k);
    const urls = [x.url].concat((x.also_on || []).map(a => a && a.url)).filter(Boolean).map(uxNormUrl);
    const ids = [String(x.id)].concat((x.also_on || []).map(a => a && a.id != null ? String(a.id) : "")).filter(Boolean);
    out.push({ s, id: x.id, k, gone: !!gone, ids, urls,
               hay: uxFold([x.title, x.street, x.city_part, x.locality, x.disposition, (x.address || {}).text,
                            x.garage_kind, String(x.id)].filter(Boolean).join(" ")) });
  };
  ALL.forEach(x => push("byt", x, false));
  GARAGES.forEach(g => push("garaz", g, !!g.gone_at));
  uxNovById.forEach(n => push("nov", n, !!n.gone_at));
  GONE.forEach(g => push("byt", g, true));
  return out;
}

// Id z odkazu na portál: Sreality …/<číslo>, Bezrealitky /nemovitosti-byty-domy/<číslo>-…,
// iDNES /detail/…/<24 hex>/. Vrací [id, portál] nebo null.
function uxIdFromUrl(q) {
  let u;
  try { u = new URL(q); } catch (e) { return null; }
  const host = u.hostname.replace(/^www\./, ""), p = u.pathname;
  let m;
  if (host.endsWith("sreality.cz") && (m = /\/(\d{3,20})\/?$/.exec(p))) return [m[1], "sreality"];
  if (host.endsWith("bezrealitky.cz") && (m = /\/nemovitosti-byty-domy\/(\d+)/.exec(p))) return ["bez-" + m[1], "bezrealitky"];
  if (host.endsWith("idnes.cz") && (m = /\/([0-9a-f]{24})\/?$/i.exec(p))) return ["idnes-" + m[1].toLowerCase(), "idnes"];
  return ["", host];
}

function uxSearch(q) {
  if (!uxIndex) uxIndex = uxBuildIndex();
  q = String(q || "").trim();
  if (!q) return { hits: [], url: null };
  if (/^https?:\/\//i.test(q)) {
    const parsed = uxIdFromUrl(q);
    const nu = uxNormUrl(q);
    const id = parsed ? parsed[0] : "";
    const hits = uxIndex.filter(e => e.urls.includes(nu) || (id && e.ids.includes(id)));
    return { hits, url: { raw: q, id, portal: parsed ? parsed[1] : "" } };
  }
  const fq = uxFold(q);
  if (/^(\d{4,20}|bez-\d+|idnes-[0-9a-f]+)$/i.test(q)) {
    const exact = uxIndex.filter(e => e.ids.includes(fq));
    if (exact.length) return { hits: exact, url: null };
  }
  const toks = fq.split(/\s+/).filter(Boolean);
  const hits = uxIndex.filter(e => toks.every(t => e.hay.includes(t)));
  hits.sort((a, b) => a.gone - b.gone);
  return { hits, url: null };
}

function uxRenderSearch() {
  const inp = document.getElementById("uxQ"), box = document.getElementById("uxQRes");
  if (!inp || !box) return;
  const q = inp.value;
  if (!q.trim()) { box.hidden = true; box.innerHTML = ""; return; }
  const { hits, url } = uxSearch(q);
  uxQSel = 0;
  let html = "";
  if (hits.length) {
    html = hits.slice(0, 12).map((e, i) => {
      const r = uxResolve(e.s, e.id), x = r ? r.x : {};
      const state = r && r.gone ? `<span class="deal-bad">zmizel ${fmtDay(r.gone)}</span>` : uxPriceTxt(x);
      return `<button type="button" class="ux-qi${i === 0 ? " sel" : ""}" role="option" data-ux-open="${uxEsc(e.k)}">
        <img src="${uxEsc(safeImg(x.thumb))}" alt="" loading="lazy">
        <span class="t">${uxEsc(uxTitle(x, e.s))}<div class="m"><span class="ux-sec">${uxEsc(UX_SEC_LABEL[e.s])}</span>
          ${uxEsc(x.street || x.city_part || x.locality || "")} · ${state}</div></span></button>`;
    }).join("") + (hits.length > 12 ? `<div class="ux-qmsg hint">… a dalších ${hits.length - 12} — upřesni hledání.</div>` : "")
      + `<div class="ux-qmsg hint">Enter otevře první, šipky vybírají.</div>`;
  } else if (url) {
    const sreality = url.portal === "sreality" && url.id;
    html = `<div class="ux-qmsg">Tenhle inzerát nesledujeme — je mimo oblast nebo dispozice${url.id ? "" : " (odkaz nevypadá jako inzerát ze Sreality, Bezrealitek ani iDNES)"}, nebo zmizel před víc než 30 dny.
      ${sreality ? `<div style="margin-top:6px;"><button type="button" class="popup-btn" data-ux-add="1">➕ Přidat ke sledovaným</button>
        <span class="hint">spustí GitHub Action (potřebuje token), výsledek za ~5–15 min</span></div>`
        : (url.id ? '<div class="hint">Ke sledovaným jde přidat jen odkaz ze Sreality.</div>' : "")}</div>`;
  } else {
    html = `<div class="ux-qmsg">Nic nenalezeno pro „${uxEsc(q)}“.</div>`;
  }
  box.innerHTML = html;
  box.hidden = false;
}

function uxSearchKey(ev) {
  const box = document.getElementById("uxQRes");
  const items = box ? [...box.querySelectorAll(".ux-qi")] : [];
  if (ev.key === "Escape") { box.hidden = true; return; }
  if (!items.length) return;
  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    ev.preventDefault();
    uxQSel = (uxQSel + (ev.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items.forEach((b, i) => b.classList.toggle("sel", i === uxQSel));
    items[uxQSel].scrollIntoView({ block: "nearest" });
  } else if (ev.key === "Enter") {
    ev.preventDefault();
    uxOpenKeyed(items[uxQSel].getAttribute("data-ux-open"));
    box.hidden = true;
  }
}

function uxOpenKeyed(k) {
  if (!k) return;
  const i = k.indexOf(":");
  const r = uxResolve(k.slice(0, i), k.slice(i + 1));
  if (r) r.open(); else uxToast("Tenhle inzerát už na stránce není.", true);
}

// ---- toast --------------------------------------------------------------------
var uxToastTimer = null, uxToastAction = null;
function uxToast(msg, isErr, actionLabel, action) {
  const el = document.getElementById("uxToast");
  if (!el) return;
  uxToastAction = action || null;
  el.innerHTML = `<span>${uxEsc(msg)}</span>${actionLabel ? `<button type="button" data-ux-toast="1">${uxEsc(actionLabel)}</button>` : ""}`;
  el.className = "ux-toast" + (isErr ? " err" : "");
  el.hidden = false;
  clearTimeout(uxToastTimer);
  uxToastTimer = setTimeout(() => { el.hidden = true; uxToastAction = null; }, isErr ? 8000 : 4000);
}

// ---- zlevnění: řazení ---------------------------------------------------------
function uxPrepDrops() {
  DATA.forEach(r => {
    const m = priceMove(r);
    r.ux_drop = m && m.diff < 0 && -m.pct <= UX.drop_max_pct ? -m.pct : 0;
  });
}

function uxRerender() {
  try { render(); } catch (e) {}
  try { renderPodHarfou(); } catch (e) {}
  try { renderGarages(); } catch (e) {}
}

// ---- jeden posluchač pro všechno (capture: hvězda nesmí otevřít řádek) --------
document.addEventListener("click", ev => {
  const t = ev.target;
  if (!t || !t.closest) return;
  const fav = t.closest("[data-ux-fav]");
  if (fav) { ev.preventDefault(); ev.stopPropagation(); uxToggleFav(fav.getAttribute("data-ux-fav")); return; }
  const copy = t.closest("[data-ux-copy]");
  if (copy) { ev.preventDefault(); ev.stopPropagation(); uxCopy(copy.getAttribute("data-ux-copy"), copy); return; }
  const open = t.closest("[data-ux-open]");
  if (open) {
    ev.preventDefault(); ev.stopPropagation();
    const box = document.getElementById("uxQRes");
    if (box && box.contains(open)) box.hidden = true;
    uxOpenKeyed(open.getAttribute("data-ux-open"));
    return;
  }
  const lst = t.closest("[data-ux-list]");
  if (lst) { const k = lst.getAttribute("data-ux-list"); uxOpenList = uxOpenList === k ? "" : k; uxRenderNews(); return; }
  const act = t.closest("[data-ux-act]");
  if (act && act.getAttribute("data-ux-act") === "read") { uxMarkRead(); return; }
  const tb = t.closest("[data-ux-toast]");
  if (tb) { const a = uxToastAction; document.getElementById("uxToast").hidden = true; if (a) a(); return; }
  const add = t.closest("[data-ux-add]");
  if (add) {
    const q = document.getElementById("uxQ").value.trim();
    const inp = document.getElementById("addUrlInput");
    if (inp) inp.value = q;
    document.getElementById("uxQRes").hidden = true;
    if (typeof ribJump === "function") ribJump("manageCard");
    manageTracked({ add_url: q });
    uxToast("Odkaz vložen do „Sledované inzeráty“ — stav akce je u tlačítka ➕ Sledovat.");
    return;
  }
  // Klik mimo hledání ho zavře.
  const box = document.getElementById("uxQRes");
  if (box && !box.hidden && !t.closest(".ux-search")) box.hidden = true;
}, true);

function uxInit() {
  (UX.nov || []).forEach(n => uxNovById.set(String(n.id), n));
  const seen = uxLoadSeen();
  if (seen) {
    uxBaseline = seen.data_at;
    uxMarkedAt = uxParse(seen.marked_at);
  }
  uxReady = true;
  uxSyncFavs();
  uxPrepDrops();
  uxRenderFresh();
  setInterval(uxRenderFresh, 60000);
  if (!seen) {
    // První návštěva v tomhle prohlížeči: od teď se počítá.
    const ok = uxSet(UX_SEEN_KEY, JSON.stringify({ data_at: UX.generated_at, marked_at: new Date().toISOString() }));
    if (ok) { uxBaseline = UX.generated_at; uxMarkedAt = Date.now(); }
    uxRenderNews(ok ? "Tady poprvé (v tomhle prohlížeči) — od teď si pamatuju, co jsi viděl. Po dalším běhu scraperu tu uvidíš, co je nového."
                    : null);
  } else {
    uxRenderNews();
  }
  uxRenderFavs();
  uxRerender();

  const q = document.getElementById("uxQ");
  if (q) {
    q.addEventListener("input", uxRenderSearch);
    q.addEventListener("keydown", uxSearchKey);
    q.addEventListener("focus", () => { if (q.value.trim()) uxRenderSearch(); });
  }
  const sortSel = document.getElementById("uxSort");
  if (sortSel) {
    sortSel.addEventListener("change", () => {
      if (sortSel.value === "drop") { sortKey = "ux_drop"; sortDir = -1; }
      else { sortKey = "price_czk_per_sqm"; sortDir = 1; }
      render();
    });
    document.querySelectorAll("#tbl th[data-k]").forEach(th => th.addEventListener("click", () => { sortSel.value = ""; }));
  }
  window.addEventListener("hashchange", uxOpenHash);
  uxOpenHash();
}
uxInit();
"""


# --- Telegram při selhání workflow ------------------------------------------ #
def failure_alert_text(env):
    run_url = env.get("RUN_URL") or ACTIONS_RUNS_URL
    status = env.get("JOB_STATUS") or "failure"
    event = env.get("RUN_EVENT") or "?"
    what = "byl zrušen (timeout nebo ručně)" if status == "cancelled" else "selhal"
    return (f"<b>⚠️ Sreality tracker: běh {html.escape(what)}</b>\n"
            f"Spuštění: {html.escape(event)}. Dashboard zůstává na datech z předchozího úspěšného běhu.\n"
            f'<a href="{html.escape(run_url, quote=True)}">Otevřít běh v Actions</a>')


def _send_telegram_stdlib(text):
    """Záloha, když selhal už krok instalace a `requests` (tedy notify) chybí."""
    import urllib.parse
    import urllib.request
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30) as r:
        return r.status == 200


def send_failure_alert(env=None):
    """Nikdy nevyhodí výjimku a vždy vrátí 0: běh je už červený, alert je
    doplněk. Bez secrets jen varování v logu."""
    env = os.environ if env is None else env
    if not (env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")):
        print("::warning::Telegram secrets chybí — alert o selhání běhu se neposlal.", file=sys.stderr)
        return 0
    text = failure_alert_text(env)
    try:
        try:
            import notify
            sent = notify.send_telegram(text)
        except ImportError:
            sent = _send_telegram_stdlib(text)
        print("Alert o selhání běhu odeslán." if sent else "::warning::Alert o selhání se neodeslal.",
              file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 -- deliberate: never fail the step
        print(f"::warning::Alert o selhání běhu se neodeslal: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["notify-failure"]:
        sys.exit(send_failure_alert())
    print("Použití: python3 ux.py notify-failure", file=sys.stderr)
    sys.exit(2)
