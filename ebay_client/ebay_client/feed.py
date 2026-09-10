"""
The Feed API: asking eBay to generate a report, then downloading it.

This is how the Active Inventory report is obtained without anybody clicking
through Seller Hub. ``LMS_ACTIVE_INVENTORY_REPORT`` returns the price and
quantity of every active listing for the seller, one row per SKU, which is
exactly what a store-mirror sync needs.

**Why the Feed API rather than the Inventory API.** The Inventory API can only
see listings that were created *through* the Inventory API -- that is what
``bulkMigrateListing`` exists to change. A store built with File Exchange has
no inventory items or offers at all, so ``getOffers`` returns nothing for it.
The Feed API's LMS reports live in the same world as File Exchange and see
those listings as they are, with nothing migrated and nothing mutated.

**Why not the Trading API.** ``GetSellerList`` would also work, but it demands
a start-time window of at most 120 days, so covering a store with older
listings means paging over several windows and stitching the results. It also
returns XML that would need a second parser. The Feed report is a CSV, which
the existing report parser already handles.

The flow is asynchronous by design, because eBay generates the report on its
own schedule:

1. ``createInventoryTask`` -- ask for the report. The new task's id comes back
   in the ``Location`` header, not the body.
2. ``getInventoryTask`` -- poll until the status leaves the in-progress states.
3. ``getResultFile`` -- download it. Usually gzipped.
"""

import gzip
import io
import time
import zipfile
from typing import Any, Callable, Dict, Optional

from .errors import ApiError, EbayError

ACTIVE_INVENTORY_REPORT = "LMS_ACTIVE_INVENTORY_REPORT"

# eBay's schema version for the LMS feed types. Sent as a string because that
# is what their payload expects; a number is rejected.
DEFAULT_SCHEMA_VERSION = "1.0"

# Terminal states. COMPLETED_WITH_ERROR still produces a file, and refusing to
# read it would throw away a report that is merely partial -- which is worse
# than a partial sync, because the alternative is no sync at all.
DONE_STATUSES = frozenset({"COMPLETED", "COMPLETED_WITH_ERROR"})
FAILED_STATUSES = frozenset({"FAILED"})

DEFAULT_POLL_INTERVAL_SECONDS = 3.0
DEFAULT_POLL_TIMEOUT_SECONDS = 300.0


class FeedError(EbayError):
    """A report could not be generated or downloaded."""


def create_inventory_task(
    transport,
    feed_type: str = ACTIVE_INVENTORY_REPORT,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
    filter_payload: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Ask eBay to generate a report and return the new task id.

    The id arrives in the ``Location`` header as a URL whose last segment is
    the task id; the body is empty. Reading it from the body -- the obvious
    guess -- yields None and a confusing failure two calls later.
    """
    payload: Dict[str, Any] = {
        "feedType": feed_type,
        "schemaVersion": schema_version,
    }
    if filter_payload:
        payload["filterCriteria"] = filter_payload

    response = transport.post(
        "/sell/feed/v1/inventory_task", payload=payload, raw=True
    )
    location = response.headers.get("location", "")
    task_id = location.rstrip("/").rsplit("/", 1)[-1] if location else ""
    if not task_id:
        # Some responses do carry it in the body. Try that before giving up,
        # so a change at eBay's end degrades rather than breaks.
        try:
            body = response.json() or {}
        except (ValueError, UnicodeDecodeError):
            body = {}
        task_id = body.get("taskId") or ""
    if not task_id:
        raise FeedError(
            "eBay accepted the report request but returned no task id"
        )
    return task_id


def get_inventory_task(transport, task_id: str) -> Dict[str, Any]:
    return transport.get(f"/sell/feed/v1/inventory_task/{task_id}") or {}


def wait_for_task(
    transport,
    task_id: str,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_status: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Poll until the task finishes, and return its final record.

    Bounded by a timeout rather than looping forever: a task stuck in
    ``IN_PROGRESS`` would otherwise hold a request open until something else
    gave up, and the caller can always try again.
    """
    deadline = clock() + timeout
    last_status = ""
    while True:
        task = get_inventory_task(transport, task_id)
        status = str(task.get("status") or "").upper()
        if status != last_status:
            last_status = status
            if on_status:
                on_status(status)

        if status in DONE_STATUSES:
            return task
        if status in FAILED_STATUSES:
            detail = task.get("detailHref") or ""
            raise FeedError(
                f"eBay reported the report task as FAILED. {detail}".strip()
            )
        if clock() >= deadline:
            raise FeedError(
                f"the report was still {status or 'unknown'} after "
                f"{int(timeout)}s; try again shortly"
            )
        sleep(poll_interval)


def get_result_file(transport, task_id: str) -> bytes:
    """
    Download a finished task's file, decompressing it if needed.

    eBay returns the report gzipped, and has also been observed returning a
    zip archive. Both are unwrapped here so the caller only ever deals in
    report bytes -- otherwise every caller has to know eBay's packaging.
    """
    response = transport.request(
        "GET",
        f"/sell/feed/v1/task/{task_id}/download_result_file",
        extra_headers={"Accept": "application/octet-stream"},
        raw=True,
    )
    return decompress(response.body)


def decompress(payload: bytes) -> bytes:
    """Unwrap gzip or zip, or pass plain bytes straight through."""
    if not payload:
        return b""
    # gzip magic
    if payload[:2] == b"\x1f\x8b":
        return gzip.decompress(payload)
    # zip magic
    if payload[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            if not names:
                raise FeedError("eBay returned an empty zip archive")
            return archive.read(names[0])
    return payload


def download_active_inventory_report(
    client,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    on_status: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Request, wait for and download the Active Inventory report.

    Returns the report bytes alongside the task id and status rather than the
    bytes alone, because a COMPLETED_WITH_ERROR report is usable but the
    caller needs to know that is what it got.
    """
    transport = client.seller
    task_id = create_inventory_task(transport)
    if on_status:
        on_status("CREATED")
    task = wait_for_task(
        transport,
        task_id,
        poll_interval=poll_interval,
        timeout=timeout,
        on_status=on_status,
    )
    try:
        content = get_result_file(transport, task_id)
    except ApiError as exc:
        raise FeedError(
            f"the report finished but could not be downloaded: {exc}"
        ) from exc
    return {
        "task_id": task_id,
        "status": str(task.get("status") or "").upper(),
        "content": content,
    }
