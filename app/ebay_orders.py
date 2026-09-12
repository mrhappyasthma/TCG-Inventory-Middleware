"""
The adapter binding ``tcg_engine.order_sync`` to ``ebay_client.orders``.

Its whole reason for existing is the projection below, which is a compliance
boundary rather than a convenience.

``getOrders`` returns a great deal about the person who bought the card:
``buyer.username``, ``buyer.buyerRegistrationAddress``, and inside
``fulfillmentStartInstructions`` a full ``shipTo`` with a name, a contact
address, an email address and a phone number. This application persists none
of it, in either database, and that is what its account-deletion exemption
rests on: a request to erase a buyer can be answered with "nothing about them
was ever kept".

So the projection is built by **naming the fields to keep**, never by
copying an order and deleting what is unwanted. The difference matters: a
delete-list silently admits every field eBay adds in future, while a
keep-list admits nothing that was not decided on. ``order_sync`` then refuses
any line whose keys are not exactly the allowed set, so a mistake here fails
at the boundary instead of reaching the database.

Nothing in here is logged, for the same reason the notification endpoint logs
only a topic.
"""

from typing import Any, Dict, List, Sequence

from ebay_client import orders as ebay_orders
from tcg_engine.order_sync import (
    STATUS_ACTIVE,
    STATUS_CANCELED,
    STATUS_REFUNDED,
)


def line_status(order: Dict[str, Any], item: Dict[str, Any]) -> str:
    """
    Whether this line is a sale that stands, in our own vocabulary.

    eBay spreads the answer across three fields -- ``cancelStatus`` on the
    order, ``orderPaymentStatus`` on the order, and
    ``lineItemFulfillmentStatus`` on the line -- and none of them alone says
    it. Collapsing them here keeps that knowledge in the layer that already
    knows eBay's shapes, and hands the engine a closed set of three values.

    Fulfillment status is deliberately not consulted: whether a card has been
    posted yet has no bearing on whether it left the shelf.
    """
    if ebay_orders.is_cancelled(order):
        return STATUS_CANCELED
    if ebay_orders.is_fully_refunded(order):
        return STATUS_REFUNDED
    # A line refunded on its own, in an order that was not. eBay records it
    # under the line's own refunds rather than the order's payment status.
    refunds = item.get("refunds")
    if isinstance(refunds, list) and refunds:
        return STATUS_REFUNDED
    return STATUS_ACTIVE


def project_order_lines(
    orders: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    eBay's orders reduced to the only fields allowed past this point.

    A keep-list, not a delete-list. Every value is read by name out of the
    payload and placed into a dictionary literal, so nothing travels by
    accident -- and adding one means changing this, the frozenset in
    ``order_sync`` and two tests, which is friction on purpose.
    """
    projected: List[Dict[str, Any]] = []
    for order in orders:
        order_id = str(order.get("orderId") or "").strip()
        if not order_id:
            continue
        # The moment of sale, for the record. Creation rather than last
        # modification: a cancellation weeks later must not make the sale
        # look recent.
        sold_at = str(order.get("creationDate") or "").strip() or None

        for item in ebay_orders.line_items(order):
            line_item_id = str(item.get("lineItemId") or "").strip()
            if not line_item_id:
                continue
            try:
                quantity = int(item.get("quantity") or 0)
            except (TypeError, ValueError):
                quantity = 0
            projected.append({
                "order_id": order_id,
                "line_item_id": line_item_id,
                "sku": str(item.get("sku") or "").strip(),
                # eBay's item number for the listing. A public identifier,
                # not personal data, and the only way to tell a sale from one
                # of our listings apart from a sale from one of the many
                # listed by hand -- which arrive through the same feed, have
                # no SKU, and have nothing to deduct.
                "legacy_item_id": str(item.get("legacyItemId") or "").strip(),
                "quantity": max(0, quantity),
                "sold_at": sold_at,
                "status": line_status(order, item),
            })
    return projected
