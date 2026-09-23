#!/usr/bin/env python3
"""
Re-title the live variation listings from the current title templates.

Changing ``variation_title_template`` (or one of the per-language ones)
changes what a *future* write sends. It does nothing to the listings already
up: a title lives on the inventory item group at eBay, and nothing re-sends
that group until something else happens to the listing. So a template change
leaves the store in two styles until this is run.

**It re-sends each listing through ``push.refresh_listing`` rather than
writing the group itself.** That is the whole design of this script. Writing
an inventory item group is a *full replace* -- every variation SKU, the
cover photo and the listing-level aspects all have to be in the payload, and
a SKU left out is a card taken off sale immediately. ``refresh_listing``
already gets all of that right, including re-sending cards whose own write
failed and resolving the cover from what is recorded rather than guessing.
A second implementation here would be a second place for those rules to
drift, and the way that fails is silent: a shorter listing, discovered by
counting the dropdown by hand.

What that means in practice:

* It cannot move stock or money. Quantity is re-sent as what eBay is already
  known to hold and no price is sent at all.
* It re-sends pictures and item specifics as well as the title, because
  that is what a refresh is. A listing carrying an image below eBay's
  500-pixel minimum will be refused -- run ``scripts/check_images.py --all``
  first if that is a possibility.
* It publishes the group afterwards, which is a no-op on a listing already
  on sale.
* **Singles are skipped.** A single listing's title is built from the card
  itself, not from the variation template, so there is nothing here to
  apply. They are reported rather than silently passed over.

Reports by default and writes nothing. Pass ``--yes`` to apply.

    python scripts/retitle_listings.py
    python scripts/retitle_listings.py --listing 227521446958
    python scripts/retitle_listings.py --language CS --yes

On the NAS, which is where the databases and the eBay connection live:

    cd /volume1/docker/tcg-middleware
    docker compose exec tcg-middleware python scripts/retitle_listings.py --yes
"""

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app import deps  # noqa: E402
from tcg_engine.batches import (  # noqa: E402
    group_language,
    group_title_fields,
    render_variation_title,
    title_template_for,
)
from tcg_engine.plans import is_single  # noqa: E402
from tcg_engine.push import (  # noqa: E402
    PushError,
    inventory_group_key,
    refresh_listing,
)


def planned_title(db, settings, listing):
    """
    (title, language, trimmed, cards) for one listing, or None if it has none.

    Rendered from the cards the mirror says the listing holds, which is the
    same source ``refresh_listing`` rebuilds the group from -- so this is a
    preview of the write rather than an independent guess at it.
    """
    cards = db.get_cards_for_listing(str(listing["ebay_parent_id"]))
    if not cards:
        return None
    group_key = str(listing["group_key"])
    set_name, _, condition = group_key.partition("|")
    language = group_language(cards)
    fields = group_title_fields(cards)
    title, trimmed = render_variation_title(
        set_name,
        condition=condition,
        template=title_template_for(settings, language),
        set_code=fields["set_code"],
        year=fields["year"],
    )
    return title, language, trimmed, cards


def live_title(adapter, listing, group_key):
    """
    The title eBay currently holds, or None when it could not be read.

    Read back rather than assumed. None is reported as unknown and never as
    "unchanged" -- an unreadable answer is not evidence that the listing
    already says what we think it says.
    """
    key = (
        listing.get("inventory_item_group_key")
        or inventory_group_key(group_key)
    )
    try:
        group = adapter.get_group(key) or {}
    except Exception:  # noqa: BLE001 - a failed read is not a failure
        return None
    return str(group.get("title") or "") or None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Re-title live variation listings from the current templates.",
    )
    parser.add_argument(
        "--listing", action="append", default=[],
        help="Only this eBay item number. Repeatable.",
    )
    parser.add_argument(
        "--language", default="",
        help=(
            "Only listings whose cards are in this language, as the export "
            "spells it (e.g. CS, EN). Case-insensitive."
        ),
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Actually re-send. Without this the script only reports.",
    )
    args = parser.parse_args(argv)

    db = deps.owner_inventory()
    owner = deps._owner_scope()
    settings = db.get_listing_settings(user_id=owner)

    listings = [
        row for row in db.get_managed_listings()
        if str(row.get("ebay_parent_id") or "").strip()
    ]
    if args.listing:
        wanted = {str(value).strip() for value in args.listing}
        listings = [
            row for row in listings
            if str(row["ebay_parent_id"]).strip() in wanted
        ]
        found = {str(row["ebay_parent_id"]).strip() for row in listings}
        for missing in sorted(wanted - found):
            print(f"  #{missing}: not a listing this application manages")

    if not listings:
        print(
            "No managed listings to re-title. A listing created through "
            "File Exchange and never migrated is not visible to this API, "
            "and a listing the mirror has not linked yet needs a Module B "
            "sync first."
        )
        return 0

    client = deps.get_ebay_client(owner) if args.yes else None
    adapter = None
    if args.yes:
        if client is None or not client.oauth.is_connected():
            print("Connect the eBay account first: a re-title acts as the seller.")
            return 2
        adapter = deps.InventoryApiAdapter(client)
    else:
        # A read-only preview still benefits from eBay's current title, but
        # must not require a connection to run at all.
        client = deps.get_ebay_client(owner)
        if client is not None and client.oauth.is_connected():
            adapter = deps.InventoryApiAdapter(client)

    singles, changed, unchanged, empty = [], [], [], []

    for listing in sorted(listings, key=lambda r: str(r.get("group_key") or "")):
        group_key = str(listing["group_key"])
        item_id = str(listing["ebay_parent_id"])
        if is_single(group_key) or not group_key:
            singles.append(item_id)
            continue
        planned = planned_title(db, settings, listing)
        if planned is None:
            empty.append(item_id)
            continue
        title, language, trimmed, cards = planned
        if args.language and language.upper() != args.language.strip().upper():
            continue
        current = live_title(adapter, listing, group_key) if adapter else None
        row = {
            "item_id": item_id, "group_key": group_key, "title": title,
            "language": language, "trimmed": trimmed, "current": current,
            "cards": len(cards),
        }
        (unchanged if current == title else changed).append(row)

    def describe(row):
        print(f"\n  #{row['item_id']}  {row['group_key']}  "
              f"({row['cards']} card(s), language {row['language'] or 'mixed'})")
        print(f"     now : {row['current'] if row['current'] else '(could not be read from eBay)'}")
        print(f"     new : {row['title']}  [{len(row['title'])} chars]")
        if row["trimmed"]:
            print("     WARNING: the set name had to be cut short to fit "
                  "eBay's 80 characters.")

    if changed:
        print(f"{len(changed)} listing(s) would be re-titled:")
        for row in changed:
            describe(row)
    if unchanged:
        print(f"\n{len(unchanged)} listing(s) already carry the new title.")
    if singles:
        print(f"\n{len(singles)} single listing(s) skipped -- a single is "
              f"titled from its card, not from the variation template: "
              + ", ".join(f"#{i}" for i in singles))
    if empty:
        print(f"\n{len(empty)} listing(s) have no cards linked in the mirror, "
              f"so there is nothing to render a title from. Run a Module B "
              f"sync: " + ", ".join(f"#{i}" for i in empty))

    if not args.yes:
        print("\nNothing was sent to eBay. Pass --yes to apply.")
        return 0
    if not changed:
        print("\nNothing to do.")
        return 0

    applied = failed = 0
    for row in changed:
        print(f"\n#{row['item_id']}: re-sending...")
        try:
            with db.session():
                result = refresh_listing(
                    db, adapter, row["item_id"], user_id=owner,
                    log=lambda level, message: print(f"   {level}: {message}"),
                )
        except PushError as exc:
            failed += 1
            print(f"   ERROR: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one listing must not sink the rest
            failed += 1
            print(f"   ERROR: {exc}")
            continue
        if result.get("failed"):
            failed += 1
        else:
            applied += 1

    print(f"\n{applied} listing(s) re-sent, {failed} with problems.")
    print(
        "eBay's own view of a listing can lag a few minutes, so a title that "
        "has not changed on the page yet is not evidence it did not take. "
        "Re-run this without --yes to read the titles back."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
