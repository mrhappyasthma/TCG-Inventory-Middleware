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
Listings tab. A **card image** comes from the export's ``CDN Image``
column -- or, for a card with no scan, the ``Stock Image`` column it falls
back to, labelled ``(stock)`` in the report -- and has to be corrected in
SortSwift and the export re-uploaded. Fixing
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
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

from ebay_client.pictures import (  # noqa: E402
    MIN_LONGEST_SIDE as EBAY_MIN_LONGEST_SIDE,
    WIDE_ASPECT_RATIO,
    measure as image_dimensions,
)
from tcg_engine.db import Database  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "data/inventory.db")

COVER_LABEL = "cover photo"

# A trading card is 2.5 x 3.5 inches, so a picture of a whole one is about
# 0.714 wide for every unit tall. This is not an eBay rule -- eBay accepts
# any shape over the minimum size -- which is exactly why it is checked
# here, in the card-aware tool, rather than in ebay_client, which knows
# nothing about cards.
#
# It catches a failure the size check cannot see: an image that is a *crop*
# of a card rather than the card. Two of a 214-card Chinese import were the
# top half only -- 866x569 and 856x581, full width and about half the
# height, so the attacks, weakness, retreat cost and set number were all
# missing. Both sailed through the 500-pixel check at 866 and 856, and
# would have gone live as those variations' photographs.
CARD_ASPECT = 2.5 / 3.5
# Generous: a scan with a little border, or trimmed tight, is still a card.
# Only something well outside this is worth a person's attention.
CARD_ASPECT_TOLERANCE = 0.12


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

    if not card_shaped(width, height):
        # Louder than the letterbox note below, because the consequence is
        # different in kind: that one is cosmetic, this one means the
        # picture is not of the whole card.
        print(f"{indent}NOT CARD-SHAPED: {width/height:.2f} wide per unit "
              f"tall, where a trading card is {CARD_ASPECT:.2f}. This is "
              f"usually a crop rather than the whole card -- check it "
              f"before listing. eBay accepts it; a buyer looking for the "
              f"attacks and the set number will not find them.")

    ratio = longest / max(1, min(width, height))
    if ok and ratio >= WIDE_ASPECT_RATIO:
        print(f"{indent}note: {ratio:.1f}:1, so it letterboxes in eBay's "
              f"square gallery thumbnail. Allowed -- the minimum size is the "
              f"only hard rule -- but it shows small in search results.")
    return ok


def card_shaped(width, height):
    """
    Whether these proportions are plausibly a photograph of a whole card.

    Deliberately separate from the size verdict and never fatal: it is a
    judgement about the subject of the picture, not about eBay's rules, and
    a cover photo legitimately is not a card at all.
    """
    if not width or not height:
        return True
    return abs((width / height) - CARD_ASPECT) / CARD_ASPECT <= CARD_ASPECT_TOLERANCE


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
        # The resolved picture, not the scan: a card with no scan is listed
        # with SortSwift's generic catalogue photo, and eBay applies its
        # 500-pixel minimum to whatever we actually send. A stock photo too
        # small to accept would block every future revision of the listing
        # exactly as an undersized scan does.
        url = str(card.get("image_url") or card.get("cdn_image") or "").strip()
        if url:
            label = f"{card['manifest_id']} {card.get('product_name') or ''}"
            if not str(card.get("cdn_image") or "").strip():
                label = f"{label} (stock)"
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
