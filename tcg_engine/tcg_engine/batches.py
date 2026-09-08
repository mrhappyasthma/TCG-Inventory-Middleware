import csv
import hashlib
import io
import os
from typing import Dict, Any, List, Optional
from .db import Database


# eBay File Exchange / Seller Hub Headers
REVISE_HEADERS = ["Action", "Item Number", "Custom Label", "Quantity", "Price"]
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
]

# eBay's variation syntax: within one attribute, values are separated by
# semicolons; a pipe separates different attributes. Card names containing
# either character would corrupt the RelationshipDetails field.
VARIATION_VALUE_SEPARATOR = ";"
VARIATION_ATTRIBUTE_SEPARATOR = "|"
VARIATION_ATTRIBUTE_NAME = "Card"

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
    for sep in (VARIATION_VALUE_SEPARATOR, VARIATION_ATTRIBUTE_SEPARATOR):
        cleaned = cleaned.replace(sep, "/")
    return cleaned


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


def _find_column(row: Dict[str, str], candidate_names: List[str]) -> Optional[str]:
    """Find a column in a row dict matching candidate names (case-insensitive and trimmed)."""
    normalized_row = {
        k.strip().lower(): v for k, v in row.items() if k is not None
    }
    for candidate in candidate_names:
        cand_clean = candidate.strip().lower()
        if cand_clean in normalized_row:
            return normalized_row[cand_clean]
    return None


def _parse_price(val: Optional[str]) -> float:
    """Safely parse price string to float."""
    if not val:
        return 0.0
    try:
        clean = str(val).replace("$", "").replace(",", "").strip()
        return float(clean) if clean and clean.upper() != "N/A" else 0.0
    except (ValueError, TypeError):
        return 0.0


def _empty_batch_result(logs: List[Dict[str, str]], **overrides) -> Dict[str, Any]:
    """Build a no-op batch result carrying the supplied log messages."""
    result = {
        "revise_csv": ",".join(REVISE_HEADERS) + "\n",
        "add_csv": ",".join(ADD_HEADERS) + "\n",
        "revise_count": 0,
        "add_count": 0,
        "new_catalog_count": 0,
        "skipped_count": 0,
        "duplicate": False,
        "logs": logs,
    }
    result.update(overrides)
    return result


def process_batch_csv(
    csv_text: str,
    db: Database,
    source_name: str = "batch.csv",
    force: bool = False,
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
    """
    logs: List[Dict[str, str]] = []
    revise_rows: List[Dict[str, Any]] = []

    # Raw items to be added (categorized into singles vs variation sets)
    staged_singles: List[Dict[str, Any]] = []
    # (set_name, condition) -> [cards]. Condition is part of the key because a
    # variation listing carries a single ConditionID for all of its options.
    staged_variations: Dict[tuple, List[Dict[str, Any]]] = {}

    new_catalog_count = 0
    skipped_count = 0

    # Load configuration settings
    single_threshold = float(db.get_listing_setting("single_threshold", "5.00"))
    title_template = db.get_listing_setting(
        "variation_title_template",
        DEFAULT_VARIATION_TITLE_TEMPLATE,
    )
    category_id = db.get_listing_setting("category_id", "183454")
    group_by_set = str(db.get_listing_setting("group_by_set", "true")).strip().lower() not in (
        "false",
        "0",
        "no",
    )

    lines = [line for line in csv_text.splitlines() if line.strip()]
    if not lines:
        return _empty_batch_result(
            [{"level": "WARN", "message": "Uploaded SortSwift batch file is empty."}]
        )

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return _empty_batch_result(
            [{
                "level": "ERROR",
                "message": "Unable to parse CSV headers in SortSwift batch file.",
            }]
        )

    # Refuse a replay of an already-processed file unless explicitly forced.
    batch_hash = hashlib.sha256(csv_text.encode("utf-8", errors="replace")).hexdigest()
    previous = db.find_processed_batch(batch_hash)
    if previous and not force:
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
    if previous and force:
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
            effective_price, _ = db.calculate_price(raw_base_price)

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

        try:
            quantity = int(qty_str.strip()) if qty_str else 1
            if quantity <= 0:
                quantity = 1
        except (ValueError, AttributeError):
            quantity = 1

        # Check or create manifest ID
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

        # Accumulate our own catalogued stock count for this card.
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
            new_consolidated_qty = prev_qty + quantity

            # Update live store mirror in DB
            db.upsert_variation(manifest_id, ebay_item_id, new_consolidated_qty)

            revise_rows.append({
                "Action": "Revise",
                "Item Number": ebay_item_id,
                "Custom Label": ebay_custom_label,
                "Quantity": new_consolidated_qty,
                "Price": f"{effective_price:.2f}",
            })

            logs.append({
                "level": "SUCCESS",
                "message": f"Row {row_idx}: [REVISE] #{ebay_item_id} [{ebay_custom_label}] {product_name} (+{quantity} => Total {new_consolidated_qty})",
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
                "printing": printing,
                "quantity": quantity,
                "price": effective_price,
                "cdn_image": cdn_image or "",
                "cdn_back_image": cdn_back_image or "",
                "stock_image": stock_image or "",
                "card_number": card_number or "",
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

        # Generate Parent Container Row
        parent_title = generate_variation_title(
            set_title, condition=group_condition, template=title_template
        )
        cover_image = next((c["cdn_image"] for c in cards if c["cdn_image"]), "")

        # Declare the option list on the parent. eBay separates values within
        # one attribute by semicolons; a pipe would be read as the start of a
        # second attribute and rejected.
        option_names = [_sanitize_variation_value(c["product_name"]) for c in cards]
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
        })

        # Append Child Variation Rows
        for c in cards:
            final_add_rows.append({
                "Action": "Add",
                "Category": category_id,
                "Title": "",
                "Relationship": "Variation",
                "RelationshipDetails": (
                    f"{VARIATION_ATTRIBUTE_NAME}="
                    f"{_sanitize_variation_value(c['product_name'])}"
                ),
                "Description": "",
                "ConditionID": c["condition_id"],
                "StartPrice": f"{c['price']:.2f}",
                "Quantity": c["quantity"],
                "CustomLabel": c["custom_label"],
                "PicURL": c["cdn_image"],
                "Format": "FixedPrice",
                "Duration": "GTC",
                "Price": f"{c['price']:.2f}",
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
            s["cdn_image"],
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
        })

    # Generate REVISE CSV
    revise_io = io.StringIO()
    rev_writer = csv.DictWriter(revise_io, fieldnames=REVISE_HEADERS, lineterminator="\n")
    rev_writer.writeheader()
    rev_writer.writerows(revise_rows)
    revise_csv = revise_io.getvalue()

    # Generate ADD CSV
    add_io = io.StringIO()
    add_writer = csv.DictWriter(add_io, fieldnames=ADD_HEADERS, lineterminator="\n")
    add_writer.writeheader()
    add_writer.writerows(final_add_rows)
    add_csv = add_io.getvalue()

    total_added_cards = len(staged_singles) + sum(len(c) for c in staged_variations.values())

    logs.append({
        "level": "INFO",
        "message": f"Batch routing finished: {len(revise_rows)} items to REVISE, {len(staged_variations)} Set/Condition Variation Listings ({sum(len(c) for c in staged_variations.values())} child cards), {len(staged_singles)} Single Listings ({new_catalog_count} new catalog entries created).",
    })

    # Fingerprint only after the batch has actually been applied, so a failure
    # part-way through does not mark the file as done.
    db.record_processed_batch(
        sha256=batch_hash,
        source_name=source_name,
        row_count=len(lines) - 1,
    )

    return {
        "revise_csv": revise_csv,
        "add_csv": add_csv,
        "revise_count": len(revise_rows),
        "add_count": total_added_cards,
        "new_catalog_count": new_catalog_count,
        "skipped_count": skipped_count,
        "duplicate": False,
        "logs": logs,
    }


def process_batch_file(
    input_path: str,
    db: Database,
    revise_output_path: Optional[str] = None,
    add_output_path: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Process a SortSwift batch CSV file from disk."""
    with open(input_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    result = process_batch_csv(
        content,
        db,
        source_name=os.path.basename(input_path),
        force=force,
    )
    if revise_output_path and result["revise_count"] > 0:
        with open(revise_output_path, "w", encoding="utf-8", newline="") as f:
            f.write(result["revise_csv"])
    if add_output_path and result["add_count"] > 0:
        with open(add_output_path, "w", encoding="utf-8", newline="") as f:
            f.write(result["add_csv"])
    return result
