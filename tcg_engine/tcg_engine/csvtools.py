"""
Shared CSV helpers for reading third-party exports.

Every module used to carry its own copy of the column lookup, which meant a
parsing quirk had to be fixed four times. It was not: an eBay report shipped
with a UTF-8 BOM, so its first header arrived as ``'\\ufeffItem number'``,
matched nothing, and Module B silently recorded every eBay item number as
"UNKNOWN" while otherwise appearing to succeed. Keeping this in one place means
the next quirk is fixed once.
"""

from typing import Dict, List, Optional

BOM = "﻿"


def strip_bom(text: str) -> str:
    """Remove a leading UTF-8 BOM, which otherwise corrupts the first header."""
    if text and text.startswith(BOM):
        return text[len(BOM):]
    return text


def read_csv_text(path: str) -> str:
    """
    Read a CSV from disk, tolerating a BOM and undecodable bytes.

    ``utf-8-sig`` consumes a BOM if present and behaves like ``utf-8`` when it
    is not, so it is always the safer choice for a file we did not write.
    """
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        return strip_bom(f.read())


def decode_csv_bytes(raw: bytes) -> str:
    """Decode uploaded CSV bytes, tolerating a BOM and undecodable bytes."""
    return strip_bom(raw.decode("utf-8-sig", errors="replace"))


def find_column(
    row: Dict[str, str],
    candidate_names: List[str],
    skip_blank: bool = False,
) -> Optional[str]:
    """
    Look up a value by any of several possible column names.

    Matching ignores case, surrounding whitespace, a stray BOM and eBay's
    leading asterisk on required fields, so "*ConditionID", " conditionid " and
    "ConditionID" are all the same column.

    ``skip_blank`` keeps searching past a column that exists but is empty.
    Off by default because emptiness is meaningful for some lookups -- a blank
    custom label is how a variation listing's parent row is recognised -- but
    it is essential where the same value lives in different columns depending
    on the row, as eBay's price columns do.
    """
    normalized_row = {}
    for key, value in row.items():
        if key is None:
            continue
        clean = strip_bom(str(key)).strip().lstrip("*").strip().lower()
        # First occurrence wins; a duplicate header should not shadow it.
        normalized_row.setdefault(clean, value)

    for candidate in candidate_names:
        clean = strip_bom(str(candidate)).strip().lstrip("*").strip().lower()
        if clean in normalized_row:
            value = normalized_row[clean]
            if skip_blank and (value is None or not str(value).strip()):
                # Present but empty. Keep looking: eBay's Active Listings
                # report puts a variation's price in "Start price" and leaves
                # "Current price" blank, while the parent row does the
                # opposite. Returning the blank would lose the value entirely.
                continue
            return value
    return None


def parse_price(value) -> float:
    """
    Parse a price cell to a float, tolerating report formatting.

    Handles currency symbols, thousands separators, a trailing currency code
    and the literal "N/A". Returns 0.0 rather than raising, because a single
    unreadable price must not abort a whole report.
    """
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return 0.0
    # Strip everything that is not part of a number, which covers "$1.99",
    # "1,299.00", "GBP 4.50" and "4.50 USD" alike.
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch in ".-")
    if cleaned in ("", "-", ".", "-."):
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0
