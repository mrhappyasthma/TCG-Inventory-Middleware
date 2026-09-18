#!/usr/bin/env python3
"""
Say what condition is stored for a set's cards, and which draft group it puts
them in.

Written for a report that a freshly imported set showed as **LP** on the
drafts page when every row of the export said **NM**. The ingest was not the
cause: ``process_batch_csv`` reads the plain ``Condition`` column, passes it
through verbatim, and was verified against the export in question -- so the
question worth asking is what the *catalogue* holds, and there was no way to
ask it short of opening the database by hand.

Three things it separates, because they look identical from the drafts page
and need different remedies:

* **The condition stored on the card.** This is the only thing the drafts
  page's block heading reflects: a group's key is ``<set>|<condition>`` and
  its heading is the condition of the cards in it. No per-card condition is
  shown anywhere on that page, so a block headed LP means those cards are
  stored LP.
* **A twin.** Condition is part of a card's identity (name + set + condition +
  printing), so importing the same card at a second grade creates a *second
  card* rather than correcting the first. Two blocks then appear for one set,
  and the older one carries the stock. This is the likeliest way a set that
  was imported before comes out looking like the wrong grade now, and it is
  the case the export alone cannot explain.
* **When it was catalogued.** ``created_at`` says whether a card predates the
  import being blamed. A card older than the file did not come from it.

Reads only. It writes nothing, and it does not touch eBay.

    python scripts/explain_condition.py "Ascended Heroes"
    python scripts/explain_condition.py "Ascended Heroes" --limit 0
    python scripts/explain_condition.py          # every set, one line each

On the NAS:

    cd /volume1/docker/tcg-middleware
    docker compose exec tcg-middleware python scripts/explain_condition.py "Ascended Heroes"
"""

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

from tcg_engine.db import Database  # noqa: E402
from tcg_engine.plans import PLAN_DRAFT, variation_group_key  # noqa: E402


def resolve_inventory(db_path=None, user_id=None):
    """
    The database to read: an explicit path, or the account's own file.

    ``app.deps`` is imported only when it is needed, because importing it
    pulls in ``app.auth``, which refuses to load without ``GOOGLE_CLIENT_ID``.
    That is right for the running app and wrong for a read-only diagnostic
    pointed at a backup file, which needs no web configuration at all.
    """
    if db_path:
        return Database(db_path)
    from app import deps

    scope = user_id if user_id is not None else deps._owner_scope()
    return deps.inventory_for(scope)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Show the condition stored for a set's cards and the draft group "
            "it places them in."
        ),
    )
    parser.add_argument(
        "set_name", nargs="?", default=None, metavar="SET",
        help=(
            "Part of the expansion set name, matched case-insensitively. "
            "Omit to summarise every set."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=25, metavar="N",
        help="How many cards to list per section (default 25, 0 for all).",
    )
    parser.add_argument(
        "--user-id", type=int, default=None,
        help="Whose inventory to read. Defaults to the deployment owner.",
    )
    parser.add_argument(
        "--db", default=None, metavar="PATH",
        help=(
            "Read this database file instead of resolving the account's own. "
            "Useful against a downloaded backup."
        ),
    )
    args = parser.parse_args(argv)

    inv = resolve_inventory(args.db, args.user_id)
    cap = None if args.limit == 0 else args.limit

    with inv.get_connection() as conn:
        cur = conn.cursor()

        if args.set_name is None:
            summarise_every_set(cur)
            return 0

        pattern = f"%{args.set_name.strip()}%"
        cur.execute(
            """
            SELECT set_name, condition, COUNT(*) AS cards,
                   SUM(COALESCE(quantity, 0)) AS copies,
                   MIN(created_at) AS first_seen,
                   MAX(created_at) AS last_seen
              FROM manifest
             WHERE set_name LIKE ? COLLATE NOCASE
             GROUP BY set_name, condition
             ORDER BY set_name, cards DESC
            """,
            (pattern,),
        )
        breakdown = cur.fetchall()

        if not breakdown:
            print()
            print(f"  No catalogued card's set name contains "
                  f"{args.set_name.strip()!r}.")
            print("  Nothing else below can mean anything, so it is not printed.")
            return 1

        print()
        print("  What the catalogue holds")
        print(f"    {'set':<34} {'condition':<12} {'cards':>6} {'copies':>7} "
              f"  catalogued")
        print(f"    {'-' * 34} {'-' * 12} {'-' * 6} {'-' * 7}   {'-' * 19}")
        for row in breakdown:
            window = str(row["first_seen"] or "")[:19]
            if str(row["last_seen"] or "")[:10] != str(row["first_seen"] or "")[:10]:
                window += f" .. {str(row['last_seen'] or '')[:10]}"
            print(f"    {str(row['set_name'] or '')[:34]:<34} "
                  f"{str(row['condition'] or '(none)'):<12} "
                  f"{row['cards']:>6,} {row['copies'] or 0:>7,}   {window}")

        # The group key each of those becomes, spelled out. The drafts page
        # heading is this key's condition half and nothing else, so seeing the
        # two side by side is what connects the screen to the data.
        print()
        print("  The draft group each would fall into")
        for row in breakdown:
            print(f"    {variation_group_key(row['set_name'], row['condition'])!r}")
        print("    (a card priced at or above the single-listing threshold "
              "becomes its own")
        print("     single instead, and a single's heading shows no condition)")

        twins(cur, pattern, cap)
        odd_conditions(cur, pattern, cap)
        open_draft(inv, cur, pattern)

    return 0


def summarise_every_set(cur):
    """One line per (set, condition), for finding the odd one out."""
    cur.execute(
        """
        SELECT set_name, condition, COUNT(*) AS cards
          FROM manifest
         GROUP BY set_name, condition
         ORDER BY set_name, cards DESC
        """
    )
    rows = cur.fetchall()
    print()
    print("  Every set, by the condition stored on its cards")
    print(f"    {'set':<40} {'condition':<12} {'cards':>6}")
    print(f"    {'-' * 40} {'-' * 12} {'-' * 6}")
    for row in rows:
        print(f"    {str(row['set_name'] or '(none)')[:40]:<40} "
              f"{str(row['condition'] or '(none)'):<12} {row['cards']:>6,}")
    print()
    print(f"  {len(rows)} (set, condition) pair(s). A set appearing on more "
          f"than one line is")
    print("  listed as more than one eBay listing, which is correct when you "
          "really do")
    print("  hold it in two grades -- and a duplicated import when you do not. "
          "Name a")
    print("  set to see which.")


def twins(cur, pattern, cap):
    """
    Cards differing only by condition: the same card catalogued twice.

    Condition is part of the identity, so this is not a data error the
    importer could have prevented -- but it is invisible on the drafts page,
    where the two simply appear as two listings for one set.
    """
    cur.execute(
        """
        SELECT product_name, set_name, printing,
               COUNT(DISTINCT condition) AS grades,
               GROUP_CONCAT(condition || ' [' || manifest_id || ', qty ' ||
                            COALESCE(quantity, 0) || ', ' ||
                            SUBSTR(COALESCE(created_at, '?'), 1, 10) || ']',
                            '  |  ') AS detail
          FROM manifest
         WHERE set_name LIKE ? COLLATE NOCASE
         GROUP BY LOWER(product_name), LOWER(set_name), LOWER(printing)
        HAVING COUNT(DISTINCT condition) > 1
         ORDER BY product_name
        """,
        (pattern,),
    )
    rows = cur.fetchall()

    print()
    if not rows:
        print("  No card in this set is catalogued at more than one condition.")
        return

    print(f"  {len(rows)} card(s) catalogued at more than one condition")
    print("    Each is two rows with two manifest ids, because condition is "
          "part of a")
    print("    card's identity. The older row is the one holding stock and "
          "any eBay")
    print("    link; a later import at a different grade cannot correct it, "
          "only add")
    print("    beside it.")
    print()
    for row in rows[:cap] if cap else rows:
        print(f"    {str(row['product_name'] or '')[:46]}")
        print(f"      {row['detail']}")
    if cap and len(rows) > cap:
        print(f"    ... and {len(rows) - cap} more (--limit 0 for all)")


def odd_conditions(cur, pattern, cap):
    """
    The cards whose condition is the minority spelling in their own set.

    Named rather than counted, because the remedy is per card: the condition
    comes verbatim from the export's ``Condition`` column, so a wrong one is
    corrected in SortSwift and re-exported.
    """
    cur.execute(
        """
        SELECT condition, COUNT(*) AS cards
          FROM manifest
         WHERE set_name LIKE ? COLLATE NOCASE
         GROUP BY condition
         ORDER BY cards DESC
        """,
        (pattern,),
    )
    grades = cur.fetchall()
    if len(grades) < 2:
        return

    majority = grades[0]["condition"]
    cur.execute(
        """
        SELECT manifest_id, product_name, card_number, condition,
               COALESCE(quantity, 0) AS quantity,
               SUBSTR(COALESCE(created_at, '?'), 1, 19) AS catalogued
          FROM manifest
         WHERE set_name LIKE ? COLLATE NOCASE
         ORDER BY condition, card_number
        """,
        (pattern,),
    )
    # Filtered here rather than in SQL: the majority grade can be NULL, and a
    # NULL comparison in SQL would silently drop every row instead of keeping
    # the ones that differ.
    rows = [r for r in cur.fetchall() if r["condition"] != majority]
    if not rows:
        return

    print()
    print(f"  {len(rows)} card(s) not at {majority!r}, the majority grade here")
    print(f"    {'id':<9} {'condition':<12} {'#':<9} {'qty':>4}  "
          f"{'catalogued':<19} card")
    print(f"    {'-' * 9} {'-' * 12} {'-' * 9} {'-' * 4}  {'-' * 19} "
          f"{'-' * 24}")
    for row in rows[:cap] if cap else rows:
        print(f"    {row['manifest_id']:<9} "
              f"{str(row['condition'] or '(none)'):<12} "
              f"{str(row['card_number'] or '-'):<9} {row['quantity']:>4}  "
              f"{row['catalogued']:<19} "
              f"{str(row['product_name'] or '')[:24]}")
    if cap and len(rows) > cap:
        print(f"    ... and {len(rows) - cap} more (--limit 0 for all)")
    print()
    print("    A condition is passed through verbatim from the export's "
          "Condition")
    print("    column and is never inferred, so a wrong one here was wrong in "
          "the")
    print("    file. Fix it in SortSwift, re-export, and re-upload; the card "
          "at the")
    print("    corrected grade is a new row, so the old one has to be zeroed "
          "or left")
    print("    out of the draft.")


def open_draft(inv, cur, pattern):
    """The open draft's groups for this set, as the page would show them."""
    draft = next(
        (p for p in inv.get_plans() if p["status"] == PLAN_DRAFT), None
    )
    print()
    if draft is None:
        print("  There is no open draft, so nothing is being proposed for "
              "these cards.")
        return

    cur.execute(
        """
        SELECT COALESCE(i.group_key, '') AS group_key, COUNT(*) AS cards
          FROM listing_plan_item i
          JOIN manifest m ON m.manifest_id = i.manifest_id
         WHERE i.plan_id = ? AND m.set_name LIKE ? COLLATE NOCASE
         GROUP BY COALESCE(i.group_key, '')
         ORDER BY COALESCE(i.group_key, '')
        """,
        (draft["id"], pattern),
    )
    rows = cur.fetchall()
    if not rows:
        print(f"  Draft {draft['id']} proposes nothing for this set. Every "
              f"card already")
        print("  matches what eBay is known to hold, which is the correct "
              "outcome of an")
        print("  import that changed nothing.")
        return

    print(f"  Draft {draft['id']} proposes these groups for this set, in the "
          f"page's order")
    for row in rows:
        print(f"    {row['cards']:>5,} card(s)  {row['group_key']}")
    print()
    print("    The page orders blocks by this key, so 'LP' sorts above 'NM' "
          "and a")
    print("    one-card LP block appears *above* the large NM one.")


if __name__ == "__main__":
    sys.exit(main())
