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

Usage, from the repository root:

    python scripts/migrate_csv_listings.py                  # preflight only
    python scripts/migrate_csv_listings.py --migrate 227511361186
    python scripts/migrate_csv_listings.py --migrate all

Nothing is sent to eBay without ``--migrate``, and each listing is confirmed
at the keyboard unless ``--yes`` is given.
"""

import argparse
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
from ebay_client.errors import EbayError  # noqa: E402
from ebay_client.oauth import TokenStore  # noqa: E402
from tcg_engine.db import Database  # noqa: E402
from tcg_engine.plans import variation_group_key  # noqa: E402

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
            "app runs, so EBAY_CLIENT_ID and friends are set -- on the NAS "
            "that is: docker exec -it <container> python "
            "scripts/migrate_csv_listings.py"
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

    ids = {str(o.get("listingId") or "").strip() for o in offers}
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
    args = parser.parse_args()

    db = Database(db_path=DATABASE_URL)
    user_db = UserDatabase(db_path=USER_DATABASE_URL)

    listings = db.get_ebay_listings()
    candidates = []
    print(f"{len(listings)} live listing(s) in the mirror.")
    for listing in listings:
        blockers, notes, cards = preflight(db, listing)
        describe(listing, cards, blockers, notes)
        if not blockers:
            candidates.append((listing, cards))

    if not args.migrate:
        print(f"\nPreflight only. {len(candidates)} listing(s) could be "
              f"migrated. Nothing was sent to eBay.")
        print("Re-run with --migrate <item id>, or --migrate all, to proceed.")
        return 0

    if args.migrate != "all":
        candidates = [(l, c) for l, c in candidates
                      if str(l["ebay_parent_id"]).strip() == args.migrate.strip()]
        if not candidates:
            print(f"\n{args.migrate} is not a listing that can be migrated. "
                  f"See the report above.")
            return 1

    print("\n" + "=" * 70)
    print("bulkMigrateListing CANNOT BE UNDONE.")
    print("After it succeeds, File Exchange and the Trading API can no longer")
    print("revise these listings -- every future change goes through this")
    print("application's API path. The eBay item id, watchers and search")
    print("standing are preserved.")
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
            print(f"    FAILED: {exc}")
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
