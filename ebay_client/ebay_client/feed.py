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

import csv
import gzip
import io
import time
import xml.etree.ElementTree as ElementTree
import zipfile
from typing import Any, Callable, Dict, List, Optional

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


# The report is XML, not CSV. That is not a detail: an LMS feed type returns
# an eBay XML document, and reading it as a CSV "succeeds" -- every line
# becomes a row, no row carries a recognisable SKU column, and the sync then
# looks exactly like a store that has ended every listing. It cost a live
# store mirror to learn.
XML_PREAMBLE = b"<?xml"

# The columns the existing report parser already understands. Emitting these
# names is what lets the XML be reconciled by the same code the uploaded
# Seller Hub report goes through.
REPORT_COLUMNS = ("ItemID", "SKU", "Price", "Quantity")


def looks_like_xml(payload: bytes) -> bool:
    return payload.lstrip()[:5].lower() == XML_PREAMBLE


def _local_name(tag: str) -> str:
    """Strip the namespace eBay wraps every element in."""
    return tag.rsplit("}", 1)[-1]


def _first_text(element, *names: str) -> str:
    """The text of the first direct child matching one of ``names``."""
    for name in names:
        for child in element:
            if _local_name(child.tag) == name and (child.text or "").strip():
                return child.text.strip()
    return ""


def _records_from_sku_details(element) -> List[Dict[str, str]]:
    """
    One record per SKU inside a single SKUDetails block.

    A multi-variation listing reports its quantity and price **per
    variation**, nested inside the item's block, and the item level carries no
    SKU at all -- a blank SKU there is how eBay identifies the parent. Reading
    only the direct children therefore produced one blank-SKU row per listing:
    54 rows for a store of 4 variation listings and 50 singles, none of them
    matching anything. That is what a store mirror sync must never mistake for
    an empty store.

    Element names are matched loosely on purpose. The merchant-data schema
    calls the price StartPrice in some places and Price in others, and pinning
    one spelling is how a schema revision becomes a silent zero-match parse.
    """
    item_id = _first_text(element, "ItemID")

    variations = [
        node
        for node in element.iter()
        if _local_name(node.tag) == "Variation" and node is not element
    ]
    if variations:
        records = []
        for variation in variations:
            records.append(
                {
                    "ItemID": item_id,
                    "SKU": _first_text(variation, "SKU"),
                    "Price": _first_text(variation, "StartPrice", "Price"),
                    "Quantity": _first_text(
                        variation, "Quantity", "QuantityAvailable"
                    ),
                }
            )
        return records

    # A single-variation listing: everything is at the item level.
    return [
        {
            "ItemID": item_id,
            "SKU": _first_text(element, "SKU"),
            "Price": _first_text(element, "Price", "StartPrice"),
            "Quantity": _first_text(element, "Quantity", "QuantityAvailable"),
        }
    ]


def report_outline(payload: bytes, limit: int = 40) -> List[str]:
    """
    The element paths in a report, with no values at all.

    Purely diagnostic, and values are deliberately excluded so it is safe to
    show and to paste. Two rounds of "the report is not the shape the parser
    expects" were spent inferring structure from row counts; an outline settles
    it in one look.
    """
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return []

    seen: List[str] = []

    def walk(element, prefix: str) -> None:
        path = f"{prefix}/{_local_name(element.tag)}" if prefix else _local_name(
            element.tag
        )
        if path not in seen:
            seen.append(path)
        if len(seen) >= limit:
            return
        for child in element:
            walk(child, path)
            if len(seen) >= limit:
                return

    walk(root, "")
    return seen[:limit]


def parse_active_inventory_report(payload: bytes) -> List[Dict[str, str]]:
    """
    Pull the SKU rows out of an ActiveInventoryReport XML document.

    Namespace-agnostic on purpose: eBay declares
    ``urn:ebay:apis:eBLBaseComponents`` today, and matching on it exactly
    would turn a namespace revision into another silent zero-row parse.

    An ``Ack`` of Failure, or any Errors block, is raised rather than returned
    as an empty list -- because an empty list is indistinguishable from a
    store with nothing listed, which is the confusion that caused the damage
    in the first place.
    """
    if not payload:
        raise FeedError("eBay returned an empty report")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise FeedError(f"the report was not parseable XML: {exc}") from exc

    errors = []
    ack = ""
    for element in root.iter():
        name = _local_name(element.tag)
        if name == "Ack" and element.text:
            ack = element.text.strip()
        elif name in ("LongMessage", "ShortMessage") and element.text:
            errors.append(element.text.strip())
    if ack.lower() in ("failure", "partialfailure") and errors:
        raise FeedError("eBay reported: " + "; ".join(dict.fromkeys(errors)))

    records: List[Dict[str, str]] = []
    for element in root.iter():
        if _local_name(element.tag) != "SKUDetails":
            continue
        records.extend(_records_from_sku_details(element))

    if not records:
        raise FeedError(
            "the report parsed as XML but contained no SKUDetails entries. "
            "Its root element is "
            f"{_local_name(root.tag)!r}; the parser expected an "
            "ActiveInventoryReport."
        )
    return records


def records_to_csv(records: List[Dict[str, str]]) -> str:
    """
    Render parsed records as CSV for the existing report parser.

    An adapter rather than a second reconciler. Every rule that matters --
    carrying a variation parent's item id down to its children, skipping rows
    with no label, resolving the manifest id out of a bin-suffixed SKU, and
    above all the sweep that zeroes cards missing from the report -- lives in
    that parser. A separate path for XML would have to reimplement all of it,
    and a drift in the last rule either leaves sold-out cards on sale or
    delists a live store.

    Safe to round-trip through CSV because every field here is a machine
    value: an item id, a SKU, a price and a quantity. No free text, no card
    names, nothing a title could smuggle in.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(REPORT_COLUMNS)
    for record in records:
        writer.writerow([record.get(column, "") for column in REPORT_COLUMNS])
    return buffer.getvalue()


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
    # Normalised here rather than by the caller, so there is one place that
    # knows the report is XML and one place that converts it.
    is_xml = looks_like_xml(content)
    csv_text = (
        records_to_csv(parse_active_inventory_report(content))
        if is_xml
        else content.decode("utf-8-sig", errors="replace")
    )
    return {
        "task_id": task_id,
        "status": str(task.get("status") or "").upper(),
        "content": content,
        "was_xml": is_xml,
        "csv_text": csv_text,
    }
