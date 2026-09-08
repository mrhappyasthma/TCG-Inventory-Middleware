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
from tcg_engine.relink import relink_from_active_listings, parse_option_name


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
Gengar,Fossil,Near Mint,Holofoil,3,4000
Alakazam,Base Set,Lightly Played,Normal,1,4000
"""
        res = process_batch_csv(sample_batch, self.db)
        self.assertEqual(res["revise_count"], 1)
        self.assertEqual(res["add_count"], 1)
        self.assertEqual(res["new_catalog_count"], 1)

        # Verify Revise CSV has updated quantity (2 previous + 3 new = 5)
        revise_lines = res["revise_csv"].strip().splitlines()
        # A File Exchange upload identifies an existing listing by "ItemID";
        # "Item Number" is the Active Listings report's name for it and is not
        # a valid upload column.
        self.assertEqual(revise_lines[0], "Action,ItemID,CustomLabel,Quantity,Price")
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
"6a9f37cd1ffb1c868bf60ba0","Pokemon","","SV05: Temporal Forces","TEF","016/162","Deerling","Common","0.17","0.01","0.17","19.98","","NM","EN","Normal",1,"","Bin-1",542678,7805758,"760646","","https://cdn.example.com/deerling.jpg","","0.00","0.15","","","","","","","","4000"
"6a9f37cd1ffb1c868bf60ba1","Pokemon","","SV05: Temporal Forces","TEF","001/162","Iron Leaves ex","Ultra Rare","4.50","0.01","4.50","19.98","","NM","EN","Normal",1,"","Bin-2",542679,7805759,"760647","","https://cdn.example.com/ironleaves.jpg","","0.00","4.50","","","","","","","","4000"
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
"6a9f37cd1ffb1c868bf60ba0","Pokemon","","SV05: Temporal Forces","TEF","016/162","Deerling - 016/162","Common","0.17","0.01","0.17","19.98","","NM","EN","Normal",1,"","No Remark",542678,7805758,"760646","","https://cdn.example.com/deerling.jpg","https://cdn.example.com/back.jpg","0.00","0.15","","","","","","","","4000"
"6a9f37cd1ffb1c868bf60ba3","Pokemon","","SV05: Temporal Forces","TEF","018/162","Grubbin","Common","0.13","0.01","0.15","2.99","","NM","EN","Normal",2,"","No Remark",542763,7806223,"760648","","https://cdn.example.com/grubbin.jpg","","0.00","0.14","","","","","","","","4000"
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
        # Two things are pinned here. The condition round-trips verbatim: the
        # source export said "NM", so the deduction file says "NM" rather than a
        # normalised "Near Mint". And the quantity is NEGATIVE: SortSwift's
        # import adds the quantity column to existing stock, so a deduction has
        # to be expressed as a negative number or it increases inventory.
        self.assertIn("7805758,542678,ORD-501,Deerling - 016/162,SV05: Temporal Forces,NM,Normal,-1", csv_lines)

    def test_bin_remark_encoding_and_reexport(self):
        # Initial export with Remarks "Bin A-12"
        batch_1 = """"Stock Item ID","Game","File Name","Set","Set Code","Card Number","Name","Rarity","Market Price","Low Price","Mid Price","High Price","EU Price","Condition","Language","Printing","Quantity","Comment","Remarks","TCGplayer Id","SKU Id","ID Product","UPC","CDN Image","Card Back CDN Image","Cost","Price","TCGPlayer Price","Shopify Price","Cardtrader Price","Manapool Price","Misprint Price","eBay Price","Square Price","*ConditionID"
"6a9f37cd1ffb1c868bf60ba0","Pokemon","","SV05: Temporal Forces","TEF","016/162","Deerling - 016/162","Common","0.17","0.01","0.17","19.98","","NM","EN","Normal",1,"","Bin A-12",542678,7805758,"760646","","https://cdn.example.com/deerling.jpg","","0.00","0.15","","","","","","","","4000"
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
"6a9f37cd1ffb1c868bf60ba0","Pokemon","","SV05: Temporal Forces","TEF","016/162","Deerling - 016/162","Common","0.17","0.01","0.17","19.98","","NM","EN","Normal",2,"","Bin A-12",542678,7805758,"760646","","https://cdn.example.com/deerling.jpg","","0.00","0.15","","","","","","","","4000"
"6a9f37cd1ffb1c868bf60ba1","Pokemon","","Base Set","BS","058/102","Pikachu","Common","2.50","0.01","2.50","19.98","","NM","EN","Normal",1,"","Box 4",12345,67890,"12345","","https://cdn.example.com/pikachu.jpg","","0.00","2.50","","","","","","","","4000"
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

        # Both listings are ungraded (ConditionID 4000); the grade that differs
        # is carried by the Condition Descriptor.
        self.assertEqual({p["ConditionID"] for p in parents}, {"4000"})
        by_desc = {p["CD:40001"]: p for p in parents}
        self.assertEqual(
            set(by_desc),
            {"Near mint or better - (ID: 400010)", "Excellent - (ID: 400015)"},
        )
        self.assertIn("NM", by_desc["Near mint or better - (ID: 400010)"]["Title"])
        self.assertIn("LP", by_desc["Excellent - (ID: 400015)"]["Title"])

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
        batch = self.MIXED_CONDITION_BATCH.replace('"LP"', '"NM"')
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

    # ------------------------------------------------------------------
    # Catalogued quantity vs eBay's reported quantity
    # ------------------------------------------------------------------

    def test_catalog_quantity_accumulates_across_batches(self):
        process_batch_csv(self.MIXED_CONDITION_BATCH, self.db, source_name="b1.csv")
        rows = {r["manifest_id"]: r for r in self.db.export_all_manifest()}
        self.assertEqual(rows["ID1001"]["quantity"], 1)

        # A different batch adding the same card again accumulates rather than
        # overwriting, matching the additive semantics of Module A.
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

    def test_descriptor_appears_on_every_generated_row(self):
        from tcg_engine.batches import CONDITION_DESCRIPTOR_COLUMN

        res = process_batch_csv(self.MIXED_CONDITION_BATCH, self.db)
        rows = list(csv.DictReader(io.StringIO(res["add_csv"])))
        self.assertTrue(rows)
        for r in rows:
            self.assertTrue(
                r[CONDITION_DESCRIPTOR_COLUMN],
                "every Add row needs a Condition Descriptor",
            )
        # NM and LP groups carry their own descriptor.
        descriptors = {r[CONDITION_DESCRIPTOR_COLUMN] for r in rows}
        self.assertEqual(
            descriptors,
            {"Near mint or better - (ID: 400010)", "Excellent - (ID: 400015)"},
        )

    def test_explicit_descriptor_column_is_passed_through(self):
        from tcg_engine.batches import CONDITION_DESCRIPTOR_COLUMN

        # An export that already supplies the descriptor wins over our mapping.
        hdr = (
            '"Set","Name","Market Price","Condition","Printing","Quantity",'
            '"Remarks","SKU Id","*ConditionID","CD:40001"'
        )
        row = (
            '"Chilling Reign","Deerling","0.17","NM","Normal",1,"Bin-1",111,'
            '"4000","Poor - (ID: 400017)"'
        )
        res = process_batch_csv(hdr + chr(10) + row + chr(10), self.db)
        rows = list(csv.DictReader(io.StringIO(res["add_csv"])))
        for r in rows:
            self.assertEqual(r[CONDITION_DESCRIPTOR_COLUMN], "Poor - (ID: 400017)")

    def test_graded_condition_id_is_skipped(self):
        batch = self.MIXED_CONDITION_BATCH.replace('"4000"', '"2750"')
        res = process_batch_csv(batch, self.db)
        self.assertTrue(
            any("not the ungraded value" in log["message"] for log in res["logs"])
        )

    # ------------------------------------------------------------------
    # Item location and business policies (eBay error 10009)
    # ------------------------------------------------------------------

    def test_postal_code_is_emitted_and_location_is_not(self):
        self.db.set_listing_settings({"seller_postal_code": "94301"})
        res = process_batch_csv(self.MIXED_CONDITION_BATCH, self.db)

        header = res["add_csv"].splitlines()[0].split(",")
        self.assertIn("PostalCode", header)
        # Location and PostalCode are alternatives; sending both is a documented
        # cause of the 10009 error this column exists to fix.
        self.assertNotIn("Location", header)

        for r in csv.DictReader(io.StringIO(res["add_csv"])):
            self.assertEqual(r["PostalCode"], "94301")

    def test_missing_postal_code_raises_an_error_log(self):
        self.db.set_listing_settings({"seller_postal_code": ""})
        res = process_batch_csv(self.MIXED_CONDITION_BATCH, self.db)

        errors = [lg for lg in res["logs"] if lg["level"] == "ERROR"]
        self.assertTrue(errors, "an unset postal code must be surfaced loudly")
        self.assertTrue(
            any("10009" in lg["message"] for lg in errors),
            "the log should name the eBay error code it will cause",
        )

    def test_business_policy_columns_appear_only_when_configured(self):
        # Explicitly blank the other two: they ship with defaults, and the
        # behaviour under test is that a BLANK policy omits its column.
        self.db.set_listing_settings({
            "seller_postal_code": "94301",
            "shipping_profile_name": "Standard Free Shipping",
            "return_profile_name": "",
            "payment_profile_name": "",
        })
        res = process_batch_csv(self.MIXED_CONDITION_BATCH, self.db)

        header = res["add_csv"].splitlines()[0].split(",")
        self.assertIn("ShippingProfileName", header)
        # An unconfigured policy is omitted entirely rather than sent blank.
        self.assertNotIn("ReturnProfileName", header)
        self.assertNotIn("PaymentProfileName", header)

        for r in csv.DictReader(io.StringIO(res["add_csv"])):
            self.assertEqual(r["ShippingProfileName"], "Standard Free Shipping")

    def test_all_business_policies_are_emitted_verbatim(self):
        policies = {
            "shipping_profile_name": "Standard Free Shipping",
            "return_profile_name": "30 Day Returns",
            "payment_profile_name": "Managed Payments",
        }
        self.db.set_listing_settings(dict(policies, seller_postal_code="94301"))
        res = process_batch_csv(self.MIXED_CONDITION_BATCH, self.db)

        rows = list(csv.DictReader(io.StringIO(res["add_csv"])))
        self.assertTrue(rows)
        for r in rows:
            # Policy names are matched case-sensitively by eBay, so they must
            # survive untouched.
            self.assertEqual(r["ShippingProfileName"], "Standard Free Shipping")
            self.assertEqual(r["ReturnProfileName"], "30 Day Returns")
            self.assertEqual(r["PaymentProfileName"], "Managed Payments")

    def test_empty_batch_still_reports_the_effective_headers(self):
        self.db.set_listing_settings({
            "seller_postal_code": "94301",
            "shipping_profile_name": "Standard Free Shipping",
        })
        res = process_batch_csv("", self.db)
        header = res["add_csv"].splitlines()[0].split(",")
        self.assertIn("PostalCode", header)
        self.assertIn("ShippingProfileName", header)

    # ------------------------------------------------------------------
    # Shipped seller defaults (postal code + business policies)
    # ------------------------------------------------------------------

    def test_seller_settings_are_prepopulated_by_default(self):
        settings = self.db.get_listing_settings()
        self.assertEqual(settings["seller_postal_code"], "94305")
        self.assertEqual(settings["shipping_profile_name"], "Free Shipping Cards")
        self.assertEqual(settings["return_profile_name"], "No Returns")
        self.assertEqual(settings["payment_profile_name"], "Immediate Payment")

    def test_defaults_reach_the_generated_csv_with_no_configuration(self):
        # Straight out of the box, with nothing configured, the Add file must
        # already carry everything eBay rejected it for.
        res = process_batch_csv(self.MIXED_CONDITION_BATCH, self.db)

        header = res["add_csv"].splitlines()[0].split(",")
        for column in (
            "PostalCode",
            "ShippingProfileName",
            "ReturnProfileName",
            "PaymentProfileName",
        ):
            self.assertIn(column, header)
        self.assertNotIn("Location", header)

        for r in csv.DictReader(io.StringIO(res["add_csv"])):
            self.assertEqual(r["PostalCode"], "94305")
            self.assertEqual(r["ShippingProfileName"], "Free Shipping Cards")
            self.assertEqual(r["ReturnProfileName"], "No Returns")
            self.assertEqual(r["PaymentProfileName"], "Immediate Payment")

        # And no error about a missing location.
        self.assertFalse(
            [lg for lg in res["logs"] if lg["level"] == "ERROR"],
            "a fully defaulted run should not warn about missing seller fields",
        )

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

    def test_item_specific_columns_are_forwarded(self):
        res = process_batch_csv(self.EBAY_EXPORT_BATCH, self.db)
        header = res["add_csv"].splitlines()[0].split(",")

        # The leading asterisk is a template annotation, not part of the name.
        for column in ("C:Game", "C:Set", "C:Language"):
            self.assertIn(column, header)
        self.assertNotIn("*C:Game", header)

    def test_parent_keeps_only_specifics_shared_by_the_whole_group(self):
        # Clear the overriding setting so the export-supplied value is used.
        self.db.set_listing_settings({"default_game": ""})
        res = process_batch_csv(self.EBAY_EXPORT_BATCH, self.db)
        rows = list(csv.DictReader(io.StringIO(res["add_csv"])))
        parent = next(r for r in rows if r["Title"])

        # Uniform across the group -> valid at listing level.
        self.assertEqual(parent["C:Game"], "Test TCG")
        self.assertEqual(parent["C:Set"], "Chilling Reign")
        self.assertEqual(parent["C:Language"], "English")

        # Differs per card -> cannot be a single listing-level value, and the
        # variation axis already expresses it.
        self.assertEqual(parent["C:Card Name"], "")
        self.assertEqual(parent["C:Card Number"], "")

    def test_game_falls_back_to_the_configured_default(self):
        self.db.set_listing_settings({"default_game": "Fallback Game"})
        # A batch with no game information anywhere.
        res = process_batch_csv(self.MIXED_CONDITION_BATCH, self.db)
        rows = list(csv.DictReader(io.StringIO(res["add_csv"])))
        for r in rows:
            if r["Title"]:
                self.assertEqual(r["C:Game"], "Fallback Game")

    def test_bare_game_column_is_normalised(self):
        # Clear the overriding setting so the export-supplied value is used.
        self.db.set_listing_settings({"default_game": ""})
        hdr = (
            '"Game","Set","Name","Market Price","Condition","Printing",'
            '"Quantity","Remarks","SKU Id","*ConditionID"'
        )
        row = '"Some Game","Chilling Reign","Deerling","0.17","NM","Normal",1,"C-1",111,"4000"'
        res = process_batch_csv(chr(10).join([hdr, row]) + chr(10), self.db)
        rows = list(csv.DictReader(io.StringIO(res["add_csv"])))
        parent = next(r for r in rows if r["Title"])
        self.assertEqual(parent["C:Game"], "Some Game")

    def test_single_listing_keeps_all_of_its_own_specifics(self):
        # Clear the overriding setting so the export-supplied value is used.
        self.db.set_listing_settings({"default_game": ""})
        batch = self.EBAY_EXPORT_BATCH.replace('"0.17"', '"40.00"')
        res = process_batch_csv(batch, self.db)
        rows = list(csv.DictReader(io.StringIO(res["add_csv"])))
        # Above the threshold every card is a single, so nothing is uniform-only.
        self.assertFalse([r for r in rows if r["Relationship"] == "Variation"])
        for r in rows:
            self.assertEqual(r["C:Game"], "Test TCG")
            self.assertTrue(r["C:Card Name"], "a single keeps its own card name")

    PLAIN_EXPORT_BATCH = """"Game","Set","Set Code","Card Number","Name","Rarity","Market Price","Condition","Language","Printing","Quantity","Remarks","SKU Id","*ConditionID"
"Pokemon","SWSH06: Chilling Reign","CRE","121/198","Crushing Gloves","Uncommon","0.17","NM","English","Normal",1,"C-1",111,"4000"
"Pokemon","SWSH06: Chilling Reign","CRE","004/198","Heracross","Common","0.17","NM","English","Normal",2,"C-1",112,"4000"
"""

    def test_specifics_are_derived_from_plain_sortswift_columns(self):
        """A plain export with no C: columns still gets eBay specifics."""
        res = process_batch_csv(self.PLAIN_EXPORT_BATCH, self.db)
        header = res["add_csv"].splitlines()[0].split(",")
        for column in ("C:Game", "C:Set", "C:Card Name", "C:Card Number",
                       "C:Language", "C:Rarity", "C:Finish"):
            self.assertIn(column, header, f"{column} should be derived")

        rows = list(csv.DictReader(io.StringIO(res["add_csv"])))
        parent = next(r for r in rows if r["Title"])
        # Uniform across the group.
        self.assertEqual(parent["C:Set"], "SWSH06: Chilling Reign")
        self.assertEqual(parent["C:Language"], "English")
        self.assertEqual(parent["C:Finish"], "Normal")
        # Varies per card, so not stated at listing level.
        self.assertEqual(parent["C:Rarity"], "")
        self.assertEqual(parent["C:Card Number"], "")

    def test_configured_game_overrides_the_export_value(self):
        """eBay only accepts its own Game values, so the setting wins."""
        self.db.set_listing_settings({"default_game": "Pokemon TCG (exact)"})
        res = process_batch_csv(self.PLAIN_EXPORT_BATCH, self.db)
        for r in csv.DictReader(io.StringIO(res["add_csv"])):
            if r["Title"]:
                # Not "Pokemon", which is what the export says.
                self.assertEqual(r["C:Game"], "Pokemon TCG (exact)")

    def test_blank_game_setting_falls_back_to_the_export(self):
        self.db.set_listing_settings({"default_game": ""})
        res = process_batch_csv(self.PLAIN_EXPORT_BATCH, self.db)
        for r in csv.DictReader(io.StringIO(res["add_csv"])):
            if r["Title"]:
                self.assertEqual(r["C:Game"], "Pokemon")

    def test_explicit_c_column_beats_a_derived_one(self):
        hdr = (
            '"*C:Set","Set","Name","Market Price","Condition","Printing",'
            '"Quantity","Remarks","SKU Id","*ConditionID"'
        )
        row = (
            '"eBay Set Name","Internal Set Name","Deerling","0.17","NM","Normal",'
            '1,"C-1",111,"4000"'
        )
        res = process_batch_csv(chr(10).join([hdr, row]) + chr(10), self.db)
        rows = list(csv.DictReader(io.StringIO(res["add_csv"])))
        parent = next(r for r in rows if r["Title"])
        self.assertEqual(parent["C:Set"], "eBay Set Name")

    # ------------------------------------------------------------------
    # Download-only (dry run) regeneration
    # ------------------------------------------------------------------

    def _full_state(self):
        return (
            [tuple(sorted(r.items())) for r in self.db.export_all_manifest()],
            self.db.get_stats(),
            self.db.get_inventory(limit=1000),
        )

    def test_dry_run_writes_nothing_but_still_produces_files(self):
        process_batch_csv(self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv")
        before = self._full_state()

        res = process_batch_csv(
            self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv", dry_run=True
        )
        self.assertTrue(res["dry_run"])
        # Not reported as a duplicate: a read-only rebuild is always safe.
        self.assertFalse(res["duplicate"])
        self.assertEqual(res["add_count"], 2, "the files must still be generated")
        self.assertEqual(self._full_state(), before, "dry run mutated state")

    def test_dry_run_does_not_fingerprint_the_batch(self):
        res = process_batch_csv(
            self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv", dry_run=True
        )
        # Nothing was catalogued, so nothing could be emitted...
        self.assertEqual(res["add_count"], 0)
        # ...and the batch must remain un-fingerprinted so a real run still works.
        real = process_batch_csv(self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv")
        self.assertFalse(real["duplicate"])
        self.assertEqual(real["add_count"], 2)

    def test_dry_run_uses_current_settings_not_the_previous_output(self):
        process_batch_csv(self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv")
        self.db.set_listing_settings({
            "seller_postal_code": "10001",
            "default_game": "Changed Game",
        })
        res = process_batch_csv(
            self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv", dry_run=True
        )
        parent = next(
            r for r in csv.DictReader(io.StringIO(res["add_csv"])) if r["Title"]
        )
        # The whole point of rebuilding is to pick up changed settings.
        self.assertEqual(parent["PostalCode"], "10001")
        self.assertEqual(parent["C:Game"], "Changed Game")

    def test_dry_run_skips_cards_not_yet_catalogued(self):
        res = process_batch_csv(
            self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv", dry_run=True
        )
        self.assertEqual(res["add_count"], 0)
        self.assertEqual(res["skipped_count"], 2)
        self.assertTrue(
            any("not in the catalogue yet" in lg["message"] for lg in res["logs"]),
            "the skip reason should explain why an ID cannot be minted",
        )

    def test_dry_run_does_not_double_the_revise_quantity(self):
        process_batch_csv(self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv")
        # Put a card live on eBay so the revise path is exercised.
        self.db.upsert_variation("ID1001", "998877665544", 3)

        res = process_batch_csv(
            self.PLAIN_EXPORT_BATCH, self.db, source_name="b.csv", dry_run=True
        )
        revise = list(csv.DictReader(io.StringIO(res["revise_csv"])))
        self.assertTrue(revise)
        # The mirror already includes this batch, so the file reports it as-is
        # rather than adding the batch quantity a second time.
        self.assertEqual(revise[0]["Quantity"], "3")
        self.assertEqual(self.db.get_variation("ID1001")["last_known_qty"], 3)

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
        res = process_batch_csv(self.NUMBERED_BATCH, self.db)
        options = self._parent(res)["RelationshipDetails"].split("=", 1)[1].split(";")
        self.assertIn("Crushing Gloves (133/198)", options)

    def test_options_are_sorted_numerically_by_card_number(self):
        res = process_batch_csv(self.NUMBERED_BATCH, self.db)
        options = self._parent(res)["RelationshipDetails"].split("=", 1)[1].split(";")
        # 4 before 16 before 133 -- a string sort would give 133, 16, 4.
        self.assertEqual(
            options,
            ["Heracross (4/198)", "Deerling (16/198)", "Crushing Gloves (133/198)"],
        )
        # Child rows must follow the same order as the parent's option list.
        child_options = [
            c["RelationshipDetails"].split("=", 1)[1] for c in self._children(res)
        ]
        self.assertEqual(child_options, options)

    def test_cards_without_a_number_sort_last_and_drop_the_brackets(self):
        batch = self.NUMBERED_BATCH.replace('"16/198","Deerling"', '"","Deerling"')
        res = process_batch_csv(batch, self.db)
        options = self._parent(res)["RelationshipDetails"].split("=", 1)[1].split(";")
        self.assertEqual(options[-1], "Deerling", "no empty brackets, sorted last")

    def test_each_variation_image_is_prefixed_with_its_option_name(self):
        res = process_batch_csv(self.NUMBERED_BATCH, self.db)
        pics = {
            c["RelationshipDetails"].split("=", 1)[1]: c["PicURL"]
            for c in self._children(res)
        }
        # eBay ignores a bare URL on a child row; it must name the option.
        self.assertEqual(
            pics["Crushing Gloves (133/198)"],
            "Crushing Gloves (133/198)=https://cdn/gloves.jpg",
        )
        self.assertEqual(
            pics["Heracross (4/198)"], "Heracross (4/198)=https://cdn/heracross.jpg"
        )
        # A card with no image gets an empty cell, not a dangling separator.
        self.assertEqual(pics["Deerling (16/198)"], "")

    def test_cover_photo_setting_overrides_the_parent_image(self):
        self.db.set_listing_settings({"cover_image_url": "https://cdn/cover.jpg"})
        res = process_batch_csv(self.NUMBERED_BATCH, self.db)
        self.assertEqual(self._parent(res)["PicURL"], "https://cdn/cover.jpg")
        # Variations keep their own images regardless.
        pics = [c["PicURL"] for c in self._children(res) if c["PicURL"]]
        self.assertTrue(any("gloves.jpg" in p for p in pics))

    def test_parent_falls_back_to_the_first_cards_image(self):
        res = process_batch_csv(self.NUMBERED_BATCH, self.db)
        # First in sorted order is Heracross (4/198).
        self.assertEqual(self._parent(res)["PicURL"], "https://cdn/heracross.jpg")

    def test_option_template_is_configurable(self):
        self.db.set_listing_settings(
            {"variation_option_template": "{card_number} {name}"}
        )
        res = process_batch_csv(self.NUMBERED_BATCH, self.db)
        options = self._parent(res)["RelationshipDetails"].split("=", 1)[1].split(";")
        self.assertEqual(options[0], "4/198 Heracross")

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
        self.assertEqual(again["add_count"], 3)
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

    def test_bom_is_tolerated_by_every_module(self):
        # Module A
        db_b = Database(self.db_path + ".b")
        plain = process_batch_csv(self.NUMBERED_BATCH, db_b)
        db_b2 = Database(self.db_path + ".b2")
        with_bom = process_batch_csv(self.BOM + self.NUMBERED_BATCH, db_b2)
        self.assertEqual(plain["add_count"], with_bom["add_count"])
        self.assertEqual(with_bom["skipped_count"], 0)

        # Module C
        self._seed_live_catalog()
        orders = (
            "Sales Record Number,Order Number,Item Title,Custom Label,Quantity" + chr(10)
            + "901,ORD-901,Ledyba,ID1050-C-1,2" + chr(10)
        )
        res = process_orders_csv(self.BOM + orders, self.db)
        self.assertEqual(res["converted_count"], 2)
        self.assertEqual(res["skipped_count"], 0)

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

    def test_deduction_row_matches_the_sortswift_shape(self):
        from tcg_engine.orders import build_deduction_csv, deduction_row, SORTSWIFT_HEADERS

        process_batch_csv(self.TWO_SET_BATCH, self.db)
        card = next(c for c in self.db.get_inventory(limit=100)
                    if c["product_name"] == "Ledyba")
        full = self.db.get_manifest_by_id(card["manifest_id"])

        csv_text = build_deduction_csv([deduction_row(full, 2, "MANUAL-TEST")])
        lines = csv_text.strip().splitlines()
        self.assertEqual(lines[0], ",".join(SORTSWIFT_HEADERS))

        row = list(csv.DictReader(io.StringIO(csv_text)))[0]
        self.assertEqual(row["skuId"], "111")
        self.assertEqual(row["productId"], "542678")
        self.assertEqual(row["Order Number"], "MANUAL-TEST")
        self.assertEqual(row["Product Name"], "Ledyba")
        # Negative, because SortSwift's import adds this column to stock.
        self.assertEqual(row["Quantity"], "-2")

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

    def test_every_deduction_quantity_is_negative(self):
        """
        SortSwift's inventory import ADDS the quantity column to existing stock.
        A positive figure therefore increases inventory, which is the opposite
        of a deduction -- importing one previously showed up as "+1". Every
        quantity this engine writes into a deduction file must be negative.
        """
        self.db.insert_manifest(
            "ID1001", "Ledyba", "Chilling Reign", "NM", "Normal",
            sku_id="111", tcgplayer_id="542678")

        orders = (
            "Sales Record Number,Order Number,Item Title,Custom Label,Quantity" + chr(10)
            + "901,ORD-901,Ledyba,ID1001,3" + chr(10)
            + "902,ORD-902,Ledyba,ID1001,1" + chr(10)
        )
        res = process_orders_csv(orders, self.db)

        rows = list(csv.DictReader(io.StringIO(res["csv_content"])))
        self.assertEqual(len(rows), 2)
        quantities = [int(r["Quantity"]) for r in rows]
        self.assertEqual(quantities, [-3, -1])
        self.assertTrue(all(q < 0 for q in quantities))

        # The reported total stays positive: it is a count of cards sold, not a
        # figure written into the file.
        self.assertEqual(res["converted_count"], 4)

    def test_deduction_row_negates_whatever_sign_it_is_given(self):
        from tcg_engine.orders import deduction_row

        card = {"sku_id": "111", "tcgplayer_id": "542678", "product_name": "Ledyba",
                "set_name": "Chilling Reign", "condition": "NM", "printing": "Normal"}

        # Callers pass a positive count of cards removed...
        self.assertEqual(deduction_row(card, 2, "X")["Quantity"], -2)
        # ...and a caller that already negated must not double-negate back to
        # positive, which would silently re-add stock.
        self.assertEqual(deduction_row(card, -2, "X")["Quantity"], -2)
        self.assertEqual(deduction_row(card, 0, "X")["Quantity"], 0)

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

    def test_cover_revise_csv_uses_the_upload_column_names(self):
        from tcg_engine.batches import build_cover_photo_revise_csv

        text = build_cover_photo_revise_csv("227511361186", "https://cdn/cover.jpg")
        lines = text.strip().splitlines()
        # ItemID, not "Item Number": the latter is the Active Listings report's
        # name for it and is not a File Exchange upload column.
        self.assertEqual(lines[0], "Action,ItemID,PicURL")

        row = list(csv.DictReader(io.StringIO(text)))[0]
        self.assertEqual(row["Action"], "Revise")
        self.assertEqual(row["ItemID"], "227511361186")
        self.assertEqual(row["PicURL"], "https://cdn/cover.jpg")
        self.assertEqual(len(lines), 2, "one listing, one row")

    def test_revise_output_uses_itemid_not_item_number(self):
        """The batch Revise file is an upload, so it must use ItemID."""
        self.db.insert_manifest("ID1001", "Gengar", "Fossil", "NM", "Holofoil")
        self.db.upsert_variation("ID1001", "123456789099", 2)

        batch = (
            "Product Name,Set Name,Condition,Printing,Quantity,ConditionID" + chr(10)
            + "Gengar,Fossil,NM,Holofoil,3,4000" + chr(10)
        )
        res = process_batch_csv(batch, self.db)
        header = res["revise_csv"].splitlines()[0].split(",")
        self.assertIn("ItemID", header)
        self.assertNotIn("Item Number", header)

    # ------------------------------------------------------------------
    # Database snapshot export / inspect / restore
    # ------------------------------------------------------------------

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


if __name__ == "__main__":
    unittest.main()
