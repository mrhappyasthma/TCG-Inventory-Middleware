"""
Tests for the card-shape check in ``scripts/check_images.py``.

The size check answers eBay's question -- is the longest side at least 500
pixels. It cannot answer ours: is this a picture of the whole card. Two
images in a 214-card Chinese import were the top half only, 866x569 and
856x581, and both passed the size check comfortably on their width while
missing the attacks, the weakness, the retreat cost and the set number.
"""

import importlib.util
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

_spec = importlib.util.spec_from_file_location(
    "check_images",
    os.path.join(PROJECT_ROOT, "scripts", "check_images.py"),
)
check_images = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_images)


class CardShapeTests(unittest.TestCase):
    def test_a_whole_card_is_card_shaped(self):
        for width, height in [
            (862, 1206), (300, 419), (800, 1094), (965, 1320), (733, 1024),
        ]:
            self.assertTrue(
                check_images.card_shaped(width, height), f"{width}x{height}"
            )

    def test_the_two_cropped_imports_are_caught(self):
        """The real sizes, from the real file."""
        self.assertFalse(check_images.card_shaped(866, 569))
        self.assertFalse(check_images.card_shaped(856, 581))

    def test_a_square_image_is_not_card_shaped(self):
        self.assertFalse(check_images.card_shaped(600, 600))

    def test_a_slightly_trimmed_or_bordered_scan_still_passes(self):
        """
        The tolerance is deliberately generous. A false positive here sends
        somebody to inspect a picture that is fine, and a check that cries
        wolf stops being read.
        """
        self.assertTrue(check_images.card_shaped(700, 1000))
        self.assertTrue(check_images.card_shaped(760, 1000))

    def test_an_unmeasurable_image_is_not_called_misshapen(self):
        """
        None means the question was not answered. It must not be turned
        into an accusation -- the size check already reports an unreadable
        image as a warning of its own.
        """
        self.assertTrue(check_images.card_shaped(None, None))
        self.assertTrue(check_images.card_shaped(0, 0))


if __name__ == "__main__":
    unittest.main()
