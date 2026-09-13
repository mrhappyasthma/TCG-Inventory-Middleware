"""
Pricing: the tiered rules, the market feed, and the automatic repricer.

The rules are the source of truth. A price is computed when it is needed --
when a draft is built and again when it is pushed -- and never stored on a
card, which is what lets the rules be per-account without two users
conflicting.

The repricer is the part to be careful with, because it writes to live
listings unattended. Three protections, and none of them is decoration:

* **A rise applies at once; a fall is held.** The lower figure has to stay
  true for the whole hold window before it is sent, so a one-day dip does not
  reprice the shelf.
* **A boundary margin**, because the shipped price tiers are steps: at
  $0.249 the rule says $1.99 and at $0.251 it says $2.49, so a one-cent move
  in the market is a 25% move in the price. The market has to clear the edge
  by a margin before the step is taken.
* **A proportional cap on the whole run.** A repricer is downstream of a
  third-party feed, and the signature of bad feed data is that it moves
  everything at once -- so a run that would change more than the configured
  share of live cards is refused outright rather than applied.

Both background loops live here and are registered from `main`.
"""

import asyncio
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from tcg_engine.db import (
    apply_condition_multiplier,
    apply_pricing_rules,
)
from tcg_engine.pricing_feed import PriceFeedError, refresh_market_prices
from tcg_engine.repricer import (
    RepriceError,
    auto_reprice_enabled,
    plan_reprice,
    run_reprice,
)

try:
    from app import deps
    from app.deps import (
        EbayError,
        _owner_scope,
        InventoryApiAdapter,
        inventory_for,
        owner_inventory,
        record_logs,
        require_active_user,
        user_db,
    )
except ImportError:
    from . import deps
    from .deps import (
        EbayError,
        _owner_scope,
        InventoryApiAdapter,
        inventory_for,
        owner_inventory,
        record_logs,
        require_active_user,
        user_db,
    )

class PricingRuleItem(BaseModel):
    min_price: float = 0.0
    max_price: Optional[float] = None
    rule_type: str  # 'fixed', 'markup_fixed', 'markup_percent'
    rule_value: float
    sort_order: Optional[int] = 1

class ConditionMultiplierItem(BaseModel):
    condition_key: str
    multiplier: float
    label: Optional[str] = ""

router = APIRouter()


class PricingRulesUpdateRequest(BaseModel):
    rules: List[PricingRuleItem]

class ConditionMultipliersUpdateRequest(BaseModel):
    multipliers: List[ConditionMultiplierItem]

class PricePreviewRequest(BaseModel):
    price: float
    # Optional so existing callers keep working; without it the preview is the
    # mint price, which is what it always was.
    condition: Optional[str] = None

@router.get("/api/pricing-rules")
def get_pricing_rules_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """
    The pricing rules that apply to the signed-in user.

    Rules are per-user, so this returns the caller's own set if they have saved
    one and the shared baseline otherwise. ``is_own`` lets the UI say which,
    since an inherited set looks identical but resetting it does nothing.
    """
    inv = inventory_for(user)
    return {
        "rules": inv.get_pricing_rules(user_id=user["id"]),
        "is_own": inv.has_own_pricing_rules(user["id"]),
    }

@router.post("/api/pricing-rules")
def update_pricing_rules_endpoint(
    req: PricingRulesUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Save the signed-in user's own pricing rules.

    No longer admin-only: the rules belong to the user, and saving them cannot
    affect anyone else's prices. The write is scoped to the caller, so a user
    editing theirs for the first time creates their own set rather than
    changing the baseline others still inherit.
    """
    inv = inventory_for(user)
    rules_data = [r.model_dump() for r in req.rules]
    inv.set_pricing_rules(rules_data, user_id=user["id"])
    return {
        "success": True,
        "rules": inv.get_pricing_rules(user_id=user["id"]),
        "is_own": inv.has_own_pricing_rules(user["id"]),
    }

@router.post("/api/pricing-rules/reset")
def reset_pricing_rules_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """Discard the caller's own rules and inherit the shared defaults again."""
    inv = inventory_for(user)
    rules = inv.reset_default_pricing_rules(user_id=user["id"])
    return {
        "success": True,
        "rules": rules,
        "is_own": inv.has_own_pricing_rules(user["id"]),
    }

# How often the background refresh runs, and whether it runs at all. TCGCSV
# publishes once a day and asks for at most one sync per 24 hours, so anything
# under that is wasted requests against a service that asks us not to.
PRICE_REFRESH_ENABLED = os.environ.get(
    "PRICE_REFRESH_ENABLED", "true"
).strip().lower() not in ("0", "false", "no", "off")

PRICE_REFRESH_INTERVAL_HOURS = max(
    1, int(os.environ.get("PRICE_REFRESH_INTERVAL_HOURS", "24"))
)

@router.post("/api/pricing/refresh")
async def refresh_prices_endpoint(
    force: bool = Form(False),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Fetch current market prices from TCGCSV.

    Not admin-only. It writes the shared catalogue's market_price, but the
    value is objective external data rather than a preference, the write is
    idempotent, and every previous value is kept in price_history -- so no
    information can be lost and there is nothing for one user to impose on
    another.

    Runs in a worker thread: it makes outbound HTTP calls with deliberate
    spacing between them, and doing that on the event loop would freeze the
    dashboard exactly as the CSV pipelines used to.
    """
    inv = inventory_for(user)
    def run():
        with inv.session():
            return refresh_market_prices(inv, force=force)

    try:
        result = await run_in_threadpool(run)
    except PriceFeedError as exc:
        record_logs(inv, [{"level": "ERROR", "message": str(exc)}], "prices")
        raise HTTPException(status_code=502, detail=str(exc))
    record_logs(inv, result.get("logs") or [], "prices")
    return result

@router.get("/api/pricing/history/{manifest_id}")
def price_history_endpoint(
    manifest_id: str,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Recent market prices for one card, so a surprising reprice is traceable."""
    inv = inventory_for(user)
    return {"manifest_id": manifest_id,
            "history": inv.get_price_history(manifest_id)}

def _reprice_now(user_id: int, dry_run: bool = False) -> Dict[str, Any]:
    """
    One repricing round, against the listings the Inventory API can reach.

    Shared by the manual endpoint and the nightly job so there is exactly one
    code path: a preview the operator ran by hand and the run that happens at
    three in the morning must reach the same verdicts, or the preview is
    worthless.

    Its lines are printed as they happen -- the container log is still the
    first place to look when something is wrong -- and persisted in one write
    at the end, from a ``finally`` so that a run which raises part-way through
    still leaves the account of how far it got.
    """
    inv = inventory_for(user_id)
    client = deps.get_ebay_client(user_id)
    adapter = None
    if client is not None and client.oauth.is_connected():
        adapter = InventoryApiAdapter(client)
    elif not dry_run:
        raise RepriceError("Connect the eBay account first.")

    collected: List[Dict[str, str]] = []

    def log(level: str, message: str) -> None:
        print(f"[reprice] {level}: {message}", flush=True)
        collected.append({"level": level, "message": message})

    try:
        with inv.session():
            return run_reprice(
                inv, adapter, user_id=user_id, dry_run=dry_run, log=log,
            )
    finally:
        record_logs(inv, collected, "reprice")

@router.post("/api/pricing/auto-reprice")
async def auto_reprice_endpoint(
    dry_run: bool = Form(False),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Run the repricer now, or preview what it would do.

    The nightly job does this by itself; this exists so the first run can be
    inspected before being trusted, and so a hold that has just expired can be
    applied without waiting for the next cycle.

    Runs in a worker thread: it makes outbound calls to eBay in batches of 25,
    and doing that on the event loop would freeze the dashboard.
    """
    try:
        result = await run_in_threadpool(
            lambda: _reprice_now(user["id"], dry_run=dry_run)
        )
    except RepriceError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except EbayError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return result

@router.get("/api/pricing/auto-reprice/preview")
def auto_reprice_preview_endpoint(
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    What the repricer would change and what it is holding, without eBay.

    Reads nothing but our own database, so the dashboard can show the pending
    changes and the yellow-flagged holds without a network call or the risk of
    a stray write.
    """
    inv = inventory_for(user)
    planned = plan_reprice(inv, user_id=user["id"])
    return {
        "enabled": auto_reprice_enabled(inv, user_id=user["id"]),
        "considered": planned["considered"],
        "eligible": planned["eligible"],
        "change_count": len(planned["changes"]),
        "hold_count": len(planned["holds"]),
        "over_cap": planned["over_cap"],
        "change_share": planned["change_share"],
        "hold_days": planned["config"]["hold_days"],
        "changes": [
            {
                "manifest_id": d["manifest_id"],
                "label": d["label"],
                "verdict": d["verdict"],
                "market_price": d["market_price"],
                "current_price": d["current_price"],
                "target_price": d["target_price"],
                "reason": d["reason"],
            }
            for d in planned["changes"]
        ],
        "holds": [
            {
                "manifest_id": d["manifest_id"],
                "label": d["label"],
                "market_price": d["market_price"],
                "current_price": d["current_price"],
                "target_price": d["target_price"],
                "days_remaining": d["days_remaining"],
            }
            for d in planned["holds"]
        ],
    }

@router.get("/api/pricing/reprice-log")
def reprice_log_endpoint(
    limit: int = 200,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    What the repricer has actually done.

    The nightly job logs to the terminal, but a container restart takes that
    with it, and on a Synology nobody is watching the console anyway. This is
    the durable record of every verdict, including the holds.
    """
    inv = inventory_for(user)
    return {"entries": inv.get_reprice_history(limit=max(1, min(1000, limit)))}

async def _nightly_reprice() -> None:
    """
    Reprice the API-managed listings, straight after the market refresh.

    Deliberately part of the same loop iteration rather than a second timer.
    Repricing is only as good as the prices it reads, so the ordering is not
    incidental -- a separate schedule would drift and eventually reprice
    against yesterday's market for no reason anybody could see.

    It runs even when the refresh fetched nothing. A refresh is skipped when
    TCGCSV has not published since the last one, but the hold windows are
    measured on the calendar: a fall that has now waited out its window has to
    be applied on a day with no new data just the same.

    Nothing in here is allowed to be fatal. This runs inside the loop that
    also keeps market prices current, and an exception would take that with
    it.
    """
    inv = owner_inventory()
    def skip(reason: str) -> None:
        print(f"[reprice] {reason}, skipping", flush=True)
        record_logs(inv, [{
            "level": "INFO",
            "message": f"Nightly repricing skipped: {reason}.",
        }], "reprice")

    try:
        if not auto_reprice_enabled(inv):
            skip("disabled in Listing Rules")
            return

        owner = user_db.get_owner_user_id()
        if owner is None:
            skip("no admin account to act as")
            return

        client = deps.get_ebay_client(_owner_scope())
        if client is None or not client.oauth.is_connected():
            skip("eBay is not connected")
            return

        result = await run_in_threadpool(lambda: _reprice_now(owner))
        if not result.get("attempted"):
            print(f"[reprice] nothing applied: {result.get('reason')}",
                  flush=True)
        else:
            print(
                f"[reprice] applied {result['applied']}, "
                f"held {result['held']}, failed {result['failed']}, "
                f"skipped {result['skipped']} of {result['considered']}",
                flush=True,
            )
    except RepriceError as exc:
        print(f"[reprice] refused, no price changed: {exc}", flush=True)
    except Exception as exc:
        print(f"[reprice] unexpected error, prices left alone: {exc}",
              flush=True)

async def price_refresh_loop():
    """
    Refresh prices on a schedule.

    An in-process task rather than a host cron, so a Synology deployment needs
    no extra setup. The first run is delayed: a container restart should not
    fire an outbound fetch before the app is even serving. Every run is gated
    on TCGCSV's own last-updated timestamp, so a loop that wakes more often
    than they publish costs one request and changes nothing.
    """
    inv = owner_inventory()
    if not PRICE_REFRESH_ENABLED:
        print("[prices] background refresh disabled", flush=True)
        return

    async def loop():
        await asyncio.sleep(120)
        while True:
            try:
                def run():
                    with inv.session():
                        return refresh_market_prices(inv)

                result = await run_in_threadpool(run)
                record_logs(inv, result.get("logs") or [], "prices")
                if result.get("skipped"):
                    print(f"[prices] already current ({result.get('snapshot')})",
                          flush=True)
                else:
                    print(f"[prices] updated {result.get('updated')} card(s) "
                          f"from snapshot {result.get('snapshot')}", flush=True)
            except PriceFeedError as exc:
                # Never fatal: a refresh that cannot reach TCGCSV leaves every
                # stored price exactly as it was.
                print(f"[prices] refresh failed, prices unchanged: {exc}",
                      flush=True)
                record_logs(inv, [{
                    "level": "WARN",
                    "message": f"Price refresh failed, prices unchanged: {exc}",
                }], "prices")
            except Exception as exc:
                print(f"[prices] unexpected refresh error: {exc}", flush=True)
                record_logs(inv, [{
                    "level": "ERROR",
                    "message": f"Unexpected price refresh error: {exc}",
                }], "prices")

            await _nightly_reprice()
            await asyncio.sleep(PRICE_REFRESH_INTERVAL_HOURS * 3600)

    asyncio.create_task(loop())

@router.get("/api/condition-multipliers")
def get_condition_multipliers_endpoint(
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    The grade discounts that apply to the signed-in user.

    The market price we can obtain is product-level -- neither TCGplayer's
    public price data nor the SortSwift export it was relayed through breaks
    down by condition -- so the grade adjustment is policy, configured here.
    """
    inv = inventory_for(user)
    return {
        "multipliers": inv.get_condition_multipliers(user_id=user["id"]),
        "is_own": inv.has_own_condition_multipliers(user["id"]),
    }

@router.post("/api/condition-multipliers")
def update_condition_multipliers_endpoint(
    req: ConditionMultipliersUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Save the signed-in user's own grade discounts."""
    inv = inventory_for(user)
    for item in req.multipliers:
        if item.multiplier < 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The multiplier for {item.condition_key} is negative. "
                    f"A grade discount cannot invert a price."
                ),
            )
        if item.multiplier > 10:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The multiplier for {item.condition_key} is {item.multiplier}, "
                    f"which would multiply the price rather than discount it. "
                    f"Use a markup rule for that."
                ),
            )
    inv.set_condition_multipliers(
        [m.model_dump() for m in req.multipliers], user_id=user["id"]
    )
    return {
        "success": True,
        "multipliers": inv.get_condition_multipliers(user_id=user["id"]),
        "is_own": inv.has_own_condition_multipliers(user["id"]),
    }

@router.post("/api/condition-multipliers/reset")
def reset_condition_multipliers_endpoint(
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Discard the caller's own grade discounts and inherit the shared set."""
    inv = inventory_for(user)
    multipliers = inv.reset_condition_multipliers(user_id=user["id"])
    return {
        "success": True,
        "multipliers": multipliers,
        "is_own": inv.has_own_condition_multipliers(user["id"]),
    }

@router.post("/api/pricing-rules/preview")
def preview_pricing_endpoint(
    req: PricePreviewRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Test and preview what an eBay price would be for a given TCG price."""
    inv = inventory_for(user)
    multipliers = {
        m["condition_key"]: m["multiplier"]
        for m in inv.get_condition_multipliers(user_id=user["id"])
    }
    adjusted, factor = apply_condition_multiplier(
        req.price, req.condition, multipliers
    )
    calculated_price, rule = apply_pricing_rules(
        inv.get_pricing_rules(user_id=user["id"]), adjusted
    )
    return {
        "input_price": req.price,
        "condition": req.condition,
        "condition_multiplier": factor,
        "adjusted_price": round(adjusted, 2),
        "calculated_price": calculated_price,
        "matched_rule": rule,
    }
