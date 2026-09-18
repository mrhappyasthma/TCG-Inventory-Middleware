"""
The catalogue: what we hold, and the SortSwift upload that grows it.

This is the authoritative record of stock. SortSwift's own numbers drift
upward from reality by design -- nothing is written back to it -- and eBay's
figures are a mirror of what eBay was last told, so neither is the answer to
"how many do I have".

Uploads are **deltas of newly scanned cards** and their quantities are
**added**. There is no replace mode: a card absent from an upload has not
sold, it simply was not in that batch. That makes duplicate-upload
protection load-bearing rather than a convenience, because re-processing a
file double-counts stock and oversells -- hence the sha256 fingerprint in
`processed_batches` and `force` as an explicit override.

The per-card edits here are the hand corrections: quantity, the bin, and the
stock target that drives the restock filter.
"""

import csv
import io
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from tcg_engine.batches import process_batch_csv
from tcg_engine.plans import PlanError, build_plan
from tcg_engine.csvtools import decode_csv_bytes

try:
    from app.deps import (
        MAX_UPLOAD_BYTES,
        inventory_for,
        read_upload_limited,
        record_logs,
        require_active_user,
        require_admin_user,
    )
except ImportError:
    from .deps import (
        MAX_UPLOAD_BYTES,
        inventory_for,
        read_upload_limited,
        record_logs,
        require_active_user,
        require_admin_user,
    )

# The longest bin/remark accepted from the dashboard. A shelf label, not a
# field for prose: an unbounded string here would reach the inventory table
# and the packing-slip column and wreck both.
REMARK_MAX_LENGTH = 60

def _csv_safe(value: Any) -> Any:
    """
    Neutralise spreadsheet formula injection for a human-facing export.

    A cell beginning =, +, - or @ is evaluated as a formula by Excel and
    Sheets, so a card name or bin note carrying one becomes code in whoever
    opens the file. Prefixing with an apostrophe makes it literal text.

    Applied ONLY to this export, which exists to be opened in a spreadsheet.
    The eBay Add/Revise files must never be touched this way: eBay parses them
    as data, and an apostrophe would corrupt a title or a price.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value

router = APIRouter()


class QuantityUpdateRequest(BaseModel):
    quantity: int

class TargetQuantityRequest(BaseModel):
    # How many copies of this card to aim to hold. None clears the override
    # so the card follows the account default again -- which is not the same
    # as zero, a deliberate "never restock this one".
    target_quantity: Optional[int] = Field(default=None, ge=0, le=999)

class RemarkUpdateRequest(BaseModel):
    # The bin or note a card is stored under. Capped because it is a shelf
    # label, not a field for prose, and an unbounded string here would end up
    # in the inventory table and the packing-slip column.
    remarks: str = Field(default="", max_length=REMARK_MAX_LENGTH)

class ConditionUpdateRequest(BaseModel):
    # The grade, as your export spells it -- "NM", "Near Mint", whatever
    # SortSwift exports. It is stored verbatim and there is deliberately no
    # translation table, so this is not a closed set: a value from one
    # vocabulary would disagree with both SortSwift's and eBay's. Capped
    # because it is rendered into a listing title.
    condition: str = Field(min_length=1, max_length=40)

class BulkRemarkRequest(BaseModel):
    """
    Set one bin/remark across every card matching an inventory filter.

    The filter fields mirror the inventory list's query parameters exactly,
    because the rows written have to be the rows the person was looking at.

    ``expect_count`` is the safety interlock. The caller sends the number of
    cards it believes it is about to change, and the request is refused if the
    server counts something else. It guards the failure that matters here: a
    filter field dropped, renamed or mistyped between the page and the server
    makes the WHERE clause match *everything*, which would silently relabel
    the entire catalogue and cannot be undone from the page. A count is cheap
    to send and turns that from a disaster into a 409.
    """

    remarks: str = Field(default="", max_length=REMARK_MAX_LENGTH)
    search: Optional[str] = None
    set_name: Optional[str] = None
    below_target: bool = False
    expect_count: int = Field(ge=0, le=100000)

class ManualCardAddRequest(BaseModel):
    product_name: str
    set_name: str
    condition: str = "Near Mint"
    printing: str = "Normal"
    quantity: int = 0
    ebay_parent_id: Optional[str] = None

@router.post("/api/process/batch")
async def process_batch_endpoint(
    file: UploadFile = File(...),
    force: bool = Form(False),
    dry_run: bool = Form(False),
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Module A: ingest a SortSwift export, catalogue the cards, stage a draft.

    It no longer produces anything to download. The catalogue is updated
    from the file and a draft plan is staged from the catalogue, which the
    drafts page reviews and the API push applies -- the only route to eBay
    there is.

    An upload is a **delta of newly scanned cards**: its quantities are added
    to what is already held, and a card absent from it means nothing at all.
    So re-processing the same file double-counts stock, which makes the
    fingerprint load-bearing -- a repeat is refused unless the caller passes
    force=true.

    Pass dry_run=true to see what a file would change without writing
    anything to the catalogue, the store mirror or a draft.

    Prices and listing settings come from the signed-in user's own rules, so
    two sellers processing the same export each get their own output.

    """
    inv = inventory_for(user)
    content_bytes = await read_upload_limited(file)
    csv_text = decode_csv_bytes(content_bytes)

    # A few thousand card rows is seconds of synchronous SQLite work. Run it in
    # a worker thread: doing it inline blocks uvicorn's event loop, which makes
    # the whole dashboard unresponsive rather than just this request. The
    # session holds one connection open for the run instead of opening and
    # closing several per card.
    def run():
        with inv.session():
            result = process_batch_csv(
                csv_text,
                inv,
                source_name=file.filename or "upload.csv",
                force=force,
                dry_run=dry_run,
                user_id=user["id"],
            )
            # Stage the draft in the same breath as the ingest. Module A used
            # to finish by handing over two files; now it finishes by leaving
            # a reviewable draft of what eBay needs, which is the only route
            # to eBay there is. Doing it here rather than inside
            # process_batch_csv keeps the engine's ingest free of any opinion
            # about plans, and avoids an import cycle.
            #
            # Skipped for a dry run, which must write nothing, and for a
            # refused duplicate, which changed nothing to re-plan against.
            if not dry_run and not result.get("duplicate"):
                try:
                    result["plan"] = build_plan(
                        inv, user["id"], source="batch",
                        source_ref=file.filename or None,
                    )
                except PlanError as exc:
                    # An ingest that succeeded must not report failure because
                    # the draft could not be built; the Rebuild button is
                    # still there.
                    result["plan"] = None
                    result["logs"].append({
                        "level": "WARN",
                        "message": (
                            f"The catalogue was updated, but the draft could "
                            f"not be staged: {exc}. Press Rebuild draft."
                        ),
                    })
            return result

    return await run_in_threadpool(run)

@router.get("/api/inventory")
def get_inventory_endpoint(
    search: Optional[str] = None,
    sort_by: str = "manifest_id",
    sort_dir: str = "ASC",
    limit: int = 50,
    offset: int = 0,
    set_name: Optional[str] = None,
    below_target: bool = False,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Fetch paginated, filtered, and sorted inventory list.

    ``below_target`` narrows it to cards held below the depth they aim for,
    which is the restock list: combined with the set filter it answers "which
    cards in this set do I still need".
    """
    # Read once and passed to both calls, so the rows and the total cannot
    # be computed against different targets.
    inv = inventory_for(user)
    target_default = inv.get_target_quantity_default(user_id=user["id"])
    items = inv.get_inventory(
        search=search,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
        set_name=set_name,
        below_target=below_target,
        target_default=target_default,
    )
    total = inv.get_inventory_count(
        search=search, set_name=set_name,
        below_target=below_target, target_default=target_default,
    )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "target_default": target_default,
        # Always the full shortfall under the current search and set, not
        # just this page's. The question is how much there is left to buy,
        # which a page cannot answer.
        "restock": inv.get_restock_summary(
            set_name=set_name, search=search, target_default=target_default
        ),
    }

@router.post("/api/inventory/{manifest_id}/target-quantity")
def set_card_target_quantity(
    manifest_id: str,
    req: TargetQuantityRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Set how many copies of one card to aim to hold.

    A target, not a ceiling: nothing refuses stock above it and no listing is
    delisted down to it. Holding more than the target simply means nothing is
    needed. Its whole purpose is the restock question.

    Sending no value clears the override, so the card follows the account
    default again.
    """
    inv = inventory_for(user)
    result = inv.set_manifest_target_quantity(manifest_id, req.target_quantity)
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found.")
    target_default = inv.get_target_quantity_default(user_id=user["id"])
    effective = (
        result["current"] if result["current"] is not None else target_default
    )
    return {
        "success": True,
        "manifest_id": manifest_id,
        "previous": result["previous"],
        "target_quantity": result["current"],
        "effective_target": effective,
        "quantity": result["quantity"],
        "needed": max(0, effective - result["quantity"]),
    }

@router.get("/api/inventory/sets")
def get_inventory_sets(user: Dict[str, Any] = Depends(require_active_user)):
    """
    Expansion sets present in the catalog, for the dashboard filter.

    Derived from the catalog rather than a fixed list, so the filter can only
    ever offer a set that actually has cards behind it.
    """
    inv = inventory_for(user)
    return {"sets": inv.get_distinct_set_names()}

@router.post("/api/inventory/{manifest_id}/quantity")
def set_card_quantity(
    manifest_id: str,
    req: QuantityUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Set a card's catalogued quantity to an absolute value.

    This is the manual correction path, so the figure supplied is the figure
    stored -- unlike batch intake, which accumulates.

    Nothing is written back to SortSwift. Its numbers drift upward from
    reality by design; this catalogue is the authoritative one, and eBay's
    orders API is the only other thing that moves a quantity.
    """
    inv = inventory_for(user)
    if req.quantity < 0:
        raise HTTPException(status_code=400, detail="Quantity cannot be negative.")

    # One existence check, not two: set_manifest_quantity already reports
    # a missing card. The card itself was only read to build the
    # deduction row that no longer exists.
    result = inv.set_manifest_quantity(manifest_id, req.quantity)
    if not result:
        raise HTTPException(status_code=404, detail="Card not found.")

    return {
        "success": True,
        "manifest_id": manifest_id,
        "previous": result["previous"],
        "current": result["current"],
    }

@router.post("/api/inventory/{manifest_id}/remark")
def set_card_remark(
    manifest_id: str,
    req: RemarkUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Set a card's bin/remark by hand.

    A **local label only**. It cannot be pushed to eBay: the bin reaches eBay
    only encoded in a variation's SKU, which is set when the listing is
    created and cannot be renamed afterwards -- eBay returns Success and
    changes nothing. So this changes where the dashboard says a card is, and
    nothing else.

    Editing it marks the value as owned here, which stops the next SortSwift
    upload from overwriting it. Clearing it hands ownership back to the
    export.
    """
    inv = inventory_for(user)
    result = inv.set_manifest_remarks(manifest_id, req.remarks)
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found.")
    return {
        "success": True,
        "manifest_id": manifest_id,
        "previous": result["previous"],
        "current": result["current"],
    }

@router.post("/api/inventory/{manifest_id}/condition")
def set_card_condition(
    manifest_id: str,
    req: ConditionUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Correct a card's condition by hand.

    The grade comes verbatim from the export and is never inferred, so this is
    for a grade that was wrong in the file. There was previously no way to fix
    one at all: re-uploading a corrected export creates a *second* card,
    because condition is part of a card's identity. People used the drafts
    page's Listing dropdown instead, moving the odd card into the listing they
    wanted -- which eBay cannot represent, since one ConditionID covers a
    whole listing.

    Two answers are refusals rather than errors, and both are shown to the
    person: a card that already exists at the target grade is named rather
    than merged into, and a card eBay already holds is changed with a warning,
    because the live listing still describes the old grade.
    """
    inv = inventory_for(user)
    result = inv.set_manifest_condition(manifest_id, req.condition)

    if result["status"] == "missing":
        raise HTTPException(status_code=404, detail="Card not found.")
    if result["status"] == "twin":
        raise HTTPException(
            status_code=409,
            detail=(
                f"{result['twin_id']} is already this card at "
                f"{req.condition.strip()}, holding {result['twin_quantity']} "
                f"cop{'y' if result['twin_quantity'] == 1 else 'ies'}. Two "
                f"cards cannot share one identity, and merging them would "
                f"have to reconcile both stock counts and any eBay link, so "
                f"it is not done for you: move the copies onto "
                f"{result['twin_id']} with the On Hand dialog and set this "
                f"card to zero."
            ),
        )

    notes: List[str] = []
    if result["changed"]:
        notes.append(
            "Rebuild the draft to re-group this card: its listing is chosen "
            "from its condition."
        )
        if result["was_live"]:
            notes.append(
                "eBay already holds this card on a listing describing the "
                "old grade, and a variation cannot be moved between "
                "listings. The next draft will propose taking it off that "
                "listing and adding it to one for its new grade."
            )
        notes.append(
            "Your export still owns this field, so correct it in SortSwift "
            "too -- otherwise the next upload mentioning this card "
            "re-creates it at the old grade."
        )

    return {
        "success": True,
        "manifest_id": manifest_id,
        "previous": result["previous"],
        "current": result["current"],
        "changed": result["changed"],
        "notes": notes,
    }

@router.post("/api/inventory/bulk-remarks")
def bulk_set_remarks_endpoint(
    req: BulkRemarkRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Set the bin/remark on every card matching a filter, in one write.

    Answers "put everything in this set, or everything matching this search,
    on shelf B" without touching eight hundred cards by hand.

    A **local label only**, exactly as for a single card: a bin reaches eBay
    only encoded in a variation's SKU, and a SKU cannot be renamed once its
    listing exists. Nothing here is pushed, no listing changes, and no plan is
    needed.

    Two things make it safe to hand a filter a write like this. The filter is
    resolved by the same builder the inventory list uses, so the rows written
    are the rows shown; and ``expect_count`` must match what the server
    counts, so a filter that silently widened is refused rather than applied.
    """
    inv = inventory_for(user)
    # The same default the list was rendered against, since below_target is
    # defined in terms of it -- a different value here would select a
    # different set of cards than the page counted.
    target_default = inv.get_target_quantity_default(user_id=user["id"])

    matched = inv.get_inventory_count(
        search=req.search,
        set_name=req.set_name,
        below_target=req.below_target,
        target_default=target_default,
    )
    if matched != req.expect_count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This would have changed {matched} card(s), but the page "
                f"expected {req.expect_count}. Nothing was written. Reload "
                f"the inventory and try again -- the filter or the catalogue "
                f"changed in between."
            ),
        )
    if matched == 0:
        raise HTTPException(
            status_code=409,
            detail="No cards match that filter, so there is nothing to set.",
        )

    result = inv.bulk_set_manifest_remarks(
        req.remarks,
        search=req.search,
        set_name=req.set_name,
        below_target=req.below_target,
        target_default=target_default,
    )
    # Recorded because it is the one edit here that touches many cards at
    # once: the console is the only place to see afterwards what a bulk
    # relabel actually did, and which filter it was aimed at.
    where = req.set_name or req.search or ("below target" if req.below_target
                                           else "the whole catalogue")
    record_logs(inv, [{
        "level": "INFO",
        "message": (
            f"Bulk bin/remark over {where}: {result['changed']} of "
            f"{result['matched']} card(s) set to "
            + (f"'{result['remarks']}'" if result["remarks"] else "no remark")
        ),
    }], "inventory")
    return {"success": True, **result}

@router.get("/api/stats")
def get_stats_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """Get catalog and stock statistics."""
    inv = inventory_for(user)
    return inv.get_stats()

@router.post("/api/inventory/add")
def add_card_manually(
    req: ManualCardAddRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Manually add or update a card in the master catalog."""
    inv = inventory_for(user)
    manifest_id, is_new, card_data = inv.get_or_create_manifest(
        req.product_name, req.set_name, req.condition, req.printing
    )
    if req.ebay_parent_id:
        inv.upsert_variation(manifest_id, req.ebay_parent_id, req.quantity)
    return {
        "success": True,
        "manifest_id": manifest_id,
        "is_new": is_new,
        "card": card_data,
    }

@router.delete("/api/inventory/{manifest_id}")
def delete_card_endpoint(
    manifest_id: str,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """Delete a card from the master catalog."""
    inv = inventory_for(user)
    success = inv.delete_manifest(manifest_id)
    if not success:
        raise HTTPException(status_code=404, detail="Card not found.")
    return {"success": True, "manifest_id": manifest_id}

@router.get("/api/export/manifest")
def export_manifest_endpoint(user: Dict[str, Any] = Depends(require_active_user)):
    """Export complete Master Catalog & Live Mirror as CSV download."""
    inv = inventory_for(user)
    items = inv.export_all_manifest()
    fieldnames = [
        "manifest_id",
        "product_name",
        "card_number",
        "set_name",
        "condition",
        "printing",
        "quantity",
        "ebay_parent_id",
        "last_known_qty",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    # This file is meant to be opened in a spreadsheet, so cells that would
    # otherwise be read as formulas are made literal first.
    writer.writerows(
        {key: _csv_safe(row.get(key)) for key in fieldnames} for row in items
    )
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=master_catalog_export.csv"},
    )
