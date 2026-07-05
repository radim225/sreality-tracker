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
TARGET_DISPOSITIONS = {"1+kk", "2+kk"}

# Rough GPS bounding box for Praha 9 (Vysočany/Prosek/Libeň/Letňany/Kbely area),
# centred on the tracked Pod Harfou listing. Approximate on purpose — it is a
# locality filter, not a cadastral boundary.
PRAHA9_BOX = {"lat_min": 50.085, "lat_max": 50.165, "lng_min": 14.470, "lng_max": 14.620}

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
    }


def _finalize_costs(comp):
    """Fill total_czk / electricity / price_czk_per_sqm consistently with Sreality:
    rentals get a uniform electricity estimate + known fees; sales stay at price."""
    price = comp["price_czk"]
    sqm = comp["floor_area_sqm"]
    if comp["transaction_type"] == "pronajem" and price:
        fees = comp["fees_czk"] or 0
        comp["electricity_czk"] = ELECTRICITY_ESTIMATE_CZK
        comp["electricity_estimated"] = True
        comp["total_czk"] = price + fees + ELECTRICITY_ESTIMATE_CZK
    else:
        comp["total_czk"] = price
    total = comp["total_czk"]
    if total and sqm:
        comp["price_czk_per_sqm"] = round(total / sqm)
    return comp


def _in_praha9(lat, lng):
    if lat is None or lng is None:
        return False
    b = PRAHA9_BOX
    return b["lat_min"] <= lat <= b["lat_max"] and b["lng_min"] <= lng <= b["lng_max"]


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
    if not _in_praha9(lat, lng):
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
        "locality": "Praha 9 (přibl.)",
        "city_part": "Praha 9",
        "url": f"https://www.bezrealitky.cz/nemovitosti-byty-domy/{uri}" if uri else None,
        "lat": lat, "lon": lng,
        "thumb": thumb or main,
        "images": [main or thumb] if (main or thumb) else [],
    })
    return _finalize_costs(comp)


def fetch_bezrealitky(max_pages=8, sleep=0.4):
    """Read the allowed /vypis/ Prague apartment listing pages, page by page,
    and keep only Praha-9 1+kk/2+kk adverts (both sale and rent)."""
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
    return list(out.values())


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
    if "Praha 9" not in info and "Praha 9" not in title:
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
        "locality": info or "Praha 9",
        "city_part": "Praha 9",
        "url": url,
        "thumb": thumb,
        "images": [thumb] if thumb else [],
    })
    return _finalize_costs(comp)


def fetch_idnes(max_pages=5, sleep=0.4):
    out = {}
    for tx, seg_path in (("prodej", "prodej"), ("pronajem", "pronajem")):
        for page in range(1, max_pages + 1):
            url = f"https://reality.idnes.cz/s/{seg_path}/byty/praha-9/"
            try:
                resp = SESSION.get(url, params={"page": page} if page > 1 else None, timeout=25)
                if resp.status_code != 200:
                    break
            except Exception:
                break
            cards = IDNES_CARD_RE.findall(resp.text)
            if not cards:
                break
            added = 0
            for seg in cards:
                comp = _idnes_parse_card(seg, tx)
                if comp:
                    out[comp["id"]] = comp
                    added += 1
            time.sleep(sleep)
            # crude last-page guard: no listing links at all -> stop
            if not IDNES_LINK_RE.search("".join(cards)):
                break
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
