"""
Push an approved plan to eBay.

The only thing in this project that writes to eBay. Everything upstream --
the catalogue, the diff, the drafts page, the approval -- exists to make this
call safe to make, and the "generate a CSV and upload it by hand" path it
replaced has been deleted.

Four properties matter more than the size of this module.

**Every card gets its own verdict.** eBay's bulk calls answer HTTP 200 and
report success or failure *per SKU* inside the body. A push that reads the
status code marks all twenty-five cards pushed when three of them failed, and
the store mirror then disagrees with eBay with nothing to show why. So each
plan item ends as ``pushed`` or ``failed`` with eBay's own message against it,
and one card's failure never fails its neighbours.

**Only listings the Inventory API can see are pushed.** A listing this API
cannot see -- ``getOffers`` returns nothing for its SKUs -- would not be
updated by a push; a *second* listing would be created beside the live one.
Those items are left ``deferred`` with the reason. Every listing has now been
migrated, so this should never fire, which is exactly why it stays: the cost
of being wrong is a duplicate live listing. That is the whole purpose of
``ebay_managed_listing``.

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
    variation_sort_key,
    generate_variation_title,
    resolve_condition_descriptor,
)
from .db import (
    DEFAULT_VARIATION_OPTION_TEMPLATE,
    SHARED_SCOPE,
    Database,
)
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
    STATUS_PENDING,
    STATUS_PUSHED,
    is_single,
)

# eBay's item condition for an ungraded card. Its numeric equivalent is
# ConditionID 4000 -- the same fact in eBay's two vocabularies. LIKE_NEW
# (2750) is the graded counterpart, which this project does not support yet.
UNGRADED_ITEM_CONDITION = "USED_VERY_GOOD"

# The Card Condition condition descriptor. Its value is a numeric id such as
# 400010 for "Near mint or better"; eBay requires this one descriptor on an
# ungraded card and rejects prose here, even though its own CSV template
# accepted the human-readable form.
CARD_CONDITION_DESCRIPTOR_ID = "40001"

# The variation axis. It matches the attribute name the File Exchange files
# used, so a listing migrated from that era and one created here look the same
# to a buyer -- and to the inventory item group, which is keyed on it.
VARIATION_ASPECT_NAME = "Card"

# Item specifics readable off the card's own stored columns, for cards
# catalogued before the export's "C:" columns were persisted. Translates
# nothing: each value is the column, passed through.
#
# A floor, not a substitute. Around thirteen of the specifics eBay marks
# required on a card listing (Card Type, Manufacturer, Graded, Card Size,
# Character, Stage, both Country fields, Age Level, Year Manufactured,
# Autographed, Material, Attribute) exist nowhere but the export, which is why
# a card with no persisted specifics is a blocker in ``plans.validate_card``
# rather than something this quietly papers over.
DERIVED_SPECIFICS = (
    ("C:Set", "set_name"),
    ("C:Card Name", "product_name"),
    ("C:Card Number", "card_number"),
    ("C:Language", "language"),
    ("C:Finish", "printing"),
)

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


def _derived_specifics(item: Dict[str, Any]) -> Dict[str, str]:
    """The specifics readable from the card's own stored columns."""
    derived: Dict[str, str] = {}
    for column, source in DERIVED_SPECIFICS:
        value = str(item.get(source) or "").strip()
        if value:
            derived[column] = value
    return derived


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

    # The card's own front scan, and nothing else.
    #
    # Exactly one picture, and the back scan is deliberately excluded. Every
    # card in a set has a near-identical back, so including it puts what look
    # like duplicate photos on the listing, and a buyer choosing between 105
    # variations gains nothing from five pictures of the same card back. This
    # is the one place where the API push differs from the old CSV path on
    # purpose: the first real listing went up with three pictures per
    # variation and looked wrong.
    #
    # The stock photo is used only when there is no scan. Alongside a scan it
    # is a second view of the same front and reads as another duplicate; on
    # its own it is the difference between a listing with a picture and one
    # with none.
    images = [url for url in (card_image(item),) if url]

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

    # A retry starts clean. An item still carrying ``failed`` or ``deferred``
    # from an earlier attempt is about to be tried again, so that verdict is
    # stale -- and leaving it in place is not cosmetic. This module asks "did
    # this card fail?" several times while walking a listing, by reading the
    # item's own status; a status left over from last time makes those checks
    # answer yes before anything has been attempted.
    #
    # That is exactly what happened on the second real push: the inventory
    # items went up, five offers were created, and then every card was
    # silently dropped because it still said ``failed`` from the previous
    # attempt. No group was written, nothing was published, nothing was
    # marked, and the run reported "0 pushed, 0 failed" -- with five orphaned
    # offers on eBay and no hint of why.
    for item in items:
        if item["status"] in (STATUS_FAILED, STATUS_DEFERRED):
            db.update_plan_item(item["id"], status=STATUS_PENDING, validation=None)
            item["status"] = STATUS_PENDING

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(item.get("group_key") or "", []).append(item)

    if group_keys is not None:
        wanted = {str(key) for key in group_keys}
        # "Nothing left to push" and "no such listing" are different answers,
        # and conflating them is how pressing Push twice on a listing that
        # went up perfectly reported that the plan had no such listing.
        in_plan = {str(row.get("group_key") or "") for row in all_items}
        unknown = wanted - in_plan
        if unknown:
            raise PushError(
                "this plan has no listing(s) called: " + ", ".join(sorted(unknown))
            )
        done = wanted - set(groups)
        if done:
            reason = (
                "every card in " + ", ".join(sorted(done)) + " has already "
                "been pushed. Use Refresh on the eBay Listings tab to re-send "
                "its pictures or specifics; rebuild the draft to push a change "
                "to stock or price."
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
        groups = {key: value for key, value in groups.items() if key in wanted}

    counts = {"pushed": 0, "failed": 0, "deferred": 0}
    listings_created = 0
    listings_updated = 0

    for group_key, group_items in sorted(groups.items()):
        managed = db.get_managed_listing(group_key)

        # A listing eBay already has but this API cannot see. Pushing it
        # would not update it -- it would create a second listing beside the
        # live one -- so it is skipped, and says so.
        legacy = [i for i in group_items if i.get("ebay_parent_id")]
        if managed is None and legacy:
            for item in group_items:
                _mark(db, item, STATUS_DEFERRED, counts)
            record("WARN", (
                f"{group_key or 'ungrouped'}: {len(group_items)} card(s) "
                f"skipped. The Inventory API cannot see listing "
                f"#{legacy[0]['ebay_parent_id']}, so a push would create a "
                f"duplicate beside it. Sync from eBay; if it stays "
                f"unmanaged, it was not created through this API."
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
                # Anything not confirmed pushed carries this run's reason.
                # Skipping items that already said "failed" meant a retry's
                # own error was discarded and the run reported nothing.
                if item["status"] != STATUS_PUSHED:
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
        f"{counts['deferred']} skipped; {listings_created} listing(s) "
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

    # 2. An offer per card, 25 to a call. One already on record is reused: a
    #    second offer for the same SKU is an error, and the offer id is the
    #    only handle that can change a price.
    description = _group_description(group_items, single)
    _create_offers(
        db, api,
        [item for item in live if not item.get("offer_id")],
        settings=settings,
        category_id=category_id,
        marketplace_id=marketplace_id,
        description=description,
        counts=counts,
        record=record,
    )
    live = [item for item in live if item["status"] != STATUS_FAILED]

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
    if not published:
        # Ask eBay before creating anything. Our records cannot distinguish
        # "eBay never published this" from "eBay published it and the reply
        # was lost" -- a timeout after the listing was created looks exactly
        # like a listing that was never created. Guessing wrong publishes a
        # second live listing for the same cards, which is the most expensive
        # mistake this module can make and the hardest to undo.
        existing = _already_published(api, live, record)
        if existing:
            db.upsert_managed_listing(
                group_key, ebay_parent_id=existing, pushed=True
            )
            managed = dict(managed or {"group_key": group_key})
            managed["ebay_parent_id"] = existing
            published = True
            record("WARN", (
                f"{group_key or 'ungrouped'}: eBay already has listing "
                f"#{existing} for these cards, so this is being treated as an "
                f"update. A previous attempt published it and lost the reply."
            ))

    if single:
        item = live[0]
        if not published:
            listing_id = api.publish_offer(str(item["offer_id"]))
            db.upsert_managed_listing(
                group_key, ebay_parent_id=listing_id, pushed=True
            )
            _record_cover(db, listing_id, cover_image_url)
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
    # Sorted by card number, because this order *is* the order eBay shows the
    # variation dropdown in. Left in plan order it comes out sorted by
    # manifest id -- the order the cards happened to be catalogued in, which
    # is meaningless to a buyer looking for 045/132. The CSV path has always
    # sorted here; the API path did not, and the first listings went up
    # scrambled.
    # Everything the listing should still hold afterwards -- not just what
    # this plan touched.
    #
    # Writing the group is a full replace, so a SKU missing from variantSKUs
    # is a card taken off sale. Building this from the plan's items alone
    # meant approving one card's quantity change and pushing it replaced a
    # 35-card listing with a 1-card listing: every other variation removed
    # from the live listing, immediately, with a SUCCESS in the log. A plan
    # is a diff, so anything it does not mention has to survive it.
    keep: Dict[str, Dict[str, Any]] = {}
    if published and (managed or {}).get("ebay_parent_id"):
        for card in db.get_cards_for_listing(str(managed["ebay_parent_id"])):
            keep[_sku_for(card)] = card
    for item in live:
        if item["action"] == ACTION_REMOVE:
            keep.pop(_sku_for(item), None)
            continue
        keep[_sku_for(item)] = item
    # A card whose offer was withdrawn above is off sale, so it must not be
    # written back into the group -- the mirror still links it to this
    # listing at quantity zero.
    for item in ends:
        keep.pop(_sku_for(item), None)

    kept = sorted(keep.values(), key=variation_sort_key)
    entries = [(card, _sku_for(card)) for card in kept]
    api.upsert_group(ebay_group_key, _group_payload(
        ebay_group_key,
        entries,
        title=generate_variation_title(
            set_name, condition=condition, template=title_template
        ),
        description=description,
        # Both from the full set, for the same reason: a one-card plan was
        # narrowing the listing-level aspects and could blank the cover.
        cover_image_url=cover_image_url or _first_image(kept),
        aspects=_uniform_aspects(kept, settings),
    ))
    db.upsert_managed_listing(
        group_key, inventory_item_group_key=ebay_group_key, pushed=True
    )

    if not published:
        listing_id = api.publish_group(ebay_group_key)
        db.upsert_managed_listing(
            group_key, ebay_parent_id=listing_id, pushed=True
        )
        _record_cover(db, listing_id, cover_image_url)
        record("INFO", (
            f"{group_key}: listed as #{listing_id} with {len(entries)} "
            f"variation(s)"
        ))
    else:
        listing_id = managed["ebay_parent_id"]
        _record_cover(db, listing_id, cover_image_url)
        record("INFO", (
            f"{group_key}: updated listing #{listing_id}, which now carries "
            f"{len(entries)} variation(s)"
        ))

    _confirm(db, live, listing_id, counts)
    return (0 if published else 1, 1 if published else 0)


def _create_offers(
    db: Database,
    api: Any,
    items: List[Dict[str, Any]],
    *,
    settings: Dict[str, str],
    category_id: str,
    marketplace_id: str,
    description: str,
    counts: Dict[str, int],
    record: Callable[[str, str], None],
) -> None:
    """
    Create the offers these cards need, in batches of 25.

    One call per card is what made a hundred-card listing take minutes: the
    offer loop was 94% of the requests. eBay's bulk endpoint does the same
    work in a twenty-fifth of the round trips.

    The care that the per-card loop earned is kept. An offer id is the only
    handle that can later change a card's price, so each one is written down
    as it comes back, and a SKU eBay reports as created *without* an id is
    chased up rather than shrugged at -- leaving it unrecorded would make the
    next push try to create a second offer for that SKU, which eBay refuses.
    A row eBay rejected fails its own card and no other.
    """
    if not items:
        return

    by_sku = {_sku_for(item): item for item in items}
    payloads = [
        {
            **_offer_payload(
                item,
                settings=settings,
                category_id=category_id,
                marketplace_id=marketplace_id,
                description=description,
            ),
        }
        for item in items
    ]

    for start in range(0, len(payloads), BULK_LIMIT):
        batch = payloads[start:start + BULK_LIMIT]
        try:
            rows = api.create_offers(batch)
        except Exception as exc:  # noqa: BLE001 - the batch, not the listing
            for entry in batch:
                item = by_sku.get(entry["sku"])
                if item is not None:
                    _mark(db, item, STATUS_FAILED, counts, str(exc))
                    record("ERROR", f"{entry['sku']}: {exc}")
            continue

        rejected = dict(api.failures(rows))
        for row in rows:
            sku = str(row.get("sku") or "")
            item = by_sku.get(sku)
            if item is None:
                continue
            if sku in rejected:
                _mark(db, item, STATUS_FAILED, counts, rejected[sku])
                record("ERROR", f"{sku}: {rejected[sku]}")
                continue

            offer_id = str(row.get("offerId") or "").strip()
            if not offer_id:
                offer_id = _recover_offer_id(api, sku, record)
            if not offer_id:
                _mark(db, item, STATUS_FAILED, counts, (
                    "eBay created the offer but returned no offer id, and it "
                    "could not be read back. Without it the price cannot be "
                    "changed later, so this card is left out rather than "
                    "listed with a handle nobody holds."
                ))
                continue

            db.set_variation_offer(item["manifest_id"], offer_id)
            item["offer_id"] = offer_id

        # A SKU eBay said nothing about at all is not a success.
        answered = {str(row.get("sku") or "") for row in rows}
        for entry in batch:
            sku = entry["sku"]
            item = by_sku.get(sku)
            if item is None or sku in answered or item.get("offer_id"):
                continue
            _mark(db, item, STATUS_FAILED, counts, (
                "eBay's response did not mention this SKU, so whether its "
                "offer was created is unknown."
            ))
            record("ERROR", f"{sku}: missing from eBay's bulk offer response")


def _recover_offer_id(
    api: Any, sku: str, record: Callable[[str, str], None]
) -> str:
    """Ask eBay for an offer id a bulk response did not include."""
    lookup = getattr(api, "offer_ids_for", None)
    if not callable(lookup):
        return ""
    try:
        ids = lookup(sku) or []
    except Exception as exc:  # noqa: BLE001 - recovery is best-effort
        record("WARN", f"{sku}: could not read the offer back from eBay: {exc}")
        return ""
    if ids:
        record("INFO", f"{sku}: offer id recovered from eBay ({ids[0]})")
        return str(ids[0])
    return ""


def _already_published(
    api: Any, items: List[Dict[str, Any]], record: Callable[[str, str], None]
) -> str:
    """
    The listing id eBay already holds for these cards, if any.

    One question, asked of the SKUs we are about to publish. A published offer
    carries the id of the listing it belongs to, so if any of them has one,
    this listing exists and must be updated rather than created again.
    """
    lookup = getattr(api, "published_listing_id", None)
    if not callable(lookup) or not items:
        return ""
    # One SKU is enough to answer. Publishing a group publishes every offer in
    # it, so if the listing exists the first card knows about it -- and asking
    # all hundred would put back the per-card round trips that bulk creation
    # just removed.
    sku = _sku_for(items[0])
    try:
        listing = lookup(sku)
    except Exception as exc:  # noqa: BLE001 - a read must not block a push
        record("WARN", (
            f"{sku}: could not check whether eBay already has a listing for "
            f"this card ({exc}). Continuing."
        ))
        return ""
    return str(listing or "")


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
        batch = requests[start:start + BULK_LIMIT]
        # As in the repricer: a whole-batch failure marks its own cards failed
        # and lets the other batches stand, rather than unwinding the push and
        # leaving the cards eBay already accepted unrecorded.
        try:
            rows = api.update_price_quantity(batch)
        except Exception as exc:  # noqa: BLE001 - attributed per card below
            for entry in batch:
                failed[str(entry.get("sku") or "")] = str(exc)
            continue
        for sku, message in api.failures(rows):
            failed[sku] = message

    for item in updates:
        sku = _sku_for(item)
        if sku in failed:
            _mark(db, item, STATUS_FAILED, counts, failed[sku])
            record("ERROR", f"{sku}: {failed[sku]}")


def _record_cover(db: Database, listing_id: str, cover_image_url: str) -> None:
    """
    Remember the cover a push chose, against the listing it created.

    Until this existed, a staged cover reached eBay and then vanished from our
    own records: the drafts page's choice lives in ``listing_plan_group``,
    keyed by plan, and nothing carried it across to the listing. Two things
    went wrong as a result -- the eBay Listings tab showed the listing as
    having no cover, and a later refresh, finding none recorded, replaced the
    cover on eBay with the first card's photo.

    Only an explicitly chosen cover is recorded. A fallback to the first
    card's picture is not a decision, and storing it would make it look like
    one on every screen that reads this.
    """
    url = str(cover_image_url or "").strip()
    if url and listing_id:
        db.set_listing_cover_image(str(listing_id), url)


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


def card_image(item: Dict[str, Any]) -> str:
    """
    The picture to list a card with: its own scan, or failing that the
    generic catalogue photo SortSwift carries for it.

    The queries resolve this already and hand over ``image_url``; the
    fallbacks are here for callers holding a row that predates it, so a
    stale dict cannot quietly turn into a listing with no photograph.
    """
    for key in ("image_url", "cdn_image", "stock_image"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _first_image(items: List[Dict[str, Any]]) -> str:
    for item in items:
        found = card_image(item)
        if found:
            return found
    return ""


def _uniform_aspects(
    items: List[Dict[str, Any]], settings: Dict[str, str]
) -> Dict[str, List[str]]:
    """
    The aspects every card in the group agrees on.

    A variation listing carries one set of listing-level aspects, so a value
    differing between cards (Card Name, Card Number) cannot be stated there --
    the variation axis expresses it instead.
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


def _confirm_group_cover(
    api: Any,
    ebay_group_key: str,
    cover_sent: Optional[str],
    record: Callable[[str, str], None],
) -> Optional[bool]:
    """
    Ask eBay what the group's cover now is, and say whether it took.

    Returns True when eBay reports the picture we sent, False when it does
    not *yet*, and None when the question could not be asked.

    A read-back rather than the write's own response, because this project
    has been bitten by the difference: a SKU rename returned Success and
    changed nothing, and the bulk calls answer 200 with failures in the body.
    What eBay reports holding is the only answer worth having.

    Note the asymmetry, which is deliberate. **True is definite** -- eBay
    reports our picture, so it took. **False is not**: this runs immediately
    after the write and eBay's own view of a listing can lag by minutes, so a
    mismatch is far more often propagation than rejection. It is reported as
    unconfirmed rather than failed, because sending somebody to chase a
    change that actually landed is worse than the silence this replaced.
    """
    if not cover_sent:
        return None
    try:
        live = api.get_group(ebay_group_key) or {}
    except Exception as exc:  # noqa: BLE001 - a failed check is not a failure
        record(
            "WARN",
            f"Could not read the group back from eBay to confirm the cover "
            f"photo, so it is unconfirmed rather than known good: {exc}",
        )
        return None

    images = live.get("imageUrls") or []
    applied = str(images[0]).strip() if images else ""
    if applied == str(cover_sent).strip():
        record("INFO", f"eBay confirms the cover photo: {applied}")
        return True

    record(
        "WARN",
        "eBay accepted the update but has not reported the new cover photo "
        f"back yet. Sent {cover_sent}; eBay currently holds "
        + (applied if applied else "none")
        + ". eBay's view of a listing can lag a few minutes, so this is "
        "usually just propagation -- open the listing again, or press "
        "Refresh on it, to re-check. If it persists, the cause is normally a "
        "picture eBay would not fetch: it must be at least 500 pixels on the "
        "longest side and reachable without credentials, and "
        "scripts/check_images.py --url <url> measures one.",
    )
    return False


def _cover_for_refresh(
    db: Database,
    api: Any,
    ebay_parent_id: str,
    ebay_group_key: str,
    cards: List[Dict[str, Any]],
    settings: Dict[str, str],
    record: Callable[[str, str], None],
) -> str:
    """
    The cover a refresh should send, in order of how much it is known.

    Writing an inventory item group is a *full replace*, so a refresh that
    guesses at the cover does not leave it alone -- it overwrites it. That is
    what happened to the first listing: nothing was recorded against it, the
    refresh fell back to the first card's photo, and the cover chosen on the
    drafts page was replaced on eBay.

    So: our own record first; then whatever eBay currently has, read back
    rather than assumed; and only then the first card's picture, said out loud
    because at that point it is a change and not a preservation.
    """
    recorded = db.get_listing_cover_image(ebay_parent_id)
    if recorded:
        return recorded

    # Read eBay's own answer before overwriting it. A group we did not set a
    # cover on may still have one -- set in Seller Hub, or by an earlier push.
    getter = getattr(api, "get_group", None)
    if callable(getter):
        try:
            existing = (getter(ebay_group_key) or {}).get("imageUrls") or []
        except Exception as exc:  # noqa: BLE001 - a read must not stop a repair
            existing = []
            record("WARN", f"could not read the current cover from eBay: {exc}")
        if existing:
            url = str(existing[0]).strip()
            if url:
                # Recorded now, so the next refresh does not have to ask.
                db.set_listing_cover_image(str(ebay_parent_id), url)
                record("INFO", f"keeping the cover eBay already has: {url}")
                return url

    account = str(settings.get("cover_image_url") or "").strip()
    if account:
        return account

    fallback = _first_image(cards)
    if fallback:
        record("WARN", (
            "no cover photo is recorded for this listing and eBay reports "
            "none, so the first card's picture is being used. Set a cover on "
            "the eBay Listings tab to choose one."
        ))
    return fallback


def refresh_listing(
    db: Database,
    api: Any,
    ebay_parent_id: str,
    user_id: Optional[int] = None,
    log: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, Any]:
    """
    Re-send what a live listing is made of, without changing what it sells.

    Pictures, item specifics, the title and the description live on the
    inventory items and the group -- not on the plan -- so once a listing is
    up, a correction to any of them has no route through the drafts page: the
    plan is a diff of quantities and prices, and a picture change produces no
    diff at all. The first real listing went live with the card backs on it
    and nothing could reach in to fix it.

    Quantity comes from what eBay is known to hold and price is not sent, so
    this is a repair rather than a repricing: it cannot move stock or money.
    Only a listing we created ourselves can be refreshed; a File Exchange
    listing is invisible to this API.
    """
    logs: List[Dict[str, str]] = []

    def record(level: str, message: str) -> None:
        logs.append({"level": level, "message": message})
        if log:
            log(level, message)

    managed = db.get_managed_listing_by_parent(ebay_parent_id)
    if managed is None:
        raise PushError(
            f"listing #{ebay_parent_id} is not managed through this API, so "
            f"its contents cannot be refreshed. Sync from eBay first."
        )

    cards = db.get_cards_for_listing(ebay_parent_id)
    if not cards:
        raise PushError(
            f"no cards are linked to listing #{ebay_parent_id}. Run a Module "
            f"B sync first."
        )

    settings = db.get_listing_settings(
        user_id=SHARED_SCOPE if user_id is None else user_id
    )
    category_id = str(settings.get("category_id") or "183454")
    marketplace_id = str(settings.get("marketplace_id") or "EBAY_US")
    option_template = (
        settings.get("variation_option_template")
        or DEFAULT_VARIATION_OPTION_TEMPLATE
    )
    title_template = settings.get(
        "variation_title_template",
        "{set_name}: Pick Your Card - {condition} - Complete Your Set",
    )

    group_key = str(managed["group_key"])
    single = is_single(group_key) or not group_key
    entries: List[Tuple[Dict[str, Any], str]] = []
    payloads = []
    # Card-number order, for the same reason as a create: this is the order
    # the variation dropdown appears in. Sorted here rather than in SQL
    # because a card number is not a number -- "10/132" sorts before "2/132"
    # as text, and "TG12/TG30" has no integer to sort on at all.
    for card in sorted(cards, key=variation_sort_key):
        # The quantity eBay is known to hold, not what we would like it to
        # be: a repair must not become a stock change.
        card = dict(card)
        card["proposed_qty"] = int(card.get("last_known_qty") or 0)
        sku = _sku_for(card)
        option_name = build_variation_option_name(
            card.get("product_name") or "", card.get("card_number") or "",
            option_template,
        )
        payloads.append({
            "sku": sku,
            "locale": locale_for(marketplace_id),
            **_inventory_item_payload(
                card,
                settings=settings,
                category_id=category_id,
                option_name=option_name,
                is_variation=not single,
            ),
        })
        entries.append((card, sku))

    failures: Dict[str, str] = {}
    for start in range(0, len(payloads), BULK_LIMIT):
        rows = api.upsert_items(payloads[start:start + BULK_LIMIT])
        for sku, message in api.failures(rows):
            failures[sku] = message
            record("ERROR", f"{sku}: {message}")

    refreshed = len(payloads) - len(failures)

    cover_sent = None
    cover_verified = None
    # None when eBay was never asked to publish -- a single listing, or a
    # refresh with nothing left to send. True or False only when it was.
    republished = None

    if not single and refreshed:
        set_name, _, condition = group_key.partition("|")
        ebay_group_key = (
            managed.get("inventory_item_group_key")
            or inventory_group_key(group_key)
        )
        kept = [(card, sku) for card, sku in entries if sku not in failures]
        cover_sent = _cover_for_refresh(
            db, api, ebay_parent_id, ebay_group_key,
            [c for c, _ in kept], settings, record,
        )
        api.upsert_group(ebay_group_key, _group_payload(
            ebay_group_key,
            kept,
            title=generate_variation_title(
                set_name, condition=condition, template=title_template
            ),
            description=_group_description([c for c, _ in kept], single),
            cover_image_url=cover_sent,
            aspects=_uniform_aspects([c for c, _ in kept], settings),
        ))
        cover_verified = _confirm_group_cover(
            api, ebay_group_key, cover_sent, record
        )

        # Put the group back on sale, which is the other half of "re-send
        # what this listing is made of".
        #
        # Withdrawing an offer keeps the offer and leaves its status at
        # UNPUBLISHED, and naming its SKU in the group again does not revive
        # it. A live 123-card listing was found in exactly that state: the
        # group named all 123, every offer held the right quantity and the
        # right pictures, every offer was unpublished, and a buyer saw one
        # card. Refresh rebuilt the group and changed nothing a buyer could
        # see, because nothing here published anything. Recovery needed a
        # script.
        #
        # That state is also reachable with no bug involved: ending a card
        # withdraws its offer, and restocking it later produces an update,
        # which never publishes. So Refresh owns this -- it is the button
        # whose job is making eBay match us.
        #
        # Published unconditionally rather than after reading every offer's
        # status to find out whether it is needed: this is one call against
        # one per card, and republishing a group that is already on sale is
        # a no-op that returns the id it already has. The cost of asking
        # first is the thing being avoided.
        try:
            listing_id = api.publish_group(ebay_group_key)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            # The items and the group were written, so this is not a failed
            # refresh; it is a refresh that could not put the result on sale.
            # Publishing is all-or-nothing, so one invalid card blocks the
            # whole group -- and eBay's message names it.
            republished = False
            record("ERROR", (
                f"the variations were re-sent but could not be put back on "
                f"sale: {exc}"
            ))
        else:
            republished = True
            if str(listing_id) != str(ebay_parent_id):
                # eBay published the group somewhere other than the listing
                # it belonged to, which means there may now be two. The
                # mirror follows eBay, because leaving it on the old id would
                # send every later write to a listing eBay no longer
                # associates with these offers.
                db.upsert_managed_listing(
                    group_key, ebay_parent_id=str(listing_id), pushed=True
                )
                record("ERROR", (
                    f"eBay published this group as #{listing_id}, not "
                    f"#{ebay_parent_id}, so there may now be two listings "
                    f"for it. Check both before pushing again."
                ))

    # Only real failures downgrade the summary. An unconfirmed cover has
    # already said so on its own line, and is usually eBay lagging.
    # Anything that recorded an ERROR costs the SUCCESS, whatever else the
    # refresh managed to do. Derived from what was actually logged rather than
    # from a list of the ways it can go wrong, because the list kept missing
    # one: a publish that landed on a *different* listing reported the
    # duplicate as an error and then summarised the run as a success, which
    # is the same false reassurance that hid the original damage.
    #
    # An unconfirmed cover is deliberately not an error -- it says so on its
    # own line and is usually eBay lagging.
    level = "SUCCESS" if not failures and not any(
        entry["level"] == "ERROR" for entry in logs
    ) else "WARN"
    record(
        level,
        f"Listing #{ebay_parent_id}: refreshed {refreshed} variation(s)"
        + (f", {len(failures)} failed" if failures else "")
        # What eBay was asked and what it answered -- not a claim about what
        # a buyer can now see, which has not been read back.
        + (", and eBay accepted the publish" if republished else ""),
    )
    return {
        "ebay_parent_id": str(ebay_parent_id),
        "refreshed": refreshed,
        "failed": len(failures),
        # None when there was no group write to check -- a single listing, or
        # nothing left to send. True or False only when eBay was asked.
        "cover_sent": cover_sent,
        "cover_verified": cover_verified,
        # True when eBay accepted a publish, False when it refused one, None
        # when it was never asked.
        "republished": republished,
        "logs": logs,
    }
