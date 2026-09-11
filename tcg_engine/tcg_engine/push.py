"""
Push an approved plan to eBay.

The last step of the migration away from "generate a CSV and upload it by
hand". Everything upstream of this -- the catalogue, the diff, the drafts
page, the approval -- existed to make this call safe to make.

Four properties matter more than the size of this module.

**Every card gets its own verdict.** eBay's bulk calls answer HTTP 200 and
report success or failure *per SKU* inside the body. A push that reads the
status code marks all twenty-five cards pushed when three of them failed, and
the store mirror then disagrees with eBay with nothing to show why. So each
plan item ends as ``pushed`` or ``failed`` with eBay's own message against it,
and one card's failure never fails its neighbours.

**Only listings the Inventory API can see are pushed.** A listing created
through File Exchange is invisible to this API -- ``getOffers`` returns
nothing for its SKUs -- so pushing its cards would not update it, it would
create a *second* listing beside the live one. Those items are left
``deferred`` with the reason, and the CSV files still cover them. That is the
whole purpose of ``ebay_managed_listing``.

**What eBay says happened is written down immediately.** Item numbers, offer
ids and the group key are recorded as they arrive, before the next call. A
push that dies halfway must leave behind enough to be resumed rather than
repeated, because repeating a create is how a duplicate listing appears.

**Nothing is inferred about a listing's state from our own intent.** The
quantity eBay reports is only written when eBay has confirmed it.
"""

import json
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .batches import (
    build_variation_option_name,
    generate_variation_title,
    resolve_condition_descriptor,
)
from .db import Database
from .plan_exports import DEFAULT_VARIATION_OPTION_TEMPLATE, _derived_specifics
from .plans import (
    ACTION_END,
    ACTION_REMOVE,
    ACTION_UPDATE,
    ACTION_ZERO_OUT,
    PLAN_DRAFT,
    PLAN_PARTIAL,
    PLAN_PUSHED,
    STATUS_DEFERRED,
    STATUS_EXCLUDED,
    STATUS_FAILED,
    STATUS_PUSHED,
    is_single,
)

# eBay's item condition for an ungraded card. Its numeric equivalent is 4000,
# which is what the CSV path writes into ConditionID -- the same fact in the
# two vocabularies. LIKE_NEW (2750) is the graded counterpart, which this
# project does not support yet.
UNGRADED_ITEM_CONDITION = "USED_VERY_GOOD"

# The Card Condition condition descriptor. Its value is a numeric id such as
# 400010 for "Near mint or better"; eBay requires this one descriptor on an
# ungraded card and rejects prose here, which is why the CSV path's
# human-readable rendering cannot be reused.
CARD_CONDITION_DESCRIPTOR_ID = "40001"

# The variation axis, matching the CSV path's "Card=..." attribute so a
# migrated listing and a created one look the same to a buyer.
VARIATION_ASPECT_NAME = "Card"

# eBay's ceiling on one bulk call.
BULK_LIMIT = 25

# The natural language of the values we send. Required *per record* by
# bulkCreateOrReplaceInventoryItem, and its absence is reported as
# "Valid SKU and locale information are required for all the InventoryItems in
# the request" -- which reads like the SKU is the problem when the SKU is
# present and fine. The Content-Language header the transport already sends is
# necessary but not sufficient: the header describes the request, this field
# describes the record.
LOCALE_BY_MARKETPLACE = {
    "EBAY_US": "en_US",
    "EBAY_CA": "en_CA",
    "EBAY_GB": "en_GB",
    "EBAY_AU": "en_AU",
    "EBAY_DE": "de_DE",
    "EBAY_FR": "fr_FR",
    "EBAY_IT": "it_IT",
    "EBAY_ES": "es_ES",
}
DEFAULT_LOCALE = "en_US"


def locale_for(marketplace_id: str) -> str:
    """The record locale for a marketplace, defaulting to US English."""
    return LOCALE_BY_MARKETPLACE.get(
        str(marketplace_id or "").strip().upper(), DEFAULT_LOCALE
    )

_DESCRIPTOR_ID_PATTERN = re.compile(r"(\d{4,})")


class PushError(RuntimeError):
    """The push could not be attempted at all."""


def condition_descriptor_value_id(
    stored_descriptor: str, condition: str, category_id: str
) -> Optional[str]:
    """
    The numeric Card Condition value id for this card.

    The CSV path may hold this as prose -- "Near mint or better - (ID:
    400010)", or whatever spelling the seller's own export used -- because
    File Exchange accepts that. The API does not: it wants the bare id. So the
    id is lifted out of the stored value when it is there, and otherwise
    derived from the condition. Re-deriving is safe because the mapping is
    from grade to eBay's published id and involves no judgement about the
    card.
    """
    match = _DESCRIPTOR_ID_PATTERN.search(str(stored_descriptor or ""))
    if match:
        return match.group(1)
    return resolve_condition_descriptor(
        condition, category_id=category_id, style="id"
    )


def inventory_group_key(group_key: str) -> str:
    """
    Turn our group key into one eBay will accept in a URL path.

    Our keys are "<set>|<condition>", which contains characters that have to
    be escaped in a path and read badly in eBay's own UI. The slug is
    deterministic and carries a short digest of the original, so the same
    listing always maps to the same eBay group -- rebuilding a draft must not
    invent a second group for a listing that already exists.
    """
    import hashlib

    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(group_key)).strip("-")[:32]
    digest = hashlib.sha1(str(group_key).encode("utf-8")).hexdigest()[:8]
    return f"{slug or 'group'}-{digest}".upper()


def _inventory_item_payload(
    item: Dict[str, Any],
    *,
    settings: Dict[str, str],
    category_id: str,
    option_name: str,
    is_variation: bool,
) -> Dict[str, Any]:
    """
    One inventory item: what the card *is*, plus how many we hold.

    A full replace rather than a merge, because that is what eBay stores: a
    field omitted here is a field removed from the item.
    """
    try:
        fields = json.loads(item.get("ebay_fields_json") or "{}")
    except (ValueError, TypeError):
        fields = {}
    if not isinstance(fields, dict):
        fields = {}

    specifics = dict(_derived_specifics(item))
    specifics.update({
        key: value
        for key, value in (fields.get("item_specifics") or {}).items()
        if str(value or "").strip()
    })
    default_game = (settings.get("default_game") or "").strip()
    if default_game:
        specifics["C:Game"] = default_game

    # eBay's aspects are unprefixed names mapping to *lists* of values. The
    # "C:" prefix belongs to File Exchange's column naming and nothing else.
    aspects: Dict[str, List[str]] = {}
    for key, value in specifics.items():
        name = key[2:] if key.startswith("C:") else key
        text = str(value or "").strip()
        if name and text:
            aspects[name] = [text]
    if is_variation:
        # The variation axis has to be an aspect of the item as well as of
        # the group, or eBay cannot tell which variation this item is.
        aspects[VARIATION_ASPECT_NAME] = [option_name]

    images = [
        url for url in (
            item.get("cdn_image") or "",
            fields.get("cdn_back_image") or "",
            fields.get("stock_image") or "",
        ) if url
    ]

    descriptor_value = condition_descriptor_value_id(
        fields.get("condition_descriptor") or "",
        item.get("condition") or "",
        category_id,
    )

    payload: Dict[str, Any] = {
        "availability": {
            "shipToLocationAvailability": {
                "quantity": int(item.get("proposed_qty") or 0)
            }
        },
        "condition": UNGRADED_ITEM_CONDITION,
        "product": {
            "title": _item_title(item, option_name, is_variation),
            "aspects": aspects,
        },
    }
    if images:
        payload["product"]["imageUrls"] = images
    if descriptor_value:
        payload["conditionDescriptors"] = [{
            "name": CARD_CONDITION_DESCRIPTOR_ID,
            "values": [str(descriptor_value)],
        }]
    return payload


def _item_title(item: Dict[str, Any], option_name: str, is_variation: bool) -> str:
    """
    The title for one inventory item.

    A variation's own title is not what a buyer sees -- the group's title is
    -- but eBay requires one, and the option name is the most useful thing to
    put there when looking at the item in Seller Hub.
    """
    if is_variation:
        return option_name[:80]
    name = str(item.get("product_name") or "").strip()
    set_name = str(item.get("set_name") or "").strip()
    condition = str(item.get("condition") or "").strip()
    title = f"{name} - {set_name} - {condition}".strip(" -")
    if len(title) > 80:
        title = f"{name} - {set_name}".strip(" -")
    return title[:80]


def _offer_payload(
    item: Dict[str, Any],
    *,
    settings: Dict[str, str],
    category_id: str,
    marketplace_id: str,
    description: str,
) -> Dict[str, Any]:
    """
    One offer: the saleable proposition against a SKU.

    Price lives here rather than on the inventory item, which is the single
    most common confusion in this API and the reason a price change needs an
    offer id.
    """
    price = item.get("proposed_price")
    payload: Dict[str, Any] = {
        "sku": item.get("custom_label") or item["manifest_id"],
        "marketplaceId": marketplace_id,
        "format": "FIXED_PRICE",
        "availableQuantity": int(item.get("proposed_qty") or 0),
        "categoryId": str(category_id),
        "listingDescription": description,
        "listingPolicies": {},
    }
    if price is not None:
        payload["pricingSummary"] = {
            "price": {"value": f"{float(price):.2f}", "currency": "USD"}
        }
    # Business policies are referenced by id, not by the names File Exchange
    # uses. Sent only when configured, because an empty id is rejected while
    # an absent one falls back to the seller's default.
    for setting_key, field in (
        ("shipping_policy_id", "fulfillmentPolicyId"),
        ("return_policy_id", "returnPolicyId"),
        ("payment_policy_id", "paymentPolicyId"),
    ):
        value = str(settings.get(setting_key) or "").strip()
        if value:
            payload["listingPolicies"][field] = value
    # eBay will not publish an offer without an inventory location. Sent only
    # when configured: an empty key is rejected outright, whereas an absent
    # one lets eBay fall back to the seller's default location, which is the
    # right behaviour for an account that has exactly one.
    location = str(settings.get("merchant_location_key") or "").strip()
    if location:
        payload["merchantLocationKey"] = location
    return payload


def _group_payload(
    group_key: str,
    entries: List[Tuple[Dict[str, Any], str]],
    *,
    title: str,
    description: str,
    cover_image_url: str,
    aspects: Dict[str, List[str]],
) -> Dict[str, Any]:
    """
    The inventory item group that becomes one variation listing.

    Note what writing this does to a *published* group: it updates the live
    listing immediately, with no publish step and nothing staged to inspect
    first. Removing a SKU from ``variantSKUs`` takes that card off sale. This
    is the most consequential payload in the module, and the reason the drafts
    page exists at all.
    """
    skus = [sku for _, sku in entries]
    options = [
        build_variation_option_name(
            entry.get("product_name") or "", entry.get("card_number") or "",
            DEFAULT_VARIATION_OPTION_TEMPLATE,
        )
        for entry, _ in entries
    ]
    payload: Dict[str, Any] = {
        "inventoryItemGroupKey": group_key,
        "title": title,
        "description": description,
        "variantSKUs": skus,
        "variesBy": {
            "aspectsImageVariesBy": [VARIATION_ASPECT_NAME],
            "specifications": [
                {"name": VARIATION_ASPECT_NAME, "values": options}
            ],
        },
    }
    if aspects:
        payload["aspects"] = aspects
    if cover_image_url:
        payload["imageUrls"] = [cover_image_url]
    return payload


# -- the surface this module needs from the eBay library -----------------
#
# Passed in rather than imported. ``tcg_engine`` holds no dependency on
# ``ebay_client``: the client speaks SKUs, offers and inventory item groups
# and knows nothing about manifest ids or pricing rules, and the adapter
# binding the two lives in the web layer. The practical payoff is that every
# test drives a fake, so the whole push is exercised with no network, no
# credentials and no quota.
#
# The expected methods, each raising on a transport or auth failure:
#
#   upsert_items(items)            -> per-SKU status rows
#   create_offer(payload)          -> offer id
#   update_price_quantity(reqs)    -> per-SKU status rows
#   upsert_group(group_key, body)  -> None
#   publish_group(group_key)       -> eBay listing id
#   publish_offer(offer_id)        -> eBay listing id
#   withdraw_offer(offer_id)       -> None
#   failures(rows)                 -> [(sku, message), ...]


def _sku_for(item: Dict[str, Any]) -> str:
    return str(item.get("custom_label") or item["manifest_id"]).strip()


def push_plan(
    db: Database,
    api: Any,
    plan_id: int,
    user_id: Optional[int] = None,
    log: Optional[Callable[[str, str], None]] = None,
    group_keys: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """
    Apply an approved plan to eBay, one listing at a time.

    Returns a summary plus a log. Never raises for a single card's failure:
    that card is marked ``failed`` carrying eBay's own reason and the rest
    continue. A plan can hold hundreds of cards across several listings, and
    abandoning all of them over one bad aspect value would make the drafts
    page useless.

    ``group_keys`` restricts the push to those listings, leaving the rest of
    the plan exactly as it was -- still approved, still pushable later. That
    is what makes a first push testable: a create cannot be undone by pressing
    the button again, so the sane way to begin is one small listing, checked in
    Seller Hub, before several hundred cards go live in one call.
    """
    plan = db.get_plan(plan_id)
    if plan is None:
        raise PushError(f"no such plan: {plan_id}")
    if plan["status"] == PLAN_DRAFT:
        raise PushError("approve the plan before pushing it")

    logs: List[Dict[str, str]] = []

    def record(level: str, message: str) -> None:
        logs.append({"level": level, "message": message})
        if log:
            log(level, message)

    scope = plan["user_id"] if user_id is None else user_id
    settings = db.get_listing_settings(user_id=scope)
    category_id = str(settings.get("category_id") or "183454")
    marketplace_id = str(settings.get("marketplace_id") or "EBAY_US")
    title_template = settings.get(
        "variation_title_template",
        "{set_name}: Pick Your Card - {condition} - Complete Your Set",
    )
    option_template = (
        settings.get("variation_option_template")
        or DEFAULT_VARIATION_OPTION_TEMPLATE
    )
    covers = db.get_plan_group_covers(plan_id)
    account_cover = settings.get("cover_image_url") or ""

    # Already-pushed items are skipped rather than repeated. A push that died
    # halfway has to be resumable, and repeating a create is exactly how a
    # duplicate listing appears.
    all_items = db.get_plan_items(plan_id)
    items = [
        item for item in all_items
        if item["status"] not in (STATUS_EXCLUDED, STATUS_PUSHED)
    ]

    # A push that considers nothing must say why, and must not say SUCCESS.
    # Reporting "0 pushed, 0 failed" as a success is indistinguishable from a
    # push that worked, and it sent someone to eBay's active listings looking
    # for a listing that was never attempted. The three causes need different
    # responses, so they are named separately.
    if not items:
        already = sum(1 for row in all_items if row["status"] == STATUS_PUSHED)
        excluded = sum(1 for row in all_items if row["status"] == STATUS_EXCLUDED)
        if not all_items:
            reason = (
                "this plan has no items at all. Either it was built when "
                "nothing had changed, or the cards it referred to are no "
                "longer in the catalogue. Rebuild the draft."
            )
        elif already and not excluded:
            reason = (
                f"all {already} card(s) in this plan were already pushed. "
                f"Rebuild the draft to pick up anything that has changed since."
            )
        else:
            reason = (
                f"nothing is left to push: {already} card(s) already pushed, "
                f"{excluded} left out."
            )
        record("WARN", f"Nothing was sent to eBay -- {reason}")
        return {
            "plan_id": plan_id,
            "pushed": 0,
            "failed": 0,
            "deferred": 0,
            "listings_created": 0,
            "listings_updated": 0,
            "attempted": False,
            "reason": reason,
            "logs": logs,
        }

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(item.get("group_key") or "", []).append(item)

    if group_keys is not None:
        wanted = {str(key) for key in group_keys}
        unknown = wanted - set(groups)
        if unknown:
            raise PushError(
                "this plan has no listing(s) called: " + ", ".join(sorted(unknown))
            )
        groups = {key: value for key, value in groups.items() if key in wanted}

    counts = {"pushed": 0, "failed": 0, "deferred": 0}
    listings_created = 0
    listings_updated = 0

    for group_key, group_items in sorted(groups.items()):
        managed = db.get_managed_listing(group_key)

        # A listing eBay already has but this API cannot see. Pushing it would
        # not update it -- it would create a second listing beside the live
        # one -- so it stays on the CSV path, and says so.
        legacy = [i for i in group_items if i.get("ebay_parent_id")]
        if managed is None and legacy:
            for item in group_items:
                _mark(db, item, STATUS_DEFERRED, counts)
            record("WARN", (
                f"{group_key or 'ungrouped'}: {len(group_items)} card(s) left "
                f"for the CSV path. This listing was created through File "
                f"Exchange, so the Inventory API cannot see it and a push "
                f"would create a duplicate. Migrate it first."
            ))
            continue

        try:
            created, updated = _push_group(
                db, api, group_key, group_items, managed,
                settings=settings,
                category_id=category_id,
                marketplace_id=marketplace_id,
                title_template=title_template,
                option_template=option_template,
                cover_image_url=covers.get(group_key) or account_cover,
                counts=counts,
                record=record,
            )
            listings_created += created
            listings_updated += updated
        except Exception as exc:  # noqa: BLE001 - one listing must not sink the rest
            for item in group_items:
                if item["status"] not in (STATUS_PUSHED, STATUS_FAILED):
                    _mark(db, item, STATUS_FAILED, counts, str(exc))
            record("ERROR", f"{group_key or 'ungrouped'}: {exc}")

    # The plan's status is read back off its items rather than inferred from
    # this run's counters, because a push restricted to one listing leaves the
    # rest of the plan outstanding. Calling that "pushed" would hide the work
    # still to do behind a word that means finished.
    remaining = [
        row for row in db.get_plan_items(plan_id)
        if row["status"] not in (STATUS_EXCLUDED, STATUS_PUSHED)
    ]
    if remaining:
        if any(row["status"] == STATUS_PUSHED for row in db.get_plan_items(plan_id)):
            db.set_plan_status(plan_id, PLAN_PARTIAL)
    elif counts["pushed"]:
        db.set_plan_status(plan_id, PLAN_PUSHED)

    # SUCCESS only when something actually reached eBay. Anything else is at
    # best a partial outcome and must not read like a completed push.
    level = "SUCCESS" if counts["pushed"] and not counts["failed"] else "WARN"
    record(level, (
        f"{counts['pushed']} card(s) pushed, {counts['failed']} failed, "
        f"{counts['deferred']} left for CSV; {listings_created} listing(s) "
        f"created, {listings_updated} updated"
    ))
    return {
        "plan_id": plan_id,
        "pushed": counts["pushed"],
        "failed": counts["failed"],
        "deferred": counts["deferred"],
        "listings_created": listings_created,
        "listings_updated": listings_updated,
        "attempted": True,
        "logs": logs,
    }


def _mark(
    db: Database,
    item: Dict[str, Any],
    status: str,
    counts: Dict[str, int],
    reason: str = "",
) -> None:
    """Record one card's verdict, counting it exactly once."""
    fields: Dict[str, Any] = {"status": status}
    if reason:
        fields["validation"] = json.dumps([reason])
    db.update_plan_item(item["id"], **fields)
    key = {
        STATUS_PUSHED: "pushed",
        STATUS_FAILED: "failed",
        STATUS_DEFERRED: "deferred",
    }.get(status)
    if key:
        counts[key] += 1
    item["status"] = status


def _push_group(
    db: Database,
    api: Any,
    group_key: str,
    group_items: List[Dict[str, Any]],
    managed: Optional[Dict[str, Any]],
    *,
    settings: Dict[str, str],
    category_id: str,
    marketplace_id: str,
    title_template: str,
    option_template: str,
    cover_image_url: str,
    counts: Dict[str, int],
    record: Callable[[str, str], None],
) -> Tuple[int, int]:
    """
    Push one listing's worth of cards. Returns (created, updated).

    Ordered so that dying partway through leaves eBay and our records
    agreeing: items and offers are written and recorded before the group that
    publishes them, and the group is what makes anything visible to a buyer.
    """
    single = is_single(group_key) or not group_key
    ends = [i for i in group_items if i["action"] == ACTION_END]
    changes = [i for i in group_items if i["action"] != ACTION_END]

    # Ending a listing is the one action needing no item or offer work.
    for item in ends:
        offer_id = item.get("offer_id")
        if not offer_id:
            _mark(db, item, STATUS_DEFERRED, counts)
            record("WARN", (
                f"{_sku_for(item)}: no offer on record, so there is nothing "
                f"to end through the API."
            ))
            continue
        api.withdraw_offer(str(offer_id))
        _mark(db, item, STATUS_PUSHED, counts)
        db.upsert_variation(
            item["manifest_id"], str(item.get("ebay_parent_id") or ""), 0,
            custom_label=_sku_for(item),
        )

    if not changes:
        return (0, 1 if ends else 0)

    option_names = {
        item["id"]: build_variation_option_name(
            item.get("product_name") or "", item.get("card_number") or "",
            option_template,
        )
        for item in changes
    }

    # 1. The items themselves, in batches eBay will accept.
    payloads = []
    for item in changes:
        payloads.append({
            "sku": _sku_for(item),
            "locale": locale_for(marketplace_id),
            **_inventory_item_payload(
                item,
                settings=settings,
                category_id=category_id,
                option_name=option_names[item["id"]],
                is_variation=not single,
            ),
        })

    failed_skus: Dict[str, str] = {}
    for start in range(0, len(payloads), BULK_LIMIT):
        rows = api.upsert_items(payloads[start:start + BULK_LIMIT])
        for sku, message in api.failures(rows):
            failed_skus[sku] = message

    live = []
    for item in changes:
        sku = _sku_for(item)
        if sku in failed_skus:
            _mark(db, item, STATUS_FAILED, counts, failed_skus[sku])
            record("ERROR", f"{sku}: {failed_skus[sku]}")
            continue
        live.append(item)

    if not live:
        record("ERROR", f"{group_key or 'ungrouped'}: every card failed")
        return (0, 0)

    # 2. An offer per card. One already on record is reused: a second offer
    #    for the same SKU is an error, and the offer id is the only handle
    #    that can change a price.
    description = _group_description(group_items, single)
    for item in list(live):
        if item.get("offer_id"):
            continue
        try:
            offer_id = api.create_offer(_offer_payload(
                item,
                settings=settings,
                category_id=category_id,
                marketplace_id=marketplace_id,
                description=description,
            ))
        except Exception as exc:  # noqa: BLE001 - a per-card verdict
            _mark(db, item, STATUS_FAILED, counts, str(exc))
            record("ERROR", f"{_sku_for(item)}: {exc}")
            live.remove(item)
            continue
        db.set_variation_offer(item["manifest_id"], offer_id)
        item["offer_id"] = offer_id

    if not live:
        return (0, 0)

    # 3. Price and quantity for cards whose offer already existed. A freshly
    #    created offer already carries both, so re-sending would be a call
    #    that changes nothing.
    _apply_price_quantity(
        db, api,
        [i for i in live
         if i["action"] in (ACTION_UPDATE, ACTION_ZERO_OUT, ACTION_REMOVE)],
        counts, record,
    )

    live = [item for item in live if item["status"] != STATUS_FAILED]
    if not live:
        return (0, 0)

    published = bool(managed and managed.get("ebay_parent_id"))

    if single:
        item = live[0]
        if not published:
            listing_id = api.publish_offer(str(item["offer_id"]))
            db.upsert_managed_listing(
                group_key, ebay_parent_id=listing_id, pushed=True
            )
            record("INFO", f"{_sku_for(item)}: listed as #{listing_id}")
        else:
            listing_id = managed["ebay_parent_id"]
            db.upsert_managed_listing(group_key, pushed=True)
            record("INFO", f"{_sku_for(item)}: updated listing #{listing_id}")
        _confirm(db, live, listing_id, counts)
        return (0 if published else 1, 1 if published else 0)

    # 4. The group. Writing it to an already-published listing changes that
    #    listing immediately -- there is no staged state to inspect first.
    ebay_group_key = (
        (managed or {}).get("inventory_item_group_key")
        or inventory_group_key(group_key)
    )
    set_name, _, condition = group_key.partition("|")
    entries = [
        (item, _sku_for(item)) for item in live
        if item["action"] != ACTION_REMOVE
    ]
    api.upsert_group(ebay_group_key, _group_payload(
        ebay_group_key,
        entries,
        title=generate_variation_title(
            set_name, condition=condition, template=title_template
        ),
        description=description,
        cover_image_url=cover_image_url or _first_image(live),
        aspects=_uniform_aspects(live, settings),
    ))
    db.upsert_managed_listing(
        group_key, inventory_item_group_key=ebay_group_key, pushed=True
    )

    if not published:
        listing_id = api.publish_group(ebay_group_key)
        db.upsert_managed_listing(
            group_key, ebay_parent_id=listing_id, pushed=True
        )
        record("INFO", (
            f"{group_key}: listed as #{listing_id} with {len(entries)} "
            f"variation(s)"
        ))
    else:
        listing_id = managed["ebay_parent_id"]
        record("INFO", f"{group_key}: updated listing #{listing_id}")

    _confirm(db, live, listing_id, counts)
    return (0 if published else 1, 1 if published else 0)


def _apply_price_quantity(
    db: Database,
    api: Any,
    updates: List[Dict[str, Any]],
    counts: Dict[str, int],
    record: Callable[[str, str], None],
) -> None:
    """Send the price and quantity changes, then read the per-SKU verdicts."""
    if not updates:
        return
    requests = []
    for item in updates:
        request: Dict[str, Any] = {
            "sku": _sku_for(item),
            "shipToLocationAvailability": {
                "quantity": int(item.get("proposed_qty") or 0)
            },
        }
        price = item.get("proposed_price")
        # A zero-out leaves the price alone: the card is out of stock, not on
        # sale. An omitted field is how eBay is told to leave one untouched.
        if price is not None and item["action"] == ACTION_UPDATE:
            request["offers"] = [{
                "offerId": str(item["offer_id"]),
                "price": {"value": f"{float(price):.2f}", "currency": "USD"},
            }]
        requests.append(request)

    failed: Dict[str, str] = {}
    for start in range(0, len(requests), BULK_LIMIT):
        rows = api.update_price_quantity(requests[start:start + BULK_LIMIT])
        for sku, message in api.failures(rows):
            failed[sku] = message

    for item in updates:
        sku = _sku_for(item)
        if sku in failed:
            _mark(db, item, STATUS_FAILED, counts, failed[sku])
            record("ERROR", f"{sku}: {failed[sku]}")


def _confirm(
    db: Database,
    items: List[Dict[str, Any]],
    listing_id: str,
    counts: Dict[str, int],
) -> None:
    """
    Write down what eBay now holds, for the cards it accepted.

    The mirror is only ever written from a confirmed outcome. Recording our
    own intent here is what made the two quantity columns disagree for weeks
    on the CSV path: the point of ``last_known_qty`` is that it is eBay's
    number and not ours.
    """
    for item in items:
        if item["status"] == STATUS_FAILED:
            continue
        db.upsert_variation(
            item["manifest_id"],
            str(listing_id),
            int(item.get("proposed_qty") or 0),
            custom_label=_sku_for(item),
            last_known_price=item.get("proposed_price"),
        )
        if item["status"] != STATUS_PUSHED:
            _mark(db, item, STATUS_PUSHED, counts)


def _group_description(items: List[Dict[str, Any]], single: bool) -> str:
    first = items[0] if items else {}
    set_name = str(first.get("set_name") or "").strip()
    condition = str(first.get("condition") or "").strip()
    if single:
        name = str(first.get("product_name") or "").strip()
        return f"{name} from {set_name}. Condition: {condition}.".strip()
    return (
        f"Pick Your Card from {set_name}! Condition: {condition}. "
        f"Complete your collection."
    )


def _first_image(items: List[Dict[str, Any]]) -> str:
    for item in items:
        if item.get("cdn_image"):
            return str(item["cdn_image"])
    return ""


def _uniform_aspects(
    items: List[Dict[str, Any]], settings: Dict[str, str]
) -> Dict[str, List[str]]:
    """
    The aspects every card in the group agrees on.

    A variation listing carries one set of listing-level aspects, so a value
    differing between cards (Card Name, Card Number) cannot be stated there --
    the variation axis expresses it instead. This mirrors the CSV path's
    ``_uniform_item_specifics`` for exactly the same reason.
    """
    per_card = []
    default_game = (settings.get("default_game") or "").strip()
    for item in items:
        try:
            fields = json.loads(item.get("ebay_fields_json") or "{}")
        except (ValueError, TypeError):
            fields = {}
        specifics = dict(_derived_specifics(item))
        if isinstance(fields, dict):
            specifics.update({
                k: v for k, v in (fields.get("item_specifics") or {}).items()
                if str(v or "").strip()
            })
        if default_game:
            specifics["C:Game"] = default_game
        per_card.append({
            (k[2:] if k.startswith("C:") else k): str(v).strip()
            for k, v in specifics.items() if str(v or "").strip()
        })

    if not per_card:
        return {}
    shared: Dict[str, List[str]] = {}
    for name in set().union(*(set(card) for card in per_card)):
        values = {card.get(name, "") for card in per_card}
        if len(values) == 1:
            value = values.pop()
            if value and name != VARIATION_ASPECT_NAME:
                shared[name] = [value]
    return shared
