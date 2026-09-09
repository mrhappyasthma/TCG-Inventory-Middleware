import csv
import hashlib
import io
import os
import re
from typing import Dict, Any, List, Optional
from .csvtools import find_column as _find_column, read_csv_text, strip_bom
from .db import Database, SHARED_SCOPE, apply_pricing_rules


# eBay File Exchange / Seller Hub Headers
# A File Exchange *upload* identifies an existing listing by "ItemID". "Item
# Number" is what the Active Listings *report* calls it, which is a different
# document; using the report's spelling in an upload leaves the row with no
# identifier.
REVISE_HEADERS = ["Action", "ItemID", "CustomLabel", "Quantity", "Price"]

# A cover-photo revision only needs to identify the listing and supply the
# picture. Sending the minimum avoids overwriting fields we were not asked to
# touch.
COVER_REVISE_HEADERS = ["Action", "ItemID", "PicURL"]


def build_cover_photo_revise_csv(item_id: str, cover_image_url: str) -> str:
    """
    Build a File Exchange Revise file that sets one listing's photo.

    Note that eBay REPLACES a listing's whole picture set on revision -- "you
    must upload and replace the whole set for one item" -- so this is a
    replacement, not an addition. It also ignores an image whose URL matches one
    already uploaded to that listing, so re-sending the same URL is a no-op.
    """
    output = io.StringIO()
    writer = csv.DictWriter(
        output, fieldnames=COVER_REVISE_HEADERS, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerow({
        "Action": "Revise",
        "ItemID": str(item_id).strip(),
        "PicURL": str(cover_image_url or "").strip(),
    })
    return output.getvalue()
# eBay requires a Condition Descriptor for trading cards. For ungraded cards
# the descriptor is "Card Condition", ID 40001, so the column is "CD:40001".
CONDITION_DESCRIPTOR_COLUMN = "CD:40001"

ADD_HEADERS = [
    "Action",
    "Category",
    "Title",
    "Relationship",
    "RelationshipDetails",
    "Description",
    "ConditionID",
    "StartPrice",
    "Quantity",
    "CustomLabel",
    "PicURL",
    "Format",
    "Duration",
    "Price",
    "PostalCode",
    CONDITION_DESCRIPTOR_COLUMN,
]

# eBay item specifics travel in columns prefixed "C:" (the seller's own
# templates mark required ones with a leading asterisk, e.g. "*C:Game"). The
# SortSwift eBay-flavoured export already carries these, so they are forwarded
# straight through rather than being reconstructed here.
ITEM_SPECIFIC_PATTERN = re.compile(r"^\*?C:(.+)$", re.IGNORECASE)

# "Game" is required for the card categories, so it is emitted even when the
# export omits it, using a configurable default.
GAME_ITEM_SPECIFIC = "C:Game"

# Item specifics we can build from the plain SortSwift columns already parsed
# for other purposes. This maps an eBay specific to the input columns it can be
# read from -- it does not invent or translate values, so a plain (non-eBay)
# export still produces a listing with the specifics eBay expects. An explicit
# "C:"-prefixed column in the upload always wins over these.
DERIVED_ITEM_SPECIFICS = (
    ("C:Game", ["Game"]),
    ("C:Set", ["Set", "Set Name", "Expansion", "Edition"]),
    ("C:Card Name", ["Name", "Product Name", "Card Name", "Card", "Product"]),
    ("C:Card Number", ["Card Number", "Number"]),
    ("C:Language", ["Language", "Lang", "Language (Full Name)"]),
    ("C:Rarity", ["Rarity"]),
    ("C:Finish", ["Printing", "Finish", "Variant", "Foil"]),
)


def _detect_item_specific_columns(fieldnames) -> Dict[str, str]:
    """
    Map each item-specific input column to its normalised output column.

    Returns e.g. {"*C:Game": "C:Game", "C:Set": "C:Set"}. The asterisk is a
    template annotation, not part of the field name, so it is stripped.
    """
    detected: Dict[str, str] = {}
    for name in fieldnames or []:
        if not name:
            continue
        match = ITEM_SPECIFIC_PATTERN.match(name.strip())
        if match:
            detected[name] = "C:" + match.group(1).strip()
    return detected


def _uniform_item_specifics(cards: List[Dict[str, Any]]) -> Dict[str, str]:
    """
    Keep only the specifics that every card in a variation group agrees on.

    A variation listing carries ONE set of listing-level item specifics, so a
    field that differs between cards (Card Name, Card Number) cannot be stated
    at listing level -- the variation axis expresses it instead. Fields common
    to the whole group (Game, Set, Language) can be.
    """
    if not cards:
        return {}

    shared: Dict[str, str] = {}
    all_keys = set()
    for c in cards:
        all_keys.update(c.get("item_specifics", {}))

    for key in all_keys:
        values = {str(c.get("item_specifics", {}).get(key, "")).strip() for c in cards}
        if len(values) == 1:
            value = values.pop()
            if value:
                shared[key] = value
    return shared


# Business policy columns, emitted only when configured. eBay matches these by
# name, case-sensitively, against the seller's Manage Business Policies page.
# When policies are used the individual Payment/Shipping/Returns fields must be
# absent -- which they are.
POLICY_SETTING_COLUMNS = (
    ("shipping_profile_name", "ShippingProfileName"),
    ("return_profile_name", "ReturnProfileName"),
    ("payment_profile_name", "PaymentProfileName"),
)

# eBay's variation syntax: within one attribute, values are separated by
# semicolons; a pipe separates different attributes. Card names containing
# either character would corrupt the RelationshipDetails field.
VARIATION_VALUE_SEPARATOR = ";"
VARIATION_ATTRIBUTE_SEPARATOR = "|"
VARIATION_ATTRIBUTE_NAME = "Card"

# Per-variation images are declared as "<option value>=<url>", so "=" is a
# delimiter in that position and must not survive inside an option name.
VARIATION_PICTURE_SEPARATOR = "="

# How a batch's quantities relate to what is already on hand.
#
# SortSwift can export either a full dump of everything you hold or a delta of
# just-scanned cards, and the two need opposite arithmetic. Getting it wrong is
# not a cosmetic error: treating a full dump as a delta adds your whole
# inventory on top of itself every time you upload, which oversells on eBay.
QUANTITY_MODE_SET = "set"   # the file is the truth; replace what we hold
QUANTITY_MODE_ADD = "add"   # the file is new stock; add to what we hold
QUANTITY_MODES = (QUANTITY_MODE_SET, QUANTITY_MODE_ADD)

DEFAULT_VARIATION_OPTION_TEMPLATE = "{name} ({card_number})"

# eBay's ConditionID for an ungraded card. Graded cards use IDs extending 2750
# and need a different descriptor, which this engine does not yet emit.
UNGRADED_CONDITION_ID = "4000"

# eBay accepts exactly four ungraded grades, and SortSwift does not supply them,
# so a translation is genuinely required here rather than being an invented
# vocabulary. The numeric value IDs differ by card family: game/CCG cards and
# sports cards share only "Near mint or better".
EBAY_UNGRADED_VALUE_IDS = {
    # Game / CCG / non-sport singles (e.g. category 183454)
    "game": {
        "Near mint or better": "400010",
        "Excellent": "400015",
        "Very good": "400016",
        "Poor": "400017",
    },
    # Sports card singles (category 261328)
    "sports": {
        "Near mint or better": "400010",
        "Excellent": "400011",
        "Very good": "400012",
        "Poor": "400013",
    },
}

# eBay categories that use the sports-card value IDs.
SPORTS_CARD_CATEGORIES = {"261328"}

# SortSwift / TCGplayer grades mapped onto eBay's four ungraded buckets. eBay
# has no separate bucket below "Poor", so Heavily Played and Damaged both land
# there.
CONDITION_TO_EBAY_GRADE = {
    "nm": "Near mint or better",
    "near mint": "Near mint or better",
    "near mint or better": "Near mint or better",
    "m": "Near mint or better",
    "mint": "Near mint or better",
    "lp": "Excellent",
    "lightly played": "Excellent",
    "excellent": "Excellent",
    "mp": "Very good",
    "moderately played": "Very good",
    "very good": "Very good",
    "vg": "Very good",
    "good": "Very good",
    "hp": "Poor",
    "heavily played": "Poor",
    "played": "Poor",
    "poor": "Poor",
    "dm": "Poor",
    "dmg": "Poor",
    "damaged": "Poor",
}


def _value_ids_for_category(category_id: str) -> Dict[str, str]:
    """Pick the ungraded value-ID table that matches the target category."""
    family = "sports" if str(category_id).strip() in SPORTS_CARD_CATEGORIES else "game"
    return EBAY_UNGRADED_VALUE_IDS[family]


def resolve_condition_descriptor(
    condition: str,
    category_id: str = "183454",
    style: str = "label_id",
) -> Optional[str]:
    """
    Render the CD:40001 cell for an ungraded card, or None if unmappable.

    ``style`` selects the cell format, because reports differ on which eBay
    accepts: 'label_id' produces "Excellent - (ID: 400015)" and 'id' produces
    the bare "400015". Switch it in Listing Rules if an upload is rejected.
    """
    grade = CONDITION_TO_EBAY_GRADE.get(str(condition or "").strip().lower())
    if not grade:
        return None

    value_id = _value_ids_for_category(category_id).get(grade)
    if not value_id:
        return None

    if str(style).strip().lower() == "id":
        return value_id
    return f"{grade} - (ID: {value_id})"


DEFAULT_VARIATION_TITLE_TEMPLATE = (
    "{set_name}: Pick Your Card - {condition} - Complete Your Set"
)


def _sanitize_variation_value(value: str) -> str:
    """
    Make a card name safe to embed in RelationshipDetails.

    A literal ';' or '|' inside a value would be read by eBay as a separator,
    splitting one card into several bogus options.
    """
    cleaned = str(value or "").strip()
    for sep in (
        VARIATION_VALUE_SEPARATOR,
        VARIATION_ATTRIBUTE_SEPARATOR,
        VARIATION_PICTURE_SEPARATOR,
    ):
        cleaned = cleaned.replace(sep, "/")
    return cleaned


def build_variation_option_name(
    product_name: str,
    card_number: str = "",
    template: str = DEFAULT_VARIATION_OPTION_TEMPLATE,
) -> str:
    """
    Build the dropdown label for one card, e.g. "Crushing Gloves (121/198)".

    The card number makes otherwise-identical reprints distinguishable and
    gives the list a natural order. When a card has no number the template
    collapses to just the name rather than leaving empty brackets.
    """
    name = str(product_name or "").strip()
    number = str(card_number or "").strip()
    if not number:
        return _sanitize_variation_value(name)
    rendered = template.replace("{name}", name).replace("{card_number}", number)
    return _sanitize_variation_value(rendered)


def variation_sort_key(card: Dict[str, Any]):
    """
    Order a variation group by card number so the eBay dropdown reads in
    collector order rather than upload order.

    Card numbers are not plain integers -- "121/198", "TG12/TG30", "SV107" --
    so the leading integer is used, with any non-numeric prefix as a secondary
    key. Cards with no usable number sort last, alphabetically, instead of
    being scattered through the list.
    """
    raw = str(card.get("card_number") or "").strip()
    match = re.search(r"(\d+)", raw)
    if not match:
        return (1, "", 0, str(card.get("product_name") or "").lower())
    prefix = raw[: match.start()].upper()
    return (0, prefix, int(match.group(1)), str(card.get("product_name") or "").lower())


def generate_variation_title(
    set_name: str,
    condition: str = "",
    template: str = DEFAULT_VARIATION_TITLE_TEMPLATE,
) -> str:
    """
    Build an eBay variation listing title within the 80-character limit.

    The condition is substituted verbatim from the source data and is never
    abbreviated or altered here: the title makes a factual claim about the
    cards, so shortening it risks misdescribing them. When a title is too long
    the set name is truncated instead.
    """
    cond = str(condition or "").strip()

    def render(tpl: str, s_name: str) -> str:
        return tpl.replace("{set_name}", s_name).replace("{condition}", cond)

    s_name = str(set_name or "").strip()

    title = render(template, s_name)
    if len(title) <= 80:
        return title

    # Legacy templates hardcode "Near Mint" instead of using {condition}.
    # Shortening that literal is safe because it is template text, not data.
    shortened = render(template.replace("Near Mint", "NM"), s_name)
    if len(shortened) <= 80:
        return shortened

    # Last resort: trim the set name by exactly the overflow amount.
    overflow = len(shortened) - 80
    trimmed_set = s_name[: max(0, len(s_name) - overflow)].strip()
    return render(template.replace("Near Mint", "NM"), trimmed_set)[:80]




def _parse_price(val: Optional[str]) -> float:
    """Safely parse price string to float."""
    if not val:
        return 0.0
    try:
        clean = str(val).replace("$", "").replace(",", "").strip()
        return float(clean) if clean and clean.upper() != "N/A" else 0.0
    except (ValueError, TypeError):
        return 0.0


def _empty_batch_result(
    logs: List[Dict[str, str]],
    add_headers: Optional[List[str]] = None,
    **overrides,
) -> Dict[str, Any]:
    """Build a no-op batch result carrying the supplied log messages."""
    result = {
        "revise_csv": ",".join(REVISE_HEADERS) + "\n",
        "add_csv": ",".join(add_headers or ADD_HEADERS) + "\n",
        "revise_count": 0,
        "zeroed_count": 0,
        "unchanged_count": 0,
        "add_count": 0,
        "new_catalog_count": 0,
        "skipped_count": 0,
        "duplicate": False,
        "dry_run": False,
        "logs": logs,
    }
    result.update(overrides)
    return result


def process_batch_csv(
    csv_text: str,
    db: Database,
    source_name: str = "batch.csv",
    force: bool = False,
    dry_run: bool = False,
    user_id: int = SHARED_SCOPE,
    quantity_mode: str = QUANTITY_MODE_SET,
) -> Dict[str, Any]:
    """
    Process fresh SortSwift inventory batch CSV.
    Routes rows into:
    1. ebay_inventory_updates.csv (REVISE - already live on eBay)
    2. ebay_new_additions.csv (ADD - new to eBay)

    Automatically creates:
    - Multi-item Variation Listings (grouped by Set Name, for cards below the
      configured single-listing threshold, when 'group_by_set' is enabled)
    - Standalone Single Listings (for cards at or above the threshold, and for
      every card when 'group_by_set' is disabled)

    Revise quantities are additive, so processing the same export twice would
    double the live stock. Uploads are therefore fingerprinted and a repeat is
    refused unless ``force`` is set.

    ``quantity_mode`` decides what the file's quantities mean:

    * ``"set"`` (default) treats the export as a **full inventory dump**. The
      quantity in the file becomes the quantity on eBay. Rows for the same card
      still sum, because a card held in two bins appears twice and the bin is
      not part of a card's identity -- but the total replaces whatever was
      there before, so re-uploading is idempotent. Cards that are live on eBay
      and absent from the file are revised down to zero, since a full dump
      omitting a card means it is gone.
    * ``"add"`` treats the export as a **delta of newly scanned cards** and
      adds to the running total, which is only correct if the file contains
      nothing you have already processed.

    ``user_id`` selects whose pricing rules and listing settings to apply.
    Both are per-user, so two sellers can process the same export and each get
    their own prices, title template and business policy names. The catalogue
    itself is shared, but no computed price is stored in it -- prices live only
    in the generated CSV -- so per-user rules cannot conflict there. Defaults to
    the shared baseline, which is what the command line uses.

    ``dry_run`` regenerates the output files **without writing anything**: no
    catalogue entries, no quantity accumulation, no store-mirror updates and no
    batch fingerprint. It exists for the common case of re-downloading a batch
    that has already been applied because a setting changed and the CSV needs
    rebuilding -- so it renders with current settings rather than replaying a
    stored file. Cards it cannot already find in the catalogue are skipped,
    since minting a manifest ID would itself be a write.
    """
    csv_text = strip_bom(csv_text)
    logs: List[Dict[str, str]] = []
    revise_rows: List[Dict[str, Any]] = []

    # Raw items to be added (categorized into singles vs variation sets)
    staged_singles: List[Dict[str, Any]] = []
    # (set_name, condition) -> [cards]. Condition is part of the key because a
    # variation listing carries a single ConditionID for all of its options.
    staged_variations: Dict[tuple, List[Dict[str, Any]]] = {}

    new_catalog_count = 0
    skipped_count = 0

    mode = str(quantity_mode or QUANTITY_MODE_SET).strip().lower()
    if mode not in QUANTITY_MODES:
        raise ValueError(
            f"quantity_mode must be one of {QUANTITY_MODES}, got {quantity_mode!r}"
        )
    replace_quantities = mode == QUANTITY_MODE_SET

    # The rules cannot change while a batch runs, so read them once instead of
    # once per card.
    pricing_rules = db.get_pricing_rules(user_id=user_id)

    # Total seen in *this file* per card, so several rows for one card (the
    # same card in two bins) sum together before replacing the stored value.
    file_totals: Dict[str, int] = {}

    # In replace mode there must be exactly one Revise row per card, keyed by
    # manifest id rather than appended per row: two rows for one card would
    # otherwise emit two Revise rows whose CustomLabels differ by bin, and only
    # one of those labels exists on the listing.
    revise_by_manifest: Dict[str, Dict[str, Any]] = {}
    # Add mode keeps one row per CSV row, paired with its manifest id so the
    # unchanged check never has to guess which card a row belongs to.
    revise_pairs: List[tuple] = []

    # What eBay is believed to hold for each card we touched, so an unchanged
    # Revise row can be recognised and dropped after the final total is known.
    believed_state: Dict[str, Dict[str, Any]] = {}
    # The quantity we intend to ask eBay for, written only for rows that
    # survive; recording one for a suppressed row would claim an outstanding
    # request that no file actually contains.
    intended_qty: Dict[str, int] = {}

    # Load configuration settings
    single_threshold = float(
        db.get_listing_setting("single_threshold", "5.00", user_id=user_id)
    )
    title_template = db.get_listing_setting(
        "variation_title_template",
        DEFAULT_VARIATION_TITLE_TEMPLATE,
        user_id=user_id,
    )
    category_id = db.get_listing_setting("category_id", "183454", user_id=user_id)
    descriptor_style = db.get_listing_setting(
        "condition_descriptor_style", "label_id", user_id=user_id
    )
    postal_code = str(
        db.get_listing_setting("seller_postal_code", "", user_id=user_id)
    ).strip()
    default_game = str(
        db.get_listing_setting("default_game", "", user_id=user_id)
    ).strip()
    option_template = db.get_listing_setting(
        "variation_option_template", DEFAULT_VARIATION_OPTION_TEMPLATE, user_id=user_id
    )
    cover_image_url = str(
        db.get_listing_setting("cover_image_url", "", user_id=user_id)
    ).strip()

    # Only emit policy columns that are actually configured; a blank policy
    # name is worse than an absent column.
    active_policies = {}
    for setting_key, column in POLICY_SETTING_COLUMNS:
        value = str(
            db.get_listing_setting(setting_key, "", user_id=user_id)
        ).strip()
        if value:
            active_policies[column] = value

    # Cells appended to every generated Add row.
    common_add_fields = {"PostalCode": postal_code}
    common_add_fields.update(active_policies)

    # Populated once the uploaded file's headers are known.
    item_specific_columns: Dict[str, str] = {}
    add_headers = ADD_HEADERS + list(active_policies) + [GAME_ITEM_SPECIFIC]
    group_by_set = str(
        db.get_listing_setting("group_by_set", "true", user_id=user_id)
    ).strip().lower() not in (
        "false",
        "0",
        "no",
    )

    lines = [line for line in csv_text.splitlines() if line.strip()]
    if not lines:
        return _empty_batch_result(
            [{"level": "WARN", "message": "Uploaded SortSwift batch file is empty."}],
            add_headers=add_headers,
        )

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return _empty_batch_result(
            [{
                "level": "ERROR",
                "message": "Unable to parse CSV headers in SortSwift batch file.",
            }],
            add_headers=add_headers,
        )

    # Item specifics are discovered from the uploaded file's own headers, so
    # whatever your export provides is forwarded without needing a mapping.
    item_specific_columns = _detect_item_specific_columns(reader.fieldnames)

    # Derived specifics only apply where the upload has no explicit C: column
    # for them and does have a plain column to read from.
    present = {str(f).strip().lower() for f in (reader.fieldnames or []) if f}
    derived_specifics = tuple(
        (out_col, candidates)
        for out_col, candidates in DERIVED_ITEM_SPECIFICS
        if out_col not in set(item_specific_columns.values())
        and any(c.strip().lower() in present for c in candidates)
    )

    item_specific_outputs = sorted(
        set(item_specific_columns.values())
        | {out for out, _ in derived_specifics}
        | {GAME_ITEM_SPECIFIC}
    )
    add_headers = ADD_HEADERS + list(active_policies) + item_specific_outputs

    if item_specific_outputs:
        forwarded = ", ".join(item_specific_outputs)
        logs.append({
            "level": "INFO",
            "message": f"eBay item specifics included: {forwarded}",
        })

    # Refuse a replay of an already-processed file unless explicitly forced.
    batch_hash = hashlib.sha256(csv_text.encode("utf-8", errors="replace")).hexdigest()
    previous = db.find_processed_batch(batch_hash)
    if previous and not force and not dry_run:
        return _empty_batch_result(
            [{
                "level": "WARN",
                "message": (
                    f"This exact batch file was already processed on "
                    f"{previous['processed_at']} (as '{previous['source_name']}', "
                    f"{previous['row_count']} rows). Re-processing would add its "
                    f"quantities to your live eBay stock a second time. "
                    f"Re-upload with 'force' enabled if that is really intended."
                ),
            }],
            add_headers=add_headers,
            duplicate=True,
        )

    grouping_note = (
        f"Single Threshold: ${single_threshold:.2f}"
        if group_by_set
        else "Set grouping disabled - every card listed as a single"
    )
    logs.append({
        "level": "INFO",
        "message": f"Processing SortSwift scan batch with {len(lines) - 1} cards ({grouping_note})..."
    })
    if not postal_code:
        logs.append({
            "level": "ERROR",
            "message": (
                "No seller postal code is configured, so eBay will reject this "
                "Add file with error 10009 (No <Item.Location> exists). Set your "
                "postal code under Listing Rules before uploading."
            ),
        })
    if not active_policies:
        logs.append({
            "level": "WARN",
            "message": (
                "No eBay business policy names are configured. eBay usually "
                "requires shipping and return details on an Add, so set your "
                "policy names under Listing Rules if the upload is rejected."
            ),
        })
    if dry_run:
        logs.append({
            "level": "INFO",
            "message": (
                "Download-only run: regenerating the CSV files with current "
                "settings. Nothing will be written to your inventory."
            ),
        })
    if previous and force and not dry_run:
        logs.append({
            "level": "WARN",
            "message": (
                f"Forced re-processing of a batch already handled on "
                f"{previous['processed_at']}; quantities will be added again."
            ),
        })

    for row_idx, row in enumerate(reader, start=1):
        product_name = _find_column(row, [
            "*C:Card Name", "Name", "Product Name", "Product Name (No Card Number)",
            "Card Name", "Card", "Product",
        ])
        set_name = _find_column(row, ["*C:Set", "Set", "Set Name", "Expansion", "Edition"])
        raw_condition = _find_column(row, [
            "Condition", "Condition Code", "Condition (Full Name)", "C:Card Condition",
            "Card Condition", "Grade",
        ])
        printing = _find_column(row, [
            "Printing", "*C:Finish", "Finish", "Variant", "Foil", "Foil (normal/foil)",
        ]) or "Normal"
        qty_str = _find_column(row, ["*Quantity", "Quantity", "Qty", "Count", "Amount"])

        # Extra SortSwift / eBay-export fields
        sku_id = _find_column(row, ["SKU ID", "SKU Id", "SKU", "SkuId", "skuId"])
        tcgplayer_id = _find_column(row, [
            "TCGPlayer ID", "TCGplayer Id", "Product ID", "ProductId", "productId", "ID Product",
        ])
        card_number = _find_column(row, ["*C:Card Number", "Card Number", "Number"])
        set_code = _find_column(row, ["Set Code", "SetCode"])
        language = _find_column(row, ["Language", "*C:Language", "Lang", "Language (Full Name)"]) or "EN"
        cdn_image = _find_column(row, ["PicURL", "CDN Image", "CDN Link", "cdn_link", "Image URL"])
        cdn_back_image = _find_column(row, ["CDN Back Link", "Card Back CDN Image", "Back Image"])
        stock_image = _find_column(row, ["Stock Image", "Stock Photo"])
        remarks = _find_column(row, [
            "Remark + Bin Index", "Remarks", "Remark", "Bin Index", "Bin", "binIndex", "Comment",
        ])
        # Raw ConditionID from CSV (e.g. 4000) — prefer over string mapping
        raw_condition_id = _find_column(row, ["*ConditionID", "ConditionID", "Condition ID"])

        # Clean remark / bin string for eBay SKU encoding
        clean_remark = ""
        if remarks and str(remarks).strip().lower() not in ("no remark", "none", ""):
            clean_remark = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(remarks).strip())
            clean_remark = clean_remark[:20]

        # Dynamic Pricing Calculation
        ebay_price_val = _parse_price(_find_column(row, ["Platform Price (Ebay)", "eBay Price", "Ebay Price", "ebay_price"]))
        standard_price_val = _parse_price(_find_column(row, ["Your Price", "Price", "Selling Price", "Platform Price (Internal)"]))
        market_price_val = _parse_price(_find_column(row, ["Market Price", "Platform Price (Tcgplayer)", "TCGPlayer Price", "Market"]))

        raw_base_price = market_price_val if market_price_val > 0 else standard_price_val

        if ebay_price_val > 0:
            effective_price = ebay_price_val
        else:
            effective_price, _ = apply_pricing_rules(
                pricing_rules, raw_base_price
            )

        if not product_name or not set_name:
            skipped_count += 1
            logs.append({
                "level": "WARN",
                "message": f"Row {row_idx}: Missing Product Name or Set Name. Skipped.",
            })
            continue

        # Condition is passed through from the source export verbatim. We do not
        # translate it: the value originates in SortSwift and is destined for
        # eBay or back into SortSwift, so interposing our own vocabulary only
        # creates a third one that can disagree with both.
        condition_name = str(raw_condition or "").strip()
        if not condition_name:
            skipped_count += 1
            logs.append({
                "level": "WARN",
                "message": f"Row {row_idx}: No Condition value. Skipped rather than guessing the card's condition.",
            })
            continue

        # An explicit descriptor in the export wins; we only derive one when the
        # source does not supply it.
        explicit_descriptor = _find_column(row, [
            "CD:40001", "CD:Card Condition - (ID: 40001)", "CD_Card_Condition",
            "Condition Descriptor", "Card Condition Descriptor",
        ])

        # eBay requires a numeric ConditionID for category 183454. It must come
        # from the input; we will not infer one.
        condition_id = str(raw_condition_id or "").strip()
        if not condition_id.isdigit():
            skipped_count += 1
            logs.append({
                "level": "WARN",
                "message": (
                    f"Row {row_idx}: No numeric ConditionID column for "
                    f"'{product_name}' (Condition: {condition_name}). Skipped rather "
                    f"than guessing. Re-export from SortSwift including the "
                    f"ConditionID column."
                ),
            })
            continue

        # Resolve the eBay Condition Descriptor, required for card categories.
        if explicit_descriptor and str(explicit_descriptor).strip():
            condition_descriptor = str(explicit_descriptor).strip()
        elif condition_id != UNGRADED_CONDITION_ID:
            skipped_count += 1
            logs.append({
                "level": "WARN",
                "message": (
                    f"Row {row_idx}: ConditionID {condition_id} is not the ungraded "
                    f"value ({UNGRADED_CONDITION_ID}). Graded cards need a different "
                    f"Condition Descriptor, which is not supported yet. Skipped."
                ),
            })
            continue
        else:
            condition_descriptor = resolve_condition_descriptor(
                condition_name, category_id=category_id, style=descriptor_style
            )
            if not condition_descriptor:
                skipped_count += 1
                accepted = ", ".join(sorted(set(CONDITION_TO_EBAY_GRADE.values())))
                logs.append({
                    "level": "WARN",
                    "message": (
                        f"Row {row_idx}: Condition '{condition_name}' does not map to "
                        f"an eBay ungraded grade ({accepted}). Skipped rather than "
                        f"guessing. Add a '{CONDITION_DESCRIPTOR_COLUMN}' column to "
                        f"your export to set it explicitly."
                    ),
                })
                continue

        try:
            quantity = int(qty_str.strip()) if qty_str else 1
            if quantity <= 0:
                quantity = 1
        except (ValueError, AttributeError):
            quantity = 1

        # Check or create manifest ID
        if dry_run:
            # Read-only: find the existing catalogue entry, never mint one.
            existing = db.find_manifest(
                product_name, set_name, condition_name, printing
            )
            if not existing:
                skipped_count += 1
                logs.append({
                    "level": "WARN",
                    "message": (
                        f"Row {row_idx}: '{product_name}' is not in the catalogue yet, "
                        f"so it has no manifest ID to put in the file. Process the "
                        f"batch for real to catalogue it."
                    ),
                })
                continue
            manifest_id, is_new, card_data = existing["manifest_id"], False, existing
        else:
            manifest_id, is_new, card_data = db.get_or_create_manifest(
                product_name=product_name,
                set_name=set_name,
                condition=condition_name,
                printing=printing,
                sku_id=sku_id,
                tcgplayer_id=tcgplayer_id,
                card_number=card_number,
                set_code=set_code,
                language=language,
                price=effective_price,
                market_price=market_price_val,
                cdn_image=cdn_image,
                remarks=remarks,
            )

        # Forward this row's item specifics verbatim, defaulting Game because
        # eBay requires it for the card categories.
        row_specifics: Dict[str, str] = {}
        for source_col, out_col in item_specific_columns.items():
            value = str(row.get(source_col) or "").strip()
            if value:
                row_specifics[out_col] = value
        for out_col, candidates in derived_specifics:
            if row_specifics.get(out_col):
                continue
            value = str(_find_column(row, candidates) or "").strip()
            if value:
                row_specifics[out_col] = value
        # Game is the one specific where the configured value OVERRIDES the
        # export rather than merely filling a gap. eBay only accepts values from
        # its own list for the category (e.g. "Pokemon TCG" with an accented e),
        # and SortSwift exports a looser label such as "Pokemon", which eBay
        # rejects as invalid. The export's value is used only when no setting is
        # configured.
        if default_game:
            row_specifics[GAME_ITEM_SPECIFIC] = default_game
        elif not row_specifics.get(GAME_ITEM_SPECIFIC):
            game = str(_find_column(row, ["Game", "*C:Game", "C:Game"]) or "").strip()
            row_specifics[GAME_ITEM_SPECIFIC] = game

        # Our own catalogued stock count for this card.
        file_totals[manifest_id] = file_totals.get(manifest_id, 0) + quantity
        if not dry_run:
            if replace_quantities:
                db.set_manifest_quantity(manifest_id, file_totals[manifest_id])
            else:
                db.increment_manifest_quantity(manifest_id, quantity)

        ebay_custom_label = f"{manifest_id}-{clean_remark}" if clean_remark else manifest_id

        if is_new:
            new_catalog_count += 1
            bin_note = f" | Bin: {remarks}" if remarks and str(remarks).strip().lower() != "no remark" else ""
            logs.append({
                "level": "INFO",
                "message": f"Row {row_idx}: Registered NEW card -> [{manifest_id}] {product_name} ({set_name} | {condition_name} | {printing} | SKU: {sku_id or 'N/A'}{bin_note})",
            })

        # Check if live on eBay
        variation = db.get_variation(manifest_id)

        if variation and variation.get("ebay_parent_id"):
            # REVISE Scenario: Item is already live on eBay
            ebay_item_id = str(variation["ebay_parent_id"]).strip()
            prev_qty = variation.get("last_known_qty", 0)

            # eBay knows this variation by whichever label it was listed under,
            # which Module B records. Inventing a fresh label from this row's
            # bin would address a variation that does not exist.
            known_label = (variation.get("custom_label") or "").strip()
            revise_label = known_label or ebay_custom_label

            if replace_quantities:
                new_consolidated_qty = file_totals[manifest_id]
                qty_note = f"= {new_consolidated_qty}"
            else:
                # Accumulate on whatever we most recently asked eBay for, or on
                # eBay's own figure if nothing is outstanding. Reading only
                # last_known_qty would make two scan batches uploaded before a
                # sync both start from the same base, losing the first one.
                outstanding = variation.get("pending_qty")
                base_qty = prev_qty if outstanding is None else int(outstanding)
                new_consolidated_qty = base_qty + file_totals[manifest_id]
                qty_note = f"+{quantity} => Total {new_consolidated_qty}"

            # Deferred until the row is known to survive; see below.
            intended_qty[manifest_id] = new_consolidated_qty
            believed_state[manifest_id] = {
                "last_known_qty": variation.get("last_known_qty"),
                "last_known_price": variation.get("last_known_price"),
                "pending_qty": variation.get("pending_qty"),
            }

            revise_entry = {
                "Action": "Revise",
                "ItemID": ebay_item_id,
                "CustomLabel": revise_label,
                "Quantity": new_consolidated_qty,
                "Price": f"{effective_price:.2f}",
            }
            if replace_quantities:
                # Later rows for the same card update the running total in
                # place, keeping one row per card.
                revise_by_manifest[manifest_id] = revise_entry
            else:
                revise_pairs.append((manifest_id, revise_entry))

            logs.append({
                "level": "SUCCESS",
                "message": f"Row {row_idx}: [REVISE] #{ebay_item_id} [{revise_label}] {product_name} ({qty_note})",
            })
        else:
            # ADD Scenario: Stage for Single vs Variation listing
            card_entry = {
                "manifest_id": manifest_id,
                "custom_label": ebay_custom_label,
                "product_name": product_name,
                "set_name": set_name,
                "condition_name": condition_name,
                "condition_id": condition_id,
                "condition_descriptor": condition_descriptor,
                "item_specifics": row_specifics,
                "printing": printing,
                "quantity": quantity,
                "price": effective_price,
                "cdn_image": cdn_image or "",
                "cdn_back_image": cdn_back_image or "",
                "stock_image": stock_image or "",
                "card_number": card_number or "",
                "option_name": build_variation_option_name(
                    product_name, card_number or "", option_template
                ),
            }

            if not group_by_set:
                staged_singles.append(card_entry)
                logs.append({
                    "level": "SUCCESS",
                    "message": f"Row {row_idx}: [ADD SINGLE] [{ebay_custom_label}] {product_name} (set grouping disabled)",
                })
            elif effective_price >= single_threshold:
                staged_singles.append(card_entry)
                logs.append({
                    "level": "SUCCESS",
                    "message": f"Row {row_idx}: [ADD SINGLE] [{ebay_custom_label}] {product_name} (Price: ${effective_price:.2f} >= ${single_threshold:.2f} threshold)",
                })
            else:
                group_key = (set_name, condition_name)
                staged_variations.setdefault(group_key, []).append(card_entry)
                logs.append({
                    "level": "SUCCESS",
                    "message": f"Row {row_idx}: [ADD VARIATION] [{ebay_custom_label}] {product_name} grouped into '{set_name}' / {condition_name} (Price: ${effective_price:.2f})",
                })

    # -----------------------------------------------------------------
    # BUILD FINAL EBAY ADD CSV (PARENT CONTAINERS + CHILD VARIATIONS + SINGLES)
    # -----------------------------------------------------------------
    final_add_rows: List[Dict[str, Any]] = []

    # 1. Multi-Item Variation Listings (one per set + condition)
    for (set_title, group_condition), cards in staged_variations.items():
        if not cards:
            continue

        # Order the dropdown by card number. The parent's option list and the
        # child rows must agree, so sort once and use it for both.
        cards = sorted(cards, key=variation_sort_key)

        # Generate Parent Container Row
        parent_title = generate_variation_title(
            set_title, condition=group_condition, template=title_template
        )
        # An explicit cover image wins; otherwise fall back to the first card's.
        cover_image = cover_image_url or next(
            (c["cdn_image"] for c in cards if c["cdn_image"]), ""
        )

        # Declare the option list on the parent. eBay separates values within
        # one attribute by semicolons; a pipe would be read as the start of a
        # second attribute and rejected.
        option_names = [c["option_name"] for c in cards]
        parent_rel_details = (
            f"{VARIATION_ATTRIBUTE_NAME}="
            + VARIATION_VALUE_SEPARATOR.join(option_names)
        )

        # Append Parent Container Row. The parent leaves Relationship EMPTY;
        # only the child rows are marked "Variation". Marking the parent too
        # leaves eBay unable to tell which row is the container.
        final_add_rows.append({
            "Action": "Add",
            "Category": category_id,
            "Title": parent_title,
            "Relationship": "",
            "RelationshipDetails": parent_rel_details,
            "Description": (
                f"Pick Your Card from {set_title}! "
                f"Condition: {group_condition}. Complete your collection."
            ),
            "ConditionID": cards[0]["condition_id"],
            "StartPrice": "",
            "Quantity": "",
            "CustomLabel": "",
            "PicURL": cover_image,
            "Format": "FixedPrice",
            "Duration": "GTC",
            "Price": "",
            CONDITION_DESCRIPTOR_COLUMN: cards[0]["condition_descriptor"],
            **common_add_fields,
            **_uniform_item_specifics(cards),
        })

        # Append Child Variation Rows
        for c in cards:
            final_add_rows.append({
                "Action": "Add",
                "Category": category_id,
                "Title": "",
                "Relationship": "Variation",
                "RelationshipDetails": (
                    f"{VARIATION_ATTRIBUTE_NAME}={c['option_name']}"
                ),
                "Description": "",
                "ConditionID": c["condition_id"],
                "StartPrice": f"{c['price']:.2f}",
                "Quantity": c["quantity"],
                "CustomLabel": c["custom_label"],
                # A per-variation image must name the option it belongs to:
                # "<option value>=<url>". A bare URL here is ignored by eBay,
                # which is why only the parent's picture used to appear.
                "PicURL": (
                    f"{c['option_name']}{VARIATION_PICTURE_SEPARATOR}{c['cdn_image']}"
                    if c["cdn_image"]
                    else ""
                ),
                "Format": "FixedPrice",
                "Duration": "GTC",
                "Price": f"{c['price']:.2f}",
                CONDITION_DESCRIPTOR_COLUMN: c["condition_descriptor"],
                **common_add_fields,
            })

    # 2. Standalone Single Listings (Cards >= threshold)
    for s in staged_singles:
        single_title = f"{s['product_name']} - {s['set_name']} - {s['condition_name']}"
        if len(single_title) > 80:
            single_title = f"{s['product_name']} - {s['set_name']}"
        if len(single_title) > 80:
            single_title = single_title[:80]

        # Single listings: include front, back, and stock images (pipe-delimited)
        single_pic_parts = [url for url in [
            cover_image_url or s["cdn_image"],
            s.get("cdn_back_image", ""),
            s.get("stock_image", ""),
        ] if url]
        single_pic_url = "|".join(single_pic_parts)

        final_add_rows.append({
            "Action": "Add",
            "Category": category_id,
            "Title": single_title,
            "Relationship": "",
            "RelationshipDetails": "",
            "Description": f"{s['product_name']} from {s['set_name']}. Condition: {s['condition_name']}, Printing: {s['printing']}.",
            "ConditionID": s["condition_id"],
            "StartPrice": f"{s['price']:.2f}",
            "Quantity": s["quantity"],
            "CustomLabel": s["custom_label"],
            "PicURL": single_pic_url,
            "Format": "FixedPrice",
            "Duration": "GTC",
            "Price": f"{s['price']:.2f}",
            CONDITION_DESCRIPTOR_COLUMN: s["condition_descriptor"],
            **common_add_fields,
            **s.get("item_specifics", {}),
        })

    # -----------------------------------------------------------------
    # REPLACE MODE: RECONCILE AGAINST THE FULL DUMP
    # -----------------------------------------------------------------
    def revise_row_changes_anything(manifest_id: str, row: Dict[str, Any]) -> bool:
        """
        Would this Revise row actually change the listing?

        Only ever answers False when eBay is *known* to hold exactly these
        values already. Anything unknown -- no sync yet, or a report with no
        price column -- counts as a change, so a real update is never dropped
        on a guess. That makes the check safe but conservative: it stays quiet
        until a Module B sync has taught us both figures.
        """
        state = believed_state.get(manifest_id)
        if state is None:
            return True

        known_qty = state["last_known_qty"]
        known_price = state["last_known_price"]
        pending = state["pending_qty"]

        if known_qty is None or known_price is None:
            return True

        # An outstanding request eBay has not confirmed must keep appearing in
        # the file, or the change would never reach eBay at all. A pending
        # value equal to eBay's own figure is not outstanding -- it is what a
        # previous no-op run recorded.
        if pending is not None and int(pending) != int(known_qty):
            return True

        return not (
            int(known_qty) == int(row["Quantity"])
            and round(float(known_price), 2) == round(float(row["Price"]), 2)
        )

    from_file_rows = (
        list(revise_by_manifest.items()) if replace_quantities else revise_pairs
    )

    unchanged_count = 0
    surviving: List[Dict[str, Any]] = []
    for manifest_key, row in from_file_rows:
        if not revise_row_changes_anything(manifest_key, row):
            unchanged_count += 1
            continue
        surviving.append(row)
        if not dry_run:
            # Record the request only now that it is really in the file. Only a
            # Module B sync may move last_known_qty.
            db.set_pending_quantity(manifest_key, intended_qty[manifest_key])

    revise_rows = surviving

    if unchanged_count:
        logs.append({
            "level": "INFO",
            "message": (
                f"{unchanged_count} card(s) already match eBay on both quantity "
                f"and price, so no Revise row was written for them."
            ),
        })

    zeroed_count = 0
    if replace_quantities:

        # A full dump lists everything on hand, so a card that is live on eBay
        # and absent from the file has sold out. Left alone it would keep its
        # old eBay quantity and carry on selling stock that is gone.
        for live in db.get_live_variations():
            live_id = live["manifest_id"]
            if live_id in file_totals:
                continue
            outstanding = live.get("pending_qty")
            believed_qty = (
                int(live.get("last_known_qty") or 0)
                if outstanding is None
                else int(outstanding)
            )
            if believed_qty == 0:
                # Already zero on eBay, or already asked to be. Re-emitting the
                # row on every dump would be noise.
                continue

            item_id = str(live["ebay_parent_id"]).strip()
            label = (live.get("custom_label") or "").strip() or live_id

            revise_rows.append({
                "Action": "Revise",
                "ItemID": item_id,
                "CustomLabel": label,
                "Quantity": 0,
                # Price is required by the Revise header. Leaving it blank
                # tells eBay not to change the listed price, which is what we
                # want: this row is only about stock.
                "Price": "",
            })
            zeroed_count += 1

            if not dry_run:
                # Same rule: we are asking eBay for zero, not observing it.
                db.set_pending_quantity(live_id, 0)
                db.set_manifest_quantity(live_id, 0)

            logs.append({
                "level": "WARN",
                "message": (
                    f"[SOLD OUT] #{item_id} [{label}] "
                    f"{live.get('product_name') or live_id} "
                    f"({live.get('set_name') or '?'} | {live.get('condition') or '?'}) "
                    f"is not in this dump, so the file asks eBay to set it to "
                    f"0 (eBay currently reports {live.get('last_known_qty')})."
                ),
            })

        if zeroed_count:
            logs.append({
                "level": "INFO",
                "message": (
                    f"{zeroed_count} card(s) live on eBay were absent from this "
                    f"dump and are revised down to 0."
                ),
            })

    # Generate REVISE CSV
    revise_io = io.StringIO()
    rev_writer = csv.DictWriter(revise_io, fieldnames=REVISE_HEADERS, lineterminator="\n")
    rev_writer.writeheader()
    rev_writer.writerows(revise_rows)
    revise_csv = revise_io.getvalue()

    # Generate ADD CSV
    add_io = io.StringIO()
    add_writer = csv.DictWriter(add_io, fieldnames=add_headers, lineterminator="\n")
    add_writer.writeheader()
    add_writer.writerows(final_add_rows)
    add_csv = add_io.getvalue()

    total_added_cards = len(staged_singles) + sum(len(c) for c in staged_variations.values())

    logs.append({
        "level": "INFO",
        "message": f"Batch routing finished: {len(revise_rows)} items to REVISE ({zeroed_count} of them zeroed as sold out, {unchanged_count} unchanged and skipped), {len(staged_variations)} Set/Condition Variation Listings ({sum(len(c) for c in staged_variations.values())} child cards), {len(staged_singles)} Single Listings ({new_catalog_count} new catalog entries created).",
    })

    # Fingerprint only after the batch has actually been applied, so a failure
    # part-way through does not mark the file as done.
    if not dry_run:
        db.record_processed_batch(
            sha256=batch_hash,
            source_name=source_name,
            row_count=len(lines) - 1,
        )

    return {
        "revise_csv": revise_csv,
        "add_csv": add_csv,
        "revise_count": len(revise_rows),
        "zeroed_count": zeroed_count,
        "unchanged_count": unchanged_count,
        "quantity_mode": mode,
        "add_count": total_added_cards,
        "new_catalog_count": new_catalog_count,
        "skipped_count": skipped_count,
        "duplicate": False,
        "dry_run": dry_run,
        "logs": logs,
    }


def process_batch_file(
    input_path: str,
    db: Database,
    revise_output_path: Optional[str] = None,
    add_output_path: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
    user_id: int = SHARED_SCOPE,
    quantity_mode: str = QUANTITY_MODE_SET,
) -> Dict[str, Any]:
    """Process a SortSwift batch CSV file from disk."""
    content = read_csv_text(input_path)
    # One connection for the whole run; see Database.session.
    with db.session():
        result = process_batch_csv(
            content,
            db,
            source_name=os.path.basename(input_path),
            force=force,
            dry_run=dry_run,
            user_id=user_id,
            quantity_mode=quantity_mode,
        )
    if revise_output_path and result["revise_count"] > 0:
        with open(revise_output_path, "w", encoding="utf-8", newline="") as f:
            f.write(result["revise_csv"])
    if add_output_path and result["add_count"] > 0:
        with open(add_output_path, "w", encoding="utf-8", newline="") as f:
            f.write(result["add_csv"])
    return result
