#!/usr/bin/env python3
"""Tests for listing overrides.

A corrected m² or fee has to land in the estimate (recompute total and Kč/m²).
An excluded listing stays on the page but off the median. Deleting an override
returns the parser's numbers. The same id, gone then active, keeps the record.

Run: python3 test_overrides.py
"""
import json
import sys
import tempfile
from pathlib import Path

import market
import pool
import scrape

FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}\n        got  {got!r}\n        want {want!r}")
        FAILURES.append(label)


def listing(**kwargs):
    rec = {
        "id": 1,
        "transaction_type": "pronajem",
        "price_czk": 20000,
        "fees_czk": None,
        "fees_missing": True,
        "fees_source": None,
        "electricity_czk": scrape.ELECTRICITY_ESTIMATE_CZK,
        "electricity_estimated": True,
        "total_czk": 20000 + scrape.ELECTRICITY_ESTIMATE_CZK,
        "floor_area_sqm": 30,
        "price_czk_per_sqm": round((20000 + scrape.ELECTRICITY_ESTIMATE_CZK) / 30),
        "title": "Byt 1+kk",
        "url": "https://example.test/1",
        "disposition": "1+kk",
    }
    rec.update(kwargs)
    rec["price_czk_per_sqm"] = kwargs.get(
        "price_czk_per_sqm",
        round((rec["total_czk"] or 0) / rec["floor_area_sqm"]) if rec.get("floor_area_sqm") and rec.get("total_czk") else rec.get("price_czk_per_sqm"),
    )
    return rec


# --- Kč/m² after area ---------------------------------------------------- #
l = listing()
parser_sqm = l["price_czk_per_sqm"]
scrape.apply_overrides([l], {"1": {"id": "1", "floor_area_sqm": 40}})
check("area override replaces parser m²", l["floor_area_sqm"], 40.0)
check("Kč/m² recomputed from new area", l["price_czk_per_sqm"], round(l["total_czk"] / 40))
check("Kč/m² actually moved", l["price_czk_per_sqm"] != parser_sqm, True)
check("fees still parser when field omitted", l["fees_missing"], True)


# --- fees in total ------------------------------------------------------- #
l = listing()
scrape.apply_overrides([l], {"1": {"id": "1", "fees_czk": 3000}})
check("fees_missing cleared", l["fees_missing"], False)
check("fees_czk stored", l["fees_czk"], 3000)
check("fees_source is override", l["fees_source"], "override")
check("total includes fees + electricity", l["total_czk"], 20000 + 3000 + scrape.ELECTRICITY_ESTIMATE_CZK)
check("Kč/m² uses the new total", l["price_czk_per_sqm"], round((20000 + 3000 + scrape.ELECTRICITY_ESTIMATE_CZK) / 30))


# --- exclude from median, stays on the page ------------------------------ #
rows = [
    listing(id=1, price_czk_per_sqm=1000, total_czk=30000, floor_area_sqm=30, fees_missing=False),
    listing(id=2, price_czk_per_sqm=1000, total_czk=30000, floor_area_sqm=30, fees_missing=False),
    listing(id=3, price_czk_per_sqm=9000, total_czk=270000, floor_area_sqm=30, fees_missing=False),
]
scrape.apply_overrides(rows, {"3": {"id": "3", "exclude_from_stats": True, "note": "družstevní převod"}})
stats = scrape.compute_stats(rows)
check("excluded listing stays in the list", len(rows), 3)
check("excluded listing still has its Kč/m²", rows[2]["price_czk_per_sqm"] is not None, True)
check("median ignores the excluded outlier", stats["rent_median_czk_per_sqm"], 1000)
check("count ignores the excluded outlier", stats["rent_count"], 2)
check("exclude flag is on the row", rows[2]["exclude_from_stats"], True)
check("base_eligible drops excluded", market.base_eligible(rows[2]), False)
check("non-excluded rental can still be eligible",
      market.base_eligible(listing(id=8, fees_missing=False, fees_czk=3000,
                                  total_czk=24500, price_czk_per_sqm=817)), True)


# --- sale: exclude + note, do not invent rent ---------------------------- #
sale = {
    "id": "bez-abc",
    "transaction_type": "prodej",
    "price_czk": 5_000_000,
    "fees_czk": None,
    "fees_missing": True,
    "total_czk": 5_000_000,
    "floor_area_sqm": 32,
    "price_czk_per_sqm": round(5_000_000 / 32),
    "title": "Prodej",
}
scrape.apply_overrides([sale], {
    "bez-abc": {"id": "bez-abc", "exclude_from_stats": True, "note": "ne-tržní"},
})
check("sale exclude does not invent rent", sale.get("rent_czk"), None)
check("sale price untouched", sale["price_czk"], 5_000_000)
check("bez- id matches", sale["exclude_from_stats"], True)


# --- delete -------------------------------------------------------------- #
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "overrides.json"
    rec = scrape.upsert_override({"id": 99, "fees_czk": 1111, "note": "x"}, path)
    check("upsert keys by str id", rec["id"], "99")
    stored = json.loads(path.read_text())
    check("file keyed by id", "99" in stored, True)
    l = listing(id=99)
    scrape.apply_overrides([l], scrape.load_overrides(path))
    check("applied before delete", l["fees_czk"], 1111)
    removed = scrape.drop_override(99, path)
    check("drop returns the record", removed["id"], "99")
    check("file empty after delete", scrape.load_overrides(path), {})
    check("drop of missing id is a no-op", scrape.drop_override(99, path), None)
    fresh = listing(id=99)
    scrape.apply_overrides([fresh], scrape.load_overrides(path))
    check("parser fees after delete", fresh["fees_missing"], True)
    check("parser total after delete", fresh["total_czk"], 20000 + scrape.ELECTRICITY_ESTIMATE_CZK)


# --- id survives gone then active ---------------------------------------- #
ov = {"idnes-7": {"id": "idnes-7", "floor_area_sqm": 41}}
live = [listing(id="idnes-7", floor_area_sqm=29)]
scrape.apply_overrides(live, ov)
check("applied while live", live[0]["floor_area_sqm"], 41.0)
# Listing gone: overrides.json is not pruned.
check("override record stays when listing is absent", "idnes-7" in ov, True)
gone_apply = []
scrape.apply_overrides(gone_apply, ov)
check("apply on empty list does not drop the record", "idnes-7" in ov, True)
back = [listing(id="idnes-7", floor_area_sqm=29)]
scrape.apply_overrides(back, ov)
check("same id after return still corrected", back[0]["floor_area_sqm"], 41.0)


# --- fee_review_queue is not rewritten by an override -------------------- #
queued = listing(id=5, fee_unsure=["lookahead"], transaction_type="pronajem")
before = scrape.build_fee_review_queue([queued])
scrape.apply_overrides([queued], {"5": {"id": "5", "fees_czk": 2500}})
after = scrape.build_fee_review_queue([queued])
check("override does not empty the fee queue", [q["id"] for q in after], [q["id"] for q in before])
check("queue still holds the flagged rental", [q["id"] for q in after], [5])


# --- pool is not silently deleted ---------------------------------------- #
p = {}
pool.update_from_snapshot(p, {
    "generated_at": "2026-08-01T00:00:00Z",
    "comparables": [
        listing(id=10, fees_missing=False, fees_czk=2000, total_czk=23500),
        listing(id=11, fees_missing=False, fees_czk=2000, total_czk=23500),
    ],
})
n_before = len(p)
rows = [listing(id=10, fees_missing=False, fees_czk=2000, total_czk=23500)]
scrape.apply_overrides(rows, {"10": {"id": "10", "floor_area_sqm": 35}})
pool.update_from_snapshot(p, {
    "generated_at": "2026-08-02T00:00:00Z",
    "comparables": rows,
})
# Listing 11 is absent this snapshot — still in the pool (absence ≠ delete).
check("pool size not reduced by override apply", len(p), n_before)
check("absent listing stays in the pool", "11" in p, True)
scrape.apply_overrides(list(p.values()), {"10": {"id": "10", "floor_area_sqm": 35}}, stamp_ui=False)
check("pool still has both records after re-apply", sorted(p), ["10", "11"])
check("absent listing 11 still has its url", bool(p["11"].get("url")), True)
check("corrected area lands on the pool record", p["10"]["floor_area_sqm"], 35.0)


# --- card: empty hidden, XSS escaped, gone listing shown ----------------- #
check("empty overrides make no card", scrape.overrides_card({}, []), "")
evil = '<script>alert("xss")</script> & "x"'
card = scrape.overrides_card(
    {"bez-1": {"id": "bez-1", "note": evil, "exclude_from_stats": True}},
    [{"id": "bez-1", "title": evil, "url": "https://example.com/a?x=1&y=2"}],
)
check("script in note does not pass", "<script>alert" in card, False)
check("note is escaped", "&lt;script&gt;" in card, True)
check("card title is Opravy", "Opravy" in card, True)
gone_card = scrape.overrides_card(
    {"400": {"id": "400", "floor_area_sqm": 33, "note": "čeká"}},
    [],
)
check("gone listing still listed on the card", "400" in gone_card, True)
check("gone listing is labelled as waiting", "není v nabídce" in gone_card, True)


# --- normalize rejects junk ---------------------------------------------- #
try:
    scrape.normalize_override({"fees_czk": 1})
    check("id is required", True, False)
except ValueError:
    check("id is required", True, True)
try:
    scrape.normalize_override({"id": "1", "floor_area_sqm": 0})
    check("zero m² rejected", True, False)
except ValueError:
    check("zero m² rejected", True, True)


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED:")
    for f in FAILURES:
        print(f"  {f}")
    sys.exit(1)
print("all override checks pass")
