"""
Orders: reading what sold from eBay, and the list of cards to pull.

Polled rather than pushed, because eBay's REST Notification API has no order
topic -- order events exist only in the legacy Platform Notifications that
eBay is retiring, and eBay's own guidance is to poll `getOrders` regardless.

Three properties hold this together and are each worth keeping in mind before
changing anything here:

* **The watermark advances only on a complete read.** A truncated page raises
  rather than returning what it has, because a partial result is
  indistinguishable from a quiet week -- and a quiet week moves the watermark
  past sales nobody has seen.
* **A deduction happens exactly once**, claimed by an UPDATE guarded on
  `deducted_at IS NULL` rather than by a prior read, so two pollers running at
  once cannot both claim one sale.
* **The first poll adopts.** eBay serves ninety days of history, all of it
  already accounted for, so a deployment with no watermark records every line
  as handled and deducts nothing.

Buyer data never lands here: `app/ebay_orders.py` projects each line down to
a keep-list of named fields before this module sees it.
"""

import asyncio
import os

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from tcg_engine.db import SHARED_SCOPE, Database
from tcg_engine.order_sync import OrderSyncError, sync_orders
from tcg_engine.repricer import format_timestamp, parse_timestamp

try:
    from app import deps
    from app.deps import (
        ORDER_HISTORY_DAYS,
        _owner_scope,
        EbayError,
        OrderPageError,
        inventory_for,
        owner_inventory,
        project_order_lines,
        record_logs,
        require_active_user,
    )
except ImportError:
    from . import deps
    from .deps import (
        ORDER_HISTORY_DAYS,
        _owner_scope,
        EbayError,
        OrderPageError,
        inventory_for,
        owner_inventory,
        project_order_lines,
        record_logs,
        require_active_user,
    )

router = APIRouter()

# Past this, the watermark is old enough that eBay's ninety-day window may
# have swallowed sales we never read. Worth saying loudly rather than
# reporting a quiet poll.
ORDER_WATERMARK_STALE_DAYS = 60


class PickRequest(BaseModel):
    # True when a card has been pulled off the shelf, False when it has been
    # put back. Unticking is deliberately allowed: unlike a deduction, which
    # a machine claims exactly once, this records what a person did in a room
    # and people change their minds.
    picked: bool = True
    # One card, or the whole order when absent. Most orders are a single
    # card, where those are the same action.
    line_item_id: Optional[str] = None

ORDER_POLL_ENABLED = os.environ.get(
    "ORDER_POLL_ENABLED", "true"
).strip().lower() not in ("0", "false", "no", "off")

ORDER_POLL_INTERVAL_MINUTES = max(
    1, int(os.environ.get("ORDER_POLL_INTERVAL_MINUTES", "15"))
)

ORDER_POLL_OVERLAP_MINUTES = 30

ORDER_WATERMARK_SETTING = "orders_last_polled_at"

def _ebay_timestamp(moment: datetime) -> str:
    """eBay's filter wants ISO 8601 in UTC with milliseconds and a Z."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")

def _poll_orders_now(
    inv: Database, user_id: int, dry_run: bool = False
) -> Dict[str, Any]:
    """
    One round of reading orders and deducting what sold.

    Shared by the manual button and the background loop so there is exactly
    one code path: a poll somebody ran by hand and the one that happens every
    fifteen minutes must reach the same decisions.

    The watermark only advances on success. A poll that raises leaves it
    where it was, so the next attempt re-reads the same window rather than
    stepping over sales nobody has seen.
    """
    client = deps.get_ebay_client(user_id)
    if client is None or not client.oauth.is_connected():
        raise OrderSyncError("Connect the eBay account first.")

    collected: List[Dict[str, str]] = []

    def log(level: str, message: str) -> None:
        print(f"[orders] {level}: {message}", flush=True)
        collected.append({"level": level, "message": message})

    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    stored = inv.get_listing_setting(ORDER_WATERMARK_SETTING, "")
    watermark = parse_timestamp(stored)

    # No watermark means this deployment has never polled. eBay will hand
    # over ninety days of history, all of it already accounted for, so the
    # first run adopts rather than deducts.
    first_run = watermark is None
    if first_run:
        since = now - timedelta(days=ORDER_HISTORY_DAYS)
    else:
        since = watermark - timedelta(minutes=ORDER_POLL_OVERLAP_MINUTES)
        stale = (now - watermark).days
        if stale >= ORDER_WATERMARK_STALE_DAYS:
            log("WARN", (
                f"The last successful poll was {stale} days ago, and eBay only "
                f"serves {ORDER_HISTORY_DAYS} days of orders. Any sale older "
                f"than that window cannot be read now and will have to be "
                f"corrected by hand."
            ))

    try:
        raw = deps.get_orders(client.seller, modified_since=_ebay_timestamp(since))
    except OrderPageError as exc:
        # A partial read looks like a quiet week, which is the one thing this
        # must never report. The watermark stays put.
        log("ERROR", f"Refusing a partial read of orders: {exc}")
        record_logs(inv, collected, "orders")
        inv.record_order_poll(outcome="failed", detail=str(exc)[:200])
        raise OrderSyncError(str(exc))

    lines = project_order_lines(raw)

    if dry_run:
        log("INFO", (
            f"Preview only: {len(raw)} order(s) and {len(lines)} line item(s) "
            f"since {_ebay_timestamp(since)}. Nothing was deducted and the "
            f"watermark was not moved."
        ))
        record_logs(inv, collected, "orders")
        inv.record_order_poll(
            orders=len(raw), seen=len(lines),
            outcome="preview",
        )
        return {
            "attempted": False, "reason": "preview", "orders": len(raw),
            "seen": len(lines), "deducted": 0, "logs": collected,
        }

    def run():
        with inv.session():
            return sync_orders(inv, lines, log=log, adopt=first_run)

    try:
        result = run()
    finally:
        record_logs(inv, collected, "orders")

    # Only now, and only on success. Recorded as the moment the poll started
    # rather than finished: an order modified during the call belongs to the
    # next window, not to neither.
    inv.set_listing_settings(
        {ORDER_WATERMARK_SETTING: format_timestamp(now)}, user_id=SHARED_SCOPE
    )

    inv.record_order_poll(
        orders=len(raw),
        seen=result.get("seen", 0),
        deducted=result.get("deducted", 0),
        cards=result.get("deducted_cards", 0),
        foreign_sales=result.get("foreign", 0),
        unmatched=result.get("unmatched", 0),
        outcome="adopted" if first_run else "ok",
    )

    result["attempted"] = True
    result["orders"] = len(raw)
    result["first_run"] = first_run
    result["logs"] = collected
    return result

@router.post("/api/orders/poll")
async def poll_orders_endpoint(
    dry_run: bool = Form(False),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Read orders now and deduct what sold.

    The background loop does this every fifteen minutes; this exists so the
    first run can be inspected before being trusted, and so a sale can be
    accounted for without waiting.
    """
    inv = inventory_for(user)
    try:
        return await run_in_threadpool(
            lambda: _poll_orders_now(inv, user["id"], dry_run=dry_run)
        )
    except OrderSyncError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except EbayError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

@router.get("/api/orders/recent")
def recent_orders_endpoint(
    limit: int = 100,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    What the poller has seen, and what it deducted.

    Includes the lines it could not act on -- a SKU matching no card, a
    cancellation after a deduction -- because those are the ones needing a
    person, and a list that showed only successes would hide them.
    """
    inv = inventory_for(user)
    return {
        "last_polled_at": inv.get_listing_setting(ORDER_WATERMARK_SETTING, ""),
        "poll_interval_minutes": ORDER_POLL_INTERVAL_MINUTES,
        "enabled": ORDER_POLL_ENABLED,
        # The polls themselves. A row of quiet ones is how you know the
        # poller is alive, which nothing else on the page can tell you.
        "polls": inv.get_order_polls(limit=8),
        # Only sales that resolved to a catalogued card. The rest are
        # overwhelmingly ordinary business -- sales from listings this
        # application does not manage -- and listing them read like a page
        # of problems, which is exactly what it was not.
        "lines": inv.get_recent_order_lines(
            limit=max(1, min(500, limit)), matched_only=True
        ),
        "unmatched_count": inv.count_order_lines(matched=False),
        # How many cards are waiting to be pulled. Returned here as well as
        # from the pick endpoint so the To Pick badge is right without the
        # tab having been opened -- this is the call the dashboard already
        # makes on a timer, and a queue nobody knows about is not a queue.
        "outstanding_cards": inv.count_outstanding_pick_lines(),
    }

@router.get("/api/orders/pick")
def pick_list_endpoint(
    scope: str = "outstanding",
    limit: int = 50,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    The cards to pull, grouped by order.

    ``scope=outstanding`` is the work queue: orders with at least one live
    line not yet picked. ``scope=all`` includes the packed ones, so a
    finished order can be checked or un-ticked.

    Carries **no buyer information**, because none is stored -- not a name,
    not an address, not a username. The address is eBay's to print. This
    answers the other half: which cards, and out of which box.
    """
    inv = inventory_for(user)
    outstanding = str(scope or "outstanding").lower() != "all"
    return {
        "scope": "outstanding" if outstanding else "all",
        "orders": inv.get_pick_orders(
            outstanding_only=outstanding, limit=max(1, min(200, limit))
        ),
        "outstanding_cards": inv.count_outstanding_pick_lines(),
    }

@router.post("/api/orders/pick/{order_id}")
def set_pick_endpoint(
    order_id: str,
    req: PickRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Tick a card as pulled, or a whole order at once.

    Touches nothing but this record: it does not move stock, and it does not
    tell eBay anything. The poller already took the card off the catalogue
    when the sale was seen -- this is only the note that the physical card is
    now in an envelope rather than on a shelf.
    """
    inv = inventory_for(user)
    if req.line_item_id:
        moved = inv.set_order_line_picked(
            order_id, req.line_item_id, req.picked
        )
        if not moved:
            raise HTTPException(
                status_code=404, detail="That order line is not on record."
            )
        changed = 1
    else:
        changed = inv.set_order_picked(order_id, req.picked)
        if not changed:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Order {order_id} has no live lines on record, so there "
                    f"is nothing to mark."
                ),
            )
    return {
        "success": True,
        "order_id": order_id,
        "picked": req.picked,
        "lines_changed": changed,
        "outstanding_cards": inv.count_outstanding_pick_lines(),
    }

async def _scheduled_order_poll() -> None:
    """
    Read orders on a schedule.

    Nothing in here may be fatal: it shares a task with nothing, but an
    exception would end the loop and the symptom would be silence -- which is
    indistinguishable from a shop with no sales. So every path returns.
    """
    inv = owner_inventory()
    try:
        client = deps.get_ebay_client(_owner_scope())
        if client is None or not client.oauth.is_connected():
            print("[orders] eBay is not connected, skipping", flush=True)
            return
        # Acts as the owner throughout: the same account whose store is
        # read and whose stock is deducted.
        result = await run_in_threadpool(
            lambda: _poll_orders_now(inv, _owner_scope())
        )
        print(
            f"[orders] {result.get('seen', 0)} line(s) seen, "
            f"{result.get('deducted', 0)} deducted, "
            f"{result.get('unmatched', 0)} matching no card",
            flush=True,
        )
    except (OrderSyncError, EbayError) as exc:
        print(f"[orders] poll failed, no stock moved: {exc}", flush=True)
        record_logs(inv, 
            [{"level": "WARN", "message": f"Order poll failed: {exc}"}],
            "orders",
        )
    except Exception as exc:
        print(f"[orders] unexpected error, no stock moved: {exc}", flush=True)
        record_logs(inv, 
            [{"level": "ERROR",
              "message": f"Unexpected order poll error: {exc}"}],
            "orders",
        )

async def order_poll_loop():
    """
    Poll for orders on its own timer, separate from the price loop.

    Separate because the two have nothing to do with each other and very
    different cadences: prices change once a day, a sale can happen at any
    moment and leaves stock on the shelf that is gone until it is read.
    """
    if not ORDER_POLL_ENABLED:
        print("[orders] background poll disabled", flush=True)
        return

    async def loop():
        # A little after the price loop's own delay, so a cold start does not
        # fire two outbound bursts at once.
        await asyncio.sleep(150)
        while True:
            await _scheduled_order_poll()
            await asyncio.sleep(ORDER_POLL_INTERVAL_MINUTES * 60)

    asyncio.create_task(loop())
