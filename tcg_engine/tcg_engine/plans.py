"""
Draft plans: what we intend to change on eBay, before anything is changed.

Every producer of change funnels through here -- a SortSwift batch, a manual
quantity edit, a price refresh, a photo change, a regrouping -- instead of
generating a CSV for somebody to upload by hand. The plan is reviewed on the
drafts page and only an *approved* plan may be pushed. See
``docs/ebay-api-design.md`` for why the staging is ours rather than eBay's.

Two properties of this module matter more than its size.

**A plan is derived from stored state, not from the uploaded file.** After a
batch has run, the catalogue already holds the desired quantity and price, and
``ebay_variations`` holds what eBay is known to hold. The difference between
those two is the plan. That keeps this module independent of CSV parsing, lets
a plan be rebuilt at any time, and means the suppression rule is written once
here instead of once per producer.

**Validation happens before approval, not at push time.**
``publishOfferByInventoryItemGroup`` fails if any single offer in the group is
invalid, so one card missing a required field blocks a listing that may hold
hundreds of cards -- and it fails *after* approval, when whoever approved it
has walked away. Catching that on the drafts page is the page's real job.
"""

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .db import SHARED_SCOPE, Database

# What a plan item asks for. Quantity and price travel together rather than as
# separate actions because eBay updates them in one call
# (bulkUpdatePriceQuantity), so splitting them would double the call count and
# let a listing sit briefly at a new price with an old quantity.
ACTION_CREATE = "create_listing"
ACTION_UPDATE = "update"
ACTION_ZERO_OUT = "zero_out"
ACTION_REMOVE = "remove_from_group"
ACTION_END = "end_listing"

ACTIONS = frozenset(
    {ACTION_CREATE, ACTION_UPDATE, ACTION_ZERO_OUT, ACTION_REMOVE, ACTION_END}
)

STATUS_PENDING = "pending"
STATUS_EXCLUDED = "excluded"
STATUS_PUSHED = "pushed"
STATUS_FAILED = "failed"
STATUS_DEFERRED = "deferred"

ITEM_STATUSES = frozenset(
    {
        STATUS_PENDING,
        STATUS_EXCLUDED,
        STATUS_PUSHED,
        STATUS_FAILED,
        STATUS_DEFERRED,
    }
)

PLAN_DRAFT = "draft"
PLAN_APPROVED = "approved"
PLAN_PUSHING = "pushing"
PLAN_PUSHED = "pushed"
PLAN_PARTIAL = "partial"
PLAN_FAILED = "failed"
PLAN_DISCARDED = "discarded"

# A single listing is still a listing, so it gets a group key of its own
# rather than a null. That way the push worker and the drafts page treat
# singles and variation listings uniformly, and get_plan_groups counts one row
# per eBay listing either way.
SINGLE_PREFIX = "single:"

EBAY_TITLE_LIMIT = 80

# Prices are compared to the cent. Floats accumulate error, and a plan that
# re-proposes 4.9999999 against a stored 5.00 on every rebuild would show a
# permanent phantom change on the drafts page.
PRICE_EPSILON = 0.005


class PlanError(RuntimeError):
    """A plan could not be built or approved."""


def variation_group_key(set_name: str, condition: str) -> str:
    """
    The key identifying one multi-variation listing.

    (set, condition) rather than set alone, because eBay applies one
    ConditionID per listing: a set holding both NM and LP cards has to become
    two listings.
    """
    return f"{(set_name or '').strip()}|{(condition or '').strip()}"


def single_group_key(manifest_id: str) -> str:
    return f"{SINGLE_PREFIX}{manifest_id}"


def is_single(group_key: Optional[str]) -> bool:
    return bool(group_key) and group_key.startswith(SINGLE_PREFIX)


def desired_price(card: Dict[str, Any]) -> Optional[float]:
    """
    The price we want the card listed at.

    ``price`` is what the pricing rules produced and is authoritative. Falling
    back to the market price would bypass the tiers and the condition
    multiplier, so an unpriced card returns None and is reported as a
    validation failure rather than being listed at raw market.
    """
    for key in ("price",):
        value = card.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return round(number, 2)
    return None


def desired_quantity(card: Dict[str, Any]) -> int:
    try:
        return max(0, int(card.get("quantity") or 0))
    except (TypeError, ValueError):
        return 0


def _is_live(card: Dict[str, Any]) -> bool:
    """Whether the store mirror believes this card is on eBay right now."""
    parent = str(card.get("ebay_parent_id") or "").strip()
    return bool(parent)


def _changed(proposed: Optional[float], known: Optional[float]) -> bool:
    """
    Whether a value differs from what eBay is known to hold.

    An unknown ``known`` counts as changed. That is the conservative default
    the CSV path already uses: nothing is suppressed until a sync has told us
    what eBay actually holds, because suppressing against a value we never
    learned would silently drop a real change.
    """
    if proposed is None:
        return False
    if known is None:
        return True
    return abs(float(proposed) - float(known)) > PRICE_EPSILON


def group_key_for(
    card: Dict[str, Any],
    group_by_set: bool,
    single_threshold: float,
) -> str:
    """
    Which listing this card belongs in, by the configured rules.

    Mirrors the CSV path exactly: with grouping off everything is a single;
    otherwise a card at or above the threshold is a single and the rest are
    grouped by set and condition.
    """
    if not group_by_set:
        return single_group_key(card["manifest_id"])
    price = desired_price(card)
    if price is not None and price >= single_threshold:
        return single_group_key(card["manifest_id"])
    return variation_group_key(card.get("set_name", ""), card.get("condition", ""))


def _has_export_specifics(card: Dict[str, Any]) -> bool:
    """
    Whether this card carries the item specifics its eBay export supplied.

    ``manifest.ebay_fields_json`` is written while a batch is catalogued, from
    the ``C:``-prefixed columns of the eBay-flavoured SortSwift export. A card
    catalogued before that existed -- or from the wrong export -- has nothing
    here, and the only remedy is to upload the export again.
    """
    raw = card.get("ebay_fields_json")
    if not raw:
        return False
    try:
        fields = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if not isinstance(fields, dict):
        return False
    specifics = fields.get("item_specifics")
    return isinstance(specifics, dict) and bool(specifics)


def validate_card(
    card: Dict[str, Any], settings: Dict[str, str], action: str
) -> List[str]:
    """
    Everything wrong with this card that would make eBay refuse the listing.

    Only the checks that can be made locally, and only the ones eBay actually
    enforces. A check that produces a false positive is worse than no check:
    it blocks an approval for a listing that would have succeeded, and the
    user has no way to overrule it.
    """
    problems: List[str] = []

    # A zero-out or a removal touches quantity only, so the fields an Add
    # needs are irrelevant. Validating them anyway would block the very
    # operation used to react to a problem.
    if action in (ACTION_ZERO_OUT, ACTION_REMOVE, ACTION_END):
        return problems

    if desired_price(card) is None:
        problems.append(
            "no price: the pricing rules produced nothing for this card, and "
            "a listing cannot be priced from an unknown market value"
        )

    if not str(card.get("product_name") or "").strip():
        problems.append("no card name")

    if action == ACTION_CREATE:
        # eBay marks around twenty item specifics as required on a card
        # listing, and most of them -- Card Type, Manufacturer, Graded, Card
        # Size, Character, Stage, the two Country fields, Age Level, Year
        # Manufactured, Autographed, Material -- exist nowhere but the eBay
        # export. They cannot be derived from a card's identity.
        #
        # Without them the Add file is built, looks plausible and creates
        # listings missing fields eBay demands. Blocking here is the
        # difference between a clear "re-upload the export" and discovering
        # it from a rejection report over hundreds of rows.
        if not _has_export_specifics(card):
            problems.append(
                "no eBay item specifics on record for this card, so a listing "
                "built from it would be missing fields eBay requires. Re-upload "
                "the SortSwift eBay export (export_eBay_<date>.csv) to supply "
                "them"
            )
        if not str(settings.get("category_id") or "").strip():
            problems.append("no eBay category is configured")
        if not str(settings.get("seller_postal_code") or "").strip():
            problems.append(
                "no postal code: an Add without an item location is rejected "
                "with error 10009"
            )
        if not str(settings.get("default_game") or "").strip():
            problems.append(
                "no Game value: eBay requires it on card listings and only "
                "accepts values from its own list"
            )
        for key, label in (
            ("shipping_profile_name", "shipping"),
            ("return_profile_name", "return"),
            ("payment_profile_name", "payment"),
        ):
            if not str(settings.get(key) or "").strip():
                problems.append(f"no {label} business policy is configured")

    return problems


def validate_group_title(
    group_key: str, set_name: str, condition: str, settings: Dict[str, str]
) -> List[str]:
    """
    Whether the listing title this group would get fits eBay's limit.

    Reported against the group rather than a card because the title belongs to
    the listing. Singles are titled from the card and are checked per card.
    """
    if is_single(group_key):
        return []
    template = settings.get(
        "variation_title_template",
        "{set_name}: Pick Your Card - {condition} - Complete Your Set",
    )
    title = template.replace("{set_name}", set_name or "").replace(
        "{condition}", condition or ""
    )
    if len(title) > EBAY_TITLE_LIMIT:
        return [
            f"title is {len(title)} characters, over eBay's "
            f"{EBAY_TITLE_LIMIT}-character limit"
        ]
    return []


def derive_plan_items(
    cards: Iterable[Dict[str, Any]],
    settings: Dict[str, str],
    group_by_set: bool = True,
    single_threshold: float = 5.00,
) -> List[Dict[str, Any]]:
    """
    Turn catalogue rows into plan items, one per card that needs a change.

    ``cards`` rows must carry the manifest columns plus the ``ebay_parent_id``,
    ``last_known_qty`` and ``last_known_price`` of the store mirror -- i.e.
    what :meth:`Database.get_cards_for_planning` returns.

    A card whose desired quantity and price already match what eBay is known
    to hold produces nothing. That is the whole point: a plan should be the
    diff, so an empty plan is the correct outcome of a dump that changed
    nothing, not evidence of a bug.
    """
    items: List[Dict[str, Any]] = []

    for card in cards:
        quantity = desired_quantity(card)
        price = desired_price(card)
        live = _is_live(card)
        known_qty = card.get("last_known_qty")
        known_price = card.get("last_known_price")

        if not live:
            # Never create a listing for a card with no copies. eBay would
            # reject a zero-quantity Add anyway, and offering to list stock
            # that does not exist is how a store oversells.
            if quantity <= 0:
                continue
            action = ACTION_CREATE
        elif quantity <= 0:
            # Live, but sold out. The listing stays and goes to zero, which is
            # also how eBay wants a variation with sales retired.
            if not _changed(0, known_qty):
                continue
            action = ACTION_ZERO_OUT
        elif _changed(quantity, known_qty) or _changed(price, known_price):
            action = ACTION_UPDATE
        else:
            continue

        group_key = group_key_for(card, group_by_set, single_threshold)
        problems = validate_card(card, settings, action)
        items.append(
            {
                "manifest_id": card["manifest_id"],
                "group_key": group_key,
                "action": action,
                "proposed_qty": quantity,
                "proposed_price": price,
                "observed_qty": known_qty,
                "observed_price": known_price,
                "status": STATUS_PENDING,
                "validation": json.dumps(problems) if problems else None,
            }
        )

    return items


def read_settings(db: Database, user_id: int = SHARED_SCOPE) -> Dict[str, Any]:
    """
    The listing settings a plan is built against, already coerced.

    Read once per plan. The settings are a table, and reading them inside a
    per-card loop is the mistake that made a few thousand rows take minutes.
    """
    settings = db.get_listing_settings(user_id=user_id)
    try:
        threshold = float(settings.get("single_threshold", "5.00"))
    except (TypeError, ValueError):
        threshold = 5.00
    grouped = str(settings.get("group_by_set", "true")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    return {
        "settings": settings,
        "single_threshold": threshold,
        "group_by_set": grouped,
    }


def build_plan(
    db: Database,
    user_id: int,
    source: str = "manual",
    source_ref: Optional[str] = None,
    note: Optional[str] = None,
    replace_open: bool = True,
) -> Dict[str, Any]:
    """
    Compute a fresh plan for this user and store it as a draft.

    ``replace_open`` discards any existing draft first. A draft is a snapshot
    of a diff, and once the catalogue moves underneath it the old draft is
    describing a change that no longer applies -- so the default is to replace
    rather than to accumulate. Only one draft per user can exist anyway; the
    schema enforces that.
    """
    resolved = read_settings(db, user_id)

    with db.session():
        existing = db.get_open_plan_id(user_id)
        if existing is not None:
            if not replace_open:
                raise PlanError(
                    f"plan {existing} is already open; approve or discard it first"
                )
            db.delete_plan(existing)

        cards = db.get_cards_for_planning()
        items = derive_plan_items(
            cards,
            resolved["settings"],
            group_by_set=resolved["group_by_set"],
            single_threshold=resolved["single_threshold"],
        )
        plan_id = db.create_plan(
            user_id, source=source, source_ref=source_ref, note=note
        )
        db.add_plan_items(plan_id, items)

    return {
        "plan_id": plan_id,
        "item_count": len(items),
        "invalid_count": sum(1 for i in items if i.get("validation")),
        "group_count": len({i["group_key"] for i in items}),
        "actions": _count_actions(items),
    }


def _count_actions(items: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    return counts


def plan_blockers(db: Database, plan_id: int) -> List[Dict[str, Any]]:
    """
    Every reason this plan cannot be approved, grouped by listing.

    Excluded items are skipped: excluding a broken card is exactly how a user
    is meant to unblock a listing whose other cards are fine.
    """
    plan = db.get_plan(plan_id)
    if plan is None:
        raise PlanError(f"no such plan: {plan_id}")
    resolved = read_settings(db, plan["user_id"])
    settings = resolved["settings"]

    by_group: Dict[str, Dict[str, Any]] = {}
    for item in db.get_plan_items(plan_id):
        if item["status"] == STATUS_EXCLUDED:
            continue
        group_key = item.get("group_key") or ""
        entry = by_group.setdefault(
            group_key,
            {
                "group_key": group_key,
                "set_name": item.get("set_name"),
                "condition": item.get("condition"),
                "problems": [],
            },
        )
        for problem in json.loads(item["validation"] or "[]"):
            entry["problems"].append(
                {
                    "item_id": item["id"],
                    "manifest_id": item["manifest_id"],
                    "product_name": item.get("product_name"),
                    "problem": problem,
                }
            )

    for group_key, entry in by_group.items():
        for problem in validate_group_title(
            group_key, entry.get("set_name") or "", entry.get("condition") or "", settings
        ):
            entry["problems"].append(
                {
                    "item_id": None,
                    "manifest_id": None,
                    "product_name": None,
                    "problem": problem,
                }
            )

    return [entry for entry in by_group.values() if entry["problems"]]


def approve_plan(db: Database, plan_id: int, approved_by: int) -> Dict[str, Any]:
    """
    Mark a plan approved, which is the only thing that authorises an eBay write.

    Refuses while any included item has a validation problem. This is the
    guard that keeps a push from dying half way: eBay fails a whole variation
    group if one of its offers is invalid, and it does so after approval.
    """
    plan = db.get_plan(plan_id)
    if plan is None:
        raise PlanError(f"no such plan: {plan_id}")
    if plan["status"] != PLAN_DRAFT:
        raise PlanError(
            f"plan {plan_id} is {plan['status']}, and only a draft can be approved"
        )

    blockers = plan_blockers(db, plan_id)
    if blockers:
        count = sum(len(b["problems"]) for b in blockers)
        raise PlanError(
            f"{count} problem(s) across {len(blockers)} listing(s) must be "
            "fixed or excluded before this plan can be approved"
        )

    included = [
        item
        for item in db.get_plan_items(plan_id)
        if item["status"] != STATUS_EXCLUDED
    ]
    if not included:
        raise PlanError("every item in this plan is excluded; nothing to approve")

    db.set_plan_status(plan_id, PLAN_APPROVED, approved_by=approved_by)
    return {"plan_id": plan_id, "approved_items": len(included)}


def revalidate_item(db: Database, item_id: int) -> List[str]:
    """
    Re-run validation for one item after it has been edited.

    Called when a price or grouping is changed by hand, so the drafts page
    never shows a stale blocker for a problem the user has just fixed.
    """
    item = db.get_plan_item(item_id)
    if item is None:
        raise PlanError(f"no such plan item: {item_id}")
    plan = db.get_plan(item["plan_id"])
    resolved = read_settings(db, plan["user_id"] if plan else SHARED_SCOPE)

    card = db.get_manifest_by_id(item["manifest_id"]) or {}
    # The proposal, not the catalogue, is what will be pushed, so validation
    # has to see the edited numbers.
    card = dict(card)
    if item.get("proposed_price") is not None:
        card["price"] = item["proposed_price"]
    if item.get("proposed_qty") is not None:
        card["quantity"] = item["proposed_qty"]

    problems = validate_card(card, resolved["settings"], item["action"])
    db.update_plan_item(
        item_id, validation=json.dumps(problems) if problems else None
    )
    return problems
