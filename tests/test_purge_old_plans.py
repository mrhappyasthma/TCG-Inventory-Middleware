"""
Tests for ``scripts/purge_old_plans.py``.

The script is irreversible and will delete a plan the web app deliberately
refuses to, so the two things worth pinning are the cutoff arithmetic and the
fact that a run without ``--yes`` writes nothing at all.
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


def load_script():
    """Import the script by path: scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location(
        "purge_old_plans",
        os.path.join(PROJECT_ROOT, "scripts", "purge_old_plans.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


purge_old_plans = load_script()


class PurgeOldPlansTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "inventory.db")
        self.db = Database(self.db_path)
        self.db.insert_manifest("ID1001", "Pikachu", "Base Set", "NM", "Normal")
        # Twelve plans, alternating pushed and approved below the cutoff, so
        # a test can tell "everything below 10" from "everything pushed".
        for n in range(1, 13):
            plan_id = self.db.create_plan(
                user_id=1, source="upload", note=f"batch {n}"
            )
            self.db.add_plan_items(plan_id, [{
                "manifest_id": "ID1001",
                "group_key": "Base Set|NM",
                "action": "create",
                "proposed_qty": 1,
                "proposed_price": 1.99,
            }])
            self.db.set_plan_group_cover(
                plan_id, "Base Set|NM", "https://cover/x.jpg"
            )
            if n < 12:
                self.db.set_plan_status(
                    plan_id, "pushed" if n % 2 else "approved"
                )

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_script(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = purge_old_plans.main(
                list(argv) + ["--db", self.db_path]
            )
        return code, out.getvalue()

    def plan_ids(self):
        return [p["id"] for p in purge_old_plans.plan_summary(self.db)]

    def test_the_cutoff_is_exclusive_so_the_named_plan_survives(self):
        """
        "Purge everything before plan 10" has to keep plan 10.

        An off-by-one here deletes a plan the user meant to keep, and there is
        no undo beyond the snapshot.
        """
        code, output = self.run_script("--before", "10", "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.plan_ids(), [10, 11, 12])

    def test_a_dry_run_is_the_default_and_changes_nothing(self):
        code, output = self.run_script("--before", "10")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.plan_ids(), list(range(1, 13)))
        self.assertIn("Dry run", output)
        # And it said what it would have done, so the list can be checked
        # before committing to it.
        self.assertIn("Would delete 9 plan(s)", output)

    def test_the_items_and_cover_choices_go_with_the_plan(self):
        """
        A plan's rows are removed by ON DELETE CASCADE, which only fires
        because foreign keys are enabled on every connection. If that ever
        regressed, the visible symptom would be nothing at all -- just rows
        pointing at plans that no longer exist -- so the script checks for
        orphans and fails loudly.
        """
        code, output = self.run_script("--before", "10", "--yes")
        self.assertEqual(code, 0, output)
        with self.db.get_connection() as conn:
            items = conn.execute(
                "SELECT COUNT(*) AS c FROM listing_plan_item"
            ).fetchone()["c"]
            groups = conn.execute(
                "SELECT COUNT(*) AS c FROM listing_plan_group"
            ).fetchone()["c"]
        self.assertEqual(items, 3)
        self.assertEqual(groups, 3)
        self.assertNotIn("orphaned", output)

    def test_a_real_run_snapshots_the_database_first(self):
        """There is no undo, so the snapshot is not optional."""
        code, output = self.run_script("--before", "10", "--yes")
        self.assertEqual(code, 0, output)
        snapshots = [
            name for name in os.listdir(self.temp_dir.name)
            if "before-plan-purge" in name
        ]
        self.assertEqual(len(snapshots), 1, snapshots)
        self.assertIn(snapshots[0], output)
        # And it is a real database with the plans still in it, not an empty
        # file that merely exists.
        restored = Database(os.path.join(self.temp_dir.name, snapshots[0]))
        self.assertEqual(
            [p["id"] for p in purge_old_plans.plan_summary(restored)],
            list(range(1, 13)),
        )

    def test_pushed_plans_are_named_because_their_loss_is_the_real_one(self):
        """
        The drafts page refuses to delete a pushed plan: it is the only record
        of who authorised a live change. This script will delete one, so the
        report has to say which.
        """
        _, output = self.run_script("--before", "10")
        self.assertIn("reached eBay", output)
        for plan_id in (1, 3, 5, 7, 9):
            self.assertIn(str(plan_id), output)

    def test_keep_pushed_clears_only_the_abandoned_approvals(self):
        code, output = self.run_script("--before", "10", "--keep-pushed", "--yes")
        self.assertEqual(code, 0, output)
        # The even ids below the cutoff were approved and never pushed.
        self.assertEqual(self.plan_ids(), [1, 3, 5, 7, 9, 10, 11, 12])

    def test_a_cutoff_below_every_plan_does_nothing_and_says_so(self):
        code, output = self.run_script("--before", "1", "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.plan_ids(), list(range(1, 13)))
        self.assertIn("Nothing is below plan 1", output)

    def test_a_missing_database_is_refused_rather_than_created(self):
        """
        ``Database()`` creates what is not there, so a typo in --db would
        otherwise produce an empty database, report "no plans" and exit
        successfully -- looking exactly like a purge that had already run.
        """
        out = io.StringIO()
        with redirect_stdout(out):
            code = purge_old_plans.main([
                "--before", "10",
                "--db", os.path.join(self.temp_dir.name, "nope.db"),
            ])
        self.assertEqual(code, 2)
        self.assertIn("No database at", out.getvalue())
        self.assertFalse(
            os.path.exists(os.path.join(self.temp_dir.name, "nope.db"))
        )

    def test_selection_ignores_which_user_owns_a_plan(self):
        """
        A purge has to see every plan it is about to delete, including one
        belonging to another account -- unlike the drafts page, which is
        scoped to the signed-in user and would hide it.
        """
        other = self.db.create_plan(user_id=2, source="upload", note="theirs")
        self.db.set_plan_status(other, "approved")
        plans = purge_old_plans.plan_summary(self.db)
        self.assertIn(other, [p["id"] for p in plans])
        doomed, _ = purge_old_plans.select_plans(plans, before=99)
        self.assertIn(other, [p["id"] for p in doomed])


if __name__ == "__main__":
    unittest.main()
