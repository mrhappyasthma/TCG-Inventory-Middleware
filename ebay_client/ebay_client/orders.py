"""
Reading orders from the Fulfillment API.

Reads only. Nothing here creates a fulfillment, marks anything shipped, or
issues a refund — the only thing this project does with an order is learn that
a card left the building.

Two properties matter more than the size of this module.

**A page is not the answer.** ``getOrders`` returns a paged collection with a
``total``, and treating the first page as the whole result silently loses
sales the moment there are more than fifty in a window. So this paginates to
exhaustion and the caller receives every order or an exception, never a
partial list that looks complete.

**It knows nothing about cards.** The vocabulary is orders, line items and
SKUs. Deciding what a SKU means, what to deduct and what to do about a
cancellation belongs to the layer above — which is also where the buyer's
personal details are stripped off, because this module deliberately hands back
eBay's payload exactly as it arrived rather than deciding what is safe to
keep.
"""

from typing import Any, Dict, List, Optional

FULFILLMENT_BASE = "/sell/fulfillment/v1"

# eBay's own default page size. Larger pages mean fewer round trips, and the
# call is cheap, but a page that is too large is a bigger thing to lose to a
# timeout and retry.
ORDER_PAGE_LIMIT = 50

# A ceiling on pagination, purely so a bug at eBay's end or ours cannot spin
# forever against a rate-limited API. At the default page size this is 10,000
# orders in one poll, which is far beyond anything this store will see; hitting
# it means something is wrong, and an exception says so.
MAX_ORDER_PAGES = 200

# The filter's own limit: eBay serves order data for the last ninety days.
# Asking for older than this returns nothing rather than failing, which is the
# more dangerous answer -- so a caller whose watermark has fallen behind needs
# to be told rather than handed an empty list.
ORDER_HISTORY_DAYS = 90


class OrderPageError(RuntimeError):
    """Pagination could not be completed, so the result would be partial."""


def modified_since_filter(timestamp: str) -> str:
    """
    eBay's filter syntax for "changed at or after this moment".

    An open-ended range: ``lastmodifieddate:[2026-09-11T08:00:00.000Z..]``.
    The timestamp must be ISO 8601 in UTC with the ``Z`` suffix; eBay rejects
    a naive one. The transport's URL builder keeps ``[``, ``]``, ``:`` and
    ``..`` unescaped, which this syntax depends on.
    """
    stamp = str(timestamp or "").strip()
    if not stamp:
        raise ValueError("a timestamp is required")
    return f"lastmodifieddate:[{stamp}..]"


def get_orders(
    transport,
    modified_since: Optional[str] = None,
    limit: int = ORDER_PAGE_LIMIT,
    max_pages: int = MAX_ORDER_PAGES,
) -> List[Dict[str, Any]]:
    """
    Every order modified since a moment, as eBay returned them.

    Paginates to exhaustion. ``modified_since`` is an ISO 8601 UTC timestamp;
    omitting it asks for everything eBay still holds, which is the last ninety
    days and is what a first run wants.

    Raises ``OrderPageError`` rather than returning a partial list, because a
    truncated result here reads as "no more sales" and would leave stock on
    the shelf that has gone.
    """
    params: Dict[str, Any] = {"limit": max(1, int(limit))}
    if modified_since:
        params["filter"] = modified_since_filter(modified_since)

    orders: List[Dict[str, Any]] = []
    offset = 0
    total: Optional[int] = None

    for _ in range(max(1, int(max_pages))):
        page = transport.get(
            f"{FULFILLMENT_BASE}/order", dict(params, offset=offset)
        ) or {}
        batch = page.get("orders")
        if not isinstance(batch, list):
            batch = []
        orders.extend(batch)

        if total is None and isinstance(page.get("total"), int):
            total = page["total"]

        # An empty page ends it. Checked before the total, because eBay's
        # total is a count at the time of the first page and an order
        # modified mid-pagination can move the target.
        if not batch:
            return orders
        if total is not None and len(orders) >= total:
            return orders
        offset += len(batch)

    raise OrderPageError(
        f"stopped after {max_pages} pages with {len(orders)} order(s) and no "
        f"end in sight; refusing to return a partial list"
    )


def line_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The line items of one order, or an empty list."""
    items = order.get("lineItems")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def is_cancelled(order: Dict[str, Any]) -> bool:
    """
    Whether eBay reports this order as cancelled.

    ``cancelStatus.cancelState`` is the field; ``NONE_REQUESTED`` is the
    ordinary case. A *requested* cancellation is not treated as cancelled,
    because the buyer asking is not the same as it happening.
    """
    state = str(
        (order.get("cancelStatus") or {}).get("cancelState") or ""
    ).strip().upper()
    return state in ("CANCELED", "CANCELLED", "CLOSED")


def is_fully_refunded(order: Dict[str, Any]) -> bool:
    """Whether the whole order's money has gone back."""
    return str(
        order.get("orderPaymentStatus") or ""
    ).strip().upper() == "FULLY_REFUNDED"
