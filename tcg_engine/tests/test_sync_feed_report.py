"""
The store-mirror sync must accept both report shapes.

Two different reports carry the same facts under different column names. The
Seller Hub **Active Listings** report, downloaded through a browser, uses
human labels: "Item number", "Custom label (SKU)", "Available quantity",
"Current price". The Feed API's **LMS_ACTIVE_INVENTORY_REPORT**, fetched
without anyone touching Seller Hub, uses field names: ItemID, SKU, Quantity,
Price.

Both go through the one parser deliberately. Every rule in it -- skipping
variation parent rows, resolving the manifest id out of a bin-suffixed custom
label, and above all zeroing cards absent from the report -- would otherwise
need reimplementing for the second shape, and the two copies could drift. A
drift in the zeroing rule in particular would either leave sold-out cards on
sale or delist a live store.
"""

import os
import tempfile
import unittest

from tcg_engine.db import Database
from tcg_engine.sync import sync_active_listings_csv

# The Feed API's shape. Header names taken from eBay's ActiveInventoryReport
# field list: ItemID, SKU, Price, Quantity.
FEED_REPORT = (
    "ItemID,SKU,Price,Quantity\n"
    "110111222333,ID1001-Bin_A12,2.50,4\n"
    "110111222333,ID1002-Bin_B03,1.25,2\n"
)

# The Seller Hub shape, for comparison. Same two cards, same numbers.
SELLER_HUB_REPORT = (
    "Item number,Custom label (SKU),Available quantity,Current price\n"
    "110111222333,ID1001-Bin_A12,4,2.50\n"
    "110111222333,ID1002-Bin_B03,2,1.25\n"
)


class FeedReportCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "sync.db"))
        self.db.insert_manifest("ID1001", "Charizard", "Base Set", "NM", "Holofoil")
        self.db.insert_manifest("ID1002", "Blastoise", "Base Set", "NM", "Normal")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_the_feed_report_syncs(self):
        result = sync_active_listings_csv(FEED_REPORT, self.db)
        self.assertEqual(result["synced_count"], 2)

        first = self.db.get_variation("ID1001")
        self.assertEqual(first["ebay_parent_id"], "110111222333")
        self.assertEqual(first["last_known_qty"], 4)
        self.assertAlmostEqual(first["last_known_price"], 2.50)
        # The label eBay knows, bin suffix and all. It cannot be rebuilt from
        # a card's identity, so it has to be learned here.
        self.assertEqual(first["custom_label"], "ID1001-Bin_A12")

    def test_both_shapes_produce_the_same_mirror(self):
        sync_active_listings_csv(FEED_REPORT, self.db)
        from_feed = {
            mid: dict(self.db.get_variation(mid)) for mid in ("ID1001", "ID1002")
        }

        other = Database(os.path.join(self.temp_dir.name, "sync2.db"))
        other.insert_manifest("ID1001", "Charizard", "Base Set", "NM", "Holofoil")
        other.insert_manifest("ID1002", "Blastoise", "Base Set", "NM", "Normal")
        sync_active_listings_csv(SELLER_HUB_REPORT, other)
        from_hub = {mid: dict(other.get_variation(mid)) for mid in ("ID1001", "ID1002")}

        for manifest_id in ("ID1001", "ID1002"):
            for field in (
                "ebay_parent_id",
                "custom_label",
                "last_known_qty",
                "last_known_price",
            ):
                self.assertEqual(
                    from_feed[manifest_id][field],
                    from_hub[manifest_id][field],
                    f"{manifest_id}.{field} differs between report shapes",
                )

    def test_a_card_absent_from_the_feed_report_is_zeroed(self):
        # The rule that matters most, and the reason there is one parser: a
        # card linked to a listing but missing from the report is not live any
        # more, and leaving its quantity in place keeps reporting stock eBay
        # does not have.
        sync_active_listings_csv(FEED_REPORT, self.db)
        self.assertEqual(self.db.get_variation("ID1002")["last_known_qty"], 2)

        shrunk = "ItemID,SKU,Price,Quantity\n110111222333,ID1001-Bin_A12,2.50,4\n"
        result = sync_active_listings_csv(shrunk, self.db)
        self.assertEqual(result["delisted_count"], 1)
        self.assertEqual(self.db.get_variation("ID1002")["last_known_qty"], 0)

    def test_an_unknown_sku_is_reported_rather_than_guessed(self):
        report = "ItemID,SKU,Price,Quantity\n110111222333,ID9999-Bin_Z01,1.00,1\n"
        result = sync_active_listings_csv(report, self.db)
        self.assertEqual(result["synced_count"], 0)
        self.assertEqual(result["skipped_unmapped_count"], 1)

    def test_an_explicit_available_quantity_wins_over_a_bare_one(self):
        # A report carrying both must not have the ambiguous column win.
        report = (
            "ItemID,SKU,Quantity,Available quantity,Price\n"
            "110111222333,ID1001-Bin_A12,99,4,2.50\n"
        )
        sync_active_listings_csv(report, self.db)
        self.assertEqual(self.db.get_variation("ID1001")["last_known_qty"], 4)


if __name__ == "__main__":
    unittest.main()
