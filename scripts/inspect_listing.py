#!/usr/bin/env python3
"""
Compare a live eBay listing against what we think is in it, and say what will
restore it.

Written for one situation. A push built from a partial plan used to replace
the whole inventory item group, which takes every variation it did not send
off sale -- so a one-card plan could reduce a 35-card listing to one card.
Three states look identical from outside the listing and need different
remedies, so this reports them separately, per card:

* **Missing from the group.** The variation is gone. Refresh rebuilds the
  group from every card our mirror links to the listing, so this is the case
  Refresh fixes.
* **In the group, offer live, quantity zero.** The variation exists and
  nobody can buy it. eBay hides a zero-quantity variation from the dropdown,
  so this looks like a listing that lost its variations and is not one.
  Refresh cannot fix it: quantity lives on the offer, and Refresh writes the
  inventory items and the group and never touches offers.
* **In the group, no offer or an unpublished one.** A half-built variation,
  which needs its offer recreated rather than restocked.

Everything is read from eBay. Our mirror's `last_known_qty` is the one number
that cannot be trusted here -- the push never updated it for the cards it did
not touch, so it still believes they hold stock.

Reads only. It writes nothing, to our database or to eBay.

    python scripts/inspect_listing.py 227521446958
    python scripts/inspect_listing.py 227521446958 --limit 10
    python scripts/inspect_listing.py 227521446958 --pictures --all

On the NAS, which is where the eBay connection lives:

    cd /volume1/docker/tcg-middleware
    docker compose exec tcg-middleware python scripts/inspect_listing.py \\
        227521446958 --limit 10

There is no bulk read in the Inventory API for any of this, so it is one call
per card -- a 123-card listing takes a couple of minutes. Rows are therefore
printed as they arrive rather than collected and tabulated at the end: a slow
script that prints nothing is indistinguishable from a hung one. `--limit`
samples the first N cards, which is usually enough to establish the pattern.
`--pictures` reads each inventory item too, which doubles the calls, so it is
off by default.
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


def read_offer(client, sku):
    """
    eBay's own view of one SKU's offer: (quantity, note, offers).

    Quantity is None when it could not be established, and that is **not**
    the same fact as zero. The first version of this script treated the two
    alike: it only counted a card as out of stock when the quantity was a
    number and that number was zero, so every card it failed to read was
    scored as healthy, printed nothing, and was counted into "123 of 123
    variation(s) have stock at eBay" -- on a listing that did not have it.
    An unreadable card is now its own outcome and says why.

    Two ways the quantity goes missing, which the note distinguishes:
    the call failed, or the call succeeded and the offer carried no
    ``availableQuantity`` field at all.
    """
    from ebay_client import inventory  # noqa: PLC0415

    try:
        offers = inventory.get_offers(client.seller, sku)
    except Exception as exc:  # noqa: BLE001 - report, never traceback
        return None, f"unreadable: {exc}", None
    if not offers:
        return None, "no offer", offers
    offer = offers[0]
    note = str(offer.get("offerId") or "?")
    status = str(offer.get("status") or "").upper()
    if status and status != "PUBLISHED":
        note += " (" + status.lower() + ")"
    if "availableQuantity" not in offer:
        # eBay answered, and its answer contained no quantity. Reporting that
        # as stock is how the listing got a clean bill of health.
        return None, note + " (no qty field)", offers
    raw = offer.get("availableQuantity")
    return (int(raw) if raw is not None else None), note, offers


def read_pictures(client, sku):
    """
    How many photos the inventory item carries, or None if unreadable.

    "Every variation has 0 photos" is a statement about the inventory items,
    not about the group -- the group only says pictures vary by Card. None
    here means the question was not answered, and is reported as unknown
    rather than folded in with either answer.
    """
    from ebay_client import inventory  # noqa: PLC0415

    getter = getattr(inventory, "get_inventory_item", None)
    if not callable(getter):
        return None
    try:
        item = getter(client.seller, sku) or {}
    except Exception:  # noqa: BLE001 - reported as unknown, not as zero
        return None
    return len(((item.get("product") or {}).get("imageUrls") or []))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Compare a live eBay listing with our mirror, and say what will "
            "restore it."
        ),
    )
    parser.add_argument("item_id", help="The eBay item number")
    parser.add_argument(
        "--all", action="store_true",
        help="Print every card. By default only the ones with something "
             "wrong are listed and the rest are counted.",
    )
    parser.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="Only check the first N cards. Each card is an eBay call, and a "
             "sample is usually enough to establish the pattern.",
    )
    parser.add_argument(
        "--pictures", action="store_true",
        help="Also count each inventory item's photos. This doubles the "
             "number of eBay calls.",
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

    checked = cards[:args.limit] if args.limit > 0 else cards
    print(f"  reading {len(checked)} card(s) from eBay, "
          f"{len(checked) * (2 if args.pictures else 1)} call(s)"
          + (f", sampled from {len(cards)}"
             if len(checked) < len(cards) else "")
          + " -- rows appear as they arrive")
    print()

    header = (f"  {'SKU':16} {'in group':9} {'offer':34} {'eBay qty':9} "
              f"{'ours':5}")
    if args.pictures:
        header += f"{'pics':5} "
    print(header + "card")
    print("  " + "-" * (104 if args.pictures else 99))

    missing_with_offer = []
    missing_without_offer = []
    no_offer_but_present = []
    out_of_stock = []
    unknown_qty = []
    unreadable = []
    no_pictures = []
    unknown_pictures = []
    confirmed_stock = 0
    right = 0

    for index, card in enumerate(checked, start=1):
        sku = sku_for(card)
        present = sku in on_ebay
        ours = int(card.get("quantity") or 0)
        live_qty, note, offers = read_offer(client, sku)
        pics = read_pictures(client, sku) if args.pictures else None

        if not present:
            if offers:
                missing_with_offer.append(sku)
            elif offers is not None:
                missing_without_offer.append(sku)
        elif offers is not None and not offers:
            no_offer_but_present.append(sku)
        if present and live_qty is not None and live_qty <= 0:
            out_of_stock.append(sku)
        if present and live_qty is not None and live_qty > 0:
            confirmed_stock += 1
        # Not knowing the quantity is a finding, not a pass. Scoring it as a
        # pass is what let this report 123 of 123 in stock while the listing
        # was not up to date.
        if present and live_qty is None and offers:
            unknown_qty.append(sku)
        if offers is None:
            # The call itself failed, so this card contributes nothing to any
            # other count -- including the missing-from-group ones, which we
            # also cannot judge without knowing whether an offer exists.
            unreadable.append(sku)
        if present and pics == 0:
            no_pictures.append(sku)
        if args.pictures and present and pics is None:
            unknown_pictures.append(sku)

        wrong = (
            (not present)
            or (offers is not None and not offers)
            or offers is None
            or live_qty is None
            or live_qty <= 0
            or pics == 0
            or (args.pictures and pics is None)
        )
        if not wrong:
            right += 1
        if wrong or args.all:
            line = (
                f"  {sku:16} {'yes' if present else 'NO':9} {note[:34]:34} "
                f"{('?' if live_qty is None else str(live_qty)):<9} "
                f"{ours:<5}"
            )
            if args.pictures:
                line += f"{('?' if pics is None else str(pics)):<5} "
            line += (f"{card.get('product_name')} "
                     f"{card.get('card_number') or ''}")
            print(line)
        # Flushed per row, not collected for a table at the end: at one
        # HTTP call per card this runs for minutes, and silence for minutes
        # is why the first version of this looked like it had hung.
        sys.stdout.flush()
        if index % 25 == 0 and index < len(checked):
            print(f"  ... {index}/{len(checked)} read", flush=True)

    print()
    if not args.all and right:
        print(f"  {right} of {len(checked)} card(s) checked are in the group, "
              f"have a published")
        print(f"  offer and have stock"
              + (" and a picture" if args.pictures else "")
              + " (--all lists them).")

    # Every count below is of something eBay actually told us. The ones it
    # did not answer are reported as unanswered rather than divided between
    # the other columns.
    with_stock = confirmed_stock
    print(f"  {with_stock} of {len(checked)} are confirmed in stock at eBay.")
    if unreadable:
        print(f"  {len(unreadable)} could not be read from eBay at all, so "
              f"nothing is known")
        print(f"  about them -- the reason is on each row above.")
    if out_of_stock:
        print(f"  {len(out_of_stock)} are confirmed at quantity zero.")
    if unknown_qty:
        print(f"  {len(unknown_qty)} have an offer whose quantity eBay did "
              f"not report.")
    if no_pictures:
        print(f"  {len(no_pictures)} of {len(checked)} have no picture on "
              f"their inventory item.")
    if unknown_pictures:
        print(f"  {len(unknown_pictures)} inventory item(s) could not be "
              f"read for pictures.")
    if not args.pictures:
        print("  Pictures were not checked at all; add --pictures to count "
              "them.")
    print()

    if unknown_qty and not out_of_stock:
        print("  Read the quantity column before drawing any conclusion: "
              "eBay returned an")
        print("  offer for these and no quantity in it, so this run cannot "
              "say whether they")
        print("  are in stock. It is not evidence that they are. The offer "
              "note on each row")
        print("  above says which of the two happened -- the call failing, "
              "or the call")
        print("  succeeding with no availableQuantity field in the answer.")
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
        # The guard is the count of cards that came back clean, not a list of
        # the ways they can come back dirty. Enumerating the failure lists
        # here missed the case where every single read failed: none of the
        # lists filled, and the report declared the whole listing healthy.
        if right < len(checked):
            print("  The variation set itself needs nothing from Refresh -- "
                  "every card")
            print("  our mirror links to this listing is already in eBay's "
                  "group. That is the")
            print("  only thing this run establishes; the stock and the "
                  "pictures are separate")
            print("  facts and are listed above.")
            return 0
        print(f"  All {len(checked)} card(s) checked are in the group and "
              f"have a published offer")
        print(f"  holding stock"
              + (", with at least one picture" if args.pictures
                 else " (pictures not checked)")
              + ".")
        if len(checked) < len(cards):
            print(f"  That is a sample of {len(checked)} from {len(cards)}; "
                  f"drop --limit to check them all.")
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
