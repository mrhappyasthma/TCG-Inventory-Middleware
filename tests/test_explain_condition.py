"""
Tests for ``scripts/explain_condition.py``.

A diagnostic that gets its own subject wrong is worse than none, because it is
believed. This one exists to answer "why does the drafts page say LP when the
export said NM", and the two answers it must never confuse are *the export was
LP for that card* and *the card was already catalogued at another grade*. So
the twin detection is what is pinned here, along with the guarantee that the
script writes nothing.
"""

import importlib.util
import io
import os
import sys
import unittest
import tempfile
from contextlib import redirect_stdout

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

from tcg_engine.db import Database, SHARED_SCOPE  # noqa: E402
from tcg_engine.plans import build_plan  # noqa: E402


def load_script():
    """Import the script by path: scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location(
        "explain_condition",
        os.path.join(PROJECT_ROOT, "scripts", "explain_condition.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


explain_condition = load_script()


class ExplainConditionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "inventory.db")
        self.db = Database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def add(self, manifest_id, name, condition, number, qty=4):
        self.db.insert_manifest(
            manifest_id, name, "ME: Ascended Heroes", condition, "Normal",
            card_number=number,
        )
        self.db.set_manifest_quantity(manifest_id, qty)

    def run_script(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = explain_condition.main(
                list(argv) + ["--db", self.db_path]
            )
        return code, buffer.getvalue()

    def test_the_stored_condition_is_reported_per_set(self):
        self.add("ID1001", "Acerola's Mischief", "NM", "180/217")
        self.add("ID1002", "Iono's Wattrel", "LP", "071/217")

        code, out = self.run_script("Ascended Heroes")

        self.assertEqual(code, 0)
        self.assertIn("ME: Ascended Heroes", out)
        # Both grades, and the group key each becomes -- which is what the
        # drafts page turns into a block heading.
        self.assertIn("'ME: Ascended Heroes|NM'", out)
        self.assertIn("'ME: Ascended Heroes|LP'", out)

    def test_the_card_at_the_minority_grade_is_named(self):
        """
        Counting them would be useless: the remedy is per card, in SortSwift.
        """
        self.add("ID1001", "Acerola's Mischief", "NM", "180/217")
        self.add("ID1002", "Bayleef", "NM", "009/217")
        self.add("ID1003", "Iono's Wattrel", "LP", "071/217")

        _, out = self.run_script("Ascended Heroes")

        self.assertIn("1 card(s) not at 'NM'", out)
        self.assertIn("Iono's Wattrel", out)
        self.assertIn("ID1003", out)

    def test_a_card_catalogued_at_two_grades_is_reported_as_a_twin(self):
        """
        The case the export cannot explain, and the reason this script exists.

        Condition is part of a card's identity, so importing the same card at
        a second grade adds a row rather than correcting one. On the drafts
        page that is simply two blocks for one set, with no hint that they
        describe the same physical card.
        """
        self.add("ID1001", "Bayleef", "LP", "009/217", qty=3)
        self.add("ID1002", "Bayleef", "NM", "009/217", qty=4)

        _, out = self.run_script("Ascended Heroes")

        self.assertIn("1 card(s) catalogued at more than one condition", out)
        self.assertIn("ID1001", out)
        self.assertIn("ID1002", out)

    def test_one_grade_reports_no_twins_and_no_minority(self):
        # The healthy case must read as healthy, or the script cries wolf.
        self.add("ID1001", "Acerola's Mischief", "NM", "180/217")
        self.add("ID1002", "Bayleef", "NM", "009/217")

        _, out = self.run_script("Ascended Heroes")

        self.assertIn(
            "No card in this set is catalogued at more than one condition", out
        )
        self.assertNotIn("not at", out)

    def test_an_unknown_set_is_an_error_rather_than_an_empty_report(self):
        """
        An empty report reads as "nothing wrong". A set name that matches
        nothing has established nothing at all, so it exits non-zero.
        """
        self.add("ID1001", "Acerola's Mischief", "NM", "180/217")

        code, out = self.run_script("Base Set")

        self.assertEqual(code, 1)
        self.assertIn("No catalogued card's set name contains", out)

    def test_the_open_draft_is_reported_in_the_pages_own_order(self):
        self.add("ID1001", "Acerola's Mischief", "NM", "180/217")
        self.add("ID1002", "Iono's Wattrel", "LP", "071/217")
        build_plan(self.db, user_id=SHARED_SCOPE)

        _, out = self.run_script("Ascended Heroes")

        self.assertIn("ME: Ascended Heroes|LP", out)
        self.assertIn("ME: Ascended Heroes|NM", out)
        # LP above NM, because the page orders blocks by group key and that is
        # how a one-card LP block comes to sit above a large NM one.
        self.assertLess(
            out.index("card(s)  ME: Ascended Heroes|LP"),
            out.index("card(s)  ME: Ascended Heroes|NM"),
        )

    def test_no_draft_says_so_rather_than_printing_nothing(self):
        self.add("ID1001", "Acerola's Mischief", "NM", "180/217")

        _, out = self.run_script("Ascended Heroes")

        self.assertIn("no open draft", out)

    def test_it_writes_nothing(self):
        self.add("ID1001", "Acerola's Mischief", "NM", "180/217")
        self.add("ID1002", "Bayleef", "LP", "009/217")
        before = os.path.getmtime(self.db_path)
        rows_before = self.snapshot()

        self.run_script("Ascended Heroes")

        self.assertEqual(self.snapshot(), rows_before)
        self.assertEqual(os.path.getmtime(self.db_path), before)

    def snapshot(self):
        with self.db.get_connection() as conn:
            return [
                tuple(row) for row in conn.execute(
                    "SELECT manifest_id, condition, quantity FROM manifest "
                    "ORDER BY manifest_id"
                )
            ]


if __name__ == "__main__":
    unittest.main()
