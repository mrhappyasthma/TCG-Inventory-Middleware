#!/usr/bin/env python3
"""
Correct the identity fields in a Chinese SortSwift export before it is imported.

Three columns in ``export_eBay_<date>.csv`` for the Chinese sets are wrong in
ways that **cannot be fixed after the import**, because two of them form part
of a card's identity. The natural key is (product name, set name, condition,
printing), so changing either afterwards does not correct the card -- it
creates a second one, and the next upload re-creates the original beside it.
That is the same trap ``db.set_manifest_condition`` documents for the grade.

What it fixes, and why each is worth a pass over the file:

* **Three set codes are placeholders.** ``Gem Pack 4/5/6`` restate the set
  name rather than naming the set, and the title template puts the code in
  front of buyers. The real codes are CBB4C / CBB5C / CBB6C.

* **Two set names are inconsistent.** The export spells them ``Gem Pack Vol
  4``, ``Gem Pack Vol 5`` but ``Gem Pack Volume 6``. All three become
  "Volume"; the title renderer abbreviates back to "Vol" by itself if a
  particular title will not fit in eBay's 80 characters.

* **The language aspect says Czech.** SortSwift exports these cards with
  ``Language`` = ``CS`` -- Chinese Simplified -- and then spells that out in
  ``*C:Language`` as ``Czech``. That column becomes a live eBay item
  specific, so every card would be listed as being in Czech. ``Chinese`` is
  confirmed against eBay's own item specifics for the card category, which
  matters because eBay only takes Language values from its own list. Only
  the aspect is corrected; the plain ``Language`` code stays ``CS``, because
  it selects the listing title template and ``CS`` is the code Listing Rules
  holds the Chinese format under.

**It deliberately leaves the card name alone**, and that is worth stating
because the obvious "improvement" is destructive. Every row spells
``*C:Card Name`` as ``"Applin - 1902"`` while ``Product Name (No Card
Number)`` holds a clean ``"Applin"``, which looks like an easy tidy-up --
the variation dropdown otherwise reads ``Applin - 1902 (1902)``. But the
card number is **not** part of this system's natural key, so the number
inside the name is the only thing keeping two different cards apart:
stripping it merges "Applin - 1902" and "Applin - 1903", which have
different card numbers, different TCGplayer ids and different prices.
Measured on the real file, it collapses 214 cards into 112 across 49
merges. The duplicated number in a dropdown label is the far cheaper
problem, and the option template is where to fix it.

**This is a workaround, not the fix.** The durable fix is to correct these in
SortSwift, because the next export will otherwise carry the old values again
and catalogue 172 Gem Pack cards a second time under the old names. Run this
only until SortSwift is right.

Writes nothing without ``-o``. By default it reports what it would change.

    python scripts/prepare_chinese_export.py ~/Desktop/chinese.csv
    python scripts/prepare_chinese_export.py ~/Desktop/chinese.csv \\
        -o ~/Desktop/chinese-corrected.csv

Deliberately a script rather than anything in the web app: it is a one-time
correction to a file on disk, it runs before the data exists in the
catalogue, and nothing in the dashboard should be able to rewrite an export.
"""

import argparse
import collections
import csv
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

from tcg_engine.csvtools import strip_bom  # noqa: E402

# The set corrections, as (set name in the export) -> (set name, set code).
#
# A hand-written table, which app code deliberately avoids -- but this is a
# one-off correction to one supplier's file, not a vocabulary the application
# carries. The codes came from the account holder; nothing derives them, and
# TCGCSV cannot supply them because it has no Chinese Pokemon catalogue at
# all (94 categories, only "Pokemon" and "Pokemon Japan").
SET_CORRECTIONS = {
    "Gem Pack Vol 4": ("Gem Pack Volume 4", "CBB4C"),
    "Gem Pack Vol 5": ("Gem Pack Volume 5", "CBB5C"),
    "Gem Pack Volume 6": ("Gem Pack Volume 6", "CBB6C"),
}

SET_COLUMN = "*C:Set"
SET_CODE_COLUMN = "Set Code"
CARD_NAME_COLUMN = "*C:Card Name"

# The eBay-facing language aspect, and the wrong value to replace.
#
# SortSwift exports these cards with `Language` = "CS" -- Chinese Simplified
# -- and then renders that into `*C:Language` as **"Czech"**. The plain code
# is right and the expansion is wrong, so the mistake is in whatever maps one
# to the other at their end. Left alone it ships as a live eBay item specific
# reading "Language: Czech" on every Chinese card.
#
# Corrected here, in the file, rather than by a lookup table inside the
# application. That distinction is the point: this project passes source
# values through verbatim precisely so it never holds a third vocabulary that
# can disagree with SortSwift's and eBay's, and a mapping in the ingest would
# be exactly that. A correction applied to the export keeps the app honest
# and leaves one obvious place to delete once SortSwift is fixed.
#
# "Chinese" is not a guess. eBay only accepts Language values from its own
# per-category list -- the same constraint that makes `default_game` an
# override rather than a fallback -- and this spelling was read off eBay's
# item specifics for the card category and confirmed to be one of them.
LANGUAGE_ASPECT_COLUMN = "*C:Language"
LANGUAGE_CODE_COLUMN = "Language"
WRONG_LANGUAGE = "Czech"
RIGHT_LANGUAGE = "Chinese"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Correct the set names, set codes and language aspect in a "
            "Chinese SortSwift export before importing it."
        ),
    )
    parser.add_argument("source", help="The export to read")
    parser.add_argument(
        "-o", "--out",
        help=(
            "Where to write the corrected copy. Without this the script "
            "only reports what it would change."
        ),
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.source):
        print(f"No such file: {args.source}")
        return 2

    with open(args.source, "r", encoding="utf-8-sig", newline="") as handle:
        text = strip_bom(handle.read())
    reader = csv.DictReader(text.splitlines())
    fieldnames = reader.fieldnames or []
    rows = list(reader)

    missing = [
        column for column in (SET_COLUMN, SET_CODE_COLUMN, CARD_NAME_COLUMN)
        if column not in fieldnames
    ]
    if missing:
        print(
            "This file does not have the columns this corrects: "
            + ", ".join(missing)
            + ".\nIt expects SortSwift's eBay export "
            "(export_eBay_<date>.csv), not the inventory export."
        )
        return 2

    set_changes = {}
    unknown_sets = {}
    language_fixed = 0
    language_other = collections.Counter()

    for row in rows:
        # The eBay aspect only. The plain `Language` column is deliberately
        # left as "CS", because that value becomes `manifest.language` and
        # is what selects the listing title template -- and `CS` is the key
        # Listing Rules has a Chinese template under. Rewriting it to
        # "Chinese" would look tidier and would silently drop every one of
        # these listings back onto the English title format.
        current = str(row.get(LANGUAGE_ASPECT_COLUMN) or "").strip()
        if current == WRONG_LANGUAGE:
            row[LANGUAGE_ASPECT_COLUMN] = RIGHT_LANGUAGE
            language_fixed += 1
        elif current and current != RIGHT_LANGUAGE:
            language_other[current] += 1

        original_set = str(row.get(SET_COLUMN) or "").strip()
        correction = SET_CORRECTIONS.get(original_set)
        if correction:
            new_set, new_code = correction
            key = (original_set, str(row.get(SET_CODE_COLUMN) or "").strip())
            set_changes.setdefault(key, [new_set, new_code, 0])
            set_changes[key][2] += 1
            row[SET_COLUMN] = new_set
            row[SET_CODE_COLUMN] = new_code
        elif original_set:
            unknown_sets.setdefault(
                original_set, str(row.get(SET_CODE_COLUMN) or "").strip()
            )

    print(f"{len(rows)} row(s) read from {args.source}\n")

    print("Set name and code corrections:")
    if set_changes:
        for (old_set, old_code), (new_set, new_code, count) in sorted(
            set_changes.items()
        ):
            print(
                f"  {count:>4} row(s)  {old_set!r} [{old_code}]"
                f"  ->  {new_set!r} [{new_code}]"
            )
    else:
        print("   none -- no row named a set this knows how to correct")

    print(
        f"\nLanguage aspect ({LANGUAGE_ASPECT_COLUMN}): "
        f"{language_fixed} row(s) {WRONG_LANGUAGE!r} -> {RIGHT_LANGUAGE!r}"
    )
    if language_fixed:
        print(
            "   SortSwift is exporting Chinese Simplified cards with the\n"
            "   language code 'CS' and then spelling that out as 'Czech'.\n"
            "   Left uncorrected it reaches eBay as a live item specific\n"
            "   reading 'Language: Czech' on every card. Fix it in SortSwift:\n"
            "   this rewrite is a stopgap that has to be re-run on every\n"
            "   export until then.\n"
            f"   {RIGHT_LANGUAGE!r} is confirmed against eBay's own item\n"
            "   specifics for this category, so the value is one it accepts."
        )
    if language_other:
        print(
            "   Rows left alone because they say something else: "
            + ", ".join(f"{v!r} x{n}" for v, n in sorted(language_other.items()))
        )
    # The plain code column is load-bearing and untouched; say so, because
    # "why is this still CS" is the obvious next question.
    codes = collections.Counter(
        str(row.get(LANGUAGE_CODE_COLUMN) or "").strip() for row in rows
    )
    print(
        f"   The plain {LANGUAGE_CODE_COLUMN!r} column is unchanged "
        f"({', '.join(f'{v!r} x{n}' for v, n in sorted(codes.items()))}). "
        f"It selects the title template, and Listing Rules holds the "
        f"Chinese format under that code."
    )

    if unknown_sets:
        # Named rather than silently passed through: a set this does not know
        # keeps whatever code the export gave it, and if that is another
        # placeholder it reaches the listing title.
        print(
            "\nSets left exactly as they are (no correction on record). "
            "Check their codes look like real set codes:"
        )
        for name, code in sorted(unknown_sets.items()):
            print(f"   {name!r} [{code}]")

    if not args.out:
        print("\nNothing was written. Pass -o <file> to write the corrected copy.")
        return 0

    with open(args.out, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} row(s) to {args.out}")
    print(
        "Upload that file through Module A. Correct SortSwift too -- the "
        "next export will otherwise carry the old names and catalogue these "
        "cards a second time."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
