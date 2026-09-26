"""Ribbon nahoře na dashboardu: navigace mezi kartami, přepínač oblasti a
„🔥 Žhavé nabídky".

Radim (26. 9. 2026): „přidej žhavé nabídky a section selection... hned na
začátku takový ribbon". Stránka má tucet karet pod sebou a to, co ho zajímá
nejdřív -- co je teď levné nebo právě zlevnilo -- bylo tři obrazovky dolů.

Rozdělení práce:
  * VÝBĚR žhavých nabídek je deterministický a běží v Pythonu při generování
    stránky (`hot_offers`). Žádné LLM, žádná síť: stejná data dají stejný
    výběr a každý důvod („−14 % pod mediánem 2+kk") jde dohledat v datech.
  * JS jen vykresluje hotový seznam `HOT`, filtruje ho podle oblasti a řeší
    skoky na karty.

Stránka je veřejná (GitHub Pages) a texty jsou cizí -- názvy, ulice a URL
přicházejí z portálů. Proto: každý scrapovaný řetězec jde přes escapeHtml
(JS) nebo html.escape (Python), URL projde jen s https://, a id inzerátu se
do HTML atributu vůbec nedává -- karta nese jen index do pole HOT a handler
si id vezme odtamtud. Tím odpadá celá třída chyb „uvozovka v id rozbila
onclick", kterou už repo jednou zažilo.

Tenhle modul záměrně neimportuje scrape.py: scrape.py importuje jeho a
kruhový import by se rozbil při prvním refaktoru.
"""
import html
import json
import math
from datetime import datetime, timezone

# Oblasti a jejich popisky -- zrcadlí AREAS v scrape.py. Jsou to naše vlastní
# řetězce, ne scrapovaný text, takže je smí JS vložit i bez escapování
# (escapuje je stejně, pro jistotu).
AREA_LABELS = {"vysocany": "Vysočany", "jinonice": "Jinonice"}
# Záznamy z doby před 26. 9. nemají `area` a všechny jsou z Vysočan.
HOME_AREA = "vysocany"

# --- Prahy a váhy skóre ------------------------------------------------------
# Skóre je součet bodů za jednotlivé signály, aby šly srovnat různé druhy
# důvodů v jednom seznamu. Jednotka je „procento pod mediánem": inzerát 12 %
# pod mediánem má 12 bodů. Ostatní signály jsou naváženy tak, aby čerstvá
# událost (zlevnění, nový inzerát) přebila stejně levný, ale týdny starý
# inzerát -- ten tam bude i zítra, zlevnění je zpráva dneška.
# Body za odchylku od mediánu se nasytí na 20. Hlouběji pod mediánem roste
# spíš šance, že jde o chybu v datech (překlep v ploše, podíl, družstevní byt
# bez anuity -- viz DEAL_FLOOR_PCT v scrape.py), než že je nabídka lepší.
# Bez stropu by pás obsadily inzeráty těsně nad −45 % a čerstvá zlevnění by
# se do něj nedostala (změřeno na snapshotu z 26. 9.: 11 ze 14 míst).
MEDIAN_PTS_CAP = 20
DROP_WINDOW_H = 72          # zlevnění starší než 3 dny už není „žhavé"
DROP_MIN_PCT = 3            # pod 3 % je to zaokrouhlení, ne sleva
# Nad 50 % to není sleva, ale chyba v datech: v historii je třeba
# „18 000 → 8 500 000", tj. inzerát přehozený mezi pronájmem a prodejem.
DROP_MAX_PCT = 50
DROP_BASE_PTS = 10          # zlevnění o 6 % = 16 bodů ≈ inzerát 16 % pod mediánem
NEW_WINDOW_H = 48
# Nový inzerát se dostane do výběru jen s oporou v ceně: aspoň 5 % pod
# mediánem. Mírnější než práh výhodné nabídky (8 %), protože novinka má
# vlastní hodnotu -- ale „nové a drahé" žhavé není.
NEW_BELOW_PCT = 5
NEW_BONUS_PTS = 8
RELIST_MIN_PCT = 2          # znovu vloženo o 1 % levněji je šum
RELIST_BASE_PTS = 8
GARAGE_MAX = 3              # garáže jsou doplněk, byty jsou hlavní
# ...a proto se jejich skóre před zařazením do pásu násobí 0,6. Stání
# zlevněné z 1 800 na 1 400 Kč (−22 %) je procentně velká sleva, ale v
# korunách 400 Kč měsíčně; bez váhy by stálo na prvním místě před bytem
# zlevněným o 460 tisíc (změřeno na snapshotu z 26. 9.).
GARAGE_WEIGHT = 0.6
GARAGE_CHEAPEST_PTS = 12
GARAGE_CHEAPEST_MIN_N = 4   # „nejlevnější ze dvou" nic neříká
# Diverzifikace: když mají kandidáty obě oblasti, jedna smí mít nejvýš 60 %
# míst. Bez toho by větší trh (Vysočany mají ~3× víc inzerátů) zaplnil celý
# pás a Jinonice by v něm nebyly vůbec vidět.
AREA_MAX_SHARE = 0.6

GARAGE_KIND_LABEL = {"Garáž": "garáž", "Garážové stání": "stání"}
TX_LABEL = {"pronajem": "k pronájmu", "prodej": "na prodej"}


# --- Pomocné -----------------------------------------------------------------
def _ts(value):
    """ISO čas z dat → aware datetime; nečitelný čas je None, ne výjimka."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _czk(v):
    """„18 500 Kč" -- nezlomitelné mezery, ať se částka nerozdělí na dva řádky."""
    return f"{int(round(v)):,}".replace(",", "\u00a0") + "\u00a0Kč"


def _pct(new, old):
    """Kolik procent je `new` pod `old` (kladné = zlevnění)."""
    return (old - new) / old * 100


def _safe_url(url):
    """Jen https://. javascript:, data: a relativní cesty z cizích dat na
    veřejnou stránku nepustíme -- ani do href, ani do window.open."""
    return url if isinstance(url, str) and url.startswith("https://") else None


def _area(item):
    return item.get("area") or HOME_AREA


def _median_claim_ok(c):
    """Smí se o inzerátu tvrdit „X % pod mediánem"?

    Ne u odlehlých (podíly, dražby, překlepy v ploše -- viz DEAL_FLOOR_PCT),
    ne u ručně vyřazených ze statistiky, a ne u pronájmů bez uvedených
    poplatků: jejich celková cena chybí o reálný náklad, takže vypadají
    levněji, než jsou. To je pravidlo projektu, stejné jako u karty
    „Nejlepší nabídky"."""
    if c.get("deal_pct") is None or c.get("deal_outlier") or c.get("exclude_from_stats"):
        return False
    return not (c.get("transaction_type") == "pronajem" and c.get("fees_missing"))


def _hours_ago(at, now):
    t = _ts(at)
    return None if t is None else (now - t).total_seconds() / 3600


# --- Signály -----------------------------------------------------------------
def _recent_drops(history, now):
    """id → nejstarší „původní" cena ze zlevnění za posledních 72 h.

    Víc změn v okně se sčítá: 20 000 → 19 000 → 18 000 je jedno zlevnění
    o 10 %, ne dvě malá. Porovnává se pak se současnou cenou inzerátu, ne
    s poslední událostí -- kdyby mezitím zase zdražil, sleva neplatí."""
    first_old = {}
    for ev in history or []:
        if ev.get("kind") != "price_change":
            continue
        age = _hours_ago(ev.get("at"), now)
        if age is None or age < 0 or age > DROP_WINDOW_H:
            continue
        old = ev.get("old_price_czk")
        if not old:
            continue
        key = str(ev.get("id"))
        t = _ts(ev.get("at"))
        if key not in first_old or t < first_old[key][0]:
            first_old[key] = (t, old)
    return {k: v[1] for k, v in first_old.items()}


def _recent_new(history, now):
    """id → stáří v hodinách pro inzeráty poprvé zachycené za posledních 48 h."""
    out = {}
    for ev in history or []:
        if ev.get("kind") != "new":
            continue
        age = _hours_ago(ev.get("at"), now)
        if age is None or age < 0 or age > NEW_WINDOW_H:
            continue
        key = str(ev.get("id"))
        out[key] = min(age, out.get(key, age))
    return out


def _relist_signal(item):
    """(procento, dřívější cena), když se smazaný inzerát vrátil levněji.

    Jen verdikt „same": „maybe" je náznak pro člověka, ne fakt, na kterém by
    se dalo postavit tvrzení „znovu vloženo levněji"."""
    rel = item.get("relist_of") or {}
    if rel.get("verdict") != "same":
        return None
    old, new = rel.get("price_czk"), item.get("price_czk")
    if not old or not new or new >= old:
        return None
    pct = _pct(new, old)
    if pct < RELIST_MIN_PCT or pct > DROP_MAX_PCT:
        return None
    return pct, old


def _flat_candidate(c, drops, news):
    """Signály jednoho bytu → (skóre, kind, důvod), nebo None, když žhavý není."""
    if c.get("deal_outlier") or c.get("exclude_from_stats") or c.get("tx_suspect"):
        return None
    price = c.get("price_czk")
    if not price:
        return None
    key = str(c.get("id"))
    signals = []   # (priorita headline, body, kind, text)

    median_ok = _median_claim_ok(c)
    deal_pct = c.get("deal_pct")
    below = -deal_pct if (median_ok and deal_pct < 0) else 0
    median_text = f"−{below} % pod mediánem {c.get('disposition') or ''}".strip() if below else None

    old = drops.get(key)
    if old and price < old:
        pct = _pct(price, old)
        if DROP_MIN_PCT <= pct <= DROP_MAX_PCT:
            what = "nájem snížen" if c.get("transaction_type") == "pronajem" else "zlevněno"
            signals.append((0, DROP_BASE_PTS + pct, "drop",
                            f"{what} o {round(pct)} % (z {_czk(old)})"))

    rel = _relist_signal(c)
    if rel:
        signals.append((1, RELIST_BASE_PTS + rel[0], "relist",
                        f"znovu vloženo o {round(rel[0])} % levněji (dřív {_czk(rel[1])})"))

    age = news.get(key)
    if age is not None and below >= NEW_BELOW_PCT:
        when = "nové dnes" if age < 24 else "nové včera"
        # „dnes/včera" tady znamená posledních 24 / 48 hodin, ne kalendářní
        # den -- přesnější by bylo „před 30 h", ale to se na kartě čte hůř.
        signals.append((2, NEW_BONUS_PTS, "new", when))

    deal_ok = bool(c.get("deal_ok")) and median_ok
    if not signals and not deal_ok:
        return None

    score = min(below, MEDIAN_PTS_CAP) + sum(s[1] for s in signals)
    signals.sort(key=lambda s: s[0])
    kind = signals[0][2] if signals else "deal"
    parts = [s[3] for s in signals]
    if median_text and (deal_ok or signals):
        parts.append(median_text)
    return score, kind, " · ".join(parts[:2])


def _garage_candidates(garages):
    """Garáže: zlevněné (price_old_czk) a nejlevnější v oblasti a druhu.

    „Nejlevnější" se počítá zvlášť pro (oblast, pronájem/prodej, garáž/stání)
    -- stání a samostatná garáž jsou jiný produkt -- a jen ze skupin s aspoň
    čtyřmi inzeráty. Stání „jen pro motocykl" (štítek s ⚠) se nesoutěží:
    stojí zlomek ceny, takže by bylo nejlevnější vždycky a nic by to neříkalo.

    price_old_czk nemá datum -- je to buď „původní cena" z portálu, nebo cena
    z minulého běhu. Důvod proto říká jen „zlevněno z X", nikdy „dnes"."""
    live = [g for g in garages or []
            if not g.get("gone_at") and g.get("price_czk")
            and not any("⚠" in f for f in g.get("features") or [])]
    groups = {}
    for g in live:
        groups.setdefault((_area(g), g.get("transaction_type"), g.get("garage_kind")), []).append(g)
    cheapest = {}
    for k, items in groups.items():
        if len(items) >= GARAGE_CHEAPEST_MIN_N:
            low = min(i["price_czk"] for i in items)
            for i in items:
                if i["price_czk"] == low:
                    cheapest[str(i.get("id"))] = (k, len(items))

    out = []
    for g in live:
        price, parts, score, kind = g["price_czk"], [], 0, None
        old = g.get("price_old_czk")
        if old and price < old:
            pct = _pct(price, old)
            if DROP_MIN_PCT <= pct <= DROP_MAX_PCT:
                score += DROP_BASE_PTS + pct
                parts.append(f"zlevněno o {round(pct)} % (z {_czk(old)})")
        rel = _relist_signal(g)
        if rel:
            score += RELIST_BASE_PTS + rel[0]
            parts.append(f"znovu vloženo o {round(rel[0])} % levněji")
        ch = cheapest.get(str(g.get("id")))
        if ch:
            (area, tx, gk), n = ch
            score += GARAGE_CHEAPEST_PTS
            parts.append(f"nejlevnější {GARAGE_KIND_LABEL.get(gk, 'garáž')} "
                         f"{TX_LABEL.get(tx, '')} v oblasti (z {n})".replace("  ", " "))
        if not parts:
            continue
        out.append((score * GARAGE_WEIGHT, "garage", " · ".join(parts[:2]), g))
    return out


def _item(src, score, kind, reason, is_garage):
    """Jen pole, která ribbon potřebuje -- HOT je inlinovaný v HTML a každý
    bajt navíc je bajt navíc na mobilu."""
    return {
        "id": src.get("id"),
        "url": _safe_url(src.get("url")),
        "title": src.get("title") or "",
        "thumb": _safe_url(src.get("thumb")),
        "area": _area(src),
        "transaction_type": src.get("transaction_type"),
        "disposition": src.get("disposition") if not is_garage else src.get("garage_kind"),
        "floor_area_sqm": src.get("usable_area_sqm") if is_garage else src.get("floor_area_sqm"),
        "price_czk": src.get("price_czk"),
        "total_czk": None if is_garage else src.get("total_czk"),
        "fees_missing": bool(src.get("fees_missing")) and not is_garage,
        # Jen když se o něm smí mluvit (viz _median_claim_ok); slouží i k
        # řazení remíz.
        "deal_pct": src.get("deal_pct") if (not is_garage and _median_claim_ok(src)) else None,
        "reason": reason,
        "score": round(score, 1),
        "kind": kind,
        "is_garage": is_garage,
    }


def _dedupe_key(it):
    """Tentýž byt inzerovaný dvakrát (dva makléři, dva portály, které se
    nesloučily) by v pásu zabral dvě místa."""
    a = it.get("floor_area_sqm")
    return (it["is_garage"], it.get("transaction_type"), it.get("disposition"),
            round(a) if a else None, it.get("price_czk"))


def _pick(ranked, limit):
    """Hladový výběr s limitem na oblast.

    Limit platí jen, když má kandidáty víc oblastí; co se kvůli němu
    přeskočilo, doplní volná místa na konci -- limit je přednost, ne
    zákaz. Když má Jinonice jen dva kandidáty, pás se stejně zaplní."""
    areas = {it["area"] for it in ranked}
    cap = math.ceil(limit * AREA_MAX_SHARE) if len(areas) > 1 else limit
    picked, skipped, per_area = [], [], {}
    for it in ranked:
        if len(picked) >= limit:
            break
        if per_area.get(it["area"], 0) >= cap:
            skipped.append(it)
            continue
        picked.append(it)
        per_area[it["area"]] = per_area.get(it["area"], 0) + 1
    for it in skipped:
        if len(picked) >= limit:
            break
        picked.append(it)
    return picked


def hot_offers(comparables, history, garages, now, limit=14):
    """Žhavé nabídky pro ribbon, seřazené od nejžhavější.

    Kandidát bytu musí mít aspoň jeden z těchto signálů:
      * deal_ok -- ≥ 8 % Kč/m² pod mediánem stejné oblasti, typu a dispozice;
      * zlevnění ≥ 3 % za posledních 72 h (z historie změn, proti dnešní ceně);
      * nový za posledních 48 h A zároveň ≥ 5 % pod mediánem;
      * znovu vložený (relist_of, verdikt „same") za nižší cenu.
    Garáže: nejvýš GARAGE_MAX, zlevněné nebo nejlevnější ve své skupině.

    Tvrzení „pod mediánem" se nikdy nedělá u odlehlých, vyřazených ze
    statistiky a u pronájmů bez poplatků (_median_claim_ok). Odlehlé
    a vyřazené se nevybírají vůbec -- nejsou to nabídky, které jde koupit
    nebo pronajmout za uvedenou cenu.

    Vrací seznam dictů připravených pro script_json()."""
    now = _ts(now) or datetime.now(timezone.utc)
    drops = _recent_drops(history, now)
    news = _recent_new(history, now)

    flats = []
    for c in comparables or []:
        cand = _flat_candidate(c, drops, news)
        if cand:
            flats.append(_item(c, *cand, is_garage=False))
    garage_items = [_item(g, s, k, r, is_garage=True)
                    for s, k, r, g in _garage_candidates(garages)]

    def order(items):
        # Remíza (typicky nové inzeráty nad stropem MEDIAN_PTS_CAP) se láme
        # hlubší odchylkou od mediánu, pak id -- ať je výběr stabilní mezi
        # běhy se stejnými daty.
        return sorted(items, key=lambda it: (-it["score"], it["deal_pct"] or 0, str(it["id"])))

    def unique(items):
        seen_id, seen_key, out = set(), set(), []
        for it in items:
            k = _dedupe_key(it)
            if str(it["id"]) in seen_id or k in seen_key:
                continue
            seen_id.add(str(it["id"]))
            seen_key.add(k)
            out.append(it)
        return out

    g_pick = _pick(unique(order(garage_items)), min(GARAGE_MAX, max(limit // 4, 1)))
    f_pick = _pick(unique(order(flats)), limit - len(g_pick))
    return order(f_pick + g_pick)


# --- HTML / CSS / JS ---------------------------------------------------------
def ribbon_css():
    """CSS ribbonu. Vložit NA KONEC <style> v render_dashboard.

    Lepí se jen řádek s čipy a přepínačem (#ribNav), ne pás nabídek: ten je
    ~150 px vysoký a na telefonu by lepící pás zabral čtvrtinu obrazovky
    natrvalo. Navigace je jeden řádek (~40 px), ta se unese.

    Kontrakt proměnných: --hdr (výška hlavičky, nastavuje measureHeader)
    zůstává, ribbon přidává --rib = výška #ribNav. Lepící hlavičky tabulek
    musí sedět pod oběma: `top: calc(var(--hdr, 0px) + var(--rib, 0px))`.
    Pod 1100 px se nic nemění -- tam hlavička tabulky lepí ke svému boxu
    (top: 0) a ribbon ji neovlivní.

    Tady v CSS je f-string escapování zbytečné -- vrací se obyčejný řetězec,
    který integrátor vloží do f-stringu jako hodnotu {ribbon_css}, takže
    složené závorky zůstanou jednoduché."""
    return """
  /* Ribbon: navigace + oblast (lepí pod hlavičkou) a pás žhavých nabídek. */
  #ribNav { position: sticky; top: var(--hdr, 0px); z-index: 5; background: #131620;
            border-bottom: 1px solid #262a33; display: flex; align-items: center; gap: 8px;
            padding: 6px 12px; }
  #ribChips { display: flex; gap: 6px; overflow-x: auto; flex: 1 1 auto; min-width: 0;
              scrollbar-width: none; -webkit-overflow-scrolling: touch; }
  #ribChips::-webkit-scrollbar { display: none; }
  .rib-chip { flex-shrink: 0; background: #1b1f29; color: #cdd; border: 1px solid #2a2f3a;
              border-radius: 14px; padding: 4px 10px; font-size: 0.75rem; cursor: pointer;
              white-space: nowrap; }
  .rib-chip:hover { border-color: #7ab8ff66; }
  .rib-chip.active { background: #1d2c44; border-color: #7ab8ff; color: #fff; }
  #ribArea { display: flex; flex-shrink: 0; border: 1px solid #2a2f3a; border-radius: 14px;
             overflow: hidden; }
  .rib-area { background: #1b1f29; color: #aab; border: none; padding: 4px 9px;
              font-size: 0.72rem; cursor: pointer; }
  .rib-area[aria-pressed="true"] { background: #2563eb; color: #fff; }
  #hotStrip { margin: 12px 12px 0; }
  #hotStrip h2 { margin: 0 0 8px; font-size: 1rem; }
  #hotList { display: flex; gap: 10px; overflow-x: auto; padding-bottom: 6px;
             scroll-snap-type: x proximity; -webkit-overflow-scrolling: touch; }
  .hot-card { flex: 0 0 172px; scroll-snap-align: start; background: #1b1f29; border: 1px solid #262a33;
              border-radius: 10px; padding: 0; text-align: left; color: inherit; cursor: pointer;
              font: inherit; overflow: hidden; display: flex; flex-direction: column; }
  .hot-card:hover, .hot-card:focus-visible { border-color: #7ab8ff; outline: none; }
  .hot-card img { width: 100%; height: 88px; object-fit: cover; display: block; background: #11141b; }
  .hot-body { padding: 6px 8px 8px; display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .hot-reason { font-size: 0.66rem; font-weight: 600; color: #7CFFB2; background: #14301f;
                border-radius: 6px; padding: 2px 6px; line-height: 1.3; }
  .hot-card[data-kind="drop"] .hot-reason, .hot-card[data-kind="relist"] .hot-reason {
                color: #ffd27a; background: #33290f; }
  .hot-card[data-kind="new"] .hot-reason { color: #9cc9ff; background: #152338; }
  .hot-title { font-size: 0.75rem; color: #dde; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hot-price { font-size: 0.85rem; font-weight: 700; }
  .hot-meta { font-size: 0.64rem; color: #889; }
  .hot-empty { font-size: 0.8rem; color: #888; padding: 8px 0; }
  @media (max-width: 480px) {
    .rib-area { padding: 4px 7px; }
    .hot-card { flex-basis: 150px; }
  }
"""


def ribbon_html(sections):
    """HTML ribbonu. Vložit hned za </header>.

    `sections` = [(id_prvku, popisek), ...]. Oboje jsou naše řetězce, ale
    escapují se stejně -- šablona se kopíruje a příště tu může být něco
    z dat. Čip, jehož cíl na stránce není (karta se nevyrenderovala), JS
    schová, takže se sem smí předat i karta, která existuje jen někdy."""
    chips = "".join(
        f'<button type="button" class="rib-chip" data-target="{html.escape(str(cid), quote=True)}">'
        f'{html.escape(str(label))}</button>'
        for cid, label in sections
    )
    areas = [("", "Vše")] + list(AREA_LABELS.items())
    switch = "".join(
        f'<button type="button" class="rib-area" data-area="{html.escape(k, quote=True)}" '
        f'aria-pressed="false">{html.escape(v)}</button>'
        for k, v in areas
    )
    return f"""<nav id="ribNav" aria-label="Sekce stránky">
  <div id="ribChips">{chips}</div>
  <div id="ribArea" role="group" aria-label="Oblast">{switch}</div>
</nav>
<section id="hotStrip">
  <h2>🔥 Žhavé nabídky <span class="hint" id="hotCount"></span></h2>
  <div id="hotList"></div>
  <p class="hint">Vybráno automaticky: ≥ 8 % pod mediánem Kč/m² stejné dispozice a oblasti,
    zlevnění ≥ 3 % za poslední 3 dny, nové za 48 h pod mediánem, levněji znovu vložené.
    Pronájmy bez uvedených poplatků se jako „pod mediánem" nevydávají.</p>
</section>"""


def ribbon_js():
    """JS ribbonu. Vložit do js_template (r-string) kamkoli PŘED volání
    initRibbon(); samo nic nespouští.

    Předpoklady (globální v js_template): HOT (seznam z hot_offers přes
    script_json), ALL, openModal, escapeHtml, fmtCzk, PLACEHOLDER.
    Nastavuje window.AREA_FILTER a posílá událost „areachange" na document."""
    labels = json.dumps(AREA_LABELS, ensure_ascii=False).replace("<", "\\u003c")
    return r"""
// ---- Ribbon: sekce, oblast, žhavé nabídky ----------------------------------
const RIB_AREA_LABELS = __RIB_AREA_LABELS__;
const RIB_AREA_KEY = "areaFilter";
window.AREA_FILTER = "";

// Čísla na kartě jsou vždy z dat, text jde přes escapeHtml, URL jen https.
function ribSafeUrl(u) {
  return (typeof u === "string" && u.startsWith("https://")) ? u : "";
}

function ribOffset() {
  const cs = getComputedStyle(document.documentElement);
  const px = v => parseFloat(cs.getPropertyValue(v)) || 0;
  return px("--hdr") + px("--rib");
}

// Výška lepícího řádku -> --rib. Mění se při zalomení hlavičky i při
// otočení telefonu, proto se měří znovu při resize (stejně jako --hdr).
function measureRibbon() {
  const nav = document.getElementById("ribNav");
  if (nav) document.documentElement.style.setProperty("--rib", nav.offsetHeight + "px");
}

function ribJump(id) {
  const el = document.getElementById(id);
  if (!el) return;
  // Sbalenou kartu rozbalit: skočit na nadpis bez obsahu je k ničemu.
  if (el.classList.contains("collapsed")) {
    const h = el.querySelector(".card-toggle");
    if (h) h.click();
  }
  const top = el.getBoundingClientRect().top + window.scrollY - ribOffset() - 8;
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  window.scrollTo({ top: Math.max(0, top), behavior: reduce ? "auto" : "smooth" });
}

// Zvýrazní čip sekce, ve které se čte: poslední sekce, jejíž horní hrana
// už prošla pod ribbon. IntersectionObserver jen říká, KDY přepočítat --
// výběr podle polohy je spolehlivější než podle toho, co zrovna protíná.
let ribActive = null;
function ribPickActive() {
  const chips = [...document.querySelectorAll("#ribChips .rib-chip:not([hidden])")];
  const line = ribOffset() + 24;
  let best = null;
  chips.forEach(ch => {
    const el = document.getElementById(ch.dataset.target);
    if (el && el.getBoundingClientRect().top <= line) best = ch;
  });
  if (!best && chips.length) best = chips[0];
  if (best === ribActive) return;
  if (ribActive) ribActive.classList.remove("active");
  ribActive = best;
  if (!best) return;
  best.classList.add("active");
  // Čip posunout do viditelné části řádku -- ale ne scrollIntoView, to by
  // hýbalo i celou stránkou.
  const row = document.getElementById("ribChips");
  const l = best.offsetLeft - row.offsetLeft, r = l + best.offsetWidth;
  if (l < row.scrollLeft) row.scrollLeft = l - 12;
  else if (r > row.scrollLeft + row.clientWidth) row.scrollLeft = r - row.clientWidth + 12;
}

function ribSetArea(area, persist) {
  if (!Object.prototype.hasOwnProperty.call(RIB_AREA_LABELS, area)) area = "";
  window.AREA_FILTER = area;
  document.querySelectorAll("#ribArea .rib-area").forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset.area === area)));
  if (persist) {
    try { localStorage.setItem(RIB_AREA_KEY, area); } catch (e) {}
  }
  document.dispatchEvent(new CustomEvent("areachange"));
}

function hotPrice(h) {
  if (h.transaction_type === "pronajem") {
    // U bytu celková cena (nájem + poplatky + elektřina), jak ji čte zbytek
    // stránky; hvězdička = poplatky neuvedené, celková cena je podhodnocená.
    const v = h.is_garage ? h.price_czk : (h.total_czk ?? h.price_czk);
    if (v === null || v === undefined) return "—";
    return fmtCzk(Math.round(v)) + (h.fees_missing ? "*" : "") + "/měs";
  }
  return h.price_czk == null ? "—" : fmtCzk(Math.round(h.price_czk));
}

function renderHot() {
  const list = document.getElementById("hotList");
  if (!list) return;
  const area = window.AREA_FILTER || "";
  const items = HOT.map((h, idx) => [h, idx]).filter(([h]) => !area || (h.area || "vysocany") === area);
  const count = document.getElementById("hotCount");
  if (count) count.textContent = items.length ? `(${items.length})` : "";
  if (!items.length) {
    list.innerHTML = `<div class="hot-empty">${area ? "V téhle oblasti teď nic žhavého." : "Teď nic žhavého."}</div>`;
    return;
  }
  // Karta nese jen index do HOT, žádné id ani URL v atributech: id bývá
  // řetězec z cizího portálu a uvozovka v něm už jednou rozbila onclick.
  list.innerHTML = items.map(([h, idx]) => {
    const meta = [h.is_garage ? "🅿️" : "", RIB_AREA_LABELS[h.area || "vysocany"] || "",
                  h.floor_area_sqm ? Math.round(h.floor_area_sqm) + " m²" : ""].filter(Boolean).join(" · ");
    return `<button type="button" class="hot-card" data-idx="${idx}" data-kind="${escapeHtml(h.kind)}">
      <img src="${escapeHtml(ribSafeUrl(h.thumb) || PLACEHOLDER)}" loading="lazy" alt="">
      <div class="hot-body">
        <span class="hot-reason">${escapeHtml(h.reason)}</span>
        <span class="hot-title" title="${escapeHtml(h.title)}">${escapeHtml(h.title)}</span>
        <span class="hot-price">${escapeHtml(hotPrice(h))}</span>
        <span class="hot-meta">${escapeHtml(meta)}</span>
      </div>
    </button>`;
  }).join("");
  list.querySelectorAll("img").forEach(img =>
    img.addEventListener("error", () => { img.src = PLACEHOLDER; }, { once: true }));
}

function openHot(idx) {
  const h = HOT[idx];
  if (!h) return;
  // Detail v modalu jen u bytu, který stránka zná (garáže v ALL nejsou);
  // jinak rovnou na portál, v nové záložce a bez window.opener.
  if (!h.is_garage && ALL.some(r => r.id === h.id)) { openModal(h.id); return; }
  // Garáž má vlastní detail (openGarage), když ji stránka zná.
  if (h.is_garage && typeof openGarage === "function" && typeof GARAGE_BY_ID !== "undefined"
      && GARAGE_BY_ID.has(String(h.id))) { openGarage(h.id); return; }
  const url = ribSafeUrl(h.url);
  if (url) window.open(url, "_blank", "noopener,noreferrer");
}

// Volat jednou, na konci js_template, po vykreslení tabulek a karet (a po
// makeCollapsible/measureHeader, protože ribbon měří až hotovou stránku).
function initRibbon() {
  document.querySelectorAll("#ribChips .rib-chip").forEach(ch => {
    if (!document.getElementById(ch.dataset.target)) { ch.hidden = true; return; }
    ch.addEventListener("click", () => ribJump(ch.dataset.target));
  });
  document.querySelectorAll("#ribArea .rib-area").forEach(b =>
    b.addEventListener("click", () => ribSetArea(b.dataset.area, true)));
  const list = document.getElementById("hotList");
  if (list) list.addEventListener("click", e => {
    const card = e.target.closest(".hot-card");
    if (card) openHot(Number(card.dataset.idx));
  });
  document.addEventListener("areachange", renderHot);

  measureRibbon();
  window.addEventListener("resize", measureRibbon);
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver(ribPickActive, { threshold: [0, 1] });
    document.querySelectorAll("#ribChips .rib-chip:not([hidden])").forEach(ch =>
      io.observe(document.getElementById(ch.dataset.target)));
  }
  // Pojistka: dlouhá karta (tabulka) protíná viewport pořád, takže observer
  // při čtení uprostřed ní nic nehlásí. Scroll s rAF je levný.
  let ticking = false;
  window.addEventListener("scroll", () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; ribPickActive(); });
  }, { passive: true });

  let saved = "";
  try { saved = localStorage.getItem(RIB_AREA_KEY) || ""; } catch (e) {}
  // Vždy jedna událost na startu: posluchači (tabulky, mapa) se tak dozví
  // uloženou oblast, i když se zaregistrovali až po vykreslení.
  ribSetArea(saved, false);
  ribPickActive();
}
""".replace("__RIB_AREA_LABELS__", labels)


def preview_html(hot, sections=None):
    """Samostatná stránka pro kouřový test ribbonu mimo scrape.py.

    Obsahuje minimální náhrady globálních funkcí dashboardu (escapeHtml,
    fmtCzk, openModal, PLACEHOLDER, ALL) -- ty jsou schválně zkopírované
    ve stejné podobě jako v scrape.py, ať se test chová jako stránka."""
    sections = sections or [("dealsCard", "Nejlepší"), ("garageCard", "Garáže"),
                            ("historyCard", "Historie")]
    hot_json = json.dumps(hot, ensure_ascii=False).replace("<", "\\u003c") \
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    cards = "".join(
        f'<div class="card" id="{html.escape(cid, quote=True)}" style="height:900px">'
        f'<h2>{html.escape(label)}</h2></div>'
        for cid, label in sections
    )
    return f"""<!DOCTYPE html>
<html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ribbon preview</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; background: #0f1115; color: #e6e6e6; }}
  header {{ padding: 16px; background: #161922; position: sticky; top: 0; z-index: 5; }}
  .card {{ margin: 12px; padding: 14px; background: #1b1f29; border-radius: 10px; }}
  .hint {{ font-size: 0.7rem; color: #888; }}
{ribbon_css()}
</style></head><body>
<header><h1>Preview</h1></header>
{ribbon_html(sections)}
{cards}
<script>
const HOT = {hot_json};
const ALL = [];
const PLACEHOLDER = "data:image/svg+xml;utf8,";
function escapeHtml(s) {{
  return (s || "").replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]));
}}
function fmtCzk(v) {{ return v === null || v === undefined ? "—" : v.toLocaleString("cs-CZ") + " Kč"; }}
function openModal(id) {{ console.log("openModal", id); }}
document.documentElement.style.setProperty("--hdr", document.querySelector("header").offsetHeight + "px");
{ribbon_js()}
initRibbon();
</script></body></html>
"""
