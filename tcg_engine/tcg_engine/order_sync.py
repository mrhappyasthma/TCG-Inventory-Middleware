"""
Turn eBay's order lines into stock deductions, exactly once each.

This is the only thing that takes a sold card off the shelf. Module A used to
do it as a side effect: a card live on eBay and absent from a full inventory
dump had sold, so its quantity went to zero. That inference died with the full
dump — an upload is a batch of newly scanned cards now, and an omission means
nothing. So without this, the catalogue drifts upward from reality and the next
draft offers stock that has already gone.

Four properties, in the order they matter:

**A sale is deducted once.** The poller deliberately re-reads a window of
orders it has already seen, because overlapping is how a sale is not missed.
That makes double-counting the live hazard, and it is prevented by claiming
each ``(order_id, line_item_id)`` in the database before touching stock —
never by remembering what was read last time.

**A cancellation is reported, not reversed.** Money coming back does not put a
card on the shelf; it may already have shipped. So a line that cancels after
being deducted is surfaced for a human, in the same spirit as the repricer
holding a price drop: the direction that can lose something gets a person.

**A SKU that matches no card is never silently dropped.** It means a card
catalogued under a different id, or a listing made outside this application,
and both are worth knowing. The line is recorded with no card attached and
reported every time until it is dealt with.

**It holds nothing about the buyer.** Names, addresses, emails, phone numbers
and eBay usernames are stripped off in the app layer before anything reaches
here, which is the basis of the account-deletion exemption. This module could
not persist them if it wanted to: the only fields it receives are the six in
``REQUIRED_LINE_FIELDS``.
"""

from typing import Any, Callable, Dict, List, Optional, Sequence

from .db import Database
from .orders import base_manifest_id

# Exactly what a projected line may carry. Asserted rather than assumed,
# because the projection is a compliance boundary and an extra key here would
# be a buyer's address arriving somewhere it must never be.
REQUIRED_LINE_FIELDS = frozenset({
    "order_id", "line_item_id", "sku", "quantity", "sold_at", "status",
})

# Our own closed vocabulary, not eBay's. eBay expresses the same facts across
# cancelStatus, orderPaymentStatus and lineItemFulfillmentStatus; what this
# module needs to know is only whether the sale stands.
STATUS_ACTIVE = "ACTIVE"
STATUS_CANCELED = "CANCELED"
STATUS_REFUNDED = "REFUNDED"
STANDING_STATUSES = (STATUS_ACTIVE,)


class OrderSyncError(RuntimeError):
    """The batch was refused. No stock moved."""


def _validate(lines: Sequence[Dict[str, Any]]) -> None:
    """
    Refuse a batch whose lines are not exactly the allowed shape.

    A missing field is a bug; an *extra* one is a compliance failure, and the
    whole point of failing here rather than ignoring it is that a field which
    is merely unused today gets persisted by someone tomorrow.
    """
    for index, line in enumerate(lines):
        keys = set(line)
        if keys != REQUIRED_LINE_FIELDS:
            missing = sorted(REQUIRED_LINE_FIELDS - keys)
            extra = sorted(keys - REQUIRED_LINE_FIELDS)
            raise OrderSyncError(
                f"order line {index} has the wrong shape"
                + (f"; missing {missing}" if missing else "")
                + (f"; unexpected {extra} -- the projection must not let "
                   f"anything else through" if extra else "")
            )


def sync_orders(
    db: Database,
    lines: Sequence[Dict[str, Any]],
    log: Optional[Callable[[str, str], None]] = None,
    adopt: bool = False,
) -> Dict[str, Any]:
    """
    Record every line eBay reported, and deduct the ones newly sold.

    Returns a summary and the per-line decisions. Every line is recorded even
    when nothing is deducted, so a second poll can tell "already handled" from
    "never seen".

    ``adopt`` records the batch as accounted for **without** deducting
    anything, and exists for the very first poll. eBay serves ninety days of
    order history, and every sale in it has already been reflected in the
    catalogue one way or another -- by the sold-out sweep that used to run on
    a full inventory dump, or simply by the operator's own counts. Deducting
    that history would take three months of sales off the shelf a second
    time. So the first run adopts, and only sales seen after it are deducted.
    """
    logs: List[Dict[str, str]] = []

    def record(level: str, message: str) -> None:
        logs.append({"level": level, "message": message})
        if log is not None:
            log(level, message)

    _validate(lines)

    if not lines:
        record("INFO", "No order activity since the last poll.")
        return {
            "seen": 0, "adopted": 0, "deducted": 0, "deducted_cards": 0,
            "already": 0, "unmatched": 0, "cancelled": 0, "decisions": [],
            "logs": logs,
        }

    known = db.get_order_lines(
        [(l["order_id"], l["line_item_id"]) for l in lines]
    )

    decisions: List[Dict[str, Any]] = []
    deducted = 0
    deducted_cards = 0
    adopted = 0
    already = 0
    unmatched = 0
    cancelled = 0

    for line in lines:
        key = (line["order_id"], line["line_item_id"])
        previous = known.get(key)
        sku = str(line["sku"] or "").strip()
        quantity = max(0, int(line["quantity"] or 0))
        status = str(line["status"] or STATUS_ACTIVE).strip().upper()

        card = db.get_manifest_by_id(base_manifest_id(sku)) if sku else None
        manifest_id = card["manifest_id"] if card else None

        db.upsert_order_line(
            order_id=line["order_id"],
            line_item_id=line["line_item_id"],
            sku=sku,
            quantity=quantity,
            status=status,
            sold_at=line.get("sold_at"),
            manifest_id=manifest_id,
        )

        decision = {
            "order_id": line["order_id"],
            "line_item_id": line["line_item_id"],
            "sku": sku,
            "quantity": quantity,
            "status": status,
            "manifest_id": manifest_id,
            "card": (card or {}).get("product_name"),
            "outcome": "",
            "removed": 0,
        }

        if status not in STANDING_STATUSES:
            cancelled += 1
            decision["outcome"] = "not_a_sale"
            # Reported whether or not it was ever deducted, because the two
            # cases need different things from a person: one is a card to put
            # back, the other is a sale that never happened.
            was_deducted = bool(previous and previous.get("deducted_at"))
            record("WARN", (
                f"{line['order_id']} {sku or '(no SKU)'}: {status.lower()}"
                + (f" -- {quantity} card(s) were already deducted, so check "
                   f"whether they are back on the shelf and correct the count "
                   f"by hand" if was_deducted
                   else " -- nothing was deducted for it")
            ))
            decisions.append(decision)
            continue

        if previous and previous.get("deducted_at"):
            already += 1
            decision["outcome"] = "already_deducted"
            decisions.append(decision)
            continue

        if manifest_id is None:
            unmatched += 1
            decision["outcome"] = "no_such_card"
            record("WARN", (
                f"{line['order_id']}: sold SKU {sku or '(blank)'} matches no "
                f"catalogued card, so no stock was deducted. It was either "
                f"catalogued under another id or listed outside this "
                f"application."
            ))
            decisions.append(decision)
            continue

        if not quantity:
            decision["outcome"] = "zero_quantity"
            decisions.append(decision)
            continue

        if adopt:
            # Claimed so a later poll will not deduct it, with a recorded
            # quantity of zero because that is what was taken: nothing. The
            # honest record is "seen, accounted for, nothing removed".
            db.mark_order_line_deducted(
                line["order_id"], line["line_item_id"], 0
            )
            adopted += 1
            decision["outcome"] = "adopted"
            decisions.append(decision)
            continue

        # Claimed in the database before stock moves, so two pollers running
        # at once cannot both deduct the same sale.
        if not db.mark_order_line_deducted(
            line["order_id"], line["line_item_id"], quantity
        ):
            already += 1
            decision["outcome"] = "already_deducted"
            decisions.append(decision)
            continue

        removed = db.decrement_manifest_quantity(manifest_id, quantity)
        deducted += 1
        deducted_cards += removed
        decision["outcome"] = "deducted"
        decision["removed"] = removed

        held = db.get_manifest_by_id(manifest_id) or {}
        if removed < quantity:
            # The catalogue said it held less than eBay just sold. Clamped at
            # zero rather than going negative, and said out loud: it means the
            # count was already wrong, not that this sale was.
            record("WARN", (
                f"{line['order_id']} {sku}: sold {quantity} but the catalogue "
                f"held only {removed}, so it is now 0. The count was already "
                f"short before this sale."
            ))
        else:
            record("INFO", (
                f"{line['order_id']} {sku} {decision['card'] or ''}: "
                f"-{removed}, {held.get('quantity', '?')} left"
            ))
        decisions.append(decision)

    if adopt:
        record("WARN", (
            f"First poll: {adopted} order line(s) from eBay's 90-day "
            f"history were recorded as already accounted for, and nothing "
            f"was deducted. Deducting them would take three months of sales "
            f"off the shelf a second time. Only sales seen from now on are "
            f"deducted."
        ))
    else:
        level = "SUCCESS" if deducted else "INFO"
        record(level, (
            f"{len(lines)} order line(s) seen: {deducted} deducted "
            f"({deducted_cards} card(s)), {already} already handled, "
            f"{unmatched} matching no card, {cancelled} not a sale."
        ))

    return {
        "seen": len(lines),
        "adopted": adopted,
        "deducted": deducted,
        "deducted_cards": deducted_cards,
        "already": already,
        "unmatched": unmatched,
        "cancelled": cancelled,
        "decisions": decisions,
        "logs": logs,
    }
