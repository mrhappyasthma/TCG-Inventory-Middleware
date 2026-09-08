"""
Recover the link between the local catalog and already-live eBay listings.

Manifest IDs are the join key between this catalog and eBay: they are written
into each listing's Custom Label, and Module C reads them back off an order to
work out which card sold. If the catalog is rebuilt -- after a purge, or on a
fresh install restoring from exports -- the newly minted IDs will not match the
labels already published on eBay, and Module B reports every row as "not in
Master Catalog".

Rather than re-creating the listings, this realigns the *catalog* to the labels
eBay already holds. The listing is the expensive, externally visible artefact;
a manifest ID is an internal detail, so it is the one that should move.

Cards are matched on identity taken from the listing's own variation details
(``Card=Ledyba (004/198)``), which is the only trustworthy link once the IDs
have diverged.
"""

import csv
import io
import re
from typing import Any, Dict, List, Optional, Tuple

from .csvtools import find_column as _find_column, read_csv_text, strip_bom
from .db import Database

# "Card=Ledyba (004/198)" -> attribute "Card", value "Ledyba (004/198)"
_VARIATION_VALUE = re.compile(r"^[^=]+=(.*)$", re.DOTALL)

# "Ledyba (004/198)" -> name "Ledyba", number "004/198"
_OPTION_NAME = re.compile(r"^(?P<name>.*?)\s*\((?P<number>[^()]+)\)\s*$")

_MANIFEST_ID = re.compile(r"(ID\d+)", re.IGNORECASE)


def parse_option_name(value: str) -> Tuple[str, str]:
    """
    Split a dropdown option back into its card name and number.

    "Ledyba (004/198)" -> ("Ledyba", "004/198")
    "Mystery Promo"    -> ("Mystery Promo", "")
    """
    text = str(value or "").strip()
    match = _OPTION_NAME.match(text)
    if not match:
        return text, ""
    return match.group("name").strip(), match.group("number").strip()


def relink_from_active_listings(csv_text: str, db: Database) -> Dict[str, Any]:
    """
    Realign catalog manifest IDs to the Custom Labels on live eBay listings.

    Returns counts plus a log. Nothing is renamed unless a single unambiguous
    catalog card matches the listing's card identity.
    """
    csv_text = strip_bom(csv_text)
    logs: List[Dict[str, str]] = []
    renamed = 0
    already_ok = 0
    unmatched = 0
    conflicted = 0

    lines = [line for line in csv_text.splitlines() if line.strip()]
    if not lines:
        return {
            "renamed_count": 0,
            "already_linked_count": 0,
            "unmatched_count": 0,
            "conflict_count": 0,
            "logs": [{"level": "WARN", "message": "Uploaded Active Listings file is empty."}],
        }

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return {
            "renamed_count": 0,
            "already_linked_count": 0,
            "unmatched_count": 0,
            "conflict_count": 0,
            "logs": [{"level": "ERROR", "message": "Unable to parse CSV headers."}],
        }

    for row_idx, row in enumerate(reader, start=1):
        custom_label = _find_column(
            row,
            ["Custom label (SKU)", "Custom label", "Custom Label", "CustomLabel", "SKU"],
        )
        if not custom_label or not custom_label.strip():
            continue

        id_match = _MANIFEST_ID.search(custom_label.strip())
        if not id_match:
            continue
        target_id = id_match.group(1).upper()

        variation = _find_column(
            row,
            ["Variation details", "Variation", "Relationship details", "RelationshipDetails"],
        )
        if not variation or not variation.strip():
            unmatched += 1
            logs.append({
                "level": "WARN",
                "message": (
                    f"Row {row_idx}: '{custom_label.strip()}' has no variation details, "
                    f"so the card it refers to cannot be identified. Skipped."
                ),
            })
            continue

        value_match = _VARIATION_VALUE.match(variation.strip())
        option_value = value_match.group(1).strip() if value_match else variation.strip()
        card_name, card_number = parse_option_name(option_value)

        candidates = db.find_manifest_by_identity(card_name, card_number)

        if not candidates:
            unmatched += 1
            logs.append({
                "level": "WARN",
                "message": (
                    f"Row {row_idx}: no catalog card matches '{option_value}' "
                    f"(label {target_id}). Skipped."
                ),
            })
            continue

        if len(candidates) > 1:
            conflicted += 1
            ids = ", ".join(c["manifest_id"] for c in candidates)
            logs.append({
                "level": "WARN",
                "message": (
                    f"Row {row_idx}: '{option_value}' matches several catalog cards "
                    f"({ids}), so it is ambiguous. Skipped."
                ),
            })
            continue

        current = candidates[0]
        current_id = current["manifest_id"]

        if current_id == target_id:
            already_ok += 1
            continue

        occupant = db.get_manifest_by_id(target_id)
        if occupant:
            conflicted += 1
            logs.append({
                "level": "WARN",
                "message": (
                    f"Row {row_idx}: cannot rename {current_id} to {target_id} because "
                    f"{target_id} is already used by '{occupant['product_name']}'. Skipped."
                ),
            })
            continue

        db.rename_manifest(current_id, target_id)
        renamed += 1
        logs.append({
            "level": "SUCCESS",
            "message": (
                f"Row {row_idx}: {current_id} -> {target_id} for '{option_value}', "
                f"matching the live listing's custom label."
            ),
        })

    summary = [f"Relink complete: {renamed} card(s) realigned to their eBay labels"]
    if already_ok:
        summary.append(f"{already_ok} already correct")
    if unmatched:
        summary.append(f"{unmatched} label(s) with no matching catalog card")
    if conflicted:
        summary.append(f"{conflicted} skipped as ambiguous or conflicting")
    logs.append({"level": "INFO", "message": "; ".join(summary) + "."})

    return {
        "renamed_count": renamed,
        "already_linked_count": already_ok,
        "unmatched_count": unmatched,
        "conflict_count": conflicted,
        "logs": logs,
    }


def relink_from_file(input_path: str, db: Database) -> Dict[str, Any]:
    """Relink from an Active Listings report on disk."""
    return relink_from_active_listings(read_csv_text(input_path), db)
