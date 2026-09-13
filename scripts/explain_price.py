#!/usr/bin/env python3
"""
Why is this card's price the way it is, and why does a draft want to change it?

There are **two** paths that change a listed price, and they do not share
their safety rules. That is the usual reason a draft surprises somebody:

* The **automatic repricer** is unattended and writes straight to eBay, so it
  is heavily damped -- a rise applies at once, a fall is held until the lower
  figure has stayed true for the whole hold window, the market must clear a
  tier edge by a margin, and a run that would move too much of the store at
  once is refused outright.

* A **draft plan** is the diff between the catalogue and what eBay is known
  to hold. It has **none** of that damping, because it is reviewed by a
  person before anything is sent. It proposes a price whenever the stored
  price differs from eBay's -- *including when eBay's is simply unknown*,
  which is the case most often mistaken for a bug.

This prints both verdicts side by side for one card, with the numbers each
was reached from.

Usage, from the repository root or inside the container:

    python scripts/explain_price.py "Risky Ruins"
    python scripts/explain_price.py ID1074

On the NAS:

    cd /volume1/docker/tcg-middleware
    docker compose exec tcg-middleware python scripts/explain_price.py "Rare Candy"

Reads only. Touches eBay not at all and writes nothing.
"""

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

from tcg_engine.db import (  # noqa: E402
    SHARED_SCOPE,
    Database,
    apply_condition_multiplier,
    apply_pricing_rules,
)
from tcg_engine.plans import PRICE_EPSILON, desired_price  # noqa: E402
from tcg_engine.repricer import (  # noqa: E402
    decide_card,
    reprice_settings,
    utcnow,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "data/inventory.db")


def find_cards(db, needle):
    """Every catalogued card whose id or name matches, so a typo is visible."""
    text = str(needle).strip()
    rows = db.get_inventory(search=text, limit=50)
    exact = [r for r in rows if r["manifest_id"].upper() == text.upper()]
    return exact or rows


def money(value):
    return "unknown" if value is None else f"${float(value):.2f}"


def explain(db, card, user_id):
    manifest_id = card["manifest_id"]
    full = db.get_manifest_by_id(manifest_id)
    variation = db.get_variation(manifest_id)

    print("=" * 72)
    print(f"{manifest_id}  {full['product_name']}")
    number = full.get("card_number") or ""
    print(f"  {full['set_name']} {number}  {full['condition']} "
          f"{full['printing']}")
    print("=" * 72)

    stored = full.get("price")
    market = full.get("market_price")
    known = variation["last_known_price"] if variation else None
    parent = (variation or {}).get("ebay_parent_id") or ""

    print()
    print("  the numbers")
    print(f"    listed price we hold   {money(stored)}"
          "   <- manifest.price, what a draft proposes")
    print(f"    market price           {money(market)}"
          "   <- from TCGCSV, what the repricer reads")
    print(f"    eBay's last known      {money(known)}"
          "   <- from Module B or a confirmed push")
    print(f"    on eBay as             {parent or 'not linked'}")

    # What the rules alone would say about the current market price.
    if market:
        # Rules first, then the condition discount -- the same order the
        # ingest and the repricer apply them in.
        multipliers = {
            m["condition_key"]: m["multiplier"]
            for m in db.get_condition_multipliers(user_id=user_id)
        }
        graded, _ = apply_condition_multiplier(
            float(market), full.get("condition"), multipliers
        )
        ruled, _ = apply_pricing_rules(
            db.get_pricing_rules(user_id=user_id), graded
        )
        print(f"    the rules say          {money(ruled)}"
              "   <- from the market price above")

    # -- would a draft propose a change? ------------------------------------
    print()
    print("  a draft plan")
    proposed = desired_price(full)
    if proposed is None:
        print("    proposes nothing: this card has no stored price, which a")
        print("    plan reports as a validation failure rather than listing")
        print("    it at raw market.")
    elif known is None:
        print(f"    WOULD propose {money(proposed)} -- and this is almost")
        print("    certainly your answer. eBay's price for this variation is")
        print("    *unknown*, and an unknown counts as changed by design:")
        print("    nothing is suppressed until a sync has told us what eBay")
        print("    actually holds, because suppressing against a value we")
        print("    never learned would silently drop a real change.")
        print("    Run Module B (eBay Listings -> sync) and it should stop.")
    elif abs(float(proposed) - float(known)) > PRICE_EPSILON:
        direction = "up" if float(proposed) > float(known) else "down"
        print(f"    WOULD propose {money(known)} -> {money(proposed)} "
              f"({direction})")
        print("    A draft has no hold window and no boundary margin. Those")
        print("    belong to the repricer, which writes unattended; a draft")
        print("    is reviewed before anything is sent, so it simply reports")
        print("    the difference.")
    else:
        print("    proposes no price change: the stored price already matches")
        print("    what eBay is known to hold.")

    # -- and what would the repricer do? ------------------------------------
    print()
    print("  the automatic repricer")
    managed = {
        row["manifest_id"]: row
        for row in db.get_managed_cards_for_repricing()
    }
    row = managed.get(manifest_id)
    if row is None:
        print("    does not consider this card. It only touches listings")
        print("    created through the eBay API -- if this one was made on")
        print("    File Exchange, the repricer cannot see its offer.")
        return

    config = reprice_settings(db, user_id=user_id)
    decision = decide_card(
        row,
        rules=db.get_pricing_rules(user_id=user_id),
        multipliers={
            m["condition_key"]: m["multiplier"]
            for m in db.get_condition_multipliers(user_id=user_id)
        },
        margin_fraction=config["margin_fraction"],
        hold_days=config["hold_days"],
        now=utcnow(),
    )
    print(f"    verdict   {decision.get('verdict')}")
    print(f"    reason    {decision.get('reason')}")
    if decision.get("new_price") is not None:
        print(f"    would set {money(decision.get('old_price'))} -> "
              f"{money(decision.get('new_price'))}")
    if row.get("hold_since"):
        print(f"    holding since {row['hold_since']} "
              f"(window {config['hold_days']} days)")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Explain one card's price, and both paths that change it.",
    )
    parser.add_argument("card", help="A manifest id, or part of a card name")
    parser.add_argument(
        "--db", default=DATABASE_URL,
        help=f"Path to the inventory database (default {DATABASE_URL}).",
    )
    parser.add_argument(
        "--user-id", type=int, default=SHARED_SCOPE,
        help="Whose pricing rules to read. Defaults to the shared baseline.",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"No database at {args.db}. Set DATABASE_URL or pass --db.")
        return 2

    db = Database(args.db)
    matches = find_cards(db, args.card)
    if not matches:
        print(f"Nothing in the catalogue matches {args.card!r}.")
        return 1
    if len(matches) > 12:
        print(f"{len(matches)} cards match {args.card!r}. Narrow it down, or "
              f"name a manifest id.")
        for row in matches[:12]:
            print(f"  {row['manifest_id']}  {row['product_name']} "
                  f"({row['condition']})")
        return 1

    for card in matches:
        explain(db, card, args.user_id)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
