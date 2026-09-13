#!/usr/bin/env python3
"""
Stop a draft proposing to undo a price eBay already has.

A draft plan proposes a price whenever the price we hold for a card disagrees
with what eBay is known to hold. The automatic repricer moves the eBay price
and records it against the *variation* -- it does not write back to the card
-- so after it acts, the catalogue still holds the pre-repricer number and
every draft rebuild offers to put it back. That is the usual reason a card
reappears in a draft with a price change you did not ask for and do not want.

This settles it by accepting eBay's figure as ours, which is the thing to do
when the change was the repricer's and you are happy with it. Nothing
recomputes the stored price afterwards -- no upload, no market refresh -- so
the pin is permanent.

    python scripts/accept_ebay_price.py "Risky Ruins" "Rare Candy"
    python scripts/accept_ebay_price.py ID1074 --yes
    python scripts/accept_ebay_price.py ID1074 --set 2.99 --yes

On the NAS:

    cd /volume1/docker/tcg-middleware
    docker compose exec tcg-middleware python scripts/accept_ebay_price.py \\
        "Risky Ruins" "Rare Candy"

A dry run is the default and writes nothing. Every change prints the previous
value and the exact command that undoes it. Touches eBay not at all: this
only changes what *we* hold, so the next draft agrees with the listing
instead of arguing with it.
"""

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

from tcg_engine.db import Database  # noqa: E402
from tcg_engine.plans import PRICE_EPSILON  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "data/inventory.db")


def money(value):
    return "unknown" if value is None else f"${float(value):.2f}"


def resolve(db, needle):
    """The one card this names, or every candidate so a typo is visible."""
    text = str(needle).strip()
    rows = db.get_inventory(search=text, limit=50)
    exact = [r for r in rows if r["manifest_id"].upper() == text.upper()]
    return exact or rows


def plan_one(db, card, override):
    """What would change for one card, and why, without changing it."""
    manifest_id = card["manifest_id"]
    full = db.get_manifest_by_id(manifest_id)
    variation = db.get_variation(manifest_id)

    held = full.get("price")
    known = variation["last_known_price"] if variation else None
    label = (
        f"{manifest_id}  {full['product_name']}"
        f"{' #' + full['card_number'] if full.get('card_number') else ''}"
        f"  ({full['condition']})"
    )

    if override is not None:
        target = round(float(override), 2)
        why = "the price you named"
    elif known is None:
        return {
            "id": manifest_id, "label": label, "skip": (
                "eBay's price for this variation is unknown, so there is "
                "nothing to accept. Run Module B (eBay Listings -> sync) "
                "first, or pass --set to name a price yourself."
            ),
        }
    else:
        target = round(float(known), 2)
        why = "what eBay already holds"

    if held is not None and abs(float(held) - target) <= PRICE_EPSILON:
        return {
            "id": manifest_id, "label": label, "skip": (
                f"already {money(held)}, so no draft would propose a change."
            ),
        }

    return {
        "id": manifest_id,
        "label": label,
        "held": held,
        "known": known,
        "market": full.get("market_price"),
        "target": target,
        "why": why,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Accept eBay's price as ours, so a draft stops proposing to "
            "undo it."
        ),
    )
    parser.add_argument(
        "cards", nargs="+",
        help="Manifest ids, or parts of card names",
    )
    parser.add_argument(
        "--set", type=float, default=None, metavar="PRICE",
        help=(
            "Pin to this price instead of eBay's. Use it to undo a previous "
            "run, or when eBay's price is not yet known."
        ),
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Actually write. Without it this is a dry run.",
    )
    parser.add_argument(
        "--db", default=DATABASE_URL,
        help=f"Path to the inventory database (default {DATABASE_URL}).",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"No database at {args.db}. Set DATABASE_URL or pass --db.")
        return 2

    db = Database(args.db)

    # Resolve everything before writing anything, so an ambiguous name is not
    # discovered halfway through a batch.
    targets = []
    for needle in args.cards:
        matches = resolve(db, needle)
        if not matches:
            print(f"Nothing in the catalogue matches {needle!r}.")
            return 1
        if len(matches) > 1:
            print(f"{needle!r} matches {len(matches)} cards. Name one:")
            for row in matches[:15]:
                print(f"  {row['manifest_id']}  {row['product_name']} "
                      f"({row['condition']}, {row['set_name']})")
            return 1
        targets.append(matches[0])

    if args.set is not None and len(targets) > 1:
        print("--set names one price, so give it one card at a time.")
        return 2

    plans = [plan_one(db, card, args.set) for card in targets]

    print()
    changing = [p for p in plans if "skip" not in p]
    for plan in plans:
        print(f"  {plan['label']}")
        if "skip" in plan:
            print(f"      nothing to do: {plan['skip']}")
        else:
            print(f"      we hold      {money(plan['held'])}")
            print(f"      eBay holds   {money(plan['known'])}")
            print(f"      market       {money(plan['market'])}")
            print(f"      -> set ours to {money(plan['target'])} "
                  f"({plan['why']})")
        print()

    if not changing:
        print("Nothing to change.")
        return 0

    if not args.yes:
        print(f"Dry run -- nothing was written. Re-run with --yes to set "
              f"{len(changing)} price(s).")
        return 0

    for plan in changing:
        result = db.set_manifest_price(plan["id"], plan["target"])
        if result is None:
            print(f"  {plan['id']}: vanished between planning and writing.")
            continue
        print(f"  {plan['id']}: {money(result['previous'])} -> "
              f"{money(result['current'])}")
        # The undo, spelled out, because the previous value is the only copy
        # of it and this output may be the only place it survives.
        if result["previous"] is not None:
            print(f"      to undo: accept_ebay_price.py {plan['id']} "
                  f"--set {result['previous']:.2f} --yes")

    print()
    print("Rebuild the draft and those cards should no longer appear with a "
          "price change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
