import csv
import io
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

# Standard eBay Condition IDs for Trading Card Singles
CONDITION_MAP = {
    "nm": ("Near Mint", "3000"),
    "near mint": ("Near Mint", "3000"),
    "mint": ("Near Mint", "3000"),
    "lp": ("Lightly Played", "4000"),
    "lightly played": ("Lightly Played", "4000"),
    "excellent": ("Lightly Played", "4000"),
    "mp": ("Moderately Played", "5000"),
    "moderately played": ("Moderately Played", "5000"),
    "good": ("Moderately Played", "5000"),
    "hp": ("Heavily Played", "6000"),
    "heavily played": ("Heavily Played", "6000"),
    "played": ("Heavily Played", "6000"),
    "dm": ("Damaged", "6000"),
    "damaged": ("Damaged", "6000"),
    "dmg": ("Damaged", "6000"),
    "poor": ("Damaged", "6000"),
}


def generate_variation_title(
    set_name: str,
    template: str = "{set_name}: Pick Your Card - Near Mint - Complete Your Set",
) -> str:
    """
    Generate an optimized eBay listing title for variation drop-down listings.
    If longer than 80 chars, automatically replaces 'Near Mint' with 'NM'.
    """
    # 1. Try default template with full 'Near Mint'
    title = template.replace("{set_name}", set_name.strip())
    if len(title) <= 80:
        return title

    # 2. Fallback: Replace 'Near Mint' with 'NM'
    title_nm = template.replace("Near Mint", "NM").replace("{set_name}", set_name.strip())
    if len(title_nm) <= 80:
        return title_nm

    # 3. Compact fallback
    compact = f"{set_name.strip()}: Pick Your Card - NM - Complete Set"
    if len(compact) <= 80:
        return compact

    # 4. Truncate set name to ensure <= 80 chars
    suffix = ": Pick Your Card - NM"
    avail = 80 - len(suffix)
    return f"{set_name.strip()[:avail]}{suffix}"


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


def process_batch_csv(
    csv_text: str, db: Database
) -> Dict[str, Any]:
    """
    Process fresh SortSwift inventory batch CSV.
    Routes rows into:
    1. ebay_inventory_updates.csv (REVISE - already live on eBay)
    2. ebay_new_additions.csv (ADD - new to eBay)
    
    Automatically creates:
    - Multi-item Variation Listings (grouped by Set Name for cards < $5.00 threshold)
    - Standalone Single Listings (for high-value cards >= $5.00 threshold)
    """
    logs: List[Dict[str, str]] = []
    revise_rows: List[Dict[str, Any]] = []
    
    # Raw items to be added (categorized into singles vs variation sets)
    staged_singles: List[Dict[str, Any]] = []
    staged_variations: Dict[str, List[Dict[str, Any]]] = {}  # set_name -> [cards]

    new_catalog_count = 0
    skipped_count = 0

    # Load configuration settings
    single_threshold = float(db.get_listing_setting("single_threshold", "5.00"))
    title_template = db.get_listing_setting(
        "variation_title_template",
        "{set_name}: Pick Your Card - Near Mint - Complete Your Set",
    )
    category_id = db.get_listing_setting("category_id", "183454")

    lines = [line for line in csv_text.splitlines() if line.strip()]
    if not lines:
        return {
            "revise_csv": ",".join(REVISE_HEADERS) + "\n",
            "add_csv": ",".join(ADD_HEADERS) + "\n",
            "revise_count": 0,
            "add_count": 0,
            "new_catalog_count": 0,
            "skipped_count": 0,
            "logs": [{"level": "WARN", "message": "Uploaded SortSwift batch file is empty."}],
        }

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return {
            "revise_csv": ",".join(REVISE_HEADERS) + "\n",
            "add_csv": ",".join(ADD_HEADERS) + "\n",
            "revise_count": 0,
            "add_count": 0,
            "new_catalog_count": 0,
            "skipped_count": 0,
            "logs": [{"level": "ERROR", "message": "Unable to parse CSV headers in SortSwift batch file."}],
        }

    logs.append({
        "level": "INFO",
        "message": f"Processing SortSwift scan batch with {len(lines) - 1} cards (Single Threshold: ${single_threshold:.2f})..."
    })

    for row_idx, row in enumerate(reader, start=1):
        product_name = _find_column(row, ["Name", "Product Name", "Card Name", "Card", "Title", "Product"])
        set_name = _find_column(row, ["Set", "Set Name", "Expansion", "Edition"])
        raw_condition = _find_column(row, ["Condition", "Card Condition", "Grade"]) or "NM"
        printing = _find_column(row, ["Printing", "Finish", "Variant", "Foil"]) or "Normal"
        qty_str = _find_column(row, ["Quantity", "Qty", "Count", "Amount", "Add Quantity"])

        # Extra SortSwift fields
        sku_id = _find_column(row, ["SKU Id", "SKU", "SkuId", "skuId"])
        tcgplayer_id = _find_column(row, ["TCGplayer Id", "ProductId", "productId", "ID Product"])
        card_number = _find_column(row, ["Card Number", "Number"])
        set_code = _find_column(row, ["Set Code", "SetCode"])
        language = _find_column(row, ["Language", "Lang"]) or "EN"
        cdn_image = _find_column(row, ["CDN Image", "cdn_link", "PicURL", "Image URL", "CDN Link"])
        remarks = _find_column(row, ["Remarks", "Remark", "Bin", "binIndex", "Comment"])

        # Clean remark / bin string for eBay SKU encoding
        clean_remark = ""
        if remarks and str(remarks).strip().lower() not in ("no remark", "none", ""):
            clean_remark = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(remarks).strip())
            clean_remark = clean_remark[:20]

        # Dynamic Pricing Calculation
        ebay_price_val = _parse_price(_find_column(row, ["eBay Price", "Ebay Price", "ebay_price"]))
        standard_price_val = _parse_price(_find_column(row, ["Price", "Selling Price"]))
        market_price_val = _parse_price(_find_column(row, ["Market Price", "TCGPlayer Price", "Market"]))

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

        # Standardize Condition and ConditionID
        cond_clean = raw_condition.strip().lower()
        condition_name, condition_id = CONDITION_MAP.get(cond_clean, (raw_condition.strip(), "3000"))

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
                "card_number": card_number or "",
            }

            if effective_price >= single_threshold:
                staged_singles.append(card_entry)
                logs.append({
                    "level": "SUCCESS",
                    "message": f"Row {row_idx}: [ADD SINGLE] [{ebay_custom_label}] {product_name} (Price: ${effective_price:.2f} >= ${single_threshold:.2f} threshold)",
                })
            else:
                if set_name not in staged_variations:
                    staged_variations[set_name] = []
                staged_variations[set_name].append(card_entry)
                logs.append({
                    "level": "SUCCESS",
                    "message": f"Row {row_idx}: [ADD VARIATION] [{ebay_custom_label}] {product_name} grouped into '{set_name}' (Price: ${effective_price:.2f})",
                })

    # -----------------------------------------------------------------
    # BUILD FINAL EBAY ADD CSV (PARENT CONTAINERS + CHILD VARIATIONS + SINGLES)
    # -----------------------------------------------------------------
    final_add_rows: List[Dict[str, Any]] = []

    # 1. Multi-Item Variation Listings (grouped by Set)
    for set_title, cards in staged_variations.items():
        if not cards:
            continue

        # Generate Parent Container Row
        parent_title = generate_variation_title(set_title, template=title_template)
        cover_image = next((c["cdn_image"] for c in cards if c["cdn_image"]), "")
        
        # Build list of variation card options for parent container
        # Format: Card=Name1|Name2|Name3...
        option_names = [c["product_name"].replace("|", "/") for c in cards]
        parent_rel_details = "Card=" + "|".join(option_names)

        # Append Parent Container Row
        final_add_rows.append({
            "Action": "Add",
            "Category": category_id,
            "Title": parent_title,
            "Relationship": "Variation",
            "RelationshipDetails": parent_rel_details,
            "Description": f"Pick Your Card from {set_title}! Near Mint / Mint condition. Complete your collection.",
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
                "RelationshipDetails": f"Card={c['product_name'].replace('|', '/')}",
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
            "PicURL": s["cdn_image"],
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
        "message": f"Batch routing finished: {len(revise_rows)} items to REVISE, {len(staged_variations)} Set Variation Listings ({sum(len(c) for c in staged_variations.values())} child cards), {len(staged_singles)} Single Listings ({new_catalog_count} new catalog entries created).",
    })

    return {
        "revise_csv": revise_csv,
        "add_csv": add_csv,
        "revise_count": len(revise_rows),
        "add_count": total_added_cards,
        "new_catalog_count": new_catalog_count,
        "skipped_count": skipped_count,
        "logs": logs,
    }


def process_batch_file(
    input_path: str,
    db: Database,
    revise_output_path: Optional[str] = None,
    add_output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Process a SortSwift batch CSV file from disk."""
    with open(input_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    result = process_batch_csv(content, db)
    if revise_output_path and result["revise_count"] > 0:
        with open(revise_output_path, "w", encoding="utf-8", newline="") as f:
            f.write(result["revise_csv"])
    if add_output_path and result["add_count"] > 0:
        with open(add_output_path, "w", encoding="utf-8", newline="") as f:
            f.write(result["add_csv"])
    return result
