import csv
import io
import re
from typing import Dict, Any, List, Optional, Tuple
from .csvtools import find_column as _find_column, read_csv_text, strip_bom
from .db import Database

# SortSwift / TCGplayer Import & Deduction Template Headers.
#
# Quantities in a deduction file are NEGATIVE. SortSwift's import adds the
# quantity column to existing stock, so a positive value would increase
# inventory instead of reducing it.
# SortSwift officially accepts 'skuId' (Recommended), or 'productId', 'Product Name', 'Set Name', 'Condition', 'Printing', 'Quantity'
SORTSWIFT_HEADERS = [
    "skuId",
    "productId",
    "Order Number",
    "Product Name",
    "Set Name",
    "Condition",
    "Printing",
    "Quantity",
]




def _find_header_line(text_lines: List[str]) -> int:
    """
    Find line index where the CSV headers start.
    Handles metadata/intro lines in eBay reports.
    """
    keywords = ["custom label", "item number", "order number", "sales record", "quantity", "sku"]
    for idx, line in enumerate(text_lines):
        line_lower = line.lower()
        matches = sum(1 for kw in keywords if kw in line_lower)
        if matches >= 2:
            return idx
    return 0


def build_deduction_csv(rows: List[Dict[str, Any]]) -> str:
    """
    Render rows in SortSwift's deduction import shape.

    Shared by Module C and by manual quantity corrections, so a hand-made
    adjustment produces a file identical in form to a real order.
    """
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=SORTSWIFT_HEADERS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def deduction_row(
    card: Dict[str, Any], quantity: int, order_number: str
) -> Dict[str, Any]:
    """
    Build one SortSwift deduction row from a catalog card.

    ``quantity`` is given as a positive number of cards sold or removed, and is
    written out **negative**. SortSwift's inventory import adds the value in the
    quantity column, so a positive figure would increase stock -- the opposite of
    a deduction. Per its documentation, "if you place a negative number in the
    quantity field, it will remove that amount from your existing quantity",
    clamping at zero rather than going negative.
    """
    return {
        "skuId": card.get("sku_id") or "",
        "productId": card.get("tcgplayer_id") or "",
        "Order Number": order_number,
        "Product Name": card.get("product_name", ""),
        "Set Name": card.get("set_name", ""),
        "Condition": card.get("condition", ""),
        "Printing": card.get("printing", ""),
        "Quantity": -abs(int(quantity)),
    }


def process_orders_csv(
    csv_text: str, db: Database
) -> Dict[str, Any]:
    """
    Process raw eBay orders CSV and generate SortSwift / TCGplayer Orders Import CSV.
    
    Uses manifest_id lookup to retrieve exact card attributes and SortSwift 'skuId' / 'productId'.
    """
    csv_text = strip_bom(csv_text)
    logs: List[Dict[str, str]] = []
    output_rows: List[Dict[str, Any]] = []
    converted_count = 0
    skipped_count = 0

    lines = [line for line in csv_text.splitlines() if line.strip()]
    if not lines:
        return {
            "csv_content": ",".join(SORTSWIFT_HEADERS) + "\n",
            "converted_count": 0,
            "skipped_count": 0,
            "logs": [{"level": "WARN", "message": "Uploaded eBay orders file is empty."}],
            "output_rows": [],
        }

    header_idx = _find_header_line(lines)
    csv_payload = "\n".join(lines[header_idx:])

    reader = csv.DictReader(io.StringIO(csv_payload))
    if not reader.fieldnames:
        return {
            "csv_content": ",".join(SORTSWIFT_HEADERS) + "\n",
            "converted_count": 0,
            "skipped_count": 0,
            "logs": [{"level": "ERROR", "message": "Unable to parse CSV headers in eBay orders file."}],
            "output_rows": [],
        }

    logs.append({
        "level": "INFO",
        "message": f"Processing eBay orders file with {len(lines) - header_idx - 1} data rows..."
    })

    for row_idx, row in enumerate(reader, start=1):
        # Extract custom label (the manifest_id)
        custom_label = _find_column(
            row,
            [
                "Custom Label",
                "Custom label (SKU)",
                "Custom label",
                "CustomLabel",
                "SKU",
                "Custom label / SKU",
            ],
        )

        if not custom_label or not custom_label.strip():
            skipped_count += 1
            item_title = _find_column(row, ["Item Title", "Title", "Item title"]) or "Unknown Item"
            logs.append({
                "level": "WARN",
                "message": f"Row {row_idx}: Skipped non-TCG or unlabeled item '{item_title[:40]}' (No Custom Label).",
            })
            continue

        raw_custom_label = custom_label.strip()
        # Extract base manifest ID (e.g. 'ID1001' from 'ID1001-BIN_A12' or 'ID1001')
        match = re.search(r"(ID\d+)", raw_custom_label, re.IGNORECASE)
        manifest_id = match.group(1).upper() if match else raw_custom_label

        # Lookup card in master manifest
        card = db.get_manifest_by_id(manifest_id)
        if not card:
            skipped_count += 1
            logs.append({
                "level": "WARN",
                "message": f"Row {row_idx}: Custom Label '{raw_custom_label}' (ID: {manifest_id}) not found in master catalog. Skipped.",
            })
            continue

        # Extract Quantity
        qty_str = _find_column(
            row,
            ["Quantity", "Qty", "Sold Quantity", "Item Quantity", "Number of Items"],
        )
        try:
            quantity = int(qty_str.strip()) if qty_str else 1
            if quantity <= 0:
                quantity = 1
        except (ValueError, AttributeError):
            quantity = 1

        # Extract Order Number
        order_num = _find_column(
            row,
            [
                "Order Number",
                "Order number",
                "Order ID",
                "Sales Record Number",
                "Record Number",
                "Sales record number",
            ],
        )
        if not order_num or not order_num.strip():
            order_num = f"ORD-{row_idx:04d}"
        else:
            order_num = order_num.strip()

        # Negative: this file deducts sold stock. See deduction_row().
        converted_row = deduction_row(card, quantity, order_num)
        output_rows.append(converted_row)
        converted_count += quantity

        sku_note = f" (SKU: {card.get('sku_id')})" if card.get("sku_id") else ""
        logs.append({
            "level": "SUCCESS",
            "message": f"Row {row_idx}: Converted order {order_num} -> [{manifest_id}] {card['product_name']} ({card['condition']}, {card['printing']}){sku_note} x{quantity}",
        })

    # Generate output CSV
    csv_content = build_deduction_csv(output_rows)

    logs.append({
        "level": "INFO",
        "message": f"Orders conversion complete: {len(output_rows)} line items ({converted_count} cards total), {skipped_count} skipped.",
    })

    return {
        "csv_content": csv_content,
        "converted_count": converted_count,
        "skipped_count": skipped_count,
        "logs": logs,
        "output_rows": output_rows,
    }


def process_orders_file(
    input_path: str, db: Database, output_path: Optional[str] = None
) -> Dict[str, Any]:
    """Process an eBay orders CSV file from disk."""
    content = read_csv_text(input_path)
    result = process_orders_csv(content, db)
    if output_path:
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            f.write(result["csv_content"])
    return result
