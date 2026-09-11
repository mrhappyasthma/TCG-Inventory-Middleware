"""
The Inventory API calls that create and update a listing.

This is the write surface the drafts page has been staging for. It covers the
two things we do to a listing we manage ourselves -- create it, and change it
-- and deliberately nothing else: the legacy listings created through File
Exchange are not reachable from here at all (eBay cannot see them in this
model until they are migrated), so they stay on the CSV path until that is a
separate, deliberate decision.

Three properties are worth more than the size of this module.

**A 200 does not mean it worked.** The bulk calls answer HTTP 200 and report
success or failure *per SKU* inside the body. Treating the status code as the
answer is how a push reports success while half the cards silently kept their
old price. ``bulk_statuses`` pulls those rows out in one shape so no caller
has to remember which key eBay used this time, and the bulk helpers refuse to
hide a partial failure.

**Batch limits are enforced, not assumed.** eBay caps the bulk calls at 25
records. Sending 26 fails the whole call, so ``chunked`` splits the work and
the limits are named constants rather than numbers buried in a loop.

**Nothing here knows what a card is.** The vocabulary is SKUs, offers and
inventory item groups. Translation from manifest ids, bins and pricing rules
happens in the layer above, which is what keeps this testable with no network
and no database.
"""

from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from .errors import ApiError

INVENTORY_BASE = "/sell/inventory/v1"

# eBay's documented ceilings for the bulk calls. Exceeding one fails the
# entire request rather than the surplus, so they are enforced here.
BULK_INVENTORY_ITEM_LIMIT = 25
BULK_PRICE_QUANTITY_LIMIT = 25
BULK_OFFER_LIMIT = 25
# Migration's ceiling is five, not twenty-five. It is also the only bulk call
# here that is irreversible, which is why callers are expected to pass one.
BULK_MIGRATE_LIMIT = 5

# A SKU longer than this is rejected by eBay. Ours are manifest ids with an
# optional bin suffix, so this should never fire -- but it fires here, naming
# the SKU, rather than as a generic 400 on a 25-record batch.
MAX_SKU_LENGTH = 50


def chunked(items: Sequence[Any], size: int) -> Iterator[List[Any]]:
    """Split a sequence into batches no larger than ``size``."""
    if size < 1:
        raise ValueError("batch size must be at least 1")
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


def validate_sku(sku: str) -> str:
    """Reject a SKU eBay will refuse, while we still know which one it was."""
    cleaned = str(sku or "").strip()
    if not cleaned:
        raise ValueError("a SKU is required")
    if len(cleaned) > MAX_SKU_LENGTH:
        raise ValueError(
            f"SKU {cleaned!r} is {len(cleaned)} characters; eBay allows "
            f"{MAX_SKU_LENGTH}"
        )
    return cleaned


# -- inventory items -----------------------------------------------------


def create_or_replace_inventory_item(
    transport, sku: str, payload: Dict[str, Any]
) -> None:
    """
    Create or overwrite one inventory item.

    A full replace, not a merge: eBay stores exactly what is sent, so a field
    omitted here is a field removed from the item. The caller assembles the
    whole record every time for that reason.
    """
    transport.put(f"{INVENTORY_BASE}/inventory_item/{validate_sku(sku)}", payload)


def get_inventory_item(transport, sku: str) -> Dict[str, Any]:
    """Read one inventory item back, for verifying what a push landed."""
    return transport.get(
        f"{INVENTORY_BASE}/inventory_item/{validate_sku(sku)}"
    ) or {}


def bulk_create_or_replace_inventory_item(
    transport, items: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Create or overwrite up to 25 inventory items in one call.

    Each entry is ``{"sku": ..., "product": {...}, ...}``. Returns the
    per-SKU response rows; use ``failed_statuses`` to find the ones that did
    not take, because the call itself answers 200 either way.
    """
    if not items:
        return []
    if len(items) > BULK_INVENTORY_ITEM_LIMIT:
        raise ValueError(
            f"{len(items)} inventory items exceeds eBay's limit of "
            f"{BULK_INVENTORY_ITEM_LIMIT} per call; use chunked()"
        )
    for entry in items:
        validate_sku(entry.get("sku", ""))
    payload = {"requests": list(items)}
    response = transport.post(
        f"{INVENTORY_BASE}/bulk_create_or_replace_inventory_item", payload
    ) or {}
    return bulk_statuses(response)


# -- offers --------------------------------------------------------------


def create_offer(transport, payload: Dict[str, Any]) -> str:
    """
    Create an unpublished offer and return its id.

    An offer is the saleable proposition -- price, quantity, marketplace,
    policies -- attached to a SKU. Creating one publishes nothing, which is
    what makes it safe to build a whole listing before anything goes live.
    """
    response = transport.post(f"{INVENTORY_BASE}/offer", payload) or {}
    offer_id = response.get("offerId")
    if not offer_id:
        raise ApiError(
            "eBay created an offer but returned no offerId", 200,
            response.get("warnings") or [],
        )
    return str(offer_id)


def bulk_create_offer(
    transport, offers: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Create up to 25 unpublished offers in one call.

    The alternative is one call per card, which is what made creating a
    hundred-card listing take minutes: the offer loop was 94% of the
    requests. This is the same work in a twenty-fifth of the round trips.

    Returns the per-SKU rows. Each successful row carries the ``offerId``
    eBay generated, which is the *only* handle that can later change that
    card's price -- so a caller must read the ids out of this response and
    store them. Losing one means the next push tries to create a second offer
    for a SKU that already has one, which eBay refuses.
    """
    if not offers:
        return []
    if len(offers) > BULK_OFFER_LIMIT:
        raise ValueError(
            f"{len(offers)} offers exceeds eBay's limit of "
            f"{BULK_OFFER_LIMIT} per call; use chunked()"
        )
    for entry in offers:
        validate_sku(entry.get("sku", ""))
    response = transport.post(
        f"{INVENTORY_BASE}/bulk_create_offer", {"requests": list(offers)}
    ) or {}
    return bulk_statuses(response)


def update_offer(transport, offer_id: str, payload: Dict[str, Any]) -> None:
    """
    Replace an existing offer.

    Like the inventory item, a full replace. On a *published* offer this is
    what revises the live listing.
    """
    transport.put(f"{INVENTORY_BASE}/offer/{offer_id}", payload)


def get_offers(transport, sku: str) -> List[Dict[str, Any]]:
    """
    Every offer against one SKU.

    An empty list means "not visible in this model", not "not on sale". A SKU
    whose listing was created through File Exchange and never migrated has no
    offers in the Inventory API's world even though the listing is live.

    That case does not necessarily arrive as an empty list, though: eBay has
    been observed answering **404** for a SKU it holds no inventory item for
    at all, which is what an unmigrated File Exchange SKU looks like from
    here. This function does not flatten that into an empty list, because the
    two are different facts -- a caller asking "has this been migrated" wants
    the 404 as a definitive *no*, while a caller asking "what is this SKU
    priced at" wants the error. ``ApiError.status_code`` distinguishes them.
    """
    response = transport.get(
        f"{INVENTORY_BASE}/offer", {"sku": validate_sku(sku)}
    ) or {}
    return list(response.get("offers") or [])


def publish_offer(transport, offer_id: str) -> str:
    """Publish a single (non-variation) offer; returns the eBay listing id."""
    response = transport.post(
        f"{INVENTORY_BASE}/offer/{offer_id}/publish"
    ) or {}
    listing_id = response.get("listingId")
    if not listing_id:
        raise ApiError(
            f"offer {offer_id} published without returning a listingId", 200,
            response.get("warnings") or [],
        )
    return str(listing_id)


def withdraw_offer(transport, offer_id: str) -> None:
    """
    End the listing this offer backs, keeping the offer itself.

    Distinct from deleting the offer: withdrawing ends the sale but leaves the
    record, so the same offer can be republished later. For a card that has
    merely gone out of stock, a quantity of zero is the gentler instrument --
    it keeps the listing's history and ranking.
    """
    transport.post(f"{INVENTORY_BASE}/offer/{offer_id}/withdraw")


# -- inventory item groups (variation listings) --------------------------


def create_or_replace_inventory_item_group(
    transport, group_key: str, payload: Dict[str, Any]
) -> None:
    """
    Create or overwrite the group that becomes a variation listing.

    Note what this does once the group is published: it **updates the live
    listing immediately**, with no separate publish step. Adding a SKU to the
    group's ``variantSKUs`` puts that card on sale; removing one takes it off.
    That makes this the single most consequential call in the module, and the
    reason the drafts page exists -- there is no staged state on eBay's side
    to inspect before it takes effect.
    """
    transport.put(
        f"{INVENTORY_BASE}/inventory_item_group/{group_key}", payload
    )


def get_inventory_item_group(transport, group_key: str) -> Dict[str, Any]:
    """Read a group back -- the check after a migration or a regrouping."""
    return transport.get(
        f"{INVENTORY_BASE}/inventory_item_group/{group_key}"
    ) or {}


def publish_offer_by_inventory_item_group(
    transport, group_key: str, marketplace_id: str
) -> str:
    """
    Publish every offer in a group as one variation listing.

    Fails the *whole group* if any single offer in it is invalid, and fails
    after the fact rather than at approval time. That asymmetry is why
    validation lives on the drafts page: here, one bad card takes down a
    listing that may hold hundreds.
    """
    response = transport.post(
        f"{INVENTORY_BASE}/offer/publish_by_inventory_item_group",
        {"inventoryItemGroupKey": group_key, "marketplaceId": marketplace_id},
    ) or {}
    listing_id = response.get("listingId")
    if not listing_id:
        raise ApiError(
            f"group {group_key} published without returning a listingId", 200,
            response.get("warnings") or [],
        )
    return str(listing_id)


# -- price and quantity --------------------------------------------------


def bulk_update_price_quantity(
    transport, requests: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Change price and/or quantity for up to 25 SKUs in one call.

    The workhorse: nearly every plan is quantities and prices, and this is the
    cheapest way to apply them. Price and quantity travel together because
    eBay applies them together -- splitting them would double the call count
    and leave a listing briefly at a new price with an old quantity.
    """
    if not requests:
        return []
    if len(requests) > BULK_PRICE_QUANTITY_LIMIT:
        raise ValueError(
            f"{len(requests)} updates exceeds eBay's limit of "
            f"{BULK_PRICE_QUANTITY_LIMIT} per call; use chunked()"
        )
    response = transport.post(
        f"{INVENTORY_BASE}/bulk_update_price_quantity", {"requests": list(requests)}
    ) or {}
    return bulk_statuses(response)


def price_quantity_request(
    sku: str,
    *,
    offer_id: Optional[str] = None,
    quantity: Optional[int] = None,
    price: Optional[float] = None,
    currency: str = "USD",
) -> Dict[str, Any]:
    """
    Build one row for ``bulk_update_price_quantity``.

    Quantity is set on the **inventory item** (``shipToLocationAvailability``)
    and price on the **offer**, which is a distinction eBay's own payload
    makes and callers routinely get wrong. An omitted field is left alone:
    that is how a zero-out changes stock without touching the listed price.
    """
    request: Dict[str, Any] = {"sku": validate_sku(sku)}
    if quantity is not None:
        if int(quantity) < 0:
            raise ValueError(f"quantity for {sku} is negative")
        request["shipToLocationAvailability"] = {"quantity": int(quantity)}
    if price is not None:
        if offer_id is None:
            raise ValueError(
                f"changing the price of {sku} needs its offerId: price lives "
                f"on the offer, not on the inventory item"
            )
        request["offers"] = [{
            "offerId": str(offer_id),
            "price": {"value": f"{float(price):.2f}", "currency": currency},
        }]
    if len(request) == 1:
        raise ValueError(f"no change requested for {sku}")
    return request


# -- reading the per-record outcome of a bulk call -----------------------


def bulk_migrate_listing(
    transport, listing_ids: Sequence[str]
) -> List[Dict[str, Any]]:
    """
    Convert existing eBay listings into Inventory API objects.

    This is the one call in this module that cannot be undone. It creates the
    inventory items, offers and -- for a multi-variation listing -- the
    inventory item group behind a listing that already exists, keeping the
    same eBay item id, its watchers and its search standing. Afterwards the
    listing belongs to the Inventory API: the Trading API and File Exchange
    can no longer revise it.

    At most five listings per call, and callers here pass one. eBay reports
    per-listing outcomes inside a 200, so a batch of five can be four
    successes and a failure, and unpicking which is which after an
    irreversible operation is not worth the saved round trip.

    Each returned row carries ``listingId``, ``statusCode``, an
    ``inventoryItemGroupKey`` when the listing had variations, and
    ``inventoryItems`` pairing each ``sku`` with its new ``offerId``. Those
    offer ids are the only handles that can later change a price, and they
    exist nowhere else -- losing this response means reading them back with
    getOffers, one SKU at a time.
    """
    if not listing_ids:
        return []
    if len(listing_ids) > BULK_MIGRATE_LIMIT:
        raise ValueError(
            f"{len(listing_ids)} listings exceeds eBay's limit of "
            f"{BULK_MIGRATE_LIMIT} per migrate call"
        )
    response = transport.post(
        f"{INVENTORY_BASE}/bulk_migrate_listing",
        {"requests": [{"listingId": str(i).strip()} for i in listing_ids]},
    ) or {}
    return bulk_statuses(response)


def bulk_statuses(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Normalise a bulk response into one row per record.

    eBay names the envelope differently between calls ("responses" here,
    "results" there), and a caller that guesses wrong sees an empty list and
    concludes everything succeeded. Matching both, in one place, is cheaper
    than that failure mode.
    """
    rows = response.get("responses")
    if rows is None:
        rows = response.get("results")
    return [row for row in (rows or []) if isinstance(row, dict)]


def status_failed(row: Dict[str, Any]) -> bool:
    """
    Whether one row of a bulk response represents a failure.

    Judged on the row's own HTTP status when it has one, and on the presence
    of an ``errors`` list otherwise. A row carrying only ``warnings`` counts
    as a success: eBay warns about things like a picture it could not fetch,
    and refusing the whole push over that would be wrong.
    """
    code = row.get("statusCode")
    if isinstance(code, int):
        return not 200 <= code < 300
    return bool(row.get("errors"))


def failed_statuses(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Just the rows that failed, for reporting against individual cards."""
    return [row for row in rows if status_failed(row)]


def describe_failure(row: Dict[str, Any]) -> str:
    """A one-line reason for a failed row, safe to store against a card."""
    errors = row.get("errors") or []
    details = "; ".join(
        str(e.get("longMessage") or e.get("message") or e) for e in errors
    )
    sku = row.get("sku") or row.get("offerId") or "?"
    if not details:
        return f"{sku}: eBay reported status {row.get('statusCode', 'unknown')}"
    return f"{sku}: {details}"
