"""Security boundaries: HTML parser, JSON round-trip, and actual workflow shell."""
import copy
import json
import os
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

import scrape
from listing_url import listing_id


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks = []
        self.active = False

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.active = True
            self.blocks.append('')

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.blocks[-1] += data


class Security(unittest.TestCase):
    def test_json_roundtrip(self):
        value = {'text': '</ScRiPt><script>marker()</script>\u2028\u2029 & česky'}
        encoded = scrape.script_json(value)
        self.assertNotIn('<', encoded)
        self.assertEqual(json.loads(encoded), value)

    def test_full_renderer_keeps_untrusted_text_inside_json(self):
        snapshot = json.loads(Path('latest_snapshot.json').read_text())
        malicious = '</script><script>globalThis.securityMarker=1</script>'
        snapshot['comparables'][0]['title'] = malicious + '__HISTORY_JSON__'
        history = [{'id': 999, 'type': 'new', 'item': {'title': malicious}}]
        if snapshot['tracked']:
            snapshot['tracked'][0]['title'] = malicious + '__DATA_JSON__'
        target = Mock()
        with patch.object(scrape, 'DASHBOARD_PATH', target):
            scrape.render_dashboard(copy.deepcopy(snapshot), {'new': [], 'removed': [], 'price_changes': []}, snapshot['stats'], history)
        parser = Scripts()
        parser.feed(target.write_text.call_args.args[0])
        self.assertEqual(len(parser.blocks), 2)  # Leaflet and the application script
        app = parser.blocks[-1]
        for name in ['DATA', 'TRACKED', 'HISTORY']:
            line = next(line for line in app.splitlines() if line.startswith('const '+name+' = '))
            records = json.loads(line.removeprefix('const '+name+' = ').removesuffix(';'))
            if name == 'DATA':
                self.assertEqual(records[0]['title'], malicious+'__HISTORY_JSON__')
            elif name == 'HISTORY':
                self.assertEqual(records[0]['item']['title'], malicious)

    def test_workflow_treats_input_as_literal_argument(self):
        workflow = Path('.github/workflows/scrape.yml').read_text()
        for command, variable in [('add_tracked.py', 'ADD_URL'), ('remove_tracked.py', 'REMOVE_URL')]:
            line = next(line.strip()[5:] for line in workflow.splitlines() if line.strip().startswith('run: python '+command))
            self.assertNotIn('${{', line)
            # Read argv instead of modifying tracked.json, preserving actual shell quoting.
            receiver = "python3 -c 'import sys; print(repr(sys.argv[1]))'"
            line = line.replace('python '+command, receiver, 1)
            payload = '$(printf SHOULD_NOT_RUN >&2)";echo ALSO_NOT_RUN;#'
            result = subprocess.run(['/bin/bash', '-c', line], env={**os.environ, variable: payload}, capture_output=True, text=True, check=True)
            self.assertEqual(result.stderr, '')
            self.assertEqual(result.stdout.strip(), repr(payload))

    def test_listing_host_and_scheme(self):
        self.assertEqual(listing_id('https://www.sreality.cz/detail/prodej/byt/1%2Bkk/x/123'), 123)
        self.assertEqual(listing_id('123', allow_id=True), 123)
        for url in ['javascript:alert(1)/123', 'https://evil.example/detail/a/123',
                    'https://www.sreality.cz@evil.example/detail/a/123',
                    'https://www.sreality.cz/detail/a/123?x=1',
                    'https://www.sreality.cz/detail/a/$(id)/123']:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    listing_id(url)

    def test_workflow_does_not_inject_own_secrets_into_scrape(self):
        workflow = Path('.github/workflows/scrape.yml').read_text()
        for name in ('OWN_PRICE_CZK', 'OWN_EXTRA_PRICES_CZK', 'OWN_DEPOSITS_CZK', 'OWN_LTV_PCT'):
            self.assertNotIn(f'{name}:', workflow)
            self.assertNotIn(f'secrets.{name}', workflow)

    def test_public_dashboard_omits_own_card_even_when_secrets_are_set(self):
        snapshot = json.loads(Path('latest_snapshot.json').read_text())
        marker_price = 918273645
        marker_garage = 817263544
        marker_deposit = 726354433
        saved = (
            scrape.OWN_PROPERTY['price_czk'],
            dict(scrape.OWN_EXTRA_PRICES),
            scrape.OWN_DEPOSITS_CZK,
        )
        scrape.OWN_PROPERTY['price_czk'] = marker_price
        scrape.OWN_EXTRA_PRICES = {'garaz': marker_garage, 'komora': 110110}
        scrape.OWN_DEPOSITS_CZK = marker_deposit
        try:
            target = Mock()
            with patch.object(scrape, 'DASHBOARD_PATH', target):
                scrape.render_dashboard(
                    copy.deepcopy(snapshot),
                    {'new': [], 'removed': [], 'price_changes': []},
                    snapshot['stats'],
                    [],
                )
            html = target.write_text.call_args.args[0]
            self.assertNotIn('ownCard', html)
            self.assertNotIn(str(marker_price), html)
            self.assertNotIn(str(marker_garage), html)
            self.assertNotIn(str(marker_deposit), html)
            self.assertIn('id="manageCard"', html)
            self.assertIn('id="dealsCard"', html)
        finally:
            scrape.OWN_PROPERTY['price_czk'], scrape.OWN_EXTRA_PRICES, scrape.OWN_DEPOSITS_CZK = saved

    def test_own_finance_tripwire(self):
        with self.assertRaises(RuntimeError):
            scrape.reject_own_finance_in_public_html('<div class="card" id="ownCard"></div>')
        scrape.reject_own_finance_in_public_html('<div class="card" id="estimateCard"></div>')

    def test_committed_pages_html_has_no_own_card(self):
        for name in ('dashboard.html', 'index.html'):
            text = Path(name).read_text(encoding='utf-8')
            self.assertNotIn('id="ownCard"', text, name)
            self.assertNotIn("id='ownCard'", text, name)


if __name__ == '__main__':
    unittest.main()
