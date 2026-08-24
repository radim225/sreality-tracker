#!/usr/bin/env python3
"""Regression tests for the enrichment cache across cross-portal dedup.

Two failures this guards against, both silent -- neither raises, neither shows
up on the dashboard, and both were only visible in the Actions log:

1. Dedup folds ~300 iDNES adverts into their Sreality twins every run, which
   removes them from the snapshot. Without a record that they were read, the
   next run treats them as new and burns its whole detail budget re-reading
   them -- measured at exactly the 200/run cap on three consecutive runs, with
   ~117 more deferred that were therefore never read at all.

2. PARSER_VERSION invalidates cached fee data, but the check only ever existed
   on the Sreality path. A fee fix would never reach a Bezrealitky or iDNES
   advert already in the cache.

Run: python3 test_cache.py
"""
import json
import sys

import scrape
import sources


def case_fold_returns_duplicates():
    """merge_cross_portal hands back what it folded, instead of dropping it."""
    twin = dict(id=1, source="sreality", transaction_type="pronajem",
                disposition="2+kk", floor_area_sqm=50.0, price_czk=20000,
                street="Kolbenova", url="https://sreality/1", description="popis",
                fees_missing=True, fees_czk=None)
    copy = dict(twin, id="idnes-9", source="idnes", url="https://idnes/9",
                total_czk=24000,
                fees_czk=2500, fees_missing=False, fees_source="text",
                parser_version=scrape.PARSER_VERSION)
    survivors, folded = scrape.merge_cross_portal([twin, copy])
    assert len(survivors) == 1, survivors
    assert [c["id"] for c in folded] == ["idnes-9"], folded
    # The fee the folded copy carried is still on the row Radim sees.
    assert survivors[0]["fees_czk"] == 2500, survivors[0]
    return folded


def case_cache_is_compact(folded):
    """The cache keeps what decides a re-fetch, not the bulk of the record."""
    cache = scrape.build_enrichment_cache(folded)
    entry = cache["idnes-9"]
    assert entry["fees_czk"] == 2500, entry
    assert entry["cached_only"] is True, entry
    # Descriptions are most of a record's size, and the folded copy's was
    # already donated to the survivor, so keeping a second copy is dead weight.
    assert "description" not in entry, entry
    return cache


def case_folded_advert_is_not_refetched(cache):
    """The regression: a fold-cache record used to read as a failed read.

    needs_enrichment treats a missing description as "the last detail fetch
    failed, try again". A fold-cache record has none by design, so every folded
    advert asked to be re-read on every run.
    """
    prev = scrape.fold_cache_records(cache)["idnes-9"]
    comp = dict(id="idnes-9", price_czk=20000)
    needed, reason = scrape.needs_enrichment(comp, prev)
    assert not needed, f"folded advert asked for a detail fetch again ({reason})"

    # A repriced one still gets read: the fee moves with the rent.
    needed, reason = scrape.needs_enrichment(dict(id="idnes-9", price_czk=21000), prev)
    assert needed and reason == "price", (needed, reason)


def case_sources_skip_cached_folded_advert(cache):
    """Same for the Bezrealitky/iDNES planner, which has its own cache."""
    sources.configure(
        (50.0995, 14.49), 3.0, ["2+kk"], None, None,
        prev_fold_cache=scrape.fold_cache_records(cache),
        parser_version=scrape.PARSER_VERSION,
    )
    comp = dict(id="idnes-9", price_czk=20000)
    assert sources._plan_detail_fetches([comp], "idnes") == [], "re-fetched a known advert"
    # ...and the cached fee is restored onto the fresh card.
    assert comp["fees_czk"] == 2500, comp


def case_parser_bump_flushes_source_cache():
    """A parser fix has to reach adverts already in the cache, not just new ones.

    Uses a fully-read advert -- description and all -- because that is the case
    only the version check can catch: nothing else about the record looks stale.
    This is the shape of the ~40 Bezrealitky rows that survive dedup and sit in
    the snapshot from one run to the next.
    """
    stored = dict(id="bez-7", price_czk=20000, description="popis",
                  fees_czk=2500, fees_missing=False, fees_source="text",
                  parser_version=scrape.PARSER_VERSION)
    fresh = lambda: dict(id="bez-7", price_czk=20000)

    sources.configure((50.0995, 14.49), 3.0, ["2+kk"], None, None,
                      prev_comparables=[stored],
                      parser_version=scrape.PARSER_VERSION)
    assert sources._plan_detail_fetches([fresh()], "bezrealitky") == [], \
        "re-read an advert whose fee is already current"

    sources.configure((50.0995, 14.49), 3.0, ["2+kk"], None, None,
                      prev_comparables=[stored],
                      parser_version=scrape.PARSER_VERSION + 1)
    comp = fresh()
    assert sources._plan_detail_fetches([comp], "bezrealitky") == [comp], \
        "stale fee survived a PARSER_VERSION bump"


def case_int_ids_survive_json():
    """Sreality ids are ints, JSON object keys are always strings.

    The cache is written to the snapshot and read back next run, so a Sreality
    advert stored under 400465996 comes back as "400465996" and would look
    unknown -- the same "read it again every run" bug, one portal over.
    """
    folded = [dict(id=400465996, price_czk=20000, fees_czk=2500, fees_missing=False,
                   parser_version=scrape.PARSER_VERSION)]
    round_tripped = json.loads(json.dumps(scrape.build_enrichment_cache(folded)))
    records = scrape.fold_cache_records(round_tripped)
    assert records.get(400465996) is not None, "int id lost in the JSON round-trip"
    assert records.get("400465996") is not None, records.keys()


def case_unknown_advert_is_read():
    """Nothing above may turn into "never fetch anything"."""
    sources.configure((50.0995, 14.49), 3.0, ["2+kk"], None, None,
                      parser_version=scrape.PARSER_VERSION)
    comp = dict(id="idnes-new", price_czk=20000)
    assert sources._plan_detail_fetches([comp], "idnes") == [comp]
    needed, reason = scrape.needs_enrichment(comp, None)
    assert needed and reason == "new", (needed, reason)


def main():
    # The cases build on each other: dedup produces the folded copies, which
    # produce the cache, which the planners are then asked about.
    folded = case_fold_returns_duplicates()
    cache = case_cache_is_compact(folded)
    checks = [
        ("dedup returns folded copies", lambda: None),
        ("cache entry is compact", lambda: None),
        ("folded advert is not re-fetched",
         lambda: case_folded_advert_is_not_refetched(cache)),
        ("sources planner skips it too",
         lambda: case_sources_skip_cached_folded_advert(cache)),
        ("parser bump flushes source cache", case_parser_bump_flushes_source_cache),
        ("int ids survive the JSON round-trip", case_int_ids_survive_json),
        ("unknown advert is still read", case_unknown_advert_is_read),
    ]
    failures = []
    for label, fn in checks:
        try:
            fn()
            print(f"PASS  {label}")
        except AssertionError as exc:
            failures.append(f"{label}: {exc}")
            print(f"FAIL  {label}")
    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print("  " + f)
        return 1
    print(f"all {len(checks)} cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
