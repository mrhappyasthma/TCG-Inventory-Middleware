"""
Automatic repricing of the listings this application created through the
Inventory API.

The manual path already existed: refresh market prices from TCGCSV, compute
what the pricing rules say, and download a Revise file. That file is only
usable on File Exchange listings, and it is a file -- somebody has to notice
it, download it and upload it. This module closes the loop for the listings we
can address directly, once a day, without anybody watching.

Four properties, in the order they matter:

**A price only ever moves for a reason that is still true.** Raising is close
to free: if the market recovers the card was underpriced anyway. Lowering
gives away margin that only a sale at the higher price could have earned. So
the two directions are not symmetric -- a rise applies the same day, a fall
has to keep being true for the whole hold window before it is accepted. A card
in that window is reported as a hold on every run, which is the point: it is
the one case worth looking at.

**A tier boundary is not a hair trigger.** The pricing rules are cliffs. At
the shipped rules a card whose market price is $0.249 lists at $1.99 and one
at $0.251 lists at $2.49, so a one-cent move produces a 25% price change, and
a card sitting on a boundary would be rewritten every day forever. The market
therefore has to move a configurable margin *past* a boundary before the card
changes tier. This matters more than the hold window does, because it is what
stops the churn rather than merely delaying it.

**It cannot do anything except change a price.** No quantity is sent -- not as
a repeat of the stored value, but omitted from the request entirely, which is
how eBay is told to leave a field alone. Nothing is created, published or
ended. The blast radius of a bug in here is one field on listings that already
exist.

**It refuses a run that looks like bad data.** It sits downstream of a
third-party price feed, and the signature of a feed problem is that everything
moves at once. Past a configured share of the catalogue the whole run is
abandoned, having written nothing at all.

Only API-managed listings are eligible, by construction rather than by a
check: the query joins ebay_managed_listing and requires an offer id. The four
File Exchange listings are invisible to the Inventory API, so there is nothing
here that could reach them even by accident.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .db import (
    DEFAULT_PRICE_BOUNDARY_MARGIN_PERCENT,
    DEFAULT_PRICE_HOLD_DAYS,
    DEFAULT_REPRICE_MAX_CHANGE_PERCENT,
    Database,
    SHARED_SCOPE,
    apply_condition_multiplier,
)

# What a run decided about one card.
VERDICT_RAISE = "raise"
VERDICT_DROP = "drop"
VERDICT_HOLD = "hold"
VERDICT_UNCHANGED = "unchanged"
VERDICT_SKIPPED = "skipped"

# bulkUpdatePriceQuantity, like every bulk call in the Inventory API, takes at
# most 25 records.
BULK_LIMIT = 25

# Below this many eligible cards the proportional cap is not applied, because
# the ratio carries no information: one card out of three is 33% and means
# nothing. The guard exists to catch a feed that has moved the whole
# catalogue, and a handful of cards is not that. The absolute exposure it
# declines to protect is correspondingly small.
CAP_MINIMUM_CARDS = 20

# How SQLite's CURRENT_TIMESTAMP renders, which is what the hold clock is
# compared against. UTC, no timezone suffix.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class RepriceError(RuntimeError):
    """A run was abandoned. No price was changed and no hold clock moved."""


def utcnow() -> datetime:
    """The clock the hold window is measured on, to the second."""
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)


def format_timestamp(moment: datetime) -> str:
    return moment.strftime(TIMESTAMP_FORMAT)


def parse_timestamp(value: Any) -> Optional[datetime]:
    """
    Read a stored timestamp, or None if it cannot be read.

    Unreadable is treated as absent rather than as ancient. The alternative --
    falling back to ``datetime.min`` -- would make every malformed value look
    like a hold that expired long ago, and mark the card down on the next run.
    """
    if isinstance(value, datetime):
        return value.replace(microsecond=0, tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in (TIMESTAMP_FORMAT, "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(microsecond=0)
        except ValueError:
            continue
    return None


def _price_from_rule(rule: Dict[str, Any], base: float) -> Optional[float]:
    """What one rule would price a card at. None for a rule type we do not know."""
    kind = rule.get("rule_type")
    value = float(rule.get("rule_value") or 0.0)
    if kind == "fixed":
        return round(value, 2)
    if kind == "markup_fixed":
        return round(base + value, 2)
    if kind == "markup_percent":
        return round(base * (1.0 + value / 100.0), 2)
    return None


def _rule_matches(rule: Dict[str, Any], base: float) -> bool:
    """The same containment test Database.calculate_price uses."""
    low = float(rule["min_price"])
    high = rule["max_price"]
    if high is None:
        return base >= low
    return low <= base < float(high)


def matching_index(rules: Sequence[Dict[str, Any]], base: float) -> Optional[int]:
    """Which rule covers this base price, by position, or None if none does."""
    for index, rule in enumerate(rules):
        if _rule_matches(rule, base):
            return index
    return None


def _rule_can_emit(rule: Dict[str, Any], price: float) -> bool:
    """
    Whether this rule could have produced a listed price.

    This is how the tier a card is *currently* priced from is recovered
    without storing it. For a fixed rule the answer is exact; for a markup the
    rule's output is a range, so the listed price is run back through the
    markup and the implied market price tested against the rule's own bounds.
    """
    kind = rule.get("rule_type")
    value = float(rule.get("rule_value") or 0.0)
    if kind == "fixed":
        return abs(price - round(value, 2)) < 0.005
    if kind == "markup_fixed":
        implied = price - value
    elif kind == "markup_percent":
        divisor = 1.0 + value / 100.0
        if divisor <= 0:
            return False
        implied = price / divisor
    else:
        return False
    # A cent of slack at each end: the stored price was rounded when it was
    # computed, so the implied base will not land exactly on a boundary.
    low = float(rule["min_price"]) - 0.01
    high = rule["max_price"]
    if high is None:
        return implied >= low
    return low <= implied < float(high) + 0.01


def holding_index(
    rules: Sequence[Dict[str, Any]], price: Optional[float]
) -> Optional[int]:
    """Which rule the card's current listed price came from, if any."""
    if price is None:
        return None
    for index, rule in enumerate(rules):
        if _rule_can_emit(rule, float(price)):
            return index
    return None


def target_price(
    rules: Sequence[Dict[str, Any]],
    adjusted: float,
    current_price: Optional[float],
    margin_fraction: float,
) -> Tuple[Optional[float], Optional[int], Optional[str]]:
    """
    The price the rules give for this market price, damped at the boundaries.

    Returns (price, rule_index, damping_note). A price of None means no rule
    covers this market price: the caller must skip the card rather than price
    it, because the untiered fallback is the raw market value and listing a
    20-cent card at 20 cents is worse than leaving it alone.

    The damping: if the card is already priced from a different tier and the
    market has crossed that tier's boundary by less than the margin, it stays
    where it is. Expressed against the boundary rather than against the price
    so that it reads the same in both directions, and so the margin means the
    same thing whatever the tier's markup happens to be.
    """
    matched = matching_index(rules, adjusted)
    if matched is None:
        return None, None, None

    computed = _price_from_rule(rules[matched], adjusted)
    if computed is None:
        return None, None, None
    if current_price is None or margin_fraction <= 0:
        return computed, matched, None

    current = float(current_price)
    # Already consistent with the tier the market is in -- there is no
    # boundary crossing to damp, only the ordinary movement inside a tier.
    if _rule_can_emit(rules[matched], current):
        return computed, matched, None

    held = holding_index(rules, current)
    if held is None or held == matched:
        return computed, matched, None

    rule = rules[held]
    low = float(rule["min_price"])
    high = rule["max_price"]

    boundary: Optional[float] = None
    threshold: Optional[float] = None
    crossed_up = False
    if high is not None and adjusted >= float(high):
        boundary = float(high)
        threshold = boundary * (1.0 + margin_fraction)
        crossed_up = True
    elif adjusted < low:
        boundary = low
        threshold = boundary * (1.0 - margin_fraction)
        crossed_up = False
    if boundary is None or threshold is None or boundary <= 0:
        return computed, matched, None

    inside = adjusted < threshold if crossed_up else adjusted > threshold
    if not inside:
        return computed, matched, None

    stayed = _price_from_rule(rule, adjusted)
    if stayed is None:
        return computed, matched, None
    note = (
        f"market ${adjusted:.2f} is within {margin_fraction * 100:.0f}% of the "
        f"${boundary:.2f} tier boundary, so the tier did not change"
    )
    return stayed, held, note


def reprice_settings(
    db: Database, user_id: int = SHARED_SCOPE
) -> Dict[str, float]:
    """
    The repricer's three numbers, parsed, with the shipped defaults on
    anything unreadable.

    A blank or corrupt setting must not disable a safety threshold, so an
    unparseable cap falls back to the default rather than to "no cap".
    """
    settings = db.get_listing_settings(user_id=user_id)

    def number(key: str, fallback: str) -> float:
        try:
            return float(str(settings.get(key, "")).strip() or fallback)
        except (TypeError, ValueError):
            return float(fallback)

    margin = number("price_boundary_margin_percent",
                    DEFAULT_PRICE_BOUNDARY_MARGIN_PERCENT)
    hold_days = number("price_hold_days", DEFAULT_PRICE_HOLD_DAYS)
    cap = number("reprice_max_change_percent",
                 DEFAULT_REPRICE_MAX_CHANGE_PERCENT)
    return {
        "margin_fraction": max(0.0, margin) / 100.0,
        "hold_days": max(0.0, hold_days),
        "cap_fraction": max(0.0, cap) / 100.0,
    }


def auto_reprice_enabled(db: Database, user_id: int = SHARED_SCOPE) -> bool:
    value = db.get_listing_setting("auto_reprice_enabled", "true", user_id=user_id)
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _label(card: Dict[str, Any]) -> str:
    """How a card is named in a log line: enough to find it on eBay."""
    parts = [str(card.get("product_name") or "").strip()]
    number = str(card.get("card_number") or "").strip()
    if number:
        parts.append(f"#{number}")
    name = " ".join(p for p in parts if p)
    return f"{card['manifest_id']} {name}".strip()


def decide_card(
    card: Dict[str, Any],
    *,
    rules: Sequence[Dict[str, Any]],
    multipliers: Dict[str, float],
    margin_fraction: float,
    hold_days: float,
    now: datetime,
) -> Dict[str, Any]:
    """
    What should happen to one card's price, and why.

    Pure: everything it needs is in the arguments, so the hold window and the
    boundary damping are testable without a database or a clock.
    """
    decision: Dict[str, Any] = {
        "manifest_id": card["manifest_id"],
        "sku": (str(card.get("custom_label") or "").strip()
                or card["manifest_id"]),
        "offer_id": str(card.get("offer_id") or "").strip(),
        "ebay_parent_id": str(card.get("ebay_parent_id") or "").strip(),
        "label": _label(card),
        "market_price": None,
        "adjusted_price": None,
        "current_price": None,
        "target_price": None,
        "verdict": VERDICT_SKIPPED,
        "reason": "",
        "hold_since": None,
        "clear_hold": False,
        "days_held": None,
        "days_remaining": None,
    }

    market = float(card.get("market_price") or 0.0)
    if market <= 0:
        decision["reason"] = (
            "no market price is stored, so there is nothing to price from"
        )
        return decision
    decision["market_price"] = round(market, 4)

    current = card.get("last_known_price")
    if current is None:
        # Without eBay's own number there is no way to tell a rise from a
        # fall, which is the one distinction this whole module turns on.
        decision["reason"] = (
            "eBay's current price for this card is not known, so a rise "
            "cannot be told from a fall"
        )
        return decision
    current = round(float(current), 2)
    decision["current_price"] = current

    adjusted, _factor = apply_condition_multiplier(
        market, card.get("condition"), multipliers
    )
    decision["adjusted_price"] = round(adjusted, 4)

    target, _index, damping = target_price(
        rules, adjusted, current, margin_fraction
    )
    if target is None:
        decision["reason"] = (
            f"no pricing rule covers a market price of ${adjusted:.2f}, so it "
            f"was left alone rather than priced from nothing"
        )
        return decision
    target = round(float(target), 2)
    decision["target_price"] = target

    if target == current:
        decision["verdict"] = VERDICT_UNCHANGED
        decision["clear_hold"] = card.get("hold_since") is not None
        decision["reason"] = damping or "already at the price the rules compute"
        return decision

    if target > current:
        decision["verdict"] = VERDICT_RAISE
        decision["clear_hold"] = card.get("hold_since") is not None
        decision["reason"] = (
            f"the market supports more than the listed price"
            + (f"; {damping}" if damping else "")
        )
        return decision

    # A fall. This is the case the hold window exists for.
    if hold_days <= 0:
        decision["verdict"] = VERDICT_DROP
        decision["clear_hold"] = card.get("hold_since") is not None
        decision["reason"] = "the market has fallen and no hold window is set"
        return decision

    started = parse_timestamp(card.get("hold_since"))
    if started is None:
        decision["verdict"] = VERDICT_HOLD
        decision["hold_since"] = format_timestamp(now)
        decision["days_held"] = 0.0
        decision["days_remaining"] = hold_days
        decision["reason"] = (
            f"the market has fallen; holding ${current:.2f} for "
            f"{hold_days:.0f} more day(s) before dropping to ${target:.2f}"
        )
        return decision

    elapsed = (now - started).total_seconds() / 86400.0
    decision["days_held"] = round(max(0.0, elapsed), 2)
    if elapsed >= hold_days:
        decision["verdict"] = VERDICT_DROP
        decision["clear_hold"] = True
        decision["reason"] = (
            f"the computed price stayed below ${current:.2f} for "
            f"{elapsed:.0f} day(s), which is the whole hold window"
        )
        return decision

    decision["verdict"] = VERDICT_HOLD
    decision["days_remaining"] = round(hold_days - elapsed, 2)
    decision["reason"] = (
        f"holding ${current:.2f} against a computed ${target:.2f} for another "
        f"{decision['days_remaining']:.0f} day(s)"
    )
    return decision


def plan_reprice(
    db: Database,
    *,
    user_id: int = SHARED_SCOPE,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Every verdict a run would reach, without touching eBay or the database.

    Split from the apply step so the same decisions can be previewed. The
    proportional cap is evaluated here too, so a preview says the run would be
    refused instead of that refusal only showing up at push time.
    """
    moment = now or utcnow()
    rules = db.get_pricing_rules(user_id=user_id)
    multipliers = {
        m["condition_key"]: m["multiplier"]
        for m in db.get_condition_multipliers(user_id=user_id)
    }
    config = reprice_settings(db, user_id=user_id)

    cards = db.get_managed_cards_for_repricing()
    decisions = [
        decide_card(
            card,
            rules=rules,
            multipliers=multipliers,
            margin_fraction=config["margin_fraction"],
            hold_days=config["hold_days"],
            now=moment,
        )
        for card in cards
    ]

    changes = [d for d in decisions
               if d["verdict"] in (VERDICT_RAISE, VERDICT_DROP)]
    eligible = [d for d in decisions if d["verdict"] != VERDICT_SKIPPED]
    holds = [d for d in decisions if d["verdict"] == VERDICT_HOLD]

    share = (len(changes) / len(eligible)) if eligible else 0.0
    over_cap = (
        len(eligible) >= CAP_MINIMUM_CARDS
        and config["cap_fraction"] > 0
        and share > config["cap_fraction"]
    )

    return {
        "decisions": decisions,
        "changes": changes,
        "holds": holds,
        "considered": len(cards),
        "eligible": len(eligible),
        "change_share": round(share, 4),
        "over_cap": over_cap,
        "config": config,
        "now": moment,
    }


def _price_requests(changes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    bulkUpdatePriceQuantity records that carry a price and nothing else.

    ``shipToLocationAvailability`` is absent on purpose. eBay leaves out what
    it is not sent, so omitting it is the difference between "change the
    price" and "change the price and set the quantity to whatever we last
    believed" -- and what we last believed is exactly the thing that goes
    stale between a sale and the next sync.
    """
    return [
        {
            "sku": change["sku"],
            "offers": [{
                "offerId": change["offer_id"],
                "price": {
                    "value": f"{change['target_price']:.2f}",
                    "currency": "USD",
                },
            }],
        }
        for change in changes
    ]


def run_reprice(
    db: Database,
    api: Any = None,
    *,
    user_id: int = SHARED_SCOPE,
    now: Optional[datetime] = None,
    log: Optional[Callable[[str, str], None]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Decide, then apply, one round of repricing.

    Every verdict is logged and written to reprice_history, including the ones
    that changed nothing, because an unattended job's own account of what it
    did is the only thing that can answer "why is this card priced at this".
    Holds are logged at WARN so they stand out: a card whose price is being
    held above the market is the one thing here worth a human glance.
    """
    logs: List[Dict[str, str]] = []

    def record(level: str, message: str) -> None:
        logs.append({"level": level, "message": message})
        if log is not None:
            log(level, message)

    planned = plan_reprice(db, user_id=user_id, now=now)
    decisions = planned["decisions"]
    changes = planned["changes"]
    holds = planned["holds"]
    config = planned["config"]

    if not decisions:
        record("INFO", (
            "No API-managed listing has a price to review. Cards on File "
            "Exchange listings are deliberately not eligible."
        ))
        return {
            "attempted": False, "reason": "no eligible cards",
            "applied": 0, "failed": 0, "held": 0, "skipped": 0,
            "considered": 0, "logs": logs, "decisions": [],
        }

    for decision in decisions:
        if decision["verdict"] == VERDICT_HOLD:
            record("WARN", (
                f"HOLD {decision['label']}: keeping "
                f"${decision['current_price']:.2f} against a computed "
                f"${decision['target_price']:.2f} "
                f"(market ${decision['market_price']:.2f}), "
                f"{decision['days_remaining']:.0f} day(s) left"
            ))
        elif decision["verdict"] == VERDICT_SKIPPED:
            record("INFO", f"SKIP {decision['label']}: {decision['reason']}")

    if planned["over_cap"]:
        share = planned["change_share"] * 100
        cap = config["cap_fraction"] * 100
        record("ERROR", (
            f"Refusing to reprice: {len(changes)} of {planned['eligible']} "
            f"card(s) ({share:.0f}%) would change, which is over the "
            f"{cap:.0f}% cap. That pattern is what bad market data looks "
            f"like, so nothing was changed and no hold was started. Check "
            f"the market prices, then raise the cap if they are right."
        ))
        db.record_reprice([
            {
                "manifest_id": d["manifest_id"],
                "ebay_parent_id": d["ebay_parent_id"],
                "verdict": d["verdict"],
                "market_price": d["market_price"],
                "old_price": d["current_price"],
                "new_price": d["target_price"],
                "reason": "run refused by the proportional cap",
                "applied": False,
            }
            for d in decisions
        ])
        return {
            "attempted": False, "reason": "over the proportional cap",
            "applied": 0, "failed": 0, "held": len(holds),
            "skipped": sum(1 for d in decisions
                           if d["verdict"] == VERDICT_SKIPPED),
            "considered": planned["considered"],
            "change_share": planned["change_share"],
            "logs": logs, "decisions": decisions,
        }

    for decision in changes:
        direction = "UP  " if decision["verdict"] == VERDICT_RAISE else "DOWN"
        record("INFO", (
            f"{direction} {decision['label']}: "
            f"${decision['current_price']:.2f} -> "
            f"${decision['target_price']:.2f} "
            f"(market ${decision['market_price']:.2f}) -- {decision['reason']}"
        ))

    if dry_run:
        record("INFO", (
            f"Preview only: {len(changes)} price change(s) and "
            f"{len(holds)} hold(s). Nothing was sent to eBay."
        ))
        return {
            "attempted": False, "reason": "preview",
            "applied": 0, "failed": 0, "held": len(holds),
            "skipped": sum(1 for d in decisions
                           if d["verdict"] == VERDICT_SKIPPED),
            "considered": planned["considered"],
            "changes": len(changes),
            "logs": logs, "decisions": decisions,
        }

    failed: Dict[str, str] = {}
    if changes:
        if api is None:
            raise RepriceError(
                "There is no connected eBay account to send price changes to."
            )
        requests = _price_requests(changes)
        for start in range(0, len(requests), BULK_LIMIT):
            rows = api.update_price_quantity(requests[start:start + BULK_LIMIT])
            for sku, message in api.failures(rows):
                failed[sku] = message

    applied = 0
    entries: List[Dict[str, Any]] = []
    for decision in decisions:
        verdict = decision["verdict"]
        was_applied = False

        if verdict in (VERDICT_RAISE, VERDICT_DROP):
            reason = failed.get(decision["sku"])
            if reason:
                record("ERROR", f"{decision['label']}: {reason}")
                decision["verdict"] = VERDICT_SKIPPED
                decision["reason"] = reason
            else:
                # eBay accepted it, so this is now eBay's price. Written
                # through a setter that touches nothing else: a pending
                # quantity request from a plan must survive a price change.
                db.set_variation_known_price(
                    decision["manifest_id"], decision["target_price"]
                )
                applied += 1
                was_applied = True

        if decision["hold_since"]:
            db.set_variation_hold_since(
                decision["manifest_id"], decision["hold_since"]
            )
        elif decision["clear_hold"] or was_applied:
            db.set_variation_hold_since(decision["manifest_id"], None)

        entries.append({
            "manifest_id": decision["manifest_id"],
            "ebay_parent_id": decision["ebay_parent_id"],
            "verdict": decision["verdict"],
            "market_price": decision["market_price"],
            "old_price": decision["current_price"],
            "new_price": decision["target_price"],
            "reason": decision["reason"],
            "applied": was_applied,
        })

    db.record_reprice(entries)

    skipped = sum(1 for d in decisions if d["verdict"] == VERDICT_SKIPPED)
    level = "ERROR" if failed else ("SUCCESS" if applied else "INFO")
    record(level, (
        f"Repriced {applied} card(s), {len(failed)} refused by eBay, "
        f"{len(holds)} in a holding window, {skipped} skipped, out of "
        f"{planned['considered']} reviewed."
    ))
    if holds:
        record("WARN", (
            f"{len(holds)} card(s) are priced above what the market now "
            f"supports, waiting out the {config['hold_days']:.0f}-day hold "
            f"window. They are listed above."
        ))

    return {
        "attempted": True,
        "applied": applied,
        "failed": len(failed),
        "held": len(holds),
        "skipped": skipped,
        "considered": planned["considered"],
        "change_share": planned["change_share"],
        "logs": logs,
        "decisions": decisions,
    }
