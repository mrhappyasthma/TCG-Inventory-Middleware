#!/usr/bin/env python3
"""
One-off migration of the File Exchange listings onto the Inventory API.

The store was built with File Exchange, and the Inventory API cannot see those
listings at all -- ``getOffers`` returns nothing for their SKUs, so nothing in
the push path, the refresh path or the automatic repricer can reach them.
``bulkMigrateListing`` is what converts one: it creates the inventory items,
the offers and (for a multi-variation listing) the inventory item group behind
a listing that already exists, keeping the same eBay item id, its watchers and
its search standing.

**This is deliberately not part of the web app.** It is irreversible, it runs
once, and nothing in the dashboard should offer it by accident. It is also the
one operation here with no dry run available at eBay's end: a migration either
happened or it did not.

What it does, in order, per listing:

1. Refuses outright unless every variation has a unique non-blank SKU that our
   own mirror knows, because eBay requires one and a migration that lands with
   SKUs we cannot match leaves offers we cannot address.
2. Migrates exactly one listing per call, never a batch -- eBay allows five,
   and reports per-listing outcomes inside a 200, which means a batch can be
   four successes and a failure that nobody can undo.
3. Records the new offer ids and the inventory item group key immediately.
   Those ids exist nowhere else; losing the response means reading them back
   one SKU at a time, and until they are stored the repricer cannot see the
   listing it just gained.
4. Records the listing's existing gallery image as its cover, because a later
   Refresh writes the group as a full replace and would otherwise substitute
   the first card's photo.
5. Verifies against eBay that the listing is still published under the same
   item id, and stops before touching another listing if it is not.

And because the call is irreversible while its reply is not guaranteed to
arrive, the preflight asks eBay whether each listing already has offers --
which is true of a migrated listing and of no other kind. A listing that
migrated on an attempt whose reply was lost is therefore detected rather than
migrated again, and ``--migrate`` **adopts** it instead: the offer ids are
read back one SKU at a time with ``getOffers``, which is the price of having
lost the response, and nothing is sent to eBay.

Usage, from the repository root:

    python scripts/migrate_csv_listings.py                  # preflight only
    python scripts/migrate_csv_listings.py --migrate 227511361186
    python scripts/migrate_csv_listings.py --migrate all

Nothing is sent to eBay without ``--migrate``, and each listing is confirmed
at the keyboard unless ``--yes`` is given.
"""

import argparse
import csv
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT,
             os.path.join(REPO_ROOT, "tcg_engine"),
             os.path.join(REPO_ROOT, "ebay_client")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.user_db import UserDatabase  # noqa: E402
from ebay_client import inventory  # noqa: E402
from ebay_client.client import EbayClient  # noqa: E402
from ebay_client.config import EbayConfig  # noqa: E402
from ebay_client.errors import ApiError, EbayError  # noqa: E402
from ebay_client.oauth import TokenStore  # noqa: E402
from tcg_engine.db import Database  # noqa: E402
from tcg_engine.plans import variation_group_key  # noqa: E402
from tcg_engine.push import inventory_group_key  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "data/inventory.db")
USER_DATABASE_URL = os.environ.get("USER_DATABASE_URL", "data/users.db")


class _ReadOnlyTokenStore(TokenStore):
    """
    Reads the connection the web app established, and never writes it.

    A script that refreshed and re-saved the token could race the running
    container's own refresh. Reading is enough: the refresh token in the
    database is what mints the access token this needs.
    """

    def __init__(self, user_db):
        self.user_db = user_db

    def load(self):
        return self.user_db.get_ebay_token()

    def save(self, token):
        pass


def build_client(user_db):
    if not EbayConfig.is_configured():
        raise SystemExit(
            "eBay is not configured in this environment. Run this where the "
            "app runs, so EBAY_CLIENT_ID and friends are set. On the NAS, "
            "from the project directory:\n"
            "    docker compose exec tcg-middleware python "
            "scripts/migrate_csv_listings.py\n"
            "That is the compose *service* name, which survives the container "
            "being recreated under a different name; `docker exec` needs the "
            "container name from `docker compose ps`."
        )
    client = EbayClient(EbayConfig.from_env(), store=_ReadOnlyTokenStore(user_db))
    if not client.oauth.is_connected():
        raise SystemExit(
            "No eBay account is connected. Connect it in the dashboard first; "
            "this script deliberately cannot start an OAuth flow."
        )
    return client


def preflight(db, listing):
    """
    Whether this listing can be migrated, and everything worth knowing first.

    Returns (blockers, notes, cards). A blocker is a reason eBay will refuse
    or a reason we will not be able to use the result; a note is something to
    read before agreeing.
    """
    parent = listing["ebay_parent_id"]
    cards = db.get_cards_for_listing(parent)
    blockers = []
    notes = []

    if not cards:
        blockers.append(
            "our mirror holds no cards for this listing, so there would be "
            "nothing to match the migrated SKUs against -- sync from eBay first"
        )
        return blockers, notes, cards

    labels = {}
    for card in cards:
        label = str(card.get("custom_label") or "").strip()
        if not label:
            blockers.append(
                f"{card['manifest_id']} has no SKU recorded; eBay requires a "
                f"unique SKU on every variation before it will migrate one"
            )
            continue
        if len(label) > inventory.MAX_SKU_LENGTH:
            blockers.append(
                f"SKU {label!r} is longer than eBay's "
                f"{inventory.MAX_SKU_LENGTH} characters"
            )
        labels.setdefault(label, []).append(card["manifest_id"])

    for label, owners in labels.items():
        if len(owners) > 1:
            blockers.append(
                f"SKU {label!r} is shared by {', '.join(owners)}; eBay "
                f"requires them unique within a listing"
            )

    if db.get_managed_listing_by_parent(parent) is not None:
        blockers.append(
            "this listing is already managed through the Inventory API, so "
            "there is nothing to migrate"
        )

    if listing.get("set_count", 1) > 1 or listing.get("condition_count", 1) > 1:
        notes.append(
            "the cards in it span more than one set or condition, so the "
            "group key recorded for it is a best guess and worth checking"
        )
    if not str(listing.get("cover_image_url") or "").strip():
        notes.append(
            "no cover photo is recorded for it here; this script reads eBay's "
            "own gallery image after migrating, but check the listing "
            "afterwards before using Refresh on it"
        )
    if int(listing.get("live_quantity") or 0) <= 0:
        notes.append(
            "eBay reports zero total quantity for it, which may mean it is "
            "out of stock rather than active"
        )

    return blockers, notes, cards


def already_migrated(api_transport, cards):
    """
    Ask eBay whether it already has offers for this listing's SKUs.

    This answers the question a failed migration leaves behind. The call is
    irreversible and its reply can be lost, so "it returned an error" does not
    mean "nothing happened" -- and our own records cannot tell the difference,
    because they are only written after a success. The Inventory API can:
    offers exist for a migrated listing and for no other kind.

    Returns (migrated, listing_ids) or (None, []) when the question could not
    be asked, which is deliberately not the same as "no".
    """
    if api_transport is None:
        return None, []
    sku = next((str(c.get("custom_label") or "").strip() for c in cards
                if str(c.get("custom_label") or "").strip()), "")
    if not sku:
        return None, []
    try:
        offers = inventory.get_offers(api_transport, sku)
    except ApiError as exc:
        # A 404 is the definitive *no*, not a failure to ask. eBay has no
        # inventory item for the SKU at all, which is precisely what an
        # unmigrated File Exchange listing looks like from this API. Reported
        # as "could not check" it made the four remaining listings look
        # unverified when they had in fact been verified.
        if exc.status_code == 404:
            return False, []
        return None, []
    except EbayError:
        return None, []
    ids = sorted({inventory.offer_listing_id(o) for o in offers
                  if inventory.offer_listing_id(o)})
    return bool(offers), ids


def describe(listing, cards, blockers, notes):
    parent = listing["ebay_parent_id"]
    print(f"\n  Listing #{parent}")
    print(f"    {listing.get('set_name') or '?'} / "
          f"{listing.get('condition') or '?'}")
    print(f"    {len(cards)} card(s), eBay reports "
          f"{listing.get('live_quantity')} in stock, catalogue says "
          f"{listing.get('catalog_quantity')}")
    for note in notes:
        print(f"    note: {note}")
    for blocker in blockers:
        print(f"    BLOCKED: {blocker}")
    if not blockers:
        print("    ready to migrate")


def record_result(db, listing, cards, row):
    """
    Store what the migration returned, before anything else can lose it.

    The offer ids are the whole point: without them the repricer and every
    future push cannot address this listing, and eBay will not issue them
    again.
    """
    parent = str(listing["ebay_parent_id"]).strip()
    group_key = variation_group_key(
        listing.get("set_name") or "", listing.get("condition") or ""
    )
    by_label = {
        str(c.get("custom_label") or "").strip(): c["manifest_id"]
        for c in cards
        if str(c.get("custom_label") or "").strip()
    }

    group = str(row.get("inventoryItemGroupKey") or "").strip()
    db.upsert_managed_listing(
        group_key,
        inventory_item_group_key=group or None,
        ebay_parent_id=parent,
        managed_by="api",
        pushed=True,
    )
    print(f"    recorded as API-managed under group key {group_key!r}"
          + (f", inventory group {group!r}" if group else ""))

    recorded = 0
    unmatched = []
    for item in row.get("inventoryItems") or []:
        sku = str(item.get("sku") or "").strip()
        offer_id = ""
        for offer in item.get("offers") or []:
            offer_id = str(offer.get("offerId") or "").strip()
            if offer_id:
                break
        offer_id = offer_id or str(item.get("offerId") or "").strip()
        manifest_id = by_label.get(sku)
        if not manifest_id or not offer_id:
            unmatched.append(f"{sku or '?'} -> {offer_id or 'no offer id'}")
            continue
        db.set_variation_offer(manifest_id, offer_id)
        recorded += 1

    print(f"    recorded {recorded} offer id(s)")
    for line in unmatched:
        print(f"    WARNING: could not match migrated SKU {line}")
    return group


def record_cover(db, api_transport, listing, group_key):
    """
    Keep the gallery image the listing already has.

    A Refresh writes the inventory item group as a full replace, so an
    unrecorded cover is a cover that the first later repair will silently
    replace with the first card's photo. That has happened once already, on a
    listing this application created itself.
    """
    if str(listing.get("cover_image_url") or "").strip():
        return
    if not group_key:
        return
    try:
        group = inventory.get_inventory_item_group(api_transport, group_key)
    except EbayError as exc:
        print(f"    WARNING: could not read the migrated group back: {exc}")
        return
    images = group.get("imageUrls") or []
    if images:
        db.set_listing_cover_image(str(listing["ebay_parent_id"]), images[0])
        print(f"    cover photo recorded from eBay: {images[0]}")
    else:
        print("    WARNING: eBay reports no gallery image for the migrated "
              "group. Set a cover photo before using Refresh on this listing.")


def verify(api_transport, listing, cards):
    """
    Confirm with eBay that the listing is still live under the same item id.

    There is an unresolved report of migrating several listings that share an
    inventory item group key unpublishing all but the first. Nothing here
    shares one, but the cost of checking is a single call and the cost of
    being wrong is a live listing quietly withdrawn.
    """
    parent = str(listing["ebay_parent_id"]).strip()
    sku = next((str(c.get("custom_label") or "").strip() for c in cards
                if str(c.get("custom_label") or "").strip()), "")
    if not sku:
        return True
    try:
        offers = inventory.get_offers(api_transport, sku)
    except EbayError as exc:
        print(f"    WARNING: could not verify the listing afterwards: {exc}")
        return True

    ids = {inventory.offer_listing_id(o) for o in offers}
    statuses = {str(o.get("status") or "").strip() for o in offers}
    if parent in ids:
        print(f"    verified: still published as #{parent} "
              f"(status {'/'.join(sorted(s for s in statuses if s)) or '?'})")
        return True
    print(f"    PROBLEM: eBay's offers for {sku} name listing(s) "
          f"{', '.join(sorted(i for i in ids if i)) or 'none'}, not #{parent}. "
          f"Stopping here -- check the listing on eBay before migrating "
          f"anything else.")
    return False


EBAY_MIN_LONGEST_SIDE = 500
IMAGE_HEADER_BYTES = 131072


def image_dimensions(url):
    """
    (width, height) of an image, read from its header alone.

    Parsed by hand rather than with Pillow, which is not a dependency of this
    project and would be a large one to add for a diagnostic. Only the header
    is fetched: the dimensions live in the first few bytes of every format
    here, so a 6 MB photograph costs the same as a thumbnail.

    Returns None when the format is unrecognised or the fetch fails, which
    the caller must report as "unknown" rather than as "fine" -- the whole
    point is to find pictures eBay will refuse.
    """
    import struct
    import urllib.request

    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "TCG-Inventory-Middleware/1.0"}
        )
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
            w = int.from_bytes(head[24:27], "little") + 1
            h = int.from_bytes(head[27:30], "little") + 1
            return (w, h)
        if head[12:16] == b"VP8 " and len(head) >= 30:
            w = int.from_bytes(head[26:28], "little") & 0x3FFF
            h = int.from_bytes(head[28:30], "little") & 0x3FFF
            return (w, h)
        return None
    if head[:2] == b"\xff\xd8":
        # Walk the marker chain to a Start Of Frame, which is where a JPEG
        # states its size. It is not at a fixed offset: EXIF, colour profiles
        # and an embedded thumbnail can all precede it.
        index = 2
        # "<=", not "<": a SOF ending exactly at the last byte of the buffer
        # is still a SOF, and a minimal JPEG is nothing but SOI and SOF.
        while index + 9 <= len(head):
            if head[index] != 0xFF:
                index += 1
                continue
            marker = head[index + 1]
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
    return None


def check_images(db, parent):
    """
    Every picture we hold for one listing, with the ones eBay would refuse.

    eBay re-validates pictures on any revise, so a single image below 500
    pixels on its longest side blocks every future change to a listing --
    including the item-level SKU that migration requires. It names the
    offending URL but not which card it belongs to, and its copy lives on
    i.ebayimg.com under a name that cannot be matched back to a source.

    Checking our own images answers the question that actually matters: if
    the undersized picture is one of ours, replacing it on eBay alone is
    temporary, because the next push or Refresh sends it straight back.

    Read-only, and touches eBay not at all.
    """
    cards = db.get_cards_for_listing(parent)
    if not cards:
        print(f"Nothing recorded for listing #{parent}.")
        return 1

    targets = []
    cover = ""
    for row in db.get_ebay_listings():
        if str(row["ebay_parent_id"]).strip() == parent:
            cover = str(row.get("cover_image_url") or "").strip()
    if cover:
        targets.append(("cover photo", cover))
    for card in cards:
        url = str(card.get("cdn_image") or "").strip()
        if url:
            targets.append((f"{card['manifest_id']} {card.get('product_name') or ''}"[:38], url))

    if not targets:
        print(f"Listing #{parent}: we hold no image URLs for it at all.")
        return 1

    print(f"\nListing #{parent}: checking {len(targets)} image(s) we hold. "
          f"eBay requires {EBAY_MIN_LONGEST_SIDE}px on the longest side.\n")
    too_small = []
    unknown = []
    for label, url in targets:
        size = image_dimensions(url)
        if size is None:
            unknown.append((label, url))
            print(f"  {label:40} {'unreadable':>12}  {url[:70]}")
            continue
        width, height = size
        longest = max(width, height)
        verdict = "OK" if longest >= EBAY_MIN_LONGEST_SIDE else "TOO SMALL"
        if verdict == "TOO SMALL":
            too_small.append((label, url, width, height))
        print(f"  {label:40} {width:>5}x{height:<6} {verdict:10} {url[:60]}")

    print()
    if too_small:
        print(f"  {len(too_small)} of our own image(s) are below eBay's "
              f"minimum. Fixing them here is the durable fix -- changing them "
              f"on eBay alone lasts until the next push or Refresh:")
        for label, url, width, height in too_small:
            print(f"    {label}: {width}x{height}  {url}")
        # The two sources need different remedies, and saying the wrong one
        # sends somebody to re-export their whole inventory for a field that
        # does not come from the export at all.
        if any(label == "cover photo" for label, _, _, _ in too_small):
            print("\n  The cover photo is not from the SortSwift export. It "
                  "is stored against the listing by this application: change "
                  "it on the eBay Listings tab, with the photo button on that "
                  "listing's row.")
            if any("gstatic.com" in url or "tbn" in url
                   for _, url, _, _ in too_small):
                print("  That URL is a Google Images *search thumbnail*, "
                      "which is always a few hundred pixels and is not a "
                      "stable link either. Use the card's own image, or any "
                      "URL at least 500px on its longest side.")
        if any(label != "cover photo" for label, _, _, _ in too_small):
            print("\n  Card images come from the export's CDN Image column, "
                  "so those have to be corrected in SortSwift and the export "
                  "re-uploaded.")
    else:
        print("  Every image we hold meets the minimum. The picture eBay is "
              "refusing is therefore one it holds and we do not -- an older "
              "upload still attached to the listing. Remove or replace it in "
              "Seller Hub; nothing in our catalogue will put it back.")
    if unknown:
        print(f"  {len(unknown)} image(s) could not be read, so they are "
              f"neither confirmed nor cleared.")
    return 0


def verify_report(db, client, parent):
    """
    What eBay holds for one listing, card by card, against what we recorded.

    Exists because the first successful migration was reported as a failure:
    the verification read the listing id from the wrong level of the offer, so
    a healthy published offer looked like an offer belonging to no listing.
    Printing the rows rather than a verdict is the remedy -- a summary that
    can be wrong in that way is worth less than the facts it was summarising.

    Read-only.
    """
    cards = db.get_cards_for_listing(parent)
    if not cards:
        print(f"Nothing recorded for listing #{parent}.")
        return 1

    managed = db.get_managed_listing_by_parent(parent)
    print(f"\nListing #{parent}")
    print(f"  our record: "
          + (f"managed, group key {managed['group_key']!r}, inventory group "
             f"{managed.get('inventory_item_group_key') or '-'!r}"
             if managed else "not managed through the API"))

    group = None
    group_key = str((managed or {}).get("inventory_item_group_key") or "").strip()
    if group_key:
        try:
            group = inventory.get_inventory_item_group(client.seller, group_key)
        except EbayError as exc:
            print(f"  inventory group could not be read: {exc}")
    if group is not None:
        images = group.get("imageUrls") or []
        print(f"  eBay's group: {len(group.get('variantSKUs') or [])} SKU(s), "
              f"{len(images)} image(s), title "
              f"{str(group.get('title') or '')[:60]!r}")

    ok = 0
    problems = 0
    for card in cards:
        sku = str(card.get("custom_label") or "").strip() or card["manifest_id"]
        stored = str(card.get("offer_id") or "").strip() or "-"
        try:
            offers = inventory.get_offers(client.seller, sku)
        except ApiError as exc:
            note = ("no inventory item at eBay (404) -- not migrated"
                    if exc.status_code == 404 else str(exc))
            print(f"  {sku:24} stored offer {stored:14} eBay: {note}")
            problems += 1
            continue

        if not offers:
            print(f"  {sku:24} stored offer {stored:14} eBay: no offers")
            problems += 1
            continue

        for offer in offers:
            offer_id = str(offer.get("offerId") or "").strip()
            listing_id = inventory.offer_listing_id(offer)
            status = str(offer.get("status") or "?")
            price = ((offer.get("pricingSummary") or {})
                     .get("price") or {}).get("value")
            agrees = "OK" if listing_id == parent else "DIFFERENT LISTING"
            matches = "" if offer_id == stored.strip() else "  (we stored a different offer id)"
            print(f"  {sku:24} stored offer {stored:14} eBay offer "
                  f"{offer_id:14} listing #{listing_id or '-':14} "
                  f"{status:10} ${price or '-':8} {agrees}{matches}")
            if listing_id == parent:
                ok += 1
            else:
                problems += 1

    print(f"\n  {ok} offer(s) confirmed against #{parent}, {problems} to look at.")
    return 0 if problems == 0 else 1


def sku_fix_rows(listings):
    """
    A File Exchange Revise that gives each parent an item-level SKU.

    eBay refuses to migrate a multi-variation listing whose *listing-level*
    SKU is empty -- error 25002, "The listing SKU cannot be null or empty" --
    even when every variation has one. Our own sync already recorded that
    these listings have exactly that shape: ``AGENTS.md`` notes the Active
    Inventory report carries "a blank SKU at the item level" for a
    multi-variation listing, which is the convention for a parent row.

    The label is derived the same way a push derives an inventory item group
    key: a slug of "<set>|<condition>" plus a hash, so it is deterministic,
    unique across the store, within eBay's 50 characters, and impossible to
    confuse with a manifest id occupying a variation's SKU.

    The file carries no variation columns at all. File Exchange revises the
    fields present and leaves the rest alone, so the variation structure --
    which a malformed Revise *can* damage, and which is why this project
    documents the parent-row rule at all -- is not addressed by it.
    """
    rows = []
    for listing in listings:
        group_key = variation_group_key(
            listing.get("set_name") or "", listing.get("condition") or ""
        )
        rows.append({
            "Action": "Revise",
            "ItemID": str(listing["ebay_parent_id"]).strip(),
            "CustomLabel": inventory_group_key(group_key),
        })
    return rows


def write_sku_fix(listings, path):
    rows = sku_fix_rows(listings)
    if not rows:
        print("\nNothing needs an item-level SKU.")
        return
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["Action", "ItemID", "CustomLabel"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} row(s) to {path}:\n")
    print("Action,ItemID,CustomLabel")
    for row in rows:
        print(f"{row['Action']},{row['ItemID']},{row['CustomLabel']}")
    print(
        "\nUpload that through File Exchange, wait for eBay to report it "
        "applied, then re-run the preflight. It sets only the listing-level "
        "SKU -- no variation column is present, so the variations are not "
        "addressed by it."
    )


def recover_one(db, client, listing, cards):
    """
    Adopt a listing eBay has already migrated but we never recorded.

    The irreversible half is done and cannot be done again; what is missing is
    the offer id per SKU, which is the only handle that can change a price.
    ``bulkMigrateListing`` would not hand them over a second time, but
    ``getOffers`` will, one SKU at a time -- which is the price of having lost
    the reply.

    Writes nothing to eBay.
    """
    parent = str(listing["ebay_parent_id"]).strip()
    print(f"\n  Adopting #{parent}, already migrated at eBay ...")

    group_key = variation_group_key(
        listing.get("set_name") or "", listing.get("condition") or ""
    )
    inventory_group = ""
    recorded = 0
    mismatched = []

    for card in cards:
        sku = str(card.get("custom_label") or "").strip()
        if not sku:
            continue
        try:
            offers = inventory.get_offers(client.seller, sku)
        except EbayError as exc:
            print(f"    WARNING: could not read offers for {sku}: {exc}")
            continue
        for offer in offers:
            offer_id = str(offer.get("offerId") or "").strip()
            listing_id = inventory.offer_listing_id(offer)
            if listing_id and listing_id != parent:
                mismatched.append(f"{sku} -> #{listing_id}")
                continue
            if offer_id:
                db.set_variation_offer(card["manifest_id"], offer_id)
                recorded += 1
            group = str(offer.get("inventoryItemGroupKey") or "").strip()
            if group:
                inventory_group = group
            break

    db.upsert_managed_listing(
        group_key,
        inventory_item_group_key=inventory_group or None,
        ebay_parent_id=parent,
        managed_by="api",
        pushed=True,
    )
    print(f"    recorded {recorded} offer id(s) under group key {group_key!r}"
          + (f", inventory group {inventory_group!r}" if inventory_group else ""))
    for line in mismatched:
        print(f"    WARNING: an offer for {line} belongs to a different "
              f"listing and was left alone")
    if not inventory_group:
        print("    note: eBay reported no inventory item group for these "
              "offers. Check the listing before using Refresh on it, which "
              "writes the group as a full replace.")
    record_cover(db, client.seller, listing, inventory_group)
    return True


def report_call_failure(exc):
    """
    Print everything eBay said, not just that it said no.

    The first real migration attempt failed with "returned 400" and nothing
    else, which is indistinguishable from a malformed request of our own. Each
    of these three is a different diagnosis: an ``errorId`` names a documented
    eBay condition, a body without an ``errors`` list usually means the
    request never reached the Inventory API, and an empty body with a 400
    means the payload shape itself was rejected.
    """
    print(f"    FAILED: {exc}")
    for error in getattr(exc, "errors", None) or []:
        print(f"      errorId {error.get('errorId')}: "
              f"{error.get('longMessage') or error.get('message')}")
        for parameter in error.get("parameters") or []:
            print(f"        {parameter.get('name')} = {parameter.get('value')}")
    body = getattr(exc, "body", "") or ""
    if body and not getattr(exc, "errors", None):
        print(f"      raw body: {body[:1500]}")


def migrate_one(db, client, listing, cards):
    parent = str(listing["ebay_parent_id"]).strip()
    print(f"\n  Migrating #{parent} ...")
    rows = inventory.bulk_migrate_listing(client.seller, [parent])
    if not rows:
        print("    eBay returned no result rows at all. Nothing can be "
              "concluded from that -- check the listing on eBay before "
              "retrying.")
        return False

    row = rows[0]
    print(f"    eBay says: statusCode={row.get('statusCode')}")
    for warning in row.get("warnings") or []:
        print(f"    warning: {warning.get('longMessage') or warning.get('message')}")

    if inventory.status_failed(row):
        print(f"    FAILED: {inventory.describe_failure(row)}")
        print(f"    full response: {json.dumps(row, indent=2)[:2000]}")
        return False

    group_key = record_result(db, listing, cards, row)
    record_cover(db, client.seller, listing, group_key)
    return verify(client.seller, listing, cards)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate File Exchange listings onto the Inventory API."
    )
    parser.add_argument(
        "--migrate", metavar="ITEM_ID", default=None,
        help="an eBay item id to migrate, or 'all'. Omit for a preflight "
             "report that sends nothing.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="do not ask before each listing. Every other safeguard still "
             "applies.",
    )
    parser.add_argument(
        "--check-images", metavar="ITEM_ID", default=None,
        help="report the pixel size of every image we hold for one listing, "
             "flagging any below eBay's 500px minimum. Touches eBay not at "
             "all.",
    )
    parser.add_argument(
        "--verify", metavar="ITEM_ID", default=None,
        help="report what eBay holds for one listing's SKUs -- offer ids, "
             "listing ids and statuses -- and what we have recorded. "
             "Read-only.",
    )
    parser.add_argument(
        "--sku-fix", metavar="PATH", nargs="?", const="ebay_listing_sku_fix.csv",
        default=None,
        help="write a File Exchange Revise file giving each migratable "
             "listing an item-level SKU, which eBay requires before it will "
             "migrate a multi-variation listing (error 25002). Sends nothing "
             "to eBay.",
    )
    args = parser.parse_args()

    db = Database(db_path=DATABASE_URL)
    user_db = UserDatabase(db_path=USER_DATABASE_URL)

    if args.check_images:
        return check_images(db, args.check_images.strip())

    if args.verify:
        return verify_report(db, build_client(user_db), args.verify.strip())

    # Built up front so the preflight can ask eBay whether a listing is
    # already migrated. Read-only either way: nothing is written to eBay
    # without --migrate. A missing connection is not fatal here, because the
    # rest of the preflight needs only our own database.
    probe = None
    try:
        probe = build_client(user_db).seller
    except SystemExit as exc:
        print(f"note: {exc}")
        print("      The preflight below still runs, but cannot ask eBay "
              "whether a listing has already been migrated.")

    listings = db.get_ebay_listings()
    candidates = []
    recoverable = []
    print(f"\n{len(listings)} live listing(s) in the mirror.")
    for listing in listings:
        blockers, notes, cards = preflight(db, listing)

        # Whether *we* have a record of managing it, which the preflight has
        # already reported as a blocker if so.
        recorded = db.get_managed_listing_by_parent(
            str(listing["ebay_parent_id"]).strip()
        ) is not None

        migrated, ids = already_migrated(probe, cards)
        if migrated and not recorded:
            # Not a migration candidate, but not a dead end either: eBay has
            # done the irreversible part and only our own record is missing.
            blockers.append(
                "eBay already has offers for this listing's SKUs"
                + (f" under listing {', '.join(ids)}" if ids else "")
                + ", but we have no record of managing it. Re-run with "
                  "--migrate to adopt the existing offers; nothing will be "
                  "sent to eBay."
            )
            recoverable.append((listing, cards))
        elif migrated and recorded:
            # The normal state of a listing this application created itself.
            # Saying "we never recorded it" here was simply false, and it
            # said it about seven healthy listings.
            notes.append(
                "confirmed with eBay: the Inventory API can see it, which is "
                "what being managed means"
            )
        elif migrated is None and probe is not None:
            notes.append(
                "could not check with eBay whether it is already migrated"
            )

        describe(listing, cards, blockers, notes)
        if not blockers:
            candidates.append((listing, cards))

    if args.sku_fix:
        # Only the ones that pass every other check: a listing already
        # migrated, or blocked for a different reason, does not need this.
        write_sku_fix([l for l, _ in candidates], args.sku_fix)
        return 0

    if not args.migrate:
        print(f"\nPreflight only. {len(candidates)} listing(s) could be "
              f"migrated, {len(recoverable)} already migrated but not "
              f"recorded. Nothing was written.")
        print("Re-run with --migrate <item id>, or --migrate all, to proceed.")
        print("If eBay refuses with error 25002 (\"The listing SKU cannot be "
              "null or empty\"), re-run with --sku-fix to get the File "
              "Exchange Revise file that fixes it.")
        return 0

    if args.migrate != "all":
        wanted = args.migrate.strip()
        candidates = [(l, c) for l, c in candidates
                      if str(l["ebay_parent_id"]).strip() == wanted]
        recoverable = [(l, c) for l, c in recoverable
                       if str(l["ebay_parent_id"]).strip() == wanted]
        if not candidates and not recoverable:
            print(f"\n{args.migrate} is not a listing that can be migrated. "
                  f"See the report above.")
            return 1

    # Adoption first, and without any of the migration warnings: it sends no
    # writes to eBay at all, so there is nothing to confirm.
    if recoverable:
        client = build_client(user_db)
        for listing, cards in recoverable:
            recover_one(db, client, listing, cards)
        if not candidates:
            return 0

    print("\n" + "=" * 70)
    print("bulkMigrateListing CANNOT BE UNDONE.")
    print("After it succeeds, File Exchange and the Trading API can no longer")
    print("revise these listings -- every future change goes through this")
    print("application's API path. The eBay item id, watchers and search")
    print("standing are preserved.")
    print("=" * 70)
    # eBay's own eligibility rules, printed because none of them can be
    # checked from here. The first migration attempt failed with a bare 400,
    # and every one of these produces one -- so they belong where somebody is
    # about to type an item id, not in a document.
    print("eBay will refuse the migration unless all of these are true of the")
    print("listing, and none of them is visible to this script:")
    print("  1. It is fixed-price. Auctions cannot be migrated at all.")
    print("  2. Every variation has its own seller-defined SKU.")
    print("  3. It uses Business Policies for payment, return and shipping.")
    print("     A listing carrying the legacy per-listing shipping, returns or")
    print("     payment fields is rejected -- and File Exchange could write")
    print("     either form, so this is the one worth checking first.")
    print("  4. Its payment policy has immediate payment enabled.")
    print("Check the listing in Seller Hub against that list if a call fails.")
    print("=" * 70)

    client = build_client(user_db)

    migrated = 0
    for listing, cards in candidates:
        parent = listing["ebay_parent_id"]
        if not args.yes:
            answer = input(f"\nMigrate #{parent} "
                           f"({len(cards)} cards)? Type the item id to "
                           f"confirm: ").strip()
            if answer != str(parent).strip():
                print("    skipped")
                continue
        try:
            ok = migrate_one(db, client, listing, cards)
        except EbayError as exc:
            report_call_failure(exc)
            print("    Stopping. Check the listing on eBay before retrying: a "
                  "failed call may still have migrated it.")
            return 1
        if not ok:
            return 1
        migrated += 1

    print(f"\nMigrated {migrated} listing(s).")
    if migrated:
        print("They are now API-managed: pushes, Refresh and the automatic "
              "repricer can all reach them, and they are excluded from the "
              "Revise CSV files from here on.")
        print("Check each one on eBay before trusting the next step.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
