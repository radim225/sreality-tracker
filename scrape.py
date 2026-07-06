#!/usr/bin/env python3
"""Sreality.cz property monitor: scrapes a seed listing plus comparable
listings in the same area, snapshots the result, diffs against the previous
snapshot, and regenerates a mobile-friendly dashboard with photos and a map."""
import html
import json
import re
import statistics
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

import sources

ROOT = Path(__file__).parent
SNAPSHOTS_DIR = ROOT / "snapshots"
DASHBOARD_PATH = ROOT / "dashboard.html"
CHANGES_PATH = ROOT / "last_changes.json"
LATEST_SNAPSHOT_PATH = ROOT / "latest_snapshot.json"
TRACKED_PATH = ROOT / "tracked.json"
CHANGES_HISTORY_PATH = ROOT / "changes_history.json"

# Sreality category_sub_cb codes (from /hledani estatesFilterPage)
DISPOSITION_CODES = {2: "1+kk", 4: "2+kk"}
TRANSACTION_TYPES = ["pronajem", "prodej"]  # rent, sale
SEARCH_REGION_TEXT = "Vysočany"  # free-text resolved server-side to the ward/locality
MAX_IMAGES_PER_LISTING = 5
MAX_DESCRIPTION_CHARS = 1200
MAX_HISTORY_EVENTS = 300

# Sreality's "estate" payload gives base rent (price) and service fees
# (params.costOfLiving) separately, but never itemizes electricity -- it's
# folded into "energie"/"služby" inconsistently or omitted entirely. We add a
# single uniform estimate so every rental has a comparable all-in total.
ELECTRICITY_ESTIMATE_CZK = 1500

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def fetch_next_data(url, params=None):
    resp = SESSION.get(url, params=params, timeout=20)
    if resp.status_code == 404:
        return None, resp.status_code
    resp.raise_for_status()
    m = NEXT_DATA_RE.search(resp.text)
    if not m:
        return None, resp.status_code
    return json.loads(m.group(1)), resp.status_code


def get_query_data(next_data, key_prefix):
    queries = next_data["props"]["pageProps"]["dehydratedState"]["queries"]
    for q in queries:
        qk = q.get("queryKey")
        if qk and qk[0] == key_prefix:
            if q["state"].get("status") == "success":
                return q["state"]["data"], qk[1] if len(qk) > 1 else None
    return None, None


# The bare sdn.cz CDN path returns 401 Unauthorized when hotlinked directly --
# it only serves images through its resize pipeline, via one of a fixed set of
# whitelisted "fl=res,W,H,MODE|shr,,20|FORMAT,QUALITY" transform strings (any
# other width/height combination is rejected with 400). These two presets are
# scraped straight from the site's own srcset markup.
THUMB_SUFFIX = "?fl=res,400,400,1|shr,,20|webp,60"
FULL_SUFFIX = "?fl=res,800,800,1|shr,,20|jpg,80"


def cdn_url(url):
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    return url


def extract_image_bases(images_field):
    urls = []
    for img in images_field or []:
        u = cdn_url(img.get("url") if isinstance(img, dict) else None)
        if u:
            urls.append(u)
        if len(urls) >= MAX_IMAGES_PER_LISTING:
            break
    return urls


def extract_images(images_field):
    """Full-size (gallery) image URLs, ready to hotlink."""
    return [u + FULL_SUFFIX for u in extract_image_bases(images_field)]


def extract_thumb(images_field):
    """Small thumbnail URL for the first image, ready to hotlink."""
    bases = extract_image_bases(images_field)
    return bases[0] + THUMB_SUFFIX if bases else None


# params.costOfLiving is almost never a clean integer -- it's usually a short
# Czech phrase like "+ poplatky 3.400 Kč + el. energie + vratná kauce + provize
# RK" that bundles the actual monthly fee together with one-time costs
# (deposit, agency commission) and a vague electricity mention. We split it
# (and, as a fallback, the free-text description) into clauses and only keep
# amounts attached to recurring-fee language, never deposit/commission ones.
FEE_KEYWORDS = [
    "poplatky", "poplatek", "služby", "zálohy", "záloha", "měsíční výdaje",
    "provozní náklady", "fond oprav", "svj", "společné prostory", "správa domu",
]
EXCLUDE_KEYWORDS = ["kauce", "provize", "jednorázov", "deposit", "refundable"]
ELECTRICITY_KEYWORDS = ["energie", "elektřin"]

# Splits on '+', ';', newline, "plus", a sentence comma (not a Czech
# thousands-separator comma like "3,400"), or a sentence-ending period
# (not an abbreviation period like "el." before a lowercase word).
CLAUSE_SPLIT_RE = re.compile(r'\+|;|\n|\bplus\b|,\s+|(?<=[a-zá-ž])\.\s+(?=[A-ZÁ-Ž])', re.I)
# Czech number formats: "3 400", "3.400", "3,400", "3400", with optional Kč/CZK.
NUMBER_RE = re.compile(r'(\d{1,3}(?:[ .,]\d{3})+|\d{3,6})\s*(?:k[čc]|czk)?', re.I)


def parse_amount(clause):
    m = NUMBER_RE.search(clause)
    if not m:
        return None
    digits = re.sub(r"[ .,]", "", m.group(1))
    try:
        v = int(digits)
    except ValueError:
        return None
    return v if v > 0 else None


def parse_cost_of_living_text(text):
    """costOfLiving is themed as monthly living costs already, so any amount
    in it that isn't in a deposit/commission clause is the fee -- no keyword
    required (e.g. "4800 Kč plus elektřina")."""
    fee = electricity = None
    for clause in CLAUSE_SPLIT_RE.split(text or ""):
        low = clause.lower()
        if any(k in low for k in EXCLUDE_KEYWORDS):
            continue
        amt = parse_amount(clause)
        if amt is None:
            continue
        if any(k in low for k in ELECTRICITY_KEYWORDS):
            electricity = electricity if electricity is not None else amt
        else:
            fee = fee if fee is not None else amt
    return fee, electricity


def parse_fee_from_description(text):
    """Free-text description fallback -- here a fee keyword IS required, since
    unanchored numbers in prose are far more likely to be unrelated (m²,
    floor, year built, etc.)."""
    fee = electricity = None
    for clause in CLAUSE_SPLIT_RE.split(text or ""):
        low = clause.lower()
        if any(k in low for k in EXCLUDE_KEYWORDS):
            continue
        if fee is None and any(k in low for k in FEE_KEYWORDS):
            fee = parse_amount(clause)
        if electricity is None and any(k in low for k in ELECTRICITY_KEYWORDS):
            electricity = parse_amount(clause)
    return fee, electricity


def extract_fees_and_electricity(cost_of_living_raw, description):
    """Returns (fees_czk, fees_source, electricity_explicit_czk).
    fees_source is "field" (clean int or parsed from costOfLiving text),
    "text" (parsed from the description), or None (not found anywhere)."""
    raw = (cost_of_living_raw or "").strip()
    try:
        v = int(raw)
        if v > 0:
            return v, "field", None
    except ValueError:
        pass
    fee, electricity = parse_cost_of_living_text(raw)
    if fee is not None:
        return fee, "field", electricity
    fee, electricity_2 = parse_fee_from_description(description)
    electricity = electricity if electricity is not None else electricity_2
    if fee is not None:
        return fee, "text", electricity
    return None, None, electricity


def cost_breakdown(price_czk, fees_czk, transaction_type, electricity_explicit=None):
    """Returns (fees_czk, fees_missing, electricity_czk, electricity_estimated,
    total_czk) for a listing. Only rentals (pronajem) get an electricity
    figure and a fees+electricity total; sales just total to the purchase
    price. If the listing states a real electricity amount, use it instead of
    the uniform estimate."""
    fees_missing = fees_czk is None
    if transaction_type != "pronajem":
        return fees_czk, fees_missing, None, False, price_czk
    if electricity_explicit is not None:
        electricity_czk, electricity_estimated = electricity_explicit, False
    else:
        electricity_czk, electricity_estimated = ELECTRICITY_ESTIMATE_CZK, True
    total_czk = None
    if price_czk is not None:
        total_czk = price_czk + (fees_czk or 0) + electricity_czk
    return fees_czk, fees_missing, electricity_czk, electricity_estimated, total_czk


def garage_parking_from_params(params):
    garage = params.get("garage")
    garage = bool(garage) if garage is not None else None
    parking = params.get("parkingLots")
    if parking is None:
        parking = params.get("parking")
    parking = bool(parking) if parking is not None else None
    return garage, parking


def load_tracked_config():
    if not TRACKED_PATH.exists():
        return []
    return json.loads(TRACKED_PATH.read_text())


def fetch_tracked(url, listing_id):
    next_data, status = fetch_next_data(url)
    if next_data is None:
        return {
            "id": listing_id,
            "url": url,
            "active": False,
            "fetched_at": now_iso(),
            "error": f"HTTP {status}",
        }
    data, _ = get_query_data(next_data, "estate")
    if data is None:
        return {
            "id": listing_id,
            "url": url,
            "active": False,
            "fetched_at": now_iso(),
            "error": "estate query missing from page",
        }
    params = data.get("params") or {}
    locality = data.get("locality") or {}
    seller = data.get("seller") or {}
    premise = data.get("premise") or {}
    rent_czk = data.get("priceCzk")
    # categoryTypeCb.name is a Czech display string ("Pronájem"/"Prodej");
    # normalize via its numeric code (1=sale, 2=rent) to match the ASCII
    # "pronajem"/"prodej" values used everywhere else (comparables, URLs).
    type_code = (data.get("categoryTypeCb") or {}).get("value")
    transaction_type = "pronajem" if type_code == 2 else "prodej"
    fees_czk, fees_source, electricity_explicit = extract_fees_and_electricity(
        params.get("costOfLiving"), data.get("description")
    )
    fees_czk, fees_missing, electricity_czk, electricity_estimated, total_czk = (
        cost_breakdown(rent_czk, fees_czk, transaction_type, electricity_explicit)
    )
    garage, parking = garage_parking_from_params(params)
    floor_area_sqm = params.get("floorArea")
    price_czk_per_sqm = data.get("priceCzkPerSqM")
    if transaction_type == "pronajem" and total_czk and floor_area_sqm:
        price_czk_per_sqm = round(total_czk / floor_area_sqm)

    return {
        "id": listing_id,
        "url": url,
        "active": True,
        "fetched_at": now_iso(),
        "title": data.get("name"),
        "disposition": (data.get("categorySubCb") or {}).get("name"),
        "transaction_type": transaction_type,
        "rent_czk": rent_czk,
        "fees_czk": fees_czk,
        "fees_missing": fees_missing,
        "fees_source": fees_source,
        "electricity_czk": electricity_czk,
        "electricity_estimated": electricity_estimated,
        "total_czk": total_czk,
        "garage": garage,
        "parking": parking,
        "price_czk_per_sqm": price_czk_per_sqm,
        "floor_area_sqm": floor_area_sqm,
        "floor_number": params.get("floorNumber"),
        "floors_total": params.get("floors"),
        "locality": format_locality(locality),
        "city_part": locality.get("cityPart"),
        "street": locality.get("street"),
        "district": locality.get("district"),
        "description": (data.get("description") or "")[:MAX_DESCRIPTION_CHARS],
        "seller_name": seller.get("name") or premise.get("name"),
        "photo_count": len(data.get("images") or []),
        "images": extract_images(data.get("images")),
        "thumb": extract_thumb(data.get("images")),
        "refundable_deposit_czk": params.get("refundableDeposit"),
        "lat": locality.get("latitude"),
        "lon": locality.get("longitude"),
        "approx_location": False,
    }


def format_locality(locality):
    parts = [
        locality.get("street"),
        locality.get("cityPart"),
        locality.get("city"),
    ]
    return ", ".join(p for p in parts if p)


def fetch_comparables():
    by_id = {}
    for tx_type in TRANSACTION_TYPES:
        page = 1
        seen_total = None
        seen_offsets = set()
        while True:
            url = f"https://www.sreality.cz/hledani/{tx_type}/byty"
            # The site's pagination query param is the Czech "strana" (page),
            # not "page" -- "page" is silently ignored and always returns page 1.
            next_data, status = fetch_next_data(
                url, params={"region": SEARCH_REGION_TEXT, "strana": page}
            )
            if next_data is None:
                break
            data, _ = get_query_data(next_data, "estatesSearch")
            if data is None:
                break
            pagination = data.get("pagination") or {}
            seen_total = pagination.get("total")
            offset = pagination.get("offset")
            if offset in seen_offsets:
                break  # server stopped advancing pages; avoid infinite/duplicate loop
            seen_offsets.add(offset)
            page_results = data.get("results") or []
            if not page_results:
                break
            for r in page_results:
                sub = r.get("categorySubCb") or {}
                if sub.get("value") not in DISPOSITION_CODES:
                    continue
                comp = parse_comparable(r, tx_type)
                by_id[comp["id"]] = comp  # dedup strictly by listing id
            limit = pagination.get("limit") or len(page_results) or 22
            if page * limit >= (seen_total or 0):
                break
            page += 1
            time.sleep(0.3)

    comparables = list(by_id.values())
    print(f"Enriching {len(comparables)} listings with detail (description, photos)...", file=sys.stderr)
    for i, comp in enumerate(comparables, 1):
        enrich_comparable(comp)
        if i % 50 == 0:
            print(f"  ...{i}/{len(comparables)}", file=sys.stderr)
        time.sleep(0.15)

    stale = [c for c in comparables if not c.get("active")]
    if stale:
        print(
            f"Dropping {len(stale)} listing(s) that went inactive between search and detail fetch",
            file=sys.stderr,
        )
    comparables = [c for c in comparables if c.get("active")]

    apply_approx_locations(comparables)
    return comparables


def parse_comparable(r, tx_type):
    locality = r.get("locality") or {}
    price_czk = r.get("priceCzk") or None
    sqm = None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m", r.get("name") or "")
    if m:
        try:
            sqm = float(m.group(1).replace(",", "."))
        except ValueError:
            sqm = None
    price_per_sqm = r.get("priceCzkPerSqM") or None
    if not price_per_sqm and price_czk and sqm:
        price_per_sqm = round(price_czk / sqm)
    disposition = (r.get("categorySubCb") or {}).get("name") or "x"
    slug = urllib.parse.quote(disposition)
    # Sreality redirects /detail/<type>/byt/<any-slug>/<any-locality>/<id> to the
    # canonical URL, so the locality segment doesn't need to be exact.
    detail_url = (
        f"https://www.sreality.cz/detail/{tx_type}/byt/{slug}/x/{r['id']}"
    )
    return {
        "id": r["id"],
        "title": r.get("name"),
        "disposition": (r.get("categorySubCb") or {}).get("name"),
        "transaction_type": "pronajem" if tx_type == "pronajem" else "prodej",
        "price_czk": price_czk if price_czk else None,
        "floor_area_sqm": sqm,
        "price_czk_per_sqm": price_per_sqm,
        "locality": format_locality(locality),
        "city_part": locality.get("cityPart"),
        "street": locality.get("street"),
        "pod_harfou": locality.get("street") == "Pod Harfou",
        "url": detail_url,
        "active": True,
        "lat": locality.get("latitude"),
        "lon": locality.get("longitude"),
        "approx_location": False,
        "images": extract_images(r.get("images")),
        "thumb": extract_thumb(r.get("images")),
        "description": None,
        "seller_name": None,
        # Fees/electricity/garage need the detail page (not in search payload);
        # filled in by enrich_comparable. price_czk_per_sqm above is rent-only
        # until enrichment recomputes it against the all-in total for rentals.
        "fees_czk": None,
        "fees_missing": True,
        "fees_source": None,
        "electricity_czk": None,
        "electricity_estimated": False,
        "total_czk": price_czk if price_czk else None,
        "garage": None,
        "parking": None,
    }


def enrich_comparable(comp):
    """Fetch the full detail page for description/seller/photos/fees. GPS and
    a thumbnail already came from the search payload, so this is best-effort."""
    next_data, status = fetch_next_data(comp["url"])
    if next_data is None:
        if status == 404:
            comp["active"] = False
        return
    data, _ = get_query_data(next_data, "estate")
    if data is None:
        return
    params = data.get("params") or {}
    seller = data.get("seller") or {}
    premise = data.get("premise") or {}
    locality = data.get("locality") or {}
    comp["description"] = (data.get("description") or "")[:MAX_DESCRIPTION_CHARS] or None
    comp["seller_name"] = seller.get("name") or premise.get("name")
    images = extract_images(data.get("images"))
    if images:
        comp["images"] = images
        comp["thumb"] = extract_thumb(data.get("images"))
    if comp.get("lat") is None and locality.get("latitude") is not None:
        comp["lat"] = locality.get("latitude")
        comp["lon"] = locality.get("longitude")
    if not comp.get("floor_area_sqm") and params.get("floorArea"):
        comp["floor_area_sqm"] = params.get("floorArea")
    comp["floor_number"] = params.get("floorNumber")
    comp["floors_total"] = params.get("floors")

    fees_czk, fees_source, electricity_explicit = extract_fees_and_electricity(
        params.get("costOfLiving"), data.get("description")
    )
    fees_czk, fees_missing, electricity_czk, electricity_estimated, total_czk = (
        cost_breakdown(comp.get("price_czk"), fees_czk, comp.get("transaction_type"), electricity_explicit)
    )
    comp["fees_czk"] = fees_czk
    comp["fees_missing"] = fees_missing
    comp["fees_source"] = fees_source
    comp["electricity_czk"] = electricity_czk
    comp["electricity_estimated"] = electricity_estimated
    comp["total_czk"] = total_czk
    comp["garage"], comp["parking"] = garage_parking_from_params(params)
    if (
        comp.get("transaction_type") == "pronajem"
        and total_czk
        and comp.get("floor_area_sqm")
    ):
        comp["price_czk_per_sqm"] = round(total_czk / comp["floor_area_sqm"])


def apply_approx_locations(comparables):
    known = [(c["lat"], c["lon"]) for c in comparables if c.get("lat") and c.get("lon")]
    if known:
        centroid_lat = sum(p[0] for p in known) / len(known)
        centroid_lon = sum(p[1] for p in known) / len(known)
    else:
        centroid_lat, centroid_lon = 50.1075, 14.5070  # Vysočany, Praha 9 fallback

    pod_harfou_known = [
        (c["lat"], c["lon"])
        for c in comparables
        if c.get("pod_harfou") and c.get("lat") and c.get("lon")
    ]
    if pod_harfou_known:
        ph_lat = sum(p[0] for p in pod_harfou_known) / len(pod_harfou_known)
        ph_lon = sum(p[1] for p in pod_harfou_known) / len(pod_harfou_known)
    else:
        ph_lat, ph_lon = centroid_lat, centroid_lon

    for c in comparables:
        if c.get("lat") is None or c.get("lon") is None:
            if c.get("pod_harfou"):
                c["lat"], c["lon"] = ph_lat, ph_lon
            else:
                c["lat"], c["lon"] = centroid_lat, centroid_lon
            c["approx_location"] = True


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_latest_snapshot():
    if not LATEST_SNAPSHOT_PATH.exists():
        return None
    return json.loads(LATEST_SNAPSHOT_PATH.read_text())


def load_changes_history():
    if not CHANGES_HISTORY_PATH.exists():
        return []
    return json.loads(CHANGES_HISTORY_PATH.read_text())


def update_changes_history(changes):
    """Accumulates new/removed/price-change events across runs (capped) so the
    dashboard can show a scrollable history, not just the latest diff."""
    history = load_changes_history()
    at = changes["generated_at"]
    new_events = []
    for tc in changes.get("tracked_price_changes", []):
        new_events.append(
            {
                "at": at,
                "kind": "price_change",
                "id": tc["id"],
                "old_total_czk": tc.get("old_total_czk"),
                "new_total_czk": tc.get("new_total_czk"),
                "item": None,
            }
        )
    for c in changes.get("price_changes", []):
        new_events.append(
            {
                "at": at,
                "kind": "price_change",
                "id": c["id"],
                "old_price_czk": c.get("old_price_czk"),
                "new_price_czk": c.get("new_price_czk"),
                "old_total_czk": c.get("old_total_czk"),
                "new_total_czk": c.get("new_total_czk"),
                "item": c,
            }
        )
    for c in changes.get("new_listings", []):
        new_events.append({"at": at, "kind": "new", "id": c["id"], "item": c})
    for c in changes.get("newly_inactive", []):
        new_events.append({"at": at, "kind": "removed", "id": c["id"], "item": c})

    history = (new_events + history)[:MAX_HISTORY_EVENTS]
    CHANGES_HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2))
    return history


def diff_snapshots(prev, curr):
    changes = {
        "generated_at": now_iso(),
        "tracked_price_changes": [],
        "newly_inactive": [],
        "new_listings": [],
        "price_changes": [],
    }
    if prev is None:
        return changes

    # Snapshots from before the cost-breakdown feature don't have
    # "electricity_estimated" -- their total_czk meant rent+fees only. Comparing
    # those against the new all-in total would report every rental as a "price
    # change" on this one transition run. Fall back to comparing base price for
    # any record that predates the new schema.
    def cmp_value(item):
        if item.get("total_czk") is not None and "electricity_estimated" in item:
            return item["total_czk"]
        return item.get("price_czk")

    prev_tracked_by_id = {t["id"]: t for t in prev.get("tracked", [])}
    for curr_t in curr.get("tracked", []):
        prev_t = prev_tracked_by_id.get(curr_t["id"])
        if (
            prev_t is not None
            and cmp_value(prev_t) != cmp_value(curr_t)
            and curr_t.get("active")
        ):
            changes["tracked_price_changes"].append(
                {
                    "id": curr_t["id"],
                    "old_total_czk": prev_t.get("total_czk"),
                    "new_total_czk": curr_t.get("total_czk"),
                }
            )

    prev_by_id = {c["id"]: c for c in prev.get("comparables", [])}
    curr_by_id = {c["id"]: c for c in curr.get("comparables", [])}

    for cid, old in prev_by_id.items():
        if cid not in curr_by_id:
            changes["newly_inactive"].append({**old, "removed_since": changes["generated_at"]})

    for cid, new in curr_by_id.items():
        old = prev_by_id.get(cid)
        if old is None:
            changes["new_listings"].append({**new, "first_seen": changes["generated_at"]})
        else:
            # Compare on total cost (rent+fees+electricity for rentals), not
            # just base price, so a fee change shows up as a price change too.
            old_cmp = cmp_value(old)
            new_cmp = cmp_value(new)
            if old_cmp != new_cmp:
                changes["price_changes"].append(
                    {
                        **new,
                        "old_price_czk": old.get("price_czk"),
                        "new_price_czk": new.get("price_czk"),
                        "old_total_czk": old.get("total_czk"),
                        "new_total_czk": new.get("total_czk"),
                    }
                )
    return changes


def compute_stats(comparables):
    def per_sqm(tx, disp_filter=None):
        vals = [
            c["price_czk_per_sqm"]
            for c in comparables
            if c["transaction_type"] == tx
            and c.get("price_czk_per_sqm")
            and (disp_filter is None or c.get("disposition") == disp_filter)
        ]
        if not vals:
            return None, None, 0
        return round(statistics.median(vals)), round(statistics.mean(vals)), len(vals)

    rent_med, rent_avg, rent_n = per_sqm("pronajem")
    sale_med, sale_avg, sale_n = per_sqm("prodej")
    return {
        "rent_median_czk_per_sqm": rent_med,
        "rent_avg_czk_per_sqm": rent_avg,
        "rent_count": rent_n,
        "sale_median_czk_per_sqm": sale_med,
        "sale_avg_czk_per_sqm": sale_avg,
        "sale_count": sale_n,
    }


def fmt_czk(v):
    if v is None:
        return "—"
    try:
        return f"{int(v):,} Kč".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def build_change_note(item_id, changes):
    for tc in changes.get("tracked_price_changes", []):
        if tc.get("id") == item_id:
            return f"Price changed: {fmt_czk(tc['old_total_czk'])} → {fmt_czk(tc['new_total_czk'])}"
    for c in changes.get("price_changes", []):
        if c["id"] == item_id:
            return f"Price changed: {fmt_czk(c['old_total_czk'])} → {fmt_czk(c['new_total_czk'])}"
    for c in changes.get("new_listings", []):
        if c["id"] == item_id:
            return "New listing since last check"
    return None


def build_tracked_item(tracked, changes):
    change_note = build_change_note(tracked["id"], changes)
    if not change_note and not tracked.get("active") and tracked.get("last_active_at"):
        change_note = f"No longer listed — showing last known details from {tracked['last_active_at']}"
    return {
        "id": tracked["id"],
        "is_seed": True,
        "title": tracked.get("title"),
        "disposition": tracked.get("disposition"),
        "transaction_type": "pronajem",
        "price_czk": tracked.get("rent_czk"),
        "total_czk": tracked.get("total_czk"),
        "fees_czk": tracked.get("fees_czk"),
        "fees_missing": tracked.get("fees_missing"),
        "fees_source": tracked.get("fees_source"),
        "electricity_czk": tracked.get("electricity_czk"),
        "electricity_estimated": tracked.get("electricity_estimated"),
        "garage": tracked.get("garage"),
        "parking": tracked.get("parking"),
        "floor_area_sqm": tracked.get("floor_area_sqm"),
        "price_czk_per_sqm": tracked.get("price_czk_per_sqm"),
        "floor_number": tracked.get("floor_number"),
        "floors_total": tracked.get("floors_total"),
        "locality": tracked.get("locality"),
        "city_part": tracked.get("city_part"),
        "street": tracked.get("street"),
        "pod_harfou": tracked.get("street") == "Pod Harfou",
        "description": tracked.get("description"),
        "seller_name": tracked.get("seller_name"),
        "images": tracked.get("images") or [],
        "thumb": tracked.get("thumb"),
        "lat": tracked.get("lat"),
        "lon": tracked.get("lon"),
        "approx_location": False,
        "url": tracked.get("url"),
        "active": tracked.get("active"),
        "change_note": change_note,
    }


def render_tracked_card(tracked):
    active_badge = (
        '<span class="badge ok">active</span>'
        if tracked.get("active")
        else '<span class="badge bad">inactive / removed</span>'
    )
    last_active_html = (
        f'<div class="modal-note" style="margin-top:8px;">Showing last known details from {tracked["last_active_at"]}</div>'
        if not tracked.get("active") and tracked.get("last_active_at")
        else ""
    )
    return f"""<div class="card seed-card" onclick="openModal({tracked['id']})">
  <img class="seed-thumb" src="{html.escape(tracked.get('thumb') or '', quote=True)}" onerror="this.style.visibility='hidden'" alt="">
  <div style="flex:1;">
    <h2 style="margin-top:0;font-size:1rem;">Tracked listing {active_badge}</h2>
    <div class="seed-grid">
      <div><b>Title</b>{html.escape(tracked.get('title') or '—')}</div>
      <div><b>Disposition</b>{html.escape(tracked.get('disposition') or '—')}</div>
      <div><b>Nájem (net)</b>{fmt_czk(tracked.get('rent_czk'))}</div>
      <div><b>Poplatky{' (z popisu)' if tracked.get('fees_source') == 'text' else ''}</b>{'neuvedeno' if tracked.get('fees_missing') else fmt_czk(tracked.get('fees_czk'))}</div>
      <div><b>Elektřina{' (odhad)' if tracked.get('electricity_estimated') else ''}</b>{fmt_czk(tracked.get('electricity_czk'))}</div>
      <div><b>Celkem</b>{fmt_czk(tracked.get('total_czk'))}</div>
      <div><b>Kč/m² (total)</b>{fmt_czk(tracked.get('price_czk_per_sqm'))}</div>
      <div><b>m²</b>{html.escape(str(tracked.get('floor_area_sqm')) if tracked.get('floor_area_sqm') is not None else '—')}</div>
      <div><b>Locality</b>{html.escape(tracked.get('locality') or '—')}</div>
    </div>
    {last_active_html}
    <div style="font-size:0.75rem;color:#7ab8ff;margin-top:6px;">Tap for full details →</div>
  </div>
</div>"""


def render_dashboard(snapshot, changes, stats, history):
    tracked_list = snapshot["tracked"]
    comparables = snapshot["comparables"]

    for c in comparables:
        c["change_note"] = build_change_note(c["id"], changes)
    tracked_items = [build_tracked_item(t, changes) for t in tracked_list]

    data_json = json.dumps(comparables, ensure_ascii=False)
    tracked_json = json.dumps(tracked_items, ensure_ascii=False)
    history_json = json.dumps(history, ensure_ascii=False)
    changed_ids = {c["id"] for c in changes.get("price_changes", [])}
    changed_ids_json = json.dumps(list(changed_ids))

    tracked_cards_html = "\n".join(render_tracked_card(t) for t in tracked_list)

    head_and_body = f"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sreality Tracker – Vysočany</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 0 0 40px;
         background: #0f1115; color: #e6e6e6; }}
  header {{ padding: 16px; background: #161922; position: sticky; top: 0; z-index: 5; }}
  h1 {{ font-size: 1.1rem; margin: 0 0 4px; }}
  .updated {{ font-size: 0.75rem; color: #888; }}
  .card {{ margin: 12px; padding: 14px; background: #1b1f29; border-radius: 10px;
          box-shadow: 0 1px 3px rgba(0,0,0,.4); }}
  .seed-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; font-size: 0.85rem; }}
  .seed-grid div b {{ display: block; color: #9aa; font-size: 0.7rem; font-weight: 500; }}
  .seed-card {{ display: flex; gap: 12px; cursor: pointer; }}
  .seed-thumb {{ width: 84px; height: 84px; object-fit: cover; border-radius: 8px; flex-shrink: 0; background: #11141b; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.7rem; }}
  .badge.ok {{ background: #1e4620; color: #6f6; }}
  .badge.bad {{ background: #4a1c1c; color: #f88; }}
  .badge.approx {{ background: #4a3c1c; color: #fc6; }}
  .src {{ display: inline-block; padding: 0 5px; margin-left: 4px; border-radius: 6px;
          font-size: 0.62rem; font-weight: 700; vertical-align: middle; border: 1px solid; }}
  .stats {{ display: flex; gap: 12px; flex-wrap: wrap; }}
  .stat {{ flex: 1; min-width: 130px; text-align: center; padding: 8px; background: #11141b; border-radius: 8px; }}
  .stat .num {{ font-size: 1.2rem; font-weight: 600; }}
  .stat .lbl {{ font-size: 0.65rem; color: #999; }}
  .controls {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 12px; }}
  select, input {{ background: #1b1f29; color: #eee; border: 1px solid #333; border-radius: 6px;
                    padding: 6px 8px; font-size: 0.85rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.8rem; }}
  th, td {{ padding: 8px 6px; text-align: left; border-bottom: 1px solid #262a33; vertical-align: middle; }}
  th {{ cursor: pointer; color: #aab; white-space: nowrap; position: sticky; top: 0; z-index: 2; background: #1b1f29; }}
  tr.changed td {{ background: #2a2410; }}
  tr.clickable-row {{ cursor: pointer; }}
  tr.clickable-row:hover td {{ background: #20242f; }}
  .thumb {{ width: 48px; height: 48px; object-fit: cover; border-radius: 6px; background: #11141b; display: block; }}
  .linklike {{ background: none; border: none; color: #7ab8ff; cursor: pointer; padding: 0; font-size: 0.8rem; text-align: left; }}
  a {{ color: #7ab8ff; text-decoration: none; }}
  .scroll {{ overflow: auto; max-height: 78vh; margin: 0 12px; }}
  .changes-list {{ font-size: 0.8rem; }}
  .changes-list li {{ margin-bottom: 4px; }}
  footer {{ text-align: center; color: #666; font-size: 0.7rem; margin-top: 24px; }}
  #map {{ height: 320px; border-radius: 8px; }}
  .leaflet-popup-content {{ color: #111; }}
  .popup-thumb {{ width: 100%; max-width: 160px; height: 100px; object-fit: cover; border-radius: 6px; display: block; margin-bottom: 6px; }}
  .popup-btn {{ display: inline-block; margin-top: 4px; padding: 4px 8px; background: #2563eb; color: #fff; border-radius: 6px; font-size: 0.75rem; cursor: pointer; border: none; }}
  #modalOverlay {{ position: fixed; inset: 0; background: rgba(0,0,0,.6); display: none;
                   align-items: flex-end; justify-content: center; z-index: 50; }}
  #modalOverlay.open {{ display: flex; }}
  #modalSheet {{ background: #161922; width: 100%; max-width: 600px; max-height: 88vh; overflow-y: auto;
                border-radius: 14px 14px 0 0; padding: 16px; box-sizing: border-box; }}
  @media (min-width: 700px) {{
    #modalOverlay {{ align-items: center; }}
    #modalSheet {{ border-radius: 14px; max-height: 80vh; }}
  }}
  #modalSheet h2 {{ margin: 0 0 8px; font-size: 1.05rem; }}
  #modalClose {{ float: right; background: none; border: none; color: #999; font-size: 1.3rem; cursor: pointer; }}
  .modal-gallery {{ display: flex; gap: 6px; overflow-x: auto; margin: 8px 0; }}
  .modal-gallery img {{ height: 140px; border-radius: 8px; object-fit: cover; flex-shrink: 0; }}
  .modal-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; font-size: 0.85rem; margin: 10px 0; }}
  .modal-grid div b {{ display: block; color: #9aa; font-size: 0.7rem; font-weight: 500; }}
  .modal-desc {{ font-size: 0.85rem; line-height: 1.4; color: #ccc; white-space: pre-wrap; }}
  .modal-note {{ background: #2a2410; color: #fc6; padding: 6px 10px; border-radius: 6px; font-size: 0.8rem; margin: 8px 0; }}
  .modal-link {{ display: inline-block; margin-top: 12px; padding: 8px 14px; background: #2563eb; color: #fff;
                 border-radius: 8px; font-size: 0.85rem; }}
  .cost-box {{ background: #11141b; border-radius: 8px; padding: 8px 10px; margin: 10px 0; font-size: 0.85rem; }}
  .cost-row {{ display: flex; justify-content: space-between; padding: 3px 0; }}
  .cost-row.total {{ border-top: 1px solid #2a2f3a; margin-top: 4px; padding-top: 6px; font-weight: 600; }}
  .cost-note {{ font-size: 0.7rem; color: #998; margin-top: 4px; }}
  .history-item {{ display: flex; align-items: center; gap: 8px; padding: 8px 0; border-bottom: 1px solid #262a33; cursor: pointer; }}
  .history-item:last-child {{ border-bottom: none; }}
  .history-item .htxt {{ flex: 1; font-size: 0.8rem; }}
  .history-item .hat {{ font-size: 0.68rem; color: #888; }}
  .history-list {{ max-height: 420px; overflow-y: auto; }}
  .hkind {{ font-size: 0.95rem; }}
</style>
</head>
<body>
<header>
  <h1>Sreality Tracker · Vysočany / Pod Harfou</h1>
  <div class="updated">Last updated: {snapshot['generated_at']}</div>
</header>

{tracked_cards_html}

<div class="card">
  <h2 style="margin-top:0;font-size:1rem;">Area stats (1+kk &amp; 2+kk, Vysočany)</h2>
  <div class="stats">
    <div class="stat"><div class="num">{fmt_czk(stats['rent_median_czk_per_sqm'])}</div><div class="lbl">rent median Kč/m² total* ({stats['rent_count']})</div></div>
    <div class="stat"><div class="num">{fmt_czk(stats['rent_avg_czk_per_sqm'])}</div><div class="lbl">rent avg Kč/m² total*</div></div>
    <div class="stat"><div class="num">{fmt_czk(stats['sale_median_czk_per_sqm'])}</div><div class="lbl">sale median Kč/m² ({stats['sale_count']})</div></div>
    <div class="stat"><div class="num">{fmt_czk(stats['sale_avg_czk_per_sqm'])}</div><div class="lbl">sale avg Kč/m²</div></div>
  </div>
  <div class="cost-note">*rent Kč/m² = nájem + poplatky + odhad elektřiny ({ELECTRICITY_ESTIMATE_CZK} Kč), not base rent alone</div>
</div>

<div class="card" id="historyCard">
  <h2 style="margin-top:0;font-size:1rem;">📜 Historie změn</h2>
  <div id="historyList" class="history-list"></div>
</div>

<div class="card" style="position:relative;z-index:0;">
  <h2 style="margin-top:0;font-size:1rem;">🗺️ Map</h2>
  <div id="map"></div>
  <div style="font-size:0.7rem;color:#888;margin-top:6px;">Solid pin = exact GPS · dashed/orange pin = approximate locality center</div>
</div>

<div class="card" id="podHarfouCard">
  <h2 style="margin-top:0;font-size:1rem;">📍 Pod Harfou (same street as tracked listing)</h2>
  <div class="scroll">
  <table id="tblPod">
    <thead>
      <tr>
        <th></th><th>Title</th><th>Type</th><th>Disp.</th><th>Nájem</th><th>Celkem</th><th>m²</th><th>Kč/m²</th><th>Odkaz</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>
  </div>
</div>

<div class="controls">
  <select id="filterTx">
    <option value="">All transactions</option>
    <option value="pronajem">Pronájem (rent)</option>
    <option value="prodej">Prodej (sale)</option>
  </select>
  <select id="filterDisp">
    <option value="">All dispositions</option>
    <option value="1+kk">1+kk</option>
    <option value="2+kk">2+kk</option>
  </select>
  <select id="filterSource">
    <option value="">All sources</option>
    <option value="sreality">Sreality</option>
    <option value="bezrealitky">Bezrealitky</option>
    <option value="idnes">iDNES</option>
  </select>
  <label style="display:flex;align-items:center;gap:6px;font-size:0.85rem;">
    <input type="checkbox" id="filterPodHarfou" style="width:auto;"> Pod Harfou only
  </label>
  <input id="search" type="text" placeholder="Search title / locality…">
</div>

<div class="scroll">
<table id="tbl">
  <thead>
    <tr>
      <th></th>
      <th data-k="title">Title</th>
      <th data-k="transaction_type">Type</th>
      <th data-k="disposition">Disp.</th>
      <th data-k="price_czk" title="Base rent (sale: purchase price)">Nájem</th>
      <th data-k="total_czk" title="Rent: nájem + poplatky + elektřina (real or estimated). Sale: purchase price.">Celkem</th>
      <th data-k="floor_area_sqm">m²</th>
      <th data-k="price_czk_per_sqm">Kč/m²</th>
      <th data-k="city_part">Locality</th>
      <th>Odkaz</th>
    </tr>
  </thead>
  <tbody></tbody>
</table>
</div>

<footer>{len(comparables)} unique comparable listings tracked · generated by scrape.py</footer>

<div id="modalOverlay" onclick="if(event.target===this) closeModal()">
  <div id="modalSheet"></div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
"""

    js_template = r"""
const TRACKED = __TRACKED_JSON__;
const DATA = __DATA_JSON__;
const HISTORY = __HISTORY_JSON__;
const ALL = [...TRACKED, ...DATA];
const CHANGED_IDS = new Set(__CHANGED_IDS_JSON__);
const ELECTRICITY_ESTIMATE_CZK = __ELECTRICITY_CZK__;
let sortKey = "price_czk_per_sqm", sortDir = 1;

const PLACEHOLDER = "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="90">' +
  '<rect width="100%" height="100%" fill="#22262f"/>' +
  '<text x="50%" y="50%" fill="#777" font-size="11" text-anchor="middle" dy=".3em">No photo</text></svg>'
);

function fmtCzk(v) {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString("cs-CZ") + " Kč";
}

function fmtTotal(r) {
  const v = r.total_czk ?? r.price_czk;
  const txt = fmtCzk(v);
  return r.transaction_type === "pronajem" ? txt + (r.fees_missing ? "*" : "") : txt;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function srcBadge(s) {
  const map = { sreality: ["SR", "#7ab8ff"], bezrealitky: ["BR", "#6fd08c"], idnes: ["iD", "#f2a65a"] };
  if (!s || !map[s]) return "";
  const [lbl, col] = map[s];
  return ` <span class="src" style="color:${col};border-color:${col}66;">${lbl}</span>`;
}

function portalName(s) {
  return ({ sreality: "Sreality", bezrealitky: "Bezrealitky", idnes: "iDNES" })[s] || "Sreality";
}

function costBreakdownHtml(item) {
  const adminRow = item.admin_fee_czk
    ? `<div class="cost-row"><span>📋 Administrativní poplatek (jednorázově)</span><span>${fmtCzk(item.admin_fee_czk)}</span></div>`
    : "";
  if (item.transaction_type !== "pronajem") {
    return `<div class="cost-box"><div class="cost-row total"><span>💰 Cena</span><span>${fmtCzk(item.price_czk)}</span></div>${adminRow}</div>`;
  }
  const feesHtml = item.fees_missing
    ? `<span style="color:#998;">neuvedeno listingem</span>`
    : fmtCzk(item.fees_czk) + (item.fees_source === "text" ? ' <i style="color:#888;font-size:0.7rem;">(z popisu)</i>' : '');
  const elecNote = item.electricity_estimated
    ? `<div class="cost-note">Elektřina není u tohoto inzerátu uvedena přesně -- jednotný odhad ${ELECTRICITY_ESTIMATE_CZK} Kč/měsíc pro srovnatelnost.</div>`
    : `<div class="cost-note">Elektřina dle částky uvedené v inzerátu.</div>`;
  return `<div class="cost-box">
    <div class="cost-row"><span>🏠 Nájem (net)</span><span>${fmtCzk(item.price_czk)}</span></div>
    <div class="cost-row"><span>🧾 Poplatky / služby</span><span>${feesHtml}</span></div>
    <div class="cost-row"><span>⚡ Elektřina${item.electricity_estimated ? " (odhad)" : ""}</span><span>${fmtCzk(item.electricity_czk)}</span></div>
    <div class="cost-row total"><span>💰 Celkem (s elektřinou)</span><span>${fmtCzk(item.total_czk)}</span></div>
    ${adminRow}
    ${item.fees_missing ? '<div class="cost-note">Poplatky/služby nejsou u tohoto inzerátu uvedeny -- do celkové ceny započteny jako 0 navíc k odhadu elektřiny.</div>' : ''}
    ${elecNote}
  </div>`;
}

function garageParkingHtml(item) {
  const fmt = v => v === true ? "Ano" : v === false ? "Ne" : "neuvedeno";
  return `<div><b>Garáž</b>${fmt(item.garage)}</div><div><b>Parkování</b>${fmt(item.parking)}</div>`;
}

function buildModalHtml(item) {
  const gallery = (item.images && item.images.length)
    ? item.images.map(u => `<img src="${escapeHtml(u)}" loading="lazy">`).join("")
    : `<img src="${PLACEHOLDER}">`;
  const floorLine = item.floor_number != null ? `${item.floor_number}/${item.floors_total ?? "?"}` : "—";
  const noteHtml = item.change_note ? `<div class="modal-note">⚡ ${escapeHtml(item.change_note)}</div>` : "";
  const approxHtml = item.approx_location ? `<span class="badge approx">approximate location</span>` : "";
  return `
    <button id="modalClose" onclick="closeModal()">&times;</button>
    <h2>${escapeHtml(item.title || "Listing")} ${approxHtml}</h2>
    ${noteHtml}
    <div class="modal-gallery">${gallery}</div>
    ${costBreakdownHtml(item)}
    <div class="modal-grid">
      <div><b>Kč/m²${item.transaction_type === "pronajem" ? " (total)" : ""}</b>${fmtCzk(item.price_czk_per_sqm)}</div>
      <div><b>Disposition</b>${item.disposition || "—"}</div>
      <div><b>m²</b>${item.floor_area_sqm ?? "—"}</div>
      <div><b>Floor</b>${floorLine}</div>
      <div><b>Type</b>${item.transaction_type === "pronajem" ? "Rent" : "Sale"}</div>
      <div><b>Locality</b>${escapeHtml(item.locality || item.city_part || "—")}</div>
      ${garageParkingHtml(item)}
      <div><b>Seller / agent</b>${escapeHtml(item.seller_name || "—")}</div>
    </div>
    <div class="modal-desc">${escapeHtml(item.description || "No description available.")}</div>
    <a class="modal-link" href="${escapeHtml(item.url)}" target="_blank" rel="noopener">Otevřít na ${portalName(item.source)} →</a>
  `;
}

function openModal(id) {
  const item = ALL.find(r => r.id === id);
  if (!item) return;
  document.getElementById("modalSheet").innerHTML = buildModalHtml(item);
  document.getElementById("modalOverlay").classList.add("open");
}

function openHistoryItem(idx) {
  const ev = HISTORY[idx];
  if (!ev) return;
  const liveItem = ALL.find(r => r.id === ev.id);
  const item = liveItem || ev.item;
  if (!item) return;
  document.getElementById("modalSheet").innerHTML = buildModalHtml(item);
  document.getElementById("modalOverlay").classList.add("open");
}

function closeModal() {
  document.getElementById("modalOverlay").classList.remove("open");
}

function renderHistory() {
  const list = document.getElementById("historyList");
  if (!HISTORY.length) {
    list.innerHTML = `<div style="color:#888;font-size:0.8rem;">No changes recorded yet.</div>`;
    return;
  }
  list.innerHTML = HISTORY.map((ev, idx) => {
    const item = ev.item || {};
    const thumb = item.thumb || PLACEHOLDER;
    let icon = "🆕", text = "";
    if (ev.kind === "new") {
      icon = "🆕";
      text = `New: ${escapeHtml(item.title || ev.id)} — ${fmtCzk(item.total_czk ?? item.price_czk)}`;
    } else if (ev.kind === "removed") {
      icon = "❌";
      text = `Gone: ${escapeHtml(item.title || ev.id)} — last seen at ${fmtCzk(item.total_czk ?? item.price_czk)}`;
    } else {
      icon = "💰";
      const oldP = ev.old_total_czk ?? ev.old_price_czk;
      const newP = ev.new_total_czk ?? ev.new_price_czk;
      text = `${escapeHtml((item && item.title) || ("#" + ev.id))}: ${fmtCzk(oldP)} → ${fmtCzk(newP)}`;
    }
    return `<div class="history-item" onclick="openHistoryItem(${idx})">
      <img class="thumb" src="${escapeHtml(thumb)}" loading="lazy" onerror="this.src='${PLACEHOLDER}'">
      <div class="htxt">${text}<div class="hat">${escapeHtml(ev.at || "")}</div></div>
      <div class="hkind">${icon}</div>
    </div>`;
  }).join("");
}

function renderPodHarfou() {
  const rows = DATA.filter(r => r.pod_harfou);
  const tbody = document.querySelector("#tblPod tbody");
  tbody.innerHTML = rows.length ? rows.map(r => `
    <tr class="clickable-row ${CHANGED_IDS.has(r.id) ? 'changed' : ''}" onclick="openModal(${escapeHtml(JSON.stringify(r.id))})">
      <td><img class="thumb" src="${escapeHtml(r.thumb || PLACEHOLDER)}" loading="lazy" onerror="this.src=PLACEHOLDER"></td>
      <td><button class="linklike" onclick="event.stopPropagation();openModal(${escapeHtml(JSON.stringify(r.id))})">${escapeHtml(r.title) || '—'}</button></td>
      <td>${r.transaction_type === 'pronajem' ? 'rent' : 'sale'}</td>
      <td>${r.disposition || '—'}</td>
      <td>${fmtCzk(r.price_czk)}</td>
      <td>${fmtTotal(r)}</td>
      <td>${r.floor_area_sqm ?? '—'}</td>
      <td>${fmtCzk(r.price_czk_per_sqm)}</td>
      <td>${r.url ? `<a href="${escapeHtml(r.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Otevřít ↗</a>` : '—'}</td>
    </tr>`).join("") : `<tr><td colspan="9" style="color:#888;">No other Pod Harfou listings currently found.</td></tr>`;
}

function render() {
  const tx = document.getElementById("filterTx").value;
  const disp = document.getElementById("filterDisp").value;
  const source = document.getElementById("filterSource").value;
  const podOnly = document.getElementById("filterPodHarfou").checked;
  const q = document.getElementById("search").value.toLowerCase();
  let rows = DATA.filter(r => {
    if (tx && r.transaction_type !== tx) return false;
    if (disp && r.disposition !== disp) return false;
    if (source && r.source !== source) return false;
    if (podOnly && !r.pod_harfou) return false;
    if (q && !((r.title||"").toLowerCase().includes(q) || (r.city_part||"").toLowerCase().includes(q) || (r.locality||"").toLowerCase().includes(q))) return false;
    return true;
  });
  rows.sort((a, b) => {
    let av = a[sortKey], bv = b[sortKey];
    if (av === null || av === undefined) av = -Infinity;
    if (bv === null || bv === undefined) bv = -Infinity;
    if (av < bv) return -1 * sortDir;
    if (av > bv) return 1 * sortDir;
    return 0;
  });
  const tbody = document.querySelector("#tbl tbody");
  tbody.innerHTML = rows.map(r => `
    <tr class="clickable-row ${CHANGED_IDS.has(r.id) ? 'changed' : ''}" onclick="openModal(${escapeHtml(JSON.stringify(r.id))})">
      <td><img class="thumb" src="${escapeHtml(r.thumb || PLACEHOLDER)}" loading="lazy" onerror="this.src=PLACEHOLDER"></td>
      <td><button class="linklike" onclick="event.stopPropagation();openModal(${escapeHtml(JSON.stringify(r.id))})">${escapeHtml(r.title) || '—'}</button></td>
      <td>${r.transaction_type === 'pronajem' ? 'rent' : 'sale'}</td>
      <td>${r.disposition || '—'}</td>
      <td>${fmtCzk(r.price_czk)}</td>
      <td>${fmtTotal(r)}</td>
      <td>${r.floor_area_sqm ?? '—'}</td>
      <td>${fmtCzk(r.price_czk_per_sqm)}${CHANGED_IDS.has(r.id) ? ' ⚡' : ''}</td>
      <td>${escapeHtml(r.locality || r.city_part || '—')}${srcBadge(r.source)}</td>
      <td>${r.url ? `<a href="${escapeHtml(r.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Otevřít ↗</a>` : '—'}</td>
    </tr>`).join("");
}

function initMap() {
  const center = TRACKED.find(t => t.lat != null) || DATA.find(d => d.lat != null);
  if (!center) return;
  const map = L.map("map").setView([center.lat, center.lon], 14);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  function popupHtml(item) {
    const thumb = item.thumb || (item.images && item.images[0]) || PLACEHOLDER;
    const price = item.transaction_type === "pronajem" ? fmtTotal(item) + "/mo" : fmtCzk(item.price_czk);
    return `<div style="min-width:150px;">
      <img class="popup-thumb" src="${escapeHtml(thumb)}" onerror="this.src='${PLACEHOLDER}'">
      <div style="font-weight:600;font-size:0.85rem;">${escapeHtml(item.title || "Listing")}</div>
      <div style="font-size:0.8rem;">${price}</div>
      <button class="popup-btn" onclick="openModal(${escapeHtml(JSON.stringify(item.id))})">View details</button>
      <div><a href="${escapeHtml(item.url)}" target="_blank" rel="noopener" style="font-size:0.7rem;">Otevřít na ${portalName(item.source)} →</a></div>
    </div>`;
  }

  ALL.forEach(item => {
    if (item.lat == null || item.lon == null) return;
    let marker;
    if (item.approx_location) {
      marker = L.circleMarker([item.lat, item.lon], {
        radius: 8, color: "#fc6", weight: 2, dashArray: "3,3", fillColor: "#fc6", fillOpacity: 0.35,
      });
    } else if (item.is_seed) {
      marker = L.circleMarker([item.lat, item.lon], {
        radius: 9, color: "#3aff7a", weight: 3, fillColor: "#3aff7a", fillOpacity: 0.6,
      });
    } else {
      marker = L.circleMarker([item.lat, item.lon], {
        radius: 7, color: item.transaction_type === "pronajem" ? "#7ab8ff" : "#ff9a4d",
        weight: 2, fillColor: item.transaction_type === "pronajem" ? "#7ab8ff" : "#ff9a4d", fillOpacity: 0.5,
      });
    }
    marker.bindPopup(popupHtml(item));
    marker.addTo(map);
  });
}

document.querySelectorAll("#tbl th[data-k]").forEach(th => {
  th.addEventListener("click", () => {
    const k = th.dataset.k;
    if (sortKey === k) sortDir *= -1; else { sortKey = k; sortDir = 1; }
    render();
  });
});
document.getElementById("filterTx").addEventListener("change", render);
document.getElementById("filterDisp").addEventListener("change", render);
document.getElementById("filterSource").addEventListener("change", render);
document.getElementById("filterPodHarfou").addEventListener("change", render);
document.getElementById("search").addEventListener("input", render);
render();
renderPodHarfou();
renderHistory();
initMap();
"""

    js = (
        js_template.replace("__TRACKED_JSON__", tracked_json)
        .replace("__HISTORY_JSON__", history_json)
        .replace("__ELECTRICITY_CZK__", str(ELECTRICITY_ESTIMATE_CZK))
        .replace("__DATA_JSON__", data_json)
        .replace("__CHANGED_IDS_JSON__", changed_ids_json)
    )

    html = head_and_body + js + "</script>\n</body>\n</html>\n"
    DASHBOARD_PATH.write_text(html, encoding="utf-8")


def main():
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    prev = load_latest_snapshot()
    prev_tracked_by_id = {t["id"]: t for t in (prev or {}).get("tracked", [])}

    tracked_config = load_tracked_config()
    print(f"Fetching {len(tracked_config)} tracked listing(s)...", file=sys.stderr)
    tracked = []
    for t in tracked_config:
        fetched = fetch_tracked(t["url"], t["id"])
        if not fetched.get("active"):
            prev_t = prev_tracked_by_id.get(t["id"])
            if prev_t:
                # Carry forward the last time it was *actually* seen active, even
                # across multiple consecutive inactive runs (prev_t may itself
                # already be a backfilled record with no fresh active sighting).
                last_active_at = prev_t.get("last_active_at") or (
                    prev_t.get("fetched_at") if prev_t.get("active") else None
                )
                fetched = {**prev_t, **fetched}
                if last_active_at:
                    fetched["last_active_at"] = last_active_at
        tracked.append(fetched)
        print(f"Tracked id={fetched['id']} active={fetched.get('active')} title={fetched.get('title')!r}", file=sys.stderr)

    print("Fetching comparables...", file=sys.stderr)
    comparables = fetch_comparables()
    for c in comparables:
        c.setdefault("source", "sreality")
    print("Fetching extra sources (Bezrealitky, iDNES)...", file=sys.stderr)
    comparables += sources.fetch_extra_comparables()
    print(f"Found {len(comparables)} unique comparable listings", file=sys.stderr)

    snapshot = {
        "generated_at": now_iso(),
        "tracked": tracked,
        "comparables": comparables,
    }

    changes = diff_snapshots(prev, snapshot)
    stats = compute_stats(comparables)
    snapshot["stats"] = stats
    history = update_changes_history(changes)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_path = SNAPSHOTS_DIR / f"snapshot-{ts}.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    LATEST_SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    CHANGES_PATH.write_text(json.dumps(changes, ensure_ascii=False, indent=2))

    render_dashboard(snapshot, changes, stats, history)

    print(f"Snapshot saved: {snapshot_path}", file=sys.stderr)
    print(f"Stats: {stats}", file=sys.stderr)
    print(
        f"Changes: tracked_price_changes={len(changes['tracked_price_changes'])} "
        f"new={len(changes['new_listings'])} gone={len(changes['newly_inactive'])} "
        f"price_changes={len(changes['price_changes'])}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
