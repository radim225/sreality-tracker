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
    estimate = market.rent_estimate(pool.window(pool.load_pool(), now=now),
                                   as_of=now, state=pool.load_state(), allow_switch=False)
    scrape.render_dashboard(snapshot, changes, snapshot['stats'], history, estimate)
    shutil.copyfile(scrape.DASHBOARD_PATH, scrape.ROOT / 'index.html')
    print('Rebuilt dashboard.html and index.html from snapshot', now)


if __name__ == '__main__':
    main()
