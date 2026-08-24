#!/usr/bin/env python3
"""Extra comparable sources beyond Sreality: Bezrealitky and Reality.iDNES.

Each fetcher returns a list of comparable dicts in the SAME normalized schema
that scrape.py's parse_comparable produces, plus a "source" field. Ids are
namespaced strings ("bez-<id>", "idnes-<id>") so they never collide with
Sreality's numeric ids.

Robots note: Bezrealitky disallows /vyhledat* and /search* and its API, so we
only read the sitemap-listed /vypis/ locality pages (allowed) — never the
search endpoint. iDNES allows its /s/ search pages. Both are fetched politely
(browser UA, rate-limited, capped)."""
import math
import os
import re
import time

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "cs"})

ELECTRICITY_ESTIMATE_CZK = 1500  # keep in sync with scrape.py for comparable all-in Kč/m²
# Bezrealitky charges a one-time "Administrativní poplatek" (advert field `fee`,
# only on the detail page). Drop listings whose admin fee exceeds this cap.
MAX_ADMIN_FEE_CZK = 5000

# The watched area, the disposition set and the fee parser are all owned by
# scrape.py and pushed in by configure() at the start of a run. They live there
# rather than here so there is exactly one definition of "the area we watch" and
# one implementation of fee parsing, shared by every source.
AREA_CENTER = (50.0995, 14.4900)
AREA_RADIUS_KM = 3.0
TARGET_DISPOSITIONS = {"1+kk", "1+1", "2+kk", "2+1", "3+kk", "3+1"}
_FEE_PARSER = None   # (cost_of_living_raw, description, rent) -> (fee, source, electricity)
_COST_FN = None      # (price, fee, tx, electricity, fee_source) -> cost tuple
_STREET_GPS = {}     # lowercased street name -> (lat, lon), from the Sreality sweep
_PREV_BY_ID = {}     # last run's comparables, so unchanged adverts skip their detail fetch
_PARSER_VERSION = None  # scrape.PARSER_VERSION; a bump invalidates cached fee data
MAX_DETAIL_FETCHES = int(os.environ.get("MAX_SOURCE_DETAIL_FETCHES", "200"))  # per source, per run

# iDNES has no per-listing GPS anywhere on the card or the detail page, so it is
# filtered by the ward label its cards carry ("Kolmá, Praha 9 - Vysočany").
# These are the wards the watched circle overlaps, and the district search paths
# they live under.
IDNES_WARDS = {"Vysočany", "Hrdlořezy", "Libeň", "Karlín", "Žižkov", "Malešice"}
# Wards that lie wholly (or near enough) inside the circle, so an advert there
# can be kept even when its street can't be placed. Žižkov and Malešice are
# deliberately absent: both run far past the boundary, and Radim asked not to
# drift outward.
IDNES_CORE_WARDS = {"Vysočany", "Hrdlořezy", "Karlín"}
IDNES_DISTRICTS = ("praha-9", "praha-8", "praha-3")


def configure(center, radius_km, dispositions, fee_parser, cost_fn,
              street_gps=None, prev_comparables=None, prev_fold_cache=None,
              parser_version=None):
    global AREA_CENTER, AREA_RADIUS_KM, TARGET_DISPOSITIONS, _FEE_PARSER, _COST_FN
    global _STREET_GPS, _PREV_BY_ID, _PARSER_VERSION
    AREA_CENTER = center
    AREA_RADIUS_KM = radius_km
    TARGET_DISPOSITIONS = set(dispositions)
    _FEE_PARSER, _COST_FN = fee_parser, cost_fn
    _STREET_GPS = street_gps or {}
    _PARSER_VERSION = parser_version
    # Most iDNES adverts are the same flat as a Sreality one and get folded away
    # by cross-portal dedup, so they never reach the snapshot's comparables. The
    # fold cache is the only record that they were ever read.
    _PREV_BY_ID = dict(prev_fold_cache or {})
    _PREV_BY_ID.update({c["id"]: c for c in (prev_comparables or [])})


# Fields the detail fetch fills in. Carried forward when an advert is unchanged.
_DETAIL_FIELDS = (
    "description", "price_note", "admin_fee_czk", "old_price_czk", "fees_czk",
    "fees_missing", "fees_source", "electricity_czk", "electricity_estimated",
    "total_czk", "price_czk_per_sqm", "parser_version",
)


def _plan_detail_fetches(comps, label):
    """Split adverts into (fetch now, carried forward from last run).

    Same reasoning as the Sreality path: an advert's description and fee text
    never change while it is listed, so re-reading every one of them on every
    8h run is a large burst for nothing. Only new adverts and repriced ones are
    read, and the queue is capped so one run can't balloon."""
    fetch, cached = [], []
    for comp in comps:
        prev = _PREV_BY_ID.get(comp["id"])
        stale = (
            prev is None
            or prev.get("price_czk") != comp.get("price_czk")
            # A fee parsed by an older version of the parser is exactly the stale
            # value PARSER_VERSION exists to flush. This check used to live only
            # on the Sreality path, so a fee fix never reached these two sources.
            or prev.get("parser_version") != _PARSER_VERSION
            # A fold-cache record has no description by design; only a genuine
            # advert record missing one means the last read failed.
            or (prev.get("description") is None and not prev.get("cached_only"))
        )
        (fetch if stale else cached).append((comp, prev))
    for comp, prev in cached:
        for k in _DETAIL_FIELDS:
            if prev.get(k) is not None:
                comp[k] = prev[k]
    over = fetch[MAX_DETAIL_FETCHES:]
    for comp, prev in over:
        if prev:
            for k in _DETAIL_FIELDS:
                if prev.get(k) is not None:
                    comp[k] = prev[k]
    import sys
    print(
        f"    {label}: {min(len(fetch), MAX_DETAIL_FETCHES)} detail fetch(es), "
        f"{len(cached)} cached, {len(over)} deferred",
        file=sys.stderr,
    )
    return [c for c, _ in fetch[:MAX_DETAIL_FETCHES]]


def _place_by_street(street, ward):
    """(lat, lon, inside?) for an advert that names a street but has no GPS."""
    key = (street or "").strip().lower()
    pos = _STREET_GPS.get(key)
    if pos:
        return pos[0], pos[1], _in_area(pos[0], pos[1])
    # Sreality has no listing on that street this run, so we cannot place it.
    # Keep it only where the whole ward sits inside the circle; the wards that
    # straddle the boundary would otherwise smuggle in the far side.
    return None, None, ward in IDNES_CORE_WARDS


def _apply_fees(comp, cost_of_living_raw, description):
    """Run scrape.py's fee parser over whatever text this source could get, so
    a Bezrealitky or iDNES rental shows the same fee breakdown as a Sreality one
    instead of a permanent "neuvedeno"."""
    if _FEE_PARSER is None or _COST_FN is None:
        return comp
    fee, source, electricity = _FEE_PARSER(
        cost_of_living_raw, description, comp.get("price_czk")
    )
    if fee is None and comp.get("fees_czk") is not None:
        fee, source = comp["fees_czk"], comp.get("fees_source") or "field"
    fees, missing, elec, elec_est, total = _COST_FN(
        comp.get("price_czk"), fee, comp.get("transaction_type"), electricity, source
    )
    comp.update({
        "fees_czk": fees, "fees_missing": missing, "fees_source": source,
        "electricity_czk": elec, "electricity_estimated": elec_est, "total_czk": total,
        # Records which parser produced the fee above, so a later fix can tell
        # this value apart from one it has already corrected.
        "parser_version": _PARSER_VERSION,
    })
    sqm = comp.get("floor_area_sqm")
    if comp.get("transaction_type") == "pronajem" and total and sqm:
        comp["price_czk_per_sqm"] = round(total / sqm)
    return comp

NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

BEZ_DISPOSITION_MAP = {
    "DISP_1_KK": "1+kk", "DISP_2_KK": "2+kk", "DISP_3_KK": "3+kk",
    "DISP_4_KK": "4+kk", "DISP_1_1": "1+1", "DISP_2_1": "2+1", "DISP_3_1": "3+1",
}


def _blank_comparable():
    """Every key the dashboard/renderer reads, with safe defaults."""
    return {
        "id": None, "source": None, "title": None, "disposition": None,
        "transaction_type": None, "price_czk": None, "floor_area_sqm": None,
        "price_czk_per_sqm": None, "locality": None, "city_part": None,
        "street": None, "pod_harfou": False, "url": None, "active": True,
        "lat": None, "lon": None, "approx_location": False, "images": [],
        "thumb": None, "description": None, "seller_name": None,
        "fees_czk": None, "fees_missing": True, "fees_source": None,
        "electricity_czk": None, "electricity_estimated": False,
        "total_czk": None, "garage": None, "parking": None,
        "admin_fee_czk": None, "dist_km": None, "price_note": None,
        "old_price_czk": None, "cost_of_living_raw": None,
    }


def _finalize_costs(comp):
    """Fill total_czk / electricity / price_czk_per_sqm consistently with Sreality:
    rentals get a uniform electricity estimate + known fees; sales stay at price."""
    price = comp["price_czk"]
    sqm = comp["floor_area_sqm"]
    if comp["transaction_type"] == "pronajem" and price:
        fees = comp["fees_czk"] or 0
        comp["fees_missing"] = comp["fees_czk"] is None
        comp["electricity_czk"] = ELECTRICITY_ESTIMATE_CZK
        comp["electricity_estimated"] = True
        comp["total_czk"] = price + fees + ELECTRICITY_ESTIMATE_CZK
    else:
        comp["total_czk"] = price
    total = comp["total_czk"]
    if total and sqm:
        comp["price_czk_per_sqm"] = round(total / sqm)
    return comp


def _km_from_center(lat, lng):
    if lat is None or lng is None:
        return None
    la1, lo1 = math.radians(AREA_CENTER[0]), math.radians(AREA_CENTER[1])
    la2, lo2 = math.radians(lat), math.radians(lng)
    dlat, dlon = la2 - la1, lo2 - lo1
    a = math.sin(dlat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlon / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


def _in_area(lat, lng):
    d = _km_from_center(lat, lng)
    return d is not None and d <= AREA_RADIUS_KM


# --------------------------------------------------------------------------- #
# Bezrealitky — sitemap-listed /vypis/ locality pages (robots-allowed)
# --------------------------------------------------------------------------- #
def _walk_adverts(obj):
    found = []
    if isinstance(obj, dict):
        if obj.get("__typename") == "Advert":
            found.append(obj)
        for v in obj.values():
            found += _walk_adverts(v)
    elif isinstance(obj, list):
        for v in obj:
            found += _walk_adverts(v)
    return found


def _build_bez_image_map(obj, out=None):
    """Map normalized Image id -> (thumb_url, main_url). Bezrealitky's Next.js
    store references images by {"__ref": "Image:<id>"}; the actual URLs live on
    separate Image entities keyed like url({"filter":"RECORD_THUMB"})."""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        if obj.get("__typename") == "Image" and obj.get("id") is not None:
            thumb = main = None
            for k, v in obj.items():
                if isinstance(v, str) and k.startswith("url("):
                    if "RECORD_THUMB" in k:
                        thumb = v
                    elif "RECORD_MAIN" in k:
                        main = v
            out[str(obj["id"])] = (thumb, main)
        for v in obj.values():
            _build_bez_image_map(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _build_bez_image_map(v, out)
    return out


def _bez_parse_advert(a, img_map):
    disp = BEZ_DISPOSITION_MAP.get(a.get("disposition"))
    if disp not in TARGET_DISPOSITIONS:
        return None
    gps = a.get("gps") or {}
    lat, lng = gps.get("lat"), gps.get("lng")
    if not _in_area(lat, lng):
        return None
    if not a.get("active", True) or a.get("archived"):
        return None
    price = a.get("price")
    surface = a.get("surface")
    tx = "pronajem" if a.get("offerType") == "PRONAJEM" else "prodej"
    uri = a.get("uri")
    comp = _blank_comparable()
    # Resolve the advert's main photo via its normalized Image reference.
    thumb = main = None
    ref = (a.get("mainImage") or {}).get("__ref")
    if ref:
        thumb, main = img_map.get(ref.split(":", 1)[-1], (None, None))
    comp.update({
        "id": f"bez-{a.get('id')}",
        "source": "bezrealitky",
        "title": f"{'Pronájem' if tx == 'pronajem' else 'Prodej'} bytu {disp or ''} {surface or ''} m²".strip(),
        "disposition": disp,
        "transaction_type": tx,
        "price_czk": price,
        "floor_area_sqm": surface,
        "locality": (a.get("address") or "").strip() or "okolí Pod Harfou",
        "city_part": (a.get("addressUserInput") or "").split(",")[-1].strip() or None,
        "street": None,
        "url": f"https://www.bezrealitky.cz/nemovitosti-byty-domy/{uri}" if uri else None,
        "lat": lat, "lon": lng,
        "dist_km": round(_km_from_center(lat, lng) or 0, 2),
        "thumb": thumb or main,
        "images": [main or thumb] if (main or thumb) else [],
        # `charges` is the MONTHLY service-fee field ("Poplatky za služby") --
        # not to be confused with `fee`, the one-time administrative fee.
        "fees_czk": a.get("charges") or None,
        "fees_source": "field" if a.get("charges") else None,
    })
    return _finalize_costs(comp)


def _bez_fetch_detail(url):
    """Return (admin_fee_czk, description, monthly_charges_czk) from a listing
    detail page. The one-time administrative fee lives in the advert `fee`
    field, and the monthly service charge in `charges` -- both only present on
    the detail page, not the /vypis list."""
    import json
    try:
        resp = SESSION.get(url, timeout=25)
        if resp.status_code != 200:
            return None, None, None
        m = NEXT_DATA_RE.search(resp.text)
        if not m:
            return None, None, None
        data = json.loads(m.group(1))
    except Exception:
        return None, None, None
    adverts = _walk_adverts(data)
    if not adverts:
        return None, None, None
    a = max(adverts, key=lambda x: len(x.keys()))
    desc = a.get("description")
    return a.get("fee"), (desc[:1200] if desc else None), a.get("charges")


def fetch_bezrealitky(max_pages=8, sleep=0.4):
    """Read the allowed /vypis/ Prague apartment listing pages, page by page,
    and keep only Vysočany-area 1+kk/2+kk adverts (both sale and rent). Then
    fetch each candidate's detail page for the administrative fee + description,
    dropping listings whose one-time admin fee exceeds MAX_ADMIN_FEE_CZK."""
    out = {}
    for offer in ("nabidka-prodej", "nabidka-pronajem"):
        for page in range(1, max_pages + 1):
            url = f"https://www.bezrealitky.cz/vypis/{offer}/byt/praha"
            try:
                resp = SESSION.get(url, params={"page": page}, timeout=25)
                if resp.status_code != 200:
                    break
                m = NEXT_DATA_RE.search(resp.text)
                if not m:
                    break
                import json
                data = json.loads(m.group(1))
            except Exception:
                break
            adverts = _walk_adverts(data)
            if not adverts:
                break
            img_map = _build_bez_image_map(data)
            for a in adverts:
                comp = _bez_parse_advert(a, img_map)
                if comp:
                    out[comp["id"]] = comp
            time.sleep(sleep)

    # Enrich each candidate from its detail page (admin fee + description),
    # then drop listings whose one-time administrative fee is above the cap.
    for comp in _plan_detail_fetches(list(out.values()), "bezrealitky"):
        if not comp.get("url"):
            continue
        fee, desc, charges = _bez_fetch_detail(comp["url"])
        comp["admin_fee_czk"] = fee
        if desc:
            comp["description"] = desc
        if charges:
            comp["fees_czk"], comp["fees_source"] = charges, "field"
        _apply_fees(comp, str(charges) if charges else None, desc)
        time.sleep(sleep)
    # The one-time administrative fee is the reason for the detail fetch: above
    # the cap the advert is not worth showing at all.
    return [c for c in out.values()
            if not (c.get("admin_fee_czk") and c["admin_fee_czk"] > MAX_ADMIN_FEE_CZK)]


# --------------------------------------------------------------------------- #
# Reality.iDNES — /s/ search pages (robots-allowed), parsed from result cards
# --------------------------------------------------------------------------- #
IDNES_CARD_RE = re.compile(r'c-products__item(.*?)(?=c-products__item|</main)', re.S)
IDNES_LINK_RE = re.compile(r'href="(https://reality\.idnes\.cz/detail/[^"]+)"')
IDNES_TITLE_RE = re.compile(r'c-products__title[^>]*>(.*?)</a>', re.S)
IDNES_PRICE_RE = re.compile(r'c-products__price[^>]*>(.*?)</p>', re.S)
IDNES_INFO_RE = re.compile(r'c-products__info[^>]*>(.*?)</p>', re.S)
# from title text: "prodej bytu 2+kk 55 m² ..."
IDNES_DISP_RE = re.compile(r'(\d\s*\+\s*(?:kk|\d))', re.I)
IDNES_AREA_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*m²')
IDNES_ID_RE = re.compile(r'/detail/[^/]+/[^/]+/[^/]+/([0-9a-f]+)/?')


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _idnes_parse_card(seg, tx):
    link_m = IDNES_LINK_RE.search(seg)
    if not link_m:
        return None
    url = link_m.group(1)
    id_m = IDNES_ID_RE.search(url)
    listing_id = id_m.group(1) if id_m else url.rstrip("/").rsplit("/", 1)[-1]
    title = _text((IDNES_TITLE_RE.search(seg) or [None, ""])[1] if IDNES_TITLE_RE.search(seg) else "")
    info = _text((IDNES_INFO_RE.search(seg) or [None, ""])[1] if IDNES_INFO_RE.search(seg) else "")
    price_txt = _text((IDNES_PRICE_RE.search(seg) or [None, ""])[1] if IDNES_PRICE_RE.search(seg) else "")
    disp_m = IDNES_DISP_RE.search(title)
    disp = disp_m.group(1).replace(" ", "").lower() if disp_m else None
    if disp not in TARGET_DISPOSITIONS:
        return None
    # "Kolmá, Praha 9 - Vysočany" -> street "Kolmá", ward "Vysočany". The ward
    # is the only locality signal iDNES gives (no GPS anywhere), so it stands in
    # for the radius test the other two sources get.
    ward = info.rsplit("-", 1)[-1].strip() if "-" in info else ""
    if ward not in IDNES_WARDS:
        return None
    street = info.split(",", 1)[0].strip() if "," in info else None
    lat, lon, inside = _place_by_street(street, ward)
    if not inside:
        return None
    area_m = IDNES_AREA_RE.search(title)
    sqm = float(area_m.group(1).replace(",", ".")) if area_m else None
    digits = re.sub(r"[^\d]", "", price_txt)
    price = int(digits) if digits else None
    # Thumbnail: the listing photo is served off iDNES's reality image CDN
    # (1gr.cz); skip icons/logos by matching that host only.
    thumb = None
    for im in re.findall(r"<img[^>]+>", seg):
        mm = re.search(r'(?:data-src|src)="([^"]*1gr\.cz[^"]*)"', im)
        if mm:
            thumb = mm.group(1)
            break
    comp = _blank_comparable()
    comp.update({
        "id": f"idnes-{listing_id}",
        "source": "idnes",
        "title": f"{'Pronájem' if tx == 'pronajem' else 'Prodej'} bytu {disp} {int(sqm) if sqm else ''} m²".strip(),
        "disposition": disp,
        "transaction_type": tx,
        "price_czk": price,
        "floor_area_sqm": sqm,
        "locality": info or ward,
        "city_part": ward,
        "street": street,
        "pod_harfou": street == "Pod Harfou",
        "url": url,
        "thumb": thumb,
        "images": [thumb] if thumb else [],
        # Position is the street's average, never the building's, so it is
        # flagged approximate and drawn with the dashed marker on the map.
        "lat": lat, "lon": lon,
        "approx_location": lat is not None,
        "dist_km": round(_km_from_center(lat, lon), 2) if lat is not None else None,
    })
    return _finalize_costs(comp)


# The detail page carries the two things the search card doesn't: a
# "Poznámka k ceně" line that almost always states the monthly service fee, and
# the full description. Without them every iDNES rental showed "poplatky
# neuvedeno" and its all-in total was really just the bare rent.
IDNES_NOTE_RE = re.compile(r'wrapper-price-notes.*?</div>', re.S)
IDNES_DESC_RE = re.compile(r'<div class="b-desc[^"]*">(.*?)</div>', re.S)
IDNES_OLDPRICE_RE = re.compile(r'<del[^>]*>(.*?)</del>', re.S)


def _idnes_fetch_detail(url):
    """Return (price_note, description, old_price_czk) for one advert."""
    try:
        resp = SESSION.get(url, timeout=25)
        if resp.status_code != 200:
            return None, None, None
    except Exception:
        return None, None, None
    body = resp.text
    note_m = IDNES_NOTE_RE.search(body)
    note = _text(note_m.group(0)).replace("Poznámka k ceně:", "").strip() if note_m else None
    desc_m = IDNES_DESC_RE.search(body)
    desc = _text(desc_m.group(1))[:1200] if desc_m else None
    # A struck-through price means the advert was reduced -- worth surfacing as
    # a deal signal, and iDNES is the only source that states it outright.
    old_price = None
    old_m = IDNES_OLDPRICE_RE.search(body)
    if old_m:
        digits = re.sub(r"[^\d]", "", _text(old_m.group(1)))
        old_price = int(digits) if digits else None
    return note, desc, old_price


def fetch_idnes(max_pages=8, sleep=0.4):
    out = {}
    for tx, seg_path in (("prodej", "prodej"), ("pronajem", "pronajem")):
        # The watched circle spans three Prague districts, and iDNES only
        # searches one at a time; the ward whitelist trims each result set.
        for district in IDNES_DISTRICTS:
            for page in range(1, max_pages + 1):
                url = f"https://reality.idnes.cz/s/{seg_path}/byty/{district}/"
                try:
                    resp = SESSION.get(url, params={"page": page} if page > 1 else None, timeout=25)
                    if resp.status_code != 200:
                        break
                except Exception:
                    break
                cards = IDNES_CARD_RE.findall(resp.text)
                if not cards:
                    break
                for seg in cards:
                    comp = _idnes_parse_card(seg, tx)
                    if comp:
                        out.setdefault(comp["id"], comp)
                time.sleep(sleep)
                # crude last-page guard: no listing links at all -> stop
                if not IDNES_LINK_RE.search("".join(cards)):
                    break

    for comp in _plan_detail_fetches(list(out.values()), "idnes"):
        note, desc, old_price = _idnes_fetch_detail(comp["url"])
        if desc:
            comp["description"] = desc
        if note:
            comp["price_note"] = note
        if old_price and comp.get("price_czk") and old_price > comp["price_czk"]:
            comp["old_price_czk"] = old_price
        _apply_fees(comp, note, desc)
        time.sleep(sleep)
    return list(out.values())


def fetch_extra_comparables():
    """All non-Sreality comparables, best-effort: a failing source must not
    take the others (or the whole scrape) down."""
    import sys
    results = []
    for name, fn in (("bezrealitky", fetch_bezrealitky), ("idnes", fetch_idnes)):
        try:
            got = fn()
            print(f"  {name}: {len(got)} comparable(s)", file=sys.stderr)
            results += got
        except Exception as e:  # noqa: BLE001 - degrade gracefully
            print(f"  {name}: FAILED ({e!r}) — skipping", file=sys.stderr)
    return results
