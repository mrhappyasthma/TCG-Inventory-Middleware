#!/usr/bin/env python3
"""
Find images eBay will refuse, before it refuses them.

eBay requires at least 500 pixels on an image's longest side, and it
re-validates **every** picture on a listing whenever anything about that
listing changes. So one undersized image does not merely look bad: it blocks
all future changes to the listing, including ones that have nothing to do with
pictures. That is not hypothetical -- a 308x164 Google Images search thumbnail
sitting in a cover photo field blocked two listings completely, and the only
symptom was an error naming eBay's own copy of the image, which cannot be
matched back to anything we store.

Two questions, and the second is the one that matters:

* **Is this URL usable?** ``--url`` measures it before you paste it into the
  cover photo field.
* **Is anything already broken?** ``--listing`` and ``--all`` measure every
  image this application holds for a listing -- the cover photo plus each
  card's picture.

Whose image it is decides the remedy, so the report says. A **cover photo** is
stored against the listing by this application and changed on the eBay
Listings tab. A **card image** comes from the export's ``CDN Image`` column
and has to be corrected in SortSwift and the export re-uploaded. Fixing
either on eBay alone lasts until the next push or Refresh, which sends ours
straight back.

Reads image headers only: no Pillow, which is not a dependency of this project
and would be a large one to add for a diagnostic, and never a whole file, so a
6 MB photograph costs the same as a thumbnail.

Usage, from the repository root or inside the container:

    python scripts/check_images.py --url https://example.com/logo.png
    python scripts/check_images.py --listing 227513097221
    python scripts/check_images.py --all

On the NAS, where the databases live:

    docker compose exec tcg-middleware python scripts/check_images.py --all

Touches eBay not at all, and writes nothing anywhere.
"""

import argparse
import os
import struct
import sys
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

from tcg_engine.db import Database  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "data/inventory.db")

# eBay's published minimum for the longest side of a listing image.
EBAY_MIN_LONGEST_SIDE = 500
# Enough to reach a JPEG's Start Of Frame past EXIF and an embedded thumbnail.
IMAGE_HEADER_BYTES = 131072
# Past this, an image letterboxes badly in eBay's square gallery thumbnail.
WIDE_ASPECT_RATIO = 2.0
USER_AGENT = "TCG-Inventory-Middleware/1.0 (image size check)"

COVER_LABEL = "cover photo"


def image_dimensions(url):
    """
    (width, height) of an image, read from its header alone.

    Returns None when the fetch fails or the format is not one of the four
    handled here. A caller must report that as "unknown" and never as "fine":
    the entire purpose is to find images eBay will reject, and eBay has to
    fetch the URL too, so a fetch that fails for us may well fail for it.
    """
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            head = response.read(IMAGE_HEADER_BYTES)
    except Exception:
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


def _jpeg_dimensions(head):
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


def mediawiki_original(url):
    """
    The full-size version of a MediaWiki thumbnail URL, or "" if not one.

    Worth special-casing because it is the one situation where a too-small
    image has a larger version at a URL you can work out: MediaWiki states the
    thumbnail's width in the path, as in
    ``.../thumb/0/01/X.png/360px-X.png``. Dropping the last segment and the
    "thumb/" marker gives the original -- which for the set logos in question
    was 1280px rather than 360.

    Derived by splitting rather than with a regular expression, which needs no
    escapes and reads more plainly.
    """
    if "/thumb/" not in url:
        return ""
    head, _, tail = url.rpartition("/")
    if "px-" not in tail:
        return ""
    return head.replace("/thumb/", "/", 1)


def describe(url, indent="    "):
    """Measure one URL and print the verdict. Returns True if eBay will take it."""
    size = image_dimensions(url)
    if size is None:
        print(f"{indent}could not be read -- the fetch failed, or the format "
              f"is not PNG, JPEG, GIF or WebP.")
        print(f"{indent}eBay has to fetch it too, so treat this as a warning "
              f"rather than a pass.")
        return False

    width, height = size
    longest = max(width, height)
    ok = longest >= EBAY_MIN_LONGEST_SIDE
    print(f"{indent}{width}x{height}, longest side {longest} -- "
          + ("OK" if ok
             else f"TOO SMALL, eBay needs {EBAY_MIN_LONGEST_SIDE}"))

    ratio = longest / max(1, min(width, height))
    if ok and ratio >= WIDE_ASPECT_RATIO:
        print(f"{indent}note: {ratio:.1f}:1, so it letterboxes in eBay's "
              f"square gallery thumbnail. Allowed -- the minimum size is the "
              f"only hard rule -- but it shows small in search results.")
    return ok


def check_url(url):
    print(f"\n  {url}")
    ok = describe(url)

    original = mediawiki_original(url)
    if original:
        print("\n  This is a MediaWiki thumbnail. The original:")
        print(f"  {original}")
        describe(original)
    return 0 if ok else 1


def images_for(db, parent):
    """Every image URL this application holds for one listing, labelled."""
    targets = []
    for row in db.get_ebay_listings():
        if str(row["ebay_parent_id"]).strip() == str(parent).strip():
            cover = str(row.get("cover_image_url") or "").strip()
            if cover:
                targets.append((COVER_LABEL, cover))
            break
    for card in db.get_cards_for_listing(str(parent).strip()):
        url = str(card.get("cdn_image") or "").strip()
        if url:
            label = f"{card['manifest_id']} {card.get('product_name') or ''}"
            targets.append((label[:38], url))
    return targets


def check_listing(db, parent, quiet_when_clean=False):
    """
    Measure every image we hold for one listing.

    Returns the number of undersized images found, or -1 when the listing is
    unknown, so a caller sweeping the whole store can total them up.
    """
    targets = images_for(db, parent)
    if not targets:
        print(f"\nListing #{parent}: we hold no image URLs for it.")
        return -1

    too_small = []
    unknown = []
    lines = []
    for label, url in targets:
        size = image_dimensions(url)
        if size is None:
            unknown.append(label)
            lines.append(f"  {label:40} {'unreadable':>12}  {url[:58]}")
            continue
        width, height = size
        ok = max(width, height) >= EBAY_MIN_LONGEST_SIDE
        if not ok:
            too_small.append((label, url, width, height))
        lines.append(f"  {label:40} {width:>5}x{height:<6} "
                     f"{'OK' if ok else 'TOO SMALL':10} {url[:58]}")

    if quiet_when_clean and not too_small and not unknown:
        print(f"\nListing #{parent}: {len(targets)} image(s), all OK.")
        return 0

    print(f"\nListing #{parent}: {len(targets)} image(s). eBay requires "
          f"{EBAY_MIN_LONGEST_SIDE}px on the longest side.\n")
    for line in lines:
        print(line)

    if too_small:
        print(f"\n  {len(too_small)} image(s) are below eBay's minimum, which "
              f"blocks every future change to this listing:")
        for label, url, width, height in too_small:
            print(f"    {label}: {width}x{height}")
            print(f"      {url}")
            original = mediawiki_original(url)
            if original:
                print(f"      a larger version may exist at: {original}")
        # The two sources need different remedies, and naming the wrong one
        # sends somebody to re-export their whole inventory over a field that
        # does not come from the export at all.
        if any(label == COVER_LABEL for label, _, _, _ in too_small):
            print("\n  The cover photo is stored against the listing by this "
                  "application, not taken from the SortSwift export: change "
                  "it on the eBay Listings tab, with the photo button on that "
                  "listing's row.")
            if any("gstatic.com" in url or "tbn:" in url
                   for _, url, _, _ in too_small):
                print("  That URL is a Google Images *search thumbnail*. Those "
                      "are always a few hundred pixels and are not stable "
                      "links either.")
        if any(label != COVER_LABEL for label, _, _, _ in too_small):
            print("\n  Card images come from the export's CDN Image column, so "
                  "those must be corrected in SortSwift and the export "
                  "re-uploaded.")
        print("\n  Fixing an image on eBay alone lasts until the next push or "
              "Refresh, which sends ours back.")
    if unknown:
        print(f"\n  {len(unknown)} image(s) could not be read, so they are "
              f"neither confirmed nor cleared: {', '.join(unknown[:6])}"
              + (" ..." if len(unknown) > 6 else ""))
    return len(too_small)


def check_all(db):
    listings = db.get_ebay_listings()
    if not listings:
        print("No linked eBay listings. Sync from eBay first.")
        return 1
    print(f"Checking every image held for {len(listings)} listing(s).")
    total = 0
    for listing in listings:
        found = check_listing(db, listing["ebay_parent_id"],
                              quiet_when_clean=True)
        if found > 0:
            total += found
    print(f"\n{total} undersized image(s) across {len(listings)} listing(s).")
    return 0 if total == 0 else 1


def main():
    parser = argparse.ArgumentParser(
        description="Find images eBay will refuse for being under "
                    f"{EBAY_MIN_LONGEST_SIDE}px on the longest side.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", metavar="URL",
                       help="measure one image URL, before using it")
    group.add_argument("--listing", metavar="ITEM_ID",
                       help="measure every image we hold for one listing")
    group.add_argument("--all", action="store_true",
                       help="measure every image we hold, for every listing")
    args = parser.parse_args()

    if args.url:
        # Needs no database, so it is answered before one is opened.
        return check_url(args.url.strip())

    db = Database(db_path=DATABASE_URL)
    if args.listing:
        found = check_listing(db, args.listing.strip())
        return 0 if found == 0 else 1
    return check_all(db)


if __name__ == "__main__":
    sys.exit(main())
