#!/usr/bin/env python3
"""Sreality.cz property monitor: scrapes a seed listing plus comparable
listings in the same area, snapshots the result, diffs against the previous
snapshot, and regenerates a mobile-friendly dashboard with photos and a map."""
import json
import re
import statistics
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
SNAPSHOTS_DIR = ROOT / "snapshots"
DASHBOARD_PATH = ROOT / "dashboard.html"
CHANGES_PATH = ROOT / "last_changes.json"
LATEST_SNAPSHOT_PATH = ROOT / "latest_snapshot.json"

SEED_URL = "https://www.sreality.cz/detail/pronajem/byt/1+kk/praha-vysocany-pod-harfou/3182461004"
SEED_ID = 3182461004

# Sreality category_sub_cb codes (from /hledani estatesFilterPage)
DISPOSITION_CODES = {2: "1+kk", 4: "2+kk"}
TRANSACTION_TYPES = ["pronajem", "prodej"]  # rent, sale
SEARCH_REGION_TEXT = "Vysočany"  # free-text resolved server-side to the ward/locality
MAX_IMAGES_PER_LISTING = 5
MAX_DESCRIPTION_CHARS = 1200

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


def fetch_seed():
    next_data, status = fetch_next_data(SEED_URL)
    if next_data is None:
        return {
            "id": SEED_ID,
            "url": SEED_URL,
            "active": False,
            "fetched_at": now_iso(),
            "error": f"HTTP {status}",
        }
    data, _ = get_query_data(next_data, "estate")
    if data is None:
        return {
            "id": SEED_ID,
            "url": SEED_URL,
            "active": False,
            "fetched_at": now_iso(),
            "error": "estate query missing from page",
        }
    params = data.get("params") or {}
    locality = data.get("locality") or {}
    seller = data.get("seller") or {}
    premise = data.get("premise") or {}
    rent_czk = data.get("priceCzk")
    fees_czk = params.get("costOfLiving")
    try:
        fees_czk = int(fees_czk) if fees_czk is not None else None
    except (TypeError, ValueError):
        fees_czk = None
    total_czk = None
    if rent_czk is not None and fees_czk is not None:
        total_czk = rent_czk + fees_czk

    return {
        "id": SEED_ID,
        "url": SEED_URL,
        "active": True,
        "fetched_at": now_iso(),
        "title": data.get("name"),
        "disposition": (data.get("categorySubCb") or {}).get("name"),
        "transaction_type": (data.get("categoryTypeCb") or {}).get("name"),
        "rent_czk": rent_czk,
        "fees_czk": fees_czk,
        "total_czk": total_czk,
        "price_czk_per_sqm": data.get("priceCzkPerSqM"),
        "floor_area_sqm": params.get("floorArea"),
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
    }


def enrich_comparable(comp):
    """Fetch the full detail page for description/seller/photos. GPS and a
    thumbnail already came from the search payload, so this is best-effort."""
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


def diff_snapshots(prev, curr):
    changes = {
        "generated_at": now_iso(),
        "seed_price_change": None,
        "newly_inactive": [],
        "new_listings": [],
        "price_changes": [],
    }
    if prev is None:
        return changes

    prev_seed = prev.get("seed") or {}
    curr_seed = curr.get("seed") or {}
    if prev_seed.get("total_czk") != curr_seed.get("total_czk") and curr_seed.get(
        "active"
    ):
        changes["seed_price_change"] = {
            "id": SEED_ID,
            "old_total_czk": prev_seed.get("total_czk"),
            "new_total_czk": curr_seed.get("total_czk"),
        }

    prev_by_id = {c["id"]: c for c in prev.get("comparables", [])}
    curr_by_id = {c["id"]: c for c in curr.get("comparables", [])}

    for cid, old in prev_by_id.items():
        if cid not in curr_by_id:
            changes["newly_inactive"].append(
                {"id": cid, "title": old.get("title"), "url": old.get("url")}
            )

    for cid, new in curr_by_id.items():
        old = prev_by_id.get(cid)
        if old is None:
            changes["new_listings"].append(
                {"id": cid, "title": new.get("title"), "url": new.get("url")}
            )
        elif old.get("price_czk") != new.get("price_czk"):
            changes["price_changes"].append(
                {
                    "id": cid,
                    "title": new.get("title"),
                    "old_price_czk": old.get("price_czk"),
                    "new_price_czk": new.get("price_czk"),
                    "url": new.get("url"),
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
    sc = changes.get("seed_price_change")
    if sc and sc.get("id") == item_id:
        return f"Price changed: {fmt_czk(sc['old_total_czk'])} → {fmt_czk(sc['new_total_czk'])}"
    for c in changes.get("price_changes", []):
        if c["id"] == item_id:
            return f"Price changed: {fmt_czk(c['old_price_czk'])} → {fmt_czk(c['new_price_czk'])}"
    for c in changes.get("new_listings", []):
        if c["id"] == item_id:
            return "New listing since last check"
    return None


def build_seed_item(seed, changes):
    return {
        "id": seed["id"],
        "is_seed": True,
        "title": seed.get("title"),
        "disposition": seed.get("disposition"),
        "transaction_type": "pronajem",
        "price_czk": seed.get("rent_czk"),
        "total_czk": seed.get("total_czk"),
        "fees_czk": seed.get("fees_czk"),
        "floor_area_sqm": seed.get("floor_area_sqm"),
        "price_czk_per_sqm": seed.get("price_czk_per_sqm"),
        "floor_number": seed.get("floor_number"),
        "floors_total": seed.get("floors_total"),
        "locality": seed.get("locality"),
        "city_part": seed.get("city_part"),
        "street": seed.get("street"),
        "pod_harfou": True,
        "description": seed.get("description"),
        "seller_name": seed.get("seller_name"),
        "images": seed.get("images") or [],
        "thumb": seed.get("thumb"),
        "lat": seed.get("lat"),
        "lon": seed.get("lon"),
        "approx_location": False,
        "url": seed.get("url"),
        "active": seed.get("active"),
        "change_note": build_change_note(seed["id"], changes),
    }


def render_dashboard(snapshot, changes, stats):
    seed = snapshot["seed"]
    comparables = snapshot["comparables"]

    for c in comparables:
        c["change_note"] = build_change_note(c["id"], changes)
    seed_item = build_seed_item(seed, changes)

    data_json = json.dumps(comparables, ensure_ascii=False)
    seed_json = json.dumps(seed_item, ensure_ascii=False)
    changed_ids = {c["id"] for c in changes.get("price_changes", [])}
    changed_ids_json = json.dumps(list(changed_ids))

    seed_active_badge = (
        '<span class="badge ok">active</span>'
        if seed.get("active")
        else '<span class="badge bad">inactive / removed</span>'
    )

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
  .stats {{ display: flex; gap: 12px; flex-wrap: wrap; }}
  .stat {{ flex: 1; min-width: 130px; text-align: center; padding: 8px; background: #11141b; border-radius: 8px; }}
  .stat .num {{ font-size: 1.2rem; font-weight: 600; }}
  .stat .lbl {{ font-size: 0.65rem; color: #999; }}
  .controls {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 12px; }}
  select, input {{ background: #1b1f29; color: #eee; border: 1px solid #333; border-radius: 6px;
                    padding: 6px 8px; font-size: 0.85rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.8rem; }}
  th, td {{ padding: 8px 6px; text-align: left; border-bottom: 1px solid #262a33; vertical-align: middle; }}
  th {{ cursor: pointer; color: #aab; white-space: nowrap; position: sticky; top: 0; background: #1b1f29; }}
  tr.changed td {{ background: #2a2410; }}
  tr.clickable-row {{ cursor: pointer; }}
  tr.clickable-row:hover td {{ background: #20242f; }}
  .thumb {{ width: 48px; height: 48px; object-fit: cover; border-radius: 6px; background: #11141b; display: block; }}
  .linklike {{ background: none; border: none; color: #7ab8ff; cursor: pointer; padding: 0; font-size: 0.8rem; text-align: left; }}
  a {{ color: #7ab8ff; text-decoration: none; }}
  .scroll {{ overflow-x: auto; margin: 0 12px; }}
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
</style>
</head>
<body>
<header>
  <h1>Sreality Tracker · Vysočany / Pod Harfou</h1>
  <div class="updated">Last updated: {snapshot['generated_at']}</div>
</header>

<div class="card seed-card" id="seedCard" onclick="openModal({seed['id']})">
  <img class="seed-thumb" src="{seed.get('thumb') or ''}" onerror="this.style.visibility='hidden'" alt="">
  <div style="flex:1;">
    <h2 style="margin-top:0;font-size:1rem;">Tracked listing {seed_active_badge}</h2>
    <div class="seed-grid">
      <div><b>Title</b>{seed.get('title') or '—'}</div>
      <div><b>Disposition</b>{seed.get('disposition') or '—'}</div>
      <div><b>Total / month</b>{fmt_czk(seed.get('total_czk'))}</div>
      <div><b>Kč/m²</b>{fmt_czk(seed.get('price_czk_per_sqm'))}</div>
      <div><b>m²</b>{seed.get('floor_area_sqm') or '—'}</div>
      <div><b>Locality</b>{seed.get('locality') or '—'}</div>
    </div>
    <div style="font-size:0.75rem;color:#7ab8ff;margin-top:6px;">Tap for full details →</div>
  </div>
</div>

<div class="card">
  <h2 style="margin-top:0;font-size:1rem;">Area stats (1+kk &amp; 2+kk, Vysočany)</h2>
  <div class="stats">
    <div class="stat"><div class="num">{fmt_czk(stats['rent_median_czk_per_sqm'])}</div><div class="lbl">rent median Kč/m² ({stats['rent_count']})</div></div>
    <div class="stat"><div class="num">{fmt_czk(stats['rent_avg_czk_per_sqm'])}</div><div class="lbl">rent avg Kč/m²</div></div>
    <div class="stat"><div class="num">{fmt_czk(stats['sale_median_czk_per_sqm'])}</div><div class="lbl">sale median Kč/m² ({stats['sale_count']})</div></div>
    <div class="stat"><div class="num">{fmt_czk(stats['sale_avg_czk_per_sqm'])}</div><div class="lbl">sale avg Kč/m²</div></div>
  </div>
</div>

{render_changes_card(changes)}

<div class="card">
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
        <th></th><th>Title</th><th>Type</th><th>Disp.</th><th>Price</th><th>m²</th><th>Kč/m²</th>
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
      <th data-k="price_czk">Price</th>
      <th data-k="floor_area_sqm">m²</th>
      <th data-k="price_czk_per_sqm">Kč/m²</th>
      <th data-k="city_part">Locality</th>
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
const SEED = __SEED_JSON__;
const DATA = __DATA_JSON__;
const ALL = [SEED, ...DATA];
const CHANGED_IDS = new Set(__CHANGED_IDS_JSON__);
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

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function openModal(id) {
  const item = ALL.find(r => r.id === id);
  if (!item) return;
  const gallery = (item.images && item.images.length)
    ? item.images.map(u => `<img src="${u}" loading="lazy">`).join("")
    : `<img src="${PLACEHOLDER}">`;
  const priceLine = item.is_seed
    ? `${fmtCzk(item.total_czk)} / month <span style="color:#888;font-size:0.75rem;">(rent ${fmtCzk(item.price_czk)} + fees ${fmtCzk(item.fees_czk)})</span>`
    : (item.transaction_type === "pronajem" ? fmtCzk(item.price_czk) + " / month" : fmtCzk(item.price_czk));
  const floorLine = item.floor_number != null ? `${item.floor_number}/${item.floors_total ?? "?"}` : "—";
  const noteHtml = item.change_note ? `<div class="modal-note">⚡ ${escapeHtml(item.change_note)}</div>` : "";
  const approxHtml = item.approx_location ? `<span class="badge approx">approximate location</span>` : "";
  document.getElementById("modalSheet").innerHTML = `
    <button id="modalClose" onclick="closeModal()">&times;</button>
    <h2>${escapeHtml(item.title || "Listing")} ${approxHtml}</h2>
    ${noteHtml}
    <div class="modal-gallery">${gallery}</div>
    <div class="modal-grid">
      <div><b>Price</b>${priceLine}</div>
      <div><b>Kč/m²</b>${fmtCzk(item.price_czk_per_sqm)}</div>
      <div><b>Disposition</b>${item.disposition || "—"}</div>
      <div><b>m²</b>${item.floor_area_sqm ?? "—"}</div>
      <div><b>Floor</b>${floorLine}</div>
      <div><b>Type</b>${item.transaction_type === "pronajem" ? "Rent" : "Sale"}</div>
      <div><b>Locality</b>${escapeHtml(item.locality || item.city_part || "—")}</div>
      <div><b>Seller / agent</b>${escapeHtml(item.seller_name || "—")}</div>
    </div>
    <div class="modal-desc">${escapeHtml(item.description || "No description available.")}</div>
    <a class="modal-link" href="${item.url}" target="_blank" rel="noopener">Otevřít na Sreality →</a>
  `;
  document.getElementById("modalOverlay").classList.add("open");
}

function closeModal() {
  document.getElementById("modalOverlay").classList.remove("open");
}

function renderPodHarfou() {
  const rows = DATA.filter(r => r.pod_harfou);
  const tbody = document.querySelector("#tblPod tbody");
  tbody.innerHTML = rows.length ? rows.map(r => `
    <tr class="clickable-row ${CHANGED_IDS.has(r.id) ? 'changed' : ''}" onclick="openModal(${r.id})">
      <td><img class="thumb" src="${r.thumb || PLACEHOLDER}" loading="lazy" onerror="this.src=PLACEHOLDER"></td>
      <td><button class="linklike" onclick="event.stopPropagation();openModal(${r.id})">${escapeHtml(r.title) || '—'}</button></td>
      <td>${r.transaction_type === 'pronajem' ? 'rent' : 'sale'}</td>
      <td>${r.disposition || '—'}</td>
      <td>${fmtCzk(r.price_czk)}</td>
      <td>${r.floor_area_sqm ?? '—'}</td>
      <td>${fmtCzk(r.price_czk_per_sqm)}</td>
    </tr>`).join("") : `<tr><td colspan="7" style="color:#888;">No other Pod Harfou listings currently found.</td></tr>`;
}

function render() {
  const tx = document.getElementById("filterTx").value;
  const disp = document.getElementById("filterDisp").value;
  const podOnly = document.getElementById("filterPodHarfou").checked;
  const q = document.getElementById("search").value.toLowerCase();
  let rows = DATA.filter(r => {
    if (tx && r.transaction_type !== tx) return false;
    if (disp && r.disposition !== disp) return false;
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
    <tr class="clickable-row ${CHANGED_IDS.has(r.id) ? 'changed' : ''}" onclick="openModal(${r.id})">
      <td><img class="thumb" src="${r.thumb || PLACEHOLDER}" loading="lazy" onerror="this.src=PLACEHOLDER"></td>
      <td><button class="linklike" onclick="event.stopPropagation();openModal(${r.id})">${escapeHtml(r.title) || '—'}</button></td>
      <td>${r.transaction_type === 'pronajem' ? 'rent' : 'sale'}</td>
      <td>${r.disposition || '—'}</td>
      <td>${fmtCzk(r.price_czk)}</td>
      <td>${r.floor_area_sqm ?? '—'}</td>
      <td>${fmtCzk(r.price_czk_per_sqm)}${CHANGED_IDS.has(r.id) ? ' ⚡' : ''}</td>
      <td>${escapeHtml(r.locality || r.city_part || '—')}</td>
    </tr>`).join("");
}

function initMap() {
  if (!SEED.lat) return;
  const map = L.map("map").setView([SEED.lat, SEED.lon], 14);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  function popupHtml(item) {
    const thumb = item.thumb || (item.images && item.images[0]) || PLACEHOLDER;
    const price = item.is_seed ? fmtCzk(item.total_czk) + "/mo" : fmtCzk(item.price_czk);
    return `<div style="min-width:150px;">
      <img class="popup-thumb" src="${thumb}" onerror="this.src='${PLACEHOLDER}'">
      <div style="font-weight:600;font-size:0.85rem;">${escapeHtml(item.title || "Listing")}</div>
      <div style="font-size:0.8rem;">${price}</div>
      <button class="popup-btn" onclick="openModal(${item.id})">View details</button>
      <div><a href="${item.url}" target="_blank" rel="noopener" style="font-size:0.7rem;">Otevřít na Sreality →</a></div>
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
document.getElementById("filterPodHarfou").addEventListener("change", render);
document.getElementById("search").addEventListener("input", render);
render();
renderPodHarfou();
initMap();
"""

    js = (
        js_template.replace("__SEED_JSON__", seed_json)
        .replace("__DATA_JSON__", data_json)
        .replace("__CHANGED_IDS_JSON__", changed_ids_json)
    )

    html = head_and_body + js + "</script>\n</body>\n</html>\n"
    DASHBOARD_PATH.write_text(html, encoding="utf-8")


def render_changes_card(changes):
    if not any(
        [
            changes.get("seed_price_change"),
            changes.get("newly_inactive"),
            changes.get("new_listings"),
            changes.get("price_changes"),
        ]
    ):
        return ""
    items = []
    if changes.get("seed_price_change"):
        sc = changes["seed_price_change"]
        items.append(
            f"<li>Tracked listing price changed: {fmt_czk(sc['old_total_czk'])} → {fmt_czk(sc['new_total_czk'])}</li>"
        )
    for c in changes.get("price_changes", [])[:20]:
        items.append(
            f"<li>{c.get('title') or c['id']}: {fmt_czk(c['old_price_czk'])} → {fmt_czk(c['new_price_czk'])}</li>"
        )
    for c in changes.get("new_listings", [])[:20]:
        items.append(f"<li>New: <a href=\"{c.get('url')}\">{c.get('title') or c['id']}</a></li>")
    for c in changes.get("newly_inactive", [])[:20]:
        items.append(f"<li>Removed/inactive: {c.get('title') or c['id']}</li>")
    return f"""<div class="card">
  <h2 style="margin-top:0;font-size:1rem;">Changes since last run</h2>
  <ul class="changes-list">{''.join(items)}</ul>
</div>"""


def main():
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    print("Fetching seed listing...", file=sys.stderr)
    seed = fetch_seed()
    print(f"Seed active={seed.get('active')} title={seed.get('title')!r}", file=sys.stderr)

    print("Fetching comparables...", file=sys.stderr)
    comparables = fetch_comparables()
    print(f"Found {len(comparables)} unique comparable listings", file=sys.stderr)

    snapshot = {
        "generated_at": now_iso(),
        "seed": seed,
        "comparables": comparables,
    }

    prev = load_latest_snapshot()
    changes = diff_snapshots(prev, snapshot)
    stats = compute_stats(comparables)
    snapshot["stats"] = stats

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_path = SNAPSHOTS_DIR / f"snapshot-{ts}.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    LATEST_SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    CHANGES_PATH.write_text(json.dumps(changes, ensure_ascii=False, indent=2))

    render_dashboard(snapshot, changes, stats)

    print(f"Snapshot saved: {snapshot_path}", file=sys.stderr)
    print(f"Stats: {stats}", file=sys.stderr)
    print(
        f"Changes: seed_price_change={bool(changes['seed_price_change'])} "
        f"new={len(changes['new_listings'])} gone={len(changes['newly_inactive'])} "
        f"price_changes={len(changes['price_changes'])}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
