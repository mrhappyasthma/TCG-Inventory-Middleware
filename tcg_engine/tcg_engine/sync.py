import csv
import io
import re
from typing import Dict, Any, List, Optional
from .csvtools import find_column as _find_column, read_csv_text, strip_bom, parse_price
from .db import Database




def sync_active_listings_csv(
    csv_text: str, db: Database
) -> Dict[str, Any]:
    """
    Process eBay Active Listings report CSV and synchronize ebay_variations store mirror.
    
    Filters out parent container rows and unmapped items.
    UPSERTs Item Number and live Quantity for matching manifest IDs.
    """
    csv_text = strip_bom(csv_text)
    logs: List[Dict[str, str]] = []
    synced_count = 0
    skipped_parent_count = 0
    skipped_unlabelled_count = 0
    skipped_unmapped_count = 0
    linked_item_ids = set()
    seen_manifest_ids = set()

    lines = [line for line in csv_text.splitlines() if line.strip()]
    if not lines:
        return {
            "synced_count": 0,
            "skipped_parent_count": 0,
            "skipped_unlabelled_count": 0,
            "skipped_unmapped_count": 0,
            "logs": [{"level": "WARN", "message": "Uploaded Active Listings file is empty."}],
        }

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return {
            "synced_count": 0,
            "skipped_parent_count": 0,
            "skipped_unlabelled_count": 0,
            "skipped_unmapped_count": 0,
            "logs": [{"level": "ERROR", "message": "Unable to parse CSV headers in Active Listings file."}],
        }

    logs.append({
        "level": "INFO",
        "message": f"Synchronizing eBay store state from {len(lines) - 1} listing records..."
    })

    current_parent_item_id: Optional[str] = None

    for row_idx, row in enumerate(reader, start=1):
        # Two report shapes reach this parser and must both work. The Seller
        # Hub Active Listings report uses human labels ("Item number", "Custom
        # label (SKU)", "Available quantity"); the Feed API's
        # LMS_ACTIVE_INVENTORY_REPORT, fetched without anyone clicking through
        # Seller Hub, uses field names (ItemID, SKU, Quantity, Price). Adding
        # the second set as candidates reuses this parser rather than growing
        # a second one, which matters because every rule below -- the parent
        # row skip, the manifest lookup, the delisting sweep -- would
        # otherwise need reimplementing and could drift.
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

        # A blank custom label means one of two very different things, and
        # conflating them made an ordinary store look broken: an Active Listings
        # report contains every listing you have, most of which were never
        # created by this tool and legitimately carry no SKU.
        if not custom_label or not custom_label.strip():
            variation_details = _find_column(
                row,
                [
                    "Variation details",
                    "Variation",
                    "Relationship details",
                    "RelationshipDetails",
                ],
            )
            if variation_details and variation_details.strip():
                # The container row of a multi-variation listing; its children
                # carry the labels we actually need.
                skipped_parent_count += 1
            else:
                # A listing with no custom label at all - not managed here.
                skipped_unlabelled_count += 1
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
                # The Feed report's own name. Listed after the explicit
                # "available" spellings so a report carrying both cannot have
                # the ambiguous one win.
                "Quantity",
                "AvailableQuantity",
                "Qty",
            ],
            skip_blank=True,
        )
        try:
            quantity = int(qty_str.strip()) if qty_str else 0
            if quantity < 0:
                quantity = 0
        except (ValueError, AttributeError):
            quantity = 0

        # eBay's price for this variation. Without it, Module A cannot tell
        # whether a Revise row would change anything, and has to emit one for
        # every card. Absent from some report layouts, which is why "unknown"
        # is a distinct state from "zero".
        # On a variation listing the child rows carry "Start price" and leave
        # "Current price" blank; the parent row is the other way round. Without
        # skip_blank the first candidate wins with an empty string and the
        # price is never learned, which silently disables no-op suppression.
        price_cell = _find_column(
            row,
            [
                "Current price",
                "Current Price",
                "Start price",
                "Start Price",
                "Buy It Now price",
                "Buy It Now Price",
                # The Feed report's own name, last so the more specific
                # Seller Hub spellings win when both are present.
                "Price",
                "StartPrice",
                "Fixed price",
            ],
            skip_blank=True,
        )
        reported_price = parse_price(price_cell) if price_cell else 0.0
        known_price = reported_price if reported_price > 0 else None

        # Determine effective eBay Item Number
        effective_item_id = current_parent_item_id or (item_id.strip() if item_id else "UNKNOWN")

        # Perform atomic UPSERT into ebay_variations
        # eBay's own label is authoritative, and is the only place the
        # bin/remark suffix can be learned reliably.
        db.upsert_variation(
            manifest_id, effective_item_id, quantity,
            custom_label=raw_custom_label,
            last_known_price=known_price,
        )
        synced_count += 1
        linked_item_ids.add(effective_item_id)
        seen_manifest_ids.add(manifest_id)

        logs.append({
            "level": "SUCCESS",
            "message": f"Synced [{manifest_id}] {manifest_card['product_name']} -> eBay #{effective_item_id} (Live Qty: {quantity})",
        })

    # A card linked to a listing but absent from the report is not live on eBay
    # any more -- it sold out, or the listing ended. Leaving its last known
    # quantity in place would keep reporting stock eBay does not have, which is
    # the same failure as Module A writing this column speculatively. Only a
    # sync may set it, and this is a sync.
    delisted_count = 0
    for live in db.get_live_variations():
        if live["manifest_id"] in seen_manifest_ids:
            continue
        if int(live.get("last_known_qty") or 0) == 0:
            continue
        db.upsert_variation(live["manifest_id"], live["ebay_parent_id"], 0)
        delisted_count += 1
        logs.append({
            "level": "WARN",
            "message": (
                f"[{live['manifest_id']}] {live.get('product_name') or '?'} "
                f"({live.get('set_name') or '?'} | {live.get('condition') or '?'}) "
                f"is not in this Active Listings report, so eBay no longer has "
                f"it. Its live quantity is now 0 (was {live.get('last_known_qty')})."
            ),
        })

    summary = [
        f"Sync complete: {synced_count} variation(s) updated "
        f"across {len(linked_item_ids)} eBay listing(s)"
    ]
    if delisted_count:
        summary.append(
            f"{delisted_count} card(s) no longer on eBay set to 0"
        )
    if skipped_parent_count:
        summary.append(f"{skipped_parent_count} variation parent row(s) ignored")
    if skipped_unlabelled_count:
        summary.append(
            f"{skipped_unlabelled_count} listing(s) with no custom label ignored "
            f"(not managed by this tool)"
        )
    if skipped_unmapped_count:
        summary.append(
            f"{skipped_unmapped_count} custom label(s) not found in the master catalog"
        )
    logs.append({"level": "INFO", "message": "; ".join(summary) + "."})

    return {
        "synced_count": synced_count,
        "delisted_count": delisted_count,
        "linked_listing_count": len(linked_item_ids),
        "skipped_parent_count": skipped_parent_count,
        "skipped_unlabelled_count": skipped_unlabelled_count,
        "skipped_unmapped_count": skipped_unmapped_count,
        "logs": logs,
    }


def sync_active_listings_file(
    input_path: str, db: Database
) -> Dict[str, Any]:
    """Sync eBay active listings from a file on disk."""
    content = read_csv_text(input_path)
    return sync_active_listings_csv(content, db)
