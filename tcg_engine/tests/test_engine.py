import csv
import io
import os
import sqlite3
import tempfile
import unittest
from tcg_engine.db import (SHARED_SCOPE, Database, apply_pricing_rules,
                          apply_condition_multiplier,
                          normalize_condition_key)
from tcg_engine.batches import (
    CONDITION_CHOICES,
    build_variation_option_name,
    condition_is_mappable,
    process_batch_csv,
    variation_sort_key,
)
from tcg_engine.sync import sync_active_listings_csv
from tcg_engine.relink import relink_from_active_listings, parse_option_name
from tcg_engine.pricing_feed import refresh_market_prices, PriceFeedError


class TestTCGEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_inventory.db")
        self.db = Database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_database_id_generation_and_upsert(self):
        # Initial ID should be ID1001
        id1 = self.db.get_next_manifest_id()
        self.assertEqual(id1, "ID1001")

        # Insert first card
        card1 = self.db.insert_manifest(
            "ID1001", "Charizard", "Base Set", "Near Mint", "Holofoil"
        )
        self.assertEqual(card1["manifest_id"], "ID1001")

        # Next ID should be ID1002
        id2 = self.db.get_next_manifest_id()
        self.assertEqual(id2, "ID1002")

        # Get or create with existing card should return existing ID1001
        m_id, is_new, _ = self.db.get_or_create_manifest(
            "Charizard", "Base Set", "Near Mint", "Holofoil"
        )
        self.assertEqual(m_id, "ID1001")
        self.assertFalse(is_new)

        # Get or create with new card should create ID1002
        m_id2, is_new2, _ = self.db.get_or_create_manifest(
            "Blastoise", "Base Set", "Lightly Played", "Normal"
        )
        self.assertEqual(m_id2, "ID1002")
        self.assertTrue(is_new2)

        # Variation upsert
        self.db.upsert_variation("ID1001", "123456789012", 5)
        var = self.db.get_variation("ID1001")
        self.assertIsNotNone(var)
        self.assertEqual(var["ebay_parent_id"], "123456789012")
        self.assertEqual(var["last_known_qty"], 5)

        # Joined inventory query
        items = self.db.get_inventory()
        self.assertEqual(len(items), 2)
        stats = self.db.get_stats()
        self.assertEqual(stats["total_cards"], 2)
        self.assertEqual(stats["active_listings"], 1)
        self.assertEqual(stats["total_stock"], 5)

    def test_variation_title_generation(self):
        from tcg_engine.batches import generate_variation_title

        # The condition is substituted verbatim from the source data.
        t1 = generate_variation_title("SV05: Temporal Forces", condition="Near Mint")
        self.assertEqual(
            t1, "SV05: Temporal Forces: Pick Your Card - Near Mint - Complete Your Set"
        )
        self.assertLessEqual(len(t1), 80)

        # A short condition code passes through unchanged too.
        t2 = generate_variation_title("SV05: Temporal Forces", condition="NM")
        self.assertEqual(
            t2, "SV05: Temporal Forces: Pick Your Card - NM - Complete Your Set"
        )

        # An over-long title is brought under 80 by trimming the SET NAME; the
        # condition is never abbreviated because it is a factual claim.
        long_set = "Yu-Gi-Oh! 25th Anniversary Rarity Collection II Expansion Set"
        t3 = generate_variation_title(long_set, condition="Lightly Played")
        self.assertLessEqual(len(t3), 80)
        self.assertIn("Lightly Played", t3)

        # A legacy template that hardcodes the condition still renders.
        t4 = generate_variation_title(
            "Base Set",
            condition="Near Mint",
            template="{set_name}: Pick Your Card - Near Mint - Complete Your Set",
        )
        self.assertLessEqual(len(t4), 80)
        self.assertIn("Base Set", t4)

    def test_sortswift_real_sample(self):
        # Test with the exact user SortSwift inventory format
        real_sample = """"Stock Item ID","Game","File Name","Set","Set Code","Card Number","Name","Rarity","Market Price","Low Price","Mid Price","High Price","EU Price","Condition","Language","Printing","Quantity","Comment","Remarks","TCGplayer Id","SKU Id","ID Product","UPC","CDN Image","Card Back CDN Image","Cost","Price","TCGPlayer Price","Shopify Price","Cardtrader Price","Manapool Price","Misprint Price","eBay Price","Square Price","*ConditionID"
"6a9f37cd1ffb1c868bf60ba0","Pokemon","","SV05: Temporal Forces","TEF","016/162","Deerling - 016/162","Common","0.17","0.01","0.17","19.98","","NM","EN","Normal",1,"","No Remark",542678,7805758,"760646","","https://cdn.example.com/deerling.jpg","https://cdn.example.com/back.jpg","0.00","0.15","","","","","","","","4000"
"6a9f37cd1ffb1c868bf60ba3","Pokemon","","SV05: Temporal Forces","TEF","018/162","Grubbin","Common","0.13","0.01","0.15","2.99","","NM","EN","Normal",2,"","No Remark",542763,7806223,"760648","","https://cdn.example.com/grubbin.jpg","","0.00","0.14","","","","","","","","4000"
"""
        res = process_batch_csv(real_sample, self.db)
        self.assertEqual(res["staged_card_count"], 2)
        self.assertEqual(res["new_catalog_count"], 2)

        # Check that SKU Id, TCGplayer Id, CDN image, and calculated price were cataloged
        card1 = self.db.get_manifest_by_id("ID1001")
        self.assertEqual(card1["product_name"], "Deerling - 016/162")
        self.assertEqual(card1["sku_id"], "7805758")
        self.assertEqual(card1["tcgplayer_id"], "542678")
        self.assertEqual(card1["price"], 1.99)  # Calculated via pricing rule: <0.25 -> 1.99
        self.assertEqual(card1["market_price"], 0.17)
        self.assertEqual(card1["cdn_image"], "https://cdn.example.com/deerling.jpg")

    def test_bin_remark_encoding_and_reexport(self):
        # Initial export with Remarks "Bin A-12"
        batch_1 = """"Stock Item ID","Game","File Name","Set","Set Code","Card Number","Name","Rarity","Market Price","Low Price","Mid Price","High Price","EU Price","Condition","Language","Printing","Quantity","Comment","Remarks","TCGplayer Id","SKU Id","ID Product","UPC","CDN Image","Card Back CDN Image","Cost","Price","TCGPlayer Price","Shopify Price","Cardtrader Price","Manapool Price","Misprint Price","eBay Price","Square Price","*ConditionID"
"6a9f37cd1ffb1c868bf60ba0","Pokemon","","SV05: Temporal Forces","TEF","016/162","Deerling - 016/162","Common","0.17","0.01","0.17","19.98","","NM","EN","Normal",1,"","Bin A-12",542678,7805758,"760646","","https://cdn.example.com/deerling.jpg","","0.00","0.15","","","","","","","","4000"
"""
        res1 = process_batch_csv(batch_1, self.db)
        self.assertEqual(res1["staged_card_count"], 1)

        # The remark is stored against the card, which is what the
        # order side decodes a bin from later in this test.
        #
        # Note the SKU itself no longer carries it for a *new* listing:
        # the encoding was applied when building the Add file, and the
        # push uses the stored custom_label or the bare manifest id.
        # Whether the bin belongs in the SKU at all is the open question
        # in docs/ebay-api-design.md section 10.

        # Check that remark is saved in DB
        card = self.db.get_manifest_by_id("ID1001")
        self.assertEqual(card["remarks"], "Bin A-12")

        # Simulate eBay Active Listings Sync with that SKU
        sync_csv = "Item number,Title,Custom label (SKU),Available quantity\n998877665544,Deerling,ID1001-Bin_A-12,3\n"
        sync_res = sync_active_listings_csv(sync_csv, self.db)
        self.assertEqual(sync_res["synced_count"], 1)

        # Re-exporting inventory with new stock of Deerling (+2) and a brand new card (Pikachu)
        batch_2 = """"Stock Item ID","Game","File Name","Set","Set Code","Card Number","Name","Rarity","Market Price","Low Price","Mid Price","High Price","EU Price","Condition","Language","Printing","Quantity","Comment","Remarks","TCGplayer Id","SKU Id","ID Product","UPC","CDN Image","Card Back CDN Image","Cost","Price","TCGPlayer Price","Shopify Price","Cardtrader Price","Manapool Price","Misprint Price","eBay Price","Square Price","*ConditionID"
"6a9f37cd1ffb1c868bf60ba0","Pokemon","","SV05: Temporal Forces","TEF","016/162","Deerling - 016/162","Common","0.17","0.01","0.17","19.98","","NM","EN","Normal",2,"","Bin A-12",542678,7805758,"760646","","https://cdn.example.com/deerling.jpg","","0.00","0.15","","","","","","","","4000"
"6a9f37cd1ffb1c868bf60ba1","Pokemon","","Base Set","BS","058/102","Pikachu","Common","2.50","0.01","2.50","19.98","","NM","EN","Normal",1,"","Box 4",12345,67890,"12345","","https://cdn.example.com/pikachu.jpg","","0.00","2.50","","","","","","","","4000"
"""
        res2 = process_batch_csv(batch_2, self.db)
        # Deerling is live on eBay already, so only Pikachu is newly staged.
        self.assertEqual(res2["staged_card_count"], 1)
        self.assertEqual(res2["new_catalog_count"], 1)


    def test_pricing_rules_engine(self):
        # Default rules:
        # < 0.25 -> $1.99
        # 0.25 - 0.50 -> $2.49
        # 0.50 - 1.00 -> $2.99
        # >= 1.00 -> Market + $3.00
        p1, r1 = self.db.calculate_price(0.17)
        self.assertEqual(p1, 1.99)

        p2, r2 = self.db.calculate_price(0.35)
        self.assertEqual(p2, 2.49)

        p3, r3 = self.db.calculate_price(0.75)
        self.assertEqual(p3, 2.99)

        p4, r4 = self.db.calculate_price(4.50)
        self.assertEqual(p4, 7.50)  # 4.50 + 3.00

        # Test custom rules update
        custom_rules = [
            {"min_price": 0.0, "max_price": 1.0, "rule_type": "fixed", "rule_value": 0.99, "sort_order": 1},
            {"min_price": 1.0, "max_price": None, "rule_type": "markup_percent", "rule_value": 20.0, "sort_order": 2},
        ]
        self.db.set_pricing_rules(custom_rules)
        p5, _ = self.db.calculate_price(0.50)
        self.assertEqual(p5, 0.99)
        p6, _ = self.db.calculate_price(10.00)
        self.assertEqual(p6, 12.00)  # 10 + 20%

    def test_module_c_active_listings_sync(self):
        # Insert cards in manifest
        self.db.insert_manifest("ID1001", "Venusaur", "Base Set", "Near Mint", "Holofoil")
        self.db.insert_manifest("ID1002", "Zapdos", "Fossil", "Near Mint", "Holofoil")

        # Active listings report with parent container row and child variation rows
        sample_active_listings = """Item number,Title,Custom label (SKU),Available quantity
112233445566,Pokemon Base Set Singles Parent Container,,10
112233445566,Venusaur Holo,ID1001,4
998877665544,Pokemon Fossil Zapdos Single Listing,ID1002,2
"""
        res = sync_active_listings_csv(sample_active_listings, self.db)
        self.assertEqual(res["synced_count"], 2)
        # Exactly one row was skipped. Which bucket it lands in depends on
        # whether the report carries a "Variation details" column: with one, a
        # blank label is identifiably a variation container; without one it is
        # indistinguishable from an ordinary listing that has no SKU, and the
        # safer of the two labels is used.
        self.assertEqual(
            res["skipped_parent_count"] + res["skipped_unlabelled_count"], 1
        )

        # Check that DB was updated
        var1 = self.db.get_variation("ID1001")
        self.assertEqual(var1["ebay_parent_id"], "112233445566")
        self.assertEqual(var1["last_known_qty"], 4)

        var2 = self.db.get_variation("ID1002")
        self.assertEqual(var2["ebay_parent_id"], "998877665544")
        self.assertEqual(var2["last_known_qty"], 2)

    # ------------------------------------------------------------------
    # Regression coverage for the correctness fixes
    # ------------------------------------------------------------------

    TWO_CARD_BATCH = """"Set","Set Code","Card Number","Name","Market Price","Condition","Language","Printing","Quantity","Remarks","TCGplayer Id","SKU Id","CDN Image","Price","*ConditionID"
"SV05: Temporal Forces","TEF","016/162","Deerling","0.17","NM","EN","Normal",1,"Bin-1",542678,7805758,"https://cdn.example.com/a.jpg","0.15","4000"
"SV05: Temporal Forces","TEF","001/162","Iron Leaves ex","4.50","NM","EN","Normal",1,"Bin-2",542679,7805759,"https://cdn.example.com/b.jpg","4.50","4000"
"""

    def test_duplicate_batch_is_refused_unless_forced(self):
        first = process_batch_csv(self.TWO_CARD_BATCH, self.db, source_name="scan.csv")
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["staged_card_count"], 2)

        # Same bytes again: refused, and nothing is applied.
        second = process_batch_csv(self.TWO_CARD_BATCH, self.db, source_name="scan.csv")
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["staged_card_count"], 0)
        self.assertEqual(second["new_catalog_count"], 0)
        self.assertTrue(
            any("already processed" in log["message"] for log in second["logs"])
        )

        # Catalog is unchanged by the refusal.
        self.assertEqual(self.db.get_stats()["total_cards"], 2)

        # Forcing it through applies the batch again.
        forced = process_batch_csv(
            self.TWO_CARD_BATCH, self.db, source_name="scan.csv", force=True
        )
        self.assertFalse(forced["duplicate"])
        self.assertEqual(forced["staged_card_count"], 2)
    def test_a_different_batch_is_not_treated_as_duplicate(self):
        process_batch_csv(self.TWO_CARD_BATCH, self.db, source_name="scan.csv")
        other = self.TWO_CARD_BATCH.replace("Deerling", "Sunkern")
        res = process_batch_csv(other, self.db, source_name="scan2.csv")
        self.assertFalse(res["duplicate"])

    def test_search_and_count_use_the_same_columns(self):
        process_batch_csv(self.TWO_CARD_BATCH, self.db)
        # Bin location and SKU id are searchable; both queries must agree.
        for term in ("Bin-1", "7805759", "Deerling", "Temporal", "nope"):
            rows = self.db.get_inventory(search=term, limit=1000)
            total = self.db.get_inventory_count(search=term)
            self.assertEqual(
                len(rows), total, f"listing and count disagree for {term!r}"
            )

    def test_remarks_and_sku_id_are_sortable(self):
        process_batch_csv(self.TWO_CARD_BATCH, self.db)
        desc = self.db.get_inventory(sort_by="remarks", sort_dir="DESC")
        asc = self.db.get_inventory(sort_by="remarks", sort_dir="ASC")
        self.assertEqual(desc[0]["remarks"], "Bin-2")
        self.assertEqual(asc[0]["remarks"], "Bin-1")
        # An unknown column must fall back rather than blow up.
        fallback = self.db.get_inventory(sort_by="not_a_column")
        self.assertEqual(fallback[0]["manifest_id"], "ID1001")

    def test_natural_key_is_unique(self):
        self.db.insert_manifest("ID1001", "Charizard", "Base Set", "Near Mint", "Holofoil")
        # Case and whitespace variations are the same card, so a second insert
        # under a new ID must be rejected by the unique index.
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.insert_manifest(
                "ID1002", "charizard", "base set", "near mint", "holofoil"
            )

    def test_get_or_create_is_idempotent_under_repeat_calls(self):
        ids = set()
        for _ in range(10):
            m_id, is_new, _ = self.db.get_or_create_manifest(
                "Pikachu", "Base Set", "Near Mint", "Normal"
            )
            ids.add(m_id)
        self.assertEqual(ids, {"ID1001"})
        self.assertEqual(self.db.get_stats()["total_cards"], 1)

    # ------------------------------------------------------------------
    # Condition passthrough and eBay variation syntax
    # ------------------------------------------------------------------

    MIXED_CONDITION_BATCH = """"Set","Name","Market Price","Condition","Printing","Quantity","Remarks","SKU Id","*ConditionID"
"Chilling Reign","Deerling","0.17","NM","Normal",1,"Bin-1",111,"4000"
"Chilling Reign","Mareep","0.17","LP","Normal",1,"Bin-2",222,"4000"
"""

    BATCH_WITHOUT_CONDITION_ID = """"Set","Name","Market Price","Condition","Printing","Quantity"
"Chilling Reign","Deerling","0.17","NM","Normal",1
"""

    BATCH_WITHOUT_CONDITION = """"Set","Name","Market Price","Condition","Printing","Quantity","Remarks","SKU Id","*ConditionID"
"Chilling Reign","Deerling","0.17","","Normal",1,"Bin-1",111,"4000"
"""

    BATCH_WITH_PUNCTUATED_NAME = """"Set","Name","Market Price","Condition","Printing","Quantity","Remarks","SKU Id","*ConditionID"
"Chilling Reign","Ho-Oh; Lugia | Legend","0.17","NM","Normal",1,"Bin-1",111,"4000"
"""

    def test_condition_is_passed_through_verbatim(self):
        process_batch_csv(self.MIXED_CONDITION_BATCH, self.db)
        # "NM" and "LP" are stored exactly as the export wrote them; they are
        # not expanded to "Near Mint" / "Lightly Played" by any lookup table.
        conditions = {c["condition"] for c in self.db.export_all_manifest()}
        self.assertEqual(conditions, {"NM", "LP"})

    def test_an_export_with_no_condition_id_column_is_refused_outright(self):
        """
        No ConditionID column means the wrong SortSwift export.

        Previously this was handled per row, which produced one identical
        warning per card -- 426 of them on a real file -- and never stated the
        actual cause. The inventory export is also missing Category, Title,
        C:Game, PostalCode and the policy names, so there is nothing to salvage
        row by row.
        """
        res = process_batch_csv(self.BATCH_WITHOUT_CONDITION_ID, self.db)
        self.assertEqual(res["staged_card_count"], 0)
        self.assertEqual(self.db.get_stats()["total_cards"], 0)
        message = " ".join(log["message"] for log in res["logs"])
        self.assertIn("ConditionID", message)
        # The diagnosis has to name the fix, not just the symptom.
        self.assertIn("export_eBay_", message)

    def test_a_blank_condition_id_in_a_present_column_skips_that_row(self):
        """
        The column exists but a row's value is missing: that is a per-row
        problem, and the rest of the file is still usable.
        """
        batch = (
            '"Set","Name","Market Price","Condition","Printing","Quantity","*ConditionID"\n'
            '"Chilling Reign","Deerling","0.17","NM","Normal",1,""\n'
            '"Chilling Reign","Snover","0.17","NM","Normal",1,"4000"\n'
        )
        res = process_batch_csv(batch, self.db)
        self.assertEqual(res["skipped_count"], 1)
        self.assertEqual(res["staged_card_count"], 1)
        self.assertTrue(
            any("ConditionID" in log["message"] for log in res["logs"]),
            "the skip reason should name the missing value",
        )

    def test_row_without_condition_is_skipped(self):
        res = process_batch_csv(self.BATCH_WITHOUT_CONDITION, self.db)
        self.assertEqual(res["staged_card_count"], 0)
        self.assertEqual(res["skipped_count"], 1)

    def test_a_second_batch_adds_to_the_first(self):
        """
        An upload is a delta of newly scanned cards, so quantities add.

        This used to be one of two modes, and the other one -- replace,
        for a full inventory dump -- was the default. Both are gone: the
        exports are per-batch now, and nothing keeps a full SortSwift
        count accurate, so replacing our quantity from one would restore
        stock that had already sold.
        """
        process_batch_csv(self.MIXED_CONDITION_BATCH, self.db,
                          source_name="b1.csv")
        rows = {r["manifest_id"]: r for r in self.db.export_all_manifest()}
        self.assertEqual(rows["ID1001"]["quantity"], 1)

        second = self.MIXED_CONDITION_BATCH.replace('"Bin-1"', '"Bin-9"')
        process_batch_csv(second, self.db, source_name="b2.csv")
        rows = {r["manifest_id"]: r for r in self.db.export_all_manifest()}
        self.assertEqual(rows["ID1001"]["quantity"], 2)

    def test_catalog_quantity_is_independent_of_live_stock(self):
        process_batch_csv(self.MIXED_CONDITION_BATCH, self.db)
        # eBay reports a different figure than we have catalogued; both must be
        # visible side by side so the drift is apparent.
        self.db.upsert_variation("ID1001", "112233445566", 7)

        row = next(
            r for r in self.db.get_inventory(limit=1000) if r["manifest_id"] == "ID1001"
        )
        self.assertEqual(row["quantity"], 1)
        self.assertEqual(row["last_known_qty"], 7)

    def test_quantity_is_sortable_and_exported(self):
        process_batch_csv(self.MIXED_CONDITION_BATCH, self.db)
        rows = self.db.get_inventory(sort_by="quantity", sort_dir="DESC", limit=1000)
        self.assertIn("quantity", rows[0])
        self.assertIn("quantity", self.db.export_all_manifest()[0])

    def test_duplicate_batch_does_not_inflate_catalog_quantity(self):
        process_batch_csv(self.MIXED_CONDITION_BATCH, self.db, source_name="b.csv")
        process_batch_csv(self.MIXED_CONDITION_BATCH, self.db, source_name="b.csv")
        rows = {r["manifest_id"]: r for r in self.db.export_all_manifest()}
        self.assertEqual(rows["ID1001"]["quantity"], 1)

    # ------------------------------------------------------------------
    # eBay Condition Descriptors (CD:40001) for ungraded cards
    # ------------------------------------------------------------------

    def test_grades_map_to_ebay_game_card_value_ids(self):
        from tcg_engine.batches import resolve_condition_descriptor

        # Game/CCG categories. Only "Near mint or better" shares an ID with the
        # sports table, so these are the values that actually matter.
        expected = {
            "NM": "Near mint or better - (ID: 400010)",
            "Near Mint": "Near mint or better - (ID: 400010)",
            "LP": "Excellent - (ID: 400015)",
            "Lightly Played": "Excellent - (ID: 400015)",
            "MP": "Very good - (ID: 400016)",
            "HP": "Poor - (ID: 400017)",
            "DM": "Poor - (ID: 400017)",
            "Damaged": "Poor - (ID: 400017)",
        }
        for condition, want in expected.items():
            self.assertEqual(
                resolve_condition_descriptor(condition, category_id="183454"),
                want,
                f"wrong descriptor for {condition!r}",
            )

    def test_sports_category_uses_its_own_value_ids(self):
        from tcg_engine.batches import resolve_condition_descriptor

        self.assertEqual(
            resolve_condition_descriptor("LP", category_id="261328"),
            "Excellent - (ID: 400011)",
        )
        self.assertEqual(
            resolve_condition_descriptor("MP", category_id="261328"),
            "Very good - (ID: 400012)",
        )
        # Near mint is the one grade both families share.
        self.assertEqual(
            resolve_condition_descriptor("NM", category_id="261328"),
            resolve_condition_descriptor("NM", category_id="183454"),
        )

    def test_descriptor_style_can_be_bare_id(self):
        from tcg_engine.batches import resolve_condition_descriptor

        self.assertEqual(
            resolve_condition_descriptor("LP", category_id="183454", style="id"),
            "400015",
        )

    def test_unmappable_condition_is_skipped_not_guessed(self):
        from tcg_engine.batches import resolve_condition_descriptor

        self.assertIsNone(resolve_condition_descriptor("Slabbed 9.5"))
        self.assertIsNone(resolve_condition_descriptor(""))

        batch = self.MIXED_CONDITION_BATCH.replace('"NM"', '"Slabbed 9.5"')
        res = process_batch_csv(batch, self.db)
        self.assertGreaterEqual(res["skipped_count"], 1)
        self.assertTrue(
            any("does not map to an eBay ungraded grade" in log["message"]
                for log in res["logs"])
        )

    def test_graded_condition_id_is_skipped(self):
        batch = self.MIXED_CONDITION_BATCH.replace('"4000"', '"2750"')
        res = process_batch_csv(batch, self.db)
        self.assertTrue(
            any("not the ungraded value" in log["message"] for log in res["logs"])
        )

    # ------------------------------------------------------------------
    # Item location and business policies (eBay error 10009)
    # ------------------------------------------------------------------

    def test_seller_settings_are_prepopulated_by_default(self):
        settings = self.db.get_listing_settings()
        self.assertEqual(settings["seller_postal_code"], "94305")
        self.assertEqual(settings["shipping_profile_name"], "Free Shipping Cards")
        self.assertEqual(settings["return_profile_name"], "No Returns")
        self.assertEqual(settings["payment_profile_name"], "Immediate Payment")

    def test_blank_seller_values_are_backfilled_on_reopen(self):
        # Simulates a database created before these defaults existed.
        self.db.set_listing_settings({
            "seller_postal_code": "",
            "shipping_profile_name": "",
            "return_profile_name": "",
            "payment_profile_name": "",
        })
        reopened = Database(self.db_path)
        settings = reopened.get_listing_settings()
        self.assertEqual(settings["seller_postal_code"], "94305")
        self.assertEqual(settings["shipping_profile_name"], "Free Shipping Cards")
        self.assertEqual(settings["return_profile_name"], "No Returns")

    def test_customised_seller_values_are_never_overwritten(self):
        self.db.set_listing_settings({
            "seller_postal_code": "10001",
            "shipping_profile_name": "My Custom Shipping",
        })
        settings = Database(self.db_path).get_listing_settings()
        self.assertEqual(settings["seller_postal_code"], "10001")
        self.assertEqual(settings["shipping_profile_name"], "My Custom Shipping")
        # Untouched keys still hold their defaults.
        self.assertEqual(settings["return_profile_name"], "No Returns")

    def test_refused_duplicate_is_a_true_noop(self):
        """A conflicting batch must not touch live inventory at all."""
        process_batch_csv(self.MIXED_CONDITION_BATCH, self.db, source_name="b.csv")

        def snapshot():
            return (
                [tuple(r.items()) for r in self.db.export_all_manifest()],
                self.db.get_stats(),
            )

        before = snapshot()
        res = process_batch_csv(self.MIXED_CONDITION_BATCH, self.db, source_name="b.csv")
        self.assertTrue(res["duplicate"])
        self.assertEqual(snapshot(), before, "a refused duplicate mutated state")

    def test_quantity_comes_from_the_csv_quantity_column(self):
        hdr = (
            '"Set","Name","Market Price","Condition","Printing","Quantity",'
            '"Remarks","SKU Id","*ConditionID"'
        )
        rows = [
            '"Chilling Reign","Deerling","0.17","NM","Normal",3,"C-1",111,"4000"',
            '"Chilling Reign","Snover","0.17","NM","Normal",7,"C-2",112,"4000"',
        ]
        process_batch_csv(chr(10).join([hdr] + rows) + chr(10), self.db)

        quantities = {
            r["product_name"]: r["quantity"] for r in self.db.export_all_manifest()
        }
        self.assertEqual(quantities["Deerling"], 3)
        self.assertEqual(quantities["Snover"], 7)

    # ------------------------------------------------------------------
    # eBay item specifics (C: columns) -- error 21919303
    # ------------------------------------------------------------------

    EBAY_EXPORT_BATCH = """"*C:Game","*C:Set","*C:Language","*C:Card Name","*C:Card Number","Set","Name","Market Price","Condition","Printing","Quantity","Remarks","SKU Id","*ConditionID"
"Test TCG","Chilling Reign","English","Crushing Gloves","121/198","Chilling Reign","Crushing Gloves","0.17","NM","Normal",1,"C-1",111,"4000"
"Test TCG","Chilling Reign","English","Heracross","004/198","Chilling Reign","Heracross","0.17","NM","Normal",1,"C-1",112,"4000"
"""

    PLAIN_EXPORT_BATCH = """"Game","Set","Set Code","Card Number","Name","Rarity","Market Price","Condition","Language","Printing","Quantity","Remarks","SKU Id","*ConditionID"
"Pokemon","SWSH06: Chilling Reign","CRE","121/198","Crushing Gloves","Uncommon","0.17","NM","English","Normal",1,"C-1",111,"4000"
"Pokemon","SWSH06: Chilling Reign","CRE","004/198","Heracross","Common","0.17","NM","English","Normal",2,"C-1",112,"4000"
"""

    def _full_state(self):
        return (
            [tuple(sorted(r.items())) for r in self.db.export_all_manifest()],
            self.db.get_stats(),
            self.db.get_inventory(limit=1000),
        )

    def test_dry_run_writes_nothing_but_still_reports(self):
        process_batch_csv(self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv")
        before = self._full_state()

        res = process_batch_csv(
            self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv", dry_run=True
        )
        self.assertTrue(res["dry_run"])
        # Not reported as a duplicate: a read-only rebuild is always safe.
        self.assertFalse(res["duplicate"])
        self.assertEqual(res["staged_card_count"], 2,
                         "it must still report what it would stage")
        self.assertEqual(self._full_state(), before, "dry run mutated state")
    def test_dry_run_does_not_fingerprint_the_batch(self):
        res = process_batch_csv(
            self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv", dry_run=True
        )
        # Nothing was catalogued, so nothing could be emitted...
        self.assertEqual(res["staged_card_count"], 0)
        # ...and the batch must remain un-fingerprinted so a real run still works.
        real = process_batch_csv(self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv")
        self.assertFalse(real["duplicate"])
        self.assertEqual(real["staged_card_count"], 2)

    def test_dry_run_skips_cards_not_yet_catalogued(self):
        res = process_batch_csv(
            self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv", dry_run=True
        )
        self.assertEqual(res["staged_card_count"], 0)
        self.assertEqual(res["skipped_count"], 2)
        self.assertTrue(
            any("not in the catalogue yet" in lg["message"] for lg in res["logs"]),
            "the skip reason should explain why an ID cannot be minted",
        )

    def test_add_mode_accumulates_across_batches_before_any_sync(self):
        """
        Two scan deltas uploaded before a sync must both land.

        Add mode accumulates on the catalogue's own figure, which is correct
        whether or not eBay has been told anything. It used to accumulate on
        eBay's last reported quantity, which needed a second column --
        ``pending_qty``, recording what a generated Revise file had asked for
        -- purely so the second batch would not start from the same stale
        number and lose the first. Nothing generates such a file now, and the
        catalogue was always the better base.
        """
        process_batch_csv(self.PLAIN_EXPORT_BATCH, self.db,
                          source_name="b.csv")
        # eBay reports 3 for this card.
        self.db.upsert_variation("ID1001", "998877665544", 3)
        catalogued = self.db.get_manifest_by_id("ID1001")["quantity"]

        process_batch_csv(
            self.PLAIN_EXPORT_BATCH, self.db, source_name="b1.csv",
            force=True,
        )
        self.assertEqual(
            self.db.get_manifest_by_id("ID1001")["quantity"], catalogued + 1
        )

        process_batch_csv(
            self.PLAIN_EXPORT_BATCH, self.db, source_name="b2.csv",
            force=True,
        )
        self.assertEqual(
            self.db.get_manifest_by_id("ID1001")["quantity"], catalogued + 2,
            "the second batch builds on the first, not on eBay's figure",
        )

        # And eBay's own figure is untouched throughout: only a sync or a
        # confirmed push may move it.
        self.assertEqual(
            self.db.get_variation("ID1001")["last_known_qty"], 3)

    def test_a_preview_reaches_the_same_conclusions_as_a_real_run(self):
        """
        A preview and a real run must agree; only one of them writes.

        Quantities add, so the two cannot be compared by running them back to
        back and reading the catalogue -- the real run would have moved it.
        The counts are what is compared, and the preview is asserted to have
        changed nothing at all.
        """
        process_batch_csv(self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv")
        self.db.upsert_variation("ID1001", "998877665544", 3)
        before = {r["manifest_id"]: r["quantity"]
                  for r in self.db.export_all_manifest()}

        preview = process_batch_csv(
            self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv",
            dry_run=True, force=True,
        )
        self.assertEqual(
            {r["manifest_id"]: r["quantity"]
             for r in self.db.export_all_manifest()},
            before, "a preview must not move a single quantity",
        )

        applied = process_batch_csv(
            self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv", force=True,
        )
        comparable = ("staged_card_count", "staged_listing_count",
                      "new_catalog_count", "skipped_count", "parsed_rows")
        self.assertEqual(
            {k: preview[k] for k in comparable},
            {k: applied[k] for k in comparable},
        )

    # ------------------------------------------------------------------
    # Variation option names, ordering, and per-variation images
    # ------------------------------------------------------------------

    NUMBERED_BATCH = """"Game","Set","Card Number","Name","Rarity","Market Price","Condition","Language","Printing","Quantity","Remarks","SKU Id","CDN Image","*ConditionID"
"Pokemon","Chilling Reign","133/198","Crushing Gloves","Common","0.17","NM","English","Normal",1,"C-1",111,"https://cdn/gloves.jpg","4000"
"Pokemon","Chilling Reign","4/198","Heracross","Common","0.17","NM","English","Normal",1,"C-1",112,"https://cdn/heracross.jpg","4000"
"Pokemon","Chilling Reign","16/198","Deerling","Common","0.17","NM","English","Normal",1,"C-1",113,"","4000"
"""

    def _parent(self, res):
        return next(
            r for r in csv.DictReader(io.StringIO(res["add_csv"])) if r["Title"]
        )

    def _children(self, res):
        return [
            r for r in csv.DictReader(io.StringIO(res["add_csv"]))
            if r["Relationship"] == "Variation"
        ]

    def test_option_names_include_the_card_number(self):
        # The card number makes otherwise-identical reprints distinguishable
        # and gives the dropdown a natural order.
        self.assertEqual(
            build_variation_option_name("Crushing Gloves", "133/198"),
            "Crushing Gloves (133/198)",
        )
    def test_options_are_sorted_numerically_by_card_number(self):
        # 4 before 16 before 133 -- a string sort would give 133, 16, 4, and
        # this order *is* the order eBay shows the variation dropdown in.
        cards = [
            {"product_name": "Crushing Gloves", "card_number": "133/198"},
            {"product_name": "Heracross", "card_number": "4/198"},
            {"product_name": "Deerling", "card_number": "16/198"},
        ]
        ordered = [
            build_variation_option_name(c["product_name"], c["card_number"])
            for c in sorted(cards, key=variation_sort_key)
        ]
        self.assertEqual(ordered, [
            "Heracross (4/198)",
            "Deerling (16/198)",
            "Crushing Gloves (133/198)",
        ])

    def test_numbers_with_a_prefix_sort_within_their_prefix(self):
        # Real card numbers are not plain integers: "TG12/TG30", "SV107".
        cards = [
            {"product_name": "b", "card_number": "TG12/TG30"},
            {"product_name": "a", "card_number": "TG2/TG30"},
            {"product_name": "c", "card_number": "5/198"},
        ]
        self.assertEqual(
            [c["product_name"] for c in sorted(cards, key=variation_sort_key)],
            ["c", "a", "b"],
        )
    def test_cards_without_a_number_sort_last_and_drop_the_brackets(self):
        self.assertEqual(build_variation_option_name("Deerling", ""), "Deerling")
        cards = [
            {"product_name": "Deerling", "card_number": ""},
            {"product_name": "Heracross", "card_number": "4/198"},
        ]
        self.assertEqual(
            [c["product_name"] for c in sorted(cards, key=variation_sort_key)],
            ["Heracross", "Deerling"],
            "an unnumbered card sorts last rather than scattering",
        )
    def test_option_template_is_configurable(self):
        self.assertEqual(
            build_variation_option_name(
                "Heracross", "4/198", template="{card_number} {name}"
            ),
            "4/198 Heracross",
        )
    def test_equals_sign_is_stripped_from_option_names(self):
        from tcg_engine.batches import build_variation_option_name

        # "=" separates the option from its URL in PicURL, so it must not
        # survive inside an option name.
        built = build_variation_option_name("Weird=Card", "1/10")
        self.assertNotIn("=", built)

    # ------------------------------------------------------------------
    # Purging inventory for a clean test run
    # ------------------------------------------------------------------

    def test_purge_clears_inventory_but_keeps_configuration(self):
        process_batch_csv(self.NUMBERED_BATCH, self.db, source_name="b.csv")
        self.db.upsert_variation("ID1001", "998877665544", 5)
        self.db.set_listing_settings({"seller_postal_code": "10001"})

        self.assertGreater(self.db.get_stats()["total_cards"], 0)

        counts = self.db.purge_inventory()
        self.assertEqual(counts["manifest"], 3)
        self.assertEqual(counts["processed_batches"], 1)

        stats = self.db.get_stats()
        self.assertEqual(stats["total_cards"], 0)
        self.assertEqual(stats["active_listings"], 0)
        self.assertEqual(self.db.get_inventory(limit=100), [])

        # Configuration must survive: a reset for testing should not also
        # discard the seller setup.
        settings = self.db.get_listing_settings()
        self.assertEqual(settings["seller_postal_code"], "10001")
        self.assertEqual(settings["shipping_profile_name"], "Free Shipping Cards")
        self.assertGreaterEqual(len(self.db.get_pricing_rules()), 4)

    def test_purge_allows_the_same_batch_to_be_reprocessed(self):
        process_batch_csv(self.NUMBERED_BATCH, self.db, source_name="b.csv")
        # Refused as a duplicate before the purge.
        self.assertTrue(
            process_batch_csv(self.NUMBERED_BATCH, self.db, source_name="b.csv")["duplicate"]
        )

        self.db.purge_inventory()

        again = process_batch_csv(self.NUMBERED_BATCH, self.db, source_name="b.csv")
        self.assertFalse(again["duplicate"])
        self.assertEqual(again["staged_card_count"], 3)
        # IDs restart, since the catalogue is empty.
        self.assertEqual(self.db.get_next_manifest_id(), "ID1004")

    def test_purge_on_an_empty_database_is_harmless(self):
        counts = self.db.purge_inventory()
        self.assertEqual(counts["manifest"], 0)
        self.assertEqual(self.db.get_stats()["total_cards"], 0)

    # ------------------------------------------------------------------
    # Card # column in the inventory view
    # ------------------------------------------------------------------

    MIXED_NUMBER_BATCH = """"Game","Set","Card Number","Name","Market Price","Condition","Language","Printing","Quantity","Remarks","SKU Id","*ConditionID"
"Pokemon","Chilling Reign","133/198","Crushing Gloves","0.17","NM","English","Normal",1,"C-1",111,"4000"
"Pokemon","Chilling Reign","4/198","Heracross","0.17","NM","English","Normal",1,"C-1",112,"4000"
"Pokemon","Chilling Reign","16/198","Deerling","0.17","NM","English","Normal",1,"C-1",113,"4000"
"Pokemon","Chilling Reign","TG12/TG30","Blissey V","0.17","NM","English","Normal",1,"C-1",114,"4000"
"""

    def test_inventory_returns_the_card_number(self):
        process_batch_csv(self.MIXED_NUMBER_BATCH, self.db)
        rows = self.db.get_inventory(limit=100)
        self.assertTrue(rows)
        for r in rows:
            self.assertIn("card_number", r)
        numbers = {r["product_name"]: r["card_number"] for r in rows}
        self.assertEqual(numbers["Crushing Gloves"], "133/198")
        self.assertEqual(numbers["Blissey V"], "TG12/TG30")

    def test_card_number_sorts_numerically_both_ways(self):
        process_batch_csv(self.MIXED_NUMBER_BATCH, self.db)

        asc = [
            r["card_number"]
            for r in self.db.get_inventory(sort_by="card_number", sort_dir="ASC", limit=100)
        ]
        # 4 before 16 before 133 (a text sort would give 133, 16, 4), and
        # prefixed numbering sorts after the plain numbers, matching how the
        # engine orders variation options.
        self.assertEqual(asc, ["4/198", "16/198", "133/198", "TG12/TG30"])

        desc = [
            r["card_number"]
            for r in self.db.get_inventory(sort_by="card_number", sort_dir="DESC", limit=100)
        ]
        # DESC must apply to every term of the sort, not just the last one.
        self.assertEqual(desc, list(reversed(asc)))

    def test_card_number_is_searchable_and_counts_agree(self):
        process_batch_csv(self.MIXED_NUMBER_BATCH, self.db)
        for term, expected in (("133/198", 1), ("TG12", 1), ("/198", 3), ("nope", 0)):
            rows = self.db.get_inventory(search=term, limit=1000)
            total = self.db.get_inventory_count(search=term)
            self.assertEqual(len(rows), total, f"listing and count disagree for {term!r}")
            self.assertEqual(total, expected, f"unexpected matches for {term!r}")

    def test_card_number_is_included_in_the_export(self):
        process_batch_csv(self.MIXED_NUMBER_BATCH, self.db)
        exported = self.db.export_all_manifest()
        self.assertTrue(exported)
        self.assertIn("card_number", exported[0])

    def test_a_card_without_a_number_yields_an_empty_string(self):
        batch = self.MIXED_NUMBER_BATCH.replace('"133/198"', '""')
        process_batch_csv(batch, self.db)
        rows = {r["product_name"]: r["card_number"] for r in self.db.get_inventory(limit=100)}
        # Empty rather than None, so the template can render it directly.
        self.assertEqual(rows["Crushing Gloves"], "")

    # ------------------------------------------------------------------
    # Module B against the real Active Listings report shape
    # ------------------------------------------------------------------

    ACTIVE_LISTINGS_REPORT = """Item number,Title,Variation details,Custom label (SKU),Available quantity,Format,Condition
"227379391171",Nintendo Wii Power Supply,,,"1","FIXED_PRICE","Used"
"227496702985",4x Gwynn - Pitch Black Playset,,,"7","FIXED_PRICE","Ungraded"
"227511361186",Chilling Reign: Pick Your Card,Card=Ledyba (004/198);Heracross (006/198),,"40","FIXED_PRICE","Ungraded"
"227511361186",Chilling Reign: Pick Your Card,Card=Ledyba (004/198),ID1050-C-1,"1","FIXED_PRICE","Ungraded"
"227511361186",Chilling Reign: Pick Your Card,Card=Heracross (006/198),ID1037-C-1,"3","FIXED_PRICE","Ungraded"
"""

    def _seed_live_catalog(self):
        for mid, name in (("ID1037", "Heracross"), ("ID1050", "Ledyba")):
            self.db.insert_manifest(
                mid, name, "SWSH06: Chilling Reign", "NM", "Normal"
            )

    def test_sync_handles_the_real_active_listings_report(self):
        self._seed_live_catalog()
        res = sync_active_listings_csv(self.ACTIVE_LISTINGS_REPORT, self.db)

        self.assertEqual(res["synced_count"], 2)
        self.assertEqual(res["linked_listing_count"], 1)
        self.assertEqual(res["skipped_unmapped_count"], 0)

        links = {
            r["manifest_id"]: (r["ebay_parent_id"], r["last_known_qty"])
            for r in self.db.get_inventory(limit=50)
        }
        self.assertEqual(links["ID1050"], ("227511361186", 1))
        self.assertEqual(links["ID1037"], ("227511361186", 3))

    def test_sync_separates_variation_parents_from_unmanaged_listings(self):
        """An Active Listings report contains every listing the seller has."""
        self._seed_live_catalog()
        res = sync_active_listings_csv(self.ACTIVE_LISTINGS_REPORT, self.db)

        # One genuine variation container row...
        self.assertEqual(res["skipped_parent_count"], 1)
        # ...and two ordinary listings that simply have no SKU. Counting these
        # as "parent rows" made a normal store look broken.
        self.assertEqual(res["skipped_unlabelled_count"], 2)

        summary = [l["message"] for l in res["logs"] if l["level"] == "INFO"][-1]
        self.assertIn("not managed by this tool", summary)

    def test_sync_reports_labels_missing_from_the_catalog(self):
        # Catalogue deliberately left empty: this is what a purge-then-sync
        # would look like, and it must be loudly reported rather than silent.
        res = sync_active_listings_csv(self.ACTIVE_LISTINGS_REPORT, self.db)
        self.assertEqual(res["synced_count"], 0)
        self.assertEqual(res["skipped_unmapped_count"], 2)
        self.assertTrue(
            any("not in Master Catalog" in l["message"] for l in res["logs"])
        )

    # ------------------------------------------------------------------
    # Recovering the catalog-to-eBay link after IDs diverge
    # ------------------------------------------------------------------

    DIVERGED_REPORT = """Item number,Title,Variation details,Custom label (SKU),Available quantity
"227511361186",Pick Your Card,Card=Ledyba (004/198);Heracross (006/198),,"3"
"227511361186",Pick Your Card,Card=Ledyba (004/198),ID1050-C-1,"1"
"227511361186",Pick Your Card,Card=Heracross (006/198),ID1037-C-1,"2"
"""

    def _rebuilt_catalog(self):
        """A catalog rebuilt after a purge: same cards, different IDs."""
        self.db.insert_manifest(
            "ID1001", "Ledyba", "SWSH06: Chilling Reign", "NM", "Normal",
            card_number="004/198")
        self.db.insert_manifest(
            "ID1002", "Heracross", "SWSH06: Chilling Reign", "NM", "Normal",
            card_number="006/198")

    def test_option_name_parsing(self):
        self.assertEqual(parse_option_name("Ledyba (004/198)"), ("Ledyba", "004/198"))
        self.assertEqual(
            parse_option_name("Rapid Strike Scroll of the Skies (151/198)"),
            ("Rapid Strike Scroll of the Skies", "151/198"),
        )
        self.assertEqual(parse_option_name("Blissey V (TG12/TG30)"), ("Blissey V", "TG12/TG30"))
        # No number at all.
        self.assertEqual(parse_option_name("Mystery Promo"), ("Mystery Promo", ""))

    def test_sync_fails_before_relink_when_ids_diverge(self):
        self._rebuilt_catalog()
        res = sync_active_listings_csv(self.DIVERGED_REPORT, self.db)
        self.assertEqual(res["synced_count"], 0)
        self.assertEqual(res["skipped_unmapped_count"], 2)

    def test_relink_realigns_ids_then_sync_succeeds(self):
        self._rebuilt_catalog()

        rl = relink_from_active_listings(self.DIVERGED_REPORT, self.db)
        self.assertEqual(rl["renamed_count"], 2)
        self.assertEqual(rl["unmatched_count"], 0)

        # The catalog now uses the IDs eBay already has.
        self.assertIsNotNone(self.db.get_manifest_by_id("ID1050"))
        self.assertIsNotNone(self.db.get_manifest_by_id("ID1037"))
        self.assertIsNone(self.db.get_manifest_by_id("ID1001"))

        # Same cards, nothing lost.
        names = {c["product_name"] for c in self.db.export_all_manifest()}
        self.assertEqual(names, {"Ledyba", "Heracross"})

        res = sync_active_listings_csv(self.DIVERGED_REPORT, self.db)
        self.assertEqual(res["synced_count"], 2)
        self.assertEqual(res["skipped_unmapped_count"], 0)

    def test_relink_is_idempotent(self):
        self._rebuilt_catalog()
        relink_from_active_listings(self.DIVERGED_REPORT, self.db)
        again = relink_from_active_listings(self.DIVERGED_REPORT, self.db)
        self.assertEqual(again["renamed_count"], 0)
        self.assertEqual(again["already_linked_count"], 2)

    def test_relink_refuses_when_the_target_id_is_taken(self):
        self._rebuilt_catalog()
        # A different card already occupies ID1050.
        self.db.insert_manifest(
            "ID1050", "Some Other Card", "Other Set", "NM", "Normal",
            card_number="999/198")

        rl = relink_from_active_listings(self.DIVERGED_REPORT, self.db)
        self.assertEqual(rl["conflict_count"], 1)
        # The occupant is untouched and Ledyba keeps its original ID.
        self.assertEqual(
            self.db.get_manifest_by_id("ID1050")["product_name"], "Some Other Card"
        )
        self.assertIsNotNone(self.db.get_manifest_by_id("ID1001"))

    def test_relink_skips_cards_it_cannot_identify(self):
        # Empty catalog: nothing to match the listing against.
        rl = relink_from_active_listings(self.DIVERGED_REPORT, self.db)
        self.assertEqual(rl["renamed_count"], 0)
        self.assertEqual(rl["unmatched_count"], 2)

    def test_rename_carries_the_store_mirror_row(self):
        self._rebuilt_catalog()
        self.db.upsert_variation("ID1001", "227511361186", 7)

        self.db.rename_manifest("ID1001", "ID1050")

        self.assertIsNone(self.db.get_variation("ID1001"))
        moved = self.db.get_variation("ID1050")
        self.assertIsNotNone(moved)
        self.assertEqual(moved["ebay_parent_id"], "227511361186")
        self.assertEqual(moved["last_known_qty"], 7)

    # ------------------------------------------------------------------
    # UTF-8 BOM tolerance (eBay reports ship with one)
    # ------------------------------------------------------------------

    BOM = chr(0xFEFF)

    def test_bom_does_not_break_the_first_column(self):
        """
        A BOM turns the first header into '﻿Item number', which matched
        nothing and made Module B record every eBay item number as "UNKNOWN"
        while otherwise appearing to succeed. Item number is the first column of
        an Active Listings report, so it was always the casualty.
        """
        self._seed_live_catalog()
        res = sync_active_listings_csv(self.BOM + self.ACTIVE_LISTINGS_REPORT, self.db)

        self.assertEqual(res["synced_count"], 2)
        item_ids = {
            r["ebay_parent_id"] for r in self.db.get_inventory(limit=50)
            if r["ebay_parent_id"]
        }
        self.assertEqual(item_ids, {"227511361186"})
        self.assertNotIn("UNKNOWN", item_ids)

    def test_bom_is_tolerated_by_the_ingest(self):
        """
        Excel writes a byte-order mark and the ingest must not choke on it.

        This covered Module C as well until the deduction path was removed.
        """
        db_b = Database(self.db_path + ".b")
        plain = process_batch_csv(self.NUMBERED_BATCH, db_b)
        db_b2 = Database(self.db_path + ".b2")
        with_bom = process_batch_csv(self.BOM + self.NUMBERED_BATCH, db_b2)
        self.assertEqual(plain["staged_card_count"], with_bom["staged_card_count"])
        self.assertEqual(with_bom["skipped_count"], 0)

    def test_find_column_tolerates_bom_asterisk_and_case(self):
        from tcg_engine.csvtools import find_column

        row = {
            self.BOM + "Item number": "227511361186",
            "*ConditionID": "4000",
            "  Custom label (SKU)  ": "ID1050-C-1",
        }
        self.assertEqual(find_column(row, ["Item number"]), "227511361186")
        # eBay's leading asterisk marks a required field; it is not part of the
        # name, so either spelling must resolve.
        self.assertEqual(find_column(row, ["ConditionID"]), "4000")
        self.assertEqual(find_column(row, ["*ConditionID"]), "4000")
        self.assertEqual(find_column(row, ["custom label (sku)"]), "ID1050-C-1")
        self.assertIsNone(find_column(row, ["Nope"]))

    # ------------------------------------------------------------------
    # Set filter, absolute quantity edits, deduction rows
    # ------------------------------------------------------------------

    TWO_SET_BATCH = """"Game","Set","Card Number","Name","Market Price","Condition","Language","Printing","Quantity","Remarks","SKU Id","TCGplayer Id","*ConditionID"
"Pokemon","Chilling Reign","004/198","Ledyba","0.17","NM","English","Normal",3,"C-1",111,542678,"4000"
"Pokemon","Chilling Reign","006/198","Heracross","0.17","NM","English","Normal",2,"C-1",112,542679,"4000"
"Pokemon","Temporal Forces","016/162","Deerling","0.17","NM","English","Normal",5,"T-1",113,542680,"4000"
"""

    def test_distinct_set_names_come_from_the_catalog(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        sets = {s["set_name"]: s["card_count"] for s in self.db.get_distinct_set_names()}
        self.assertEqual(sets, {"Chilling Reign": 2, "Temporal Forces": 1})

    def test_set_filter_narrows_the_inventory(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)

        rows = self.db.get_inventory(set_name="Chilling Reign", limit=100)
        self.assertEqual({r["product_name"] for r in rows}, {"Ledyba", "Heracross"})
        self.assertEqual(self.db.get_inventory_count(set_name="Chilling Reign"), 2)

        # Matching is case-insensitive, since the value round-trips through a
        # URL parameter.
        self.assertEqual(len(self.db.get_inventory(set_name="chilling reign", limit=100)), 2)

        # An empty filter means no filter, not "match nothing".
        self.assertEqual(self.db.get_inventory_count(set_name=""), 3)
        self.assertEqual(self.db.get_inventory_count(set_name=None), 3)

    def test_set_filter_and_search_compose(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)

        rows = self.db.get_inventory(set_name="Chilling Reign", search="Ledyba", limit=100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.db.get_inventory_count(set_name="Chilling Reign", search="Ledyba"), 1)

        # The card exists, but not in that set.
        self.assertEqual(
            self.db.get_inventory_count(set_name="Temporal Forces", search="Ledyba"), 0
        )

    def test_inventory_exposes_tcgplayer_id_for_linking(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        ids = {r["product_name"]: r["tcgplayer_id"] for r in self.db.get_inventory(limit=100)}
        self.assertEqual(ids["Ledyba"], "542678")
        # Absent rather than None, so a template can test it directly.
        manual = self.db.get_or_create_manifest("Manual", "Some Set", "NM", "Normal")[0]
        row = next(r for r in self.db.get_inventory(limit=100) if r["manifest_id"] == manual)
        self.assertEqual(row["tcgplayer_id"], "")

    def test_set_quantity_is_absolute_not_additive(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        card = next(c for c in self.db.export_all_manifest() if c["product_name"] == "Ledyba")
        self.assertEqual(card["quantity"], 3)

        result = self.db.set_manifest_quantity(card["manifest_id"], 1)
        self.assertEqual(result["previous"], 3)
        self.assertEqual(result["current"], 1)

        # Batch intake accumulates; a manual correction replaces.
        again = self.db.set_manifest_quantity(card["manifest_id"], 1)
        self.assertEqual(again["current"], 1)

    def test_set_quantity_clamps_negatives_and_reports_unknown_cards(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        card = next(c for c in self.db.export_all_manifest() if c["product_name"] == "Ledyba")

        self.assertEqual(self.db.set_manifest_quantity(card["manifest_id"], -5)["current"], 0)
        self.assertIsNone(self.db.set_manifest_quantity("ID9999", 1))

    # ------------------------------------------------------------------
    # eBay Listings roll-up
    # ------------------------------------------------------------------

    def test_ebay_listings_is_empty_before_any_sync(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        # Cards exist but nothing is linked, so there are no listings to show.
        self.assertEqual(self.db.get_ebay_listings(), [])

    def test_ebay_listings_groups_variations_by_item_number(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        by_name = {c["product_name"]: c["manifest_id"]
                   for c in self.db.export_all_manifest()}

        # Two cards on one listing, one card on another.
        self.db.upsert_variation(by_name["Ledyba"], "111222333444", 3)
        self.db.upsert_variation(by_name["Heracross"], "111222333444", 2)
        self.db.upsert_variation(by_name["Deerling"], "555666777888", 1)

        listings = {l["ebay_parent_id"]: l for l in self.db.get_ebay_listings()}
        self.assertEqual(set(listings), {"111222333444", "555666777888"})

        multi = listings["111222333444"]
        self.assertEqual(multi["card_count"], 2)
        self.assertEqual(multi["live_quantity"], 5)      # 3 + 2 from eBay
        self.assertEqual(multi["catalog_quantity"], 5)   # 3 + 2 catalogued
        self.assertEqual(multi["set_name"], "Chilling Reign")
        self.assertEqual(multi["set_count"], 1)
        self.assertEqual(multi["condition_count"], 1)
        self.assertTrue(multi["last_synced"])

        single = listings["555666777888"]
        self.assertEqual(single["card_count"], 1)
        # Catalogued 5, eBay reports 1: the drift the view highlights.
        self.assertEqual(single["catalog_quantity"], 5)
        self.assertEqual(single["live_quantity"], 1)

    def test_ebay_listings_flags_a_listing_spanning_several_sets(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        by_name = {c["product_name"]: c["manifest_id"]
                   for c in self.db.export_all_manifest()}

        # Cards from two different sets sharing one listing should be visible
        # as such rather than silently reported as the first set only.
        self.db.upsert_variation(by_name["Ledyba"], "999", 1)
        self.db.upsert_variation(by_name["Deerling"], "999", 1)

        listing = self.db.get_ebay_listings()[0]
        self.assertEqual(listing["card_count"], 2)
        self.assertEqual(listing["set_count"], 2)

    def test_ebay_listings_ignores_rows_with_no_item_number(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        by_name = {c["product_name"]: c["manifest_id"]
                   for c in self.db.export_all_manifest()}
        self.db.upsert_variation(by_name["Ledyba"], "111222333444", 1)
        self.db.upsert_variation(by_name["Heracross"], "", 4)

        listings = self.db.get_ebay_listings()
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0]["card_count"], 1)

    # ------------------------------------------------------------------
    # Deduction direction (SortSwift adds the quantity column)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Per-listing cover photo
    # ------------------------------------------------------------------

    def _linked_listing(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        by_name = {c["product_name"]: c["manifest_id"]
                   for c in self.db.export_all_manifest()}
        self.db.upsert_variation(by_name["Ledyba"], "227511361186", 1)
        self.db.upsert_variation(by_name["Heracross"], "227511361186", 1)
        return "227511361186"

    def test_cover_image_defaults_to_empty_and_round_trips(self):
        item = self._linked_listing()
        self.assertEqual(self.db.get_listing_cover_image(item), "")
        self.assertEqual(
            self.db.get_ebay_listings()[0]["cover_image_url"], ""
        )

        self.db.set_listing_cover_image(item, "https://cdn/cover.jpg")
        self.assertEqual(self.db.get_listing_cover_image(item), "https://cdn/cover.jpg")
        self.assertEqual(
            self.db.get_ebay_listings()[0]["cover_image_url"], "https://cdn/cover.jpg"
        )

    def test_cover_image_is_per_listing_not_global(self):
        item = self._linked_listing()
        by_name = {c["product_name"]: c["manifest_id"]
                   for c in self.db.export_all_manifest()}
        self.db.upsert_variation(by_name["Deerling"], "999888777", 1)

        self.db.set_listing_cover_image(item, "https://cdn/one.jpg")
        covers = {l["ebay_parent_id"]: l["cover_image_url"]
                  for l in self.db.get_ebay_listings()}
        self.assertEqual(covers[item], "https://cdn/one.jpg")
        # The other listing is untouched.
        self.assertEqual(covers["999888777"], "")

    def test_cover_image_survives_a_resync(self):
        item = self._linked_listing()
        self.db.set_listing_cover_image(item, "https://cdn/cover.jpg")

        # A sync rewrites ebay_variations; the override lives elsewhere and
        # must not be collateral damage.
        report = (
            "Item number,Title,Variation details,Custom label (SKU),Available quantity"
            + chr(10)
            + '"227511361186",Pick,Card=Ledyba (004/198),'
            + [c["manifest_id"] for c in self.db.export_all_manifest()
               if c["product_name"] == "Ledyba"][0] + ',"9"' + chr(10)
        )
        sync_active_listings_csv(report, self.db)
        self.assertEqual(self.db.get_listing_cover_image(item), "https://cdn/cover.jpg")

    def test_snapshot_export_is_self_contained(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        dest = os.path.join(self.temp_dir.name, "snap.db")
        self.db.export_snapshot(dest)

        # VACUUM INTO checkpoints the WAL, so no sidecar is needed to read it.
        self.assertTrue(os.path.exists(dest))
        self.assertFalse(os.path.exists(dest + "-wal"))

        conn = sqlite3.connect(dest)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM manifest").fetchone()[0], 3)
        conn.close()

    def test_inspect_rejects_files_that_are_not_our_database(self):
        # Not SQLite at all.
        junk = os.path.join(self.temp_dir.name, "junk.txt")
        with open(junk, "wb") as f:
            f.write(b"not a database")
        bad = Database.inspect_snapshot(junk)
        self.assertFalse(bad["ok"])
        self.assertIn("not a SQLite", bad["error"])

        # Valid SQLite, wrong schema.
        foreign = os.path.join(self.temp_dir.name, "foreign.db")
        c = sqlite3.connect(foreign)
        c.execute("CREATE TABLE unrelated (x INTEGER)")
        c.commit(); c.close()
        wrong = Database.inspect_snapshot(foreign)
        self.assertFalse(wrong["ok"])
        self.assertIn("missing", wrong["error"])

        # A real snapshot passes and is summarised.
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        good_path = os.path.join(self.temp_dir.name, "good.db")
        self.db.export_snapshot(good_path)
        good = Database.inspect_snapshot(good_path)
        self.assertTrue(good["ok"])
        self.assertEqual(good["counts"]["manifest"], 3)

    def test_restore_replaces_data_and_keeps_a_backup(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        snapshot = os.path.join(self.temp_dir.name, "snap.db")
        self.db.export_snapshot(snapshot)

        self.db.purge_inventory()
        self.assertEqual(self.db.get_stats()["total_cards"], 0)

        result = self.db.replace_with_snapshot(snapshot)
        self.assertEqual(self.db.get_stats()["total_cards"], 3)
        # Reversible: the emptied database was copied aside first.
        self.assertTrue(result["backup_path"])
        self.assertTrue(os.path.exists(result["backup_path"]))
        restored = Database.inspect_snapshot(result["backup_path"])
        self.assertTrue(restored["ok"])
        self.assertEqual(restored["counts"]["manifest"], 0)

    def test_restore_refuses_a_bad_snapshot_without_touching_data(self):
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        before = self.db.get_stats()["total_cards"]

        junk = os.path.join(self.temp_dir.name, "junk.db")
        with open(junk, "wb") as f:
            f.write(b"still not a database")

        with self.assertRaises(ValueError):
            self.db.replace_with_snapshot(junk)
        self.assertEqual(self.db.get_stats()["total_cards"], before)

    def test_restore_runs_migrations_on_the_incoming_file(self):
        """A backup from an older build must still open afterwards."""
        process_batch_csv(self.TWO_SET_BATCH, self.db)
        snapshot = os.path.join(self.temp_dir.name, "old.db")
        self.db.export_snapshot(snapshot)

        # Simulate an older schema: drop a table a later migration adds.
        c = sqlite3.connect(snapshot)
        c.execute("DROP TABLE IF EXISTS ebay_listing_overrides")
        c.commit(); c.close()

        self.db.replace_with_snapshot(snapshot)
        # init_db ran, so the table is back and the feature still works.
        self.db.set_listing_cover_image("123", "https://cdn/x.jpg")
        self.assertEqual(self.db.get_listing_cover_image("123"), "https://cdn/x.jpg")

    # -- per-user pricing rules and listing settings -----------------------

    def test_pricing_rules_are_scoped_per_user(self):
        """A user's own rules must not disturb the shared baseline."""
        baseline = self.db.get_pricing_rules()
        self.assertGreaterEqual(len(baseline), 4)
        self.assertFalse(self.db.has_own_pricing_rules(7))

        # A user with nothing of their own inherits the baseline.
        self.assertEqual(self.db.get_pricing_rules(user_id=7), baseline)

        self.db.set_pricing_rules(
            [{"min_price": 0.0, "max_price": None,
              "rule_type": "fixed", "rule_value": 9.99, "sort_order": 1}],
            user_id=7,
        )
        self.assertTrue(self.db.has_own_pricing_rules(7))

        own = self.db.get_pricing_rules(user_id=7)
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["rule_value"], 9.99)

        # Neither the baseline nor an unrelated user moved.
        self.assertEqual(self.db.get_pricing_rules(), baseline)
        self.assertEqual(self.db.get_pricing_rules(user_id=8), baseline)

        # Prices follow the scope.
        self.assertEqual(self.db.calculate_price(0.30, user_id=7)[0], 9.99)
        self.assertEqual(self.db.calculate_price(0.30, user_id=8)[0], 2.49)
        self.assertEqual(self.db.calculate_price(0.30)[0], 2.49)

    def test_pricing_rules_reset_restores_inheritance(self):
        """Reset drops a user's own set; the baseline rewrites shipped defaults."""
        baseline = self.db.get_pricing_rules()
        self.db.set_pricing_rules(
            [{"min_price": 0.0, "max_price": None,
              "rule_type": "fixed", "rule_value": 4.44, "sort_order": 1}],
            user_id=7,
        )
        after = self.db.reset_default_pricing_rules(user_id=7)
        self.assertFalse(self.db.has_own_pricing_rules(7))
        self.assertEqual(after, baseline)

        # On the shared scope there is nothing to inherit, so defaults are
        # written back instead.
        self.db.set_pricing_rules(
            [{"min_price": 0.0, "max_price": None,
              "rule_type": "fixed", "rule_value": 4.44, "sort_order": 1}]
        )
        restored = self.db.reset_default_pricing_rules()
        self.assertEqual(len(restored), len(self.db.SHIPPED_PRICING_RULES))
        self.assertEqual(self.db.calculate_price(0.30)[0], 2.49)

    def test_listing_settings_merge_and_reset_per_user(self):
        """Settings merge key by key, so one override does not hide the rest."""
        self.db.set_listing_settings({"seller_postal_code": "94305"})
        shared = self.db.get_listing_settings()

        self.db.set_listing_settings({"seller_postal_code": "10001"}, user_id=7)

        own = self.db.get_listing_settings(user_id=7)
        self.assertEqual(own["seller_postal_code"], "10001")
        self.assertEqual(self.db.get_listing_settings()["seller_postal_code"], "94305")
        self.assertEqual(
            self.db.get_listing_settings(user_id=8)["seller_postal_code"], "94305"
        )

        # Every other key is still inherited, and none went missing.
        self.assertEqual(set(own), set(shared))
        for key, value in shared.items():
            if key != "seller_postal_code":
                self.assertEqual(own[key], value, key)

        self.assertEqual(self.db.get_own_listing_setting_keys(7), ["seller_postal_code"])
        self.assertEqual(self.db.get_own_listing_setting_keys(8), [])

        # Single-key reads honour the same precedence.
        self.assertEqual(
            self.db.get_listing_setting("seller_postal_code", user_id=7), "10001"
        )
        self.assertEqual(
            self.db.get_listing_setting("seller_postal_code", user_id=8), "94305"
        )

        self.db.reset_listing_settings(user_id=7)
        self.assertEqual(self.db.get_own_listing_setting_keys(7), [])
        self.assertEqual(
            self.db.get_listing_setting("seller_postal_code", user_id=7), "94305"
        )

    def test_batch_uses_the_uploaders_own_rules(self):
        """Module A must price with the caller's rules, not the baseline."""
        csv_text = (
            '"Game","Set","Card Number","Name","Market Price","Condition",'
            '"Language","Printing","Quantity","*ConditionID"' + chr(10)
            + '"Pokemon","Chilling Reign","004/198","Ledyba","0.30","NM",'
            '"English","Normal",3,"4000"' + chr(10)
        )
        # Catalogue once so the rows have manifest IDs to emit.
        process_batch_csv(csv_text, self.db, source_name="seed.csv")

        self.db.set_pricing_rules(
            [{"min_price": 0.0, "max_price": None,
              "rule_type": "fixed", "rule_value": 9.99, "sort_order": 1}],
            user_id=7,
        )

        shared = process_batch_csv(
            csv_text, self.db, source_name="a.csv", force=True, dry_run=True
        )
        mine = process_batch_csv(
            csv_text, self.db, source_name="a.csv", force=True, dry_run=True,
            user_id=7,
        )
        # The rules are the uploader's own, so the price each run computes
        # differs. A dry run stores nothing, so read it from the logs,
        # which name the price they priced each card at.
        self.assertIn("2.49", chr(10).join(
            lg["message"] for lg in shared["logs"]))
        self.assertIn("9.99", chr(10).join(
            lg["message"] for lg in mine["logs"]))

    def test_scoping_migration_preserves_an_unscoped_database(self):
        """
        A database written before scoping existed must migrate in place, with
        its rows landing in the shared baseline rather than being discarded.

        The fixture is built by taking a real database and stripping the
        scoping back out, rather than by hand-writing an old schema: a
        hand-written one drifts from what actually shipped, and would not
        exercise the same migration path.
        """
        legacy = os.path.join(self.temp_dir.name, "legacy.db")
        seeded = Database(legacy)
        seeded.set_pricing_rules(
            [{"min_price": 0.0, "max_price": None,
              "rule_type": "fixed", "rule_value": 7.77, "sort_order": 1}]
        )
        seeded.set_listing_settings({"seller_postal_code": "02134"})

        # Undo the scoping: drop user_id from both tables, restoring the old
        # shape including listing_settings' single-column primary key.
        conn = sqlite3.connect(legacy)
        conn.executescript(
            """
            PRAGMA journal_mode=DELETE;
            DROP INDEX IF EXISTS idx_pricing_rules_user;

            CREATE TABLE pr_old (
                id INTEGER PRIMARY KEY AUTOINCREMENT, min_price REAL NOT NULL,
                max_price REAL, rule_type TEXT NOT NULL,
                rule_value REAL NOT NULL, sort_order INTEGER DEFAULT 0
            );
            INSERT INTO pr_old
                SELECT id, min_price, max_price, rule_type, rule_value, sort_order
                FROM pricing_rules;
            DROP TABLE pricing_rules;
            ALTER TABLE pr_old RENAME TO pricing_rules;

            CREATE TABLE ls_old (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO ls_old SELECT key, value FROM listing_settings;
            DROP TABLE listing_settings;
            ALTER TABLE ls_old RENAME TO listing_settings;
            """
        )
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pricing_rules)")}
        conn.close()
        self.assertNotIn("user_id", cols, "fixture should be unscoped")

        opened = Database(legacy)

        # The old configuration survived and is now the shared baseline.
        self.assertEqual(
            [r["rule_value"] for r in opened.get_pricing_rules()], [7.77]
        )
        self.assertEqual(opened.get_listing_setting("seller_postal_code"), "02134")
        self.assertEqual(
            opened.get_listing_setting("seller_postal_code", user_id=7), "02134"
        )
        self.assertEqual(opened.calculate_price(0.30, user_id=7)[0], 7.77)

        with opened.get_connection() as c:
            self.assertEqual(
                [r[0] for r in c.execute(
                    "SELECT DISTINCT user_id FROM pricing_rules")], [0]
            )
            self.assertEqual(
                [r[0] for r in c.execute(
                    "SELECT DISTINCT user_id FROM listing_settings")], [0]
            )
            pk = [r["name"] for r in
                  c.execute("PRAGMA table_info(listing_settings)") if r["pk"]]
        self.assertEqual(sorted(pk), ["key", "user_id"])

        # Re-opening must not migrate a second time or lose anything.
        again = Database(legacy)
        self.assertEqual([r["rule_value"] for r in again.get_pricing_rules()], [7.77])

        # And the migrated database still supports per-user overrides.
        again.set_listing_settings({"seller_postal_code": "10001"}, user_id=7)
        self.assertEqual(
            again.get_listing_setting("seller_postal_code", user_id=7), "10001"
        )
        self.assertEqual(again.get_listing_setting("seller_postal_code"), "02134")
    # -- full inventory dump semantics -------------------------------------

    FULL_DUMP = (
        '"Game","Set","Card Number","Name","Market Price","Condition",'
        '"Language","Printing","Quantity","Remarks","*ConditionID"' + chr(10)
        + '"Pokemon","Chilling Reign","004/198","Ledyba","0.30","NM",'
          '"English","Normal",2,"Bin A-1","4000"' + chr(10)
        + '"Pokemon","Chilling Reign","004/198","Ledyba","0.30","NM",'
          '"English","Normal",1,"Bin B-7","4000"' + chr(10)
        + '"Pokemon","Chilling Reign","006/198","Heracross","0.40","NM",'
          '"English","Normal",2,"Bin A-2","4000"' + chr(10)
    )

    def _seed_live_dump(self, live_qty=2):
        """
        Catalogue the dump and put both cards live on eBay with labels.

        ``live_qty`` is what eBay is believed to hold, and it defaults to
        stock rather than zero because a live listing at zero is not a
        listing anybody is selling from. It used to default to 0, which
        only worked for the sold-out tests because the module recorded a
        non-zero *pending* request alongside it; with that gone, a
        believed quantity of zero correctly means "nothing to pull".
        """
        process_batch_csv(self.FULL_DUMP, self.db, source_name="seed.csv")
        ids = {r["product_name"]: r["manifest_id"]
               for r in self.db.export_all_manifest()}
        for name, mid in ids.items():
            self.db.upsert_variation(
                mid, "227511361186", live_qty,
                custom_label=f"{mid}-Bin_A-1"
            )
        return ids

    def test_rows_for_one_card_sum_within_a_file_then_add_across_files(self):
        """
        Two halves of the same rule, and they pull in opposite directions.

        A card held in two bins appears on two rows of one file. The bin is
        not part of a card's identity, so those rows are the same card and
        their quantities sum: 2 + 1 is 3, not two cards.

        Across files they add, because each file is a separate batch of newly
        scanned cards. Uploading the same file twice therefore doubles its
        stock, which is what the duplicate fingerprint exists to prevent --
        note the force=True here, which is the deliberate override.
        """
        ids = self._seed_live_dump()

        first = {r["product_name"]: r["quantity"]
                 for r in self.db.export_all_manifest()}
        self.assertEqual(first["Ledyba"], 3, "2 + 1 summed across two bins")
        self.assertEqual(first["Heracross"], 2)

        process_batch_csv(self.FULL_DUMP, self.db, source_name="again.csv",
                          force=True)
        second = {r["product_name"]: r["quantity"]
                  for r in self.db.export_all_manifest()}
        self.assertEqual(second["Ledyba"], 6, "a forced repeat adds again")
        self.assertEqual(second["Heracross"], 4)

        # Module A corrects the catalogue and must leave eBay's own figure
        # alone: only an Active Listings sync, or a confirmed push, may move
        # that. The difference between the two is what the draft is.
        variation = self.db.get_variation(ids["Ledyba"])
        self.assertEqual(variation["last_known_qty"], 2,
                         "ingesting a batch must not claim eBay was updated")
    def test_a_card_absent_from_an_upload_is_left_alone(self):
        """
        An upload says nothing about the cards it omits.

        It is one batch of newly scanned cards, not a picture of the
        shelves, so an omission cannot mean "sold out". Module A used to
        infer exactly that and zero the card, which was right only while
        the upload was a full dump. Sales come from eBay orders now.
        """
        ids = self._seed_live_dump()
        smaller = chr(10).join(self.FULL_DUMP.splitlines()[:3]) + chr(10)
        before = {r["product_name"]: r["quantity"]
                  for r in self.db.export_all_manifest()}
        res = process_batch_csv(smaller, self.db, source_name="d1.csv")

        after = {r["product_name"]: r["quantity"]
                 for r in self.db.export_all_manifest()}
        self.assertEqual(after["Heracross"], before["Heracross"],
                         "an omitted card must not be zeroed")
        self.assertEqual(
            self.db.get_variation(ids["Heracross"])["last_known_qty"], 2,
            "and eBay's own figure is never touched here",
        )
        self.assertNotIn("SOLD OUT",
                         chr(10).join(lg["message"] for lg in res["logs"]))

    def test_sync_records_the_ebay_custom_label(self):
        """
        Module B is the only authoritative source of the bin suffix, since a
        card's identity does not include it.
        """
        ids = self._seed_live_dump()
        mid = ids["Ledyba"]
        report = (
            "Item number,Custom label,Available quantity,Title" + chr(10)
            + f"227511361186,{mid}-Bin_ZZ-9,5,Chilling Reign: Pick Your Card"
            + chr(10)
        )
        sync_active_listings_csv(report, self.db)
        self.assertEqual(
            self.db.get_variation(mid)["custom_label"], f"{mid}-Bin_ZZ-9"
        )

        # A caller that only knows the quantity must not erase it.
        self.db.upsert_variation(mid, "227511361186", 4)
        self.assertEqual(
            self.db.get_variation(mid)["custom_label"], f"{mid}-Bin_ZZ-9"
        )

    # -- stats: cards vs copies --------------------------------------------

    def test_stats_separates_cards_from_copies(self):
        """
        Four figures in two different units. Confusing a count of cards with
        a sum of copies is exactly what made the dashboard unreadable.
        """
        # Ledyba x3 across two bins, Heracross x2 -> 2 cards, 5 copies.
        process_batch_csv(self.FULL_DUMP, self.db, source_name="s.csv")
        stats = self.db.get_stats()
        self.assertEqual(stats["total_cards"], 2, "kinds of card")
        self.assertEqual(stats["total_on_hand"], 5, "physical copies")

        # Nothing linked to eBay yet.
        self.assertEqual(stats["active_listings"], 0)
        self.assertEqual(stats["total_stock"], 0)

        ids = {r["product_name"]: r["manifest_id"]
               for r in self.db.export_all_manifest()}
        self.db.upsert_variation(ids["Ledyba"], "227511361186", 3)
        self.db.upsert_variation(ids["Heracross"], "227511361186", 1)

        stats = self.db.get_stats()
        self.assertEqual(stats["active_listings"], 2, "cards linked to eBay")
        self.assertEqual(stats["total_stock"], 4, "copies eBay reports")
        # On hand is unchanged by what eBay says; the two are independent.
        self.assertEqual(stats["total_on_hand"], 5)

    def test_stats_ignores_unlinked_rows_in_both_ebay_figures(self):
        """
        The count and the sum must describe the same rows, or the ribbon shows
        copies belonging to cards it is not counting.
        """
        process_batch_csv(self.FULL_DUMP, self.db, source_name="s.csv")
        ids = {r["product_name"]: r["manifest_id"]
               for r in self.db.export_all_manifest()}
        self.db.upsert_variation(ids["Ledyba"], "227511361186", 3)

        # A row whose listing link was cleared still carries a quantity.
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO ebay_variations "
                "(manifest_id, ebay_parent_id, last_known_qty) VALUES (?, '', ?)",
                (ids["Heracross"], 99),
            )
            conn.commit()

        stats = self.db.get_stats()
        self.assertEqual(stats["active_listings"], 1)
        self.assertEqual(stats["total_stock"], 3,
                         "the unlinked row's 99 copies must not be counted")

    def test_stats_on_an_empty_catalog_are_zero_not_none(self):
        """SUM over no rows is NULL in SQLite and would render as blank."""
        stats = self.db.get_stats()
        self.assertEqual(stats["total_cards"], 0)
        self.assertEqual(stats["total_on_hand"], 0)
        self.assertEqual(stats["active_listings"], 0)
        self.assertEqual(stats["total_stock"], 0)

    # -- session-scoped connections ----------------------------------------

    def test_session_reuses_one_connection_without_changing_results(self):
        """
        The session is a performance change only: it must not alter what a
        batch concludes, and it must leave the data committed.
        """
        import sqlite3 as _sqlite3
        real = _sqlite3.connect
        count = {"n": 0}

        def counting(*a, **kw):
            count["n"] += 1
            return real(*a, **kw)

        without = process_batch_csv(self.FULL_DUMP, self.db, source_name="a.csv")

        fresh = Database(os.path.join(self.temp_dir.name, "session.db"))
        _sqlite3.connect = counting
        try:
            with fresh.session():
                withsess = process_batch_csv(self.FULL_DUMP, fresh,
                                             source_name="a.csv")
        finally:
            _sqlite3.connect = real

        for key in ("staged_card_count", "staged_listing_count",
                    "new_catalog_count", "skipped_count", "parsed_rows"):
            self.assertEqual(without[key], withsess[key], key)
        self.assertEqual(count["n"], 1, "one connection for the whole run")

        # Reopening proves the work was committed, not lost on close.
        reopened = Database(fresh.db_path)
        self.assertEqual(reopened.get_stats()["total_on_hand"], 5)
    def test_session_is_reentrant(self):
        """Nesting must not close the outer connection early."""
        with self.db.session():
            with self.db.session():
                self.db.set_listing_settings({"category_id": "1"})
            # Still usable after the inner block exits.
            self.assertEqual(self.db.get_listing_setting("category_id"), "1")
        self.assertEqual(self.db.get_listing_setting("category_id"), "1")

    def test_apply_pricing_rules_is_pure(self):
        """
        Pricing behaviour must be testable without a database, since the batch
        path now matches against an already-loaded rule set.
        """
        rules = self.db.get_pricing_rules()
        for base in (0.10, 0.30, 0.75, 4.50, 0.0):
            self.assertEqual(
                apply_pricing_rules(rules, base),
                self.db.calculate_price(base),
                f"base={base}",
            )
    # -- Module A must not touch eBay's reported figures -------------------

    def test_module_a_never_moves_the_ebay_mirror(self):
        """
        The dashboard's "On eBay" figures come only from an Active Listings
        sync. Generating a CSV is not evidence that eBay was updated, and
        pretending otherwise hides the drift the two columns exist to show.
        """
        ids = self._seed_live_dump()
        mid = ids["Ledyba"]

        # Pretend eBay reported 7 at the last sync.
        self.db.upsert_variation(mid, "227511361186", 7,
                                 custom_label=f"{mid}-Bin_A-1")
        before = self.db.get_stats()

        for run in ("first", "second"):
            process_batch_csv(self.FULL_DUMP, self.db,
                              source_name=f"{run}.csv", force=True)
            after = self.db.get_stats()
            self.assertEqual(
                after["total_stock"], before["total_stock"],
                f"the {run} ingest moved Copies on eBay",
            )
            self.assertEqual(
                after["active_listings"], before["active_listings"],
                f"the {run} ingest moved Cards on eBay",
            )
            self.assertEqual(
                self.db.get_variation(mid)["last_known_qty"], 7,
                f"the {run} ingest overwrote eBay's reported quantity",
            )

        # On Hand did change, which is Module A's business.
        self.assertGreater(self.db.get_stats()["total_on_hand"], 0)

    def test_sync_zeroes_cards_missing_from_the_report(self):
        """
        A linked card absent from an Active Listings report is not live on
        eBay any more. Keeping its old figure would report stock eBay does not
        have -- the same failure as Module A writing the column speculatively.
        """
        ids = self._seed_live_dump()
        header = "Item number,Custom label,Available quantity,Title"
        both = (header + chr(10)
                + f"227511361186,{ids['Ledyba']},3,Pick Your Card" + chr(10)
                + f"227511361186,{ids['Heracross']},2,Pick Your Card" + chr(10))
        sync_active_listings_csv(both, self.db)
        self.assertEqual(self.db.get_stats()["total_stock"], 5)

        # Heracross sold out and dropped off the report.
        only_ledyba = (header + chr(10)
                       + f"227511361186,{ids['Ledyba']},3,Pick Your Card" + chr(10))
        res = sync_active_listings_csv(only_ledyba, self.db)

        self.assertEqual(res["delisted_count"], 1)
        self.assertEqual(
            self.db.get_variation(ids["Heracross"])["last_known_qty"], 0
        )
        self.assertEqual(self.db.get_stats()["total_stock"], 3)
        self.assertTrue(any(
            "no longer has it" in lg["message"] for lg in res["logs"]
        ))

        # Idempotent: already zero, so nothing more to report.
        again = sync_active_listings_csv(only_ledyba, self.db)
        self.assertEqual(again["delisted_count"], 0)

    def test_sync_does_not_zero_cards_it_has_never_linked(self):
        """A card with no listing link cannot be delisted from one."""
        process_batch_csv(self.FULL_DUMP, self.db, source_name="d.csv")
        header = "Item number,Custom label,Available quantity,Title"
        res = sync_active_listings_csv(header + chr(10), self.db)
        self.assertEqual(res["delisted_count"], 0)
        self.assertEqual(self.db.get_stats()["total_on_hand"], 5,
                         "on-hand is Module A's and must be untouched")
    # -- unchanged Revise rows are suppressed ------------------------------

    def _synced_dump_fixture(self, price_column=True):
        """
        Catalogue the dump, link it, and let a sync teach us eBay's quantity
        and price -- the two things an unchanged check needs.
        """
        process_batch_csv(self.FULL_DUMP, self.db, source_name="seed.csv")
        ids = {r["product_name"]: r["manifest_id"]
               for r in self.db.export_all_manifest()}
        for mid in ids.values():
            self.db.upsert_variation(mid, "227511361186", 0,
                                     custom_label=mid + "-C-1")

        # The quantities the dump produces, and the prices the shared rules
        # compute for its market prices.
        live = (("Ledyba", 3, "2.49"), ("Heracross", 2, "2.49"))
        if price_column:
            header = ("Item number,Custom label,Available quantity,"
                      "Current price,Title")
            rows = ["227511361186," + ids[n] + "-C-1," + str(q) + ",$" + p + ",Pick"
                    for n, q, p in live]
        else:
            header = "Item number,Custom label,Available quantity,Title"
            rows = ["227511361186," + ids[n] + "-C-1," + str(q) + ",Pick"
                    for n, q, _ in live]
        sync_active_listings_csv(header + chr(10) + chr(10).join(rows) + chr(10),
                                 self.db)
        return ids

    def test_parse_price_tolerates_report_formatting(self):
        """Report prices arrive with symbols, separators and currency codes."""
        from tcg_engine.csvtools import parse_price
        cases = [("$1.99", 1.99), ("1,299.00", 1299.00), ("4.50 USD", 4.50),
                 ("GBP 4.50", 4.50), ("", 0.0), ("N/A", 0.0), (None, 0.0),
                 ("2.49", 2.49), ("-", 0.0), ("  3.00  ", 3.00)]
        for raw, want in cases:
            self.assertAlmostEqual(parse_price(raw), want, places=2,
                                   msg=repr(raw))

    # -- skipped rows must never look like sold-out cards -------------------

    # The ConditionID column is present but empty on every row. That keeps
    # this exercising the reconciliation guard: an export missing the column
    # entirely is now refused up front as the wrong SortSwift export, which
    # would never reach the per-row skips this test is about.
    NO_CONDITION_ID_DUMP = (
        '"Game","Set","Card Number","Name","Market Price","Condition",'
        '"Language","Printing","Quantity","Remarks","*ConditionID"' + chr(10)
        + '"Pokemon","Chilling Reign","004/198","Ledyba","0.30","NM",'
          '"English","Normal",2,"Bin A-1",""' + chr(10)
        + '"Pokemon","Chilling Reign","006/198","Heracross","0.40","NM",'
          '"English","Normal",2,"Bin A-2",""' + chr(10)
    )

    def test_a_file_that_parsed_nothing_changes_nothing(self):
        """
        The shape of a real report: a SortSwift export with no *ConditionID.

        Every row is skipped. That used to be the most dangerous input there
        is -- a skipped row was indistinguishable from a card the dump
        omitted, and an omitted card was zeroed, so a file that failed
        entirely would have zeroed the whole catalogue and asked eBay to
        delist the store. Nothing infers a sale from an omission any more, so
        the hazard is gone, but the guarantee is still worth pinning: a file
        that yielded no usable row must leave every figure exactly as it was.
        """
        ids = self._seed_live_dump()
        for mid in ids.values():
            self.db.upsert_variation(mid, "227511361186", 3,
                                     custom_label=mid + "-C-1",
                                     last_known_price=2.49)
        before = {r["manifest_id"]: r["quantity"]
                  for r in self.db.export_all_manifest()}

        res = process_batch_csv(self.NO_CONDITION_ID_DUMP, self.db,
                                source_name="no-condid.csv")

        self.assertEqual(res["parsed_rows"], 0)
        self.assertEqual(res["skipped_count"], 2)

        self.assertEqual(
            {r["manifest_id"]: r["quantity"]
             for r in self.db.export_all_manifest()},
            before, "no catalogued quantity may move",
        )
        for mid in ids.values():
            self.assertEqual(self.db.get_variation(mid)["last_known_qty"], 3)

        messages = chr(10).join(lg["message"] for lg in res["logs"])
        self.assertIn("Not one row", messages)
    def test_find_column_can_skip_present_but_empty_columns(self):
        """
        On a variation listing the child rows carry "Start price" and leave
        "Current price" blank; the parent row is the other way round. Without
        skip_blank the first candidate wins with an empty string.
        """
        from tcg_engine.csvtools import find_column

        child = {"Start price": "1.99", "Current price": ""}
        parent = {"Start price": "1.99", "Current price": "2.49"}
        order = ["Current price", "Start price"]

        # Default behaviour is unchanged: first present column wins.
        self.assertEqual(find_column(child, order), "")
        self.assertEqual(find_column(parent, order), "2.49")

        self.assertEqual(find_column(child, order, skip_blank=True), "1.99")
        self.assertEqual(find_column(parent, order, skip_blank=True), "2.49")

        # Whitespace counts as blank, and exhausting the candidates is None.
        self.assertIsNone(
            find_column({"Current price": "   "}, order, skip_blank=True))

    def test_sync_learns_a_variation_price_from_start_price(self):
        """
        The real report leaves Current price empty on child rows, so reading
        it would learn nothing and silently disable no-op suppression.
        """
        header = ("Item number,Title,Variation details,Custom label (SKU),"
                  "Available quantity,Start price,Current price")
        ids = self._seed_live_dump()
        mid = ids["Ledyba"]
        report = chr(10).join([
            header,
            f'"227511361186",Pick Your Card,"Card=Ledyba (004/198)",,"3",1.99,1.99',
            f'"227511361186",Pick Your Card,"Card=Ledyba (004/198)",{mid},"3",2.49,',
        ]) + chr(10)

        res = sync_active_listings_csv(report, self.db)
        self.assertEqual(res["skipped_parent_count"], 1,
                         "a blank custom label must still mean parent")
        variation = self.db.get_variation(mid)
        self.assertEqual(variation["last_known_qty"], 3)
        self.assertEqual(variation["last_known_price"], 2.49,
                         "the price must come from Start price")

    # -- condition multipliers ---------------------------------------------

    def test_condition_aliases_fold_onto_canonical_keys(self):
        """Each export spells the grades differently; all must resolve."""
        for raw, want in (("NM", "NM"), ("nm", "NM"), ("Near Mint", "NM"),
                          ("Near mint or better", "NM"), ("Mint", "NM"),
                          ("LP", "LP"), ("Lightly Played", "LP"),
                          ("Excellent", "LP"),
                          ("Lightly played (Excellent)", "LP"),
                          ("MP", "MP"), ("Very Good", "MP"), ("VG", "MP"),
                          ("HP", "HP"), ("Poor", "HP"),
                          ("Damaged", "D"), ("dmg", "D")):
            self.assertEqual(normalize_condition_key(raw), want, raw)

        # An unrecognised grade must resolve to nothing, not to mint.
        for raw in ("Gem Mint 10", "PSA 9", "", None, "   "):
            self.assertEqual(normalize_condition_key(raw), "", repr(raw))

    def test_unknown_grade_is_not_silently_priced_as_mint(self):
        """
        Defaulting an unrecognised grade to 1.0 would over-price a played
        card while looking perfectly normal, so the multiplier comes back as
        None and the caller can say so.
        """
        mults = {m["condition_key"]: m["multiplier"]
                 for m in self.db.get_condition_multipliers()}

        price, factor = apply_condition_multiplier(10.0, "PSA 9", mults)
        self.assertEqual(price, 10.0)
        self.assertIsNone(factor)

        price, factor = apply_condition_multiplier(10.0, "LP", mults)
        self.assertAlmostEqual(price, 8.50, places=2)
        self.assertAlmostEqual(factor, 0.85, places=2)

    def test_grade_discount_shifts_the_tier(self):
        """
        The discount is applied before the tiers on purpose: a played card
        should land in a cheaper band, not the one its mint price implies.
        """
        mults = {m["condition_key"]: m["multiplier"]
                 for m in self.db.get_condition_multipliers()}
        rules = self.db.get_pricing_rules()

        # 0.30 mint sits in the 0.25-0.50 tier -> fixed 2.49.
        nm, _ = apply_condition_multiplier(0.30, "NM", mults)
        self.assertEqual(apply_pricing_rules(rules, nm)[0], 2.49)

        # x0.5 puts the same card at 0.15, which is the 0.00-0.25 tier.
        hp, _ = apply_condition_multiplier(0.30, "HP", mults)
        self.assertAlmostEqual(hp, 0.15, places=2)
        self.assertEqual(apply_pricing_rules(rules, hp)[0], 1.99,
                         "the discount should move it into the cheaper tier")

    def test_multipliers_are_per_user_like_pricing_rules(self):
        """All-or-nothing, so grades cannot be priced by two policies at once."""
        shared = self.db.get_condition_multipliers()
        self.assertFalse(self.db.has_own_condition_multipliers(7))
        self.assertEqual(self.db.get_condition_multipliers(user_id=7), shared)

        self.db.set_condition_multipliers(
            [{"condition_key": "NM", "multiplier": 0.9, "label": "mine"}],
            user_id=7,
        )
        self.assertTrue(self.db.has_own_condition_multipliers(7))
        own = self.db.get_condition_multipliers(user_id=7)
        self.assertEqual([(m["condition_key"], m["multiplier"]) for m in own],
                         [("NM", 0.9)])
        self.assertEqual(self.db.get_condition_multipliers(), shared,
                         "the shared set must not move")

        self.db.reset_condition_multipliers(user_id=7)
        self.assertEqual(self.db.get_condition_multipliers(user_id=7), shared)

    def test_batch_discounts_a_played_card_and_reports_unknown_grades(self):
        """The whole path: dump -> grade discount -> tiers -> Add CSV."""
        header = ('"Game","Set","Card Number","Name","Market Price",'
                  '"Condition","Language","Printing","Quantity","Remarks",'
                  '"*ConditionID"')
        rows = [
            '"Pokemon","Chilling Reign","004/198","Mint Card","10.00","NM",'
            '"English","Normal",1,"C-1","4000"',
            '"Pokemon","Chilling Reign","005/198","Played Card","10.00","MP",'
            '"English","Normal",1,"C-1","4000"',
        ]
        dump = chr(10).join([header] + rows) + chr(10)

        process_batch_csv(dump, self.db, source_name="grades.csv")
        priced = {
            r["product_name"]: round(
                self.db.get_manifest_by_id(r["manifest_id"])["price"], 2)
            for r in self.db.export_all_manifest()
        }

        # $10 mint -> markup_fixed +3.00 = 13.00; MP is x0.70 -> 7.00 -> 10.00.
        self.assertEqual(priced["Mint Card"], 13.00)
        self.assertEqual(priced["Played Card"], 10.00)

        # An unrecognised grade is reported rather than quietly priced as mint.
        odd = chr(10).join([header,
            '"Pokemon","Chilling Reign","006/198","Slabbed","10.00","PSA 9",'
            '"English","Normal",1,"C-1","4000"']) + chr(10)
        res2 = process_batch_csv(odd, self.db, source_name="odd.csv")
        self.assertTrue(
            any("No condition multiplier is configured" in lg["message"]
                for lg in res2["logs"]),
            "an unpriced grade must be surfaced",
        )

    def test_an_explicit_ebay_price_is_not_discounted_twice(self):
        """
        An eBay Price column is a per-card override the operator has already
        decided on; applying a grade discount to it would second-guess them.
        """
        header = ('"Game","Set","Card Number","Name","Market Price",'
                  '"eBay Price","Condition","Language","Printing","Quantity",'
                  '"Remarks","*ConditionID"')
        dump = chr(10).join([header,
            '"Pokemon","Chilling Reign","004/198","Override","10.00","4.44",'
            '"MP","English","Normal",1,"C-1","4000"']) + chr(10)

        process_batch_csv(dump, self.db, source_name="override.csv")
        priced = {
            r["product_name"]: round(
                self.db.get_manifest_by_id(r["manifest_id"])["price"], 2)
            for r in self.db.export_all_manifest()
        }
        self.assertEqual(priced["Override"], 4.44,
                         "the override must survive the grade discount")

    # -- market price feed -------------------------------------------------

    PRICED_DUMP_HEADER = (
        '"Game","Set","Set Code","Card Number","Name","Market Price",'
        '"Condition","Language","Printing","Quantity","Remarks",'
        '"TCGplayer Id","*ConditionID"'
    )

    def _priced_row(self, name, num, tcg_id, printing, market="0.30",
                    cond="NM", qty=1):
        return (f'"Pokemon","Chilling Reign","SWSH06","{num}","{name}",'
                f'"{market}","{cond}","English","{printing}",{qty},"C-1",'
                f'{tcg_id},"4000"')

    def _fake_feed(self, prices, snapshot="SNAP-1", groups=None):
        """A stand-in for TCGCSV, shaped like the real responses."""
        import json as _json

        group_rows = groups if groups is not None else [
            {"groupId": 2807, "name": "Chilling Reign", "abbreviation": "SWSH06"},
        ]
        self.feed_calls = []

        def fetcher(path):
            self.feed_calls.append(path)
            if path.endswith("last-updated.txt"):
                return snapshot
            if path.endswith("/groups"):
                return _json.dumps({"success": True, "errors": [],
                                    "results": group_rows})
            if path.endswith("/prices"):
                return _json.dumps({"success": True, "errors": [],
                                    "results": prices})
            raise AssertionError(f"unexpected path {path}")

        return fetcher

    def test_prices_join_on_printing_not_product_alone(self):
        """
        TCGCSV returns a row per printing, and on real cards Normal and
        Reverse Holofoil differ several-fold. Joining on productId alone
        would price every reverse holo as a normal -- a silent underprice
        that looks entirely plausible.
        """
        dump = chr(10).join([
            self.PRICED_DUMP_HEADER,
            self._priced_row("Ledyba", "004/198", 241651, "Normal"),
            self._priced_row("Ledyba", "004/198", 241651, "Reverse Holofoil"),
        ]) + chr(10)
        process_batch_csv(dump, self.db, source_name="p.csv")

        fetcher = self._fake_feed([
            {"productId": 241651, "subTypeName": "Normal", "marketPrice": 0.13},
            {"productId": 241651, "subTypeName": "Reverse Holofoil",
             "marketPrice": 0.26},
        ])
        res = refresh_market_prices(self.db, fetcher=fetcher)
        self.assertEqual(res["updated"], 2)

        by_printing = {}
        with self.db.get_connection() as conn:
            for row in conn.execute(
                "SELECT printing, market_price FROM manifest"
            ):
                by_printing[row["printing"]] = row["market_price"]
        self.assertAlmostEqual(by_printing["Normal"], 0.13, places=2)
        self.assertAlmostEqual(by_printing["Reverse Holofoil"], 0.26, places=2)

    def test_a_missing_price_leaves_the_stored_one_alone(self):
        """
        The pricing rules multiply against this number, so a missing price
        must mean "unknown", never zero -- a silent zero would reprice the
        catalogue to the floor.
        """
        dump = chr(10).join([
            self.PRICED_DUMP_HEADER,
            self._priced_row("Gloves", "133/198", 241823, "Normal",
                             market="0.30"),
        ]) + chr(10)
        process_batch_csv(dump, self.db, source_name="p.csv")

        fetcher = self._fake_feed([
            {"productId": 241823, "subTypeName": "Normal", "marketPrice": None},
        ])
        res = refresh_market_prices(self.db, fetcher=fetcher)
        self.assertEqual(res["updated"], 0)
        self.assertEqual(res["unmatched"], 1)

        with self.db.get_connection() as conn:
            price = conn.execute(
                "SELECT market_price FROM manifest"
            ).fetchone()["market_price"]
        self.assertAlmostEqual(price, 0.30, places=2,
                               msg="the dump's price must survive")

    def test_refresh_is_skipped_when_the_snapshot_is_unchanged(self):
        """
        TCGCSV publishes once a day and asks for at most one sync per 24
        hours, so an already-current day must cost one request, not one per
        set.
        """
        dump = chr(10).join([
            self.PRICED_DUMP_HEADER,
            self._priced_row("Ledyba", "004/198", 241651, "Normal"),
        ]) + chr(10)
        process_batch_csv(dump, self.db, source_name="p.csv")
        prices = [{"productId": 241651, "subTypeName": "Normal",
                   "marketPrice": 0.13}]

        first = refresh_market_prices(self.db, fetcher=self._fake_feed(prices))
        self.assertFalse(first["skipped"])
        self.assertGreater(len(self.feed_calls), 1)

        second = refresh_market_prices(self.db, fetcher=self._fake_feed(prices))
        self.assertTrue(second["skipped"])
        self.assertEqual(self.feed_calls, ["/last-updated.txt"])

        # force overrides the gate, for when a price looks wrong.
        third = refresh_market_prices(self.db, fetcher=self._fake_feed(prices),
                                      force=True)
        self.assertFalse(third["skipped"])

    def test_an_unresolvable_set_code_is_reported_not_guessed(self):
        """A set we cannot map keeps its prices, and says so."""
        dump = chr(10).join([
            self.PRICED_DUMP_HEADER,
            self._priced_row("Ledyba", "004/198", 241651, "Normal"),
        ]).replace('"SWSH06"', '"NOSUCH"') + chr(10)
        process_batch_csv(dump, self.db, source_name="p.csv")

        res = refresh_market_prices(self.db, fetcher=self._fake_feed([]))
        self.assertEqual(res["updated"], 0)
        self.assertTrue(any("does not match any TCGCSV set" in lg["message"]
                            for lg in res["logs"]))

    def test_a_failed_fetch_changes_nothing(self):
        """An unreachable feed must leave every stored price as it was."""
        dump = chr(10).join([
            self.PRICED_DUMP_HEADER,
            self._priced_row("Ledyba", "004/198", 241651, "Normal"),
        ]) + chr(10)
        process_batch_csv(dump, self.db, source_name="p.csv")

        def broken(path):
            raise PriceFeedError("network down")

        with self.assertRaises(PriceFeedError):
            refresh_market_prices(self.db, fetcher=broken)

        with self.db.get_connection() as conn:
            price = conn.execute(
                "SELECT market_price FROM manifest"
            ).fetchone()["market_price"]
        self.assertAlmostEqual(price, 0.30, places=2)

    def test_price_history_is_kept_rather_than_overwritten(self):
        """A surprising reprice is only diagnosable with the prior value."""
        dump = chr(10).join([
            self.PRICED_DUMP_HEADER,
            self._priced_row("Ledyba", "004/198", 241651, "Normal"),
        ]) + chr(10)
        process_batch_csv(dump, self.db, source_name="p.csv")
        mid = self.db.get_inventory(limit=5)[0]["manifest_id"]

        for snapshot, price in (("SNAP-1", 0.13), ("SNAP-2", 0.19)):
            refresh_market_prices(
                self.db,
                fetcher=self._fake_feed(
                    [{"productId": 241651, "subTypeName": "Normal",
                      "marketPrice": price}],
                    snapshot=snapshot,
                ),
            )

        history = self.db.get_price_history(mid)
        self.assertEqual([round(h["market_price"], 2) for h in history],
                         [0.19, 0.13], "newest first, nothing lost")
        self.assertIn("SNAP-2", history[0]["source"])

class StockImageFallbackTests(unittest.TestCase):
    """
    A card with no scan of its own still has a picture to show.

    The SortSwift export has always carried a generic catalogue photo per
    card, and the ingest has always parsed it -- but the only thing that ever
    read it was the Add-file row builder, which no longer exists. So an
    unscanned card showed an empty frame on the dashboard and went to eBay
    with no photograph at all, which is worse than a generic one.
    """

    # Three cards: one with only the generic catalogue photo, one with both,
    # one with neither.
    EXPORT = (
        "Set,Card Number,Name,Condition,Language,Printing,Quantity,"
        "Remarks,CDN Image,Stock Image,Price,*ConditionID\n"
        "Base Set,025/102,Pikachu,NM,EN,Normal,1,No Remark,,"
        "https://stock/pika.jpg,0.50,4000\n"
        "Base Set,004/102,Charizard,NM,EN,Normal,1,No Remark,"
        "https://scan/zard.jpg,https://stock/zard.jpg,9.99,4000\n"
        "Base Set,013/102,Grubbin,NM,EN,Normal,1,No Remark,,,0.20,4000\n"
    )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "stock.db"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_the_exports_stock_image_column_reaches_the_catalogue(self):
        process_batch_csv(self.EXPORT, self.db)

        by_name = {
            card["product_name"]: card for card in self.db.get_inventory()
        }
        self.assertEqual(
            by_name["Pikachu"]["stock_image"], "https://stock/pika.jpg"
        )
        # The two are kept apart on purpose. A scan shows the copy actually
        # being sold, so "have I photographed this one yet" has to stay
        # answerable -- filling the stock photo into cdn_image would make
        # every card look photographed.
        self.assertEqual(by_name["Pikachu"]["cdn_image"], "")

    def test_a_scan_wins_and_a_stock_photo_stands_in_for_a_missing_one(self):
        process_batch_csv(self.EXPORT, self.db)

        resolved = {
            card["product_name"]: card["image_url"]
            for card in self.db.get_inventory()
        }
        self.assertEqual(resolved["Charizard"], "https://scan/zard.jpg")
        self.assertEqual(resolved["Pikachu"], "https://stock/pika.jpg")
        # Neither available is still neither. The caller renders a
        # placeholder rather than a broken image.
        self.assertEqual(resolved["Grubbin"], "")

    def test_the_same_answer_reaches_planning_and_the_push(self):
        """
        Display and the push must not disagree about a card's picture, or a
        listing goes up without the photo the drafts page showed.
        """
        process_batch_csv(self.EXPORT, self.db)

        for cards in (
            self.db.get_inventory(),
            self.db.get_cards_for_planning(),
        ):
            resolved = {c["product_name"]: c["image_url"] for c in cards}
            self.assertEqual(resolved["Pikachu"], "https://stock/pika.jpg")
            self.assertEqual(resolved["Charizard"], "https://scan/zard.jpg")

    def test_a_stock_photo_is_backfilled_onto_a_card_catalogued_without_one(self):
        """
        Cards catalogued before this column existed have no stock photo
        stored. A later upload carrying one fills it in, so the fallback
        arrives without anyone having to re-catalogue anything.
        """
        manifest_id, _, _ = self.db.get_or_create_manifest(
            "Pikachu", "Base Set", "NM", "Normal"
        )
        self.assertIsNone(
            self.db.get_manifest_by_id(manifest_id)["stock_image"]
        )

        self.db.get_or_create_manifest(
            "Pikachu", "Base Set", "NM", "Normal",
            stock_image="https://stock/pika.jpg",
        )
        self.assertEqual(
            self.db.get_manifest_by_id(manifest_id)["stock_image"],
            "https://stock/pika.jpg",
        )


class HandEditedRemarkTests(unittest.TestCase):
    """
    Who owns a card's bin, and what happens when the two disagree.

    Every other export field is a backfill -- written only when ours is empty
    -- but remarks was an unconditional overwrite, on the reasoning that
    SortSwift is where cards are scanned and binned. That made editing it on
    the dashboard worse than useless: the edit would appear to work and then
    silently revert on the next upload mentioning the card.
    """

    # "Bin B-3" in the export, so a hand edit to something else conflicts.
    EXPORT = (
        "Set,Card Number,Name,Condition,Language,Printing,Quantity,"
        "Remarks,CDN Image,Stock Image,Price,*ConditionID\n"
        "Base Set,025/102,Pikachu,NM,EN,Normal,1,Bin B-3,,,0.50,4000\n"
    )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "remarks.db"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def catalogue(self):
        process_batch_csv(self.EXPORT, self.db)
        return self.db.get_inventory()[0]["manifest_id"]

    def test_the_export_owns_the_bin_until_somebody_edits_it(self):
        manifest_id = self.catalogue()
        card = self.db.get_manifest_by_id(manifest_id)
        self.assertEqual(card["remarks"], "Bin B-3")
        self.assertIsNone(card["remarks_edited_at"])

        # A later export moving the card is still applied.
        process_batch_csv(
            self.EXPORT.replace("Bin B-3", "Bin C-9"), self.db
        )
        self.assertEqual(
            self.db.get_manifest_by_id(manifest_id)["remarks"], "Bin C-9"
        )

    def test_a_hand_set_bin_survives_the_next_upload(self):
        manifest_id = self.catalogue()
        result = self.db.set_manifest_remarks(manifest_id, "Bin A-12")
        self.assertEqual(result, {"previous": "Bin B-3", "current": "Bin A-12"})

        process_batch_csv(self.EXPORT, self.db, force=True)
        card = self.db.get_manifest_by_id(manifest_id)
        self.assertEqual(card["remarks"], "Bin A-12")
        self.assertIsNotNone(card["remarks_edited_at"])

    def test_the_upload_says_which_value_it_kept(self):
        """
        Both values are plausible and dropping either silently is how
        somebody ends up looking in the wrong box.
        """
        manifest_id = self.catalogue()
        self.db.set_manifest_remarks(manifest_id, "Bin A-12")

        result = process_batch_csv(self.EXPORT, self.db, force=True)
        messages = [entry["message"] for entry in result["logs"]]
        kept = [m for m in messages if "kept its bin" in m]
        self.assertEqual(len(kept), 1, messages)
        self.assertIn("Bin A-12", kept[0])
        self.assertIn("Bin B-3", kept[0])

    def test_an_agreeing_upload_says_nothing(self):
        """A notice on every upload for a bin nobody is fighting over is noise."""
        manifest_id = self.catalogue()
        self.db.set_manifest_remarks(manifest_id, "Bin B-3")

        result = process_batch_csv(self.EXPORT, self.db, force=True)
        self.assertEqual(
            [m for m in
             (entry["message"] for entry in result["logs"])
             if "kept its bin" in m],
            [],
        )

    def test_clearing_the_bin_hands_it_back_to_the_export(self):
        """
        Otherwise a card cleared by hand would be stuck empty forever, with
        no way to let SortSwift fill it in again.
        """
        manifest_id = self.catalogue()
        self.db.set_manifest_remarks(manifest_id, "Bin A-12")
        self.db.set_manifest_remarks(manifest_id, "")

        card = self.db.get_manifest_by_id(manifest_id)
        self.assertIsNone(card["remarks"])
        self.assertIsNone(card["remarks_edited_at"])

        process_batch_csv(self.EXPORT, self.db, force=True)
        self.assertEqual(
            self.db.get_manifest_by_id(manifest_id)["remarks"], "Bin B-3"
        )

    def test_sortswifts_own_word_for_empty_is_treated_as_empty(self):
        """
        The export writes "No Remark" for a card with none and the ingest
        already reads that as blank. Typing it here has to mean the same, or
        a card ends up binned in the literal words.
        """
        manifest_id = self.catalogue()
        self.db.set_manifest_remarks(manifest_id, "No Remark")
        card = self.db.get_manifest_by_id(manifest_id)
        self.assertIsNone(card["remarks"])
        self.assertIsNone(card["remarks_edited_at"])

    def test_an_unknown_card_is_reported_rather_than_created(self):
        self.assertIsNone(self.db.set_manifest_remarks("ID9999", "Bin A-12"))


class ConditionCorrectionTests(unittest.TestCase):
    """
    Correcting a grade the export got wrong.

    Condition is passed through verbatim and is part of a card's identity, so
    a wrong grade could not be fixed at all: a corrected export creates a
    second card rather than changing the first. The drafts page's Listing
    dropdown was used instead, which puts two grades on one listing -- and
    eBay applies one ConditionID to an entire listing.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "cond.db"))
        self.db.insert_manifest(
            "ID1001", "Iono's Wattrel", "ME: Ascended Heroes", "LP", "Normal"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_a_grade_is_corrected_and_stored_verbatim(self):
        result = self.db.set_manifest_condition("ID1001", "  NM  ")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["previous"], "LP")
        self.assertEqual(result["current"], "NM")
        self.assertTrue(result["changed"])
        self.assertEqual(
            self.db.get_manifest_by_id("ID1001")["condition"], "NM"
        )

    def test_setting_the_same_grade_changes_nothing(self):
        result = self.db.set_manifest_condition("ID1001", "lp")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["changed"])
        # And the stored spelling is left as it was rather than re-cased.
        self.assertEqual(
            self.db.get_manifest_by_id("ID1001")["condition"], "LP"
        )

    def test_a_collision_is_refused_and_names_the_other_card(self):
        """
        Merging would have to reconcile two stock counts and possibly two
        live eBay links, and getting that wrong destroys a listing's link to
        its card. So it is a refusal with an answer, not a guess.
        """
        self.db.insert_manifest(
            "ID1002", "Iono's Wattrel", "ME: Ascended Heroes", "NM", "Normal"
        )
        self.db.set_manifest_quantity("ID1002", 7)

        result = self.db.set_manifest_condition("ID1001", "NM")
        self.assertEqual(result["status"], "twin")
        self.assertEqual(result["twin_id"], "ID1002")
        self.assertEqual(result["twin_quantity"], 7)
        self.assertEqual(
            self.db.get_manifest_by_id("ID1001")["condition"], "LP",
            "a refused change must leave the card alone",
        )

    def test_a_different_printing_is_not_a_collision(self):
        # Printing is part of the identity too, so a reverse holo at NM is a
        # different card and does not stand in the way.
        self.db.insert_manifest(
            "ID1002", "Iono's Wattrel", "ME: Ascended Heroes", "NM",
            "Reverse Holofoil",
        )
        result = self.db.set_manifest_condition("ID1001", "NM")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["changed"])

    def test_a_card_ebay_already_holds_is_changed_with_a_warning(self):
        """
        The live listing still states the old grade in its title and its
        condition descriptor, and a variation cannot be moved between
        listings -- so this is a fact the caller has to surface, not a
        reason to refuse.
        """
        self.db.upsert_variation("ID1001", "227523705068", 4,
                                 custom_label="ID1001")
        result = self.db.set_manifest_condition("ID1001", "NM")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["was_live"])

    def test_a_card_not_on_ebay_carries_no_such_warning(self):
        result = self.db.set_manifest_condition("ID1001", "NM")
        self.assertFalse(result["was_live"])

    def test_an_unknown_card_is_reported_rather_than_created(self):
        self.assertEqual(
            self.db.set_manifest_condition("ID9999", "NM"),
            {"status": "missing"},
        )

    def test_the_spellings_already_in_use_are_reported(self):
        """
        A catalogue can legitimately hold "NM" and "Near Mint" side by side,
        because the grade is whatever the export that catalogued the card
        used. The dialog offers those too, so it never forces a card onto a
        new spelling -- which would change its identity and leave the next
        upload re-creating the original.
        """
        self.db.insert_manifest(
            "ID1002", "Charizard", "Base Set", "Near Mint", "Holofoil"
        )
        self.db.insert_manifest(
            "ID1003", "Blastoise", "Base Set", "NM", "Normal"
        )
        self.assertEqual(
            self.db.get_conditions_in_use(), ["LP", "NM", "Near Mint"]
        )


class GradeVocabularyTests(unittest.TestCase):
    """
    Which grades a person may enter by hand.

    Not a new vocabulary and not applied to uploads -- an export's condition
    is still passed through verbatim. This is the narrower question of
    whether a value typed into this application can be listed at all, and it
    is answered by the two tables that already exist rather than by a third.
    """

    def test_the_offered_codes_all_resolve(self):
        for value, _label in CONDITION_CHOICES:
            self.assertTrue(
                condition_is_mappable(value),
                f"{value} is offered but cannot be listed",
            )

    def test_a_longhand_spelling_is_accepted(self):
        # The test is "can this be listed", not a house style: an older card
        # catalogued as "Near Mint" must stay saveable.
        for value in ("Near Mint", "near mint or better", "Excellent",
                      "Very Good", "Damaged", "  nm  "):
            self.assertTrue(condition_is_mappable(value), value)

    def test_an_unlistable_grade_is_rejected(self):
        for value in ("", None, "Sparkly", "PSA 9", "Gem Mint 10"):
            self.assertFalse(condition_is_mappable(value), repr(value))

    def test_the_bare_damaged_key_is_rejected(self):
        """
        "D" is the canonical *multiplier* key, and no descriptor table knows
        it -- so a card stored as "D" prices correctly and is then skipped at
        export. "DM" is the spelling that works end to end, and it is the one
        offered.
        """
        self.assertFalse(condition_is_mappable("D"))
        self.assertTrue(condition_is_mappable("DM"))
        self.assertIn("DM", [v for v, _ in CONDITION_CHOICES])
        self.assertNotIn("D", [v for v, _ in CONDITION_CHOICES])


class StockTargetTests(unittest.TestCase):
    """
    How many copies of a card to aim to hold, and what is still missing.

    A target, not a ceiling: nothing refuses stock above it and no listing is
    cut down to it. Its entire purpose is answering "which cards in this set
    do I still need".
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "target.db"))
        for manifest_id, name, set_name, held in (
            ("ID1001", "Charizard", "Base Set", 1),
            ("ID1002", "Pikachu", "Base Set", 4),
            ("ID1003", "Blastoise", "Base Set", 0),
            ("ID1004", "Snorlax", "Base Set", 9),
            ("ID2001", "Mewtwo", "Jungle", 2),
        ):
            self.db.insert_manifest(
                manifest_id, name, set_name, "Near Mint", "Normal",
                card_number="001/102",
            )
            self.db.set_manifest_quantity(manifest_id, held)

    def tearDown(self):
        self.temp_dir.cleanup()

    def rows(self, **kwargs):
        kwargs.setdefault("limit", 50)
        kwargs.setdefault(
            "target_default", self.db.get_target_quantity_default()
        )
        return {
            r["product_name"]: r
            for r in self.db.get_inventory(**kwargs)
        }

    def test_the_default_is_a_playset(self):
        """
        Four: the most a deck may run of one card, so it is the depth a
        singles seller stocks to.
        """
        self.assertEqual(self.db.get_target_quantity_default(), 4)
        self.assertEqual(self.rows()["Charizard"]["effective_target"], 4)
        self.assertEqual(self.rows()["Charizard"]["needed"], 3)

    def test_holding_more_than_the_target_needs_nothing(self):
        """
        Not a negative requirement, and not a cap: nine of a card we aim to
        hold four of is fine, and the answer to "how many to buy" is none.
        """
        row = self.rows()["Snorlax"]
        self.assertEqual(row["quantity"], 9)
        self.assertEqual(row["needed"], 0)

    def test_a_card_can_be_given_its_own_target(self):
        self.db.set_manifest_target_quantity("ID1004", 12)
        row = self.rows()["Snorlax"]
        self.assertEqual(row["target_quantity"], 12, "its own")
        self.assertEqual(row["effective_target"], 12)
        self.assertEqual(row["needed"], 3)

    def test_raising_the_account_default_moves_only_inherited_cards(self):
        """
        The default is resolved at query time rather than copied onto cards,
        so raising it reaches every card that never had its own -- and leaves
        the ones that do alone.
        """
        self.db.set_manifest_target_quantity("ID1004", 12)
        self.db.set_listing_settings(
            {"target_quantity_default": "6"}, user_id=SHARED_SCOPE
        )
        rows = self.rows()
        self.assertEqual(rows["Pikachu"]["effective_target"], 6)
        self.assertEqual(rows["Pikachu"]["needed"], 2, "held 4, wants 6")
        self.assertEqual(rows["Snorlax"]["effective_target"], 12, "kept")

    def test_a_target_of_zero_is_a_real_choice_and_not_the_default(self):
        """
        "Never restock this bulk common" has to be expressible, so zero and
        "no override" cannot be collapsed into one value.
        """
        self.db.set_manifest_target_quantity("ID1003", 0)
        row = self.rows()["Blastoise"]
        self.assertEqual(row["target_quantity"], 0)
        self.assertEqual(row["effective_target"], 0)
        self.assertEqual(row["needed"], 0)
        self.assertNotIn(
            "Blastoise",
            self.rows(below_target=True),
            "a card targeted at nothing is not short",
        )

    def test_clearing_an_override_returns_the_card_to_the_default(self):
        self.db.set_manifest_target_quantity("ID1003", 0)
        self.db.set_manifest_target_quantity("ID1003", None)
        row = self.rows()["Blastoise"]
        self.assertIsNone(row["target_quantity"])
        self.assertEqual(row["effective_target"], 4)
        self.assertEqual(row["needed"], 4)

    def test_the_restock_filter_answers_the_question_per_set(self):
        """
        "Which cards in this set do I still need" is the set filter and this
        one together.
        """
        short = self.rows(below_target=True, set_name="Base Set")
        self.assertEqual(
            sorted(short), ["Blastoise", "Charizard"],
            "Pikachu is at target and Snorlax is over it",
        )
        self.assertNotIn("Mewtwo", short, "a different set")

    def test_the_count_takes_the_same_filter_as_the_rows(self):
        """
        Otherwise a thirty-row restock list would page as though it had
        eight hundred rows.
        """
        default = self.db.get_target_quantity_default()
        self.assertEqual(
            self.db.get_inventory_count(
                below_target=True, target_default=default
            ),
            3,
        )
        self.assertEqual(
            self.db.get_inventory_count(
                below_target=True, set_name="Base Set",
                target_default=default,
            ),
            2,
        )

    def test_the_summary_totals_cards_and_copies(self):
        summary = self.db.get_restock_summary(
            target_default=self.db.get_target_quantity_default()
        )
        # Charizard 3 + Blastoise 4 + Mewtwo 2.
        self.assertEqual(summary["cards_short"], 3)
        self.assertEqual(summary["copies_needed"], 9)

        per_set = self.db.get_restock_summary(
            set_name="Base Set",
            target_default=self.db.get_target_quantity_default(),
        )
        self.assertEqual(per_set["cards_short"], 2)
        self.assertEqual(per_set["copies_needed"], 7)

    def test_the_shortfall_can_be_sorted_on(self):
        """The biggest gap first is how a shopping list gets read."""
        ordered = [
            r["product_name"] for r in self.db.get_inventory(
                limit=50, sort_by="needed", sort_dir="DESC",
                target_default=self.db.get_target_quantity_default(),
            )
        ]
        self.assertEqual(ordered[0], "Blastoise", "4 missing")
        self.assertEqual(ordered[1], "Charizard", "3 missing")

    def test_a_non_numeric_default_setting_falls_back_rather_than_breaking(self):
        """
        The default is spliced into SQL rather than bound, because it also
        appears in ORDER BY. So it must never be able to arrive as anything
        but a small non-negative integer.
        """
        self.db.set_listing_settings(
            {"target_quantity_default": "not a number"}, user_id=SHARED_SCOPE
        )
        self.assertEqual(self.db.get_target_quantity_default(), 4)

        self.db.set_listing_settings(
            {"target_quantity_default": "-5"}, user_id=SHARED_SCOPE
        )
        self.assertEqual(self.db.get_target_quantity_default(), 0)

        self.db.set_listing_settings(
            {"target_quantity_default": "99999"}, user_id=SHARED_SCOPE
        )
        self.assertEqual(self.db.get_target_quantity_default(), 999)

    def test_an_unknown_card_is_reported_rather_than_created(self):
        self.assertIsNone(self.db.set_manifest_target_quantity("ID9999", 4))

    def test_an_upload_does_not_disturb_a_target(self):
        """
        The target is ours, not the export's -- SortSwift has no column for
        it, and a re-upload must not clear what was set by hand.
        """
        self.db.set_manifest_target_quantity("ID1001", 8)
        self.db.get_or_create_manifest(
            "Charizard", "Base Set", "Near Mint", "Normal", remarks="Bin A-1"
        )
        self.assertEqual(
            self.db.get_manifest_by_id("ID1001")["target_quantity"], 8
        )


class PerLanguageTitleTests(unittest.TestCase):
    """
    Titles rendered per language, with the set code and year of the group.

    One template per account stopped being enough when a second language
    arrived: a Chinese listing states its set code, language and year,
    while an English one states none of them, and a single template cannot
    produce both.
    """

    def setUp(self):
        from tcg_engine.db import (
            DEFAULT_CHINESE_TITLE_TEMPLATE,
            DEFAULT_VARIATION_TITLE_TEMPLATE,
        )
        self.chinese = DEFAULT_CHINESE_TITLE_TEMPLATE
        self.english = DEFAULT_VARIATION_TITLE_TEMPLATE
        self.settings = {
            "variation_title_template": self.english,
            "variation_title_template_CS": self.chinese,
            "variation_title_template_JA": "",
        }

    def _card(self, **overrides):
        card = {
            "set_code": "CBB6C",
            "language": "CS",
            "ebay_fields_json":
                '{"item_specifics": {"C:Year Manufactured": "2026"}}',
        }
        card.update(overrides)
        return card

    def test_the_language_selects_the_template(self):
        from tcg_engine.batches import title_template_for
        self.assertEqual(title_template_for(self.settings, "CS"), self.chinese)
        self.assertEqual(title_template_for(self.settings, "EN"), self.english)

    def test_the_language_code_is_matched_case_insensitively(self):
        from tcg_engine.batches import title_template_for
        self.assertEqual(title_template_for(self.settings, "cs"), self.chinese)

    def test_a_blank_language_template_falls_back_to_the_default(self):
        """
        An empty Japanese slot exists to be visible and editable before
        there is any Japanese stock. It must behave as "not set".
        """
        from tcg_engine.batches import title_template_for
        self.assertEqual(title_template_for(self.settings, "JA"), self.english)

    def test_an_unknown_language_falls_back_rather_than_failing(self):
        from tcg_engine.batches import title_template_for
        self.assertEqual(title_template_for(self.settings, "ZZ"), self.english)
        self.assertEqual(title_template_for(self.settings, ""), self.english)

    def test_the_set_code_and_year_reach_the_title(self):
        from tcg_engine.batches import generate_variation_title
        title = generate_variation_title(
            "Gem Pack Volume 6", condition="NM", template=self.chinese,
            set_code="CBB6C", year="2026",
        )
        self.assertEqual(
            title,
            "Pokemon Gem Pack Volume 6 CBB6C Chinese 2026 "
            "Singles & Holos - CHOOSE YOUR CARD!",
        )
        self.assertLessEqual(len(title), 80)

    def test_a_group_whose_cards_disagree_states_neither_value(self):
        """
        A title describes the whole listing. Printing one member's set code
        as the group's is the mistake get_plan_groups had to stop making
        with MIN(), where a mixed block was headed with one card's grade.
        """
        from tcg_engine.batches import group_title_fields, group_language
        # Each field is judged on its own, so a group can legitimately
        # agree on the year while disagreeing about the set code.
        mixed_code = [self._card(), self._card(set_code="CSV9C")]
        self.assertEqual(
            group_title_fields(mixed_code), {"set_code": "", "year": "2026"}
        )

        mixed_year = [
            self._card(),
            self._card(
                ebay_fields_json=
                '{"item_specifics": {"C:Year Manufactured": "2025"}}'
            ),
        ]
        self.assertEqual(
            group_title_fields(mixed_year), {"set_code": "CBB6C", "year": ""}
        )

        self.assertEqual(
            group_language([self._card(), self._card(language="EN")]), ""
        )

    def test_a_uniform_group_reports_its_values(self):
        from tcg_engine.batches import group_title_fields, group_language
        cards = [self._card(), self._card()]
        self.assertEqual(
            group_title_fields(cards), {"set_code": "CBB6C", "year": "2026"}
        )
        self.assertEqual(group_language(cards), "CS")

    def test_an_unresolved_placeholder_leaves_no_double_space(self):
        """
        An English card has no year, so a template naming {year} would
        otherwise ship a listing title with a gap in it.
        """
        from tcg_engine.batches import generate_variation_title
        title = generate_variation_title(
            "Chilling Reign", condition="NM", template=self.chinese,
            set_code="", year="",
        )
        self.assertNotIn("  ", title)
        self.assertEqual(
            title, "Pokemon Chilling Reign Chinese Singles & Holos - CHOOSE YOUR CARD!"
        )

    def test_volume_is_kept_when_it_fits(self):
        """
        The abbreviation is a fallback, not a default. "Gem Pack Volume 6"
        fits at exactly 80 with a five-character code, so it stays.
        """
        from tcg_engine.batches import render_variation_title
        title, trimmed = render_variation_title(
            "Gem Pack Volume 6", condition="NM", template=self.chinese,
            set_code="CBB6C", year="2026",
        )
        self.assertIn("Volume", title)
        self.assertFalse(trimmed)

    def test_volume_is_abbreviated_only_when_it_has_to_be(self):
        """
        A six-character code tips the same title to 81, and "Vol" is a
        better answer than the mid-word chop the last resort would make.
        """
        from tcg_engine.batches import render_variation_title
        title, trimmed = render_variation_title(
            "Gem Pack Volume 6", condition="NM", template=self.chinese,
            set_code="CBB06C", year="2026",
        )
        self.assertIn("Gem Pack Vol 6", title)
        self.assertNotIn("Volume", title)
        self.assertLessEqual(len(title), 80)
        self.assertFalse(trimmed)

    def test_singles_and_holos_collapses_before_the_set_name_is_cut(self):
        """
        Template text is ours and says nothing false about a card, so it is
        given up before the set name is.
        """
        from tcg_engine.batches import render_variation_title
        title, trimmed = render_variation_title(
            "Chasing Glory Together", condition="NM", template=self.chinese,
            set_code="CSV10C", year="2026",
        )
        self.assertEqual(
            title,
            "Pokemon Chasing Glory Together CSV10C Chinese 2026 Holos "
            "- CHOOSE YOUR CARD!",
        )
        self.assertIn("Chasing Glory Together", title)
        self.assertFalse(trimmed)

    def test_a_title_that_cannot_fit_reports_that_it_was_trimmed(self):
        """
        `trimmed` is what lets the drafts page flag a clipped set name
        without blocking every template the abbreviations resolve.
        """
        from tcg_engine.batches import render_variation_title
        title, trimmed = render_variation_title(
            "A Set Name So Long That Nothing Short Of Cutting It Will Ever "
            "Make This Title Fit",
            condition="NM", template=self.chinese,
            set_code="CSV10C", year="2026",
        )
        self.assertEqual(len(title), 80)
        self.assertTrue(trimmed)

    def test_a_name_ending_in_its_own_number_is_not_given_it_twice(self):
        """
        Some exports spell the product name "Applin - 1902". The template
        would otherwise render "Applin - 1902 (1902)" on every option.
        """
        from tcg_engine.batches import build_variation_option_name
        self.assertEqual(
            build_variation_option_name("Applin - 1902", "1902"),
            "Applin - 1902",
        )

    def test_the_number_is_not_stripped_out_of_the_name_to_achieve_that(self):
        """
        The natural key is (name, set, condition, printing) and excludes the
        card number, so for such an export the number *inside the name* is
        the only thing telling two cards apart. Removing it merged 214 cards
        into 112 on the real file.
        """
        from tcg_engine.batches import build_variation_option_name
        first = build_variation_option_name("Applin - 1902", "1902")
        second = build_variation_option_name("Applin - 1903", "1903")
        self.assertNotEqual(first, second)

    def test_an_ordinary_name_still_gets_its_number(self):
        from tcg_engine.batches import build_variation_option_name
        self.assertEqual(
            build_variation_option_name("Pikachu", "025/198"),
            "Pikachu (025/198)",
        )

    def test_a_number_inside_a_name_is_left_alone(self):
        """
        Only a trailing match is suppressed. A digit that is part of the
        name, or a longer number merely ending in the card's, still gets
        the suffix -- "Mew 1151" is not card 151.
        """
        from tcg_engine.batches import build_variation_option_name
        self.assertEqual(
            build_variation_option_name("Mew 1151", "151"), "Mew 1151 (151)"
        )

    def test_the_group_declares_the_configured_option_values(self):
        """
        The group's option list must match the `Card` aspect on each item,
        which is rendered from the configured template. _group_payload
        hardcoded the shipped default, so customising the template made the
        two disagree -- latent only because the two strings were equal.
        """
        from tcg_engine.push import _group_payload
        entries = [({"product_name": "Applin - 1902", "card_number": "1902"}, "ID1")]
        payload = _group_payload(
            "GRP", entries, title="t", description="d",
            cover_image_url="", aspects={}, option_template="{name}",
        )
        values = payload["variesBy"]["specifications"][0]["values"]
        self.assertEqual(values, ["Applin - 1902"])

    def test_the_english_default_is_untouched_by_any_of_this(self):
        """
        The per-language work must not restyle a listing that never asked
        for it: an English group with no template of its own renders
        exactly as it always did.
        """
        from tcg_engine.batches import generate_variation_title, title_template_for
        title = generate_variation_title(
            "SWSH06: Chilling Reign", condition="NM",
            template=title_template_for(self.settings, "EN"),
        )
        self.assertEqual(
            title,
            "SWSH06: Chilling Reign: Pick Your Card - NM - Complete Your Set",
        )


if __name__ == "__main__":
    unittest.main()
