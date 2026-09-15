#!/usr/bin/env python3
"""
Put a variation listing's ended offers back on sale.

The situation this repairs. A push built from a partial plan replaced the
whole inventory item group with the one card the plan touched, and eBay
responded by ending every variation the group no longer named. Ending a
variation does not destroy anything: the offer record survives, with its
quantity, its price and its pictures, and its status goes back to
``UNPUBLISHED``. Pressing Refresh afterwards rebuilt the group, so the group
names all of its cards again.

What Refresh cannot do is publish them. ``_push_group`` calls
``publish_group`` only when the listing is *not* already published, which is
correct for its own purposes -- republishing on every push would be
gratuitous -- but it leaves no path back for a listing that is published and
whose offers are not. That is the exact state this fixes, and the diagnostic
names it: every row reads ``(unpublished)`` with a quantity that matches the
catalogue.

So this does not re-send quantities, prices or pictures. Re-sending data that
is already correct is a write with nothing to gain and a listing to lose.
There is one call that changes anything here:
``publishOfferByInventoryItemGroup``, which publishes every offer in the
group as one variation listing.

Two things about that call are worth knowing before running this:

* It is **all or nothing**. One invalid offer fails the whole group, and it
  fails at publish time rather than at approval time. A failure should leave
  the listing as it is, but it will name the card that blocked it.
* It returns a listing id. It should be the id the listing already has,
  because the group is already associated with it. If a *different* id comes
  back, eBay has made a second listing, which is the one outcome here that
  would need undoing -- so the id is checked and reported either way.

Reports by default and writes nothing. Pass ``--yes`` to publish.

    python scripts/republish_listing.py 227521446958
    python scripts/republish_listing.py 227521446958 --yes

On the NAS, which is where the eBay connection lives:

    cd /volume1/docker/tcg-middleware
    docker compose exec tcg-middleware python scripts/republish_listing.py \\
        227521446958 --yes
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


def offer_state(client, sku):
    """
    (status, quantity, offer_id) as eBay reports them now, or a reason.

    Status is None when the question was not answered. That is not the same
    as an offer being absent, and the two must not be added together: one
    means "eBay says there is nothing here", the other means "we do not
    know".
    """
    from ebay_client import inventory  # noqa: PLC0415

    try:
        offers = inventory.get_offers(client.seller, sku)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return None, None, f"unreadable: {exc}"
    if not offers:
        return "NONE", None, None
    offer = offers[0]
    status = str(offer.get("status") or "").upper() or "UNKNOWN"
    raw = offer.get("availableQuantity")
    qty = int(raw) if raw is not None else None
    return status, qty, str(offer.get("offerId") or "")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Put a variation listing's ended offers back on sale.",
    )
    parser.add_argument("item_id", help="The eBay item number")
    parser.add_argument(
        "--yes", action="store_true",
        help="Actually publish. Without this the script only reports.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Publish even if the audit found cards that can fail the whole "
             "group. Only meaningful with --yes.",
    )
    parser.add_argument(
        "--no-audit", action="store_true",
        help="Skip reading every offer's status first. The audit is one eBay "
             "call per card and is what establishes that the quantities are "
             "intact, so skipping it means publishing without knowing that.",
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
        return 1
    managed = inv.get_managed_listing_by_parent(item_id)
    if managed is None:
        print(f"Listing #{item_id} is not managed through the eBay API, so "
              f"there is no group to publish.")
        return 1

    client = deps.get_ebay_client(user_id)
    if client is None or not client.oauth.is_connected():
        print("eBay is not connected.")
        return 2

    from app.ebay_push import InventoryApiAdapter  # noqa: PLC0415

    adapter = InventoryApiAdapter(client)
    group_key = str(managed.get("group_key") or "")
    ebay_group_key = (
        managed.get("inventory_item_group_key")
        or inventory_group_key(group_key)
    )

    print()
    print(f"Listing #{item_id}")
    print(f"  group key        {group_key}")
    print(f"  group at eBay    {ebay_group_key}")

    try:
        live = adapter.get_group(ebay_group_key) or {}
    except Exception as exc:  # noqa: BLE001
        print(f"  could not read the group back from eBay: {exc}")
        return 2
    on_ebay = [str(s).strip() for s in (live.get("variantSKUs") or [])]
    print(f"  the group names  {len(on_ebay)} variation(s)")
    print(f"  our mirror links {len(cards)} card(s)")

    ours = {sku_for(card): card for card in cards}
    not_in_group = sorted(set(ours) - set(on_ebay))
    if not_in_group:
        print()
        print(f"  {len(not_in_group)} card(s) our mirror links to this "
              f"listing are not in the group.")
        print("  Publishing will not add them. Press Refresh on the listing "
              "first, which")
        print("  rebuilds the group, then run this again.")
        for sku in not_in_group[:10]:
            print(f"    {sku}")
        return 1

    published = unpublished = other = unknown = 0
    qty_matches = qty_differs = qty_unknown = 0
    blockers = []

    if args.no_audit:
        print()
        print("  Offer statuses were not read (--no-audit), so what follows "
              "publishes")
        print("  without having established what state the offers are in.")
    else:
        print()
        print(f"  Reading {len(on_ebay)} offer(s) from eBay, one call each. "
              f"Rows appear as")
        print(f"  they arrive; only the ones worth seeing are printed.")
        print()
        for index, sku in enumerate(on_ebay, start=1):
            status, qty, note = offer_state(client, sku)
            card = ours.get(sku) or {}
            want = int(card.get("quantity") or 0)

            if status is None:
                unknown += 1
                blockers.append((sku, note or "unreadable"))
                print(f"    {sku:14} {note}")
            elif status == "PUBLISHED":
                published += 1
            elif status == "UNPUBLISHED":
                unpublished += 1
            elif status == "NONE":
                other += 1
                blockers.append((sku, "no offer at eBay"))
                print(f"    {sku:14} no offer at eBay")
            else:
                other += 1
                blockers.append((sku, status.lower()))
                print(f"    {sku:14} {status.lower()}")

            if qty is None:
                qty_unknown += 1
            elif qty == want:
                qty_matches += 1
            else:
                qty_differs += 1
                print(f"    {sku:14} quantity {qty} at eBay, {want} in the "
                      f"catalogue")
            sys.stdout.flush()
            if index % 25 == 0 and index < len(on_ebay):
                print(f"    ... {index}/{len(on_ebay)} read", flush=True)

        print()
        print(f"  {published} published, {unpublished} unpublished"
              + (f", {other} neither" if other else "")
              + (f", {unknown} unreadable" if unknown else "") + ".")
        print(f"  quantities: {qty_matches} match the catalogue"
              + (f", {qty_differs} differ" if qty_differs else "")
              + (f", {qty_unknown} not reported by eBay" if qty_unknown
                 else "") + ".")

        if unpublished == 0:
            print()
            print("  Nothing here is unpublished, so this is not the repair "
                  "this listing needs.")
            print("  Publishing would change nothing. Stopping.")
            return 0
        if blockers:
            print()
            print(f"  {len(blockers)} card(s) are neither published nor "
                  f"unpublished. Publishing is")
            print("  all-or-nothing, so any one of these can fail the whole "
                  "group:")
            for sku, why in blockers[:10]:
                print(f"    {sku:14} {why}")
            if args.yes and not args.force:
                # --yes was given before this was known. Consent to the
                # repair is not consent to a condition that was discovered
                # afterwards and makes the repair likely to fail.
                print()
                print("  Stopping rather than publishing into that. Fix "
                      "those cards, or pass")
                print("  --force as well to publish anyway and let eBay "
                      "decide.")
                return 1
        if qty_differs:
            print()
            print("  Some quantities at eBay do not match the catalogue. "
                  "Publishing does not")
            print("  change quantities -- it puts the offers back on sale "
                  "holding whatever")
            print("  they already hold. Fix those separately if they matter.")

    if not args.yes:
        print()
        print(f"  Nothing was written. Publishing would put {unpublished} "
              f"ended variation(s)")
        print(f"  back on sale on listing #{item_id}, at the quantities and "
              f"prices their")
        print(f"  offers already hold. Re-run with --yes to do it.")
        return 0

    print()
    print(f"  Publishing group {ebay_group_key} ...")
    sys.stdout.flush()
    try:
        listing_id = adapter.publish_group(ebay_group_key)
    except Exception as exc:  # noqa: BLE001
        print(f"  eBay refused the publish: {exc}")
        print()
        print("  Publishing is all-or-nothing, so this should have changed "
              "nothing. The")
        print("  message above normally names the offer that blocked it; fix "
              "that card and")
        print("  run this again.")
        return 2

    print(f"  eBay returned listing id {listing_id}")
    if str(listing_id) != item_id:
        print()
        print(f"  That is NOT the listing this group belonged to (#{item_id}).")
        print("  eBay has published the group as a separate listing, which "
              "means there may")
        print(f"  now be two. Check both #{item_id} and #{listing_id} before "
              f"doing anything")
        print("  else. Our mirror is being pointed at the new one, because "
              "that is where")
        print("  eBay says the group now lives -- leaving it on the old id "
              "would send every")
        print("  future write to a listing eBay no longer associates with "
              "these offers.")
        inv.upsert_managed_listing(
            group_key, ebay_parent_id=str(listing_id), pushed=True
        )
        landed_elsewhere = True
    else:
        landed_elsewhere = False

    # Read back, because the publish returning an id is not the same as the
    # variations being on sale. A sample is enough to tell which happened.
    print()
    print("  Confirming, by reading the offers back ...")
    sample = on_ebay[:10]
    now_published = still_not = unconfirmed = 0
    for sku in sample:
        status, _qty, note = offer_state(client, sku)
        if status == "PUBLISHED":
            now_published += 1
        elif status is None:
            unconfirmed += 1
            print(f"    {sku:14} {note}")
        else:
            still_not += 1
            print(f"    {sku:14} {str(status).lower()}")
        sys.stdout.flush()

    print()
    print(f"  {now_published} of {len(sample)} sampled offer(s) are now "
          f"PUBLISHED"
          + (f", {still_not} are not" if still_not else "")
          + (f", {unconfirmed} could not be read" if unconfirmed else "")
          + ".")
    if now_published == len(sample) and not landed_elsewhere:
        print(f"  The variations are back on sale. eBay's own listing page "
              f"lags the API by")
        print(f"  minutes, so #{item_id} may still look wrong for a while -- "
              f"that is the page")
        print(f"  catching up, not the write failing.")
        return 0
    if landed_elsewhere:
        print(f"  Those offers are on sale under #{listing_id}, not under "
              f"#{item_id}. That is")
        print(f"  the part that needs your attention: this run did not "
              f"restore the listing")
        print(f"  you asked about, it published the same cards somewhere "
              f"else. End whichever")
        print(f"  of the two is wrong before pushing anything again.")
        return 2
    print("  The publish returned an id but the offers are not confirmed on "
          "sale. Do not")
    print("  re-run this blindly; read the rows above first.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
