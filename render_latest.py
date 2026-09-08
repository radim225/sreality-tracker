"""Rebuild HTML from already collected data; no scrape, notification or pool writes.

Uses the same optional OWN_* environment settings as scrape.py. Before publishing,
provide the intended settings so the existing personal comparison card is retained.
"""
import json
import shutil

import market
import pool
import scrape


def main():
    snapshot = json.loads(scrape.LATEST_SNAPSHOT_PATH.read_text())
    changes = json.loads(scrape.CHANGES_PATH.read_text())
    history = json.loads(scrape.CHANGES_HISTORY_PATH.read_text())
    now = snapshot['generated_at']
    all_pool = pool.load_pool()
    estimate = market.rent_estimate(pool.window(all_pool, now=now),
                                   as_of=now, state=pool.load_state(), allow_switch=False)
    # The same post-passes main() runs between enrich and render. They read text
    # already in the snapshot, so a local re-render must apply them too --
    # otherwise this rebuild silently drops the sale extras and the backfilled
    # areas that production shows.
    for listings in (snapshot['comparables'], snapshot['tracked']):
        scrape.backfill_missing_areas(listings)
        scrape.flag_transaction_mismatch(listings)
        scrape.attach_sale_extras(listings)
    scrape.render_dashboard(snapshot, changes, snapshot['stats'], history, estimate,
                            scrape.price_histories(all_pool))
    shutil.copyfile(scrape.DASHBOARD_PATH, scrape.ROOT / 'index.html')
    print('Rebuilt dashboard.html and index.html from snapshot', now)


if __name__ == '__main__':
    main()
