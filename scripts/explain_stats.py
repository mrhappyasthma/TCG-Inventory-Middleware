#!/usr/bin/env python3
"""
Reconcile the four numbers in the page header, card by card.

The header shows two units and it is easy to read them as one:

* **Cards** -- kinds of card. ``total_cards`` counts every row in the
  catalogue; ``active_listings`` counts the cards that have an eBay variation
  row with a parent listing id on it.
* **Copies** -- physical cards. ``total_on_hand`` sums ``manifest.quantity``
  over the whole catalogue; ``total_stock`` sums ``last_known_qty`` over the
  linked cards only.

The trap this exists for: the two Copies figures are **sums**, so differences
that point opposite ways cancel. 857 cards against 856 on eBay with the copy
totals identical does not mean one card is missing and nothing else is wrong.
It means the shortfall from the unlinked card and the net of every per-card
disagreement happen to add to zero. Either both are zero, or they are hiding
each other.

So the identity this checks is:

    total_on_hand - total_stock
        = copies held by cards with no eBay link
        + the net of (quantity - last_known_qty) over linked cards

and it prints all three terms plus the cards behind them.

One thing to know before reading a per-card difference as a fault. The two
quantities are meant to be able to differ: ``quantity`` is what you hold and
``last_known_qty`` is what eBay was last confirmed to hold. The gap between
them is precisely what the drafts page proposes as a plan, so a card listed
below should normally also be in your current draft. A card that appears here
and *not* in any draft is the interesting case.

Reads only. It writes nothing.

    python scripts/explain_stats.py
    python scripts/explain_stats.py --limit 40

On the NAS:

    cd /volume1/docker/tcg-middleware
    docker compose exec tcg-middleware python scripts/explain_stats.py
"""

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app import deps  # noqa: E402

# The filter get_stats uses for "on eBay". Repeated here rather than
# imported because the point is to check it, and a diagnostic that shares the
# subject's own definition of correct cannot disagree with it.
LINKED = "ev.ebay_parent_id IS NOT NULL AND TRIM(ev.ebay_parent_id) != ''"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reconcile the page header's card and copy totals.",
    )
    parser.add_argument(
        "--limit", type=int, default=20, metavar="N",
        help="How many cards to list per section (default 20, 0 for all).",
    )
    parser.add_argument(
        "--user-id", type=int, default=None,
        help="Whose inventory to read. Defaults to the deployment owner.",
    )
    args = parser.parse_args(argv)

    user_id = args.user_id if args.user_id is not None else deps._owner_scope()
    inv = deps.inventory_for(user_id)
    cap = None if args.limit == 0 else args.limit

    with inv.get_connection() as conn:
        cur = conn.cursor()

        stats = inv.get_stats()
        print()
        print("  The header, as the page computes it")
        print(f"    cards in the catalogue   {stats['total_cards']:>8,}")
        print(f"    cards linked to eBay     {stats['active_listings']:>8,}")
        print(f"    copies on hand           {stats['total_on_hand']:>8,}")
        print(f"    copies on eBay           {stats['total_stock']:>8,}")

        card_gap = stats["total_cards"] - stats["active_listings"]
        copy_gap = stats["total_on_hand"] - stats["total_stock"]
        print()
        print(f"    {card_gap:,} card(s) in the catalogue have no eBay link, "
              f"and the copy totals")
        print(f"    differ by {copy_gap:,}.")

        # Term 1: cards with no eBay variation row at all, or one whose
        # parent id is blank. Both are "not on eBay" as far as the header is
        # concerned, but they are different situations.
        cur.execute(
            f"""
            SELECT m.manifest_id, m.product_name, m.card_number, m.set_name,
                   m.condition, m.quantity,
                   CASE WHEN ev.manifest_id IS NULL THEN 'no variation row'
                        ELSE 'variation row with no listing id' END AS why
            FROM manifest m
            LEFT JOIN ebay_variations ev ON ev.manifest_id = m.manifest_id
            WHERE ev.manifest_id IS NULL OR NOT ({LINKED})
            ORDER BY m.quantity DESC, m.set_name, m.card_number
            """
        )
        unlinked = cur.fetchall()
        unlinked_copies = sum(int(r["quantity"] or 0) for r in unlinked)

        # Term 2: linked cards whose two quantities disagree.
        cur.execute(
            f"""
            SELECT m.manifest_id, m.product_name, m.card_number, m.set_name,
                   m.condition, m.quantity, ev.last_known_qty,
                   ev.ebay_parent_id,
                   m.quantity - ev.last_known_qty AS diff
            FROM manifest m
            JOIN ebay_variations ev ON ev.manifest_id = m.manifest_id
            WHERE {LINKED} AND m.quantity != ev.last_known_qty
            ORDER BY ABS(m.quantity - ev.last_known_qty) DESC,
                     m.set_name, m.card_number
            """
        )
        mismatched = cur.fetchall()
        net_mismatch = sum(int(r["diff"] or 0) for r in mismatched)
        behind = sum(1 for r in mismatched if int(r["diff"] or 0) > 0)
        ahead = len(mismatched) - behind

        print()
        print("  Where the copy difference comes from")
        print(f"    copies held by cards with no eBay link   {unlinked_copies:>8,}")
        print(f"    net of (on hand - on eBay) where linked  {net_mismatch:>8,}")
        print(f"    {'-' * 48}")
        print(f"    these add to                             "
              f"{unlinked_copies + net_mismatch:>8,}")
        print(f"    and the header's difference is            {copy_gap:>8,}")
        if unlinked_copies + net_mismatch != copy_gap:
            print()
            print("    Those do not agree, which means this script's "
                  "arithmetic is wrong")
            print("    rather than your data -- the two should be equal by "
                  "construction.")

        if unlinked_copies and net_mismatch and copy_gap == 0:
            print()
            print("    Note that both terms are non-zero and they cancel. "
                  "The matching copy")
            print("    totals in the header are a coincidence of this "
                  "subtraction, not a sign")
            print("    that the two sides agree. Read the two lists below "
                  "rather than the totals.")

        print()
        print(f"  Cards with no eBay link ({len(unlinked)})")
        if not unlinked:
            print("    none")
        for row in (unlinked if cap is None else unlinked[:cap]):
            print(f"    {row['manifest_id']:10} {row['quantity']:>4} on hand  "
                  f"{row['why']:32} {row['product_name']} "
                  f"{row['card_number'] or ''} [{row['set_name']} "
                  f"{row['condition']}]")
        if cap is not None and len(unlinked) > cap:
            print(f"    ... {len(unlinked) - cap} more")
        if unlinked and not unlinked_copies:
            print()
            print("    All of these hold zero copies, so they cost the "
                  "catalogue a card on the")
            print("    'on eBay' count and nothing on the copy count. A card "
                  "at zero is not")
            print("    listed, so this is the expected shape rather than a "
                  "fault.")

        print()
        print(f"  Linked cards whose two quantities disagree "
              f"({len(mismatched)})")
        if not mismatched:
            print("    none")
        for row in (mismatched if cap is None else mismatched[:cap]):
            diff = int(row["diff"] or 0)
            print(f"    {row['manifest_id']:10} on hand {row['quantity']:>4}, "
                  f"eBay {row['last_known_qty']:>4}  "
                  f"{('+' + str(diff)) if diff > 0 else str(diff):>5}  "
                  f"{row['product_name']} {row['card_number'] or ''} "
                  f"[#{row['ebay_parent_id']}]")
        if cap is not None and len(mismatched) > cap:
            print(f"    ... {len(mismatched) - cap} more")

        if mismatched:
            print()
            print(f"    {behind} card(s) hold more than eBay knows about and "
                  f"{ahead} hold fewer.")
            print("    This gap is what a draft is: the drafts page proposes "
                  "exactly the")
            print("    difference between these two columns, so these cards "
                  "should show up")
            print("    in your current draft. A card here that is in no "
                  "draft is the one")
            print("    worth chasing -- start with build_plan's suppression "
                  "rules, which")
            print("    decide what is allowed to become a plan item.")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
