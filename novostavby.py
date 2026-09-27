"""Novostavby 4+kk / 5+kk kolem U Kříže (Jinonice, park Waltrovka).

Radim (27. 9. 2026): „přidej trackování na Sreality u nových bytů u parku
Waltrovka Jinonice a u Kříže, novostavby 4+kk, 5+kk, když se prodávají a
pronajímají — za kolik a jak dlouho trvá, než zmizí. Nová sekce. Ještě ulice
Kohoutových a Bochovská, a Na Pomezí. Cca u Kříže poloměr kilometr, aby tam
vše spadalo. A abych v appce viděl přesně ten okruh a mohl si ho změnit."

Samostatná kolekce po vzoru garáží, ne rozšíření `comparables`: 4+kk a 5+kk
v DISPOSITION_CODES nejsou, a kdyby se sem dostaly, vstoupily by do mediánů
bytů, do poolu i do odhadu nájmu. Vlastní klíč ve snapshotu (`novostavby`),
vlastní karta, vlastní alert.

Rozdělení práce:
  * scrape.py sbírá (hledání po čtvrtích, detail jednou na inzerát, ověření
    zmizení přes 404) -- potřebuje jeho HTTP vrstvu s retry.
  * tady je všechno ostatní a je to čisté: sloučení s minulým během, baseline,
    dny na trhu, statistika, text alertu, karta a JS. Testovatelné bez sítě.

Modul záměrně neimportuje scrape.py (scrape.py importuje jeho).

Sbírá se NADMNOŽINA (SUPERSET_KM kolem U Kříže); kruh na stránce je jen
filtr zobrazení, který si Radim táhne a mění. Alerty ale chodí pro pevný
výchozí kruh -- server localStorage prohlížeče nevidí.
"""
import html
import math
import statistics
import sys
import unicodedata
from datetime import datetime

# --- Kde --------------------------------------------------------------------- #
# Geokódováno 27. 9. 2026 přes Nominatim (OSM), včetně geometrie ulic, aby šlo
# říct, jak daleko je NEJVZDÁLENĚJŠÍ bod každého místa, ne jen jeho střed.
# Vzdálenosti od středu (U Kříže):
#   U Kříže (celá ulice)            0.00–0.20 km
#   Kohoutových                     0.16 (nejdál 0.24)
#   Bochovská                       0.19 (nejdál 0.30)
#   Na Pomezí                       0.47 (nejdál 0.89 -- severní konec v Košířích)
#   Park Waltrovka                  0.76 (nejdál 0.87)
#   Waltrovka (čtvrť, OSM bod)      0.62
#   Nová Waltrovka (nová zástavba)  1.09   <- jediné, co se do 1 km nevejde
# Radim řekl „cca kilometr, aby tam vše spadalo" -- kilometr by uřízl Novou
# Waltrovku, tedy právě tu novou zástavbu u parku. Výchozí poloměr je proto
# 1,2 km: pokryje ji i s rezervou ~100 m na půdorys domů (GPS inzerátu je dům,
# ne bod čtvrti). Víc ne -- ve 1,2 km už leží náměstí Augustina Bubníka.
CENTER = (50.0541, 14.3674)           # U Kříže, úsek Waltrovka/Jinonice
CENTER_LABEL = "U Kříže"
DEFAULT_RADIUS_KM = 1.2
# Alerty chodí pro tenhle kruh a jen pro něj. Jedna konstanta vedle výchozího
# kruhu, ať se nerozjedou: posuvník na stránce alerty nemění.
ALERT_CENTER = CENTER
ALERT_RADIUS_KM = DEFAULT_RADIUS_KM
# Co se sbírá: všechno do 2 km. Posuvník na stránce jde do tohohle maxima;
# za přerušovanou hranicí data nejsou. 2 km stojí 14 stránek hledání za běh
# (7 čtvrtí × prodej/pronájem, v každé ≤ 22 výsledků) -- levné.
SUPERSET_KM = 2.0
# Čtvrti, kterých se 2km kruh dotýká. Ověřeno 27. 9. proti Sreality: všech sedm
# jmen se rozpozná jako „městská část …" (localityEntityType ward). Smíchov a
# Motol dnes do 2 km nepřispívají ničím, ale kruh jejich okraje protíná a
# stojí dohromady 4 requesty.
WARDS = ["Jinonice", "Radlice", "Košíře", "Stodůlky", "Hlubočepy", "Smíchov", "Motol"]
TRANSACTIONS = ("prodej", "pronajem")

# --- Co ---------------------------------------------------------------------- #
# categorySubCb kódy Sreality, ověřené živě 27. 9. (estatesFilterPage):
#   8 = 4+kk, 9 = 4+1, 10 = 5+kk, 11 = 5+1, 12 = 6 a více, 16 = atypický.
# Dispozice se bere VÝHRADNĚ z kódu, nikdy z názvu (Sreality občas odpoví
# anglicky a „4+kk" pak přijde jako „4+kt").
DISPOSITIONS = {8: "4+kk", 10: "5+kk"}
VELIKOST = "4+kk,5+kk"
# Stav objektu „Novostavba" = buildingCondition 6 (stejný kód jako
# NEW_BUILDING_CODE v scrape.py). Filtr jde na server: URL parametr `stav`
# s hodnotou „novostavby" (codebook z JS bundlu Sreality; „novostavba" i
# „stav-objektu" server tiše ignoruje -- ověřeno, dávají plný výsledek).
# Že filtr opravdu platí, je vidět v queryKey odpovědi: buildingCondition [6],
# a Jinonice 4+kk/5+kk na prodej spadnou ze 14 na 9.
CONDITION_CODE = 6
STAV_SLUG = "novostavby"

# Pojmenovaná místa -- špendlíky na mapě a vlajka u řádku tabulky.
LANDMARKS = [
    {"name": "U Kříže", "lat": 50.0541, "lon": 14.3674},
    {"name": "Kohoutových", "lat": 50.05350, "lon": 14.36530},
    {"name": "Bochovská", "lat": 50.05273, "lon": 14.36587},
    {"name": "Na Pomezí", "lat": 50.05825, "lon": 14.36592},
    {"name": "Park Waltrovka", "lat": 50.05756, "lon": 14.37658},
    {"name": "Nová Waltrovka", "lat": 50.05720, "lon": 14.38183},
]
# Ulice se porovnávají bez diakritiky a velikosti písmen: Sreality píše
# „U kříže" i „Na pomezí".
NAMED_STREETS = ("U Kříže", "Kohoutových", "Bochovská", "Na Pomezí")
# Waltrovka nemá jednu ulici; inzerát do 300 m od parku / Nové Waltrovky nebo
# s „Waltrovka" v popisu (např. „Rezidence Waltrovka", Kačírkova) se označí.
WALTROVKA_POINTS = [(50.05756, 14.37658), (50.05720, 14.38183), (50.05742, 14.37434)]
WALTROVKA_NEAR_KM = 0.3

MAX_ALERT_LINES = 25


# --- Pomocné ----------------------------------------------------------------- #
def haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    la1, lo1, la2, lo2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(a))


def km_from_center(lat, lon):
    d = haversine_km(lat, lon, CENTER[0], CENTER[1])
    return round(d, 2) if d is not None else None


def in_superset(rec):
    km = rec.get("km")
    return km is not None and km <= SUPERSET_KM


def _fold(s):
    """Malá písmena bez diakritiky -- „U kříže" == „U Kříže"."""
    s =unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


_NAMED = {_fold(s): s for s in NAMED_STREETS}


def named_place(rec):
    """Které z Radimových míst inzerát je, nebo None."""
    street = _NAMED.get(_fold(rec.get("street")))
    if street:
        return street
    if rec.get("mentions_waltrovka"):
        return "Waltrovka"
    lat, lon = rec.get("lat"), rec.get("lon")
    for p in WALTROVKA_POINTS:
        d = haversine_km(lat, lon, p[0], p[1])
        if d is not None and d <= WALTROVKA_NEAR_KM:
            return "Waltrovka"
    return None


def fingerprint():
    """Tvar toho, CO se sbírá. Změní-li se (jiné čtvrti, širší nadmnožina,
    další dispozice), běh proběhne jako tichá baseline: nové záznamy dostanou
    `baseline: true` a žádný alert neodejde -- naše změna konfigurace se nesmí
    hlásit jako pohyb trhu. Výchozí/alertový poloměr sem nepatří: nemění, co
    se sbírá, jen co se zobrazuje."""
    return {
        "center": list(CENTER),
        "superset_km": SUPERSET_KM,
        "dispositions": sorted(DISPOSITIONS.values()),
        "condition": CONDITION_CODE,
        "wards": sorted(WARDS),
        "transactions": sorted(TRANSACTIONS),
    }


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def days_between(a, b):
    """Celé dny, zaokrouhlené stejně jako daysBetween() v JS (Math.round)."""
    t1, t2 = parse_iso(a), parse_iso(b)
    if t1 is None or t2 is None:
        return None
    return max(0, math.floor((t2 - t1).total_seconds() / 86400 + 0.5))


def is_live(rec):
    return not rec.get("gone_at") and not rec.get("left_filter_at")


def days_on_market(rec, now):
    """{"days", "lower_bound", "live"}.

    first_seen → gone_at u zmizelého, first_seen → teď u živého. U záznamu
    z baseline (byl v nabídce už při prvním běhu) nevíme, kdy se objevil, a
    číslo je jen dolní mez -- stránka ho píše jako „≥ N dní"."""
    live = is_live(rec)
    end = rec.get("gone_at") if rec.get("gone_at") else now
    return {
        "days": days_between(rec.get("first_seen"), end),
        "lower_bound": bool(rec.get("baseline")),
        "live": live,
    }


# --- Sloučení s minulým během ------------------------------------------------ #
# Pole, která přišla z detailu (čte se jednou) a nesou se dál beze změny.
DETAIL_FIELDS = ("since", "detail_area_sqm", "mentions_waltrovka", "address_exact",
                 "building_condition", "detail_read")


def merge(prev_records, seen, now, *, baseline, verify=None, max_checks=30):
    """Sloučí záznamy z minulého běhu s tím, co našlo hledání teď.

    `seen`     -- záznamy z hledání (už odfiltrované na nadmnožinu).
    `baseline` -- tichý běh (první, nebo po změně konfigurace): nové záznamy
                  dostanou baseline: true a žádné události nevzniknou.
    `verify`   -- funkce(rec) -> "gone" | "live" | "left_filter" | "unknown",
                  ptá se detailu inzerátu. Zmizelý je jen „gone" (= 404).
                  Absence ve výsledcích hledání sama nic neznamená.

    Vrací (records, events). Události: {"kind": "new"|"gone"|"price", "rec",
    "old_price"?}."""
    prev_by_id = {str(r["id"]): r for r in (prev_records or [])}
    seen_ids = set()
    out, events = [], []

    for rec in seen:
        rid = str(rec["id"])
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        prev = prev_by_id.get(rid)
        rec = dict(rec)
        if prev:
            rec["first_seen"] = prev.get("first_seen") or now
            rec["baseline"] = bool(prev.get("baseline"))
            history = list(prev.get("price_history") or [])
            for k in DETAIL_FIELDS:
                if rec.get(k) is None and prev.get(k) is not None:
                    rec[k] = prev[k]
            old = prev.get("price_czk")
            new = rec.get("price_czk")
            if new != old and new:
                history.append({"at": now, "price_czk": new})
                rec["price_old_czk"] = old
                # Zdražení/zlevnění živého inzerátu je zpráva; návrat zmizelého
                # (404 a pak zase ve výsledcích) ne -- ten se jen zaznamená.
                if old and is_live(prev) and not baseline:
                    events.append({"kind": "price", "rec": rec, "old_price": old})
            else:
                rec["price_old_czk"] = prev.get("price_old_czk")
            rec["price_history"] = history
            if prev.get("gone_at"):
                rec["returned_at"] = now
                rec["gone_before_at"] = prev["gone_at"]
        else:
            rec["first_seen"] = now
            rec["baseline"] = bool(baseline)
            rec["price_history"] = [{"at": now, "price_czk": rec.get("price_czk")}] \
                if rec.get("price_czk") else []
            if not baseline:
                events.append({"kind": "new", "rec": rec})
        rec["last_seen"] = now
        rec["gone_at"] = None
        rec.pop("missing_from_search", None)
        rec.pop("left_filter_at", None)
        rec.pop("out_of_scope", None)
        out.append(rec)

    checks = 0
    for rid, prev in prev_by_id.items():
        if rid in seen_ids:
            continue
        rec = dict(prev)
        if rec.get("gone_at") or rec.get("left_filter_at"):
            out.append(rec)      # už rozhodnuto, nic se neověřuje znovu
            continue
        if not in_superset(rec):
            # Mimo to, co teď sbíráme (zúžená konfigurace). Nemaže se a
            # neprohlašuje za zmizelé -- jen se přestane sledovat.
            rec["out_of_scope"] = True
            out.append(rec)
            continue
        verdict = "unknown"
        if verify is not None and checks < max_checks:
            checks += 1
            try:
                verdict = verify(rec)
            except Exception as exc:  # noqa: BLE001 -- nejistota = nechat živý
                print(f"  novostavba {rid}: ověření selhalo ({exc})", file=sys.stderr)
                verdict = "unknown"
        if verdict == "gone":
            rec["gone_at"] = now
            rec.pop("missing_from_search", None)
            rec["days_on_market"] = days_on_market(rec, now)["days"]
            if not baseline:
                events.append({"kind": "gone", "rec": rec})
        elif verdict == "left_filter":
            # Inzerát žije, ale už není novostavba 4+kk/5+kk (prodejce změnil
            # stav nebo dispozici). Není to zmizení a do dní na trhu nepatří.
            rec["left_filter_at"] = now
            rec.pop("missing_from_search", None)
        else:
            rec["missing_from_search"] = rec.get("missing_from_search") or now
        out.append(rec)

    for rec in out:
        if rec.get("gone_at"):
            rec["days_on_market"] = days_on_market(rec, now)["days"]
        else:
            rec.pop("days_on_market", None)
        rec["named_place"] = named_place(rec)
    out.sort(key=lambda r: (r.get("gone_at") is not None, r.get("transaction_type") or "",
                            r.get("disposition") or "", r.get("km") or 0))
    return out, events


# --- Statistika -------------------------------------------------------------- #
def in_circle(rec, center, radius_km):
    d = haversine_km(rec.get("lat"), rec.get("lon"), center[0], center[1])
    return d is not None and d <= radius_km + 1e-9


def compute_stats(records, now, center=CENTER, radius_km=DEFAULT_RADIUS_KM):
    """Po transakci × dispozici, jen uvnitř kruhu. Stejný výpočet dělá JS na
    stránce pro kruh, který si Radim nastaví; tady je pro výchozí kruh (do
    snapshotu a pro testy)."""
    out = {}
    recs = [r for r in records if not r.get("out_of_scope") and in_circle(r, center, radius_km)]
    for tx in TRANSACTIONS:
        for disp in DISPOSITIONS.values():
            group = [r for r in recs if r.get("transaction_type") == tx and r.get("disposition") == disp]
            live = [r for r in group if is_live(r)]
            gone = [r for r in group if r.get("gone_at")]
            prices = sorted(r["price_czk"] for r in live if r.get("price_czk"))
            per_sqm = sorted(r["price_czk_per_sqm"] for r in live if r.get("price_czk_per_sqm"))
            gone_days = [days_on_market(r, now)["days"] for r in gone]
            gone_days = [d for d in gone_days if d is not None]
            out[f"{tx}_{disp}"] = {
                "live_n": len(live),
                "median_price_czk": round(statistics.median(prices)) if prices else None,
                "median_czk_per_sqm": round(statistics.median(per_sqm)) if per_sqm else None,
                "gone_n": len(gone),
                "median_days_to_gone": round(statistics.median(gone_days)) if gone_days else None,
                "min_days_to_gone": min(gone_days) if gone_days else None,
                "max_days_to_gone": max(gone_days) if gone_days else None,
                # Kterýkoli zmizelý z baseline dělá z mediánu dolní mez.
                "days_lower_bound": any(r.get("baseline") for r in gone),
                "gone_last_prices_czk": [r.get("price_czk") for r in
                                         sorted(gone, key=lambda r: r.get("gone_at") or "", reverse=True)
                                         if r.get("price_czk")][:5],
            }
    return out


# --- Alert ------------------------------------------------------------------- #
def _czk(v):
    if not isinstance(v, (int, float)) or not v:
        return "cena na dotaz"
    return f"{int(round(v)):,}".replace(",", " ") + " Kč"


def _km(v):
    return "? km" if v is None else f"{v:.1f}".replace(".", ",") + " km"


def _tx(rec):
    return "pronájem" if rec.get("transaction_type") == "pronajem" else "prodej"


def _safe_url(u):
    return u if isinstance(u, str) and u.startswith("https://") else None


def alert_events(events):
    """Jen události uvnitř výchozího (alertového) kruhu."""
    return [e for e in events if in_circle(e["rec"], ALERT_CENTER, ALERT_RADIUS_KM)]


def build_alert(events, now, dashboard_url=None):
    """Jedna seskupená zpráva (Telegram HTML), nebo None, když není co hlásit.

    Každý scrapovaný řetězec projde html.escape -- Telegram HTML je markup a
    ulice s „<" by zprávu rozbila (nebo hůř). Kontakty tu nejsou: záznam nemá
    popis ani prodejce, jen ulici, cenu a odkaz."""
    events = alert_events(events)
    if not events:
        return None
    order = {"new": 0, "price": 1, "gone": 2}
    events = sorted(events, key=lambda e: (order[e["kind"]], e["rec"].get("km") or 0))
    radius = f"{ALERT_RADIUS_KM:.1f}".replace(".", ",")
    lines = [f"<b>🏗️ Novostavby 4+kk / 5+kk · {CENTER_LABEL} ≤ {radius} km</b>"]
    for e in events[:MAX_ALERT_LINES]:
        r = e["rec"]
        unit = "/měs" if r.get("transaction_type") == "pronajem" else ""
        where = html.escape(r.get("street") or r.get("city_part") or "?")
        if r.get("named_place"):
            same = _fold(r["named_place"]) == _fold(r.get("street"))
            where += " ⭐" if same else f" ⭐{html.escape(r['named_place'])}"
        if e["kind"] == "price":
            price = f"{_czk(e.get('old_price'))} → {_czk(r.get('price_czk'))}{unit}"
            icon = "💰"
        elif e["kind"] == "gone":
            price = f"naposledy {_czk(r.get('price_czk'))}{unit}"
            icon = "❌"
        else:
            price = f"{_czk(r.get('price_czk'))}{unit}"
            icon = "🆕"
        parts = [f"{icon} {html.escape(r.get('disposition') or '?')} {_tx(r)}", where, price]
        if r.get("price_czk_per_sqm"):
            parts.append(f"{_czk(r['price_czk_per_sqm'])}/m²")
        parts.append(_km(r.get("km")))
        if e["kind"] == "gone":
            dom = days_on_market(r, now)
            if dom["days"] is not None:
                parts.append(f"na trhu {'≥ ' if dom['lower_bound'] else ''}{dom['days']} d")
        url = _safe_url(r.get("url"))
        line = " · ".join(parts)
        if url:
            line += f' · <a href="{html.escape(url, quote=True)}">odkaz</a>'
        lines.append(line)
    if len(events) > MAX_ALERT_LINES:
        lines.append(f"… a dalších {len(events) - MAX_ALERT_LINES}")
    if dashboard_url:
        lines.append(f'<a href="{html.escape(dashboard_url, quote=True)}">dashboard</a>')
    return "\n".join(lines)


def send_alert(events, now, *, dry_run=False, dashboard_url=None):
    """Pošle alert, když je co. Nikdy nevyhodí výjimku: alert je doplněk a
    jeho selhání nesmí shodit běh. Vrací kanál / "dry-run" / None."""
    try:
        text = build_alert(events, now, dashboard_url)
        if not text:
            return None
        import notify
        return notify.send_text(text, dry_run=dry_run, what="Alert novostaveb")
    except Exception as exc:  # noqa: BLE001 -- deliberate: never fail the run
        print(f"::warning::alert novostaveb se neodeslal: {exc}", file=sys.stderr)
        return None


# --- Stránka ----------------------------------------------------------------- #
PAGE_FIELDS = (
    "id", "title", "disposition", "transaction_type", "price_czk", "price_old_czk",
    "price_czk_per_sqm", "floor_area_sqm", "street", "city_part", "locality", "lat", "lon",
    "km", "url", "thumb", "first_seen", "last_seen", "gone_at", "baseline", "price_history",
    "since", "named_place", "missing_from_search", "left_filter_at", "returned_at",
)


def page_payload(records, generated_at):
    recs = []
    for r in records or []:
        if r.get("out_of_scope"):
            continue
        rec = {k: r[k] for k in PAGE_FIELDS if r.get(k) is not None}
        if not _safe_url(rec.get("url")):
            rec.pop("url", None)
        recs.append(rec)
    return {
        "records": recs,
        "center": list(CENTER),
        "center_label": CENTER_LABEL,
        "default_radius_km": DEFAULT_RADIUS_KM,
        "alert_radius_km": ALERT_RADIUS_KM,
        "superset_km": SUPERSET_KM,
        "landmarks": LANDMARKS,
        "generated_at": generated_at,
    }


def card_html(records, baseline_at=None):
    """Kostra karty; tabulky, statistiky a mapu kreslí JS (page_js), protože se
    přepočítávají z kruhu, který si Radim nastaví. None = kolekce ještě nikdy
    neběžela -> žádná karta (čip v ribbonu se pak sám schová)."""
    if records is None:
        return ""
    r_def = f"{DEFAULT_RADIUS_KM:.1f}".replace(".", ",")
    r_alert = f"{ALERT_RADIUS_KM:.1f}".replace(".", ",")
    r_sup = f"{SUPERSET_KM:.1f}".replace(".", ",")
    t = parse_iso(baseline_at)
    since = f" ({t.day}. {t.month}. {t.year})" if t is not None else ""
    return f"""<div class="card" id="novCard">
  <h2 style="margin-top:0;font-size:1rem;">🏗️ Novostavby 4+kk / 5+kk — {html.escape(CENTER_LABEL)}</h2>
  <p class="hint" style="margin:0 0 8px;">Byty 4+kk a 5+kk ve stavu <b>novostavba</b> (dle Sreality), prodej i pronájem.
    Sbírá se vše do <b>{r_sup} km</b> od {html.escape(CENTER_LABEL)} (přerušovaná hranice). Kruh je
    <b>jen filtr zobrazení</b> — táhni středem ✚ (nebo klepni do mapy) a posuvníkem měň poloměr;
    statistika i tabulka se přepočítají. Alerty chodí pro výchozí okruh {r_alert} km, posuvník je nemění.</p>
  <div class="nov-ctl">
    <label class="nov-r">Poloměr <input type="range" id="novR" min="0.2" max="{SUPERSET_KM}" step="0.05">
      <b id="novRv"></b></label>
    <button type="button" class="popup-btn" id="novReset">Reset na výchozí ({r_def} km)</button>
    <select id="novTx">
      <option value="">Prodej i pronájem</option>
      <option value="prodej">Jen prodej</option>
      <option value="pronajem">Jen pronájem</option>
    </select>
  </div>
  <div id="novWarn" class="modal-note" hidden></div>
  <div id="novMap" class="gmap"></div>
  <div class="hint">🟠 prodej · 🔵 pronájem · ⚪ zmizelo · ✚ střed kruhu · 🟣 Radimova místa ·
    bledé = mimo kruh.</div>
  <div class="est-scroll" style="margin-top:10px;"><table class="est-table" id="novStats"></table></div>
  <p class="hint">„Do zmizení" = od prvního výskytu do ověřeného zmizení (detail na Sreality vrací 404;
    kontrola každé ~4 h). <b>≥</b> = inzerát byl v nabídce už při prvním běhu sledování{since}
    (nebo když jsme rozšířili sběr), skutečné stáří neznáme — číslo je dolní mez. Zmizení neznamená prodej.</p>
  <div class="scroll" style="margin-top:8px;">
  <table id="tblNov">
    <thead><tr><th>Dispozice</th><th>Transakce</th><th>Ulice</th><th>m²</th><th>Cena</th><th>Kč/m²</th>
      <th title="Vzdušná vzdálenost od {html.escape(CENTER_LABEL)}">km od {html.escape(CENTER_LABEL)}</th>
      <th>Na trhu</th><th>Stav</th><th></th></tr></thead>
    <tbody></tbody>
  </table>
  </div>
  <p class="hint">⭐ = ulice U Kříže, Kohoutových, Bochovská, Na Pomezí nebo okolí parku Waltrovka.</p>
</div>"""


CSS = """
  .nov-ctl { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 0 0 8px; }
  .nov-ctl .nov-r { display: flex; gap: 6px; align-items: center; font-size: 0.8rem; flex: 1 1 220px; }
  .nov-ctl .nov-r input { flex: 1; min-width: 100px; padding: 0; }
  .nov-lbl { background: rgba(20,22,30,.85); color: #e6d6ff; border: 1px solid #6b4fa0;
             font-size: 0.66rem; padding: 0 4px; box-shadow: none; }
  .nov-lbl::before { display: none; }
  .nov-center { color: #7CFFB2; font-size: 20px; line-height: 22px; text-align: center;
                font-weight: 700; text-shadow: 0 0 3px #000; cursor: move; }
  tr.nov-out td { opacity: 0.45; }
  .nov-star { color: #fc6; }
"""


def page_js(payload_json):
    """JS karty. Vkládá se za hlavní šablonu (potřebuje escapeHtml, fmtCzk,
    fmtDay, daysBetween, safeImg, PLACEHOLDER a Leaflet `L`). `payload_json`
    musí už být escapovaný pro <script> (scrape.script_json)."""
    return r"""
// ---- Novostavby 4+kk / 5+kk kolem U Kříže -----------------------------------
// Kruh je filtr zobrazení; data jsou nadmnožina do NOV.superset_km. Všechno
// scrapované jde přes escapeHtml, URL jen https, do atributů žádná id.
(function () {
  const NOV = __NOV_JSON__;
  const card = document.getElementById("novCard");
  if (!card || !NOV) return;
  const KEY = "novCircle:v1";
  const DEF = { lat: NOV.center[0], lon: NOV.center[1], r: NOV.default_radius_km };
  const TXL = { prodej: "prodej", pronajem: "pronájem" };
  const DISPS = ["4+kk", "5+kk"];

  function hav(a1, o1, a2, o2) {
    const R = 6371, rad = Math.PI / 180;
    const x = Math.sin((a2 - a1) * rad / 2) ** 2 +
      Math.cos(a1 * rad) * Math.cos(a2 * rad) * Math.sin((o2 - o1) * rad / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(x));
  }
  function load() {
    try {
      const s = JSON.parse(localStorage.getItem(KEY) || "null");
      if (s && [s.lat, s.lon, s.r].every(v => typeof v === "number" && isFinite(v)) &&
          s.r >= 0.2 && s.r <= NOV.superset_km && hav(s.lat, s.lon, DEF.lat, DEF.lon) < 10) {
        return { lat: s.lat, lon: s.lon, r: s.r };
      }
    } catch (e) {}
    return { ...DEF };
  }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(circ)); } catch (e) {} }
  let circ = load();

  const num = v => (typeof v === "number" && isFinite(v)) ? v : null;
  const kmTxt = v => num(v) == null ? "—" : v.toFixed(2).replace(".", ",");
  const safeUrl = u => (typeof u === "string" && /^https:\/\//.test(u)) ? u : "";
  const live = r => !r.gone_at && !r.left_filter_at;
  function inCircle(r) {
    const la = num(r.lat), lo = num(r.lon);
    return la != null && lo != null && hav(circ.lat, circ.lon, la, lo) <= circ.r + 1e-9;
  }
  function median(a) {
    if (!a.length) return null;
    const s = [...a].sort((x, y) => x - y), m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }
  function dom(r) {
    const d = daysBetween(r.first_seen, r.gone_at || null);
    return { d, lb: !!r.baseline };
  }
  function txSel() { return document.getElementById("novTx").value; }
  function unit(r) { return r.transaction_type === "pronajem" ? "/měs" : ""; }

  function renderWarn() {
    const el = document.getElementById("novWarn");
    const reach = hav(circ.lat, circ.lon, DEF.lat, DEF.lon) + circ.r;
    if (reach > NOV.superset_km + 0.005) {
      el.textContent = `Kruh přesahuje sbíranou oblast. Data se sbírají jen do ${String(NOV.superset_km).replace(".", ",")} km ` +
        `od ${NOV.center_label} (přerušovaná hranice) — co leží za ní, tu chybí, i když by to na Sreality bylo.`;
      el.hidden = false;
    } else {
      el.hidden = true;
    }
    document.getElementById("novRv").textContent = circ.r.toFixed(2).replace(".", ",") + " km";
  }

  function renderStats() {
    const tx = txSel();
    const recs = NOV.records.filter(inCircle);
    const rows = [];
    for (const t of ["prodej", "pronajem"]) {
      if (tx && tx !== t) continue;
      for (const disp of DISPS) {
        const g = recs.filter(r => r.transaction_type === t && r.disposition === disp);
        const lv = g.filter(live), gone = g.filter(r => r.gone_at);
        const pr = lv.map(r => num(r.price_czk)).filter(v => v);
        const psm = lv.map(r => num(r.price_czk_per_sqm)).filter(v => v);
        const gd = gone.map(r => dom(r).d).filter(v => v != null);
        const lb = gone.some(r => r.baseline) ? "≥ " : "";
        const md = median(gd);
        const last = [...gone].sort((a, b) => String(b.gone_at).localeCompare(String(a.gone_at)))
          .map(r => num(r.price_czk)).filter(v => v).slice(0, 4);
        const u = t === "pronajem" ? "/měs" : "";
        rows.push(`<tr><td>${TXL[t]} ${escapeHtml(disp)}</td><td>${lv.length}</td>
          <td>${pr.length ? fmtCzk(Math.round(median(pr))) + u : "—"}</td>
          <td>${psm.length ? fmtCzk(Math.round(median(psm))) : "—"}</td>
          <td>${gone.length}</td>
          <td>${md != null ? `${lb}${Math.round(md)} d <span class="hint">(${lb}${Math.min(...gd)}–${Math.max(...gd)})</span>` : "—"}</td>
          <td>${last.length ? last.map(v => fmtCzk(v) + u).join(", ") : "—"}</td></tr>`);
      }
    }
    document.getElementById("novStats").innerHTML =
      `<thead><tr><th></th><th>Živé</th><th>Medián ceny</th><th>Medián Kč/m²</th><th>Zmizelo</th>` +
      `<th>Do zmizení (medián, rozsah)</th><th>Poslední ceny zmizelých</th></tr></thead><tbody>${rows.join("")}</tbody>`;
  }

  function stateTxt(r) {
    if (r.gone_at) return `<span class="deal-bad">zmizel ${fmtDay(r.gone_at)}</span>`;
    if (r.left_filter_at) return `<span class="hint">už není novostavba 4+kk/5+kk (${fmtDay(r.left_filter_at)})</span>`;
    const miss = r.missing_from_search ? ` <span class="hint" title="Ve výsledcích hledání chybí, ale detail inzerátu žije">(mimo hledání)</span>` : "";
    return `<span class="deal-good">živý</span>${miss}`;
  }
  function priceTxt(r) {
    const moved = num(r.price_old_czk) && r.price_old_czk !== r.price_czk
      ? ` <span class="hint" title="Předchozí cena">(dřív ${fmtCzk(r.price_old_czk)})</span>` : "";
    return (num(r.price_czk) ? fmtCzk(r.price_czk) + unit(r) : "na dotaz") + moved;
  }
  function renderTable() {
    const tx = txSel();
    const recs = NOV.records.filter(r => (!tx || r.transaction_type === tx));
    recs.sort((a, b) => (live(b) - live(a)) || (inCircle(b) - inCircle(a)) ||
      String(a.transaction_type).localeCompare(String(b.transaction_type)) || ((a.km || 0) - (b.km || 0)));
    const rows = recs.map(r => {
      const d = dom(r);
      const inside = inCircle(r);
      const days = d.d == null ? "—" : `${d.lb ? "≥ " : ""}${d.d} d`;
      const since = r.since ? `<div class="hint">na Sreality od ${fmtDay(r.since)}</div>` : "";
      const star = r.named_place ? ` <span class="nov-star" title="${escapeHtml(r.named_place)}">⭐</span>` : "";
      const url = safeUrl(r.url);
      return `<tr class="${inside ? "" : "nov-out"}">
        <td>${escapeHtml(r.disposition || "")}</td>
        <td>${TXL[r.transaction_type] || ""}</td>
        <td>${escapeHtml(r.street || r.city_part || "—")}${star}<div class="hint">${escapeHtml(r.city_part || "")}</div></td>
        <td>${numTxt(r.floor_area_sqm)}</td>
        <td>${priceTxt(r)}</td>
        <td>${num(r.price_czk_per_sqm) ? fmtCzk(r.price_czk_per_sqm) : "—"}</td>
        <td>${kmTxt(r.km)}${inside ? "" : ' <span class="hint">mimo kruh</span>'}</td>
        <td>${days}${since}</td>
        <td>${stateTxt(r)}</td>
        <td>${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">↗</a>` : ""}</td>
      </tr>`;
    });
    const tb = document.querySelector("#tblNov tbody");
    tb.innerHTML = rows.length ? rows.join("")
      : `<tr><td colspan="10" class="hint">Zatím žádná novostavba 4+kk / 5+kk v nabídce.</td></tr>`;
  }

  let NM = null, circleL = null, centerM = null, markL = null;
  function color(r) {
    if (!live(r)) return "#9aa0aa";
    return r.transaction_type === "pronajem" ? "#7ab8ff" : "#ff9a4d";
  }
  function drawMarkers() {
    if (!NM) return;
    markL.clearLayers();
    const tx = txSel();
    for (const r of NOV.records) {
      if (tx && r.transaction_type !== tx) continue;
      const la = num(r.lat), lo = num(r.lon);
      if (la == null || lo == null) continue;
      const inside = inCircle(r), lv = live(r), c = color(r);
      const m = L.circleMarker([la, lo], {
        radius: lv ? 7 : 5, color: c, weight: 2, dashArray: lv ? null : "3,3",
        fillColor: c, fillOpacity: lv ? (inside ? 0.6 : 0.15) : 0.1, opacity: inside ? 1 : 0.35,
      });
      const url = safeUrl(r.url);
      m.bindPopup(`<div style="min-width:150px;">
        <img class="popup-thumb" src="${escapeHtml(safeImg(r.thumb))}" onerror="this.src=PLACEHOLDER">
        <div style="font-weight:600;font-size:0.85rem;">${escapeHtml(r.disposition || "")} · ${TXL[r.transaction_type] || ""}</div>
        <div style="font-size:0.8rem;">${escapeHtml(r.street || "")} · ${priceTxt(r)}</div>
        <div style="font-size:0.75rem;">${stateTxt(r)} · ${kmTxt(r.km)} km</div>
        ${url ? `<a class="popup-btn" href="${escapeHtml(url)}" target="_blank" rel="noopener">Sreality ↗</a>` : ""}</div>`);
      m.addTo(markL);
    }
  }
  function moveCircle(fit) {
    if (!NM) return;
    circleL.setLatLng([circ.lat, circ.lon]).setRadius(circ.r * 1000);
    centerM.setLatLng([circ.lat, circ.lon]);
    // Hranice z geometrie, ne circleL.getBounds(): to potřebuje už
    // promítnutou vrstvu a na mapě bez pohledu (první volání) spadne.
    if (fit) NM.fitBounds(L.latLng(circ.lat, circ.lon).toBounds(circ.r * 2000), { padding: [10, 10] });
  }
  function initMap() {
    const el = document.getElementById("novMap");
    if (!el || typeof L === "undefined") return;
    NM = L.map("novMap").setView([circ.lat, circ.lon], 14);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
    }).addTo(NM);
    // Sbíraná nadmnožina: pevná, přerušovaná, bez výplně a bez kliku.
    L.circle(NOV.center, { radius: NOV.superset_km * 1000, color: "#888", weight: 1.5,
      dashArray: "6,6", fill: false, interactive: false }).addTo(NM);
    circleL = L.circle([circ.lat, circ.lon], { radius: circ.r * 1000, color: "#7CFFB2",
      weight: 2, fillOpacity: 0.05, interactive: false }).addTo(NM);
    markL = L.layerGroup().addTo(NM);
    for (const p of NOV.landmarks || []) {
      L.circleMarker([p.lat, p.lon], { radius: 4, color: "#b48cff", weight: 2,
        fillColor: "#b48cff", fillOpacity: 0.9, interactive: false })
        .bindTooltip(escapeHtml(p.name), { permanent: true, direction: "right", className: "nov-lbl", offset: [6, 0] })
        .addTo(NM);
    }
    centerM = L.marker([circ.lat, circ.lon], {
      draggable: true, zIndexOffset: 1000, title: "Střed kruhu — táhni",
      icon: L.divIcon({ className: "nov-center", html: "✚", iconSize: [22, 22], iconAnchor: [11, 11] }),
    }).addTo(NM);
    centerM.on("drag", e => {
      const ll = e.target.getLatLng();
      circ.lat = ll.lat; circ.lon = ll.lng;
      circleL.setLatLng(ll);
    });
    centerM.on("dragend", () => { save(); refresh(false); });
    NM.on("click", e => { circ.lat = e.latlng.lat; circ.lon = e.latlng.lng; save(); refresh(false); });
    moveCircle(true);
    drawMarkers();
  }
  function refresh(fit) {
    renderWarn();
    renderStats();
    renderTable();
    // Mapa je doplněk tabulky: když selže (Leaflet se nenačetl, divný
    // prohlížeč), čísla a řádky už stojí.
    try { moveCircle(fit); drawMarkers(); } catch (e) { console.error(e); }
  }
  const slider = document.getElementById("novR");
  slider.value = String(circ.r);
  slider.addEventListener("input", () => {
    const v = Number(slider.value);
    if (!isFinite(v)) return;
    circ.r = Math.min(NOV.superset_km, Math.max(0.2, v));
    save();
    refresh(false);
  });
  document.getElementById("novReset").addEventListener("click", () => {
    circ = { ...DEF };
    slider.value = String(circ.r);
    try { localStorage.removeItem(KEY); } catch (e) {}
    refresh(true);
  });
  document.getElementById("novTx").addEventListener("change", () => refresh(false));
  // Karta může být při načtení sbalená: Leaflet v skrytém kontejneru má nulovou
  // velikost, takže se po rozbalení musí přeměřit a znovu vycentrovat.
  window.novOnShow = () => { if (NM) { NM.invalidateSize(); moveCircle(true); } };
  refresh(false);
  try { initMap(); } catch (e) { console.error(e); NM = null; }
})();
""".replace("__NOV_JSON__", payload_json)
