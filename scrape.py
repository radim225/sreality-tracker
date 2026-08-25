#!/usr/bin/env python3
"""Sreality.cz property monitor: scrapes a seed listing plus comparable
listings in the same area, snapshots the result, diffs against the previous
snapshot, and regenerates a mobile-friendly dashboard with photos and a map."""
import html
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

import market
import notify
import pool
import report
import sources

ROOT = Path(__file__).parent
SNAPSHOTS_DIR = ROOT / "snapshots"
DASHBOARD_PATH = ROOT / "dashboard.html"
CHANGES_PATH = ROOT / "last_changes.json"
LATEST_SNAPSHOT_PATH = ROOT / "latest_snapshot.json"
TRACKED_PATH = ROOT / "tracked.json"
CHANGES_HISTORY_PATH = ROOT / "changes_history.json"
# The same events, uncapped and append-only. changes_history.json is what the
# dashboard inlines into the page, so it has to stay small -- but at ~150 events
# a day its 300-event cap spans about two days, and a weekly write-up asking
# "what happened this week" would silently see a third of it. The log is the
# record; the JSON file is a view of the most recent slice of it.
CHANGES_LOG_PATH = ROOT / "changes_log.jsonl"

# Sreality category_sub_cb codes (from /hledani estatesFilterPage)
DISPOSITION_CODES = {2: "1+kk", 3: "1+1", 4: "2+kk", 5: "2+1", 6: "3+kk", 7: "3+1"}
# The same set as a "velikost" query param. Sreality accepts it server-side and
# resolves it to exactly these categorySubCb codes, so the search returns only
# relevant dispositions instead of every flat in the ward -- roughly halving the
# number of search pages we have to walk.
SEARCH_VELIKOST = ",".join(DISPOSITION_CODES.values())
TRANSACTION_TYPES = ["pronajem", "prodej"]  # rent, sale

# The watched area is a circle, not a ward list: Radim's landmarks straddle
# ward boundaries (Hrdlořezy sits on the Praha 3/9 line, the Rokytka cycle path
# runs Vysočany->Libeň->Karlín). So we search every ward the circle touches and
# then keep only what actually falls inside it.
#
# Centre sits between Palmovka and Hrdlořezy, chosen so one radius covers every
# place he named while still cutting off eastwards. Distances from the centre:
#   Hrdlořezy (V Třešňovce/Nad Smetánkou) 0.9 · Podvinný mlýn 1.1
#   Palmovka 1.1 · Pod Harfou 1.5 · Kolbenova east end 2.9 · Karlín 2.9
#   -- excluded: Hloubětín metro 3.7 · Černý Most 6.9
AREA_CENTER = (50.0995, 14.4900)
AREA_RADIUS_KM = 3.0
# Wards the circle overlaps. Free text, resolved server-side to a ward id;
# a ward only contributes the listings that pass the radius test.
SEARCH_WARDS = [
    "Vysočany", "Hrdlořezy", "Libeň", "Karlín", "Žižkov", "Malešice", "Hloubětín",
]
# Radim's landmark streets, shown on the dashboard so the area stays legible.
AREA_LANDMARKS = "Pod Harfou · Kolbenova · Hrdlořezy · Podvinný mlýn · Palmovka · Karlín"


MAX_IMAGES_PER_LISTING = 5
MAX_DESCRIPTION_CHARS = 1200
MAX_HISTORY_EVENTS = 300

# Detail fetches are the expensive part of a run and Sreality throttles the
# burst. Widening the area took the listing set from ~200 to ~1000, which would
# make every 8h run a ~1000-request burst for data that almost never changes.
# So a listing is only re-fetched when something about it changed (see
# needs_enrichment); the rest carry their enrichment forward from the previous
# snapshot. This cap bounds the retry of listings that failed or came back
# without fees, so a permanently fee-less listing can't be refetched forever.
# A PARSER_VERSION bump puts every cached listing into this queue at once, and
# at 60 a run that would take ~1000 re-reads converges over a week instead of an
# afternoon -- with the estimate running on half-populated attributes the whole
# time. So the ceiling is overridable for the one backfill run that follows a
# bump (workflow input `max_reenrich`), and back to 60 for steady state.
MAX_REENRICH_PER_RUN = sources.env_int("MAX_REENRICH", 60)
# Hard ceiling on detail fetches per run. Sreality serves this burst at about
# 1 s per listing when it is happy, but drops into throttling windows where the
# retry backoff pushes it to ~7 s -- measured across one full backfill. The cap
# is set so that even an entirely throttled run finishes in well under an hour
# instead of hammering for ninety minutes. A backlog bigger than the cap simply
# converges over the next couple of runs: whatever is left unenriched comes back
# as a "retry" next time, and those listings are on the dashboard meanwhile --
# they just carry price and locality until their detail lands.
MAX_DETAIL_FETCHES_PER_RUN = sources.env_int("MAX_DETAIL_FETCHES", 300)
# How many clean reads an advert gets before "no fee stated" is accepted as the
# answer rather than retried. Roughly a third of Sreality rentals never quote a
# service charge anywhere.
MAX_FEE_ATTEMPTS = 3
# Bump whenever the fee parser changes behaviour OR the set of fields read out
# of a detail page grows. Enrichment is cached across runs, so without this a
# parser fix would only ever apply to listings that happened to appear
# afterwards -- everything already in the snapshot would keep the value the old
# code got, indefinitely. Two listings were found carrying exactly that kind of
# stale mis-parse (an agency commission and a rent, both stored as the monthly
# fee) before this existed.
#
# 3: reads the attribute block (building condition, furnishing, commission,
#    cellar/balcony/garage, `since`, views, struck-through price) that the rent
#    estimate and the weekly report are built on, and adds `priceNote` to the
#    fee chain. Every cached listing has to be read again for those, which is
#    one backfill run with the fetch caps raised.
PARSER_VERSION = 3

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


class TransientFetchError(Exception):
    """Upstream failed in a way that says nothing about the listing itself -- a
    5xx, a throttle, a timeout, a dropped connection. Callers must never read
    this as "the listing is gone"; under load Sreality redirects detail pages to
    its own /500 page, and treating that as an absence reports live flats as
    removed (and used to crash the whole run)."""


# Sreality throttles the ~200-request detail burst by serving 500s and 429s for
# a few seconds at a time, so these clear on a retry. 404 is deliberately absent
# -- it is the one status that genuinely means "delisted".
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_BACKOFF_SECONDS = (2, 5, 10)


def fetch_next_data(url, params=None):
    """Returns (next_data, status). Raises TransientFetchError once the retries
    are exhausted, so a flaky response can never be mistaken for a missing
    listing. Statuses outside RETRY_STATUSES still raise through
    raise_for_status() -- a persistent 403 block should fail loudly, not be
    swallowed into a phantom mass removal."""
    last_failure = None
    for backoff in (*RETRY_BACKOFF_SECONDS, None):
        try:
            resp = SESSION.get(url, params=params, timeout=20)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_failure = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code not in RETRY_STATUSES:
                if resp.status_code == 404:
                    return None, resp.status_code
                resp.raise_for_status()
                m = NEXT_DATA_RE.search(resp.text)
                if not m:
                    return None, resp.status_code
                return json.loads(m.group(1)), resp.status_code
            last_failure = f"HTTP {resp.status_code}"
        if backoff is None:
            break
        time.sleep(backoff)
    raise TransientFetchError(
        f"{url}: {last_failure} after {len(RETRY_BACKOFF_SECONDS) + 1} attempts"
    )


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


def haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    la1, lo1, la2, lo2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (
        math.sin((la2 - la1) / 2) ** 2
        + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    )
    return 2 * 6371 * math.asin(math.sqrt(a))


def km_from_center(lat, lon):
    return haversine_km(lat, lon, AREA_CENTER[0], AREA_CENTER[1])


def in_watched_area(lat, lon):
    """A listing with no GPS at all can't be placed, so it is kept -- dropping
    it would silently shrink the market. Enrichment usually fills the GPS in."""
    d = km_from_center(lat, lon)
    return True if d is None else d <= AREA_RADIUS_KM


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
# "jistina"/"jistota" are deposit synonyms that agencies on iDNES favour over
# "kauce" ("Nájem 17.500,- | Poplatky 3.500,- | Jistina 21.000,- | Provize
# 15.000,-"); without them the deposit gets read as the monthly fee.
EXCLUDE_KEYWORDS = [
    "kauce", "kauci", "provize", "provizi", "jistina", "jistinu", "jistota",
    "jistotu", "jednorázov", "deposit", "refundable",
    # "odměna RK za zprostředkování 24.000 Kč" -- an agency commission, and one
    # that only escaped being booked as the monthly fee because it happened to
    # exceed the plausible range.
    "odměna", "odmena", "zprostředkování", "zprostredkovani",
]
ELECTRICITY_KEYWORDS = ["energie", "energii", "elektřin", "elektrin"]
# Words that label the rent itself. A price note almost always leads with it
# ("Nájem 17.500,- | Poplatky 3.500,-"), and on a cheap flat the rent is small
# enough to pass for a service charge, so a clause that only talks about rent
# must not donate its number to the fee.
RENT_KEYWORDS = ["nájem", "najem", "nájemn", "najemn", "činže", "cinze"]
# The quoted rent is already all-in. Parsing a fee out of these double-counts
# it, so we record "fees are included" instead of a number. Written as a window
# rather than fixed phrases because the qualifier between the two halves varies
# ("včetně poplatků" but also "včetně paušálních poplatků", "vč. veškerých
# poplatků za služby").
INCLUSIVE_RE = re.compile(
    r'(?:v[čc]etn[ěe]|v[čc]\.|v cen[ěe])\s+(?:\w+\s+){0,3}'
    r'(?:poplat|slu[žz]eb|slu[žz]by|energi|inkas)',
    re.I,
)

# Splits on '+', ';', '|', newline, a markdown/bullet '*', "plus", a sentence
# comma (not a Czech thousands-separator comma like "3,400"), or a
# sentence-ending period (not an abbreviation period like "el." before a
# lowercase word). '|' and '*' matter because agency descriptions are commonly
# pipe-separated one-liners or markdown bullet lists, and without them a whole
# fee list collapses into one clause -- which then gets thrown away entirely as
# soon as it happens to also mention "kauce".
CLAUSE_SPLIT_RE = re.compile(
    r'\+|;|\||\n|\s\*+\s|\bplus\b|,\s+|(?<=[a-zá-ž])\.\s+(?=[A-ZÁ-Ž])', re.I
)
# Czech number formats: "3 400", "3.400", "3,400", "3400", with optional Kč/CZK.
NUMBER_RE = re.compile(r'(\d{1,3}(?:[ .,]\d{3})+|\d{3,6})\s*(?:k[čc]|czk)?', re.I)
# Plausible monthly service fee. Anything outside this is a rent, a deposit, a
# floor area or a year -- not a fee.
FEE_MIN_CZK, FEE_MAX_CZK = 300, 15000


def normalize_text(text):
    """Strip the zero-width joiners and non-breaking spaces portals inject into
    price markup ("17&zwj;&nbsp;500"), which otherwise break number matching."""
    if not text:
        return ""
    return (
        text.replace("‍", "").replace("​", "")
        .replace(" ", " ").replace(" ", " ").replace(" ", " ")
    )


def parse_amounts(clause):
    """All plausible amounts in a clause, in order. Multi-tier fees ("2.500 Kč
    pro 1 osobu, 3.500 Kč pro 2 osoby") list the cheapest tier first."""
    out = []
    for m in NUMBER_RE.finditer(clause):
        digits = re.sub(r"[ .,]", "", m.group(1))
        try:
            v = int(digits)
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return out


def parse_amount(clause):
    amounts = parse_amounts(clause)
    return amounts[0] if amounts else None


def plausible_fee(v):
    return v is not None and FEE_MIN_CZK <= v <= FEE_MAX_CZK


def is_all_inclusive(text):
    return bool(INCLUSIVE_RE.search(normalize_text(text)))


def _scan_clauses(clauses, require_keyword, rent_czk=None):
    """Walk clauses once, pulling out a monthly fee and an electricity amount.

    Two things the old single-clause scan got wrong on real listings:

    * The amount often sits in the *next* clause, because the sentence comma
      splits the keyword away from it -- "zálohu na služby, která činí 2.500
      Kč". So a keyword clause with no amount of its own looks ahead one clause.
    * A deposit mention used to void the whole clause. Now the exclusion only
      applies to the amounts in that clause; a fee already found in an earlier
      clause survives a later "Kauce ..." on the same line.
    """
    fee = electricity = None
    for i, clause in enumerate(clauses):
        low = clause.lower()
        if any(k in low for k in EXCLUDE_KEYWORDS):
            continue
        is_elec = any(k in low for k in ELECTRICITY_KEYWORDS)
        has_fee_kw = any(k in low for k in FEE_KEYWORDS)
        # "Nájem 17.500,-" is the rent, not a fee, even though 17 500 sits in
        # the plausible-fee range for a cheaper flat.
        if any(k in low for k in RENT_KEYWORDS) and not has_fee_kw:
            continue
        amounts = parse_amounts(clause)
        if rent_czk:
            amounts = [v for v in amounts if v != rent_czk]
        if not amounts and has_fee_kw and i + 1 < len(clauses):
            nxt = clauses[i + 1]
            if not any(k in nxt.lower() for k in EXCLUDE_KEYWORDS):
                amounts = parse_amounts(nxt)
        if not amounts:
            continue
        if is_elec and not has_fee_kw:
            if electricity is None:
                electricity = amounts[0]
        elif fee is None and (has_fee_kw or not require_keyword):
            # Cheapest tier of a multi-person fee table, and only if it looks
            # like a fee at all -- this is what keeps a 19 900 Kč rent or a
            # 45 000 Kč deposit from being booked as the monthly service charge.
            candidates = [v for v in amounts if plausible_fee(v)]
            if candidates:
                fee = min(candidates)
    return fee, electricity


def parse_cost_of_living_text(text, rent_czk=None):
    """costOfLiving is themed as monthly living costs already, so any amount
    in it that isn't in a deposit/commission clause is the fee -- no keyword
    required (e.g. "4800 Kč plus elektřina")."""
    return _scan_clauses(
        CLAUSE_SPLIT_RE.split(normalize_text(text)), require_keyword=False, rent_czk=rent_czk
    )


def parse_fee_from_description(text, rent_czk=None):
    """Free-text description fallback -- here a fee keyword IS required, since
    unanchored numbers in prose are far more likely to be unrelated (m²,
    floor, year built, etc.)."""
    return _scan_clauses(
        CLAUSE_SPLIT_RE.split(normalize_text(text)), require_keyword=True, rent_czk=rent_czk
    )


def extract_fees_and_electricity(cost_of_living_raw, description, rent_czk=None,
                                 price_note=None):
    """Returns (fees_czk, fees_source, electricity_explicit_czk).
    fees_source is "field" (clean int or parsed from costOfLiving text),
    "note" (parsed from Sreality's priceNote), "text" (parsed from the
    description), "included" (the listing says the rent is already all-in), or
    None (not found anywhere).

    `priceNote` is tried before the description because it is a short structured
    line ("poplatky 6109 Kč/měs.") rather than sales prose, so a hit there is
    more trustworthy. On the sample it added a fee to 7 of 19 adverts with an
    empty costOfLiving -- but the description already caught every one of them,
    so expect no jump in coverage, only fewer chances to mis-read.

    rent_czk, when known, is used only to veto it as a fee candidate: portals
    routinely restate the rent inside the very field that also carries the
    service charge."""
    raw = normalize_text(cost_of_living_raw).strip()
    try:
        v = int(raw)
        if v > 0:
            return v, "field", None
    except ValueError:
        pass
    fee, electricity = parse_cost_of_living_text(raw, rent_czk)
    if fee is not None:
        return fee, "field", electricity
    fee, electricity_note = parse_fee_from_description(price_note, rent_czk)
    electricity = electricity if electricity is not None else electricity_note
    if fee is not None:
        return fee, "note", electricity
    fee, electricity_2 = parse_fee_from_description(description, rent_czk)
    electricity = electricity if electricity is not None else electricity_2
    if fee is not None:
        return fee, "text", electricity
    # No number anywhere -- but if the listing states the rent already covers
    # the fees, that is an answer, not a gap, and it must not be presented as
    # "fee unknown, assumed 0" alongside listings that quote rent net of fees.
    if is_all_inclusive(raw) or is_all_inclusive(description) or is_all_inclusive(price_note):
        return 0, "included", electricity
    return None, None, electricity


def cost_breakdown(price_czk, fees_czk, transaction_type, electricity_explicit=None,
                   fees_source=None):
    """Returns (fees_czk, fees_missing, electricity_czk, electricity_estimated,
    total_czk) for a listing. Only rentals (pronajem) get an electricity
    figure and a fees+electricity total; sales just total to the purchase
    price. If the listing states a real electricity amount, use it instead of
    the uniform estimate."""
    fees_missing = fees_czk is None
    if transaction_type != "pronajem":
        return fees_czk, fees_missing, None, False, price_czk
    if fees_source == "included":
        # The quoted rent already covers fees and utilities, so adding the
        # uniform electricity estimate on top would inflate it above what the
        # tenant actually pays.
        return 0, False, 0, False, price_czk
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


# Sreality states enumerated attributes as {"name": ..., "value": <code>} and
# uses value 0 for "not filled in" -- consistently across buildingCondition,
# furnished, elevator and energyEfficiencyRating. Reading the label instead of
# the code is a trap this scraper already paid for once: under load Sreality
# answers in English, and a name-keyed map silently splits one category in two.
# So every enum is decoded from its numeric code and the label is kept only to
# show a human.
NEW_BUILDING_CODE = 6            # "Novostavba"
FURNISHED_CODES = {1: "ano", 2: "ne", 3: "castecne"}
ENERGY_CODES = {1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F", 7: "G"}


def enum_param(params, key):
    """(code, label) for one of Sreality's enumerated params, with the
    "not filled in" sentinel normalised away. Roughly a third of adverts leave
    furnishing and energy class empty, and counting those as a category would
    invent a group that isn't one."""
    raw = params.get(key)
    if not isinstance(raw, dict):
        return None, None
    code = raw.get("value")
    if code in (None, 0):
        return None, None
    return code, raw.get("name")


def _opt_bool(value):
    return bool(value) if value is not None else None


def attributes_from_params(params, data):
    """The attribute block the rent estimate and the weekly report read (§3.7).

    Only two of these are ever allowed to move a number: furnishing and
    building condition. The rest are stored because the report has to be able
    to *say* the sample cannot separate them -- and because a stored attribute
    costs ~30 bytes, while re-fetching a delisted advert to get it costs the
    advert."""
    condition_code, condition_name = enum_param(params, "buildingCondition")
    type_code, type_name = enum_param(params, "buildingType")
    energy_code, _energy_name = enum_param(params, "energyEfficiencyRating")
    furnished_code, _furnished_name = enum_param(params, "furnished")
    elevator_code, _elevator_name = enum_param(params, "elevator")
    ownership_code, ownership_name = enum_param(params, "ownership")

    commission = params.get("commission")
    tenant_not_pay = params.get("tenantNotPayCommission")
    if commission is not None:
        no_commission = commission == 0
    elif tenant_not_pay is True:
        no_commission = True
    else:
        no_commission = None

    return {
        "building_condition": condition_code,
        "building_condition_name": condition_name,
        # buildingType is deliberately NOT used for this: brick is the most
        # common type in the area and includes new builds, so "cihlová = older"
        # is simply false. The direct field is the answer (N-6).
        "is_new_building": None if condition_code is None else condition_code == NEW_BUILDING_CODE,
        "building_type": type_code,
        "building_type_name": type_name,
        "energy_rating": ENERGY_CODES.get(energy_code),
        "furnished": FURNISHED_CODES.get(furnished_code),
        "commission_czk": commission,
        "tenant_not_pay_commission": tenant_not_pay,
        "no_commission": no_commission,
        "cellar": _opt_bool(params.get("cellar")),
        "cellar_area_sqm": params.get("cellarArea"),
        "garage_count": params.get("garageCount"),
        "parking_lots": params.get("parkingLots"),
        "balcony": _opt_bool(params.get("balcony")),
        "balcony_area_sqm": params.get("balconyArea"),
        "loggia": _opt_bool(params.get("loggia")),
        "loggia_area_sqm": params.get("loggiaArea"),
        "terrace": _opt_bool(params.get("terrace")),
        "terrace_area_sqm": params.get("terraceArea"),
        "elevator": None if elevator_code is None else elevator_code == 1,
        "ownership": ownership_code,
        "ownership_name": ownership_name,
        # `since` is the insertion date, present on 100 % of adverts. Days on
        # market are computed from it and nothing else: our own first sighting
        # puts the median at 4 days, which is an artefact of unstable search
        # pagination rather than anything about the market (§3.7).
        "since": params.get("since"),
        "edited": params.get("edited"),
        "views": params.get("stats") if isinstance(params.get("stats"), int) else None,
        # Struck-through price: the advert's own record of having come down.
        "price_old_czk": data.get("priceSummaryOldCzk") or None,
    }


def load_tracked_config():
    if not TRACKED_PATH.exists():
        return []
    return json.loads(TRACKED_PATH.read_text())


def fetch_tracked(url, listing_id):
    """A record with "unavailable" means the fetch failed, not that the listing
    went away -- main() carries the previous state forward for those. Only a 404
    is treated as a real delisting; that is what Sreality actually serves for a
    removed advert."""

    def unreadable(reason):
        return {
            "id": listing_id,
            "url": url,
            "unavailable": True,
            "fetched_at": now_iso(),
            "error": reason,
        }

    try:
        next_data, status = fetch_next_data(url)
    except TransientFetchError as exc:
        return unreadable(str(exc))
    if next_data is None:
        if status != 404:
            return unreadable(f"no __NEXT_DATA__ in page (HTTP {status})")
        return {
            "id": listing_id,
            "url": url,
            "active": False,
            "fetched_at": now_iso(),
            "error": f"HTTP {status}",
        }
    data, _ = get_query_data(next_data, "estate")
    if data is None:
        return unreadable("estate query missing from page")
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
        params.get("costOfLiving"), data.get("description"), rent_czk
    )
    fees_czk, fees_missing, electricity_czk, electricity_estimated, total_czk = (
        cost_breakdown(rent_czk, fees_czk, transaction_type, electricity_explicit, fees_source)
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


# street name -> (lat, lon), filled per run from the Sreality sweep.
STREET_GPS = {}


def build_street_gps(comparables):
    """Average position of every street Sreality reports, so a source that
    names a street but gives no coordinates can still be placed on the map and
    held to the same radius as everything else."""
    pts = {}
    for c in comparables:
        street, lat, lon = c.get("street"), c.get("lat"), c.get("lon")
        if street and lat and lon:
            pts.setdefault(street.strip().lower(), []).append((lat, lon))
    return {
        k: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v))
        for k, v in pts.items()
    }


def search_ward(ward, tx_type):
    """Every listing of the wanted dispositions in one ward, as parsed comps.

    Every page of the search must land. A half-read result set is worse than no
    run at all: diff_snapshots would report the unread remainder of the market
    as removed, so any early exit aborts instead."""
    found = []
    page = 1
    seen_total = None
    seen_offsets = set()
    complete = False
    while True:
        url = f"https://www.sreality.cz/hledani/{tx_type}/byty"
        # The site's pagination query param is the Czech "strana" (page), not
        # "page" -- "page" is silently ignored and always returns page 1.
        next_data, status = fetch_next_data(
            url,
            params={"region": ward, "velikost": SEARCH_VELIKOST, "strana": page},
        )
        if next_data is None:
            raise TransientFetchError(
                f"search page {page} of {tx_type}/{ward}: no __NEXT_DATA__ (HTTP {status})"
            )
        data, _ = get_query_data(next_data, "estatesSearch")
        if data is None:
            raise TransientFetchError(
                f"search page {page} of {tx_type}/{ward}: estatesSearch query missing"
            )
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
            # The velikost param already filters server-side; this is the
            # belt-and-braces check in case the site ignores it.
            if sub.get("value") not in DISPOSITION_CODES:
                continue
            found.append(parse_comparable(r, tx_type))
        limit = pagination.get("limit") or len(page_results) or 22
        if page * limit >= (seen_total or 0):
            complete = True
            break
        page += 1
        time.sleep(0.3)
    if not complete:
        raise TransientFetchError(
            f"search pagination for {tx_type}/{ward} stopped at page {page} of "
            f"total={seen_total}; aborting rather than writing a truncated snapshot"
        )
    return found


def needs_enrichment(comp, prev_comp):
    """Which listings deserve a detail fetch this run.

    Detail data (description, fees, photos, floor) is static for the life of an
    advert, so re-fetching ~1000 unchanged listings every 8h would be a pointless
    burst against a host that already throttles us. A listing is re-read only if
    it is new, if its price moved (agencies revise the service charge along with
    the rent), or if the previous attempt came back without the fee we care
    about -- and that last case is rate-limited by the caller."""
    if prev_comp is None:
        return True, "new"
    if prev_comp.get("price_czk") != comp.get("price_czk"):
        return True, "price"
    if prev_comp.get("enrich_failed"):
        return True, "retry"
    if prev_comp.get("parser_version") != PARSER_VERSION:
        return True, "retry"
    # An advert that has been read cleanly a few times and still states no fee
    # simply doesn't state one. Retrying it every 8h forever is a standing cost
    # for a fact that already settled, so the attempts are counted and capped.
    if prev_comp.get("fee_attempts", 0) >= MAX_FEE_ATTEMPTS:
        return False, "cached"
    # A fold-cache record deliberately carries no description (see
    # build_enrichment_cache), so its absence is not evidence of a failed read.
    if prev_comp.get("description") is None and not prev_comp.get("cached_only"):
        return True, "retry"
    if comp.get("transaction_type") == "pronajem" and prev_comp.get("fees_missing"):
        return True, "retry"
    return False, "cached"


# Fields the detail fetch supplies. Carried forward verbatim when a listing is
# unchanged, so a cached listing is indistinguishable from a freshly enriched
# one downstream.
ENRICHED_FIELDS = (
    "description", "seller_name", "images", "thumb", "floor_number", "floors_total",
    "fees_czk", "fees_missing", "fees_source", "electricity_czk",
    "electricity_estimated", "total_czk", "garage", "parking",
    "price_czk_per_sqm", "floor_area_sqm", "lat", "lon", "cost_of_living_raw",
    "fee_attempts", "parser_version",
    # The attribute block (attributes_from_params). Carried forward like the
    # rest: these are static for the life of an advert, so a cached listing must
    # keep them or the 30-day pool would only ever know about this week's
    # listings' furnishing.
    "building_condition", "building_condition_name", "is_new_building",
    "building_type", "building_type_name", "energy_rating", "furnished",
    "commission_czk", "tenant_not_pay_commission", "no_commission",
    "cellar", "cellar_area_sqm", "garage_count", "parking_lots",
    "balcony", "balcony_area_sqm", "loggia", "loggia_area_sqm",
    "terrace", "terrace_area_sqm", "elevator", "ownership", "ownership_name",
    "since", "edited", "views", "price_old_czk",
)


def carry_enrichment(comp, prev_comp):
    for k in ENRICHED_FIELDS:
        if prev_comp.get(k) is not None:
            comp[k] = prev_comp[k]
    comp["from_cache"] = True


def fetch_comparables(prev_snapshot=None):
    by_id = {}
    for ward in SEARCH_WARDS:
        for tx_type in TRANSACTION_TYPES:
            for comp in search_ward(ward, tx_type):
                # Wards overlap the circle's edge and a listing can surface in
                # more than one search, so dedup strictly by listing id.
                by_id.setdefault(comp["id"], comp)
            time.sleep(0.3)
        print(f"  ward {ward}: running unique={len(by_id)}", file=sys.stderr)

    raw_count = len(by_id)
    # Built from the FULL ward sweep, before the radius cut, so it knows where
    # the streets just outside the circle are too. Sources without their own GPS
    # (iDNES) use it to place their listings; a map built from the filtered set
    # could only ever confirm "inside" and would wave everything else through.
    global STREET_GPS
    STREET_GPS = build_street_gps(by_id.values())
    comparables = [c for c in by_id.values() if in_watched_area(c.get("lat"), c.get("lon"))]
    print(
        f"Area filter: {len(comparables)}/{raw_count} listings within "
        f"{AREA_RADIUS_KM} km of {AREA_CENTER}",
        file=sys.stderr,
    )
    for c in comparables:
        c["dist_km"] = round(km_from_center(c.get("lat"), c.get("lon")) or 0, 2)

    # Adverts that dedup folded away last run are not in `comparables`, but they
    # were read, and their enrichment is remembered separately. Without this the
    # planner sees them as new every single run.
    prev_by_id = fold_cache_records((prev_snapshot or {}).get("enrichment_cache"))
    prev_by_id.update({c["id"]: c for c in (prev_snapshot or {}).get("comparables", [])})
    queues = {"price": [], "new": [], "retry": []}
    for comp in comparables:
        prev_comp = prev_by_id.get(comp["id"])
        needed, reason = needs_enrichment(comp, prev_comp)
        if needed:
            queues[reason].append(comp)
        else:
            carry_enrichment(comp, prev_comp)

    # Priority order matters once the budget binds: a repriced listing is what
    # the change alerts fire on, a new listing is what Radim wants to see, and a
    # retry is a listing that already has *something* on the dashboard.
    retries = queues["retry"][:MAX_REENRICH_PER_RUN]
    to_fetch = (queues["price"] + queues["new"] + retries)[:MAX_DETAIL_FETCHES_PER_RUN]
    queued = {c["id"] for c in to_fetch}
    # Anything that wanted a fetch but lost the budget still shows its previous
    # detail rather than going blank for a run.
    deferred = [c for c in comparables if c["id"] not in queued
                and c["id"] in prev_by_id and not c.get("from_cache")]
    for comp in deferred:
        carry_enrichment(comp, prev_by_id[comp["id"]])

    backlog = len(queues["price"]) + len(queues["new"]) + len(queues["retry"]) - len(to_fetch)
    print(
        f"Enriching {len(to_fetch)} listing(s) with detail "
        f"(new={len(queues['new'])} repriced={len(queues['price'])} retry={len(queues['retry'])}; "
        f"{len(comparables) - len(to_fetch)} carried forward, {backlog} deferred to next run)...",
        file=sys.stderr,
    )
    for i, comp in enumerate(to_fetch, 1):
        enrich_comparable(comp)
        if i % 50 == 0:
            print(f"  ...{i}/{len(to_fetch)}", file=sys.stderr)
        time.sleep(0.15)

    stale = [c for c in comparables if not c.get("active")]
    if stale:
        print(
            f"Dropping {len(stale)} listing(s) that went inactive between search and detail fetch",
            file=sys.stderr,
        )
    comparables = [c for c in comparables if c.get("active")]

    apply_approx_locations(comparables)
    # Enrichment can supply GPS the search payload lacked, so re-test the area
    # for anything that was only kept because it had no coordinates at all.
    placed = [
        c for c in comparables
        if c.get("approx_location") or in_watched_area(c.get("lat"), c.get("lon"))
    ]
    if len(placed) != len(comparables):
        print(f"Dropping {len(comparables) - len(placed)} listing(s) placed outside the area by detail GPS", file=sys.stderr)
    for c in placed:
        c["dist_km"] = round(km_from_center(c.get("lat"), c.get("lon")) or 0, 2)
    return placed


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
    # Take the disposition from the numeric code, never the display name:
    # Sreality sometimes answers with an English payload where 2+kk comes back
    # as "2+kt". Trusting the label splits one flat type into two, so the filter
    # misses those listings and their median group is too small to rank against.
    sub_code = (r.get("categorySubCb") or {}).get("value")
    disposition = DISPOSITION_CODES.get(sub_code) or (r.get("categorySubCb") or {}).get("name") or "x"
    slug = urllib.parse.quote(disposition)
    # Sreality redirects /detail/<type>/byt/<any-slug>/<any-locality>/<id> to the
    # canonical URL, so the locality segment doesn't need to be exact.
    detail_url = (
        f"https://www.sreality.cz/detail/{tx_type}/byt/{slug}/x/{r['id']}"
    )
    return {
        "id": r["id"],
        "title": r.get("name"),
        "disposition": disposition,
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
        "dist_km": None,
        "approx_location": False,
        "images": extract_images(r.get("images")),
        "thumb": extract_thumb(r.get("images")),
        "description": None,
        "seller_name": None,
        "cost_of_living_raw": None,
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
    try:
        next_data, status = fetch_next_data(comp["url"])
    except TransientFetchError as exc:
        # The search returned this listing moments ago, so it exists. Keep it
        # active and unenriched rather than dropping it -- a dropped listing
        # reads as "removed" downstream and fires a false alert.
        print(f"  detail fetch failed for {comp['id']}, keeping listing: {exc}", file=sys.stderr)
        comp["enrich_failed"] = True
        return
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

    # Kept on the record so a fee that parsed wrong can be diagnosed from the
    # snapshot alone, without re-fetching the (possibly delisted) advert.
    comp["cost_of_living_raw"] = params.get("costOfLiving")
    comp.update(attributes_from_params(params, data))
    fees_czk, fees_source, electricity_explicit = extract_fees_and_electricity(
        params.get("costOfLiving"), data.get("description"), comp.get("price_czk"),
        params.get("priceNote"),
    )
    fees_czk, fees_missing, electricity_czk, electricity_estimated, total_czk = (
        cost_breakdown(comp.get("price_czk"), fees_czk, comp.get("transaction_type"),
                       electricity_explicit, fees_source)
    )
    comp["fees_czk"] = fees_czk
    comp["fees_missing"] = fees_missing
    comp["fees_source"] = fees_source
    # Counts only reads that came back without a fee; a successful parse resets
    # it, so a listing that later adds its fee is picked up straight away.
    comp["fee_attempts"] = 0 if not fees_missing else (comp.get("fee_attempts") or 0) + 1
    comp["parser_version"] = PARSER_VERSION
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

    append_changes_log(new_events)
    history = (new_events + history)[:MAX_HISTORY_EVENTS]
    CHANGES_HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2))
    return history


# Fields worth keeping in the permanent log. The full `item` is the whole
# listing record including photos and description -- fine for a 300-event view
# inlined into a page, ruinous for a file that only ever grows.
LOG_ITEM_FIELDS = (
    "id", "url", "source", "title", "transaction_type", "disposition",
    "floor_area_sqm", "street", "locality", "price_czk", "total_czk",
    "price_czk_per_sqm", "fees_missing", "since", "is_new_building",
    "furnished", "no_commission",
)


def append_changes_log(new_events):
    """Append-only, one JSON object per line, never truncated (R-7.4 needs a
    week of history and the capped file holds two days). Slimmed on the way in
    so a year of events stays a few MB rather than a few hundred.

    Nothing in this repo reads it back: the weekly write-up derives arrivals and
    departures from the pool, which knows when each advert was first and last
    seen. The log is the raw record for anything asked of it later -- one JSON
    object per line, so `jq` is enough."""
    if not new_events:
        return
    lines = []
    for event in new_events:
        item = event.get("item") or {}
        slim = {k: item[k] for k in LOG_ITEM_FIELDS if item.get(k) is not None}
        lines.append(json.dumps({**event, "item": slim or None}, ensure_ascii=False))
    with CHANGES_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def listing_is_gone(comp):
    """Ask the listing's own page whether it still exists.

    Absence from the search is only ever circumstantial. The ward sweep walks
    ~60 paginated pages sorted by date while the underlying set keeps changing,
    so listings slip between page boundaries and vanish for a run -- eight of
    them on the run this was written for, every one still live when asked
    directly. 404 is the one answer that means delisted (established upstream in
    fetch_next_data); anything else, including an error, means "don't report".

    Returns True only for a confirmed 404."""
    url = comp.get("url")
    if not url:
        return False
    try:
        if comp.get("source") in (None, "sreality"):
            _data, status = fetch_next_data(url)
            return status == 404
        resp = SESSION.get(url, timeout=20, allow_redirects=True)
        return resp.status_code == 404
    except (TransientFetchError, requests.RequestException) as exc:
        print(f"  could not verify {comp.get('id')}, keeping it: {exc}", file=sys.stderr)
        return False


def verify_removals(changes, curr):
    """Turn the statistical guess into an observation before anything is
    announced. Listings that answer are put straight back into the snapshot so
    they don't disappear from the dashboard for a run either."""
    candidates = changes.get("newly_inactive", [])
    if not candidates:
        return
    print(f"Verifying {len(candidates)} candidate removal(s) against their own pages...", file=sys.stderr)
    confirmed, resurrected = [], []
    for comp in candidates:
        if listing_is_gone(comp):
            confirmed.append(comp)
        else:
            resurrected.append(comp)
        time.sleep(0.2)
    changes["newly_inactive"] = confirmed
    if resurrected:
        known = {c["id"] for c in curr["comparables"]}
        for comp in resurrected:
            if comp["id"] not in known:
                restored = {k: v for k, v in comp.items()
                            if k not in ("missing_since", "removed_since")}
                restored["search_missed"] = True
                curr["comparables"].append(restored)
        print(
            f"  {len(confirmed)} confirmed gone, {len(resurrected)} still live "
            f"(missed by the search, kept)",
            file=sys.stderr,
        )


def config_fingerprint():
    """Identifies the shape of the search: the area, the dispositions, the wards.

    A listing can leave the snapshot for two very different reasons -- it was
    delisted, or we changed what we look at. Only the first is news. Widening
    the area from Vysočany to the Hrdlořezy/Karlín circle dropped 148 iDNES
    listings that were still perfectly live, and without this the next run would
    have reported every one of them as gone, which is precisely the kind of
    false "❌ zmizelo" alert this scraper already learned to avoid upstream."""
    return {
        "center": list(AREA_CENTER),
        "radius_km": AREA_RADIUS_KM,
        "dispositions": sorted(DISPOSITION_CODES.values()),
        "wards": sorted(SEARCH_WARDS),
        "idnes_wards": sorted(sources.IDNES_WARDS),
    }


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

    # A listing absent from one run is only a candidate for removal, not a fact:
    # the search set itself fluctuates run to run. It has to be absent twice in a
    # row before "removed" is reported, and a listing that reappears in between
    # produces no event at all -- it was never really gone. This costs one run
    # (~8h) of latency on genuine removals and buys silence on the false ones.
    prev_pending = {c["id"]: c for c in prev.get("pending_removal", [])}
    prev_by_id = {c["id"]: c for c in prev.get("comparables", [])}
    prev_by_id.update(prev_pending)
    curr_by_id = {c["id"]: c for c in curr.get("comparables", [])}

    # The search itself changed shape this run, so an absence says nothing about
    # the listing. Re-baseline silently instead of announcing a mass removal.
    # A snapshot predating this field (config is None) also counts as "changed":
    # it is exactly the snapshot taken before the area was widened.
    if prev.get("config") != curr.get("config"):
        print(
            "Search config changed since the last snapshot -- suppressing removal "
            "detection for this run and re-baselining.",
            file=sys.stderr,
        )
        curr["pending_removal"] = []
        changes["config_changed"] = True
    else:
        pending = []
        for cid, old in prev_by_id.items():
            if cid in curr_by_id:
                continue
            if cid in prev_pending:
                changes["newly_inactive"].append({**old, "removed_since": changes["generated_at"]})
            else:
                pending.append({**old, "missing_since": changes["generated_at"]})
        curr["pending_removal"] = pending

    for cid, new in curr_by_id.items():
        old = prev_by_id.get(cid)
        if old is None:
            # Same reasoning as removals, mirrored: widening the area surfaces
            # hundreds of listings that have been on the market for months.
            # Calling them "new" would bury the handful that really are.
            if not changes.get("config_changed"):
                changes["new_listings"].append({**new, "first_seen": changes["generated_at"]})
        else:
            # Compare on total cost (rent+fees+electricity for rentals), not
            # just base price, so a fee change shows up as a price change too.
            old_cmp = cmp_value(old)
            new_cmp = cmp_value(new)
            # On a re-baseline the totals move for reasons that have nothing to
            # do with the market: merging two portals' copies donates a fee that
            # was only ever stated on one of them, so the all-in total changes
            # while the rent sits exactly where it was. Measured at 50 of 50 on
            # the dedup run -- every one with an unchanged base rent.
            # Same class of problem, second source: a PARSER_VERSION bump makes
            # us read the advert differently, so the fee -- and with it the
            # all-in total -- moves while the advertised rent has not budged.
            # Spread over the runs it takes to re-read everything, so a one-off
            # re-baseline cannot catch it; the base price is the tell.
            reparsed = (
                old.get("parser_version") != new.get("parser_version")
                and old.get("price_czk") == new.get("price_czk")
            )
            if old_cmp != new_cmp and not changes.get("config_changed") and not reparsed:
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


# A listing is only worth calling a deal if it is meaningfully below what the
# same disposition costs in the same area -- 8 % is roughly where the difference
# stops being noise between two comparable flats.
DEAL_THRESHOLD_PCT = 8
# Below this, it stops being a bargain and starts being a different product.
# Measured on the first full run: everything past -45 % was a co-ownership share
# (podíl), an auction (dražba), a co-op flat quoted without its anuita, or a
# mis-typed floor area -- an 82 m² flat "for 424 000 Kč", a "3+kk" of 225 m².
# None of them are things Radim could actually buy or rent at the quoted price,
# and left in they occupy the whole top of the list.
DEAL_FLOOR_PCT = -45

# Two portals listing the same flat is the norm, not the exception: on the first
# full run 736 of 1573 records (47 %) were cross-listings. Identical transaction,
# disposition, floor area, price AND street is the same flat -- validated across
# the run at 333 groups with no group spanning two streets. Dropping the street
# from the key over-merges (21 groups collapsed genuinely different flats that
# happened to share an area and a price), so street is required and the ~68
# listings without one simply never merge; under-merging is the safe failure.
def dedup_key(c):
    street = (c.get("street") or "").strip().lower()
    if not (street and c.get("floor_area_sqm") and c.get("price_czk")):
        return None
    return (c["transaction_type"], c.get("disposition"),
            c.get("floor_area_sqm"), c.get("price_czk"), street)


# Which portal's copy to keep when the same flat is on several. Sreality first:
# it is the only source with a real fee field, exact GPS and full photos.
SOURCE_PRIORITY = {"sreality": 0, "bezrealitky": 1, "idnes": 2}


def merge_cross_portal(comparables):
    """Collapse the same flat advertised on several portals into one row.

    The duplicates are not dropped silently -- the surviving record keeps every
    other portal's link in `also_on`, so the dashboard can still offer them and
    nothing disappears without a trace.

    Returns (survivors, folded_copies). The folded copies are handed back rather
    than discarded because they still cost a detail fetch each: they leave the
    snapshot, so without them the next run cannot tell they were ever read (see
    build_enrichment_cache)."""
    groups, singles = {}, []
    for c in comparables:
        k = dedup_key(c)
        if k is None:
            singles.append(c)
        else:
            groups.setdefault(k, []).append(c)

    merged, folded = [], []
    for dupes in groups.values():
        if len(dupes) == 1:
            merged.append(dupes[0])
            continue
        dupes.sort(key=lambda c: (
            SOURCE_PRIORITY.get(c.get("source"), 9),
            # within a portal, prefer the copy that actually has fee data
            0 if not c.get("fees_missing") else 1,
            0 if c.get("description") else 1,
            # Final tiebreak so the surviving id is stable run to run: an
            # unstable winner would read downstream as one listing removed and
            # another appearing, every single run.
            str(c.get("id")),
        ))
        keep, rest = dupes[0], dupes[1:]
        # A fee stated on one portal but not the other is still a fact about
        # the flat, so take it rather than reporting "neuvedeno".
        if keep.get("fees_missing"):
            donor = next((d for d in rest if not d.get("fees_missing")), None)
            if donor:
                for f in ("fees_czk", "fees_missing", "fees_source", "electricity_czk",
                          "electricity_estimated", "total_czk", "price_czk_per_sqm"):
                    keep[f] = donor.get(f)
        if not keep.get("description"):
            keep["description"] = next((d.get("description") for d in rest if d.get("description")), None)
        keep["also_on"] = [
            {"source": d.get("source"), "url": d.get("url")} for d in rest if d.get("url")
        ]
        merged.append(keep)
        folded.extend(rest)

    result = merged + singles
    print(
        f"Cross-portal dedup: {len(comparables)} -> {len(result)} listings "
        f"({len(comparables) - len(result)} duplicate copies folded in)",
        file=sys.stderr,
    )
    return result, folded


# The fields worth remembering about an advert that got folded into another
# portal's copy. Deliberately no description or images: they are the bulk of the
# record, and anything the folded copy had to contribute was already donated to
# the survivor and is stored there. What is left is only what decides whether
# the advert needs another detail fetch.
FOLD_CACHE_FIELDS = (
    "price_czk", "fees_czk", "fees_missing", "fees_source", "electricity_czk",
    "electricity_estimated", "total_czk", "price_czk_per_sqm", "fee_attempts",
    "parser_version",
)


def build_enrichment_cache(folded):
    """Remember the enrichment of adverts that dedup removed from the snapshot.

    Cross-portal duplicates are folded into one row, so ~300 iDNES ids vanish
    from `comparables` every run. The next run has no record of them, reads them
    as brand new, and spends its whole detail budget re-fetching adverts it has
    already read -- measured at exactly the 200/run cap on three consecutive
    runs, with another ~117 deferred that were therefore never read at all.

    Keeping a compact record of them costs a few hundred bytes each and makes
    the cache converge. `cached_only` marks the record as having no description
    on purpose, so the staleness checks don't mistake that for a failed read.

    Rebuilt from scratch every run rather than accumulated: a live duplicate is
    folded again on every run, so this run's folded set is already the complete
    list, and an advert that has gone away simply drops out."""
    cache = {}
    for comp in folded:
        entry = {k: comp[k] for k in FOLD_CACHE_FIELDS if comp.get(k) is not None}
        entry["cached_only"] = True
        cache[str(comp["id"])] = entry
    return cache


def fold_cache_records(cache):
    """Fold-cache entries in the shape the enrichment planners expect."""
    out = {}
    for cid, entry in (cache or {}).items():
        rec = dict(entry)
        # Ids are ints on Sreality and strings elsewhere; JSON keys are always
        # strings, so both spellings have to resolve.
        out[cid] = rec
        if cid.isdigit():
            out[int(cid)] = rec
    return out


def rank_deals(comparables):
    """Score every listing against the median Kč/m² of its own disposition and
    transaction type, so "cheap" means cheap for what it is rather than just
    small.

    Rentals whose fee is unknown are scored but never surfaced as deals: their
    all-in total is missing a real cost, so they look cheaper than they are.
    Left in, they would crowd out the genuine bargains -- the "best deals" list
    would mostly be a list of adverts that didn't disclose their fees."""
    def comparable_basis(c):
        """A rental's Kč/m² is only on the same footing as its neighbours once
        the fee is known -- until then it is rent-only and looks too cheap. Such
        rows are kept off the median as well as out of the deal list, or a run
        with many un-enriched listings would drag the baseline down and make
        everything else look expensive."""
        return not (c.get("transaction_type") == "pronajem" and c.get("fees_missing"))

    groups = {}
    for c in comparables:
        v = c.get("price_czk_per_sqm")
        if v and comparable_basis(c):
            groups.setdefault((c.get("transaction_type"), c.get("disposition")), []).append(v)
    medians = {k: statistics.median(v) for k, v in groups.items() if len(v) >= 4}

    for c in comparables:
        c["deal_pct"] = None
        c["deal_ok"] = False
        med = medians.get((c.get("transaction_type"), c.get("disposition")))
        v = c.get("price_czk_per_sqm")
        if not med or not v:
            continue
        c["deal_pct"] = round((v - med) / med * 100)
        c["deal_ok"] = (
            comparable_basis(c)
            and DEAL_FLOOR_PCT <= c["deal_pct"] <= -DEAL_THRESHOLD_PCT
        )
        # Too far below the market to be a price -- surfaced as a caveat on the
        # row rather than hidden, since it may still be something Radim wants
        # to look at (an auction can be a real opportunity, just not a listing
        # you can compare on Kč/m²).
        c["deal_outlier"] = c["deal_pct"] < DEAL_FLOOR_PCT
    return comparables


# The flat the area's asking prices are read against on the dashboard. The price
# is the flat alone: the garage and the storage unit are separate units on the
# contract and no advert here quotes either, so folding them in would inflate
# its Kč/m² against listings that don't include them. Deliberately no unit
# number or project name -- this repo and its Pages site are public, and the
# comparison works without naming which flat it is.
#
# The purchase price comes from the environment rather than sitting here: it is
# a personal figure and this file is public (R-10.1). Without OWN_PRICE_CZK the
# card simply does not render -- there is nothing to compare against, and a
# card that quietly drops its own reference point would be worse than no card.
#
# NOTE, and it matters: keeping the number out of the source does NOT keep it
# off the published page. The card prints it into dashboard.html, which is
# committed to this public repo and served by Pages. Only removing the personal
# marker from the card would do that; the secret alone moves the number from
# the source into a build artefact.
OWN_PROPERTY = {
    "disposition": "1+kk",
    "floor_area_sqm": 29.6,
    "price_czk": sources.env_int("OWN_PRICE_CZK", None),
    "caveat": "novostavba, dokončení ~léto 2027 — okolní inzeráty jsou převážně starší byty z druhé ruky",
}
# Comparables are drawn from a size band, not just the disposition, because
# Kč/m² falls with size even inside 1+kk: the bathroom, the kitchenette and the
# entrance cost roughly the same in 25 m² as in 40 m². Ranking his 29,6 m² flat
# against every 1+kk in the circle would flatter it for a reason that has
# nothing to do with the price.
OWN_SIZE_BAND_SQM = (25.0, 35.0)


def own_property_stats(comparables):
    """Where Radim's own price per m² sits in the area's asking prices.

    Returns None rather than a half-answer if too few comparables survive the
    filters -- a "percentile" out of three listings would read as a fact."""
    if not OWN_PROPERTY["price_czk"]:
        # Loud rather than a missing card nobody notices: the reference flat is
        # the reason half this dashboard exists.
        print(
            "::warning::OWN_PRICE_CZK není nastavené — karta „Tvůj byt\" se nevykreslí. "
            "Nastav ho jako secret repa.",
            file=sys.stderr,
        )
        return None
    own_per_sqm = round(OWN_PROPERTY["price_czk"] / OWN_PROPERTY["floor_area_sqm"])
    lo, hi = OWN_SIZE_BAND_SQM

    def eligible(c, size_band):
        if c.get("transaction_type") != "prodej":
            return False
        if c.get("disposition") != OWN_PROPERTY["disposition"]:
            return False
        if not c.get("price_czk_per_sqm"):
            return False
        # Co-ownership shares, auctions and mistyped areas -- already identified
        # as not-a-price by rank_deals. They belong nowhere near a median.
        if c.get("deal_outlier"):
            return False
        sqm = c.get("floor_area_sqm")
        if size_band:
            return sqm is not None and lo <= sqm <= hi
        return True

    band = sorted(c["price_czk_per_sqm"] for c in comparables if eligible(c, True))
    everything = sorted(c["price_czk_per_sqm"] for c in comparables if eligible(c, False))
    if len(band) < 8:
        return None

    def pct(vals, q):
        return vals[min(int(len(vals) * q), len(vals) - 1)]

    cheaper = sum(1 for v in band if v < own_per_sqm)
    return {
        "own_czk_per_sqm": own_per_sqm,
        "band_median": round(statistics.median(band)),
        "band_count": len(band),
        "disposition_median": round(statistics.median(everything)) if everything else None,
        "disposition_count": len(everything),
        "p10": pct(band, 0.10),
        "p90": pct(band, 0.90),
        "low": band[0],
        "high": band[-1],
        # Share of comparable adverts asking less per m² than he paid.
        "cheaper_pct": round(cheaper / len(band) * 100),
    }


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


def render_own_property_card(own):
    """Radim's own Kč/m² drawn as a line across the asking prices around it.

    A single number ("you paid X") says nothing without the spread it sits in,
    and a bare median hides how wide that spread is -- comparable 1+kk adverts
    in this circle run from about 149k to 328k per m². So the card draws the
    p10-p90 band and puts his price on it as a line."""
    if not own:
        return ""
    lo, hi = own["p10"], own["p90"]
    span = max(hi - lo, 1)

    def at(v):
        return max(0.0, min(100.0, (v - lo) / span * 100))

    def lab_at(v):
        """Label position, held off the ends so a centred caption can't be
        clipped by the card edge when a value sits at or outside the band."""
        return max(15.0, min(85.0, at(v)))

    def cz(x, places=1):
        return f"{x:.{places}f}".replace(".", ",")

    med, mine = own["band_median"], own["own_czk_per_sqm"]
    delta = (mine - med) / med * 100
    verdict = (
        f"o {cz(abs(delta))} % {'dráž' if delta > 0 else 'levněji'} než medián"
        if abs(delta) >= 0.5 else "prakticky na mediánu"
    )
    band_lo, band_hi = OWN_SIZE_BAND_SQM
    return f"""<div class="card" id="ownCard">
  <h2 style="margin-top:0;font-size:1rem;">🏠 Tvůj byt — {OWN_PROPERTY["disposition"]} {cz(OWN_PROPERTY["floor_area_sqm"])} m²</h2>
  <div class="own-head">
    <div class="own-big">{fmt_czk(mine)}<span>/m²</span></div>
    <div class="own-sub">{fmt_czk(OWN_PROPERTY["price_czk"])} za byt samotný<br>
      <span class="own-note">bez garáže a komory — jsou to samostatné jednotky a žádný inzerát je neobsahuje</span>
    </div>
  </div>
  <div class="own-scale">
    <div class="track"></div>
    <div class="tick med" style="left:{at(med):.1f}%"></div>
    <div class="tick own" style="left:{at(mine):.1f}%"></div>
    <div class="lab lab-top" style="left:{lab_at(mine):.1f}%">ty · {fmt_czk(mine)}/m²</div>
    <div class="lab lab-bot" style="left:{lab_at(med):.1f}%">medián · {fmt_czk(med)}/m²</div>
    <div class="lab lab-end" style="left:0;transform:none">{fmt_czk(lo)}/m²</div>
    <div class="lab lab-end" style="right:0;transform:none">{fmt_czk(hi)}/m²</div>
  </div>
  <p class="hint" style="margin-bottom:6px;">
    Srovnáno s <b>{own["band_count"]} prodeji {OWN_PROPERTY["disposition"]} o {band_lo:.0f}–{band_hi:.0f} m²</b>
    v oblasti — {verdict}, levněji za m² nabízí <b>{own["cheaper_pct"]} %</b> z nich.
    Pás výše je rozpětí p10–p90; celý vzorek jde od {fmt_czk(own["low"])}/m² do {fmt_czk(own["high"])}/m².
    Všechny {OWN_PROPERTY["disposition"]} v oblasti bez ohledu na výměru mají medián
    {fmt_czk(own["disposition_median"])}/m² ({own["disposition_count"]} inzerátů) — níž proto, že
    Kč/m² s výměrou klesá, ne proto, že by tam byly levnější nabídky.
  </p>
  <p class="hint" style="margin:0;color:#d9a3c0;">⚠ {OWN_PROPERTY["caveat"]}. A inzerát je nabídková cena,
    ne realizovaná — obojí posouvá srovnání v tvůj neprospěch a z těchhle dat se to odečíst nedá.</p>
</div>"""


def render_estimate_card(estimate):
    """The rent estimate, drawn where it can be checked against the adverts
    right below it (§12 step 3).

    Deliberately without the mortgage: this page is public, and the payment,
    the purchase price and which flat it is belong in the notification
    (R-5.6, R-10.1)."""
    if not estimate:
        return ""
    ref = estimate["reference"]
    base = estimate["base_total_per_sqm"]
    lo, hi = ref["size_band_sqm"]

    def cz(x, places=1):
        return f"{x:.{places}f}".replace(".", ",")

    def block(profile):
        rent, total = profile["rent"], profile["total"]
        return f"""<div class="est-profile">
      <div class="est-name">{html.escape(profile["name"])}</div>
      <div class="est-big">{fmt_czk(rent["median"])}<span>holý nájem / měs</span></div>
      <div class="est-range">p25–p75 {fmt_czk(rent["p25"])} – {fmt_czk(rent["p75"])}</div>
      <div class="est-second">{fmt_czk(total["median"])} <span>celkem, to platí nájemník</span></div>
      <div class="est-range">p25–p75 {fmt_czk(total["p25"])} – {fmt_czk(total["p75"])}</div>
      <div class="est-basis">{html.escape(profile["basis"])}</div>
    </div>"""

    if base["too_small"]:
        body = (
            f'<p class="hint" style="color:#d9a3c0;">⚠ Vzorek má jen {base["n"]} inzerátů. '
            "Pod osmi se medián nepublikuje — z tolika bytů se percentil čte jako fakt, "
            "a není.</p>"
        )
    else:
        # When no furnishing factor held, the two profiles are the same number
        # printed twice. Saying so beats letting the card imply the market does
        # not pay for furniture -- it is a gap in the data, not a finding.
        separated = estimate["mode"] == "hard_filters" or any(
            estimate["factors"].get(k, {}).get("usable") for k in ("zarizeny", "nezarizeny")
        )
        warning = "" if separated else (
            '<p class="hint" style="color:#d9a3c0;margin-top:10px;">⚠ Oba profily zatím vyšly '
            "stejně — v poolu není dost inzerátů s vyplněnou zařízeností, aby se daly oddělit. "
            "Mezera v datech, ne zjištění o trhu.</p>"
        )
        body = f"""<div class="est-grid">
    {block(estimate["profiles"]["nezarizeny"])}
    {block(estimate["profiles"]["zarizeny"])}
  </div>{warning}"""

    rows = ""
    for key in ("novostavba", "zarizeny", "nezarizeny"):
        f = estimate["factors"].get(key)
        if not f:
            continue
        factor = f"×{cz(f['factor'], 3)}" if f["usable"] else "nepoužit"
        contrast = f"{'+' if (f['contrast_pct'] or 0) > 0 else ''}{cz(f['contrast_pct'])} %" \
            if f["contrast_pct"] is not None else "—"
        rows += (
            f"<tr><td>{html.escape(f['label'])}</td><td>{f['n']}</td>"
            f"<td>{fmt_czk(f['median'])}</td><td>{factor}</td><td>{contrast}</td></tr>"
        )
    factors_html = (
        f"""<div class="est-scroll"><table class="est-table">
    <thead><tr><th>atribut</th><th>n</th><th>medián Kč/m²</th><th>faktor</th><th>vs. zbytek</th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>"""
        if rows else ""
    )
    # The same cross-check the weekly write-up prints: each factor is measured
    # against the whole base and then multiplied, so what the two attributes
    # share is counted twice. The subgroup's own median says by how much.
    checks = []
    # Only meaningful while factors are in play. Under hard filters the profile
    # IS the subgroup median, so the "cross-check" would compare a number with
    # itself and print a solemn 0 %.
    for key in (() if estimate["mode"] == "hard_filters" else ("nezarizeny", "zarizeny")):
        prof = estimate["profiles"][key]
        direct = prof.get("direct") or {}
        if not direct.get("available"):
            continue
        measured = direct["rent"]["median"]
        modelled = prof["rent"]["median"]
        gap = ""
        if measured and modelled:
            delta = (modelled - measured) / measured * 100
            gap = f", model o {'+' if delta > 0 else ''}{cz(delta)} % jinde"
        checks.append(
            f"{html.escape(prof['name'])}: přímý medián podskupiny "
            f"(n={direct['n']}) {fmt_czk(measured)} holého nájmu{gap}"
        )
    check_html = (
        f'<p class="hint">Kontrola proti datům — {" · ".join(checks)}. '
        "Faktory se měří každý zvlášť proti základu a pak násobí, takže co mají atributy "
        "společného, se počítá dvakrát. Rozdíl mezi modelem a podskupinou je šířka odpovědi, "
        "ne chyba jednoho z nich.</p>"
        if checks else ""
    )
    mode_note = (
        f"Režim <b>tvrdé filtry</b> (novostavba + bez provize) od {html.escape(str(estimate['hard_filters']['since']))}."
        if estimate["mode"] == "hard_filters" else
        f"Režim <b>široký základ + přirážky</b>. Filtrovaný pool má "
        f"{estimate['hard_filters']['n_this_week']} inzerátů; přepne se při "
        f"≥ {estimate['hard_filters']['min_n']} po {estimate['hard_filters']['weeks_required']} "
        "týdny v řadě."
    )
    return f"""<div class="card" id="estimateCard">
  <h2 style="margin-top:0;font-size:1rem;">💰 Odhad nájmu — {ref["disposition"]} {cz(ref["floor_area_sqm"])} m², novostavba</h2>
  {body}
  <p class="hint">Základ: <b>{base["n"]} inzerátů</b> {ref["disposition"]} o {lo:.0f}–{hi:.0f} m²
     s uvedenými poplatky, viděných za posledních {estimate["window_days"]} dní
     (medián {fmt_czk(base["median"])}/m² celkem).
     Živá nabídka by dala kolem šedesáti, třicetidenní okno skoro tři sta.</p>
  {factors_html}
  {check_html}
  <p class="hint">{mode_note}</p>
  <p class="hint">Přirážka se zavádí <b>jen</b> za zařízenost a stav budovy. Pro
     {html.escape(", ".join(estimate["not_separable"]))} vzorek rozdíl neoddělí — balkon v něm vyšel
     dokonce záporně, protože byty s balkonem jsou tady systematicky větší a větší byt má nižší
     Kč/m². To je vlastnost vzorku, ne trhu.</p>
  <p class="hint"><a href="{html.escape(report.REPO_URL, quote=True)}/tree/main/reports"
     target="_blank" rel="noopener" style="color:#7ab8ff;">📄 Týdenní zápisy a měsíční souhrny →</a></p>
</div>"""


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


# Bookkeeping the scraper needs across runs but the page never reads. The whole
# comparable set is inlined into the HTML, so at ~1000 listings every unused
# field is dead weight on a phone.
DASHBOARD_OMIT_FIELDS = (
    "cost_of_living_raw", "from_cache", "enrich_failed", "active",
    "missing_since", "removed_since", "first_seen",
)


def slim_for_dashboard(comp):
    return {k: v for k, v in comp.items() if k not in DASHBOARD_OMIT_FIELDS}


def render_dashboard(snapshot, changes, stats, history, estimate=None):
    tracked_list = snapshot["tracked"]
    comparables = snapshot["comparables"]

    for c in comparables:
        c["change_note"] = build_change_note(c["id"], changes)
    tracked_items = [build_tracked_item(t, changes) for t in tracked_list]

    data_json = json.dumps([slim_for_dashboard(c) for c in comparables], ensure_ascii=False)
    tracked_json = json.dumps(tracked_items, ensure_ascii=False)
    history_json = json.dumps(history, ensure_ascii=False)
    changed_ids = {c["id"] for c in changes.get("price_changes", [])}
    changed_ids_json = json.dumps(list(changed_ids))

    tracked_cards_html = "\n".join(render_tracked_card(t) for t in tracked_list)
    own_card_html = render_own_property_card(own_property_stats(comparables))
    estimate_card_html = render_estimate_card(estimate)

    head_and_body = f"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sreality Tracker – Vysočany, Hrdlořezy, Libeň, Karlín</title>
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
  .est-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }}
  .est-profile {{ background: #11141b; border-radius: 8px; padding: 10px 12px; }}
  .est-name {{ font-size: 0.7rem; color: #9aa; text-transform: uppercase; letter-spacing: .04em; }}
  .est-big {{ font-size: 1.5rem; font-weight: 700; color: #7CFFB2; line-height: 1.15; margin-top: 2px; }}
  .est-big span {{ display: block; font-size: 0.68rem; font-weight: 500; color: #8a9; letter-spacing: 0; }}
  .est-second {{ font-size: 1rem; font-weight: 600; color: #e6e6e6; margin-top: 8px; }}
  .est-second span {{ font-size: 0.68rem; font-weight: 400; color: #99a; }}
  .est-range {{ font-size: 0.7rem; color: #889; margin-top: 2px; }}
  .est-basis {{ font-size: 0.66rem; color: #777; margin-top: 8px; }}
  .est-scroll {{ overflow-x: auto; }}
  .est-table {{ width: 100%; border-collapse: collapse; font-size: 0.75rem; margin-top: 8px; }}
  .est-table th {{ text-align: left; color: #9aa; font-weight: 500; font-size: 0.68rem;
                   border-bottom: 1px solid #2a2f3a; padding: 4px 8px 4px 0; white-space: nowrap; }}
  .est-table td {{ padding: 4px 8px 4px 0; border-bottom: 1px solid #20242f; white-space: nowrap; }}
  .own-head {{ display: flex; gap: 14px; align-items: baseline; flex-wrap: wrap; }}
  .own-big {{ font-size: 1.7rem; font-weight: 700; color: #fc6; line-height: 1.1; }}
  .own-big span {{ font-size: 0.9rem; font-weight: 500; color: #997; }}
  .own-sub {{ font-size: 0.8rem; color: #bbb; }}
  .own-note {{ font-size: 0.7rem; color: #888; }}
  /* p10-p90 band with his price drawn on it. Absolute positioning inside a
     fixed-height box, so the labels can sit above and below the same line
     without pushing the layout around. */
  .own-scale {{ position: relative; height: 62px; margin: 16px 4px 4px; }}
  .own-scale .track {{ position: absolute; left: 0; right: 0; top: 26px; height: 8px;
                      border-radius: 4px; background: linear-gradient(90deg,#1e4620,#3f4415,#4a1c1c); }}
  .own-scale .tick {{ position: absolute; width: 2px; background: #7ab8ff; top: 18px; height: 24px; }}
  .own-scale .tick.own {{ width: 3px; background: #fc6; top: 12px; height: 36px; }}
  .own-scale .lab {{ position: absolute; font-size: 0.65rem; white-space: nowrap;
                    transform: translateX(-50%); }}
  .own-scale .lab-top {{ top: 0; color: #fc6; font-weight: 600; }}
  .own-scale .lab-bot {{ bottom: 0; color: #7ab8ff; }}
  .own-scale .lab-end {{ top: 44px; color: #777; }}
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
  .src.dupe {{ opacity: 0.45; }}
  .fee-na {{ color: #b9975b; font-style: italic; }}
  .fee-inc {{ color: #6fd08c; }}
  .fee-src {{ color: #888; font-size: 0.7rem; }}
  .deal-good {{ color: #7CFFB2; font-weight: 600; }}
  .deal-bad {{ color: #b98b8b; }}
  .deal {{ display: flex; align-items: center; gap: 10px; padding: 8px 0;
           border-bottom: 1px solid #262a33; cursor: pointer; }}
  .deal:last-child {{ border-bottom: none; }}
  .deal:hover {{ background: #20242f; }}
  .deal .dtxt {{ flex: 1; min-width: 0; }}
  .deal .dtitle {{ font-size: 0.84rem; font-weight: 600; }}
  .deal .dmeta {{ font-size: 0.7rem; color: #99a; margin-top: 2px; }}
  .deal .dpct {{ font-size: 1rem; font-weight: 700; color: #7CFFB2; flex-shrink: 0; }}
  .deals-list {{ max-height: 460px; overflow-y: auto; }}
  .hint {{ font-size: 0.7rem; color: #888; margin: 6px 0 0; }}
</style>
</head>
<body>
<header>
  <h1>Sreality Tracker · Vysočany → Hrdlořezy → Karlín</h1>
  <div class="updated">Last updated: {snapshot['generated_at']} · {AREA_RADIUS_KM} km kolem {AREA_LANDMARKS}</div>
</header>

{tracked_cards_html}

<div class="card" id="dealsCard">
  <h2 style="margin-top:0;font-size:1rem;">🔥 Nejlepší nabídky</h2>
  <div class="controls" style="margin:0 0 8px;">
    <select id="dealTx">
      <option value="pronajem">Pronájem</option>
      <option value="prodej">Prodej</option>
      <option value="">Vše</option>
    </select>
    <select id="dealDisp">
      <option value="">Všechny dispozice</option>
      <option value="1+kk">1+kk</option>
      <option value="1+1">1+1</option>
      <option value="2+kk">2+kk</option>
      <option value="2+1">2+1</option>
      <option value="3+kk">3+kk</option>
      <option value="3+1">3+1</option>
    </select>
  </div>
  <div class="deals-list" id="dealsList"></div>
  <p class="hint">Řazeno podle odchylky Kč/m² od mediánu <b>stejné dispozice</b> v oblasti — ne podle absolutní ceny,
     aby malý 1+kk a velký 3+kk šly porovnat. U pronájmů se počítá celková cena (nájem + poplatky + elektřina).
     Inzeráty bez uvedených poplatků se sem záměrně nedostanou: jejich celková cena je podhodnocená, takže by
     vypadaly levněji, než jsou.</p>
</div>

<div class="card" id="manageCard">
  <h2 style="margin-top:0;font-size:1rem;">⚙️ Sledované inzeráty</h2>
  <div id="trackedList"></div>
  <div class="controls" style="margin:10px 0 0;">
    <input id="addUrlInput" type="text" placeholder="https://www.sreality.cz/detail/..." style="flex:1;min-width:200px;">
    <button class="popup-btn" onclick="manageTracked({{add_url: document.getElementById('addUrlInput').value.trim()}})">➕ Sledovat</button>
  </div>
  <div id="patRow" class="controls" style="display:none;margin:8px 0 0;">
    <input id="patInput" type="password" placeholder="github_pat_…" style="flex:1;min-width:200px;">
    <button class="popup-btn" onclick="savePat()">Uložit token</button>
  </div>
  <div id="manageStatus" style="font-size:0.8rem;margin-top:8px;color:#7ab8ff;"></div>
  <div style="font-size:0.72rem;color:#888;margin-top:6px;">Spouští GitHub Action — změna se projeví za ~5–15 min, pak obnov stránku.
    Vyžaduje fine-grained PAT: jen repo sreality-tracker, oprávnění Actions „Read and write".
    <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">Vytvořit token</a> ·
    <button class="linklike" style="font-size:0.72rem;" onclick="localStorage.removeItem('gh_pat');document.getElementById('manageStatus').textContent='Token zapomenut.'">zapomenout token</button>
  </div>
</div>

{estimate_card_html}

{own_card_html}

<div class="card">
  <h2 style="margin-top:0;font-size:1rem;">Statistika oblasti ({", ".join(DISPOSITION_CODES.values())} · {AREA_RADIUS_KM} km)</h2>
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
        <th></th><th>Title</th><th>Type</th><th>Disp.</th><th>Nájem</th><th>Poplatky</th><th>Celkem</th><th>m²</th><th>Kč/m²</th><th>Odkaz</th>
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
    <option value="1+1">1+1</option>
    <option value="2+kk">2+kk</option>
    <option value="2+1">2+1</option>
    <option value="3+kk">3+kk</option>
    <option value="3+1">3+1</option>
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
  <label style="display:flex;align-items:center;gap:6px;font-size:0.85rem;">
    <input type="checkbox" id="filterFees" style="width:auto;"> Jen se známými poplatky
  </label>
  <input id="search" type="text" placeholder="Hledat název / ulici / lokalitu…">
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
      <th data-k="fees_czk" title="Měsíční poplatky za služby, jak je uvádí inzerát. „—“ znamená, že je inzerát neuvádí — celková cena je pak podhodnocená.">Poplatky</th>
      <th data-k="total_czk" title="Rent: nájem + poplatky + elektřina (real or estimated). Sale: purchase price.">Celkem</th>
      <th data-k="floor_area_sqm">m²</th>
      <th data-k="price_czk_per_sqm">Kč/m²</th>
      <th data-k="deal_pct" title="Odchylka Kč/m² od mediánu stejné dispozice v oblasti. Záporné = levnější.">vs. medián</th>
      <th data-k="dist_km" title="Vzdušná vzdálenost od středu sledované oblasti">km</th>
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
const DEAL_THRESHOLD = __DEAL_THRESHOLD__;
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

// Fees are the number Radim reads first, so the cell has to distinguish three
// genuinely different states rather than showing a bare dash for all of them:
// a stated fee, "the rent already includes it", and "the advert never says".
function fmtFees(r) {
  if (r.transaction_type !== "pronajem") return "—";
  if (r.fees_source === "included") return `<span class="fee-inc" title="Inzerát uvádí, že nájem je včetně poplatků">v ceně</span>`;
  if (r.fees_missing) return `<span class="fee-na" title="Inzerát poplatky neuvádí — celková cena je proto podhodnocená">neuvedeno</span>`;
  const fromText = r.fees_source === "text";
  return `<span title="${fromText ? "Vyčteno z popisu inzerátu" : "Z pole inzerátu"}">${fmtCzk(r.fees_czk)}${fromText ? ' <i class="fee-src">*</i>' : ""}</span>`;
}

function fmtDeal(r) {
  if (r.deal_pct === null || r.deal_pct === undefined) return "—";
  const cls = r.deal_ok ? "deal-good" : (r.deal_pct > 0 ? "deal-bad" : "");
  let warn = "";
  if (r.deal_outlier) {
    warn = ' <span class="fee-na" title="Tak hluboko pod trhem, že to obvykle není běžný prodej — podíl, dražba, družstevní byt bez anuity nebo špatně uvedená výměra. Ověř v inzerátu.">⚠</span>';
  } else if (r.transaction_type === "pronajem" && r.fees_missing) {
    warn = ' <span class="fee-na" title="Bez poplatků — srovnání není spolehlivé">?</span>';
  }
  return `<span class="${cls}">${r.deal_pct > 0 ? "+" : ""}${r.deal_pct}%</span>${warn}`;
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

// The same flat is usually advertised on more than one portal. We keep one row
// but never hide the others -- they show as dimmed badges next to the source.
function alsoBadges(r) {
  if (!r.also_on || !r.also_on.length) return "";
  return r.also_on.map(a => srcBadge(a.source).replace('class="src"', 'class="src dupe"')).join("");
}

function portalName(s) {
  return ({ sreality: "Sreality", bezrealitky: "Bezrealitky", idnes: "iDNES" })[s] || "Sreality";
}

/* ---- správa sledovaných inzerátů (workflow_dispatch s fine-grained PAT) ---- */
const GH_REPO = "radim225/sreality-tracker";
let pendingInputs = null;

function setManageStatus(msg) {
  document.getElementById("manageStatus").textContent = msg;
}

function renderTrackedList() {
  const el = document.getElementById("trackedList");
  if (!el) return;
  el.innerHTML = TRACKED.length ? TRACKED.map(t => `
    <div class="history-item" style="cursor:default;">
      <img class="thumb" src="${escapeHtml(t.thumb || PLACEHOLDER)}" loading="lazy" onerror="this.src=PLACEHOLDER">
      <div class="htxt">${escapeHtml(t.title || String(t.id))}
        <div class="hat">${escapeHtml(t.locality || "")} · ${t.active ? "aktivní" : "neaktivní"}</div>
      </div>
      <button class="popup-btn" style="background:#7f1d1d;" title="Přestat sledovat"
        onclick="manageTracked({remove_url: ${escapeHtml(JSON.stringify(String(t.id).replace(/^(bez|idnes)-/, "")))}})">🗑 Přestat sledovat</button>
    </div>`).join("") : '<div style="color:#888;font-size:0.8rem;">Žádné sledované inzeráty.</div>';
}

function savePat() {
  const v = document.getElementById("patInput").value.trim();
  if (!v) return;
  localStorage.setItem("gh_pat", v);
  document.getElementById("patInput").value = "";
  document.getElementById("patRow").style.display = "none";
  if (pendingInputs) { const p = pendingInputs; pendingInputs = null; manageTracked(p); }
}

async function manageTracked(inputs) {
  const val = inputs.add_url ?? inputs.remove_url;
  if (!val) { setManageStatus("Vlož URL inzerátu ze Sreality."); return; }
  const token = localStorage.getItem("gh_pat");
  if (!token) {
    pendingInputs = inputs;
    document.getElementById("patRow").style.display = "flex";
    setManageStatus("Vlož GitHub token (fine-grained: jen toto repo, Actions Read & write) — akce se pak provede.");
    return;
  }
  setManageStatus("Spouštím workflow…");
  try {
    const resp = await fetch(`https://api.github.com/repos/${GH_REPO}/actions/workflows/scrape.yml/dispatches`, {
      method: "POST",
      headers: { "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json" },
      body: JSON.stringify({ ref: "main", inputs }),
    });
    if (resp.status === 204) {
      setManageStatus((inputs.add_url ? "Přidání" : "Odebrání") + " spuštěno ✓ — hotovo za ~5–15 min, pak obnov stránku.");
      if (inputs.add_url) document.getElementById("addUrlInput").value = "";
    } else if (resp.status === 401 || resp.status === 403) {
      localStorage.removeItem("gh_pat");
      pendingInputs = inputs;
      document.getElementById("patRow").style.display = "flex";
      setManageStatus(`GitHub token odmítl (HTTP ${resp.status}) — vlož platný token.`);
    } else {
      setManageStatus(`Neočekávaná odpověď (HTTP ${resp.status}).`);
    }
  } catch (e) {
    setManageStatus("Požadavek selhal: " + e.message);
  }
}

function costBreakdownHtml(item) {
  const adminRow = item.admin_fee_czk
    ? `<div class="cost-row"><span>📋 Administrativní poplatek (jednorázově)</span><span>${fmtCzk(item.admin_fee_czk)}</span></div>`
    : "";
  if (item.transaction_type !== "pronajem") {
    return `<div class="cost-box"><div class="cost-row total"><span>💰 Cena</span><span>${fmtCzk(item.price_czk)}</span></div>${adminRow}</div>`;
  }
  if (item.fees_source === "included") {
    return `<div class="cost-box">
      <div class="cost-row"><span>🏠 Nájem</span><span>${fmtCzk(item.price_czk)}</span></div>
      <div class="cost-row total"><span>💰 Celkem</span><span>${fmtCzk(item.total_czk)}</span></div>
      ${adminRow}
      <div class="cost-note">Inzerát uvádí, že nájem je už včetně poplatků a energií — nic se nepřičítá.</div>
      ${item.price_note ? `<div class="cost-note">Poznámka k ceně: ${escapeHtml(item.price_note)}</div>` : ""}
    </div>`;
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
    ${item.price_note ? `<div class="cost-note">Poznámka k ceně (z inzerátu): ${escapeHtml(item.price_note)}</div>` : ""}
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
    ${(item.also_on || []).map(a => `<a class="modal-link" style="background:#334155;" href="${escapeHtml(a.url)}" target="_blank" rel="noopener">Také na ${portalName(a.source)} →</a>`).join(" ")}
    ${item.also_on && item.also_on.length ? '<div class="cost-note">Stejný byt inzerovaný na více portálech — sloučeno do jednoho řádku, odkazy na ostatní výše.</div>' : ""}
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
      <td>${fmtFees(r)}</td>
      <td>${fmtTotal(r)}</td>
      <td>${r.floor_area_sqm ?? '—'}</td>
      <td>${fmtCzk(r.price_czk_per_sqm)}</td>
      <td>${r.url ? `<a href="${escapeHtml(r.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Otevřít ↗</a>` : '—'}</td>
    </tr>`).join("") : `<tr><td colspan="10" style="color:#888;">No other Pod Harfou listings currently found.</td></tr>`;
}

// Deals: cheapest-for-what-it-is, not merely cheapest. Ranked by how far a
// listing's Kč/m² sits below the median for its own disposition, so a small
// 1+kk and a large 3+kk can appear in the same list on equal terms.
function renderDeals() {
  const tx = document.getElementById("dealTx").value;
  const disp = document.getElementById("dealDisp").value;
  // Two agencies advertising the same flat under different street labels
  // ("Pod Pekárnami" vs "Kolbenova") are too risky to merge in the main table —
  // in a dense area two genuinely different 2+kk can share an area and a price,
  // and hiding a real listing is worse than showing one twice. In a curated
  // top-12 the trade-off flips: a repeat wastes a slot, so identical
  // disposition+area+price collapses here and here only.
  const seen = new Set();
  const rows = DATA
    .filter(r => r.deal_ok && (!tx || r.transaction_type === tx) && (!disp || r.disposition === disp))
    .sort((a, b) => a.deal_pct - b.deal_pct)
    .filter(r => {
      const k = [r.transaction_type, r.disposition, r.floor_area_sqm, r.price_czk].join("|");
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    })
    .slice(0, 12);
  const el = document.getElementById("dealsList");
  if (!rows.length) {
    el.innerHTML = `<div style="color:#888;font-size:0.8rem;">Žádná nabídka teď není aspoň ${DEAL_THRESHOLD} % pod mediánem své dispozice.</div>`;
    return;
  }
  el.innerHTML = rows.map(r => `
    <div class="deal" onclick="openModal(${escapeHtml(JSON.stringify(r.id))})">
      <img class="thumb" src="${escapeHtml(r.thumb || PLACEHOLDER)}" loading="lazy" onerror="this.src=PLACEHOLDER">
      <div class="dtxt">
        <div class="dtitle">${escapeHtml(r.title || String(r.id))}</div>
        <div class="dmeta">${escapeHtml(r.locality || r.city_part || "")} · ${r.disposition || "—"}
          · ${r.transaction_type === "pronajem" ? "pronájem" : "prodej"}${srcBadge(r.source)}${alsoBadges(r)}</div>
        <div class="dmeta">${fmtTotal(r)}${r.transaction_type === "pronajem" ? "/měs. vč. poplatků" : ""}
          · ${fmtCzk(r.price_czk_per_sqm)}/m²${r.old_price_czk ? ` · <span style="color:#7CFFB2;">zlevněno z ${fmtCzk(r.old_price_czk)}</span>` : ""}</div>
      </div>
      <div class="dpct">${r.deal_pct}%</div>
    </div>`).join("");
}

function render() {
  const tx = document.getElementById("filterTx").value;
  const disp = document.getElementById("filterDisp").value;
  const source = document.getElementById("filterSource").value;
  const podOnly = document.getElementById("filterPodHarfou").checked;
  const feesOnly = document.getElementById("filterFees").checked;
  const q = document.getElementById("search").value.toLowerCase();
  let rows = DATA.filter(r => {
    if (tx && r.transaction_type !== tx) return false;
    if (disp && r.disposition !== disp) return false;
    if (source && r.source !== source) return false;
    if (podOnly && !r.pod_harfou) return false;
    // Only meaningful for rentals — a sale has no monthly fee to be missing.
    if (feesOnly && r.transaction_type === "pronajem" && r.fees_missing) return false;
    if (q && !((r.title||"").toLowerCase().includes(q) || (r.city_part||"").toLowerCase().includes(q) || (r.street||"").toLowerCase().includes(q) || (r.locality||"").toLowerCase().includes(q))) return false;
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
      <td>${fmtFees(r)}</td>
      <td>${fmtTotal(r)}</td>
      <td>${r.floor_area_sqm ?? '—'}</td>
      <td>${fmtCzk(r.price_czk_per_sqm)}${CHANGED_IDS.has(r.id) ? ' ⚡' : ''}</td>
      <td>${fmtDeal(r)}</td>
      <td>${r.dist_km != null ? r.dist_km.toFixed(1) : '—'}</td>
      <td>${escapeHtml(r.locality || r.city_part || '—')}${srcBadge(r.source)}${alsoBadges(r)}</td>
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
document.getElementById("filterFees").addEventListener("change", render);
document.getElementById("search").addEventListener("input", render);
document.getElementById("dealTx").addEventListener("change", renderDeals);
document.getElementById("dealDisp").addEventListener("change", renderDeals);
render();
renderDeals();
renderPodHarfou();
renderHistory();
renderTrackedList();
initMap();
"""

    js = (
        js_template.replace("__TRACKED_JSON__", tracked_json)
        .replace("__HISTORY_JSON__", history_json)
        .replace("__ELECTRICITY_CZK__", str(ELECTRICITY_ESTIMATE_CZK))
        .replace("__DEAL_THRESHOLD__", str(DEAL_THRESHOLD_PCT))
        .replace("__DATA_JSON__", data_json)
        .replace("__CHANGED_IDS_JSON__", changed_ids_json)
    )

    # Not named `html`: that would shadow the stdlib module of the same name,
    # which this function's f-strings call for escaping.
    document = head_and_body + js + "</script>\n</body>\n</html>\n"
    DASHBOARD_PATH.write_text(document, encoding="utf-8")


def due_weekly_report(now):
    """The week that has just ended, if it hasn't been written up yet.

    File existence is the whole guard: the scraper runs every 8 h, so the first
    run of a new ISO week writes the report and the next two find it already
    there. No extra cron, no state to get out of sync, and a re-run of a failed
    job simply picks up where it left off."""
    target = market.previous_week_key(market.iso_week_key(now))
    return None if report.report_path(target).exists() else target


def due_monthly_report(now):
    target = market.previous_month_key(market.month_key(now))
    return None if report.month_report_path(target).exists() else target


def has_data_for(all_pool, start, end):
    """Don't write up a period the pool cannot describe -- the first run after
    deployment would otherwise emit a confident report about a week it never
    observed."""
    for rec in all_pool.values():
        seen = pool.parse_ts(rec.get("last_seen"))
        if seen and start <= seen <= end:
            return True
    return False


def update_pool_and_reports(snapshot, changes):
    """Fold the run into the pool, then write up anything that is due.

    Returns (estimate_for_dashboard, notes). Raises on a genuine failure: a
    broken report has to turn the run red rather than leave a week silently
    missing from the archive (R-8.5)."""
    now = snapshot["generated_at"]
    all_pool = pool.load_pool()
    state = pool.load_state()
    config_changed = pool.note_config(state, snapshot["config"], now)
    counts = pool.update_from_snapshot(all_pool, snapshot, changes, at=now)
    shards = pool.save_pool(all_pool)
    notes = [
        f"Pool: {len(all_pool)} inzerátů celkem, +{counts['new']} nových, "
        f"{counts['repriced']} změn ceny, {counts['gone']} potvrzeně pryč, "
        f"{counts['resurrected']} se vrátilo · shardy {', '.join(shards)}"
    ]
    if config_changed:
        notes.append("Konfigurace hledání se změnila — zapsáno do pool/state.json (R-6.5).")

    weekly_meta = None
    week = due_weekly_report(now)
    if week:
        start, end = market.week_bounds(week)
        if has_data_for(all_pool, start, end):
            weekly_meta = report.build_weekly(all_pool, state, week)
            path = report.write_weekly(weekly_meta)
            report.remember(state, weekly_meta)
            notes.append(f"Týdenní zápis {week}: {weekly_meta['verdict']} → {path.name}")
        else:
            notes.append(f"Týdenní zápis {week} přeskočen — pool z toho týdne nemá data.")
    else:
        # The report for the last finished week exists, so the file guard says
        # "done" -- but the message may never have gone out (Telegram was down,
        # the secret was missing). Rebuild and try again, because a week without
        # a notification looks exactly like a quiet week, and that confusion is
        # the one thing R-8.4 exists to prevent.
        # Only the week that just ended is worth resending. Without this, a
        # channel configured in October would open with an August write-up.
        pending = (state.get("last_report") or {}).get("week")
        latest = market.previous_week_key(market.iso_week_key(now))
        if pending == latest and state.get("notified_week") != pending:
            weekly_meta = report.build_weekly(all_pool, state, pending)
            notes.append(f"Týdenní zápis {pending} byl zapsán dřív, ale neodeslal se — zkouším znovu.")

    month = due_monthly_report(now)
    if month:
        start, end = market.month_bounds(month)
        if has_data_for(all_pool, start, end):
            meta = report.build_monthly(all_pool, state, month)
            path = report.write_monthly(meta)
            notes.append(f"Měsíční souhrn {month} → {path.name}")
        else:
            notes.append(f"Měsíční souhrn {month} přeskočen — pool z toho měsíce nemá data.")

    # State is durable before anything can raise: a notification that fails must
    # cost a retry, not the record of the week.
    pool.save_state(state)

    if weekly_meta is not None:
        channel = notify.notify(weekly_meta)
        notes.append(f"Notifikace: {channel or 'žádný kanál nenastaven'}")
        if channel:
            state["notified_week"] = weekly_meta["week"]
            pool.save_state(state)

    # Read-only with respect to the mode: a switch to hard filters is a thing
    # the report announces, not something a dashboard render does quietly.
    estimate = market.rent_estimate(
        pool.window(all_pool), as_of=now, state=json.loads(json.dumps(state)),
        week_key=market.iso_week_key(now), allow_switch=False,
    )
    return estimate, notes


def main():
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    prev = load_latest_snapshot()
    prev_tracked_by_id = {t["id"]: t for t in (prev or {}).get("tracked", [])}

    tracked_config = load_tracked_config()
    print(f"Fetching {len(tracked_config)} tracked listing(s)...", file=sys.stderr)
    tracked = []
    for t in tracked_config:
        fetched = fetch_tracked(t["url"], t["id"])
        prev_t = prev_tracked_by_id.get(t["id"])
        unreadable = fetched.get("unavailable", False)
        if unreadable:
            # We simply could not read the page. Keep showing the last known
            # state rather than flipping the listing to "no longer listed" --
            # that would fire a removal alert about an upstream hiccup.
            print(
                f"Tracked id={t['id']} unreadable, keeping last known state: {fetched['error']}",
                file=sys.stderr,
            )
            fetched = (
                {**prev_t, "fetch_error": fetched["error"], "fetch_error_at": fetched["fetched_at"]}
                if prev_t
                else {**fetched, "active": False}
            )
        elif not fetched.get("active"):
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
        if not unreadable:
            # A successful read clears any error carried over from a failed run.
            fetched.pop("fetch_error", None)
            fetched.pop("fetch_error_at", None)
        tracked.append(fetched)
        print(f"Tracked id={fetched['id']} active={fetched.get('active')} title={fetched.get('title')!r}", file=sys.stderr)

    print("Fetching comparables...", file=sys.stderr)
    comparables = fetch_comparables(prev)
    for c in comparables:
        c.setdefault("source", "sreality")
    print("Fetching extra sources (Bezrealitky, iDNES)...", file=sys.stderr)
    sources.configure(
        AREA_CENTER, AREA_RADIUS_KM, DISPOSITION_CODES.values(),
        extract_fees_and_electricity, cost_breakdown,
        street_gps=STREET_GPS,
        prev_comparables=(prev or {}).get("comparables", []),
        prev_fold_cache=fold_cache_records((prev or {}).get("enrichment_cache")),
        parser_version=PARSER_VERSION,
    )
    comparables += sources.fetch_extra_comparables()
    comparables, folded = merge_cross_portal(comparables)
    rank_deals(comparables)
    print(f"Found {len(comparables)} unique comparable listings", file=sys.stderr)

    snapshot = {
        "generated_at": now_iso(),
        # What the search looked at this run. diff_snapshots compares it against
        # the previous snapshot's so a change of area/filters can't masquerade
        # as the market moving.
        "config": config_fingerprint(),
        "tracked": tracked,
        "comparables": comparables,
        # Listings absent this run and awaiting a second confirming absence
        # before they count as removed; diff_snapshots() fills this in.
        "pending_removal": [],
        # Enrichment of the cross-portal duplicates that dedup removed above, so
        # the next run knows it has already read them.
        "enrichment_cache": build_enrichment_cache(folded),
    }

    changes = diff_snapshots(prev, snapshot)
    verify_removals(changes, snapshot)
    # Restored listings rejoin the set, so the medians and deal ranking are
    # recomputed over the final population rather than the pre-verification one.
    comparables = rank_deals(snapshot["comparables"])
    stats = compute_stats(comparables)
    snapshot["stats"] = stats
    history = update_changes_history(changes)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_path = SNAPSHOTS_DIR / f"snapshot-{ts}.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    LATEST_SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    CHANGES_PATH.write_text(json.dumps(changes, ensure_ascii=False, indent=2))

    # The snapshot is on disk before anything downstream runs, so a bug in the
    # pool or the write-up costs a report, never a run's worth of scraping.
    estimate, pool_error = None, None
    try:
        estimate, notes = update_pool_and_reports(snapshot, changes)
        for note in notes:
            print(note, file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 -- re-raised below, after the render
        pool_error = exc
        print(f"::error::Pool nebo týdenní zápis selhal: {exc}", file=sys.stderr)

    render_dashboard(snapshot, changes, stats, history, estimate)

    print(f"Snapshot saved: {snapshot_path}", file=sys.stderr)
    print(f"Stats: {stats}", file=sys.stderr)
    print(
        f"Changes: tracked_price_changes={len(changes['tracked_price_changes'])} "
        f"new={len(changes['new_listings'])} gone={len(changes['newly_inactive'])} "
        f"price_changes={len(changes['price_changes'])} "
        f"pending_removal={len(snapshot['pending_removal'])}",
        file=sys.stderr,
    )
    if pool_error is not None:
        # Everything that could be saved has been saved; now make the run red.
        # A week missing from the archive must never be something you find out
        # about a month later (R-8.5).
        raise pool_error


if __name__ == "__main__":
    main()
