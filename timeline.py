"""Vývoj cen a doba na trhu: týdenní mediány, přežití inzerátů a zlevňování.

Radim (27. 9. 2026): „Chci novou sekci, kde to bude graficky v čase
znázorněné, jak se ty ceny posouvají a jak dlouho to trvá, jak dlouho
nemovitost je listovaná. Filtry podle lokality, typu tzn. prodej a pronájem,
metrové rozpětí a dispozice."

Rozdělení práce (stejně jako ribbon.py a novostavby.py):
  * Python připraví KOMPAKTNÍ ŘÁDKY -- jeden řádek = jeden inzerát, jen čísla
    a indexy do slovníků. Tady se rozhoduje o všem, co je úsudek: co je zombie,
    co je ověřené zmizení, kdy inzerát doopravdy začal, co je chyba v ceně.
  * JS jen filtruje a agreguje (medián, IQR, Kaplan-Meier, počty zlevnění).
    Stejné agregace jsou tu i v Pythonu (`weekly`, `survival`, ...) -- testy
    je hlídají a ověření porovnává čísla ze stránky s nimi.

Poctivost vůči datům (proč to není jen „medián dní do zmizení"):
  * Zombie (`gone_stale`, uklizené 26. 9.) se vynechávají celé: jejich
    `gone_at` je den úklidu a `last_seen` den, kdy je starý kód naposled
    vrátil do tabulky -- ani začátek, ani konec o trhu nic neříká.
  * Zmizení se počítá jen ověřené (404, pool.update_from_snapshot) a jen od
    chvíle, kdy se ověřovalo (TRACK_FROM). Inzerát znovu vložený pod novým
    id (gone_archive `relisted_as`) z trhu neodešel -- jeho doba pokračuje
    v nástupci.
  * Živé inzeráty mají dobu na trhu NEZNÁMOU (cenzurovanou): nemíchají se do
    mediánu „dní do zmizení", ale do křivky přežití vstupují jako „ještě na
    trhu po N dnech" (Kaplan-Meier se zpožděným vstupem). Inzerát, který
    přestal chodit ve výsledcích a nikdo neověřil, že zmizel („ztracený"),
    se cenzuruje v den posledního výskytu.
  * Začátek: datum vložení podle Sreality (`since`), když ho máme -- jinak náš
    první výskyt. Když náš první výskyt spadá do prvních 48 h sledování dané
    oblasti/portálu (nebo po rozšíření sběru), inzerát tam mohl viset měsíce:
    začátek je neznámý a do doby na trhu nejde vůbec.

Veřejná stránka: řádky nesou jen oblast, čtvrť, typ, dispozici, plochu, data
a cenovou cestu. Žádné id, URL, makléř ani text z inzerátu kromě názvu čtvrti
(ten jde v JS přes textContent/escapeHtml).
"""
import html
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent

# Dny se na stránku posílají jako celé číslo od tohoto data -- kratší než ISO.
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
# Pod tolik inzerátů se bod nekreslí jako hodnota (šedý prázdný kroužek).
MIN_N = 5

AREAS = [
    ("vysocany", "Vysočany"),
    ("jinonice", "Jinonice"),
    ("ukrize", "Okruh U Kříže (novostavby 4+kk/5+kk)"),
]
AREA_IDX = {k: i for i, (k, _l) in enumerate(AREAS)}
DISPS = ["1+kk", "1+1", "2+kk", "2+1", "3+kk", "3+1", "4+kk", "5+kk"]
DISP_IDX = {d: i for i, d in enumerate(DISPS)}
TXS = ["prodej", "pronajem"]

# Odkdy je zmizení v oblasti ověřené (404) a sada inzerátů stabilní:
#  * Vysočany: 23. 8. večer se sběr rozšířil na Karlín/Hrdlořezy a zároveň
#    začalo ověřování zmizení (verify_removals). „Zmizelé" v poolu před tím
#    jsou jen „chybí ve výsledcích" z přehraného archivu. Od pondělí 24. 8.
#  * Jinonice: pool.state["area_since"]; okruh U Kříže: novostavby_meta.
TRACK_FROM_DEFAULT = {"vysocany": "2026-08-24T00:00:00Z"}
# Záznamy pool.py z doby před oblastmi nemají `area` -- všechny z Vysočan.
HOME_AREA = "vysocany"
BASELINE_HOURS = 48

# Cena mimo tyhle meze je chyba v datech, ne trh: v historii je třeba
# „18 000 → 8 500 000" (pronájem přehozený na prodej, viz ribbon.DROP_MAX_PCT).
PRICE_SANE = {"pronajem": (2_000, 300_000), "prodej": (300_000, 200_000_000)}
# Zlevnění nad 50 % je tatáž chyba; pod 0,5 % je zaokrouhlení.
CUT_MIN_PCT = 0.5
CUT_MAX_PCT = 50.0

STATUS = {"live": 0, "gone": 1, "relisted": 2, "lost": 3}

# Pořadí polí v řádku -- JS má stejné konstanty (R_*).
FIELDS = ("area", "ward", "tx", "disp", "sqm", "start", "start_known", "obs_first",
          "end", "status", "prices")


# ---------------------------------------------------------------- časy

def parse_ts(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    for fmt, part in (("%Y-%m-%dT%H:%M:%SZ", text), ("%Y-%m-%dT%H:%M:%S%z", text),
                      ("%Y-%m-%d", text[:10])):
        try:
            dt = datetime.strptime(part, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def day_of(value):
    dt = parse_ts(value)
    return None if dt is None else (dt - EPOCH).days


def week_start(day):
    """Pondělí týdne, ve kterém den leží (EPOCH 1. 1. 2026 je čtvrtek)."""
    wd = (EPOCH + timedelta(days=day)).weekday()
    return day - wd


def day_label(day):
    d = EPOCH + timedelta(days=day)
    return f"{d.day}. {d.month}."


# ---------------------------------------------------------------- statistika

def quantile(sorted_vals, q):
    """Lineární interpolace (typ 7, jako numpy/R výchozí). JS má tutéž."""
    n = len(sorted_vals)
    if n == 0:
        return None
    pos = (n - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


# ---------------------------------------------------------------- řádky

def _area_key(rec):
    return rec.get("area") or HOME_AREA


def track_from(snapshot=None, state=None):
    """{oblast: den, odkdy se zmizení v ní ověřuje a sada je stabilní}."""
    out = {k: day_of(v) for k, v in TRACK_FROM_DEFAULT.items()}
    for area, at in ((state or {}).get("area_since") or {}).items():
        if area in AREA_IDX and area not in out and day_of(at) is not None:
            out[area] = day_of(at)
    base = ((snapshot or {}).get("novostavby_meta") or {}).get("baseline_at")
    if day_of(base) is not None:
        out["ukrize"] = day_of(base)
    return out


def baselines(records, state=None):
    """{(oblast, portál): [okamžiky]} -- chvíle, kdy do sledování naráz
    vtekly inzeráty, které na trhu mohly viset dávno předtím: první výskyt
    dané kombinace oblast+portál a každá změna konfigurace sběru (jen
    Vysočany, pool.note_config)."""
    first = {}
    for rec in records:
        at = parse_ts(rec.get("first_seen"))
        if at is None:
            continue
        key = (_area_key(rec), rec.get("source") or "sreality")
        if key not in first or at < first[key]:
            first[key] = at
    changes = [parse_ts(c.get("at")) for c in ((state or {}).get("config_changes") or [])]
    changes = [c for c in changes if c is not None]
    out = {}
    for key, at in first.items():
        out[key] = [at] + (changes if key[0] == HOME_AREA else [])
    return out


def _in_baseline(first_seen, marks):
    at = parse_ts(first_seen)
    if at is None:
        return True
    return any(m <= at < m + timedelta(hours=BASELINE_HOURS) for m in marks)


def relisted_ids(archive, records):
    """Id, která zmizela, ale vrátila se pod novým id -- z archivu
    (`relisted_as`) i z poolu (`relist_of` u nástupce)."""
    out = {str(k) for k, v in (archive or {}).items() if isinstance(v, dict) and v.get("relisted_as")}
    for rec in records:
        link = rec.get("relist_of")
        if isinstance(link, dict) and link.get("id") is not None:
            out.add(str(link["id"]))
    return out


def status_of(rec, live_ids, relisted, track_day):
    """live / gone / relisted / lost, nebo None = vynechat (zombie)."""
    if rec.get("gone_stale"):
        return None
    rid = str(rec.get("id"))
    gone = day_of(rec.get("gone_at"))
    if gone is not None:
        if rid in relisted:
            return "relisted"
        # Zmizení z doby před ověřováním bylo jen „chybí ve výsledcích".
        if track_day is not None and gone < track_day:
            return "lost"
        return "gone"
    return "live" if rid in live_ids else "lost"


def _chain_start(rec, by_id, known_fn, depth=0):
    """(začátek, známý?, první náš výskyt) přes řetěz znovuvložení: nástupce
    dědí začátek předchůdce, protože byt z trhu neodešel."""
    since = day_of(rec.get("since"))
    first = day_of(rec.get("first_seen"))
    cands = [d for d in (since, first) if d is not None]
    start = min(cands) if cands else None
    known = known_fn(rec)
    link = rec.get("relist_of")
    if isinstance(link, dict) and depth < 10:
        prev = by_id.get(str(link.get("id")))
        if prev is not None:
            p_start, p_known, p_first = _chain_start(prev, by_id, known_fn, depth + 1)
        else:
            # Předchůdce v poolu není (jiný portál, starší než pool): jeho
            # začátek známe jen z odkazu a nevíme, jestli sám nebyl baseline.
            p_start = day_of(link.get("listed_since") or link.get("first_seen"))
            p_known, p_first = False, p_start
        if p_start is not None and (start is None or p_start < start):
            start, known = p_start, p_known
        if p_first is not None and (first is None or p_first < first):
            first = p_first
    return start, known, first


def _prices(history, tx):
    lo, hi = PRICE_SANE[tx]
    out, last = [], None
    for h in history or []:
        d = day_of(h.get("at"))
        p = h.get("price_czk")
        if d is None or not isinstance(p, (int, float)) or not (lo <= p <= hi):
            continue
        v = int(round(p / 100))  # stovky Kč: kratší a pro medián bez újmy
        if out and out[-2] == d:
            out[-1] = v  # víc změn za den -> platí poslední
            continue
        if v == last:
            continue
        out += [d, v]
        last = v
    return out


def build_rows(records, live_ids, archive=None, state=None, snapshot=None, now=None):
    """Kompaktní řádky + počty vynechaných. `records` = záznamy poolu
    (a novostavby s `area="ukrize"`)."""
    records = list(records)
    by_id = {str(r.get("id")): r for r in records}
    relisted = relisted_ids(archive, records)
    tracks = track_from(snapshot, state)
    marks = baselines(records, state)
    wards, ward_idx = [], {}
    rows = []

    def known_fn(r):
        # Datum vložení od portálu = známý začátek. Bez něj jen tehdy, když
        # jsme inzerát viděli přijít -- ne když naráz vtekl s prvním během
        # sledování oblasti/portálu nebo po rozšíření sběru.
        if day_of(r.get("since")) is not None:
            return True
        key = (_area_key(r), r.get("source") or "sreality")
        return not _in_baseline(r.get("first_seen"), marks.get(key, []))

    skipped = {"zombie": 0, "no_sqm": 0, "no_price": 0, "other": 0, "excluded": 0}
    for rec in records:
        area = _area_key(rec)
        tx = rec.get("transaction_type")
        disp = rec.get("disposition")
        if area not in AREA_IDX or tx not in TXS or disp not in DISP_IDX:
            skipped["other"] += 1
            continue
        # Okolní čtvrti (#26): od 7. 10. je Sreality vrací navíc, v grafu by
        # se tvářily jako skok nabídky o ~400 bytů.
        if rec.get("exclude_from_stats") or rec.get("scope") == "fringe":
            skipped["excluded"] += 1
            continue
        st = status_of(rec, live_ids, relisted, tracks.get(area))
        if st is None:
            skipped["zombie"] += 1
            continue
        sqm = rec.get("floor_area_sqm")
        if not isinstance(sqm, (int, float)) or sqm <= 0:
            skipped["no_sqm"] += 1
            continue
        prices = _prices(rec.get("price_history"), tx)
        if not prices:
            skipped["no_price"] += 1
            continue
        start, known, obs_first = _chain_start(rec, by_id, known_fn)
        last = day_of(rec.get("last_seen"))
        if st in ("gone", "relisted"):
            end = day_of(rec.get("gone_at"))
        elif st == "live":
            end = day_of(now) if now else last
        else:
            end = last
        if start is None or end is None or obs_first is None:
            skipped["other"] += 1
            continue
        ward = rec.get("city_part") or "neuvedeno"
        if ward not in ward_idx:
            ward_idx[ward] = len(wards)
            wards.append(ward)
        rows.append([
            AREA_IDX[area], ward_idx[ward], TXS.index(tx), DISP_IDX[disp],
            int(round(sqm)), start, 1 if known else 0, obs_first, max(end, prices[0]),
            STATUS[st], prices,
        ])
    return rows, wards, skipped


def novostavby_records(novostavby):
    """Novostavby 4+kk/5+kk ze snapshotu jako záznamy „oblasti" ukrize."""
    out = []
    for n in novostavby or []:
        if n.get("out_of_scope"):
            continue
        rec = dict(n)
        rec["area"] = "ukrize"
        rec["source"] = "sreality"
        rec["id"] = f"nov-{n.get('id')}"
        rec.setdefault("price_history", [{"at": n.get("first_seen"), "price_czk": n.get("price_czk")}])
        out.append(rec)
    return out


def build_payload(pool_records, snapshot, archive=None, state=None):
    now = snapshot.get("generated_at")
    live = {str(c.get("id")) for c in snapshot.get("comparables") or []}
    nov = novostavby_records(snapshot.get("novostavby"))
    live |= {r["id"] for r in nov if not r.get("gone_at")}
    rows, wards, skipped = build_rows(
        list(pool_records) + nov, live, archive, state, snapshot, now)
    tracks = track_from(snapshot, state)
    return {
        "v": 1,
        "fields": list(FIELDS),
        "areas": [[k, l] for k, l in AREAS],
        "disps": DISPS,
        "txs": TXS,
        "wards": wards,
        "epoch": EPOCH.strftime("%Y-%m-%d"),
        "now": day_of(now),
        "track_from": [tracks.get(k) for k, _l in AREAS],
        "min_n": MIN_N,
        "cut": [CUT_MIN_PCT, CUT_MAX_PCT],
        "skipped": skipped,
        "rows": rows,
    }


def payload_from_disk(snapshot):
    """Pro render_dashboard: načte pool, archiv a stav sám, ať je háček ve
    scrape.py na jeden řádek. Chyba tady nesmí shodit celý dashboard --
    vrátí None a karta se nevykreslí."""
    try:
        import pool as poolmod
        records = poolmod.records_of(poolmod.load_pool())
        state = poolmod.load_state()
        try:
            archive = json.loads((ROOT / "gone_archive.json").read_text())
        except (OSError, json.JSONDecodeError):
            archive = {}
        return build_payload(records, snapshot, archive, state)
    except Exception as exc:  # noqa: BLE001 -- karta je doplněk, ne jádro
        import sys
        print(f"::warning::timeline payload failed: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- agregace
# Referenční implementace toho, co počítá JS. Filtr: dict s klíči area (index),
# tx (index), wards (set indexů nebo None), disps (set nebo None),
# sqm_min / sqm_max (nebo None).

def matches(row, flt):
    if row[0] != flt["area"] or row[2] != flt["tx"]:
        return False
    if flt.get("wards") and row[1] not in flt["wards"]:
        return False
    if flt.get("disps") and row[3] not in flt["disps"]:
        return False
    if flt.get("sqm_min") is not None and row[4] < flt["sqm_min"]:
        return False
    if flt.get("sqm_max") is not None and row[4] > flt["sqm_max"]:
        return False
    return True


def price_on(row, day):
    """Cena (stovky Kč) platná v daný den; před první známou cenou ta první."""
    p = row[10]
    val = p[1]
    for i in range(0, len(p), 2):
        if p[i] <= day:
            val = p[i + 1]
        else:
            break
    return val


def weeks_for(payload, area):
    first = payload["track_from"][area]
    if first is None:
        return []
    ws, w = [], week_start(first)
    while w <= payload["now"]:
        ws.append(w)
        w += 7
    return ws


def weekly(rows, weeks, now):
    """Po týdnech: inzeráty na trhu v tom týdnu (náš výskyt protíná týden),
    cena platná na konci týdne (nebo v poslední den výskytu)."""
    out = []
    for w in weeks:
        sun = min(w + 6, now)
        per_sqm, price = [], []
        for r in rows:
            if r[10][0] <= sun and r[8] >= w:
                v = price_on(r, min(sun, r[8]))
                price.append(v * 100)
                per_sqm.append(v * 100 / r[4])
        per_sqm.sort()
        price.sort()
        n = len(price)
        out.append({
            "week": w, "n": n,
            "sqm_med": quantile(per_sqm, 0.5), "sqm_p25": quantile(per_sqm, 0.25),
            "sqm_p75": quantile(per_sqm, 0.75),
            "price_med": quantile(price, 0.5), "price_p25": quantile(price, 0.25),
            "price_p75": quantile(price, 0.75),
        })
    return out


def exits(rows, track_day):
    """Ověřená zmizení se známým začátkem: [(den zmizení, dní na trhu)]."""
    return [(r[8], r[8] - r[5]) for r in rows
            if r[9] == STATUS["gone"] and r[6] == 1 and track_day is not None and r[8] >= track_day]


def exits_by_week(rows, weeks, track_day):
    ex = exits(rows, track_day)
    out = []
    for w in weeks:
        days = sorted(d for g, d in ex if w <= g <= w + 6)
        out.append({"week": w, "n": len(days), "med": quantile(days, 0.5),
                    "p25": quantile(days, 0.25), "p75": quantile(days, 0.75)})
    return out


def survival(rows, track_day, min_n=MIN_N):
    """Kaplan-Meier se zpožděným vstupem (levé useknutí) a cenzurou.

    Jednotka = inzerát se známým začátkem, kromě předchůdců znovu vložených
    (jejich čas nese nástupce). Vstup = věk v den, kdy jsme mohli poprvé
    vidět jeho zmizení (max(náš první výskyt, TRACK_FROM)); konec = věk při
    ověřeném zmizení (událost), jinak dnes / poslední výskyt (cenzura).
    Vrací [(den, podíl stále na trhu, v riziku)] v bodech událostí, dokud je
    v riziku aspoň min_n inzerátů, a medián (první den se S <= 0,5)."""
    units = []
    if track_day is None:
        return {"curve": [], "median": None, "events": 0, "units": 0}
    for r in rows:
        if r[6] != 1 or r[9] == STATUS["relisted"]:
            continue
        entry = max(r[7], track_day) - r[5]
        exit_age = r[8] - r[5]
        if exit_age < entry:
            continue  # skončil dřív, než jsme uměli zmizení vidět
        units.append((max(0, entry), exit_age, r[9] == STATUS["gone"]))
    times = sorted({e for _en, e, ev in units if ev})
    s, curve, median = 1.0, [(0, 1.0, None)], None
    for t in times:
        at_risk = sum(1 for en, ex, _ev in units if en <= t <= ex)
        if at_risk < min_n:
            break
        d = sum(1 for _en, ex, ev in units if ev and ex == t)
        s *= 1 - d / at_risk
        curve.append((t, s, at_risk))
        if median is None and s <= 0.5:
            median = t
    return {"curve": curve, "median": median,
            "events": sum(1 for u in units if u[2]), "units": len(units)}


def cuts(rows, weeks):
    """Zlevnění po týdnech: počet, medián hloubky v %, podíl z inzerátů na trhu."""
    events = []
    for r in rows:
        p = r[10]
        # p = [den, cena, den, cena, ...]: cena i-tého bodu je p[i + 1].
        for i in range(2, len(p), 2):
            prev, cur = p[i - 1], p[i + 1]
            if prev and cur < prev:
                pct = (prev - cur) / prev * 100
                if CUT_MIN_PCT <= pct <= CUT_MAX_PCT:
                    events.append((p[i], pct, id(r)))
    out = []
    for w in weeks:
        wk = [(pct, rid) for d, pct, rid in events if w <= d <= w + 6]
        depths = sorted(pct for pct, _ in wk)
        out.append({"week": w, "n": len(wk), "listings": len({rid for _, rid in wk}),
                    "med_pct": quantile(depths, 0.5)})
    return out


# ---------------------------------------------------------------- stránka

def card_html(payload):
    """Kostra karty; filtry a grafy doplní page_js. None = žádná data -> nic
    (čip v ribbonu se sám schová)."""
    if not payload:
        return ""
    tf = payload["track_from"]
    def lbl(i):
        return day_label(tf[i]) if tf[i] is not None else "—"
    return f"""<div class="card" id="tlCard">
  <h2 style="margin-top:0;font-size:1rem;">📈 Vývoj cen a doba na trhu</h2>
  <div class="tl-filters" id="tlFilters">
    <label class="tl-f">Lokalita <select id="tlArea">
      {"".join(f'<option value="{i}">{html.escape(l)}</option>' for i, (_k, l) in enumerate(AREAS))}
    </select></label>
    <div class="tl-seg" role="group" aria-label="Typ" id="tlTx">
      <button type="button" data-tx="0">Prodej</button><button type="button" data-tx="1">Pronájem</button>
    </div>
    <label class="tl-f">m² <input id="tlMin" type="number" inputmode="numeric" min="0" max="500" placeholder="od">
      – <input id="tlMax" type="number" inputmode="numeric" min="0" max="500" placeholder="do"></label>
    <button type="button" class="linklike" id="tlReset">reset filtrů</button>
  </div>
  <div class="tl-chips" id="tlDisps" role="group" aria-label="Dispozice"></div>
  <div class="tl-chips" id="tlWards" role="group" aria-label="Čtvrť"></div>
  <div class="hint" id="tlSummary" style="margin:6px 0 4px;"></div>

  <h3 class="tl-h">Cena v čase <span class="tl-seg tl-small" role="group" aria-label="Metrika" id="tlMetric">
    <button type="button" data-m="sqm">Kč/m²</button><button type="button" data-m="price">cena</button></span>
    <label class="tl-f tl-small"><input type="checkbox" id="tlSplit"> po dispozicích</label></h3>
  <div class="tl-chart" id="tlPrice"></div>
  <div class="tl-legend" id="tlPriceLegend"></div>
  <p class="hint">Týdenní medián inzerátů, které v daném týdnu byly na trhu (cena platná na konci týdne),
    pás = prostřední polovina (25.–75. percentil). Šedý prázdný kroužek = méně než {MIN_N} inzerátů, číslo
    je jen v tooltipu. U pronájmu holé nájemné bez poplatků (poplatky portály uvádějí nestejně).
    Jsou to nabídkové ceny, ne ceny, za které se prodalo.</p>

  <h3 class="tl-h">Jak dlouho inzeráty vydrží</h3>
  <div class="stats" id="tlDomTiles"></div>
  <div class="tl-two">
    <div><div class="tl-sub">Podíl inzerátů stále na trhu po N dnech</div><div class="tl-chart" id="tlSurv"></div></div>
    <div><div class="tl-sub">Ověřeně zmizelé: dní na trhu podle týdne zmizení</div><div class="tl-chart" id="tlExits"></div>
      <div class="tl-legend"><span><span class="tl-dot"></span>jeden inzerát</span>
        <span><span class="tl-key" style="background:#d95926;height:3px"></span>medián týdne (od {MIN_N} zmizení)</span>
        <span id="tlExitsNote"></span></div></div>
  </div>
  <p class="hint">Doba na trhu = od vložení (datum ze Sreality, jinak náš první výskyt) do ověřeného zmizení
    (stránka vrací 404). Inzeráty <b>stále na trhu</b> konečnou dobu ještě nemají — do mediánu zmizelých
    nejdou, křivka vlevo je započítává jako „vydržel aspoň N dní" (Kaplan–Meier). Znovu vložený inzerát
    (↻) z trhu neodešel, jeho doba běží dál pod novým číslem. Vynechané: zombie uklizené 26. 9., inzeráty
    bez známého začátku (vtekly s prvním během sledování, bez data ze Sreality) a zmizení před ověřováním
    (Vysočany {lbl(0)}, Jinonice {lbl(1)}, U Kříže {lbl(2)}). Křivka končí, kde je v riziku méně než {MIN_N}.
    Zmizení neznamená prodej.</p>

  <h3 class="tl-h">Zlevňování</h3>
  <div class="tl-chart" id="tlCuts"></div>
  <p class="hint">Počet snížení ceny v daném týdnu (o {CUT_MIN_PCT:g}–{CUT_MAX_PCT:g} %; větší skok je chyba
    v datech). Podíl = kolik inzerátů z těch na trhu v týdnu zlevnilo.</p>

  <details class="tl-table"><summary>Tabulka po týdnech</summary>
    <div class="est-scroll"><table class="est-table" id="tlTable"></table></div>
  </details>
</div>"""


CSS = """
  #tlCard .tl-filters { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; margin: 0 0 6px; }
  #tlCard .tl-f { display: inline-flex; gap: 6px; align-items: center; font-size: 0.8rem; color: #c3c2b7; }
  #tlCard .tl-f input[type=number] { width: 4.5em; }
  #tlCard .tl-seg { display: inline-flex; border: 1px solid #2a2f3a; border-radius: 14px; overflow: hidden; }
  #tlCard .tl-seg button { background: #11141b; color: #aab; border: none; padding: 5px 11px;
                           font-size: 0.78rem; cursor: pointer; }
  #tlCard .tl-seg button[aria-pressed="true"] { background: #2563eb; color: #fff; }
  #tlCard .tl-small { font-size: 0.72rem; vertical-align: middle; margin-left: 6px; }
  #tlCard .tl-small button { padding: 2px 8px; font-size: 0.7rem; }
  #tlCard .tl-chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0; }
  #tlCard .tl-chip { background: #11141b; color: #c3c2b7; border: 1px solid #2a2f3a; border-radius: 12px;
                     padding: 3px 10px; font-size: 0.75rem; cursor: pointer; }
  #tlCard .tl-chip[aria-pressed="true"] { border-color: #7ab8ff; color: #fff; background: #1d2c44; }
  #tlCard .tl-chip .tl-n { color: #898781; margin-left: 4px; font-size: 0.68rem; }
  #tlCard .tl-h { font-size: 0.9rem; margin: 16px 0 6px; font-weight: 600; }
  #tlCard .tl-sub { font-size: 0.75rem; color: #c3c2b7; margin: 6px 0 2px; }
  #tlCard .tl-chart { position: relative; width: 100%; min-height: 60px; }
  #tlCard .tl-chart svg { display: block; width: 100%; overflow: visible; }
  #tlCard .tl-chart .tl-empty { font-size: 0.8rem; color: #898781; padding: 18px 0; }
  #tlCard .tl-two { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
  #tlCard .tl-legend { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 0.72rem; color: #c3c2b7; margin-top: 4px; }
  #tlCard .tl-key { display: inline-block; width: 14px; height: 2px; vertical-align: middle; margin-right: 4px; border-radius: 1px; }
  #tlCard .tl-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #3987e5;
                    vertical-align: middle; margin-right: 4px; }
  #tlCard .tl-tip { position: absolute; pointer-events: none; z-index: 3; background: #11141b; border: 1px solid #383835;
                    border-radius: 8px; padding: 6px 8px; font-size: 0.72rem; color: #c3c2b7; min-width: 120px;
                    box-shadow: 0 2px 8px rgba(0,0,0,.5); white-space: nowrap; }
  #tlCard .tl-tip b { color: #fff; font-weight: 600; }
  #tlCard .tl-tip .tl-row { display: flex; align-items: center; gap: 6px; }
  #tlCard .tl-tip .tl-muted { color: #898781; }
  #tlCard .stats .num { font-size: 1.05rem; }
  #tlCard .tl-table summary { cursor: pointer; font-size: 0.8rem; color: #7ab8ff; margin-top: 12px; }
  #tlCard .tl-table td, #tlCard .tl-table th { font-variant-numeric: tabular-nums; }
  #tlCard text { font-family: -apple-system, system-ui, sans-serif; }
"""


def page_js(payload_json):
    """JS karty -- vlastní IIFE, vkládá se za hlavní šablonu (před ribbon).
    `payload_json` už musí být escapovaný pro <script> (scrape.script_json).
    Nic nepotřebuje zvenku; cizí text (názvy čtvrtí) jen přes textContent."""
    return _JS.replace("__TL_JSON__", payload_json)


_JS = r"""
// ---- Vývoj cen a doba na trhu (timeline.py) --------------------------------
(function () {
  const TL = __TL_JSON__;
  const card = document.getElementById("tlCard");
  if (!card || !TL || !TL.rows) return;
  const R_AREA = 0, R_WARD = 1, R_TX = 2, R_DISP = 3, R_SQM = 4, R_START = 5, R_KNOWN = 6,
        R_OBS = 7, R_END = 8, R_ST = 9, R_P = 10;
  const ST_LIVE = 0, ST_GONE = 1, ST_RELIST = 2, ST_LOST = 3;
  const MIN_N = TL.min_n;
  const KEY = "timeline:v1";
  const EPOCH = Date.UTC(+TL.epoch.slice(0, 4), +TL.epoch.slice(5, 7) - 1, +TL.epoch.slice(8, 10));
  // Kategorické barvy (dataviz, tmavý režim, ověřeno validátorem proti #1b1f29).
  // Barva patří dispozici, ne pořadí -- filtr nepřebarví, co zůstane.
  const SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
  const C = { surface: "#1b1f29", grid: "#2c2c2a", axis: "#383835", muted: "#898781",
              ink2: "#c3c2b7", ink: "#ffffff", accent: SERIES[0], accent2: SERIES[1] };
  const $ = id => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";

  // ---------- stav filtrů
  const DEF = { area: 0, tx: 1, wards: [], disps: [], min: null, max: null, metric: "sqm", split: false };
  let F = Object.assign({}, DEF);
  try {
    const s = JSON.parse(localStorage.getItem(KEY) || "null");
    if (s && typeof s === "object") {
      F = Object.assign({}, DEF, s);
      F.area = Number.isInteger(F.area) && TL.areas[F.area] ? F.area : 0;
      F.tx = F.tx === 0 ? 0 : 1;
      F.wards = Array.isArray(F.wards) ? F.wards.filter(v => typeof v === "string") : [];
      F.disps = Array.isArray(F.disps) ? F.disps.filter(v => Number.isInteger(v)) : [];
      F.metric = F.metric === "price" ? "price" : "sqm";
      F.split = !!F.split;
      F.min = Number.isFinite(F.min) ? F.min : null;
      F.max = Number.isFinite(F.max) ? F.max : null;
    }
  } catch (e) {}
  function save() { try { localStorage.setItem(KEY, JSON.stringify(F)); } catch (e) {} }

  // ---------- pomocné
  function dayDate(d) { return new Date(EPOCH + d * 86400000); }
  function dLabel(d) { const x = dayDate(d); return x.getUTCDate() + ". " + (x.getUTCMonth() + 1) + "."; }
  function weekStart(d) { const wd = (dayDate(d).getUTCDay() + 6) % 7; return d - wd; }
  function q(sorted, p) {
    const n = sorted.length; if (!n) return null;
    const pos = (n - 1) * p, lo = Math.floor(pos), hi = Math.min(lo + 1, n - 1);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }
  function num(v, dec) {
    if (v === null || v === undefined || !isFinite(v)) return "—";
    return v.toLocaleString("cs-CZ", { maximumFractionDigits: dec || 0, minimumFractionDigits: 0 });
  }
  function fmtMoney(v, compact) {
    if (v === null || v === undefined || !isFinite(v)) return "—";
    if (Math.abs(v) >= 1e6) return num(v / 1e6, compact ? 1 : 2) + " mil.";
    if (compact && Math.abs(v) >= 1e4) return num(v / 1e3, 0) + " tis.";
    return num(Math.round(v)) + " Kč";
  }
  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function priceOn(r, day) {
    const p = r[R_P]; let v = p[1];
    for (let i = 0; i < p.length; i += 2) { if (p[i] <= day) v = p[i + 1]; else break; }
    return v;
  }
  function niceTicks(lo, hi, count) {
    if (!(hi > lo)) { hi = lo + 1; }
    const raw = (hi - lo) / Math.max(1, count), mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || raw;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
    return { ticks: out, lo: Math.min(lo, out[0] ?? lo), hi: Math.max(hi, out[out.length - 1] ?? hi) };
  }

  // ---------- filtr a agregace (zrcadlo timeline.py)
  function wardIdxSet() {
    if (!F.wards.length) return null;
    const s = new Set();
    F.wards.forEach(w => { const i = TL.wards.indexOf(w); if (i >= 0) s.add(i); });
    return s;
  }
  function dispsHere() {
    const s = new Set();
    TL.rows.forEach(r => { if (r[R_AREA] === F.area && r[R_TX] === F.tx) s.add(r[R_DISP]); });
    return [...s].sort((a, b) => a - b);
  }
  function wardsHere() {
    const m = new Map();
    TL.rows.forEach(r => { if (r[R_AREA] === F.area && r[R_TX] === F.tx)
      m.set(r[R_WARD], (m.get(r[R_WARD]) || 0) + 1); });
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }
  function select(flt) {
    const wards = flt.wards, disps = flt.disps;
    return TL.rows.filter(r => r[R_AREA] === flt.area && r[R_TX] === flt.tx
      && (!wards || wards.has(r[R_WARD])) && (!disps || disps.has(r[R_DISP]))
      && (flt.min === null || r[R_SQM] >= flt.min) && (flt.max === null || r[R_SQM] <= flt.max));
  }
  function currentFilter() {
    const avail = new Set(dispsHere());
    const ds = F.disps.filter(d => avail.has(d));
    const wi = wardIdxSet();
    return { area: F.area, tx: F.tx, wards: wi && wi.size ? wi : null,
             disps: ds.length ? new Set(ds) : null, min: F.min, max: F.max };
  }
  function weeksFor(area) {
    const first = TL.track_from[area]; if (first === null || first === undefined) return [];
    const out = []; for (let w = weekStart(first); w <= TL.now; w += 7) out.push(w);
    return out;
  }
  function weekly(rows, weeks) {
    return weeks.map(w => {
      const sun = Math.min(w + 6, TL.now), sqm = [], price = [];
      rows.forEach(r => {
        if (r[R_P][0] <= sun && r[R_END] >= w) {
          const v = priceOn(r, Math.min(sun, r[R_END])) * 100;
          price.push(v); sqm.push(v / r[R_SQM]);
        }
      });
      sqm.sort((a, b) => a - b); price.sort((a, b) => a - b);
      return { week: w, n: price.length,
        sqm_med: q(sqm, .5), sqm_p25: q(sqm, .25), sqm_p75: q(sqm, .75),
        price_med: q(price, .5), price_p25: q(price, .25), price_p75: q(price, .75) };
    });
  }
  function exitsOf(rows, track) {
    return rows.filter(r => r[R_ST] === ST_GONE && r[R_KNOWN] === 1 && track !== null && r[R_END] >= track)
               .map(r => [r[R_END], r[R_END] - r[R_START]]);
  }
  function exitsByWeek(rows, weeks, track) {
    const ex = exitsOf(rows, track);
    return weeks.map(w => {
      const days = ex.filter(([g]) => g >= w && g <= w + 6).map(e => e[1]).sort((a, b) => a - b);
      return { week: w, n: days.length, med: q(days, .5), p25: q(days, .25), p75: q(days, .75), days };
    });
  }
  function survival(rows, track) {
    const units = [];
    if (track === null || track === undefined) return { curve: [], median: null, events: 0, units: 0 };
    rows.forEach(r => {
      if (r[R_KNOWN] !== 1 || r[R_ST] === ST_RELIST) return;
      const entry = Math.max(r[R_OBS], track) - r[R_START], ex = r[R_END] - r[R_START];
      if (ex < entry) return;
      units.push([Math.max(0, entry), ex, r[R_ST] === ST_GONE]);
    });
    const times = [...new Set(units.filter(u => u[2]).map(u => u[1]))].sort((a, b) => a - b);
    let s = 1, median = null; const curve = [[0, 1, null]];
    for (const t of times) {
      let risk = 0, d = 0;
      for (const u of units) { if (u[0] <= t && t <= u[1]) risk++; if (u[2] && u[1] === t) d++; }
      if (risk < MIN_N) break;
      s *= 1 - d / risk;
      curve.push([t, s, risk]);
      if (median === null && s <= 0.5) median = t;
    }
    return { curve, median, events: units.filter(u => u[2]).length, units: units.length };
  }
  function cutsByWeek(rows, weeks) {
    const ev = [];
    rows.forEach((r, ri) => {
      const p = r[R_P];
      for (let i = 2; i < p.length; i += 2) {
        const prev = p[i - 1], cur = p[i + 1];
        if (prev && cur < prev) {
          const pct = (prev - cur) / prev * 100;
          if (pct >= TL.cut[0] && pct <= TL.cut[1]) ev.push([p[i], pct, ri]);
        }
      }
    });
    return weeks.map(w => {
      const wk = ev.filter(e => e[0] >= w && e[0] <= w + 6);
      const depths = wk.map(e => e[1]).sort((a, b) => a - b);
      return { week: w, n: wk.length, listings: new Set(wk.map(e => e[2])).size, med_pct: q(depths, .5) };
    });
  }
  function compute(flt) {
    const rows = select(flt), weeks = weeksFor(flt.area), track = TL.track_from[flt.area];
    return { rows, weeks, track, weekly: weekly(rows, weeks), exits: exitsByWeek(rows, weeks, track),
             surv: survival(rows, track), cuts: cutsByWeek(rows, weeks) };
  }
  // Pro ověření proti Pythonu (jsdom) -- nic na stránce to nevolá.
  window.tlCompute = function (flt) {
    const f = Object.assign({ wards: null, disps: null, min: null, max: null }, flt);
    if (Array.isArray(f.wards)) f.wards = new Set(f.wards);
    if (Array.isArray(f.disps)) f.disps = new Set(f.disps);
    const r = compute(f);
    return { n: r.rows.length, weekly: r.weekly, exits: r.exits.map(e => ({ week: e.week, n: e.n, med: e.med })),
             surv: r.surv, cuts: r.cuts };
  };

  // ---------- tooltip
  function tipFor(box) {
    let t = box.querySelector(".tl-tip");
    if (!t) { t = document.createElement("div"); t.className = "tl-tip"; t.hidden = true; box.appendChild(t); }
    return t;
  }
  // rows: [[barva|null, hodnota (silně), popisek]]
  function showTip(box, x, y, title, rows) {
    const t = tipFor(box);
    t.textContent = "";
    const h = document.createElement("div"); h.className = "tl-muted"; h.textContent = title; t.appendChild(h);
    rows.forEach(([color, value, label]) => {
      const row = document.createElement("div"); row.className = "tl-row";
      if (color) { const k = document.createElement("span"); k.className = "tl-key"; k.style.background = color; row.appendChild(k); }
      const b = document.createElement("b"); b.textContent = value; row.appendChild(b);
      if (label) { const s = document.createElement("span"); s.textContent = label; row.appendChild(s); }
      t.appendChild(row);
    });
    t.hidden = false;
    const bw = box.clientWidth, tw = t.offsetWidth || 160;
    t.style.left = Math.max(0, Math.min(bw - tw, x + 12 > bw - tw ? x - tw - 12 : x + 12)) + "px";
    t.style.top = Math.max(0, y - 10) + "px";
  }
  function hideTip(box) { const t = box.querySelector(".tl-tip"); if (t) t.hidden = true; }

  // ---------- kostra grafu: osy, mřížka, hover vrstva
  function frame(box, opts) {
    box.querySelectorAll("svg, .tl-empty").forEach(n => n.remove());
    const W = Math.max(260, box.clientWidth || 600), H = opts.height || 220;
    const m = { l: opts.left || 52, r: 12, t: 10, b: 26 };
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.label || "" });
    box.insertBefore(svg, box.firstChild);
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const yt = niceTicks(opts.yLo, opts.yHi, opts.yTicks || 4);
    const y = v => m.t + ph - (v - yt.lo) / (yt.hi - yt.lo) * ph;
    const g = el("g", {}, svg);
    yt.ticks.forEach(v => {
      el("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: C.grid, "stroke-width": 1 }, g);
      const tx = el("text", { x: m.l - 6, y: y(v) + 3, "text-anchor": "end", "font-size": 10, fill: C.muted }, g);
      tx.textContent = opts.yFmt(v);
    });
    el("line", { x1: m.l, x2: W - m.r, y1: m.t + ph, y2: m.t + ph, stroke: C.axis, "stroke-width": 1 }, g);
    return { svg, W, H, m, pw, ph, y, g };
  }
  function empty(box, text) {
    box.querySelectorAll("svg, .tl-empty").forEach(n => n.remove());
    hideTip(box);
    const d = document.createElement("div"); d.className = "tl-empty"; d.textContent = text; box.appendChild(d);
  }
  function weekX(fr, weeks) {
    const n = weeks.length, band = fr.pw / Math.max(1, n);
    return { band, x: i => fr.m.l + band * (i + 0.5) };
  }
  function weekAxis(fr, weeks, wx) {
    const every = Math.max(1, Math.ceil(weeks.length / Math.max(1, Math.floor(fr.pw / 56))));
    weeks.forEach((w, i) => {
      if (i % every) return;
      const t = el("text", { x: wx.x(i), y: fr.m.t + fr.ph + 16, "text-anchor": "middle", "font-size": 10, fill: C.muted }, fr.g);
      t.textContent = dLabel(w) + (w + 6 > TL.now ? "*" : "");
    });
  }
  function hover(fr, box, count, xOf, onIdx) {
    const cross = el("line", { y1: fr.m.t, y2: fr.m.t + fr.ph, stroke: C.ink2, "stroke-width": 1, opacity: 0 }, fr.svg);
    const hit = el("rect", { x: fr.m.l, y: fr.m.t, width: fr.pw, height: fr.ph, fill: "transparent" }, fr.svg);
    function at(evt) {
      const rect = fr.svg.getBoundingClientRect();
      const sx = (evt.clientX - rect.left) * (fr.W / (rect.width || fr.W));
      let best = 0, bd = Infinity;
      for (let i = 0; i < count; i++) { const d = Math.abs(xOf(i) - sx); if (d < bd) { bd = d; best = i; } }
      cross.setAttribute("x1", xOf(best)); cross.setAttribute("x2", xOf(best)); cross.setAttribute("opacity", 0.6);
      const px = xOf(best) * ((rect.width || fr.W) / fr.W);
      onIdx(best, px, (evt.clientY - rect.top));
    }
    hit.addEventListener("pointermove", at);
    hit.addEventListener("pointerdown", at);
    hit.addEventListener("pointerleave", () => { cross.setAttribute("opacity", 0); hideTip(box); });
  }

  // ---------- graf 1: cena v čase
  function drawPrice(res) {
    const box = $("tlPrice"), legend = $("tlPriceLegend");
    legend.textContent = "";
    const weeks = res.weeks;
    if (!weeks.length || !res.rows.length) { empty(box, "Pro tenhle výběr nejsou data."); return; }
    const key = F.metric === "sqm" ? "sqm" : "price";
    const fmtAxis = v => F.metric === "sqm" ? (v >= 1e4 ? num(v / 1e3) + " tis." : num(v)) : fmtMoney(v, true);
    const fmtVal = v => F.metric === "sqm" ? num(Math.round(v)) + " Kč/m²" : fmtMoney(v);
    let series;
    if (F.split) {
      const flt = currentFilter();
      const ds = flt.disps ? [...flt.disps] : dispsHere();
      series = ds.sort((a, b) => a - b).map(d => {
        const f2 = Object.assign({}, flt, { disps: new Set([d]) });
        return { name: TL.disps[d], color: SERIES[d % SERIES.length], pts: weekly(select(f2), weeks) };
      }).filter(s => s.pts.some(p => p.n >= MIN_N));
    } else {
      series = [{ name: "medián", color: C.accent, pts: res.weekly, band: true }];
    }
    const vals = [];
    series.forEach(s => s.pts.forEach(p => {
      if (p.n >= MIN_N) { vals.push(p[key + "_med"]); if (s.band) vals.push(p[key + "_p25"], p[key + "_p75"]); }
      else if (p.n) vals.push(p[key + "_med"]);
    }));
    if (!vals.length) { empty(box, "Pro tenhle výběr nejsou data."); return; }
    let lo = Math.min(...vals), hi = Math.max(...vals); const pad = (hi - lo) * 0.08 || hi * 0.05;
    const fr = frame(box, { yLo: Math.max(0, lo - pad), yHi: hi + pad, yFmt: fmtAxis, left: 56,
                            label: "Týdenní medián " + (key === "sqm" ? "Kč/m²" : "ceny") });
    const wx = weekX(fr, weeks);
    weekAxis(fr, weeks, wx);
    series.forEach(s => {
      const ok = s.pts.map(p => p.n >= MIN_N);
      // Pás a čára jen přes souvislé úseky s dost daty -- díra zůstane dírou.
      let seg = [];
      const flush = () => {
        if (!seg.length) return;
        if (s.band && seg.length > 1) {
          const top = seg.map(i => `${wx.x(i)},${fr.y(s.pts[i][key + "_p75"])}`);
          const bot = seg.slice().reverse().map(i => `${wx.x(i)},${fr.y(s.pts[i][key + "_p25"])}`);
          el("polygon", { points: top.concat(bot).join(" "), fill: s.color, opacity: 0.14 }, fr.g);
        } else if (s.band) {
          const i = seg[0];
          el("line", { x1: wx.x(i), x2: wx.x(i), y1: fr.y(s.pts[i][key + "_p25"]), y2: fr.y(s.pts[i][key + "_p75"]),
                       stroke: s.color, "stroke-width": 6, opacity: 0.25, "stroke-linecap": "round" }, fr.g);
        }
        if (seg.length > 1) el("polyline", { points: seg.map(i => `${wx.x(i)},${fr.y(s.pts[i][key + "_med"])}`).join(" "),
          fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, fr.g);
        seg = [];
      };
      ok.forEach((v, i) => { if (v) seg.push(i); else flush(); });
      flush();
      s.pts.forEach((p, i) => {
        if (!p.n) return;
        const good = p.n >= MIN_N;
        el("circle", { cx: wx.x(i), cy: fr.y(p[key + "_med"]), r: 4,
          fill: good ? s.color : C.surface, stroke: good ? C.surface : C.muted, "stroke-width": 2 }, fr.g);
      });
    });
    // Přímý popisek konce -- jen u jedné řady (u víc řad legenda).
    if (series.length === 1) {
      const s = series[0]; let li = -1;
      s.pts.forEach((p, i) => { if (p.n >= MIN_N) li = i; });
      if (li >= 0) {
        const t = el("text", { x: Math.min(wx.x(li), fr.W - fr.m.r), y: fr.y(s.pts[li][key + "_med"]) - 9,
          "text-anchor": "end", "font-size": 11, fill: C.ink, "font-weight": 600 }, fr.g);
        t.textContent = fmtVal(s.pts[li][key + "_med"]);
      }
    } else {
      series.forEach(s => {
        const it = document.createElement("span");
        const k = document.createElement("span"); k.className = "tl-key"; k.style.background = s.color;
        it.appendChild(k); it.appendChild(document.createTextNode(s.name));
        legend.appendChild(it);
      });
    }
    hover(fr, box, weeks.length, wx.x, (i, px, py) => {
      const w = weeks[i];
      const rows = series.map(s => {
        const p = s.pts[i];
        if (!p.n) return [s.color, "—", (series.length > 1 ? s.name + " · " : "") + "bez inzerátů"];
        const small = p.n < MIN_N ? " · málo dat" : "";
        const band = s.band && p.n >= MIN_N ? ` · IQR ${fmtVal(p[key + "_p25"])} – ${fmtVal(p[key + "_p75"])}` : "";
        return [s.color, fmtVal(p[key + "_med"]), (series.length > 1 ? s.name + " · " : "") + `n=${p.n}${small}${band}`];
      });
      showTip(box, px, py, `týden od ${dLabel(w)}${w + 6 > TL.now ? " (rozpracovaný)" : ""}`, rows);
    });
  }

  // ---------- graf 2a: přežití
  function drawSurv(res) {
    const box = $("tlSurv"), sv = res.surv;
    if (sv.curve.length < 2) { empty(box, sv.units ? `Zatím málo ověřených zmizení (${sv.events}).` : "Pro tenhle výběr nejsou data."); return; }
    const maxT = sv.curve[sv.curve.length - 1][0];
    const fr = frame(box, { yLo: 0, yHi: 1, yFmt: v => Math.round(v * 100) + " %", left: 40, height: 200,
                            label: "Podíl inzerátů stále na trhu podle počtu dní" });
    const x = d => fr.m.l + d / Math.max(1, maxT) * fr.pw;
    const xt = niceTicks(0, maxT, Math.max(2, Math.floor(fr.pw / 60)));
    xt.ticks.forEach(d => { if (d > maxT) return;
      const t = el("text", { x: x(d), y: fr.m.t + fr.ph + 16, "text-anchor": "middle", "font-size": 10, fill: C.muted }, fr.g);
      t.textContent = num(d) + " d"; });
    el("line", { x1: fr.m.l, x2: fr.W - fr.m.r, y1: fr.y(0.5), y2: fr.y(0.5), stroke: C.axis, "stroke-width": 1 }, fr.g);
    let d = `M${x(0)},${fr.y(1)}`, prev = 1;
    sv.curve.slice(1).forEach(([t, s]) => { d += ` L${x(t)},${fr.y(prev)} L${x(t)},${fr.y(s)}`; prev = s; });
    el("path", { d, fill: "none", stroke: C.accent, "stroke-width": 2, "stroke-linejoin": "round" }, fr.g);
    const last = sv.curve[sv.curve.length - 1];
    el("circle", { cx: x(last[0]), cy: fr.y(last[1]), r: 4, fill: C.accent, stroke: C.surface, "stroke-width": 2 }, fr.g);
    if (sv.median !== null) {
      const t = el("text", { x: Math.min(x(sv.median) + 4, fr.W - 60), y: fr.y(0.5) - 5, "font-size": 11, fill: C.ink, "font-weight": 600 }, fr.g);
      t.textContent = "polovina do " + num(sv.median) + " dní";
    }
    const pts = sv.curve;
    hover(fr, box, pts.length, i => x(pts[i][0]), (i, px, py) => {
      const [t, s, risk] = pts[i];
      showTip(box, px, py, `po ${num(t)} dnech`, [[C.accent, Math.round(s * 100) + " %", "stále na trhu"],
        [null, risk === null ? "—" : `n=${risk}`, "v riziku (sledovaných v tu chvíli)"]]);
    });
  }

  // ---------- graf 2b: zmizelé podle týdne
  function drawExits(res) {
    const box = $("tlExits"), ex = res.exits, weeks = res.weeks;
    const all = ex.flatMap(e => e.days);
    if (!all.length) { empty(box, "Zatím žádné ověřené zmizení se známým začátkem."); return; }
    const hi = q(all.slice().sort((a, b) => a - b), 0.95);
    const cap = Math.max(10, hi);
    const fr = frame(box, { yLo: 0, yHi: cap, yFmt: v => num(v) + " d", left: 40, height: 200,
                            label: "Dní na trhu u ověřeně zmizelých inzerátů podle týdne zmizení" });
    const wx = weekX(fr, weeks);
    weekAxis(fr, weeks, wx);
    ex.forEach((e, i) => {
      const good = e.n >= MIN_N;
      e.days.forEach((d, j) => {
        const jit = ((j * 37) % 11 - 5) / 5 * Math.min(10, wx.band * 0.25);
        el("circle", { cx: wx.x(i) + jit, cy: fr.y(Math.min(d, cap)), r: 2.5,
          fill: good ? C.accent : C.muted, opacity: d > cap ? 0.9 : 0.45 }, fr.g);
      });
      if (good) {
        const hw = Math.min(14, wx.band * 0.35);
        el("line", { x1: wx.x(i) - hw, x2: wx.x(i) + hw, y1: fr.y(e.med), y2: fr.y(e.med),
          stroke: C.accent2, "stroke-width": 3, "stroke-linecap": "round" }, fr.g);
      }
    });
    hover(fr, box, weeks.length, wx.x, (i, px, py) => {
      const e = ex[i];
      const rows = e.n ? [[C.accent2, e.n >= MIN_N ? num(e.med) + " dní" : "—", "medián" + (e.n < MIN_N ? " (málo dat)" : "")],
        [null, `n=${e.n}`, e.n >= MIN_N ? `IQR ${num(e.p25)}–${num(e.p75)} dní` : ""]] : [[null, "0", "ověřených zmizení"]];
      showTip(box, px, py, `zmizelo v týdnu od ${dLabel(weeks[i])}`, rows);
    });
    const note = $("tlExitsNote");
    if (note) note.textContent = `nad ${num(Math.round(cap))} dní (95. percentil) oříznuto na horní okraj`;
  }

  // ---------- graf 3: zlevnění
  function drawCuts(res) {
    const box = $("tlCuts"), cs = res.cuts, weeks = res.weeks, wk = res.weekly;
    if (!weeks.length || !res.rows.length) { empty(box, "Pro tenhle výběr nejsou data."); return; }
    const mx = Math.max(1, ...cs.map(c => c.n));
    const fr = frame(box, { yLo: 0, yHi: mx, yFmt: v => Number.isInteger(v) ? num(v) : "", left: 40, height: 170, yTicks: 3,
                            label: "Počet zlevnění po týdnech" });
    const wx = weekX(fr, weeks);
    weekAxis(fr, weeks, wx);
    const bw = Math.min(24, wx.band * 0.6);
    cs.forEach((c, i) => {
      if (!c.n) return;
      const top = fr.y(c.n), base = fr.y(0), h = Math.max(1, base - top), r = Math.min(4, h, bw / 2), x0 = wx.x(i) - bw / 2;
      el("path", { d: `M${x0},${base} V${top + r} Q${x0},${top} ${x0 + r},${top} H${x0 + bw - r} Q${x0 + bw},${top} ${x0 + bw},${top + r} V${base} Z`,
        fill: C.accent }, fr.g);
      if (bw >= 14) {
        const t = el("text", { x: wx.x(i), y: top - 4, "text-anchor": "middle", "font-size": 10, fill: C.ink2 }, fr.g);
        t.textContent = num(c.n);
      }
    });
    hover(fr, box, weeks.length, wx.x, (i, px, py) => {
      const c = cs[i], n = wk[i].n;
      showTip(box, px, py, `týden od ${dLabel(weeks[i])}${weeks[i] + 6 > TL.now ? " (rozpracovaný)" : ""}`, [
        [C.accent, num(c.n), "zlevnění"],
        [null, n ? num(c.listings / n * 100, 1) + " %" : "—", `inzerátů zlevnilo (${c.listings} z ${n} na trhu)`],
        [null, c.n ? "−" + num(c.med_pct, 1) + " %" : "—", "medián snížení"]]);
    });
  }

  // ---------- dlaždice, souhrn, tabulka
  function tile(parent, value, label) {
    const d = document.createElement("div"); d.className = "stat";
    const n = document.createElement("div"); n.className = "num"; n.textContent = value;
    const l = document.createElement("div"); l.className = "lbl"; l.textContent = label;
    d.appendChild(n); d.appendChild(l); parent.appendChild(d);
  }
  function drawTiles(res) {
    const box = $("tlDomTiles"); box.textContent = "";
    const rows = res.rows, sv = res.surv;
    const gone = res.exits.reduce((a, e) => a + e.n, 0);
    const goneDays = res.exits.flatMap(e => e.days).sort((a, b) => a - b);
    const live = rows.filter(r => r[R_ST] === ST_LIVE);
    const liveKnown = live.filter(r => r[R_KNOWN] === 1).map(r => TL.now - r[R_START]).sort((a, b) => a - b);
    tile(box, sv.median !== null ? num(sv.median) + " dní" : (sv.curve.length > 1 ? "> " + num(sv.curve[sv.curve.length - 1][0]) + " dní" : "—"),
         "polovina inzerátů zmizí do (KM, vč. živých)");
    tile(box, gone >= MIN_N ? num(q(goneDays, .5)) + " dní" : "—", `medián u ověřeně zmizelých (n=${gone})`);
    tile(box, num(live.length), liveKnown.length ? `stále na trhu · zatím medián ${num(q(liveKnown, .5))} dní` : "stále na trhu");
  }
  function drawSummary(res) {
    const rows = res.rows, c = [0, 0, 0, 0];
    rows.forEach(r => c[r[R_ST]]++);
    const unknown = rows.filter(r => r[R_KNOWN] !== 1 && r[R_ST] !== ST_RELIST).length;
    const tf = TL.track_from[F.area];
    $("tlSummary").textContent = `Vybráno ${num(rows.length)} inzerátů: ${num(c[ST_LIVE])} na trhu, ` +
      `${num(c[ST_GONE])} ověřeně zmizelo, ${num(c[ST_RELIST])} znovu vloženo, ${num(c[ST_LOST])} ztraceno ze sledování ` +
      `(přestaly chodit ve výsledcích bez ověřeného zmizení). Začátek neznámý u ${num(unknown)}. ` +
      `Sledováno od ${tf !== null && tf !== undefined ? dLabel(tf) : "—"} · * = rozpracovaný týden.`;
  }
  function drawTable(res) {
    const t = $("tlTable"); t.textContent = "";
    const head = ["Týden", "Na trhu n", "Medián Kč/m²", "IQR Kč/m²", "Medián cena", "Zmizelo n", "Medián dní", "Zlevnění n"];
    const tr = document.createElement("tr");
    head.forEach(h => { const th = document.createElement("th"); th.textContent = h; tr.appendChild(th); });
    const thead = document.createElement("thead"); thead.appendChild(tr); t.appendChild(thead);
    const tb = document.createElement("tbody");
    res.weeks.forEach((w, i) => {
      const p = res.weekly[i], e = res.exits[i], c = res.cuts[i], ok = p.n >= MIN_N;
      const cells = [dLabel(w) + (w + 6 > TL.now ? "*" : ""), num(p.n),
        ok ? num(Math.round(p.sqm_med)) : "—", ok ? `${num(Math.round(p.sqm_p25))}–${num(Math.round(p.sqm_p75))}` : "—",
        ok ? fmtMoney(p.price_med) : "—", num(e.n), e.n >= MIN_N ? num(e.med) : "—", num(c.n)];
      const row = document.createElement("tr");
      cells.forEach(v => { const td = document.createElement("td"); td.textContent = v; row.appendChild(td); });
      tb.appendChild(row);
    });
    t.appendChild(tb);
  }

  // ---------- ovládání
  function chip(parent, label, count, pressed, onClick) {
    const b = document.createElement("button"); b.type = "button"; b.className = "tl-chip";
    b.setAttribute("aria-pressed", String(pressed));
    b.appendChild(document.createTextNode(label));
    if (count !== null) { const n = document.createElement("span"); n.className = "tl-n"; n.textContent = count; b.appendChild(n); }
    b.addEventListener("click", onClick);
    parent.appendChild(b);
  }
  function drawControls() {
    $("tlArea").value = String(F.area);
    card.querySelectorAll("#tlTx button").forEach(b => b.setAttribute("aria-pressed", String(+b.dataset.tx === F.tx)));
    card.querySelectorAll("#tlMetric button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.m === F.metric)));
    $("tlSplit").checked = F.split;
    $("tlMin").value = F.min === null ? "" : F.min;
    $("tlMax").value = F.max === null ? "" : F.max;
    const dbox = $("tlDisps"); dbox.textContent = "";
    const ds = dispsHere(), sel = new Set(F.disps.filter(d => ds.includes(d)));
    chip(dbox, "všechny dispozice", null, sel.size === 0, () => { F.disps = []; update(); });
    ds.forEach(d => chip(dbox, TL.disps[d], null, sel.has(d), () => {
      F.disps = sel.has(d) ? [...sel].filter(x => x !== d) : [...sel, d]; update(); }));
    const wbox = $("tlWards"); wbox.textContent = "";
    const ws = wardsHere();
    const wsel = new Set(F.wards.filter(w => ws.some(([i]) => TL.wards[i] === w)));
    if (ws.length > 1) {
      chip(wbox, "všechny čtvrti", null, wsel.size === 0, () => { F.wards = []; update(); });
      ws.forEach(([i, n]) => { const name = TL.wards[i];
        chip(wbox, name, n, wsel.has(name), () => {
          F.wards = wsel.has(name) ? [...wsel].filter(x => x !== name) : [...wsel, name]; update(); }); });
    }
  }
  function draw() {
    const res = compute(currentFilter());
    drawSummary(res); drawPrice(res); drawTiles(res); drawSurv(res); drawExits(res); drawCuts(res); drawTable(res);
  }
  function update() { save(); drawControls(); draw(); }

  $("tlArea").addEventListener("change", e => { F.area = +e.target.value || 0; F.wards = []; F.disps = []; update(); });
  card.querySelectorAll("#tlTx button").forEach(b => b.addEventListener("click", () => { F.tx = +b.dataset.tx; update(); }));
  card.querySelectorAll("#tlMetric button").forEach(b => b.addEventListener("click", () => { F.metric = b.dataset.m; update(); }));
  $("tlSplit").addEventListener("change", e => { F.split = e.target.checked; update(); });
  function readNum(v) { const n = parseFloat(v); return Number.isFinite(n) && n >= 0 ? n : null; }
  $("tlMin").addEventListener("change", e => { F.min = readNum(e.target.value); update(); });
  $("tlMax").addEventListener("change", e => { F.max = readNum(e.target.value); update(); });
  $("tlReset").addEventListener("click", () => { F = Object.assign({}, DEF); update(); });

  // Karta může startovat sbalená (šířka 0) -- kreslit znovu, když dostane
  // rozměr, a při změně šířky (otočení telefonu).
  let lastW = -1;
  const onResize = () => { const w = $("tlPrice").clientWidth; if (w && w !== lastW) { lastW = w; draw(); } };
  if ("ResizeObserver" in window) new ResizeObserver(onResize).observe($("tlPrice"));
  else window.addEventListener("resize", onResize);
  drawControls();
  draw();
})();
"""
