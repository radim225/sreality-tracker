"""Bezrealitky and iDNES URLs can be tracked. They are not fetched again.

The id matches the comparison row (`bez-<id>`, `idnes-<hash>`). Adding is
idempotent. The card is the same Sreality seed card: price, zlevnění/zdražení,
price history. A disappearance keeps the card.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sources
import scrape
from add_tracked import add_tracked_url, main as add_main
from listing_url import listing_id, order_tracked
from remove_tracked import main as remove_main, remove_tracked_id

BEZ_URL = (
    "https://www.bezrealitky.cz/nemovitosti-byty-domy/"
    "1079934-nabidka-prodej-bytu-podebradska-praha"
)
IDNES_URL = (
    "https://reality.idnes.cz/detail/pronajem/byt/praha-8-v-mezihori/"
    "6addf060616bccdf450d9450/"
)
SREALITY_URL = (
    "https://www.sreality.cz/detail/prodej/byt/1+kk/praha-vysocany-pod-harfou/2864615500"
)
WEEKLY = Path("weekly_segments.json")


def _comp(**extra):
    row = {
        "id": "bez-1079934",
        "source": "bezrealitky",
        "title": "Prodej bytu 1+kk 19 m²",
        "disposition": "1+kk",
        "transaction_type": "prodej",
        "price_czk": 5_190_000,
        "total_czk": 5_190_000,
        "electricity_estimated": False,
        "floor_area_sqm": 19,
        "url": BEZ_URL,
        "active": True,
        "locality": "Poděbradská, Praha",
        "thumb": "https://example.com/foto.jpg",
        "images": ["https://example.com/foto.jpg"],
    }
    row.update(extra)
    return row


class ListingId(unittest.TestCase):
    def test_bezrealitky_matches_comparison_id(self):
        self.assertEqual(listing_id(BEZ_URL), "bez-1079934")
        self.assertEqual(
            listing_id(BEZ_URL.replace("https://www.", "https://")),
            "bez-1079934",
        )

    def test_idnes_matches_comparison_regex(self):
        found = sources.IDNES_ID_RE.search(IDNES_URL)
        self.assertIsNotNone(found)
        self.assertEqual(listing_id(IDNES_URL), f"idnes-{found.group(1)}")

    def test_sreality_stays_an_int(self):
        self.assertEqual(listing_id(SREALITY_URL), 2864615500)
        self.assertIsInstance(listing_id(SREALITY_URL), int)

    def test_rejects_other_domain_http_and_shell_metacharacters(self):
        rejected = [
            "https://evil.example/nemovitosti-byty-domy/1079934-nabidka",
            "http://www.bezrealitky.cz/nemovitosti-byty-domy/1079934-nabidka",
            "http://www.sreality.cz/detail/prodej/byt/1+kk/x/123",
            "https://www.bezrealitky.cz/nemovitosti-byty-domy/1079934-nabidka;rm",
            "https://www.sreality.cz/detail/a/$(id)/123",
            "https://reality.idnes.cz/detail/prodej/byt/praha/$(x)/",
            "https://news.idnes.cz/detail/prodej/byt/praha/6addf060616bccdf450d9450/",
            "https://www.bezrealitky.cz/nemovitosti-byty-domy/abc-nabidka",
        ]
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    listing_id(url)

    def test_bare_portal_id_only_when_removing(self):
        with self.assertRaises(ValueError):
            listing_id("bez-1079934")
        self.assertEqual(listing_id("bez-1079934", allow_id=True), "bez-1079934")
        self.assertEqual(
            listing_id("idnes-6addf060616bccdf450d9450", allow_id=True),
            "idnes-6addf060616bccdf450d9450",
        )


class AddRemove(unittest.TestCase):
    def test_add_bez_is_idempotent_and_sreality_still_works(self):
        existing = [{"id": 2864615500, "url": SREALITY_URL}]
        once, parsed, added = add_tracked_url(existing, BEZ_URL)
        self.assertTrue(added)
        self.assertEqual(parsed, "bez-1079934")
        self.assertEqual(once[0]["id"], "bez-1079934")
        self.assertEqual(once[1]["id"], 2864615500)
        twice, parsed2, added2 = add_tracked_url(once, BEZ_URL)
        self.assertFalse(added2)
        self.assertEqual(parsed2, "bez-1079934")
        self.assertIs(twice, once)

        again, sid, sadd = add_tracked_url(once, SREALITY_URL)
        self.assertFalse(sadd)
        self.assertEqual(sid, 2864615500)

        fresh, sid2, ok = add_tracked_url([], SREALITY_URL)
        self.assertTrue(ok)
        self.assertEqual(fresh, [{"id": 2864615500, "url": SREALITY_URL}])
        self.assertIsInstance(sid2, int)

    def test_add_and_remove_roundtrip_does_not_touch_live_file(self):
        live_before = Path("tracked.json").read_bytes()
        weekly_before = WEEKLY.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tracked.json"
            path.write_text("[]\n", encoding="utf-8")
            with patch("add_tracked.TRACKED_PATH", path), \
                    patch("remove_tracked.TRACKED_PATH", path):
                with patch("sys.argv", ["add_tracked.py", BEZ_URL]):
                    add_main()
                first = path.read_text(encoding="utf-8")
                with patch("sys.argv", ["add_tracked.py", BEZ_URL]):
                    add_main()
                self.assertEqual(path.read_text(encoding="utf-8"), first)
                stored = json.loads(first)
                self.assertEqual(stored, [{"id": "bez-1079934", "url": BEZ_URL}])

                with patch("sys.argv", ["add_tracked.py", SREALITY_URL]):
                    add_main()
                both = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual([row["id"] for row in both], ["bez-1079934", 2864615500])

                with patch("sys.argv", ["remove_tracked.py", "bez-1079934"]):
                    remove_main()
                left = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(left, [{"id": 2864615500, "url": SREALITY_URL}])

                path.write_text(json.dumps([{"id": "bez-1079934", "url": BEZ_URL}]) + "\n",
                                encoding="utf-8")
                with patch("sys.argv", ["remove_tracked.py", BEZ_URL]):
                    remove_main()
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])
                with patch("sys.argv", ["remove_tracked.py", BEZ_URL]):
                    remove_main()
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])

                before = path.read_text(encoding="utf-8")
                for bad in (
                    "https://evil.example/nemovitosti-byty-domy/1079934-x",
                    "http://www.bezrealitky.cz/nemovitosti-byty-domy/1079934-x",
                    BEZ_URL + ";rm",
                    "https://www.sreality.cz/detail/$(id)/1",
                ):
                    with self.subTest(bad=bad):
                        with patch("sys.argv", ["add_tracked.py", bad]):
                            with self.assertRaises(SystemExit):
                                add_main()
                        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(Path("tracked.json").read_bytes(), live_before)
        self.assertEqual(WEEKLY.read_bytes(), weekly_before)

    def test_remove_helper_accepts_new_id_shape(self):
        rows = [
            {"id": "bez-1079934", "url": BEZ_URL},
            {"id": "idnes-6addf060616bccdf450d9450", "url": IDNES_URL},
            {"id": 5, "url": SREALITY_URL},
        ]
        kept, parsed, removed = remove_tracked_id(rows, "idnes-6addf060616bccdf450d9450")
        self.assertTrue(removed)
        self.assertEqual(parsed, "idnes-6addf060616bccdf450d9450")
        self.assertEqual([r["id"] for r in kept], ["bez-1079934", 5])


class PairAndCard(unittest.TestCase):
    def test_bez_card_is_first_and_shows_price_drop_and_history(self):
        comp = _comp()
        history = [
            {"at": "2026-10-01", "price_czk": 5_490_000},
            {"at": "2026-10-19", "price_czk": 5_190_000},
        ]
        with patch("scrape.fetch_next_data", side_effect=AssertionError("fetched")), \
                patch("scrape.fetch_tracked", side_effect=AssertionError("tracked fetch")), \
                patch("sources.extra_listing_gone", side_effect=AssertionError("extra")), \
                patch("requests.sessions.Session.request", side_effect=AssertionError("http")):
            rows = scrape.pair_portal_tracked(
                [{"id": "bez-1079934", "url": BEZ_URL}],
                [comp], [], [],
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rent_czk"], 5_190_000)
        self.assertEqual(rows[0]["source"], "bezrealitky")
        self.assertTrue(rows[0]["active"])
        rows[0]["price_history"] = history

        sreality = {
            "id": 2864615500,
            "url": SREALITY_URL,
            "title": "Sreality",
            "transaction_type": "prodej",
            "rent_czk": 6_000_000,
            "active": True,
            "price_history": [
                {"at": "2026-06-01", "price_czk": 6_200_000},
                {"at": "2026-09-01", "price_czk": 6_000_000},
            ],
        }
        ordered = order_tracked([sreality, rows[0]])
        self.assertEqual([r["id"] for r in ordered], ["bez-1079934", 2864615500])
        cards = "\n".join(scrape.render_tracked_card(r) for r in ordered)
        self.assertLess(cards.find("bez-1079934"), cards.find("2864615500"))
        bez_card = scrape.render_tracked_card(rows[0])
        self.assertIn("5 190 000 Kč", bez_card)
        self.assertIn("Zlevněno", bez_card)
        self.assertIn("5 490 000 Kč", bez_card)
        self.assertIn('data-open-id="&quot;bez-1079934&quot;"', bez_card)
        self.assertNotIn("onclick", bez_card)
        self.assertNotIn("<script", bez_card)

        up = dict(rows[0])
        up["rent_czk"] = 5_490_000
        up["price_history"] = [
            {"at": "2026-10-01", "price_czk": 5_190_000},
            {"at": "2026-10-19", "price_czk": 5_490_000},
        ]
        self.assertIn("Zdraženo", scrape.render_tracked_card(up))

        item = scrape.build_tracked_item(
            rows[0],
            {"tracked_price_changes": [], "price_changes": [], "new_listings": []},
        )
        self.assertEqual(item["price_czk"], 5_190_000)
        self.assertEqual(item["price_history"], history)
        self.assertEqual(item["source"], "bezrealitky")
        encoded = scrape.script_json(item)
        self.assertNotIn("<", encoded)

    def test_foreign_text_is_escaped(self):
        row = scrape.tracked_from_comparable(_comp(
            title='</div><script>alert(1)</script>',
            locality='Poděbradská "1"',
            thumb='https://example.com/a.jpg" onload="alert(1)',
        ))
        row["price_history"] = [
            {"at": "2026-10-01", "price_czk": 5_490_000},
            {"at": "2026-10-19", "price_czk": 5_190_000},
        ]
        card = scrape.render_tracked_card(row)
        self.assertNotIn("<script", card)
        self.assertNotIn("</div><script>", card)
        self.assertIn("&lt;/div&gt;&lt;script&gt;alert(1)&lt;/script&gt;", card)
        self.assertIn("&quot;1&quot;", card)
        self.assertNotIn('onload="alert(1)', card)
        self.assertIn("onload=&quot;alert(1)", card)

    def test_disappearance_keeps_the_card(self):
        prev = {
            "id": "bez-1079934",
            "url": BEZ_URL,
            "active": True,
            "rent_czk": 5_190_000,
            "total_czk": 5_190_000,
            "electricity_estimated": False,
            "title": "Prodej bytu 1+kk 19 m²",
            "fetched_at": "2026-10-25T13:19:49Z",
            "transaction_type": "prodej",
        }
        gone = _comp(active=False)
        with patch("requests.sessions.Session.request", side_effect=AssertionError("http")):
            rows = scrape.pair_portal_tracked(
                [{"id": "bez-1079934", "url": BEZ_URL}],
                [], [prev], [gone],
            )
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["active"])
        self.assertEqual(rows[0]["rent_czk"], 5_190_000)
        self.assertEqual(rows[0]["title"], "Prodej bytu 1+kk 19 m²")
        self.assertEqual(rows[0]["last_active_at"], "2026-10-25T13:19:49Z")
        card = scrape.render_tracked_card(rows[0])
        self.assertIn("inactive / removed", card)
        self.assertIn("5 190 000 Kč", card)
        self.assertIn("2026-10-25T13:19:49Z", card)

    def test_absence_without_confirmation_is_not_deletion(self):
        prev = {
            "id": "bez-1079934", "url": BEZ_URL, "active": True,
            "rent_czk": 5_190_000, "title": "pořád tady",
        }
        rows = scrape.pair_portal_tracked(
            [{"id": "bez-1079934", "url": BEZ_URL}], [], [prev], [],
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["active"])
        self.assertEqual(rows[0]["title"], "pořád tady")
        self.assertEqual(rows[0]["rent_czk"], 5_190_000)

    def test_portal_tracked_is_not_fetched(self):
        with patch("scrape.fetch_next_data", side_effect=AssertionError("network")):
            missed = scrape.fetch_tracked(BEZ_URL, "bez-1079934")
        self.assertTrue(missed.get("unavailable"))
        with patch("scrape.fetch_tracked", side_effect=AssertionError("network")):
            fetched = scrape.fetch_configured_tracked(
                [{"id": "bez-1079934", "url": BEZ_URL},
                 {"id": "idnes-6addf060616bccdf450d9450", "url": IDNES_URL}],
                {},
            )
        self.assertEqual(fetched, [])

    def test_price_change_uses_the_paired_total(self):
        prev = _comp(price_czk=5_490_000, total_czk=5_490_000)
        curr_comp = _comp()
        prev_row = scrape.tracked_from_comparable(prev)
        curr_row = scrape.tracked_from_comparable(curr_comp)
        changes = scrape.collect_tracked_price_changes([prev_row], [curr_row])
        self.assertEqual(changes, [{
            "id": "bez-1079934",
            "old_total_czk": 5_490_000,
            "new_total_czk": 5_190_000,
        }])

    def test_no_cookies_and_weekly_segments_stay_aggregates(self):
        for name in ("listing_url.py", "add_tracked.py", "remove_tracked.py"):
            text = Path(name).read_text(encoding="utf-8").lower()
            self.assertNotIn("cookie", text, name)
            self.assertNotIn("authorization", text, name)
            self.assertNotIn("requests", text, name)
        self.assertNotIn("Cookie", sources.SESSION.headers)
        self.assertNotIn("Authorization", sources.SESSION.headers)
        weekly = json.loads(WEEKLY.read_text(encoding="utf-8"))
        import segments
        segments.assert_public(weekly)
        blob = WEEKLY.read_text(encoding="utf-8")
        self.assertNotIn("bez-1079934", blob)
        self.assertNotIn(BEZ_URL, blob)
        self.assertIn("id", segments.FORBIDDEN_KEYS)
        self.assertIn("url", segments.FORBIDDEN_KEYS)
        self.assertIn("seller_name", segments.FORBIDDEN_KEYS)


if __name__ == "__main__":
    unittest.main()
