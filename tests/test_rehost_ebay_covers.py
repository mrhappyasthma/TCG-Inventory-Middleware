"""
Tests for ``scripts/rehost_ebay_covers.py``.

The one that matters is the placeholder. eBay answers an unknown image id
with **HTTP 200 and an 80x80 "no image" icon**, not a 404 -- so a
re-hoster that trusts the response replaces a real cover photo with a
grey graphic and reports success. The first version of this script did
exactly that to eight covers.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (
    PROJECT_ROOT,
    os.path.join(PROJECT_ROOT, "tcg_engine"),
    os.path.join(PROJECT_ROOT, "scripts"),
):
    if path not in sys.path:
        sys.path.insert(0, path)

_spec = importlib.util.spec_from_file_location(
    "rehost_ebay_covers",
    os.path.join(PROJECT_ROOT, "scripts", "rehost_ebay_covers.py"),
)
rehost_ebay_covers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rehost_ebay_covers)


class FakeResponse:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RehostTests(unittest.TestCase):
    URL = "https://i.ebayimg.com/images/g/abc/s-l1600.webp"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def run_rehost(self, raw):
        with mock.patch.object(
            rehost_ebay_covers.urllib.request, "urlopen",
            return_value=FakeResponse(raw),
        ):
            return rehost_ebay_covers.rehost(
                self.URL, self.tmp, "https://cards.example.com"
            )

    @staticmethod
    def png(width, height):
        """A PNG header the measurer can read, with no pixel data."""
        import struct
        ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
        return (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"
        )

    def test_a_real_cover_is_saved_and_served_from_us(self):
        public, note = self.run_rehost(self.png(1000, 1000))
        self.assertTrue(public.startswith("https://cards.example.com/card-images/"))
        self.assertIn("1000x1000", note)
        name = public.rsplit("/", 1)[-1]
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, name)))

    def test_the_80x80_placeholder_is_refused_not_enlarged(self):
        """
        eBay answers 200 with this for an id it does not know. Enlarging
        it replaces a real cover with a grey icon and calls it success.
        """
        with self.assertRaises(rehost_ebay_covers.NotTheCover) as caught:
            self.run_rehost(self.png(80, 80))
        message = str(caught.exception)
        self.assertIn("80x80", message)
        self.assertIn("placeholder", message)

    def test_nothing_is_written_when_it_is_refused(self):
        with self.assertRaises(rehost_ebay_covers.NotTheCover):
            self.run_rehost(self.png(80, 80))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_something_that_is_not_an_image_is_refused(self):
        with self.assertRaises(rehost_ebay_covers.NotTheCover) as caught:
            self.run_rehost(b"<html>gone</html>")
        self.assertIn("not a readable image", str(caught.exception))

    def test_the_filename_is_stable_for_one_url(self):
        """
        Named from a digest of the source, so re-running is idempotent
        and two listings sharing a cover share one file.
        """
        first = rehost_ebay_covers.local_name(self.URL)
        self.assertEqual(first, rehost_ebay_covers.local_name(self.URL))
        self.assertNotEqual(
            first, rehost_ebay_covers.local_name(self.URL + "x")
        )

    def test_the_name_cannot_collide_with_a_cards_own_picture(self):
        """A card's replacement is named for its manifest id."""
        self.assertTrue(
            rehost_ebay_covers.local_name(self.URL).startswith("cover-")
        )


class PlanScopeTests(unittest.TestCase):
    """
    Which plans are examined, which is where this script first failed.

    A push that put some cards live and failed the rest leaves the plan
    `partial` -- and that is precisely the state a plan blocked by its
    cover is in. Looking only at drafts skipped the only plan that
    mattered and then reported that nothing needed re-hosting.
    """

    def test_a_partial_plan_is_in_scope(self):
        self.assertNotIn("partial", rehost_ebay_covers.FINISHED_PLAN_STATUSES)

    def test_every_status_a_push_can_still_reach_is_in_scope(self):
        for status in ("draft", "approved", "pushing", "partial", "failed"):
            self.assertNotIn(
                status, rehost_ebay_covers.FINISHED_PLAN_STATUSES, status
            )

    def test_finished_plans_are_left_alone(self):
        """
        A pushed or discarded plan is a record of a decision, not
        pending work.
        """
        self.assertEqual(
            set(rehost_ebay_covers.FINISHED_PLAN_STATUSES),
            {"pushed", "discarded"},
        )


if __name__ == "__main__":
    unittest.main()
