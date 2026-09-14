#!/usr/bin/env python3
"""
Compare a live eBay listing against what we think is in it, and say whether
pressing Refresh will restore it.

Written for one situation. A push built from a partial plan used to replace
the whole inventory item group, which takes every variation it did not send
off sale -- so a one-card plan could reduce a 35-card listing to one card.
Refresh rebuilds the group from every card our mirror links to the listing,
which is the remedy, but it **touches no offers**: it writes the inventory
items and the group and nothing else.

That is the distinction this answers, per card:

* **Missing from the group but its offer is still there** -- Refresh puts it
  back, because the SKU returning to `variantSKUs` is all that was needed.
* **Missing from the group and its offer is gone** -- Refresh is not enough
  on its own. The SKU goes back into the group with nothing behind it, and
  the variation needs its offer recreated.

Reads only. It writes nothing, to our database or to eBay.

    python scripts/inspect_listing.py 227521446958

On the NAS, which is where the eBay connection lives:

    cd /volume1/docker/tcg-middleware
    docker compose exec tcg-middleware python scripts/inspect_listing.py \\
        227521446958
"""

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app import deps  # noqa: E402
from tcg_engine.push import inventory_group_key  # noqa: E402


def sku_for(card):
    return str(card.get("custom_label") or card["manifest_id"]).strip()


def read_offers(client, sku):
    """Every offer eBay holds for one SKU, with its quantity and status."""
    from ebay_client import inventory  # noqa: PLC0415

    return inventory.get_offers(client.seller, sku)


def read_item(client, sku):
    """
    The inventory item, which is where a variation's pictures live.

    "Every variation has 0 photos" is a statement about these, not about the
    group -- the group only says that pictures vary by the Card aspect.
    """
    from ebay_client import inventory  # noqa: PLC0415

    getter = getattr(inventory, "get_inventory_item", None)
    if not callable(getter):
        return None
    return getter(client.seller, sku)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Compare a live eBay listing with our mirror, and say whether "
            "Refresh will restore it."
        ),
    )
    parser.add_argument("item_id", help="The eBay item number")
    parser.add_argument(
        "--all", action="store_true",
        help="Print every card. By default only the ones with something "
             "wrong are listed and the rest are counted.",
    )
    parser.add_argument(
        "--user-id", type=int, default=None,
        help="Whose eBay connection and inventory to use. Defaults to the "
             "deployment owner.",
    )
    args = parser.parse_args(argv)

    user_id = args.user_id if args.user_id is not None else deps._owner_scope()
    inv = deps.inventory_for(user_id)
    item_id = str(args.item_id).strip()

    cards = inv.get_cards_for_listing(item_id)
    if not cards:
        print(f"Our mirror links no cards to listing #{item_id}.")
        print("Run a Module B sync first, or check the item number.")
        return 1

    managed = inv.get_managed_listing_by_parent(item_id)
    print()
    print(f"Listing #{item_id}")
    print(f"  our mirror links {len(cards)} card(s) to it")
    if managed is None:
        print("  not managed through the eBay API, so Refresh cannot reach "
              "it at all -- this listing was made on File Exchange.")
        return 1
    print(f"  group key        {managed.get('group_key')}")

    client = deps.get_ebay_client(user_id)
    if client is None or not client.oauth.is_connected():
        print()
        print("eBay is not connected, so only our side can be shown. "
              "Connect the account and run this again to compare.")
        for card in cards:
            print(f"    {sku_for(card):16} {card.get('product_name')}")
        return 2

    from app.ebay_push import InventoryApiAdapter  # noqa: PLC0415

    adapter = InventoryApiAdapter(client)
    ebay_group_key = (
        managed.get("inventory_item_group_key")
        or inventory_group_key(str(managed.get("group_key") or ""))
    )

    try:
        live = adapter.get_group(ebay_group_key) or {}
    except Exception as exc:  # noqa: BLE001 - report rather than traceback
        print()
        print(f"Could not read the group back from eBay: {exc}")
        return 2

    on_ebay = {str(s).strip() for s in (live.get("variantSKUs") or [])}
    print(f"  eBay's group holds {len(on_ebay)} variation(s)")
    print()

    missing_with_offer = []
    missing_without_offer = []
    no_offer_but_present = []
    out_of_stock = []
    no_photos = []
    rows = []

    for card in cards:
        sku = sku_for(card)
        present = sku in on_ebay
        # eBay's own figure, which is what decides whether a buyer sees the
        # variation at all -- eBay hides one with no stock from the dropdown.
        ours = int(card.get("quantity") or 0)
        # eBay's own figures, read now. Our mirror's last_known_qty is the
        # one number that cannot be trusted here: the push never updated it
        # for the cards it did not touch, so it still believes they hold
        # stock.
        live_qty = None
        item_photos = None
        try:
            offers = read_offers(client, sku)
        except Exception as exc:  # noqa: BLE001
            offers = None
            note = f"unknown ({exc})"
        else:
            if offers:
                offer = offers[0]
                note = str(offer.get("offerId") or "?")
                raw = offer.get("availableQuantity")
                live_qty = int(raw) if raw is not None else None
                if str(offer.get("status") or "").upper() != "PUBLISHED":
                    note += " (" + str(offer.get("status") or "?").lower() + ")"
            else:
                note = "none"
        try:
            item = read_item(client, sku)
        except Exception:  # noqa: BLE001 - the picture count is advisory
            item_photos = None
        else:
            item_photos = len(
                ((item or {}).get("product") or {}).get("imageUrls") or []
            )

        if not present:
            if offers:
                missing_with_offer.append(sku)
            elif offers is not None:
                missing_without_offer.append(sku)
        elif offers is not None and not offers:
            no_offer_but_present.append(sku)
        if present and live_qty is not None and live_qty <= 0:
            out_of_stock.append(sku)
        if present and item_photos == 0:
            no_photos.append(sku)

        wrong = (
            (not present)
            or (offers is not None and not offers)
            or (live_qty is not None and live_qty <= 0)
            or item_photos == 0
        )
        rows.append(
            (wrong, sku, present, note, live_qty, ours, item_photos, card)
        )

    shown = [r for r in rows if r[0] or args.all]
    if shown:
        print(f"  {'SKU':16} {'in group':9} {'offer':22} {'eBay qty':9} "
              f"{'ours':5} {'pics':5} card")
        print("  " + "-" * 92)
        for _, sku, present, note, live_qty, ours, pics, card in shown:
            qty = "?" if live_qty is None else str(live_qty)
            pic = "?" if pics is None else str(pics)
            print(f"  {sku:16} {'yes' if present else 'NO':9} {str(note):22} "
                  f"{qty:<9} {ours:<5} {pic:<5} {card.get('product_name')} "
                  f"{card.get('card_number') or ''}")
        if not args.all:
            print(f"  ... {len(rows) - len(shown)} more card(s) are in the "
                  f"group, have an offer and have stock (--all to list them)")
    print()

    with_stock = len(rows) - len(out_of_stock)
    print(f"  {with_stock} of {len(rows)} variation(s) have stock at eBay.")
    if no_photos:
        print(f"  {len(no_photos)} of {len(rows)} have no picture on their "
              f"inventory item.")
    print()

    if out_of_stock and with_stock <= 1:
        print("  The variation set is intact; what is missing is the stock. "
              "eBay hides a")
        print("  variation with no quantity from the dropdown, so this looks "
              "like a listing")
        print("  that lost its variations and is not one.")
        print()
        print("  Quantity lives on the **offer**, and Refresh writes the "
              "inventory items and")
        print("  the group and never touches offers -- so Refresh alone "
              "cannot restore it.")
        print("  Say so and the repair can be taught to re-send quantities "
              "from the")
        print("  catalogue; it is a write to live stock, so it is not being "
              "guessed at here.")
        print()
        print("  Note our own mirror still believes these hold stock, since "
              "the push never")
        print("  updated it for cards it did not touch. A Module B sync "
              "would correct that")
        print("  -- and would also be the moment the mirror stops being able "
              "to tell you what")
        print("  the quantities should be. Read the 'ours' column first.")
    elif out_of_stock:
        print(f"  {len(out_of_stock)} are at zero, so a buyer does not see "
              f"those in the dropdown.")

    if no_offer_but_present:
        print(f"  {len(no_offer_but_present)} card(s) are in the group but "
              f"have no offer at eBay, which is a half-built variation:")
        for sku in no_offer_but_present[:10]:
            print(f"    {sku}")
        print()

    if not missing_with_offer and not missing_without_offer:
        # The variation set being intact is not the same as the listing
        # being well: the stock and the pictures are separate facts, and
        # saying 'nothing to restore' over the top of them would be the
        # same false reassurance the success log gave.
        if out_of_stock or no_photos:
            print("  The variation set itself needs nothing from Refresh -- every card")
            print("  our mirror links to this listing is already in eBay's group.")
            return 0
        print("  Every card is in the group, has a live offer, has stock and has a")
        print("  picture. Nothing to restore.")
        return 0

    if missing_with_offer:
        print(f"{len(missing_with_offer)} card(s) are missing from the group "
              f"but still have an offer at eBay.")
        print("  Pressing Refresh on this listing restores these: putting the "
              "SKU back into")
        print("  the group is all that was needed.")
        print()
    if missing_without_offer:
        print(f"{len(missing_without_offer)} card(s) are missing from the "
              f"group AND have no offer at eBay:")
        for sku in missing_without_offer:
            print(f"    {sku}")
        print("  Refresh alone will not bring these back -- it writes items "
              "and the group,")
        print("  never offers, so the SKU would return to the group with "
              "nothing behind it.")
        print("  These need their offers recreated. Say so and the refresh "
              "path can be")
        print("  taught to do it; there is no safe way to guess it from here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
