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
    rows = []

    for card in cards:
        sku = sku_for(card)
        present = sku in on_ebay
        # eBay's own figure, which is what decides whether a buyer sees the
        # variation at all -- eBay hides one with no stock from the dropdown.
        live_qty = int(card.get("last_known_qty") or 0)
        ours = int(card.get("quantity") or 0)
        try:
            offers = adapter.offer_ids_for(sku)
        except Exception as exc:  # noqa: BLE001
            offers = None
            note = f"unknown ({exc})"
        else:
            note = offers[0] if offers else "none"

        if not present:
            if offers:
                missing_with_offer.append(sku)
            elif offers is not None:
                missing_without_offer.append(sku)
        elif offers is not None and not offers:
            no_offer_but_present.append(sku)
        if present and live_qty <= 0:
            out_of_stock.append(sku)

        wrong = (not present) or (offers is not None and not offers) or live_qty <= 0
        rows.append((wrong, sku, present, note, live_qty, ours, card))

    shown = [r for r in rows if r[0] or args.all]
    if shown:
        print(f"  {'SKU':16} {'in group':9} {'offer':14} {'eBay qty':9} "
              f"{'ours':5} card")
        print("  " + "-" * 78)
        for _, sku, present, note, live_qty, ours, card in shown:
            print(f"  {sku:16} {'yes' if present else 'NO':9} {str(note):14} "
                  f"{live_qty:<9} {ours:<5} {card.get('product_name')} "
                  f"{card.get('card_number') or ''}")
        if not args.all:
            print(f"  ... {len(rows) - len(shown)} more card(s) are in the "
                  f"group, have an offer and have stock (--all to list them)")
    print()

    with_stock = len(rows) - len(out_of_stock)
    print(f"  {with_stock} of {len(rows)} variation(s) have stock at eBay.")
    if out_of_stock and with_stock <= 1:
        print()
        print("  That is almost certainly what you were looking at. eBay "
              "hides a variation")
        print("  with no stock from the dropdown, so a listing whose "
              "variation set is")
        print("  complete can still show only the one card a buyer can "
              "actually buy.")
        print("  Nothing is missing from the listing -- the others are at "
              "quantity zero.")
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
        print("  Every card our mirror links to this listing is in eBay's "
              "group, so")
        print("  the variation set is intact and there is nothing for "
              "Refresh to restore.")
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
