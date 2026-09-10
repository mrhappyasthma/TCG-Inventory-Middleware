"""
Turn an approved plan into the eBay files it authorised.

This is what makes the drafts page the funnel rather than a parallel view.
Before it existed, Module A generated the Add and Revise files at upload time
-- *before* the draft existed -- so regrouping a card, changing a price or
excluding one had no way to reach eBay at all. The decision was recorded and
then discarded.

Two properties matter more than the size of this module.

**The plan's grouping is honoured, not recomputed.** A group key is stored on
each item, and moving a card between listings is one of the three edits the
page offers. Re-deriving the grouping from prices and settings here would
quietly discard those edits and produce a file that disagreed with the screen
that was approved.

**The Add rows are assembled by the same builder Module A uses.**
``build_add_rows`` was extracted for this. A second copy would be free to
drift in exactly the rules that are hardest to get right and least visible
when wrong: the parent row leaving ``Relationship`` empty, the option list
matching the child order, and a per-variation ``PicURL`` naming its option.
"""

import csv
import io
import json
from typing import Any, Dict, List, Optional

from .batches import (
    ADD_HEADERS,
    CONDITION_DESCRIPTOR_COLUMN,
    GAME_ITEM_SPECIFIC,
    POLICY_SETTING_COLUMNS,
    REVISE_HEADERS,
    UNGRADED_CONDITION_ID,
    build_add_rows,
    build_variation_option_name,
    resolve_condition_descriptor,
)
from .db import SHARED_SCOPE, Database
from .plans import (
    ACTION_CREATE,
    ACTION_END,
    ACTION_REMOVE,
    ACTION_UPDATE,
    ACTION_ZERO_OUT,
    PLAN_DRAFT,
    STATUS_EXCLUDED,
    PlanError,
    is_single,
)

DEFAULT_VARIATION_OPTION_TEMPLATE = "{name} ({card_number})"


def _card_entry(item: Dict[str, Any], option_template: str,
                category_id: str, descriptor_style: str) -> Dict[str, Any]:
    """
    Rebuild the Add-row inputs for one card from the plan and the catalogue.

    The quantity and price come from the *plan*, not the catalogue: they are
    what was reviewed and approved, and an edit to either is the common case.
    """
    try:
        fields = json.loads(item.get("ebay_fields_json") or "{}")
    except (ValueError, TypeError):
        fields = {}
    if not isinstance(fields, dict):
        fields = {}

    condition_name = item.get("condition") or ""
    # The export's own values win. Falling back keeps a card catalogued before
    # this data was persisted from being unlistable -- the descriptor is
    # derivable from the condition, and ConditionID is 4000 for every ungraded
    # card, so neither is a guess about the card itself.
    condition_id = str(fields.get("condition_id") or UNGRADED_CONDITION_ID)
    descriptor = fields.get("condition_descriptor") or resolve_condition_descriptor(
        condition_name, category_id=category_id, style=descriptor_style
    )

    price = item.get("proposed_price")
    return {
        "manifest_id": item["manifest_id"],
        # The label eBay knows, when it knows one. For a new listing there is
        # none yet, so it is built from the identity the same way Module A
        # builds it.
        "custom_label": item.get("custom_label") or _new_custom_label(item),
        "product_name": item.get("product_name") or "",
        "set_name": item.get("set_name") or "",
        "condition_name": condition_name,
        "condition_id": condition_id,
        "condition_descriptor": descriptor or "",
        "item_specifics": dict(fields.get("item_specifics") or {}),
        "printing": item.get("printing") or "",
        "quantity": int(item.get("proposed_qty") or 0),
        "price": float(price) if price is not None else 0.0,
        "cdn_image": item.get("cdn_image") or "",
        "cdn_back_image": fields.get("cdn_back_image") or "",
        "stock_image": fields.get("stock_image") or "",
        "card_number": item.get("card_number") or "",
        "option_name": build_variation_option_name(
            item.get("product_name") or "",
            item.get("card_number") or "",
            option_template,
        ),
    }


def _new_custom_label(item: Dict[str, Any]) -> str:
    """
    The Custom Label for a card not yet on eBay.

    Mirrors Module A: the manifest id with the bin/remark appended, sanitised
    the same way. A card already on eBay keeps the label eBay reports instead,
    because the suffix cannot be reconstructed reliably once it changes.
    """
    remark = str(item.get("remarks") or "").strip()
    if not remark or remark.lower() in ("no remark", "none"):
        return item["manifest_id"]
    clean = "".join(c if c.isalnum() or c in "-_" else "_" for c in remark)
    return f"{item['manifest_id']}-{clean}"


def build_plan_exports(
    db: Database, plan_id: int, user_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Build the Add and Revise files an approved plan authorised.

    Refuses a draft: the files exist to be uploaded, and generating them from
    an unapproved plan would hand out something nobody signed off.
    """
    plan = db.get_plan(plan_id)
    if plan is None:
        raise PlanError(f"no such plan: {plan_id}")
    if plan["status"] == PLAN_DRAFT:
        raise PlanError("approve the plan before building its files")

    scope = plan["user_id"] if user_id is None else user_id
    settings = db.get_listing_settings(user_id=scope)

    category_id = settings.get("category_id") or "183454"
    title_template = settings.get(
        "variation_title_template",
        "{set_name}: Pick Your Card - {condition} - Complete Your Set",
    )
    option_template = (
        settings.get("variation_option_template")
        or DEFAULT_VARIATION_OPTION_TEMPLATE
    )
    descriptor_style = settings.get("condition_descriptor_style", "")
    cover_image_url = settings.get("cover_image_url") or ""

    common_add_fields = {"PostalCode": settings.get("seller_postal_code") or ""}
    active_policies: Dict[str, str] = {}
    for setting_key, column in POLICY_SETTING_COLUMNS:
        value = (settings.get(setting_key) or "").strip()
        if value:
            active_policies[column] = value
    common_add_fields.update(active_policies)
    default_game = (settings.get("default_game") or "").strip()

    items = [
        item
        for item in db.get_plan_items(plan_id)
        if item["status"] != STATUS_EXCLUDED
    ]

    staged_variations: Dict[tuple, List[Dict[str, Any]]] = {}
    staged_singles: List[Dict[str, Any]] = []
    revise_rows: List[Dict[str, str]] = []
    unlistable: List[Dict[str, str]] = []
    item_specific_outputs = {GAME_ITEM_SPECIFIC}

    for item in items:
        action = item["action"]

        if action in (ACTION_UPDATE, ACTION_ZERO_OUT, ACTION_END, ACTION_REMOVE):
            # A Revise row must address the listing by the id and label eBay
            # itself reports. Rebuilding either from a card's identity would
            # address a variation that does not exist.
            parent = str(item.get("ebay_parent_id") or "").strip()
            label = str(item.get("custom_label") or "").strip()
            if not parent or not label:
                unlistable.append({
                    "manifest_id": item["manifest_id"],
                    "reason": (
                        "no eBay item number or custom label is on record, so "
                        "there is nothing to revise. Run a Module B sync."
                    ),
                })
                continue
            quantity = int(item.get("proposed_qty") or 0)
            price = item.get("proposed_price")
            revise_rows.append({
                "Action": "Revise",
                "ItemID": parent,
                "CustomLabel": label,
                "Quantity": str(quantity),
                # Price is left blank on a zero-out so the listed price is
                # untouched: the card is going out of stock, not on sale.
                "Price": (
                    "" if action == ACTION_ZERO_OUT or price is None
                    else f"{float(price):.2f}"
                ),
            })
            continue

        if action != ACTION_CREATE:
            continue

        entry = _card_entry(item, option_template, category_id, descriptor_style)
        if default_game:
            # eBay only accepts Game values from its own per-category list, so
            # the configured default overrides whatever the export said.
            entry["item_specifics"][GAME_ITEM_SPECIFIC] = default_game
        if not entry["condition_descriptor"]:
            unlistable.append({
                "manifest_id": item["manifest_id"],
                "reason": (
                    f"condition {entry['condition_name']!r} has no eBay "
                    f"ungraded grade, so no Condition Descriptor could be set."
                ),
            })
            continue
        item_specific_outputs.update(entry["item_specifics"].keys())

        group_key = item.get("group_key") or ""
        if is_single(group_key) or not group_key:
            staged_singles.append(entry)
        else:
            # The plan's own grouping, split back into (set, condition). Not
            # recomputed from price and settings: moving a card between
            # listings is one of the edits the drafts page offers, and
            # re-deriving it would discard that edit silently.
            set_name, _, condition = group_key.partition("|")
            staged_variations.setdefault((set_name, condition), []).append(entry)

    add_rows = build_add_rows(
        staged_variations,
        staged_singles,
        category_id=category_id,
        title_template=title_template,
        cover_image_url=cover_image_url,
        common_add_fields=common_add_fields,
    )

    add_headers = (
        ADD_HEADERS + list(active_policies) + sorted(item_specific_outputs)
    )
    return {
        "add_csv": _write_csv(add_headers, add_rows),
        "revise_csv": _write_csv(REVISE_HEADERS, revise_rows),
        "listing_count": len(staged_variations) + len(staged_singles),
        "variation_listing_count": len(staged_variations),
        "single_listing_count": len(staged_singles),
        "add_card_count": sum(len(c) for c in staged_variations.values())
        + len(staged_singles),
        "revise_count": len(revise_rows),
        "unlistable": unlistable,
    }


def _write_csv(headers: List[str], rows: List[Dict[str, Any]]) -> str:
    output = io.StringIO()
    # extrasaction="ignore" because a row carries whatever item specifics its
    # own card had, while the header set is the union across every card.
    writer = csv.DictWriter(
        output, fieldnames=headers, lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()
