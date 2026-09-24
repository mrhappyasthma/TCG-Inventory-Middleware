"""
eBay's picture policy, and how to check an image against it before sending.

eBay requires at least 500 pixels on an image's longest side, and it
**re-validates every picture on a listing whenever anything about that
listing changes**. That second half is what makes an undersized image
expensive: it does not merely look bad, it blocks every later change to the
listing, including changes that have nothing to do with pictures.

The bill for learning that:

* A 308x164 search-result thumbnail in a cover photo field blocked two
  listings completely.
* A price update -- which sends no pictures at all -- came back
  ``400 errorId 25002 ... does not meet eBay's Picture Policy requirements``
  for one card, naming an image nobody had touched in weeks. Nothing about
  that request concerned pictures; eBay was re-checking what was already
  there.

So the cheapest moment to catch this is when a picture is *chosen*, which is
what this module is for. A URL measured before it is stored costs one HTTP
fetch of a few kilobytes; the same URL discovered after a push costs a
listing that cannot be repriced until someone works out which of its hundred
pictures eBay is objecting to.

Dimensions are read from the image's header rather than with an imaging
library: Pillow is not a dependency of this project and would be a large one
to add for a size check. Only the header is read, so a 6 MB photograph costs
the same as a thumbnail.

This module makes no eBay API calls. It lives here because the 500-pixel rule
is eBay's, and eBay's rules belong in the eBay library rather than scattered
through whatever happens to need them.
"""

import base64
import struct
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

# eBay's published minimum for the longest side of a listing image.
MIN_LONGEST_SIDE = 500

# Enough to reach a JPEG's Start Of Frame past EXIF and an embedded thumbnail.
HEADER_BYTES = 131072

# Past this, an image letterboxes badly in eBay's square gallery thumbnail.
# Not a rejection -- eBay accepts it -- so it is reported and nothing more.
WIDE_ASPECT_RATIO = 2.0

# The hosts eBay serves its own copies of pictures from -- eBay Picture
# Services, "EPS".
#
# They matter because eBay refuses a listing whose pictures are a mixture
# of EPS and self-hosted ones: "A mixture of Self Hosted and EPS pictures
# are not allowed." So a URL on one of these cannot be combined with a URL
# anywhere else on the same listing, and every card picture this
# application sends is self-hosted by definition.
#
# Confirmed the expensive way. A cover photo copied out of an existing
# listing is an EPS URL, and setting one as the account-wide default made
# every new variation listing fail at publish -- six listings, 211 cards --
# while the three singles in the same push went up fine, because a single
# never writes an inventory item group and so never sends a group picture.
EPS_HOSTS = ("ebayimg.com", "ebaystatic.com")


def is_ebay_hosted(url) -> bool:
    """
    Whether this picture is one eBay already hosts.

    Matched on the host, not on a substring of the whole URL: a query
    parameter or a path that merely mentions ebayimg.com is not an eBay
    picture, and treating it as one would refuse a perfectly good URL.
    """
    from urllib.parse import urlparse  # noqa: PLC0415

    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in EPS_HOSTS)

USER_AGENT = "TCG-Inventory-Middleware/1.0 (image size check)"


def dimensions_from_header(head: bytes) -> Optional[Tuple[int, int]]:
    """
    (width, height) from an image's leading bytes, or None.

    None means "not established" -- an unrecognised format, or too few bytes.
    It must never be reported as "fine": the whole point is to find images
    eBay will reject.
    """
    if not head:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        return struct.unpack(">II", head[16:24])
    if head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
        return struct.unpack("<HH", head[6:10])
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        if head[12:16] == b"VP8X" and len(head) >= 30:
            return (int.from_bytes(head[24:27], "little") + 1,
                    int.from_bytes(head[27:30], "little") + 1)
        if head[12:16] == b"VP8 " and len(head) >= 30:
            return (int.from_bytes(head[26:28], "little") & 0x3FFF,
                    int.from_bytes(head[28:30], "little") & 0x3FFF)
        return None
    if head[:2] == b"\xff\xd8":
        return _jpeg_dimensions(head)
    return None


def _jpeg_dimensions(head: bytes) -> Optional[Tuple[int, int]]:
    """
    Walk a JPEG's marker chain to the frame header that states its size.

    Not at a fixed offset: EXIF, an ICC colour profile and an embedded
    thumbnail can all precede it, which is why this is a loop and not an
    unpack.
    """
    index = 2
    # "<=", not "<": a frame header ending exactly at the last byte of the
    # buffer is still one, and a minimal JPEG is nothing but the two markers.
    while index + 9 <= len(head):
        if head[index] != 0xFF:
            index += 1
            continue
        marker = head[index + 1]
        # Markers that carry no length: padding, and the restart series.
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = int.from_bytes(head[index + 2:index + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = int.from_bytes(head[index + 5:index + 7], "big")
            width = int.from_bytes(head[index + 7:index + 9], "big")
            return (width, height)
        if length <= 0:
            return None
        index += 2 + length
    return None


def dimensions_from_ebay_url(url: str) -> Optional[Tuple[int, int]]:
    """
    (width, height) read out of an eBay CDN URL, without fetching it.

    eBay states the size in the path of its own copies, base64-encoded:
    ``https://i.ebayimg.com/00/s/MTY5WDM2MA==/z/...`` decodes to ``169X360``.

    Worth having because a picture-policy rejection names *eBay's copy* of the
    image rather than the URL that was sent, and that copy could previously
    not be matched back to anything we store -- the error was effectively
    unreadable. It is not necessary to fetch anything to learn that the
    offending picture is 169x360 and therefore 140 pixels short on its
    longest side.

    Advisory only, and deliberately not used in preference to measuring the
    real bytes: it is a guess about a URL format eBay can change whenever it
    likes, and a wrong guess here would reject a usable picture.
    """
    if not url or "ebayimg.com" not in url:
        return None
    for segment in url.split("?")[0].split("/"):
        if len(segment) < 8 or "=" not in segment:
            continue
        try:
            decoded = base64.b64decode(segment).decode("ascii")
        except Exception:  # noqa: BLE001 - not every segment is base64
            continue
        parts = decoded.upper().split("X")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            return (int(parts[0]), int(parts[1]))
    return None


def _fetch_header(url: str) -> Optional[bytes]:
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read(HEADER_BYTES)
    except Exception:  # noqa: BLE001 - reported as unknown, never as fine
        return None


def measure(
    url: str, *, fetch: Optional[Callable[[str], Optional[bytes]]] = None
) -> Optional[Tuple[int, int]]:
    """
    (width, height) of the image at a URL, or None if not established.

    ``fetch`` exists so callers can supply the bytes -- tests, mainly, so that
    checking the parsing of a truncated JPEG does not require a web server.
    """
    head = (fetch or _fetch_header)(url)
    size = dimensions_from_header(head or b"")
    if size is not None:
        return size
    # Only now, and only for eBay's own URLs: the real bytes are authoritative
    # and this is a fallback for when they could not be had.
    return dimensions_from_ebay_url(url)


def check(
    url: str, *, fetch: Optional[Callable[[str], Optional[bytes]]] = None
) -> Dict[str, Any]:
    """
    Judge one image against eBay's policy.

    ``ok`` is deliberately three-valued:

    * ``True``  -- measured, and large enough.
    * ``False`` -- measured, and eBay will reject it.
    * ``None``  -- **not established.** The fetch failed, or the format is not
      one this can read.

    ``None`` is not a pass and must not be treated as one, but it is also not
    grounds for refusing the picture: our fetch failing is not proof eBay's
    will, and an unrecognised format may be perfectly valid. The honest
    handling is to store the choice and say it could not be checked.
    """
    size = measure(url, fetch=fetch)
    if size is None:
        return {
            "ok": None,
            "width": None,
            "height": None,
            "longest": None,
            "reason": (
                "The image could not be measured -- it may be unreachable, or "
                "in a format this cannot read. eBay requires at least "
                f"{MIN_LONGEST_SIDE} pixels on the longest side and will "
                "re-check it on every later change to the listing."
            ),
        }

    width, height = int(size[0]), int(size[1])
    longest = max(width, height)
    shortest = max(1, min(width, height))
    result: Dict[str, Any] = {
        "ok": longest >= MIN_LONGEST_SIDE,
        "width": width,
        "height": height,
        "longest": longest,
        "reason": "",
    }
    if not result["ok"]:
        result["reason"] = (
            f"This image is {width}x{height}, so its longest side is "
            f"{longest} pixels and eBay needs {MIN_LONGEST_SIDE}. eBay would "
            f"reject it, and because it re-checks every picture on a listing "
            f"whenever anything changes, it would also block later price and "
            f"quantity updates to this listing."
        )
    elif longest / shortest > WIDE_ASPECT_RATIO:
        # Accepted, so not a failure -- but it is worth knowing before it
        # turns up letterboxed in the gallery.
        result["reason"] = (
            f"This image is {width}x{height}, which eBay accepts but will "
            f"letterbox in its square gallery thumbnail."
        )
    return result
