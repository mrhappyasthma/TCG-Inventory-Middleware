"""
The eBay listings mirror: what eBay was last known to hold.

Derived rather than stored separately. `ebay_variations` is keyed by card, so
this rolls up by eBay item number to present the store the way eBay does.

The figures here are **eBay's**, not ours. A gap against the catalogue is
what a draft plan proposes to change, and the direction of that gap matters:
our count is authoritative for what is on the shelf, eBay's is authoritative
for what is offered for sale.

Module B reconciles the two from eBay's Active Listings report -- uploaded,
or fetched through the Feed API. A report that matches nothing never zeroes
the mirror: that shape is indistinguishable from a report for a different
account, and acting on it would delist the whole store.
"""

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from tcg_engine.csvtools import decode_csv_bytes
from tcg_engine.push import PushError, refresh_listing
from tcg_engine.sync import sync_active_listings_csv

try:
    from app import deps
    from app.deps import (
        EbayError,
        InventoryApiAdapter,
        inventory_for,
        read_upload_limited,
        record_logs,
        require_active_user,
    )
except ImportError:
    from . import deps
    from .deps import (
        EbayError,
        InventoryApiAdapter,
        inventory_for,
        read_upload_limited,
        record_logs,
        require_active_user,
    )

router = APIRouter()


class CoverImageRequest(BaseModel):
    cover_image_url: str

@router.post("/api/process/sync")
async def process_sync_endpoint(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Module B: Ingest eBay Active Listings report CSV & sync live store mirror state.
    """
    inv = inventory_for(user)
    content_bytes = await read_upload_limited(file)
    csv_text = decode_csv_bytes(content_bytes)

    def run():
        with inv.session():
            return sync_active_listings_csv(csv_text, inv)

    return await run_in_threadpool(run)

@router.get("/api/ebay-listings")
def get_ebay_listings_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """
    Live eBay listings, rolled up from the store mirror.

    Derived rather than stored: the mirror is keyed by card, so this groups by
    eBay item number to show the store the way eBay presents it.
    """
    # Which path manages each listing, so the page can offer the right
    # action. A listing created through this API is corrected in place; a File
    # Exchange one needs a Revise file uploaded, and the two are not
    # interchangeable -- offering the wrong one hands out a file that silently
    # does nothing, or an API call eBay refuses.
    inv = inventory_for(user)
    managed = {
        row["ebay_parent_id"]
        for row in inv.get_managed_listings()
        if row.get("ebay_parent_id")
    }
    return {
        "listings": [
            {**listing, "managed": listing["ebay_parent_id"] in managed}
            for listing in inv.get_ebay_listings()
        ]
    }

@router.post("/api/ebay-listings/{item_id}/cover")
async def set_listing_cover(
    item_id: str,
    req: CoverImageRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Record a listing's cover photo, and apply it however that listing allows.

    Saving locally is never enough on its own: the listing lives on eBay. How
    the change gets there depends on which path created the listing, and the
    two are not interchangeable -- File Exchange cannot revise a listing the
    Inventory API manages, so handing out a CSV for one of those would be
    handing out something that silently does nothing.

    A listing we created through the API is therefore updated immediately, and
    a legacy one still gets the Revise file to upload.
    """
    inv = inventory_for(user)
    url = (req.cover_image_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="A cover photo URL is required.")
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="The cover photo must be a full http:// or https:// URL that eBay can fetch.",
        )

    known = {l["ebay_parent_id"] for l in inv.get_ebay_listings()}
    if item_id not in known:
        raise HTTPException(
            status_code=404,
            detail="No linked listing with that eBay item number.",
        )

    saved = inv.set_listing_cover_image(item_id, url)

    # The choice is recorded first, so that a failure to apply it still leaves
    # it stored and retryable with the listing's Refresh button.
    if inv.get_managed_listing_by_parent(item_id) is None:
        return {
            "success": True,
            "ebay_parent_id": item_id,
            "cover_image_url": saved,
            "applied": False,
            "reason": (
                "Saved, but this listing is not managed through the eBay API, "
                "so there is no way to apply it. Sync from eBay, then press "
                "Refresh on the listing."
            ),
        }

    client = deps.get_ebay_client(user["id"])
    if client is None or not client.oauth.is_connected():
        return {
            "success": True,
            "ebay_parent_id": item_id,
            "cover_image_url": saved,
            "applied": False,
            "reason": (
                "Saved, but eBay is not connected, so it has not been applied "
                "yet. Connect the account and press Refresh on the listing."
            ),
        }

    adapter = InventoryApiAdapter(client)

    def run():
        with inv.session():
            return refresh_listing(inv, adapter, item_id, user_id=user["id"])

    try:
        result = await run_in_threadpool(run)
    except (PushError, EbayError) as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"The cover was saved but eBay refused the update: {exc}. "
                f"Press Refresh on the listing to try again."
            ),
        )
    return {
        "success": True,
        "ebay_parent_id": item_id,
        "cover_image_url": saved,
        "applied": True,
        "refreshed": result.get("refreshed", 0),
    }

@router.post("/api/ebay-listings/{item_id}/refresh")
async def refresh_listing_endpoint(
    item_id: str, user: Dict[str, Any] = Depends(require_active_user)
):
    """
    Re-send a live listing's pictures, specifics, title and description.

    These live on the inventory items rather than on a plan, so once a listing
    is up there is no route to them through the drafts page: a plan is a diff
    of quantities and prices, and a picture change produces no diff at all.
    The first listing this application created went live carrying the card
    backs, and nothing could reach in to correct it.

    Deliberately cannot move stock or money: quantity is re-sent as what eBay
    is already known to hold, and no price is sent at all.
    """
    inv = inventory_for(user)
    client = deps.get_ebay_client(user["id"])
    if client is None or not client.oauth.is_connected():
        raise HTTPException(
            status_code=409, detail="Connect the eBay account first."
        )

    adapter = InventoryApiAdapter(client)

    def run():
        with inv.session():
            return refresh_listing(inv, adapter, item_id, user_id=user["id"])

    try:
        result = await run_in_threadpool(run)
    except PushError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except EbayError as exc:
        raise HTTPException(status_code=502, detail=f"eBay refused: {exc}")

    for entry in result.get("logs", []):
        print(f"[refresh] {entry['level']}: {entry['message']}", flush=True)
    return {"success": True, **result}
