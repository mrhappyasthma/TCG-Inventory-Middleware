#!/usr/bin/env python3
"""
Show the shape of eBay's order line items, so a blank SKU can be explained.

Every line of a real first poll came back with an empty ``sku``, and there are
two very different reasons that can happen:

* the sale was from a listing that genuinely has no SKU — one made outside
  this application, or before SKUs were being set. Nothing is wrong and there
  is nothing to deduct from;
* the SKU is there and ``lineItems[].sku`` is not where eBay puts it. For a
  multi-variation listing the identifying information may arrive as
  ``legacyVariationId`` or in ``variationAspects`` instead, in which case the
  projection is reading the wrong field and every sale is being missed.

Guessing between those is how a fix gets applied to the wrong one, so this
prints what eBay actually sent. It is the same approach ``report_outline``
takes for the Feed report, and for the same reason: a shape question is
cheapest to settle by looking.

**It prints no personal data.** Field *names* are listed in full, so a field
this project has never seen shows up; field *values* are printed only for an
allowlist of card-identifying fields. A buyer's name, address, email, phone
and username are never rendered, and neither is anything outside that list,
including fields added by a future API version.

Usage, on the NAS:

    docker compose exec tcg-middleware python scripts/inspect_orders.py
    docker compose exec tcg-middleware python scripts/inspect_orders.py --days 7

Reads only, and writes nothing anywhere.
"""

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT,
             os.path.join(REPO_ROOT, "tcg_engine"),
             os.path.join(REPO_ROOT, "ebay_client")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.user_db import UserDatabase  # noqa: E402
from ebay_client.client import EbayClient  # noqa: E402
from ebay_client.config import EbayConfig  # noqa: E402
from ebay_client.errors import EbayError  # noqa: E402
from ebay_client.oauth import TokenStore  # noqa: E402
from ebay_client.orders import get_orders, line_items  # noqa: E402

USER_DATABASE_URL = os.environ.get("USER_DATABASE_URL", "data/users.db")

# The only fields whose *values* may be printed. Everything about a card and
# nothing about a person. Anything not named here is reported as present or
# absent and never rendered, which is what keeps a future API version from
# leaking something through this script.
SAFE_LINE_FIELDS = (
    "lineItemId",
    "sku",
    "legacyItemId",
    "legacyVariationId",
    "title",
    "quantity",
    "lineItemFulfillmentStatus",
)
SAFE_ORDER_FIELDS = (
    "orderId",
    "creationDate",
    "lastModifiedDate",
    "orderPaymentStatus",
    "orderFulfillmentStatus",
)


class _ReadOnlyTokenStore(TokenStore):
    def __init__(self, user_db):
        self.user_db = user_db

    def load(self):
        return self.user_db.get_ebay_token()

    def save(self, token):
        pass


def build_client():
    if not EbayConfig.is_configured():
        raise SystemExit(
            "eBay is not configured here. Run it where the app runs:\n"
            "    docker compose exec tcg-middleware python "
            "scripts/inspect_orders.py"
        )
    client = EbayClient(
        EbayConfig.from_env(),
        store=_ReadOnlyTokenStore(UserDatabase(db_path=USER_DATABASE_URL)),
    )
    if not client.oauth.is_connected():
        raise SystemExit("No eBay account is connected.")
    return client


def describe_variation_aspects(item):
    """
    The variation axis and its value, which name a card without naming a buyer.

    For a listing built by this application the value reads like
    "Ledyba (004/198)" -- and `relink.parse_option_name` already decodes that
    back into a card, so if this is where the identity lives there is a
    fallback available rather than a dead end.
    """
    aspects = item.get("variationAspects")
    if not isinstance(aspects, list):
        return None
    return [
        f"{a.get('name')}={a.get('value')}"
        for a in aspects if isinstance(a, dict)
    ]


def main():
    parser = argparse.ArgumentParser(
        description="Print the shape of eBay's order line items.",
    )
    parser.add_argument("--days", type=int, default=90,
                        help="how far back to look (default 90, eBay's limit)")
    parser.add_argument("--show", type=int, default=5,
                        help="how many line items to print in full detail")
    args = parser.parse_args()

    client = build_client()
    since = datetime.now(timezone.utc) - timedelta(days=max(1, args.days))
    stamp = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    print(f"Reading orders modified since {stamp} ...")
    try:
        orders = get_orders(client.seller, modified_since=stamp)
    except EbayError as exc:
        raise SystemExit(f"eBay refused the read: {exc}")

    lines = [(o, i) for o in orders for i in line_items(o)]
    print(f"{len(orders)} order(s), {len(lines)} line item(s).\n")
    if not lines:
        print("Nothing to inspect.")
        return 0

    # Which fields are present, and how often a value is actually there. A
    # field present on every line but empty on every line is the signature of
    # reading the wrong one.
    present = Counter()
    populated = Counter()
    for _, item in lines:
        for key, value in item.items():
            present[key] += 1
            if value not in (None, "", [], {}):
                populated[key] += 1

    print("Line item fields, by how often they carry a value:")
    print(f"  {'field':<34} {'present':>8} {'populated':>10}")
    for key in sorted(present, key=lambda k: (-populated[k], k)):
        marker = "  <-- always empty" if populated[key] == 0 else ""
        print(f"  {key:<34} {present[key]:>8} {populated[key]:>10}{marker}")

    print(f"\nFirst {min(args.show, len(lines))} line item(s) in detail "
          f"(card fields only -- nothing about the buyer is printed):\n")
    for order, item in lines[:max(1, args.show)]:
        for field in SAFE_ORDER_FIELDS:
            if order.get(field) is not None:
                print(f"  {field:<26} {order.get(field)}")
        for field in SAFE_LINE_FIELDS:
            value = item.get(field)
            shown = "(empty)" if value in (None, "", [], {}) else value
            print(f"    {field:<24} {shown}")
        aspects = describe_variation_aspects(item)
        if aspects is not None:
            print(f"    {'variationAspects':<24} {aspects or '(empty)'}")
        # Named, not rendered: this is where a SKU might be hiding in a field
        # nobody has thought of yet.
        others = sorted(set(item) - set(SAFE_LINE_FIELDS)
                        - {"variationAspects"})
        print(f"    other fields present      {others}")
        print()

    blank_sku = sum(1 for _, i in lines
                    if not str(i.get("sku") or "").strip())
    print(f"{blank_sku} of {len(lines)} line item(s) have an empty `sku`.")
    if blank_sku == len(lines):
        print(
            "\nEvery one. If `legacyVariationId` or `variationAspects` above "
            "does carry a value, the identity is there and the projection is "
            "reading the wrong field. If those are empty too, these sales are "
            "from listings that genuinely have no SKU, and there is nothing "
            "for the poller to deduct against."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
