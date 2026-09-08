# TCG Engine (`tcg-engine`)

A lightweight, standalone Python library and CLI tool for TCG inventory state management and bidirectional CSV conversions between **SortSwift** (TCGplayer schema) and **eBay Seller Hub Reports**.

## Features

- **Master Catalog (`manifest`)**: Maps card attributes (`product_name`, `set_name`, `condition`, `printing`) to un-truncatable sequential IDs (`ID1001`, `ID1002`, ...), bypassing eBay's 50-character SKU limit.
- **Live Store Mirror (`ebay_variations`)**: Tracks live eBay parent listing IDs (`ebay_parent_id`) and active quantities (`last_known_qty`).
- **Module A (Downstream Orders)**: Converts raw eBay orders reports into clean SortSwift / TCGplayer import CSVs (`Order Number, Product Name, Set Name, Condition, Printing, Quantity`).
- **Module B (Upstream Batches)**: Automatically ingests SortSwift batches, assigns manifest IDs, and routes cards into `ebay_inventory_updates.csv` (Revise) or `ebay_new_additions.csv` (Add). Pricing and set-grouping behaviour come from the `pricing_rules` and `listing_settings` tables, which are the source of truth. Batch quantities are additive, so each processed file is fingerprinted and a duplicate is refused unless `force=True`.
- **Module C (Store State Sync)**: Ingests eBay Active Listings reports and synchronizes `ebay_variations` store state.
- **Standalone CLI**: Can be run from scripts, scheduled cron jobs, or terminals with zero external web dependencies.

## Installation

```bash
# Editable install during development
pip install -e ./tcg_engine

# Or install from git
pip install git+https://github.com/<your-username>/tcg-engine.git
```

## CLI Usage

```bash
# Initialize SQLite database
python -m tcg_engine.cli init-db --db data/inventory.db

# Module A: Convert eBay Orders to SortSwift Import CSV
python -m tcg_engine.cli orders sample_ebay_orders.csv -o sortswift_orders.csv --db data/inventory.db

# Module B: Route SortSwift Batch to Add / Revise CSVs
python -m tcg_engine.cli batch sortswift_batch.csv --out-dir ./output --db data/inventory.db

# Re-apply a batch already processed (adds its quantities a second time)
python -m tcg_engine.cli batch sortswift_batch.csv --out-dir ./output --force --db data/inventory.db

# Module C: Sync eBay Active Listings report into database
python -m tcg_engine.cli sync ebay_active_listings.csv --db data/inventory.db

# Export Master Catalog to CSV
python -m tcg_engine.cli export-manifest -o master_manifest.csv --db data/inventory.db
```
