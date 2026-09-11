import csv
import hashlib
import io
import os
import re
from typing import Dict, Any, List, Optional
from .csvtools import find_column as _find_column, read_csv_text, strip_bom
from .db import (
    Database,
    SHARED_SCOPE,
    apply_pricing_rules,
    apply_condition_multiplier,
    normalize_condition_key,
)


# The export column eBay's card categories require for an ungraded card's
# condition descriptor. Named here because a missing one is something the
# operator has to fix in SortSwift, and the warning has to say what to add.
CONDITION_DESCRIPTOR_COLUMN = "CD:40001"

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

# SortSwift offers two exports and only one of them is usable here. The eBay
# template (export_eBay_*.csv) carries the File Exchange columns -- *Action,
# *Category, *Title, *ConditionID, CD:Card Condition, the *C: item specifics,
# PostalCode and the policy names. The inventory export (export_SortSwift_*.csv)
# carries none of them, and uploading it produces one skip per row: 426
# identical warnings with the real cause never stated.
#
# Detected by columns rather than by filename, because a file can be renamed
# and because the columns are what actually matter. ConditionID is the marker
# used: it is the one column whose absence stops every single row, and it is
# absent from the inventory export and present in the eBay one. Matched
# tolerantly for the same reasons find_column is -- the eBay template writes
# "*ConditionID" with the asterisk that marks a required field.
CONDITION_ID_COLUMNS = ("*ConditionID", "ConditionID", "Condition ID")


def _has_condition_id_column(fieldnames) -> bool:
    wanted = {c.replace("*", "").replace(" ", "").lower() for c in CONDITION_ID_COLUMNS}
    for name in fieldnames or []:
        if not name:
            continue
        if str(name).strip().replace("*", "").replace(" ", "").lower() in wanted:
            return True
    return False

# A batch can be several hundred rows, and one broken column produces one
# warning per row -- 426 identical lines that bury the summary telling you what
# actually happened. Warnings of the same kind are logged once with a tally
# instead.
WARN_SAMPLE_LIMIT = 3


def _warn_once(logs: List[Dict[str, str]], tallies: Dict[str, int], kind: str,
               message: str) -> None:
    """
    Log a warning, but only the first few of each kind.

    The tally is reported by :func:`_flush_warn_tallies` once the pass is done,
    so nothing is hidden -- it is summarised rather than repeated.
    """
    tallies[kind] = tallies.get(kind, 0) + 1
    if tallies[kind] <= WARN_SAMPLE_LIMIT:
        logs.append({"level": "WARN", "message": message})


def _flush_warn_tallies(logs: List[Dict[str, str]], tallies: Dict[str, int]) -> None:
    for kind, count in sorted(tallies.items()):
        if count > WARN_SAMPLE_LIMIT:
            logs.append({
                "level": "WARN",
                "message": (
                    f"{count} row(s) hit the '{kind}' problem above; only the "
                    f"first {WARN_SAMPLE_LIMIT} are listed individually."
                ),
            })
    # Cleared so a second call is a no-op: the skip path flushes early
    # to put the tally above its own warning, and the normal path
    # flushes again before the summary.
    tallies.clear()

def _log_once(logs: List[Dict[str, str]], tallies: Dict[str, int], kind: str,
              message: str, level: str = "SUCCESS") -> None:
    """
    Log a per-row SUCCESS line, sampled like the warnings are.

    A 426-row export produced 426 of these. Each one is a DOM append in the
    browser and bytes in the response, for information that is a per-kind
    tally in practice -- and the volume buried the summary that says what the
    run actually did.
    """
    tallies[kind] = tallies.get(kind, 0) + 1
    if tallies[kind] <= WARN_SAMPLE_LIMIT:
        logs.append({"level": level, "message": message})


def _flush_log_tallies(logs: List[Dict[str, str]], tallies: Dict[str, int]) -> None:
    for kind, count in sorted(tallies.items()):
        if count > WARN_SAMPLE_LIMIT:
            logs.append({
                "level": "INFO",
                "message": (
                    f"{count} row(s) routed as {kind}; the first "
                    f"{WARN_SAMPLE_LIMIT} are listed above."
                ),
            })
    # Cleared so a second call is a no-op: the skip path flushes early
    # to put the tally above its own warning, and the normal path
    # flushes again before the summary.
    tallies.clear()

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
    **overrides,
) -> Dict[str, Any]:
    """Build a no-op batch result carrying the supplied log messages."""
    result = {
        "zeroed_count": 0,
        "parsed_rows": 0,
        "reconciled": False,
        "staged_card_count": 0,
        "staged_listing_count": 0,
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

    # Raw items to be added (categorized into singles vs variation sets)
    staged_singles: List[Dict[str, Any]] = []
    # (set_name, condition) -> [cards]. Condition is part of the key because a
    # variation listing carries a single ConditionID for all of its options.
    staged_variations: Dict[tuple, List[Dict[str, Any]]] = {}

    new_catalog_count = 0
    skipped_count = 0
    # Repeated per-row warnings are sampled and tallied rather than
    # logged once each; a few hundred identical lines bury the summary.
    warn_tallies: Dict[str, int] = {}
    log_tallies: Dict[str, int] = {}

    mode = str(quantity_mode or QUANTITY_MODE_SET).strip().lower()
    if mode not in QUANTITY_MODES:
        raise ValueError(
            f"quantity_mode must be one of {QUANTITY_MODES}, got {quantity_mode!r}"
        )
    replace_quantities = mode == QUANTITY_MODE_SET

    # The rules cannot change while a batch runs, so read them once instead of
    # once per card.
    pricing_rules = db.get_pricing_rules(user_id=user_id)
    condition_multipliers = {
        m["condition_key"]: m["multiplier"]
        for m in db.get_condition_multipliers(user_id=user_id)
    }
    # Grades seen that have no multiplier, reported once each rather than once
    # per row: a whole set of played cards would otherwise bury the log.
    unpriced_grades = set()

    # Total seen in *this file* per card, so several rows for one card (the
    # same card in two bins) sum together before replacing the stored value.
    file_totals: Dict[str, int] = {}

    # Cards this file touched that are already live on eBay. They need no
    # work here: the catalogue has been corrected and the planner diffs it
    # against what eBay is known to hold.
    live_count = 0

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
    default_game = str(
        db.get_listing_setting("default_game", "", user_id=user_id)
    ).strip()
    option_template = db.get_listing_setting(
        "variation_option_template", DEFAULT_VARIATION_OPTION_TEMPLATE, user_id=user_id
    )
    # Populated once the uploaded file's headers are known.
    item_specific_columns: Dict[str, str] = {}
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
        )

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return _empty_batch_result(
            [{
                "level": "ERROR",
                "message": "Unable to parse CSV headers in SortSwift batch file.",
            }],
        )

    # Fail fast, and with the actual diagnosis, when the wrong SortSwift export
    # has been uploaded. Without this the run reaches the per-row checks and
    # produces one skip per row -- hundreds of identical warnings about a
    # missing ConditionID, with the real cause (wrong export template) never
    # stated anywhere.
    if not _has_condition_id_column(reader.fieldnames):
        return _empty_batch_result(
            [{
                "level": "ERROR",
                "message": (
                    "This export has no ConditionID column at all, which means "
                    "it is SortSwift's inventory export rather than its eBay "
                    "export. Every row would be skipped for the same reason, "
                    "and the file is also missing Category, Title, C:Game, "
                    "PostalCode and the business policy names -- so accepting "
                    "it would produce a listing file that is wrong in several "
                    "ways at once. In SortSwift, export using the eBay "
                    "template: the file is named export_eBay_<date>.csv rather "
                    "than export_SortSwift_<date>.csv. Nothing was catalogued "
                    "and no eBay file was built, so nothing has changed."
                ),
            }],
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
    if dry_run:
        logs.append({
            "level": "INFO",
            "message": (
                "Preview run: reporting what this file would change "
                "with current settings. Nothing will be written."
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

        # Priced here rather than above, because the grade is part of the
        # calculation and is only known now. An explicit eBay Price column
        # still wins outright: it is a per-card override and must not be
        # discounted a second time.
        if ebay_price_val > 0:
            effective_price = ebay_price_val
        else:
            # The market price available to us is product-level -- neither
            # TCGplayer's public data nor the SortSwift export breaks it down
            # by condition -- so the grade discount is applied here, before
            # the tiers. Discounting first is deliberate: a played card should
            # fall into a cheaper tier, not the tier its mint price implies.
            adjusted_base, condition_factor = apply_condition_multiplier(
                raw_base_price, condition_name, condition_multipliers
            )
            if condition_factor is None and condition_name:
                unpriced_grades.add(condition_name)
            effective_price, _ = apply_pricing_rules(
                pricing_rules, adjusted_base
            )

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
        #
        # Deliberately not defaulted to UNGRADED_CONDITION_ID even though it is
        # 4000 for every ungraded card. The only file that omits this column is
        # SortSwift's *inventory* export, which is also missing Category,
        # Title, C:Game, PostalCode and the policy names -- so accepting it
        # would trade one loud failure for an Add file that is quietly wrong in
        # five other ways. The template check above catches that case and says
        # so; this stays strict.
        condition_id = str(raw_condition_id or "").strip()
        if not condition_id.isdigit():
            skipped_count += 1
            _warn_once(
                logs,
                warn_tallies,
                "no ConditionID",
                (
                    f"Row {row_idx}: No numeric ConditionID for "
                    f"'{product_name}' (Condition: {condition_name}). Skipped "
                    f"rather than guessing."
                ),
            )
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
            # Sampled: a first-time import registers every row, and 426
            # near-identical lines are noise rather than information. The
            # count is reported as "new catalog entries created" in the
            # summary either way.
            _log_once(
                logs, log_tallies, "new catalogue entry",
                f"Row {row_idx}: Registered NEW card -> [{manifest_id}] "
                f"{product_name} ({set_name} | {condition_name} | {printing} "
                f"| SKU: {sku_id or 'N/A'}{bin_note})",
                level="INFO",
            )

        # Check if live on eBay
        variation = db.get_variation(manifest_id)

        if variation and variation.get("ebay_parent_id"):
            # Already live on eBay, so this row is an update rather than a new
            # listing. What that update *is* no longer gets worked out here:
            # the catalogue has just been corrected above, and the planner
            # diffs it against what eBay is known to hold. This branch only
            # says so, which is the part a person reading the log wants.
            ebay_item_id = str(variation["ebay_parent_id"]).strip()

            # eBay knows this variation by whichever label it was listed under,
            # which Module B records. Inventing a fresh label from this row's
            # bin would address a variation that does not exist.
            known_label = (variation.get("custom_label") or "").strip()
            revise_label = known_label or ebay_custom_label

            # Accumulation in add mode rests on increment_manifest_quantity
            # above, which adds to the catalogue's own figure. That is the
            # right base and always was: it is correct whether or not eBay has
            # been told anything yet. The previous base -- eBay's last
            # reported quantity, or a `pending_qty` recording what a generated
            # file had asked for -- needed that second column precisely
            # because two scan batches uploaded before a sync would otherwise
            # both start from the same stale number and lose the first.
            live_count += 1
            _log_once(logs, log_tallies, "LIVE",
                      f"Row {row_idx}: [LIVE] #{ebay_item_id} [{revise_label}] "
                      f"{product_name} (catalogue now "
                      f"{db.get_manifest_by_id(manifest_id)['quantity']}, "
                      f"eBay reports {variation.get('last_known_qty')})")
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

            # Keep the export-derived fields, so an approved draft plan can
            # rebuild this card's Add row later. Without them a plan could
            # record a decision but never produce a listing file, because
            # these values exist only in the uploaded CSV and eBay requires
            # them. Skipped on a dry run, which must write nothing.
            if not dry_run:
                db.set_manifest_ebay_fields(manifest_id, {
                    "item_specifics": row_specifics,
                    "condition_id": condition_id,
                    "condition_descriptor": condition_descriptor,
                    # Whether that descriptor came from the upload or was
                    # rendered here. A later export must re-render our own,
                    # so that changing condition_descriptor_style in Listing
                    # Rules actually reaches the file -- otherwise the style
                    # in force the day a card was catalogued is frozen into
                    # it. The upload's own value is never rewritten.
                    "condition_descriptor_from_export": bool(
                        explicit_descriptor and str(explicit_descriptor).strip()
                    ),
                    "cdn_back_image": cdn_back_image or "",
                    "stock_image": stock_image or "",
                })

            if not group_by_set:
                staged_singles.append(card_entry)
                _log_once(logs, log_tallies, "ADD SINGLE",
                          f"Row {row_idx}: [ADD SINGLE] [{ebay_custom_label}] "
                          f"{product_name} (set grouping disabled)")
            elif effective_price >= single_threshold:
                staged_singles.append(card_entry)
                _log_once(logs, log_tallies, "ADD SINGLE",
                          f"Row {row_idx}: [ADD SINGLE] [{ebay_custom_label}] "
                          f"{product_name} (Price: ${effective_price:.2f} >= "
                          f"${single_threshold:.2f} threshold)")
            else:
                group_key = (set_name, condition_name)
                staged_variations.setdefault(group_key, []).append(card_entry)
                _log_once(logs, log_tallies, "ADD VARIATION",
                          f"Row {row_idx}: [ADD VARIATION] [{ebay_custom_label}] "
                          f"{product_name} grouped into '{set_name}' / "
                          f"{condition_name} (Price: ${effective_price:.2f})")

    # -----------------------------------------------------------------
    # REPLACE MODE: RECONCILE AGAINST THE FULL DUMP
    # -----------------------------------------------------------------
    # What used to happen here was the other half of this module: comparing
    # every card against what eBay was believed to hold and writing a Revise
    # row for the ones that differed. That is now the planner's job --
    # ``build_plan`` computes the same diff from stored state, the drafts page
    # shows it, and the API push applies it. Two independent answers to "what
    # does eBay need" is one too many, and this one could only ever be acted
    # on by a human uploading a file.
    #
    # What remains is the part that belongs to ingest rather than to eBay:
    # noticing that a card live on eBay is absent from a full dump, and
    # therefore sold out.
    zeroed_count = 0

    # Zeroing is only safe if we actually understood the file. Every skip
    # happens before a row reaches file_totals, so a skipped row looks
    # identical to a card the dump omitted -- and the remedy for an omitted
    # card is to stop selling it. A file whose rows we could not parse is
    # therefore the most dangerous input there is: with every row skipped,
    # file_totals is empty and every live listing would be revised to zero,
    # delisting the whole store from a file that in fact listed all of it.
    #
    # So reconcile only against a file that parsed cleanly. One unreadable row
    # costs this run's sold-out detection, which is a trivially recoverable
    # loss next to wiping live inventory.
    reconcile = replace_quantities and skipped_count == 0

    if replace_quantities and skipped_count:
        _flush_warn_tallies(logs, warn_tallies)
        logs.append({
            "level": "WARN",
            "message": (
                f"{skipped_count} row(s) could not be processed, so this file is "
                f"not a reliable picture of your stock. Cards missing from it "
                f"have NOT been revised down to 0 -- a skipped row is "
                f"indistinguishable from a card you no longer hold. Fix the "
                f"skipped rows above and re-run to reconcile sold-out cards."
            ),
        })

    if reconcile:

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
            zeroed_count += 1

            if not dry_run:
                # The catalogue is ours to correct from the dump. eBay's own
                # figure is not touched: only a Module B sync may move
                # last_known_qty, and the draft this stock change produces is
                # what will ask eBay for zero.
                db.set_manifest_quantity(live_id, 0)

            logs.append({
                "level": "WARN",
                "message": (
                    f"[SOLD OUT] #{item_id} [{label}] "
                    f"{live.get('product_name') or live_id} "
                    f"({live.get('set_name') or '?'} | {live.get('condition') or '?'}) "
                    f"is not in this dump, so the catalogue is set to 0 "
                    f"(eBay currently reports {live.get('last_known_qty')})."
                ),
            })

    if zeroed_count:
        logs.append({
            "level": "WARN",
            "message": (
                f"{zeroed_count} card(s) live on eBay were absent from this "
                f"dump and are now 0 in the catalogue. The draft will ask eBay "
                f"to stop selling them, so check that list before approving "
                f"it: those listings stop selling."
            ),
        })

    total_added_cards = len(staged_singles) + sum(len(c) for c in staged_variations.values())

    # Tallies before the summary, so the counts read as detail leading into it
    # rather than trailing after the conclusion.
    _flush_log_tallies(logs, log_tallies)
    _flush_warn_tallies(logs, warn_tallies)

    logs.append({
        "level": "INFO",
        "message": (
            f"Ingest finished: {len(file_totals)} card(s) read, "
            f"{new_catalog_count} new to the catalogue, "
            f"{zeroed_count} zeroed as sold out. They route to "
            f"{len(staged_variations)} Set/Condition variation listing(s) "
            f"({sum(len(c) for c in staged_variations.values())} child cards) "
            f"and {len(staged_singles)} single listing(s). Rebuild the draft "
            f"to see what eBay needs."
        ),
    })

    if unpriced_grades:
        logs.append({
            "level": "WARN",
            "message": (
                "No condition multiplier is configured for: "
                + ", ".join(sorted(unpriced_grades))
                + ". Those cards were priced from the market price with no "
                "grade discount, which over-prices a played card. Add them "
                "under Pricing Rules."
            ),
        })

    parsed_rows = len(file_totals)
    if skipped_count and not parsed_rows:
        logs.append({
            "level": "ERROR",
            "message": (
                f"Not one row of this file could be processed ({skipped_count} "
                f"skipped), so nothing was catalogued and the draft will be "
                f"empty. See the warnings above for the reason."
            ),
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
        "zeroed_count": zeroed_count,
        "parsed_rows": parsed_rows,
        "reconciled": reconcile,
        "quantity_mode": mode,
        # How the cards route, which is what the draft will group them into.
        # Informational: the planner derives the grouping itself from stored
        # state, so these are a preview of it and never its input.
        "staged_card_count": total_added_cards,
        "staged_listing_count": len(staged_variations) + len(staged_singles),
        "new_catalog_count": new_catalog_count,
        "skipped_count": skipped_count,
        "duplicate": False,
        "dry_run": dry_run,
        "logs": logs,
    }


def process_batch_file(
    input_path: str,
    db: Database,
    force: bool = False,
    dry_run: bool = False,
    user_id: int = SHARED_SCOPE,
    quantity_mode: str = QUANTITY_MODE_SET,
) -> Dict[str, Any]:
    """
    Ingest a SortSwift export from disk.

    Writes no files: it updates the catalogue and reports what it found.
    The eBay-bound work is the drafts page and the API push.
    """
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
    return result
