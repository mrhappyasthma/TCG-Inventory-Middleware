#!/usr/bin/env python3
"""
Delete old draft plans, keeping the recent ones.

The drafts page accumulates. Every Module A upload builds a plan, every
rebuild replaces the draft, and the approved-but-never-pushed ones pile up
behind whatever is current -- so after a few weeks of use the page is a list
of decisions nobody is going to act on, with the one that matters at the top.
This empties the back of it.

Deliberately a script and not a button. It is irreversible, it is a one-time
tidy rather than routine housekeeping, and -- unlike the per-plan delete on
the drafts page -- it will remove a **pushed** plan, which that endpoint
refuses on purpose:

    a plan that was pushed is the only record of who authorised a live
    change and what it did, so it stays

That reasoning is still right, and nothing here weakens it for the app. But
the store's own history lives on eBay and in ``ebay_variations``; a pushed
plan is the paper trail for how a change was decided, not the change itself.
Throwing away the trail for plans one to nine is a judgement the owner of the
data is entitled to make, so the script does it -- loudly, naming every
pushed plan it is about to erase, and only when asked twice.

Usage, from the repository root or inside the container:

    python scripts/purge_old_plans.py --before 10          # dry run
    python scripts/purge_old_plans.py --before 10 --yes    # actually delete

On the NAS, where the databases live:

    cd /volume1/docker/tcg-middleware
    docker compose exec tcg-middleware python scripts/purge_old_plans.py --before 10

Add ``--keep-pushed`` to spare the plans that reached eBay and clear only the
drafts and abandoned approvals.

A dry run is the default and writes nothing. A real run takes a full snapshot
of the database first and prints its path, because there is no undo.

Touches eBay not at all.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

from tcg_engine.db import Database  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "data/inventory.db")

# A pushed plan reached eBay. Naming these separately is the whole point of
# the dry run: they are the ones whose loss is not recoverable from anywhere
# else in this application.
PUSHED_STATUS = "pushed"


def plan_summary(db):
    """
    Every plan in the database, oldest first, with its row counts.

    Read straight from the tables rather than through ``get_plans``, which
    scopes to one user and caps the count -- neither of which is wanted here.
    A purge has to see everything it is about to delete, including plans
    belonging to another account.
    """
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT p.id, p.user_id, p.status, p.source, p.note,
                   p.created_at, p.pushed_at,
                   (SELECT COUNT(*) FROM listing_plan_item i
                     WHERE i.plan_id = p.id) AS item_count,
                   (SELECT COUNT(*) FROM listing_plan_item i
                     WHERE i.plan_id = p.id AND i.status = 'pushed')
                       AS pushed_item_count,
                   (SELECT COUNT(*) FROM listing_plan_group g
                     WHERE g.plan_id = p.id) AS group_count
            FROM listing_plan p
            ORDER BY p.id
            """
        )
        return [dict(row) for row in cursor.fetchall()]


def select_plans(plans, before, keep_pushed=False):
    """
    Split the plans into the ones to delete and the ones to keep.

    ``before`` is exclusive: ``--before 10`` keeps plan 10. Stated that way
    because "purge everything before plan 10" is how the request is phrased,
    and an off-by-one here deletes a plan the user meant to keep.
    """
    doomed = []
    kept = []
    for plan in plans:
        if int(plan["id"]) >= int(before):
            kept.append(plan)
        elif keep_pushed and plan["status"] == PUSHED_STATUS:
            kept.append(plan)
        else:
            doomed.append(plan)
    return doomed, kept


def describe(plan):
    """One line for one plan, with the numbers that make its loss concrete."""
    parts = [
        f"plan {plan['id']:>4}",
        f"{str(plan['status'] or ''):<9}",
        f"{plan['item_count']:>4} item(s)",
    ]
    if plan["pushed_item_count"]:
        parts.append(f"{plan['pushed_item_count']} pushed")
    if plan["group_count"]:
        parts.append(f"{plan['group_count']} cover choice(s)")
    parts.append(f"created {plan['created_at']}")
    if plan["pushed_at"]:
        parts.append(f"pushed {plan['pushed_at']}")
    note = str(plan["note"] or "").strip()
    if note:
        parts.append(f"note: {note[:48]}")
    return "  " + "  ".join(parts)


def snapshot_path(db_path):
    """Where the pre-purge snapshot goes: beside the database, timestamped."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.basename(db_path) or "inventory.db"
    root, _ = os.path.splitext(base)
    return os.path.join(
        os.path.dirname(os.path.abspath(db_path)),
        f"{root}-before-plan-purge-{stamp}.db",
    )


def purge(db, doomed):
    """
    Delete the chosen plans and report what went with them.

    Items and cover choices go by ``ON DELETE CASCADE`` -- foreign keys are
    enabled on every connection -- but the counts are totalled from what was
    read beforehand and then verified, because a cascade that silently did
    not fire would leave orphaned rows pointing at a plan that no longer
    exists, and nothing would say so.
    """
    items = sum(int(p["item_count"]) for p in doomed)
    groups = sum(int(p["group_count"]) for p in doomed)
    deleted = 0
    for plan in doomed:
        if db.delete_plan(int(plan["id"])):
            deleted += 1

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM listing_plan_item
                  WHERE plan_id NOT IN (SELECT id FROM listing_plan))
                    AS orphan_items,
                (SELECT COUNT(*) FROM listing_plan_group
                  WHERE plan_id NOT IN (SELECT id FROM listing_plan))
                    AS orphan_groups
            """
        )
        orphans = dict(cursor.fetchone())

    return {
        "plans": deleted,
        "items": items,
        "groups": groups,
        "orphan_items": int(orphans["orphan_items"]),
        "orphan_groups": int(orphans["orphan_groups"]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Delete plans older than a cutoff.",
    )
    parser.add_argument(
        "--before", type=int, required=True, metavar="PLAN_ID",
        help=(
            "Delete plans whose id is below this. Exclusive: --before 10 "
            "keeps plan 10."
        ),
    )
    parser.add_argument(
        "--keep-pushed", action="store_true",
        help=(
            "Spare plans that reached eBay, clearing only drafts and "
            "approvals nothing acted on."
        ),
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Actually delete. Without it this is a dry run.",
    )
    parser.add_argument(
        "--db", default=DATABASE_URL,
        help=f"Path to the inventory database (default {DATABASE_URL}).",
    )
    args = parser.parse_args(argv)

    if args.before < 1:
        print("--before must be a plan id of 1 or more.")
        return 2
    if not os.path.exists(args.db):
        print(f"No database at {args.db}. Set DATABASE_URL or pass --db.")
        return 2

    db = Database(args.db)
    plans = plan_summary(db)
    if not plans:
        print("There are no plans in this database. Nothing to do.")
        return 0

    doomed, kept = select_plans(plans, args.before, args.keep_pushed)

    print(f"\n{args.db}: {len(plans)} plan(s), ids "
          f"{plans[0]['id']} to {plans[-1]['id']}.\n")

    if not doomed:
        print(f"Nothing is below plan {args.before}. Nothing to do.")
        return 0

    # Tense matters here more than it looks: the two runs print the same
    # list, and "would" on a run that actually deleted would read as though
    # nothing had happened.
    verb = "Deleting" if args.yes else "Would delete"
    print(f"{verb} {len(doomed)} plan(s) below plan {args.before}:")
    for plan in doomed:
        print(describe(plan))

    pushed = [p for p in doomed if p["status"] == PUSHED_STATUS]
    if pushed:
        print(
            f"\n  !! {len(pushed)} of those reached eBay: "
            + ", ".join(str(p["id"]) for p in pushed)
        )
        print(
            "     Those plans are this application's only record of who "
            "authorised\n"
            "     each live change and what it did. The listings themselves "
            "are not\n"
            "     affected -- they live on eBay and in our mirror of it -- "
            "but the\n"
            "     trail of how they were decided goes. Use --keep-pushed to "
            "spare them."
        )

    print(f"\n{'Keeping' if args.yes else 'Would keep'} {len(kept)} plan(s): "
          + (", ".join(str(p["id"]) for p in kept) or "none"))

    if not args.yes:
        print(
            "\nDry run -- nothing was deleted. Re-run with --yes to do it."
        )
        return 0

    dest = snapshot_path(args.db)
    print(f"\nSnapshotting the database first: {dest}")
    db.export_snapshot(dest)

    result = purge(db, doomed)
    print(
        f"Deleted {result['plans']} plan(s), {result['items']} item(s) and "
        f"{result['groups']} cover choice(s)."
    )
    if result["orphan_items"] or result["orphan_groups"]:
        print(
            f"  !! {result['orphan_items']} orphaned item(s) and "
            f"{result['orphan_groups']} orphaned cover choice(s) are left "
            f"pointing at plans that no longer exist. The cascade did not "
            f"fire; restore from the snapshot above and investigate."
        )
        return 1
    print("The drafts page will show only the plans that were kept.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
