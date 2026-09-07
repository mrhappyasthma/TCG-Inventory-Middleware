import argparse
import csv
import os
import sys
from .db import Database
from .orders import process_orders_file
from .batches import process_batch_file
from .sync import sync_active_listings_file


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
    out_dir = args.out_dir or "."
    os.makedirs(out_dir, exist_ok=True)
    revise_path = os.path.join(out_dir, "ebay_inventory_updates.csv")
    add_path = os.path.join(out_dir, "ebay_new_additions.csv")

    print(f"Processing SortSwift Batch from: {args.input_file}")
    result = process_batch_file(args.input_file, db, revise_path, add_path)
    for log in result["logs"]:
        print(f"[{log['level']}] {log['message']}")

    if result["revise_count"] > 0:
        print(f"Generated Revise CSV: {revise_path} ({result['revise_count']} items)")
    if result["add_count"] > 0:
        print(f"Generated Add CSV: {add_path} ({result['add_count']} items)")


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
        "set_name",
        "condition",
        "printing",
        "ebay_parent_id",
        "last_known_qty",
    ]
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(items)
    print(f"Exported {len(items)} catalog records to: {output_path}")


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
    parser.add_argument(
        "--db",
        default=default_db,
        help=f"Path to SQLite database (default: {default_db})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # init-db
    p_init = subparsers.add_parser("init-db", help="Initialize SQLite database schema")
    p_init.set_defaults(func=handle_init_db)

    # status
    p_status = subparsers.add_parser("status", help="Show inventory statistics")
    p_status.set_defaults(func=handle_status)

    # orders (Module A)
    p_orders = subparsers.add_parser(
        "orders", help="Module A: Convert eBay Orders CSV to SortSwift Orders Import CSV"
    )
    p_orders.add_argument("input_file", help="Path to raw eBay orders CSV")
    p_orders.add_argument("-o", "--output", help="Output path for SortSwift import CSV")
    p_orders.set_defaults(func=handle_orders)

    # batch (Module B)
    p_batch = subparsers.add_parser(
        "batch", help="Module B: Route SortSwift Scan Batch to Add vs. Revise eBay CSVs"
    )
    p_batch.add_argument("input_file", help="Path to SortSwift batch CSV")
    p_batch.add_argument("--out-dir", default=".", help="Directory to save generated CSVs")
    p_batch.set_defaults(func=handle_batch)

    # sync (Module C)
    p_sync = subparsers.add_parser(
        "sync", help="Module C: Sync active eBay listings report into store mirror database"
    )
    p_sync.add_argument("input_file", help="Path to eBay Active Listings report CSV")
    p_sync.set_defaults(func=handle_sync)

    # export-manifest
    p_export = subparsers.add_parser("export-manifest", help="Export full master catalog to CSV")
    p_export.add_argument("-o", "--output", help="Output path for exported CSV")
    p_export.set_defaults(func=handle_export_manifest)

    parsed_args = parser.parse_args()
    parsed_args.func(parsed_args)


if __name__ == "__main__":
    main()
