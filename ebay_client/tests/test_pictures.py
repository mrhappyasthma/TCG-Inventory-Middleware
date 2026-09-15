"""
eBay's picture policy check.

The cost of getting this wrong is asymmetric, which shapes what is asserted
here. Passing a too-small image blocks every later change to a listing,
including price updates that send no pictures at all -- that is how a 169x360
picture surfaced weeks later as a 400 on a bulk price update. But rejecting a
usable image stops the owner listing a card at all. So the three-valued
result is the thing under test as much as the measuring: "not established"
must never collapse into either answer.

No network: dimensions are parsed from bytes supplied directly, which is also
the only way to test a truncated JPEG honestly.
"""

import struct
import unittest

from ebay_client import pictures


def png(width, height):
    """A PNG far enough in to state its size: signature, then IHDR."""
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, height))


def gif(width, height):
    return b"GIF89a" + struct.pack("<HH", width, height)


def jpeg(width, height, *, preamble=b""):
    """
    A JPEG whose size lives in an SOF0 segment.

    ``preamble`` stands in for the EXIF, colour profile and embedded
    thumbnail that in a real file sit between the start marker and the frame
    header -- the reason the parser walks the marker chain instead of
    unpacking a fixed offset.
    """
    sof = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
           + struct.pack(">H", height) + struct.pack(">H", width))
    return b"\xff\xd8" + preamble + sof


def exif_segment(size=64):
    """An APP1 block of the given payload size, to be skipped over."""
    return b"\xff\xe1" + struct.pack(">H", size + 2) + b"E" * size


def webp_vp8x(width, height):
    body = (b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
            + (width - 1).to_bytes(3, "little")
            + (height - 1).to_bytes(3, "little"))
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


class DimensionParsingTests(unittest.TestCase):
    def test_png(self):
        self.assertEqual(pictures.dimensions_from_header(png(800, 600)),
                         (800, 600))

    def test_gif(self):
        self.assertEqual(pictures.dimensions_from_header(gif(640, 480)),
                         (640, 480))

    def test_jpeg(self):
        self.assertEqual(pictures.dimensions_from_header(jpeg(1024, 768)),
                         (1024, 768))

    def test_jpeg_with_metadata_before_the_frame_header(self):
        """The case that makes a fixed-offset read wrong."""
        self.assertEqual(
            pictures.dimensions_from_header(
                jpeg(900, 1200, preamble=exif_segment() + exif_segment(128))
            ),
            (900, 1200),
        )

    def test_webp(self):
        self.assertEqual(pictures.dimensions_from_header(webp_vp8x(700, 500)),
                         (700, 500))

    def test_an_unknown_format_is_not_established(self):
        self.assertIsNone(pictures.dimensions_from_header(b"not an image"))

    def test_empty_bytes_are_not_established(self):
        self.assertIsNone(pictures.dimensions_from_header(b""))

    def test_a_truncated_png_is_not_guessed_at(self):
        self.assertIsNone(
            pictures.dimensions_from_header(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4)
        )


class EbayCdnUrlTests(unittest.TestCase):
    """
    eBay states the size in the path of its own copies, base64-encoded.

    This matters because a picture-policy refusal names eBay's copy rather
    than the URL that was sent, so the error used to be unreadable -- there
    was no way to tell which picture, or how far off it was.
    """

    def test_the_url_from_the_real_refusal(self):
        url = ("https://i.ebayimg.com/00/s/MTY5WDM2MA==/z/"
               "TYwAAeSwzLhqo6P1/$_1.PNG?set_id=8800005007")
        self.assertEqual(pictures.dimensions_from_ebay_url(url), (169, 360))

    def test_a_non_ebay_url_is_left_alone(self):
        self.assertIsNone(
            pictures.dimensions_from_ebay_url("https://example.com/a.png")
        )

    def test_a_url_with_no_size_segment(self):
        self.assertIsNone(
            pictures.dimensions_from_ebay_url("https://i.ebayimg.com/images/g/x/s.jpg")
        )


class PolicyTests(unittest.TestCase):
    def check(self, data, url="https://example.com/a.png"):
        return pictures.check(url, fetch=lambda _u: data)

    def test_a_large_image_passes(self):
        verdict = self.check(png(800, 600))
        self.assertIs(verdict["ok"], True)
        self.assertEqual(verdict["longest"], 800)
        self.assertEqual(verdict["reason"], "")

    def test_exactly_the_minimum_passes(self):
        """eBay's wording is 'at least', so 500 is allowed."""
        self.assertIs(self.check(png(500, 300))["ok"], True)

    def test_one_pixel_short_fails(self):
        verdict = self.check(png(499, 300))
        self.assertIs(verdict["ok"], False)
        self.assertIn("499", verdict["reason"])
        self.assertIn("500", verdict["reason"])

    def test_the_failure_explains_that_it_blocks_other_updates(self):
        """
        The consequence is the part nobody guesses: a small picture stops
        price and quantity changes to the listing too.
        """
        verdict = self.check(png(169, 360))
        self.assertIs(verdict["ok"], False)
        self.assertIn("169x360", verdict["reason"])
        self.assertIn("quantity", verdict["reason"])

    def test_the_longest_side_is_what_counts(self):
        """Tall and narrow still passes if the long side is big enough."""
        self.assertIs(self.check(png(200, 900))["ok"], True)

    def test_an_unreachable_image_is_not_established(self):
        verdict = self.check(None)
        self.assertIsNone(verdict["ok"], "a failed fetch is not a pass")
        self.assertIsNone(verdict["longest"])
        self.assertIn("could not be measured", verdict["reason"])

    def test_an_unreadable_format_is_not_established(self):
        self.assertIsNone(self.check(b"<html>nope</html>")["ok"])

    def test_a_very_wide_image_passes_with_a_note(self):
        verdict = self.check(png(2000, 400))
        self.assertIs(verdict["ok"], True, "eBay accepts it")
        self.assertIn("letterbox", verdict["reason"])

    def test_an_ebay_url_falls_back_to_its_own_stated_size(self):
        """
        When the bytes cannot be had, eBay's URL still says how big its copy
        is -- enough to explain a refusal without fetching anything.
        """
        url = "https://i.ebayimg.com/00/s/MTY5WDM2MA==/z/x/$_1.PNG"
        verdict = pictures.check(url, fetch=lambda _u: None)
        self.assertIs(verdict["ok"], False)
        self.assertEqual(verdict["longest"], 360)

    def test_real_bytes_win_over_the_url(self):
        """
        The URL decode is a guess about a format eBay can change; the bytes
        are authoritative, so a large image at an eBay URL must pass.
        """
        url = "https://i.ebayimg.com/00/s/MTY5WDM2MA==/z/x/$_1.PNG"
        verdict = pictures.check(url, fetch=lambda _u: png(900, 900))
        self.assertIs(verdict["ok"], True)
        self.assertEqual(verdict["longest"], 900)


if __name__ == "__main__":
    unittest.main()
