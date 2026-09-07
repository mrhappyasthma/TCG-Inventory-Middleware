import csv
import io
import re
from typing import Dict, Any, List, Optional
from .db import Database


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


def sync_active_listings_csv(
    csv_text: str, db: Database
) -> Dict[str, Any]:
    """
    Process eBay Active Listings report CSV and synchronize ebay_variations store mirror.
    
    Filters out parent container rows and unmapped items.
    UPSERTs Item Number and live Quantity for matching manifest IDs.
    """
    logs: List[Dict[str, str]] = []
    synced_count = 0
    skipped_parent_count = 0
    skipped_unmapped_count = 0

    lines = [line for line in csv_text.splitlines() if line.strip()]
    if not lines:
        return {
            "synced_count": 0,
            "skipped_parent_count": 0,
            "skipped_unmapped_count": 0,
            "logs": [{"level": "WARN", "message": "Uploaded Active Listings file is empty."}],
        }

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return {
            "synced_count": 0,
            "skipped_parent_count": 0,
            "skipped_unmapped_count": 0,
            "logs": [{"level": "ERROR", "message": "Unable to parse CSV headers in Active Listings file."}],
        }

    logs.append({
        "level": "INFO",
        "message": f"Synchronizing eBay store state from {len(lines) - 1} listing records..."
    })

    current_parent_item_id: Optional[str] = None

    for row_idx, row in enumerate(reader, start=1):
        item_id = _find_column(
            row,
            [
                "Item number",
                "Item Number",
                "ItemID",
                "Item ID",
                "Item ID (Item number)",
                "Item",
            ],
        )
        if item_id and item_id.strip():
            current_parent_item_id = item_id.strip()

        custom_label = _find_column(
            row,
            [
                "Custom label (SKU)",
                "Custom label",
                "Custom Label",
                "CustomLabel",
                "SKU",
                "Custom label / SKU",
            ],
        )

        # Skip parent container rows where Custom Label is blank
        if not custom_label or not custom_label.strip():
            skipped_parent_count += 1
            continue

        raw_custom_label = custom_label.strip()
        match = re.search(r"(ID\d+)", raw_custom_label, re.IGNORECASE)
        manifest_id = match.group(1).upper() if match else raw_custom_label

        # Check if manifest ID exists in master catalog
        manifest_card = db.get_manifest_by_id(manifest_id)
        if not manifest_card:
            # If not found in catalog, log a note and skip
            skipped_unmapped_count += 1
            logs.append({
                "level": "WARN",
                "message": f"Row {row_idx}: Active listing has Custom Label '{raw_custom_label}' (ID: {manifest_id}) which is not in Master Catalog. Skipped.",
            })
            continue

        # Extract available quantity
        qty_str = _find_column(
            row,
            [
                "Available quantity",
                "Quantity available",
                "Available Quantity",
                "Quantity",
                "Qty",
            ],
        )
        try:
            quantity = int(qty_str.strip()) if qty_str else 0
            if quantity < 0:
                quantity = 0
        except (ValueError, AttributeError):
            quantity = 0

        # Determine effective eBay Item Number
        effective_item_id = current_parent_item_id or (item_id.strip() if item_id else "UNKNOWN")

        # Perform atomic UPSERT into ebay_variations
        db.upsert_variation(manifest_id, effective_item_id, quantity)
        synced_count += 1

        logs.append({
            "level": "SUCCESS",
            "message": f"Synced [{manifest_id}] {manifest_card['product_name']} -> eBay #{effective_item_id} (Live Qty: {quantity})",
        })

    logs.append({
        "level": "INFO",
        "message": f"Sync complete: {synced_count} active variations updated, {skipped_parent_count} parent rows ignored, {skipped_unmapped_count} unmapped items skipped.",
    })

    return {
        "synced_count": synced_count,
        "skipped_parent_count": skipped_parent_count,
        "skipped_unmapped_count": skipped_unmapped_count,
        "logs": logs,
    }


def sync_active_listings_file(
    input_path: str, db: Database
) -> Dict[str, Any]:
    """Sync eBay active listings from a file on disk."""
    with open(input_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return sync_active_listings_csv(content, db)
