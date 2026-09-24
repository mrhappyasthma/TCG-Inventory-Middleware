"""
Tests for ``scripts/replace_card_image.py``.

Two things matter more than the rest. It must not point the *wrong* card
at a picture -- that mistake is silent, and the next push publishes it --
so an ambiguous search is refused rather than guessed. And it must not
accept a picture that would make eBay refuse the listing, because an
undersized image blocks every later revision of the whole listing rather
than just that card.
"""

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

_spec = importlib.util.spec_from_file_location(
    "replace_card_image",
    os.path.join(PROJECT_ROOT, "scripts", "replace_card_image.py"),
)
replace_card_image = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replace_card_image)

from tcg_engine.db import Database  # noqa: E402

BIG = {"ok": True, "width": 800, "height": 1120, "longest": 1120, "reason": ""}
SMALL = {"ok": False, "width": 300, "height": 419, "longest": 419, "reason": ""}
CROPPED = {"ok": True, "width": 866, "height": 569, "longest": 866, "reason": ""}
UNREADABLE = {"ok": None, "width": None, "height": None, "longest": None,
              "reason": "connection refused"}


class FindCardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(db_path=os.path.join(self.tmp, "t.db"))
        for name in ("Ceruledge - 2501", "Ceruledge - 2503", "Wattrel - 2605"):
            self.db.get_or_create_manifest(
                name, "Gem Pack Volume 5", "NM", "Normal", price=1.99
            )

    def find(self, term):
        with redirect_stdout(io.StringIO()) as out:
            card = replace_card_image.find_card(self.db, term)
        return card, out.getvalue()

    def test_a_manifest_id_is_exact(self):
        card, _ = self.find("ID1002")
        self.assertEqual(card["product_name"], "Ceruledge - 2503")

    def test_a_manifest_id_is_case_insensitive(self):
        card, _ = self.find("id1002")
        self.assertIsNotNone(card)

    def test_an_unknown_id_is_reported(self):
        card, output = self.find("ID9999")
        self.assertIsNone(card)
        self.assertIn("no such card", output)

    def test_a_unique_search_resolves(self):
        card, _ = self.find("Wattrel")
        self.assertEqual(card["manifest_id"], "ID1003")

    def test_an_ambiguous_search_is_refused_not_guessed(self):
        """
        Pointing the wrong card at a picture is silent, and the next push
        publishes it.
        """
        card, output = self.find("Ceruledge")
        self.assertIsNone(card)
        self.assertIn("ambiguous", output)

    def test_a_search_matching_nothing_is_reported(self):
        card, output = self.find("Charizard")
        self.assertIsNone(card)
        self.assertIn("no card matches", output)


class InspectTests(unittest.TestCase):
    def inspect(self, verdict, url="https://example.com/c.jpg",
                can_enlarge=True):
        with mock.patch.object(
            replace_card_image.pictures, "check", return_value=verdict
        ):
            return replace_card_image.inspect(url, can_enlarge=can_enlarge)

    def test_a_good_picture_is_accepted(self):
        status, lines = self.inspect(BIG)
        self.assertEqual(status, "ok")
        self.assertFalse(any("TOO SMALL" in l for l in lines))

    def test_an_undersized_picture_is_enlarged_when_we_host_it(self):
        """
        The whole-card replacements found for the two cropped Chinese
        cards were both 300x419 -- better pictures at a worse size.
        Refusing them would mean keeping a half-card.
        """
        status, lines = self.inspect(SMALL)
        self.assertEqual(status, "small")
        self.assertTrue(any("will be enlarged" in l for l in lines))

    def test_an_undersized_picture_is_refused_when_only_linked(self):
        """With --link there is nothing we can do about the size."""
        status, lines = self.inspect(SMALL, can_enlarge=False)
        self.assertEqual(status, "unusable")
        self.assertTrue(any("TOO SMALL" in l for l in lines))

    def test_an_unreadable_picture_is_refused(self):
        """eBay has to fetch it too, so unreadable is not usable."""
        status, lines = self.inspect(UNREADABLE)
        self.assertEqual(status, "unusable")
        self.assertTrue(any("could not be read" in l for l in lines))

    def test_a_cropped_picture_is_reported_but_allowed(self):
        """
        A judgement about the subject rather than a rule eBay enforces, so
        it warns and lets the operator decide.
        """
        status, lines = self.inspect(CROPPED)
        self.assertEqual(status, "ok")
        self.assertTrue(any("NOT CARD-SHAPED" in l for l in lines))

    def test_plain_http_is_flagged(self):
        status, lines = self.inspect(BIG, url="http://example.com/c.jpg")
        self.assertEqual(status, "ok")
        self.assertTrue(any("not https" in l for l in lines))


class ReadPairsTests(unittest.TestCase):
    def write(self, text):
        path = os.path.join(tempfile.mkdtemp(), "p.csv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_it_reads_id_and_url_pairs(self):
        path = self.write(
            "ID1001,https://a/1.jpg\nID1002,https://a/2.jpg\n"
        )
        self.assertEqual(
            replace_card_image.read_pairs(path),
            [("ID1001", "https://a/1.jpg"), ("ID1002", "https://a/2.jpg")],
        )

    def test_a_header_row_is_skipped(self):
        path = self.write("manifest_id,url\nID1001,https://a/1.jpg\n")
        self.assertEqual(
            replace_card_image.read_pairs(path), [("ID1001", "https://a/1.jpg")]
        )

    def test_comments_and_blank_lines_are_ignored(self):
        path = self.write("# a note\n\nID1001,https://a/1.jpg\n\n")
        self.assertEqual(
            replace_card_image.read_pairs(path), [("ID1001", "https://a/1.jpg")]
        )

    def test_a_search_term_is_allowed_in_the_first_column(self):
        """The single-card form takes one, so the file form should too."""
        path = self.write("Wattrel - 2605,https://a/1.jpg\n")
        self.assertEqual(
            replace_card_image.read_pairs(path),
            [("Wattrel - 2605", "https://a/1.jpg")],
        )


if __name__ == "__main__":
    unittest.main()
