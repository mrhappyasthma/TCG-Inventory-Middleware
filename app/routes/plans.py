"""
Draft plans, and the push that acts on them.

Everything eBay-bound is staged here first. A plan is a diff between what the
catalogue says and what eBay is known to hold, computed from stored state
rather than from an uploaded file -- so it can be rebuilt at any time, and an
empty draft is the correct outcome of an upload that changed nothing.

The push is the only code in this application that changes a live listing,
and three things about it are deliberate:

* **It acts on a stored approval**, never on a claim made by the request, so
  the record of who authorised a change exists before the change does.
* **It returns immediately** and reports progress through a job, because a
  large push takes minutes and an open request outlived the reverse proxy --
  which answered 504 while the push carried on, so the page reported a
  failure that had not happened.
* **HTTP 200 is not success.** eBay's bulk calls answer 200 with per-SKU
  status in the body, so every response is read per record. `tcg_engine.push`
  owns that, and this module owns only the job and the approval gate.

The job registry is memory, so a restart forgets it. The console is the copy
that is still there tomorrow, which is why the job's log lines are written
through `record_logs` when it finishes.
"""

import asyncio
import threading
import time
import uuid
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from tcg_engine.plans import (
    PlanError,
    approve_plan,
    build_plan,
    plan_blockers,
    revalidate_item,
)
from tcg_engine.push import PushError, push_plan

try:
    from app import deps
    from app.deps import (
        EbayError,
        InventoryApiAdapter,
        inventory_for,
        record_logs,
        require_active_user,
    )
except ImportError:
    from . import deps
    from .deps import (
        EbayError,
        InventoryApiAdapter,
        inventory_for,
        record_logs,
        require_active_user,
    )

router = APIRouter()


class PlanItemUpdateRequest(BaseModel):
    """
    One edit to one planned change.

    Every field is optional so the drafts page can send just what changed.
    status is a Literal because it is stored verbatim, compared on later
    requests and rendered back into the page -- the same reasoning that made
    role and status closed sets on the user model.
    """

    proposed_qty: Optional[int] = None
    proposed_price: Optional[float] = None
    group_key: Optional[str] = None
    status: Optional[Literal["pending", "excluded"]] = None

class PlanBuildRequest(BaseModel):
    source: Literal["manual", "batch", "reprice", "photo", "grouping"] = "manual"
    note: Optional[str] = None

@router.get("/api/plans")
def list_plans(user: Dict[str, Any] = Depends(require_active_user)):
    """
    This user's recent plans, newest first.

    Scoped to the caller. Plans carry a user_id because approval is an
    authorisation record -- whose plan it was and who approved it -- so
    showing another user's drafts would let one person approve another's
    intent.
    """
    inv = inventory_for(user)
    return {"plans": inv.get_plans(user_id=user["id"])}

@router.post("/api/plans/build")
async def build_draft_plan(
    req: PlanBuildRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Compute a fresh draft plan from the current catalogue.

    Replaces any open draft: a draft is a snapshot of a diff, and once the
    catalogue moves underneath it the old draft describes a change that no
    longer applies. Runs in a threadpool because it walks the whole catalogue
    and would otherwise block the event loop and freeze the dashboard.
    """
    inv = inventory_for(user)
    try:
        summary = await run_in_threadpool(
            build_plan,
            inv,
            user["id"],
            source=req.source,
            note=req.note,
        )
    except PlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"success": True, **summary}

@router.get("/api/plans/{plan_id}")
def get_plan_detail(
    plan_id: int, user: Dict[str, Any] = Depends(require_active_user)
):
    """
    A plan with its listings, its items and everything blocking approval.

    One response rather than three round trips, because the page cannot render
    a meaningful row without all of them: an item's blockers decide how it is
    drawn.
    """
    inv = inventory_for(user)
    plan = inv.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")

    try:
        blockers = plan_blockers(inv, plan_id)
    except PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "plan": plan,
        "groups": inv.get_plan_groups(plan_id),
        "items": inv.get_plan_items(plan_id),
        "blockers": blockers,
    }

@router.patch("/api/plans/items/{item_id}")
def update_plan_item_endpoint(
    item_id: int,
    req: PlanItemUpdateRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Edit one planned change: its quantity, price, listing or inclusion.

    Only a draft may be edited. Editing an approved plan would change what is
    about to be pushed after the approval that authorised it, which makes the
    approval a record of something that never happened.
    """
    inv = inventory_for(user)
    item = inv.get_plan_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Plan item not found.")
    plan = inv.get_plan(item["plan_id"])
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan item not found.")
    if plan["status"] != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"This plan is {plan['status']} and can no longer be edited.",
        )

    changes = req.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    if "proposed_qty" in changes and changes["proposed_qty"] < 0:
        raise HTTPException(status_code=400, detail="Quantity cannot be negative.")
    if "proposed_price" in changes and changes["proposed_price"] <= 0:
        raise HTTPException(
            status_code=400,
            detail="Price must be above zero; eBay rejects a zero-price listing.",
        )

    inv.update_plan_item(item_id, **changes)
    # Re-validate straight away so the page never shows a blocker for a
    # problem the user has just fixed.
    problems = revalidate_item(inv, item_id)
    return {
        "success": True,
        "item": inv.get_plan_item(item_id),
        "problems": problems,
        "blockers": plan_blockers(inv, item["plan_id"]),
    }

class PlanCoverRequest(BaseModel):
    group_key: str
    cover_image_url: str = ""

@router.post("/api/plans/{plan_id}/cover")
def set_plan_cover(
    plan_id: int,
    req: PlanCoverRequest,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Stage a cover photo for one listing in a draft.

    Stored against the plan rather than written to ebay_listing_overrides,
    because that table is what the store currently has -- writing there would
    apply the change before it was approved. An empty URL clears the staged
    choice and the page falls back to showing the live listing's own picture.
    """
    inv = inventory_for(user)
    plan = inv.get_plan(plan_id)
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if plan["status"] != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"This plan is {plan['status']} and can no longer be edited.",
        )

    url = req.cover_image_url.strip()
    if url and not url.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="A cover photo must be an http:// or https:// URL that eBay can fetch.",
        )

    inv.set_plan_group_cover(plan_id, req.group_key, url)
    return {
        "success": True,
        "groups": inv.get_plan_groups(plan_id),
    }

@router.post("/api/plans/{plan_id}/approve")
def approve_plan_endpoint(
    plan_id: int, user: Dict[str, Any] = Depends(require_active_user)
):
    """
    Approve a plan, which is the only thing that authorises an eBay write.

    Nothing is pushed here. Approval and push are separate so that the push
    worker's authorisation check is a stored fact rather than a claim made by
    whichever request happens to be running.
    """
    inv = inventory_for(user)
    plan = inv.get_plan(plan_id)
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")
    try:
        result = approve_plan(inv, plan_id, approved_by=user["id"])
    except PlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"success": True, **result}

# A push is long: creating a hundred-card listing is a dozen eBay calls, and
# a whole plan is several listings of that. Held open as one HTTP request it
# outlasts the reverse proxy, which answers the browser with its own error
# page while the push carries on regardless -- and the page, having lost its
# request, also loses any idea that a push is happening. Switching tabs and
# back showed nothing in progress.
#
# So the request starts a job and returns immediately, and the page follows
# it by polling. That makes the two problems the same problem, and the fix is
# that progress lives on the server rather than in one browser request.
#
# Deliberately in memory. A job is a few minutes of transient state, and the
# durable record of what happened is the plan itself -- each card's status and
# reason are written as eBay answers, which is what a restart or a closed
# laptop has to be able to rely on. A vanished job is reported as unknown
# rather than as a failure, because the push it described may well have
# finished.
_push_jobs: Dict[str, Dict[str, Any]] = {}

_push_jobs_lock = threading.Lock()

# Finished jobs are kept long enough for a reload to collect the result.
PUSH_JOB_RETENTION_SECONDS = 3600

def _prune_push_jobs() -> None:
    cutoff = time.time() - PUSH_JOB_RETENTION_SECONDS
    for job_id, job in list(_push_jobs.items()):
        if job["status"] != "running" and job["finished_at"] < cutoff:
            _push_jobs.pop(job_id, None)

def _running_push_job(plan_id: int) -> Optional[str]:
    """The id of a push already running for this plan, if there is one."""
    with _push_jobs_lock:
        for job_id, job in _push_jobs.items():
            if job["plan_id"] == plan_id and job["status"] == "running":
                return job_id
    return None

def _push_job_snapshot(job_id: str, since: int = 0) -> Optional[Dict[str, Any]]:
    """
    A job's state, with only the log lines the caller has not seen.

    ``since`` is an index rather than a timestamp so that a poller cannot miss
    a line or replay one, however irregular its own timing is.
    """
    with _push_jobs_lock:
        job = _push_jobs.get(job_id)
        if job is None:
            return None
        logs = job["logs"][since:]
        return {
            "job_id": job_id,
            "plan_id": job["plan_id"],
            "group_key": job["group_key"],
            "status": job["status"],
            "started_at": job["started_at"],
            "logs": list(logs),
            "log_count": len(job["logs"]),
            "result": job["result"],
            "error": job["error"],
        }

def _run_push_job(job_id: str, plan_id: int, user_id: int, group_keys) -> None:
    """Run one push to completion, recording progress as it goes."""
    inv = inventory_for(user_id)
    def note(level: str, message: str) -> None:
        with _push_jobs_lock:
            job = _push_jobs.get(job_id)
            if job is not None:
                job["logs"].append({"level": level, "message": message})
        print(f"[push] {level}: {message}", flush=True)

    client = deps.get_ebay_client(user_id)
    adapter = InventoryApiAdapter(client)
    try:
        with inv.session():
            result = push_plan(
                inv, adapter, plan_id, user_id=user_id,
                group_keys=group_keys, log=note,
            )
        outcome, error = result, None
    except (PushError, EbayError) as exc:
        outcome, error = None, str(exc)
        note("ERROR", str(exc))
    except Exception as exc:  # noqa: BLE001 - a job must not die silently
        outcome, error = None, f"unexpected failure: {exc}"
        note("ERROR", f"unexpected failure: {exc}")

    with _push_jobs_lock:
        job = _push_jobs.get(job_id)
        if job is not None:
            job["status"] = "failed" if error else "done"
            job["result"] = outcome
            job["error"] = error
            job["finished_at"] = time.time()
        _prune_push_jobs()

    # A push outlives the page that started it by design, and the job registry
    # is memory that a restart clears. This is the copy that is still there
    # tomorrow.
    with _push_jobs_lock:
        job = _push_jobs.get(job_id)
        lines = list(job["logs"]) if job else []
    record_logs(inv, lines, "push")

class PlanPushRequest(BaseModel):
    """
    Which of a plan's listings to push.

    ``group_key`` restricts the push to one listing, which is what makes a
    first push testable: a create cannot be undone by pressing the button
    again, so one small listing goes up and is checked in Seller Hub before
    several hundred cards go live in a single call. Omitted means the whole
    plan.
    """

    group_key: Optional[str] = None

@router.post("/api/plans/{plan_id}/push")
async def push_plan_endpoint(
    plan_id: int,
    req: Optional[PlanPushRequest] = None,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    Start pushing an approved plan to eBay, and return the job that is doing it.

    The only endpoint in this application that changes a live listing. It acts
    on a *stored* approval rather than on a claim made by this request, so the
    record of who authorised the change exists before the change does.

    Returns immediately rather than holding the request open for the minutes a
    large push takes. Held open, it outlasted the reverse proxy, which answered
    the browser with an error page while the push carried on -- and the page,
    having lost its request, lost any sign that a push was running.

    Only listings created through this API are touched. Anything made through
    File Exchange is left for the CSV path and reported as deferred, since
    pushing it would create a duplicate rather than update the live one.
    """
    inv = inventory_for(user)
    plan = inv.get_plan(plan_id)
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if plan["status"] == "draft":
        raise HTTPException(
            status_code=409, detail="Approve the draft before pushing it."
        )

    client = deps.get_ebay_client(user["id"])
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="eBay is not configured, so nothing can be pushed.",
        )
    if not client.oauth.is_connected():
        raise HTTPException(
            status_code=409,
            detail=(
                "Connect the eBay account first: a push acts as the seller "
                "and needs their consent."
            ),
        )

    # One push per plan at a time. Two running together would race on the same
    # items and the same listing, and could publish the same group twice.
    existing = _running_push_job(plan_id)
    if existing is not None:
        return {"success": True, "job_id": existing, "already_running": True}

    group_keys = None
    if req is not None and req.group_key is not None:
        group_keys = [req.group_key]

    job_id = uuid.uuid4().hex
    with _push_jobs_lock:
        _push_jobs[job_id] = {
            "plan_id": plan_id,
            "group_key": group_keys[0] if group_keys else None,
            "status": "running",
            "logs": [],
            "result": None,
            "error": None,
            "started_at": time.time(),
            "finished_at": 0.0,
        }

    asyncio.create_task(run_in_threadpool(
        _run_push_job, job_id, plan_id, user["id"], group_keys
    ))
    return {"success": True, "job_id": job_id, "already_running": False}

@router.get("/api/push-jobs/{job_id}")
def get_push_job(
    job_id: str,
    since: int = 0,
    user: Dict[str, Any] = Depends(require_active_user),
):
    """
    How a push is going, and the log lines not yet collected.

    404 means the job is unknown -- most often because the server restarted,
    which says nothing about whether the push finished. The plan's own item
    statuses are the durable answer to that, so the page sends the reader
    there rather than declaring a failure it cannot know about.
    """
    inv = inventory_for(user)
    snapshot = _push_job_snapshot(job_id, since=max(0, since))
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "That push is no longer being tracked, which usually means "
                "the server restarted. Check the eBay Listings tab: the "
                "push may well have finished."
            ),
        )
    if snapshot["plan_id"] is not None:
        plan = inv.get_plan(snapshot["plan_id"])
        if plan is not None and plan["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail="Job not found.")
    return snapshot

@router.delete("/api/plans/{plan_id}")
def discard_plan_endpoint(
    plan_id: int, user: Dict[str, Any] = Depends(require_active_user)
):
    """
    Throw a plan away: a draft, or an approved plan nothing acted on.

    The line is drawn at whether the plan reached eBay, not at whether it was
    approved. An approval that was never pushed is a decision the user changed
    their mind about, and the drafts page fills up with them; a plan that was
    pushed is the only record of who authorised a live change and what it did,
    so it stays.
    """
    inv = inventory_for(user)
    plan = inv.get_plan(plan_id)
    if plan is None or plan["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if plan["status"] not in ("draft", "approved"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Plan {plan_id} is {plan['status']} -- it reached eBay, so it "
                f"is the record of that change and cannot be deleted."
            ),
        )
    inv.delete_plan(plan_id)
    return {"success": True}
