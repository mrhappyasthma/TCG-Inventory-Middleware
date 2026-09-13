"""
Tests for ``scripts/accept_ebay_price.py``.

The scenario it exists for is worth restating, because it is the reason a
card keeps reappearing in a draft: the automatic repricer moves eBay's price
and records it against the *variation*, not against the card. So the
catalogue keeps the pre-repricer number, a draft sees the two disagree, and
every rebuild proposes putting the old price back.
"""

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

from tcg_engine.db import Database  # noqa: E402
from tcg_engine.plans import PRICE_EPSILON, desired_price  # noqa: E402


def load_script():
    """Import the script by path: scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location(
        "accept_ebay_price",
        os.path.join(PROJECT_ROOT, "scripts", "accept_ebay_price.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


accept_ebay_price = load_script()


class AcceptEbayPriceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "inventory.db")
        self.db = Database(self.db_path)

        # The repricer scenario: eBay was raised to 2.99, we still hold 1.99.
        self.db.insert_manifest(
            "ID1001", "Risky Ruins", "Mega Evolutions", "Near Mint",
            "Normal", card_number="141/132", market_price=0.90, price=1.99,
        )
        # And one where eBay's price was never captured at all.
        self.db.insert_manifest(
            "ID1002", "Snorlax", "Jungle", "Near Mint", "Normal",
            market_price=0.50, price=1.99,
        )
        for manifest_id in ("ID1001", "ID1002"):
            self.db.set_manifest_quantity(manifest_id, 3)
        self.db.upsert_variation(
            "ID1001", "22751", 3, custom_label="ID1001", last_known_price=2.99
        )
        self.db.upsert_variation("ID1002", "22752", 3, custom_label="ID1002")

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_script(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = accept_ebay_price.main(list(argv) + ["--db", self.db_path])
        return code, out.getvalue()

    def price(self, manifest_id="ID1001"):
        return self.db.get_manifest_by_id(manifest_id)["price"]

    def draft_would_propose(self, manifest_id="ID1001"):
        """Exactly the comparison a plan makes, so this tracks the real rule."""
        card = self.db.get_manifest_by_id(manifest_id)
        known = self.db.get_variation(manifest_id)["last_known_price"]
        proposed = desired_price(card)
        if proposed is None:
            return False
        if known is None:
            return True
        return abs(float(proposed) - float(known)) > PRICE_EPSILON

    def test_a_dry_run_is_the_default_and_changes_nothing(self):
        code, output = self.run_script("Risky Ruins")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.price(), 1.99)
        self.assertIn("Dry run", output)
        # It still says what it would do, so the numbers can be checked.
        self.assertIn("$2.99", output)

    def test_accepting_ebays_price_stops_the_draft_proposing_a_change(self):
        """The whole point: the draft and the listing stop disagreeing."""
        self.assertTrue(
            self.draft_would_propose(), "fixture should start disagreeing"
        )
        code, output = self.run_script("Risky Ruins", "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.price(), 2.99)
        self.assertFalse(self.draft_would_propose())

    def test_it_prints_the_command_that_undoes_it(self):
        """
        The previous value is the only copy of itself, and this output may be
        the only place it survives.
        """
        _, output = self.run_script("Risky Ruins", "--yes")
        self.assertIn("to undo", output)
        self.assertIn("--set 1.99", output)

        code, _ = self.run_script("ID1001", "--set", "1.99", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.price(), 1.99)

    def test_running_it_twice_is_a_no_op(self):
        self.run_script("Risky Ruins", "--yes")
        code, output = self.run_script("Risky Ruins", "--yes")
        self.assertEqual(code, 0, output)
        self.assertIn("nothing to do", output)
        self.assertEqual(self.price(), 2.99)

    def test_an_unknown_ebay_price_is_refused_with_the_remedy(self):
        """
        There is nothing to accept, and guessing would be worse than saying
        so: the card may simply never have been synced.
        """
        code, output = self.run_script("Snorlax", "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.price("ID1002"), 1.99, "left alone")
        self.assertIn("unknown", output)
        self.assertIn("Module B", output)

    def test_set_names_a_price_even_when_ebays_is_unknown(self):
        code, output = self.run_script("Snorlax", "--set", "4.99", "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.price("ID1002"), 4.99)

    def test_an_ambiguous_name_lists_the_candidates_rather_than_guessing(self):
        self.db.insert_manifest(
            "ID1003", "Risky Ruins", "Mega Evolutions", "Lightly Played",
            "Normal", card_number="141/132", price=1.49,
        )
        self.db.set_manifest_quantity("ID1003", 1)

        code, output = self.run_script("Risky Ruins", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("matches 2 cards", output)
        self.assertIn("ID1001", output)
        self.assertIn("ID1003", output)
        self.assertEqual(self.price(), 1.99, "nothing was written")

    def test_a_name_matching_nothing_is_reported(self):
        code, output = self.run_script("Charizard", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Nothing in the catalogue matches", output)

    def test_set_with_several_cards_is_refused(self):
        """One price cannot sensibly be the right one for several cards."""
        code, output = self.run_script(
            "ID1001", "ID1002", "--set", "2.99", "--yes"
        )
        self.assertEqual(code, 2)
        self.assertIn("one card at a time", output)
        self.assertEqual(self.price(), 1.99)

    def test_a_missing_database_is_refused_rather_than_created(self):
        out = io.StringIO()
        missing = os.path.join(self.temp_dir.name, "nope.db")
        with redirect_stdout(out):
            code = accept_ebay_price.main(["ID1001", "--db", missing])
        self.assertEqual(code, 2)
        self.assertIn("No database at", out.getvalue())
        self.assertFalse(os.path.exists(missing))

    def test_the_pinned_price_survives_a_re_upload(self):
        """
        The only other writer of this column backfills a blank, so a pin is
        permanent. If that ever changed, this card would drift back and the
        draft would start arguing again.
        """
        self.run_script("Risky Ruins", "--yes")
        self.db.get_or_create_manifest(
            "Risky Ruins", "Mega Evolutions", "Near Mint", "Normal",
            price=1.49,
        )
        self.assertEqual(self.price(), 2.99)


if __name__ == "__main__":
    unittest.main()
