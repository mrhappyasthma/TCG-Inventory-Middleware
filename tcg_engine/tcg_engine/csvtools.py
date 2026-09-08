"""
Shared CSV helpers for reading third-party exports.

Every module used to carry its own copy of the column lookup, which meant a
parsing quirk had to be fixed four times. It was not: an eBay report shipped
with a UTF-8 BOM, so its first header arrived as ``'\\ufeffItem number'``,
matched nothing, and Module C silently recorded every eBay item number as
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


def find_column(row: Dict[str, str], candidate_names: List[str]) -> Optional[str]:
    """
    Look up a value by any of several possible column names.

    Matching ignores case, surrounding whitespace, a stray BOM and eBay's
    leading asterisk on required fields, so "*ConditionID", " conditionid " and
    "ConditionID" are all the same column.
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
            return normalized_row[clean]
    return None
