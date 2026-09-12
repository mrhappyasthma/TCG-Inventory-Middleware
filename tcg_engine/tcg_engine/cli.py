import argparse
import csv
import os
import sys
from .db import Database
from .orders import process_orders_file
from .batches import process_batch_file
from .sync import sync_active_listings_file, sync_active_listings_csv
from .relink import relink_from_file


def _get_db(db_path: str) -> Database:
    return Database(db_path=db_path)


def handle_init_db(args):
    db = _get_db(args.db)
    print(f"Database initialized successfully at: {os.path.abspath(db.db_path)}")


def handle_orders(args):
    db = _get_db(args.db)
    output_path = args.output or "sortswift_orders.csv"
    print(f"Processing eBay Orders from: {args.input_file}")
    result = process_orders_file(args.input_file, db, output_path)
    for log in result["logs"]:
        print(f"[{log['level']}] {log['message']}")
    print(f"\nGenerated SortSwift Orders file: {output_path} ({result['converted_count']} items converted)")


def handle_batch(args):
    db = _get_db(args.db)

    print(f"Ingesting SortSwift export from: {args.input_file}")
    result = process_batch_file(
        args.input_file, db,
        force=args.force, dry_run=args.dry_run,
    )
    for log in result["logs"]:
        print(f"[{log['level']}] {log['message']}")

    if result.get("duplicate"):
        print(
            "\nBatch was NOT processed because it has been handled before. "
            "Re-run with --force to apply it again."
        )
        return

    # No files are produced. eBay is written to through its API, from an
    # approved draft, so this command updates the catalogue and the
    # dashboard is where the resulting draft is reviewed and pushed.
    print(
        f"\nCatalogued {result['parsed_rows']} card(s), "
        f"{result['new_catalog_count']} new, "
        f"{result['staged_card_count']} staged for eBay."
    )
    print("Review and push the draft in the dashboard.")


def handle_sync(args):
    db = _get_db(args.db)
    print(f"Syncing eBay Active Listings from: {args.input_file}")
    result = sync_active_listings_file(args.input_file, db)
    for log in result["logs"]:
        print(f"[{log['level']}] {log['message']}")
    print(f"\nStore State Sync complete: {result['synced_count']} variations updated.")


def handle_export_manifest(args):
    db = _get_db(args.db)
    output_path = args.output or "master_manifest.csv"
    items = db.export_all_manifest()
    fieldnames = [
        "manifest_id",
        "product_name",
        "card_number",
        "set_name",
        "condition",
        "printing",
        "quantity",
        "ebay_parent_id",
        "last_known_qty",
    ]
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(items)
    print(f"Exported {len(items)} catalog records to: {output_path}")


def handle_relink(args):
    """Realign catalog manifest IDs to the labels on live eBay listings."""
    db = _get_db(args.db)
    print(f"Relinking catalog against: {args.input_file}")
    result = relink_from_file(args.input_file, db)
    for log in result["logs"]:
        print(f"[{log['level']}] {log['message']}")

    if not args.no_sync:
        print()
        print("Running Module B sync with the realigned IDs...")
        sync = sync_active_listings_file(args.input_file, db)
        for log in sync["logs"]:
            if log["level"] != "SUCCESS":
                print(f"[{log['level']}] {log['message']}")
        print()
        print(
            f"Synced {sync['synced_count']} variation(s) across "
            f"{sync.get('linked_listing_count', 0)} listing(s)."
        )


def handle_purge(args):
    db = _get_db(args.db)
    stats = db.get_stats()

    if not args.yes:
        print("This will permanently delete:")
        print(f"  - {stats['total_cards']} master catalog cards")
        print(f"  - {stats['active_listings']} live eBay listing links")
        print("  - all processed-batch fingerprints")
        print()
        print("Your pricing rules and listing settings (postal code, business")
        print("policies, templates) are NOT touched.")
        print()
        print("Nothing was deleted. Re-run with --yes to confirm:")
        print(f"  python -m tcg_engine.cli purge --yes --db {args.db}")
        return

    counts = db.purge_inventory()
    print("Purged:")
    print(f"  manifest           {counts['manifest']} rows")
    print(f"  ebay_variations    {counts['ebay_variations']} rows")
    print(f"  processed_batches  {counts['processed_batches']} rows")
    print()
    print("Pricing rules and listing settings were preserved.")
    print("You can now re-upload your batches from a clean slate.")


def handle_status(args):
    db = _get_db(args.db)
    stats = db.get_stats()
    print("=== TCG Inventory Store Status ===")
    print(f"Database Path:         {os.path.abspath(db.db_path)}")
    print(f"Total Master Cards:    {stats['total_cards']}")
    print(f"Active eBay Listings:  {stats['active_listings']}")
    print(f"Total Live Stock Qty:  {stats['total_stock']}")


def main():
    parser = argparse.ArgumentParser(
        prog="tcg-engine",
        description="TCG Card Inventory Middleware: SortSwift <-> eBay CSV Bridge",
    )
    default_db = os.environ.get("DATABASE_URL", "data/inventory.db")

    # --db is declared on a shared parent rather than the top-level parser so
    # that it can be written AFTER the subcommand, which is how every documented
    # example reads: "cli batch file.csv --db data/inventory.db". An argparse
    # option on the top-level parser only accepts the prefix position.
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument(
        "--db",
        default=default_db,
        help=f"Path to SQLite database (default: {default_db})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # init-db
    p_init = subparsers.add_parser(
        "init-db",
        parents=[db_parent], help="Initialize SQLite database schema")
    p_init.set_defaults(func=handle_init_db)

    # status
    p_status = subparsers.add_parser(
        "status",
        parents=[db_parent], help="Show inventory statistics")
    p_status.set_defaults(func=handle_status)

    # orders (Module C)
    p_orders = subparsers.add_parser(
        "orders",
        parents=[db_parent], help="Module C: Convert eBay Orders CSV to SortSwift Orders Import CSV"
    )
    p_orders.add_argument("input_file", help="Path to raw eBay orders CSV")
    p_orders.add_argument("-o", "--output", help="Output path for SortSwift import CSV")
    p_orders.set_defaults(func=handle_orders)

    # batch (Module A)
    p_batch = subparsers.add_parser(
        "batch",
        parents=[db_parent],
        help="Module A: ingest a SortSwift export and stage a draft"
    )
    p_batch.add_argument("input_file", help="Path to SortSwift batch CSV")
    p_batch.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what the file would change without writing anything",
    )
    p_batch.add_argument(
        "--force",
        action="store_true",
        help="Re-process a file that has already been applied",
    )
    p_batch.set_defaults(func=handle_batch)

    # sync (Module B)
    p_sync = subparsers.add_parser(
        "sync",
        parents=[db_parent], help="Module B: Sync active eBay listings report into store mirror database"
    )
    p_sync.add_argument("input_file", help="Path to eBay Active Listings report CSV")
    p_sync.set_defaults(func=handle_sync)

    # relink
    p_relink = subparsers.add_parser(
        "relink",
        parents=[db_parent],
        help="Realign catalog manifest IDs to the Custom Labels on live eBay listings",
    )
    p_relink.add_argument("input_file", help="Path to eBay Active Listings report CSV")
    p_relink.add_argument(
        "--no-sync",
        action="store_true",
        help="Only realign the IDs; do not run the Module B sync afterwards",
    )
    p_relink.set_defaults(func=handle_relink)

    # purge
    p_purge = subparsers.add_parser(
        "purge",
        parents=[db_parent],
        help="Delete the catalog, store mirror and batch fingerprints (keeps settings)",
    )
    p_purge.add_argument(
        "--yes",
        action="store_true",
        help="Actually perform the deletion. Without it, only a summary is shown.",
    )
    p_purge.set_defaults(func=handle_purge)

    # export-manifest
    p_export = subparsers.add_parser(
        "export-manifest",
        parents=[db_parent], help="Export full master catalog to CSV")
    p_export.add_argument("-o", "--output", help="Output path for exported CSV")
    p_export.set_defaults(func=handle_export_manifest)

    parsed_args = parser.parse_args()
    parsed_args.func(parsed_args)


if __name__ == "__main__":
    main()
