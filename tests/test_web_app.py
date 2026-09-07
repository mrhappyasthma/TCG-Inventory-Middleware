import os
import tempfile
import unittest
from fastapi.testclient import TestClient

# Set test environment before importing app
temp_dir = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = os.path.join(temp_dir.name, "test_inv.db")
os.environ["USER_DATABASE_URL"] = os.path.join(temp_dir.name, "test_users.db")
os.environ["AUTH_METHOD"] = "local"
os.environ["JWT_SECRET"] = "test-secret-key-123"

from app.main import app, db, user_db


class TestWebApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        temp_dir.cleanup()

    def test_01_registration_and_first_user_admin(self):
        # Register first user -> should automatically be 'admin' and 'active'
        res = self.client.post(
            "/api/auth/register",
            json={"username": "admin_user", "password": "password123", "email": "admin@example.com"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["user"]["role"], "admin")
        self.assertEqual(data["user"]["status"], "active")

        # Register second user -> should default to 'user' and 'pending'
        res2 = self.client.post(
            "/api/auth/register",
            json={"username": "second_user", "password": "password123", "email": "second@example.com"},
        )
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertEqual(data2["user"]["role"], "user")
        self.assertEqual(data2["user"]["status"], "pending")

    def test_02_login_and_auth_status(self):
        # Login with admin
        res = self.client.post(
            "/api/auth/login",
            json={"username": "admin_user", "password": "password123"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])

        # Check /api/auth/me
        me_res = self.client.get("/api/auth/me")
        self.assertEqual(me_res.status_code, 200)
        self.assertTrue(me_res.json()["is_authenticated"])
        self.assertEqual(me_res.json()["user"]["username"], "admin_user")

    def test_03_admin_user_approval(self):
        # List users (as admin)
        res = self.client.get("/api/admin/users")
        self.assertEqual(res.status_code, 200)
        users = res.json()["users"]
        self.assertEqual(len(users), 2)

        second_user_id = next(u["id"] for u in users if u["username"] == "second_user")

        # Approve second user
        appr_res = self.client.post(
            f"/api/admin/users/{second_user_id}/status",
            json={"status": "active"},
        )
        self.assertEqual(appr_res.status_code, 200)
        self.assertEqual(appr_res.json()["new_status"], "active")

    def test_04_batch_upload_endpoint(self):
        # Upload SortSwift batch CSV
        csv_data = """"Stock Item ID","Game","File Name","Set","Set Code","Card Number","Name","Rarity","Market Price","Low Price","Mid Price","High Price","EU Price","Condition","Language","Printing","Quantity","Comment","Remarks","TCGplayer Id","SKU Id","ID Product","UPC","CDN Image","Card Back CDN Image","Cost","Price","TCGPlayer Price","Shopify Price","Cardtrader Price","Manapool Price","Misprint Price","eBay Price","Square Price"
"6a9f37cd1ffb1c868bf60ba0","Pokemon","","SV05: Temporal Forces","TEF","016/162","Deerling - 016/162","Common","0.17","0.01","0.17","19.98","","NM","EN","Normal",1,"","No Remark",542678,7805758,"760646","","https://cdn.example.com/deerling.jpg","https://cdn.example.com/back.jpg","0.00","0.15","","","","","","",""
"""
        files = {"file": ("sortswift_batch.csv", csv_data, "text/csv")}
        res = self.client.post("/api/process/batch", files=files)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["add_count"], 1)

        # Verify inventory endpoint shows cataloged item
        inv_res = self.client.get("/api/inventory")
        self.assertEqual(inv_res.status_code, 200)
        self.assertEqual(inv_res.json()["total"], 1)

    def test_05_sync_and_orders_pipeline(self):
        # Sync live eBay variation
        sync_csv = """Item number,Title,Custom label (SKU),Available quantity
112233445566,Pokemon Deerling,ID1001,5
"""
        files = {"file": ("active_listings.csv", sync_csv, "text/csv")}
        res = self.client.post("/api/process/sync", files=files)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["synced_count"], 1)

        # Process eBay order for ID1001
        order_csv = """Sales Record Number,Order Number,Item Title,Custom Label,Quantity
901,ORD-901,Pokemon Deerling,ID1001,2
"""
        order_files = {"file": ("ebay_orders.csv", order_csv, "text/csv")}
        order_res = self.client.post("/api/process/orders", files=order_files)
        self.assertEqual(order_res.status_code, 200)
        data = order_res.json()
        self.assertEqual(data["converted_count"], 2)
        # Check generated SortSwift output
        self.assertIn("7805758", data["csv_content"])
        self.assertIn("ORD-901", data["csv_content"])

    def test_06_pricing_rules_api(self):
        # Fetch pricing rules
        res = self.client.get("/api/pricing-rules")
        self.assertEqual(res.status_code, 200)
        rules = res.json()["rules"]
        self.assertGreaterEqual(len(rules), 4)

        # Test preview calculation
        prev_res = self.client.post("/api/pricing-rules/preview", json={"price": 0.20})
        self.assertEqual(prev_res.status_code, 200)
        self.assertEqual(prev_res.json()["calculated_price"], 1.99)

        prev_res2 = self.client.post("/api/pricing-rules/preview", json={"price": 5.00})
        self.assertEqual(prev_res2.status_code, 200)
        self.assertEqual(prev_res2.json()["calculated_price"], 8.00)  # 5.00 + 3.00

    def test_07_listing_settings_api(self):
        # Update listing settings
        settings_payload = {
            "settings": {
                "single_threshold": "6.00",
                "variation_title_template": "{set_name}: Pick Your Card - Near Mint - Complete Your Set"
            }
        }
        res = self.client.post("/api/listing-settings", json=settings_payload)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["settings"]["single_threshold"], "6.00")

        # Test title preview endpoint
        preview_res = self.client.post(
            "/api/listing-settings/preview-title",
            json={"set_name": "SV05: Temporal Forces"}
        )
        self.assertEqual(preview_res.status_code, 200)
        self.assertEqual(preview_res.json()["generated_title"], "SV05: Temporal Forces: Pick Your Card - Near Mint - Complete Your Set")
        self.assertTrue(preview_res.json()["is_valid"])


if __name__ == "__main__":
    unittest.main()
