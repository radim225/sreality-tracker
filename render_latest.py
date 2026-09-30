"""Rebuild HTML from already collected data; no scrape, notification or pool writes.

The published dashboard never includes the personal OWN_* comparison card.
Those figures are private; GitHub Pages serves this HTML.
"""
import json
import shutil
import sys
from pathlib import Path

import market
import pool
import scrape


def main():
    # Volitelně jiný snapshot (lokální kopie s ručně doběhnutou kolekcí,
    # viz fetch_novostavby.py) -- bez argumentu latest_snapshot.json.
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else scrape.LATEST_SNAPSHOT_PATH
    snapshot = json.loads(path.read_text())
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
    # Plocha z popisu a medián podle typu domu (deal_basis) -- stejné pořadí
    # jako main(): obnovit plochu portálu, zkontrolovat, overrides, seřadit.
    comps = snapshot['comparables']
    scrape.restore_portal_areas(comps)
    scrape.backfill_missing_areas(comps)
    scrape.check_flat_areas(comps)
    overrides = scrape.load_overrides()
    scrape.apply_overrides(comps, overrides)
    scrape.apply_overrides(snapshot['tracked'], overrides)
    scrape.flag_transaction_mismatch(comps)
    scrape.rank_deals(comps)
    home = [c for c in comps if scrape.listing_area(c) == scrape.HOME_AREA]
    snapshot['stats'] = scrape.compute_stats(home)
    snapshot['area_stats'] = {k: scrape.compute_stats([c for c in comps if scrape.listing_area(c) == k])
                              for k in scrape.AREAS}
    scrape.render_dashboard(snapshot, changes, snapshot['stats'], history, estimate,
                            scrape.price_histories(all_pool))
    shutil.copyfile(scrape.DASHBOARD_PATH, scrape.ROOT / 'index.html')
    print('Rebuilt dashboard.html and index.html from snapshot', now)


if __name__ == '__main__':
    main()
