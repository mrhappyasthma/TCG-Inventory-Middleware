import csv
import io
import os
import sqlite3
import tempfile
import unittest
from tcg_engine.db import Database
from tcg_engine.orders import process_orders_csv
from tcg_engine.batches import process_batch_csv
from tcg_engine.sync import sync_active_listings_csv


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

    def test_module_a_orders_conversion(self):
        # Populate master catalog
        self.db.insert_manifest("ID1001", "Pikachu", "Jungle", "Near Mint", "Foil")
        self.db.insert_manifest("ID1002", "Mewtwo", "Base Set", "Near Mint", "Holofoil")

        # Sample eBay orders CSV (including non-TCG item and multi-quantity)
        sample_ebay_orders = """Sales Record Number,Order Number,Item Number,Item Title,Custom Label,Quantity,Sale Price
101,ORD-9901,123456789012,Pokemon Jungle Pikachu Foil,ID1001,3,$15.00
102,ORD-9902,123456789013,Pokemon Base Mewtwo Holo,ID1002,1,$45.00
103,ORD-9903,999999999999,Random Comic Book,,1,$5.00
"""
        res = process_orders_csv(sample_ebay_orders, self.db)
        self.assertEqual(res["converted_count"], 4)  # 3 Pikachus + 1 Mewtwo
        self.assertEqual(res["skipped_count"], 1)  # 1 comic book skipped
        self.assertEqual(len(res["output_rows"]), 2)

        # Verify output CSV headers and contents
        csv_lines = res["csv_content"].strip().splitlines()
        self.assertEqual(csv_lines[0], "skuId,productId,Order Number,Product Name,Set Name,Condition,Printing,Quantity")
        self.assertTrue(any("ORD-9901" in line and "Pikachu" in line and "3" in line for line in csv_lines))
        self.assertTrue(any("ORD-9902" in line and "Mewtwo" in line and "1" in line for line in csv_lines))

    def test_module_b_batches_routing(self):
        # Populate DB with 1 card that is already live on eBay
        self.db.insert_manifest("ID1001", "Gengar", "Fossil", "Near Mint", "Holofoil")
        self.db.upsert_variation("ID1001", "123456789099", 2)

        # Batch contains 1 existing live card (Revise) and 1 brand new card (Add)
        sample_batch = """Product Name,Set Name,Condition,Printing,Quantity,ConditionID
Gengar,Fossil,Near Mint,Holofoil,3,3000
Alakazam,Base Set,Lightly Played,Normal,1,4000
"""
        res = process_batch_csv(sample_batch, self.db)
        self.assertEqual(res["revise_count"], 1)
        self.assertEqual(res["add_count"], 1)
        self.assertEqual(res["new_catalog_count"], 1)

        # Verify Revise CSV has updated quantity (2 previous + 3 new = 5)
        revise_lines = res["revise_csv"].strip().splitlines()
        self.assertEqual(revise_lines[0], "Action,Item Number,Custom Label,Quantity,Price")
        self.assertTrue(any("Revise" in line and "123456789099" in line and "ID1001" in line and "5" in line for line in revise_lines))

        # Verify Add CSV has new card with generated ID1002
        add_lines = res["add_csv"].strip().splitlines()
        self.assertIn("Action,Category,Title,Relationship,RelationshipDetails,Description,ConditionID,StartPrice,Quantity,CustomLabel,PicURL,Format,Duration,Price", add_lines[0])
        self.assertTrue(any("ID1002" in line and "Alakazam" in line for line in add_lines))

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

    def test_single_threshold_and_variation_grouping(self):
        # Card 1: Market $0.17 -> Calculated price $1.99 (< $5.00) -> grouped into Set Variation
        # Card 2: Market $4.50 -> Calculated price $7.50 (>= $5.00) -> listed as Single
        batch_csv = """"Stock Item ID","Game","File Name","Set","Set Code","Card Number","Name","Rarity","Market Price","Low Price","Mid Price","High Price","EU Price","Condition","Language","Printing","Quantity","Comment","Remarks","TCGplayer Id","SKU Id","ID Product","UPC","CDN Image","Card Back CDN Image","Cost","Price","TCGPlayer Price","Shopify Price","Cardtrader Price","Manapool Price","Misprint Price","eBay Price","Square Price","*ConditionID"
"6a9f37cd1ffb1c868bf60ba0","Pokemon","","SV05: Temporal Forces","TEF","016/162","Deerling","Common","0.17","0.01","0.17","19.98","","NM","EN","Normal",1,"","Bin-1",542678,7805758,"760646","","https://cdn.example.com/deerling.jpg","","0.00","0.15","","","","","","","","3000"
"6a9f37cd1ffb1c868bf60ba1","Pokemon","","SV05: Temporal Forces","TEF","001/162","Iron Leaves ex","Ultra Rare","4.50","0.01","4.50","19.98","","NM","EN","Normal",1,"","Bin-2",542679,7805759,"760647","","https://cdn.example.com/ironleaves.jpg","","0.00","4.50","","","","","","","","3000"
"""
        res = process_batch_csv(batch_csv, self.db)
        self.assertEqual(res["add_count"], 2)

        add_lines = res["add_csv"].strip().splitlines()
        # Should have:
        # 1. Parent container row (Relationship=Variation, Title=SV05: Temporal Forces: Pick Your Card...)
        # 2. Child variation row (Deerling, CustomLabel=ID1001-Bin-1, Price=1.99)
        # 3. Single listing row (Iron Leaves ex, Relationship="", Title=Iron Leaves ex - SV05: Temporal Forces - Near Mint, Price=7.50)
        self.assertTrue(any("SV05: Temporal Forces: Pick Your Card - NM - Complete Your Set" in line for line in add_lines))
        self.assertTrue(any("Deerling" in line and "ID1001-Bin-1" in line and "1.99" in line for line in add_lines))
        self.assertTrue(any("Iron Leaves ex" in line and "ID1002-Bin-2" in line and "7.50" in line and "Variation" not in line for line in add_lines))

    def test_sortswift_real_sample(self):
        # Test with the exact user SortSwift inventory format
        real_sample = """"Stock Item ID","Game","File Name","Set","Set Code","Card Number","Name","Rarity","Market Price","Low Price","Mid Price","High Price","EU Price","Condition","Language","Printing","Quantity","Comment","Remarks","TCGplayer Id","SKU Id","ID Product","UPC","CDN Image","Card Back CDN Image","Cost","Price","TCGPlayer Price","Shopify Price","Cardtrader Price","Manapool Price","Misprint Price","eBay Price","Square Price","*ConditionID"
"6a9f37cd1ffb1c868bf60ba0","Pokemon","","SV05: Temporal Forces","TEF","016/162","Deerling - 016/162","Common","0.17","0.01","0.17","19.98","","NM","EN","Normal",1,"","No Remark",542678,7805758,"760646","","https://cdn.example.com/deerling.jpg","https://cdn.example.com/back.jpg","0.00","0.15","","","","","","","","3000"
"6a9f37cd1ffb1c868bf60ba3","Pokemon","","SV05: Temporal Forces","TEF","018/162","Grubbin","Common","0.13","0.01","0.15","2.99","","NM","EN","Normal",2,"","No Remark",542763,7806223,"760648","","https://cdn.example.com/grubbin.jpg","","0.00","0.14","","","","","","","","3000"
"""
        res = process_batch_csv(real_sample, self.db)
        self.assertEqual(res["add_count"], 2)
        self.assertEqual(res["new_catalog_count"], 2)

        # Check that SKU Id, TCGplayer Id, CDN image, and calculated price were cataloged
        card1 = self.db.get_manifest_by_id("ID1001")
        self.assertEqual(card1["product_name"], "Deerling - 016/162")
        self.assertEqual(card1["sku_id"], "7805758")
        self.assertEqual(card1["tcgplayer_id"], "542678")
        self.assertEqual(card1["price"], 1.99)  # Calculated via pricing rule: <0.25 -> 1.99
        self.assertEqual(card1["market_price"], 0.17)
        self.assertEqual(card1["cdn_image"], "https://cdn.example.com/deerling.jpg")

        # Now test orders conversion using the cataloged card
        sample_ebay_order = """Sales Record Number,Order Number,Item Title,Custom Label,Quantity
501,ORD-501,Pokemon Temporal Forces Deerling,ID1001,1
"""
        order_res = process_orders_csv(sample_ebay_order, self.db)
        self.assertEqual(order_res["converted_count"], 1)
        csv_lines = order_res["csv_content"].strip().splitlines()
        # Verify skuId 7805758 is present in SortSwift deduction output
        # The condition round-trips verbatim: the source export said "NM", so the
        # deduction file says "NM". We deliberately do not normalise it to
        # "Near Mint" -- SortSwift's own vocabulary goes back to SortSwift, and
        # matching is driven by skuId regardless.
        self.assertIn("7805758,542678,ORD-501,Deerling - 016/162,SV05: Temporal Forces,NM,Normal,1", csv_lines)

    def test_bin_remark_encoding_and_reexport(self):
        # Initial export with Remarks "Bin A-12"
        batch_1 = """"Stock Item ID","Game","File Name","Set","Set Code","Card Number","Name","Rarity","Market Price","Low Price","Mid Price","High Price","EU Price","Condition","Language","Printing","Quantity","Comment","Remarks","TCGplayer Id","SKU Id","ID Product","UPC","CDN Image","Card Back CDN Image","Cost","Price","TCGPlayer Price","Shopify Price","Cardtrader Price","Manapool Price","Misprint Price","eBay Price","Square Price","*ConditionID"
"6a9f37cd1ffb1c868bf60ba0","Pokemon","","SV05: Temporal Forces","TEF","016/162","Deerling - 016/162","Common","0.17","0.01","0.17","19.98","","NM","EN","Normal",1,"","Bin A-12",542678,7805758,"760646","","https://cdn.example.com/deerling.jpg","","0.00","0.15","","","","","","","","3000"
"""
        res1 = process_batch_csv(batch_1, self.db)
        self.assertEqual(res1["add_count"], 1)

        # Check that CustomLabel in eBay Add CSV encodes the bin remark: ID1001-Bin_A-12
        add_lines = res1["add_csv"].strip().splitlines()
        self.assertTrue(any("ID1001-Bin_A-12" in line for line in add_lines))

        # Check that remark is saved in DB
        card = self.db.get_manifest_by_id("ID1001")
        self.assertEqual(card["remarks"], "Bin A-12")

        # Simulate eBay Active Listings Sync with that SKU
        sync_csv = "Item number,Title,Custom label (SKU),Available quantity\n998877665544,Deerling,ID1001-Bin_A-12,3\n"
        sync_res = sync_active_listings_csv(sync_csv, self.db)
        self.assertEqual(sync_res["synced_count"], 1)

        # Re-exporting inventory with new stock of Deerling (+2) and a brand new card (Pikachu)
        batch_2 = """"Stock Item ID","Game","File Name","Set","Set Code","Card Number","Name","Rarity","Market Price","Low Price","Mid Price","High Price","EU Price","Condition","Language","Printing","Quantity","Comment","Remarks","TCGplayer Id","SKU Id","ID Product","UPC","CDN Image","Card Back CDN Image","Cost","Price","TCGPlayer Price","Shopify Price","Cardtrader Price","Manapool Price","Misprint Price","eBay Price","Square Price","*ConditionID"
"6a9f37cd1ffb1c868bf60ba0","Pokemon","","SV05: Temporal Forces","TEF","016/162","Deerling - 016/162","Common","0.17","0.01","0.17","19.98","","NM","EN","Normal",2,"","Bin A-12",542678,7805758,"760646","","https://cdn.example.com/deerling.jpg","","0.00","0.15","","","","","","","","3000"
"6a9f37cd1ffb1c868bf60ba1","Pokemon","","Base Set","BS","058/102","Pikachu","Common","2.50","0.01","2.50","19.98","","NM","EN","Normal",1,"","Box 4",12345,67890,"12345","","https://cdn.example.com/pikachu.jpg","","0.00","2.50","","","","","","","","3000"
"""
        res2 = process_batch_csv(batch_2, self.db)
        # Deerling was live on eBay -> routed to Revise with consolidated quantity (3 existing + 2 = 5)
        self.assertEqual(res2["revise_count"], 1)
        # Pikachu is new -> routed to Add with ID1002-Box_4
        self.assertEqual(res2["add_count"], 1)
        self.assertEqual(res2["new_catalog_count"], 1)

        # Test eBay order with custom label ID1001-Bin_A-12
        order_csv = "Sales Record Number,Order Number,Item Title,Custom Label,Quantity\n101,ORD-101,Deerling,ID1001-Bin_A-12,1\n"
        order_res = process_orders_csv(order_csv, self.db)
        self.assertEqual(order_res["converted_count"], 1)
        self.assertIn("7805758", order_res["csv_content"])


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
        self.assertEqual(res["skipped_parent_count"], 1)

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
"SV05: Temporal Forces","TEF","016/162","Deerling","0.17","NM","EN","Normal",1,"Bin-1",542678,7805758,"https://cdn.example.com/a.jpg","0.15","3000"
"SV05: Temporal Forces","TEF","001/162","Iron Leaves ex","4.50","NM","EN","Normal",1,"Bin-2",542679,7805759,"https://cdn.example.com/b.jpg","4.50","3000"
"""

    def test_duplicate_batch_is_refused_unless_forced(self):
        first = process_batch_csv(self.TWO_CARD_BATCH, self.db, source_name="scan.csv")
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["add_count"], 2)

        # Same bytes again: refused, and nothing is applied.
        second = process_batch_csv(self.TWO_CARD_BATCH, self.db, source_name="scan.csv")
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["add_count"], 0)
        self.assertEqual(second["revise_count"], 0)
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
        self.assertEqual(forced["add_count"], 2)

    def test_a_different_batch_is_not_treated_as_duplicate(self):
        process_batch_csv(self.TWO_CARD_BATCH, self.db, source_name="scan.csv")
        other = self.TWO_CARD_BATCH.replace("Deerling", "Sunkern")
        res = process_batch_csv(other, self.db, source_name="scan2.csv")
        self.assertFalse(res["duplicate"])

    def test_group_by_set_disabled_lists_every_card_as_single(self):
        self.db.set_listing_settings({"group_by_set": "false"})
        res = process_batch_csv(self.TWO_CARD_BATCH, self.db)

        self.assertEqual(res["add_count"], 2)
        add_lines = res["add_csv"].strip().splitlines()
        # No parent container row, so no row carries the Variation relationship.
        self.assertFalse(
            any("Variation" in line for line in add_lines[1:]),
            "set grouping is off; no variation rows expected",
        )
        # The cheap card is now a single despite being below the threshold.
        self.assertTrue(any("Deerling" in line and "1.99" in line for line in add_lines))

    def test_group_by_set_enabled_still_groups(self):
        self.db.set_listing_settings({"group_by_set": "true"})
        res = process_batch_csv(self.TWO_CARD_BATCH, self.db)
        add_lines = res["add_csv"].strip().splitlines()
        self.assertTrue(any("Variation" in line for line in add_lines[1:]))

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
"Chilling Reign","Deerling","0.17","NM","Normal",1,"Bin-1",111,"3000"
"Chilling Reign","Mareep","0.17","LP","Normal",1,"Bin-2",222,"4000"
"""

    BATCH_WITHOUT_CONDITION_ID = """"Set","Name","Market Price","Condition","Printing","Quantity"
"Chilling Reign","Deerling","0.17","NM","Normal",1
"""

    BATCH_WITHOUT_CONDITION = """"Set","Name","Market Price","Condition","Printing","Quantity","Remarks","SKU Id","*ConditionID"
"Chilling Reign","Deerling","0.17","","Normal",1,"Bin-1",111,"3000"
"""

    BATCH_WITH_PUNCTUATED_NAME = """"Set","Name","Market Price","Condition","Printing","Quantity","Remarks","SKU Id","*ConditionID"
"Chilling Reign","Ho-Oh; Lugia | Legend","0.17","NM","Normal",1,"Bin-1",111,"3000"
"""

    def test_condition_is_passed_through_verbatim(self):
        process_batch_csv(self.MIXED_CONDITION_BATCH, self.db)
        # "NM" and "LP" are stored exactly as the export wrote them; they are
        # not expanded to "Near Mint" / "Lightly Played" by any lookup table.
        conditions = {c["condition"] for c in self.db.export_all_manifest()}
        self.assertEqual(conditions, {"NM", "LP"})

    def test_row_without_condition_id_is_skipped(self):
        res = process_batch_csv(self.BATCH_WITHOUT_CONDITION_ID, self.db)
        self.assertEqual(res["add_count"], 0)
        self.assertEqual(res["skipped_count"], 1)
        self.assertEqual(self.db.get_stats()["total_cards"], 0)
        self.assertTrue(
            any("ConditionID" in log["message"] for log in res["logs"]),
            "the skip reason should name the missing column",
        )

    def test_row_without_condition_is_skipped(self):
        res = process_batch_csv(self.BATCH_WITHOUT_CONDITION, self.db)
        self.assertEqual(res["add_count"], 0)
        self.assertEqual(res["skipped_count"], 1)

    def test_variations_are_grouped_by_set_and_condition(self):
        res = process_batch_csv(self.MIXED_CONDITION_BATCH, self.db)
        self.assertEqual(res["add_count"], 2)

        rows = list(csv.DictReader(io.StringIO(res["add_csv"])))
        parents = [r for r in rows if not r["Relationship"]]
        children = [r for r in rows if r["Relationship"] == "Variation"]

        # One listing per (set, condition), so NM and LP do not share a parent.
        self.assertEqual(len(parents), 2)
        self.assertEqual(len(children), 2)

        # Each parent's ConditionID matches the condition named in its title.
        by_cond = {p["ConditionID"]: p for p in parents}
        self.assertEqual(set(by_cond), {"3000", "4000"})
        self.assertIn("NM", by_cond["3000"]["Title"])
        self.assertIn("LP", by_cond["4000"]["Title"])

    def test_parent_row_leaves_relationship_blank(self):
        res = process_batch_csv(self.MIXED_CONDITION_BATCH, self.db)
        rows = list(csv.DictReader(io.StringIO(res["add_csv"])))
        for r in rows:
            if r["Title"]:
                # Parent rows carry the title; eBay requires their Relationship
                # to be empty so it can identify the container row.
                self.assertEqual(r["Relationship"], "")
                self.assertTrue(r["RelationshipDetails"].startswith("Card="))
            else:
                self.assertEqual(r["Relationship"], "Variation")

    def test_variation_values_are_semicolon_separated(self):
        batch = self.MIXED_CONDITION_BATCH.replace('"LP"', '"NM"').replace('"4000"', '"3000"')
        res = process_batch_csv(batch, self.db)
        rows = list(csv.DictReader(io.StringIO(res["add_csv"])))
        parent = next(r for r in rows if r["Title"])

        # Both cards now share one listing. Values must be semicolon separated:
        # a pipe would be read by eBay as the start of a second attribute.
        self.assertEqual(parent["RelationshipDetails"], "Card=Deerling;Mareep")
        self.assertNotIn("|", parent["RelationshipDetails"])

    def test_separator_characters_in_card_names_are_sanitized(self):
        res = process_batch_csv(self.BATCH_WITH_PUNCTUATED_NAME, self.db)
        rows = list(csv.DictReader(io.StringIO(res["add_csv"])))
        details = [r["RelationshipDetails"] for r in rows]
        # Neither separator may survive inside a value, or one card would be
        # split into several bogus options.
        for d in details:
            self.assertNotIn(";", d.split("=", 1)[1])
            self.assertNotIn("|", d)


if __name__ == "__main__":
    unittest.main()
