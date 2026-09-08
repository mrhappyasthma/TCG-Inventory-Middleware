import io
import os
import tempfile
import unittest
import zipfile
from unittest import mock

from fastapi.testclient import TestClient

# Configure the environment before importing the app. app.auth refuses to import
# without a Google client ID, and an explicit JWT_SECRET keeps the test run from
# generating a persistent session-secret file.
temp_dir = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = os.path.join(temp_dir.name, "test_inv.db")
os.environ["USER_DATABASE_URL"] = os.path.join(temp_dir.name, "test_users.db")
os.environ["GOOGLE_CLIENT_ID"] = "test-client-id.apps.googleusercontent.com"
os.environ["JWT_SECRET"] = "test-secret-key-123"
os.environ["COOKIE_SECURE"] = "false"

from app.main import app, db, user_db  # noqa: E402

# A SortSwift export row, reused so the duplicate-batch guard can be exercised.
BATCH_CSV = """"Stock Item ID","Game","File Name","Set","Set Code","Card Number","Name","Rarity","Market Price","Low Price","Mid Price","High Price","EU Price","Condition","Language","Printing","Quantity","Comment","Remarks","TCGplayer Id","SKU Id","ID Product","UPC","CDN Image","Card Back CDN Image","Cost","Price","TCGPlayer Price","Shopify Price","Cardtrader Price","Manapool Price","Misprint Price","eBay Price","Square Price","*ConditionID"
"6a9f37cd1ffb1c868bf60ba0","Pokemon","","SV05: Temporal Forces","TEF","016/162","Deerling - 016/162","Common","0.17","0.01","0.17","19.98","","NM","EN","Normal",1,"","Bin A-12",542678,7805758,"760646","","https://cdn.example.com/deerling.jpg","https://cdn.example.com/back.jpg","0.00","0.15","","","","","","","","4000"
"""


def google_claims(sub, email, name):
    """Build a minimal set of verified Google ID token claims."""
    return {
        "iss": "https://accounts.google.com",
        "sub": sub,
        "email": email,
        "email_verified": True,
        "name": name,
        "aud": os.environ["GOOGLE_CLIENT_ID"],
    }


class TestWebApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        temp_dir.cleanup()

    def sign_in(self, sub, email, name):
        """
        Sign in as a Google user, stubbing token verification.

        The verifier is patched where main.py imported it, so the real Google
        network call is never made, while the endpoint's own logic still runs.
        """
        with mock.patch(
            "app.main.verify_google_id_token",
            return_value=google_claims(sub, email, name),
        ):
            return self.client.post(
                "/api/auth/google", json={"id_token": "stub-token"}
            )

    # -- authentication ----------------------------------------------------

    def test_01_first_google_user_becomes_admin(self):
        res = self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["user"]["role"], "admin")
        self.assertEqual(data["user"]["status"], "active")
        self.assertEqual(data["user"]["auth_provider"], "google")
        self.assertEqual(data["user"]["google_sub"], "google-sub-admin")

    def test_02_auth_status_reflects_session(self):
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        body = me.json()
        self.assertTrue(body["is_authenticated"])
        self.assertEqual(body["user"]["username"], "Admin User")
        self.assertEqual(body["google_client_id"], os.environ["GOOGLE_CLIENT_ID"])
        # The removed local-auth switch must not leak back into the payload.
        self.assertNotIn("auth_method", body)

    def test_03_invalid_google_token_is_rejected(self):
        with mock.patch("app.main.verify_google_id_token", return_value=None):
            res = self.client.post("/api/auth/google", json={"id_token": "bad"})
        self.assertEqual(res.status_code, 401)
        # The rejected attempt must not have disturbed the existing session.
        self.assertTrue(self.client.get("/api/auth/me").json()["is_authenticated"])

    def test_04_second_user_is_pending_then_approved(self):
        res = self.sign_in("google-sub-second", "second@example.com", "Second User")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["user"]["role"], "user")
        self.assertEqual(data["user"]["status"], "pending")
        self.assertTrue(data["is_pending"])

        # A pending account cannot touch the data endpoints.
        self.assertEqual(self.client.get("/api/inventory").status_code, 403)

        # Back to the admin session to approve them.
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        users = self.client.get("/api/admin/users").json()["users"]
        self.assertEqual(len(users), 2)
        second_id = next(u["id"] for u in users if u["username"] == "Second User")

        appr = self.client.post(
            f"/api/admin/users/{second_id}/status", json={"status": "active"}
        )
        self.assertEqual(appr.status_code, 200)
        self.assertEqual(appr.json()["new_status"], "active")

    def test_05_local_auth_endpoints_are_gone(self):
        for path in ("/api/auth/login", "/api/auth/register"):
            res = self.client.post(
                path, json={"username": "admin_user", "password": "password123"}
            )
            self.assertEqual(res.status_code, 404, f"{path} should no longer exist")

    def test_06_unauthenticated_requests_are_rejected(self):
        anon = TestClient(app)
        self.assertEqual(anon.get("/api/inventory").status_code, 401)
        self.assertEqual(anon.get("/api/stats").status_code, 401)
        self.assertEqual(anon.get("/api/admin/users").status_code, 401)

    def test_07_health_endpoint_is_public(self):
        anon = TestClient(app)
        res = anon.get("/api/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "ok")

    # -- processing pipeline ----------------------------------------------

    def test_08_batch_upload_endpoint(self):
        files = {"file": ("sortswift_batch.csv", BATCH_CSV, "text/csv")}
        res = self.client.post("/api/process/batch", files=files)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["duplicate"])
        self.assertEqual(data["add_count"], 1)

        inv_res = self.client.get("/api/inventory")
        self.assertEqual(inv_res.status_code, 200)
        self.assertEqual(inv_res.json()["total"], 1)

    def test_09_duplicate_batch_is_refused_then_forced(self):
        files = {"file": ("sortswift_batch.csv", BATCH_CSV, "text/csv")}
        res = self.client.post("/api/process/batch", files=files)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["duplicate"], "identical batch should be flagged")
        self.assertEqual(data["add_count"], 0)
        self.assertEqual(data["revise_count"], 0)

        # Catalog untouched by the refused upload.
        self.assertEqual(self.client.get("/api/inventory").json()["total"], 1)

        # An explicit force re-applies it.
        forced = self.client.post(
            "/api/process/batch",
            files={"file": ("sortswift_batch.csv", BATCH_CSV, "text/csv")},
            data={"force": "true"},
        )
        self.assertEqual(forced.status_code, 200)
        self.assertFalse(forced.json()["duplicate"])

    def test_10_search_and_count_agree(self):
        """
        Regression: get_inventory and get_inventory_count searched different
        column sets, so a bin-location search returned rows while the paging
        total under-reported them.
        """
        # A second card whose bin and SKU differ, so each search term below
        # genuinely discriminates rather than matching everything.
        self.client.post(
            "/api/inventory/add",
            json={
                "product_name": "Charizard",
                "set_name": "Base Set",
                "condition": "Lightly Played",
                "printing": "Holofoil",
            },
        )

        for term, expected in [
            ("Bin A-12", 1),
            ("Deerling", 1),
            ("7805758", 1),
            ("Charizard", 1),
            ("Base Set", 1),
            ("nonexistent-xyz", 0),
        ]:
            res = self.client.get(
                "/api/inventory", params={"search": term, "limit": 1000}
            )
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(
                len(body["items"]),
                body["total"],
                f"row count and total disagree for search {term!r}",
            )
            self.assertEqual(
                body["total"], expected, f"unexpected match count for {term!r}"
            )

    def test_11_remarks_column_is_sortable(self):
        res = self.client.get(
            "/api/inventory", params={"sort_by": "remarks", "sort_dir": "DESC"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["items"][0]["remarks"], "Bin A-12")

    def test_12_sync_and_orders_pipeline(self):
        sync_csv = (
            "Item number,Title,Custom label (SKU),Available quantity\n"
            "112233445566,Pokemon Deerling,ID1001-Bin_A-12,5\n"
        )
        res = self.client.post(
            "/api/process/sync",
            files={"file": ("active_listings.csv", sync_csv, "text/csv")},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["synced_count"], 1)

        order_csv = (
            "Sales Record Number,Order Number,Item Title,Custom Label,Quantity\n"
            "901,ORD-901,Pokemon Deerling,ID1001-Bin_A-12,2\n"
        )
        order_res = self.client.post(
            "/api/process/orders",
            files={"file": ("ebay_orders.csv", order_csv, "text/csv")},
        )
        self.assertEqual(order_res.status_code, 200)
        data = order_res.json()
        self.assertEqual(data["converted_count"], 2)
        self.assertIn("7805758", data["csv_content"])
        self.assertIn("ORD-901", data["csv_content"])

    # -- configuration ----------------------------------------------------

    def test_13_pricing_rules_api(self):
        res = self.client.get("/api/pricing-rules")
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(len(res.json()["rules"]), 4)

        prev = self.client.post("/api/pricing-rules/preview", json={"price": 0.20})
        self.assertEqual(prev.status_code, 200)
        self.assertEqual(prev.json()["calculated_price"], 1.99)

        prev2 = self.client.post("/api/pricing-rules/preview", json={"price": 5.00})
        self.assertEqual(prev2.status_code, 200)
        self.assertEqual(prev2.json()["calculated_price"], 8.00)  # 5.00 + 3.00

    def test_14_listing_settings_api(self):
        payload = {
            "settings": {
                "single_threshold": "6.00",
                "group_by_set": "false",
                "variation_title_template": "{set_name}: Pick Your Card - {condition} - Complete Your Set",
            }
        }
        res = self.client.post("/api/listing-settings", json=payload)
        self.assertEqual(res.status_code, 200)
        settings = res.json()["settings"]
        self.assertEqual(settings["single_threshold"], "6.00")
        self.assertEqual(settings["group_by_set"], "false")

        preview = self.client.post(
            "/api/listing-settings/preview-title",
            json={"set_name": "SV05: Temporal Forces", "condition": "Near Mint"},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(
            preview.json()["generated_title"],
            "SV05: Temporal Forces: Pick Your Card - Near Mint - Complete Your Set",
        )
        self.assertTrue(preview.json()["is_valid"])

        # Restore the default so ordering between tests cannot surprise us.
        self.client.post(
            "/api/listing-settings",
            json={"settings": {"single_threshold": "5.00", "group_by_set": "true"}},
        )

    # -- per-user rules ----------------------------------------------------

    def approved_non_admin_client(self):
        """
        A second signed-in session, in its own client so the cookie jars do
        not collide. Reuses the account test_04 already approved.
        """
        other = TestClient(app)
        with mock.patch(
            "app.main.verify_google_id_token",
            return_value=google_claims("google-sub-second", "second@example.com",
                                       "Second User"),
        ):
            res = other.post("/api/auth/google", json={"id_token": "stub-token"})
        self.assertEqual(res.json()["user"]["role"], "user")
        self.assertEqual(res.json()["user"]["status"], "active")
        return other

    def test_15_rules_are_per_user_and_not_admin_gated(self):
        """
        A non-admin may edit their own rules, and doing so must not touch the
        admin's. Both endpoints were admin-only before rules became per-user.
        """
        other = self.approved_non_admin_client()
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")

        before = self.client.get("/api/pricing-rules").json()
        self.assertFalse(before["is_own"], "the admin has not saved their own")

        res = other.post("/api/pricing-rules", json={"rules": [
            {"min_price": 0.0, "max_price": None, "rule_type": "fixed",
             "rule_value": 9.99, "sort_order": 1},
        ]})
        self.assertEqual(res.status_code, 200, "a non-admin may save their own")
        self.assertTrue(res.json()["is_own"])

        self.assertEqual(
            other.post("/api/pricing-rules/preview",
                       json={"price": 0.30}).json()["calculated_price"], 9.99)
        self.assertEqual(
            self.client.post("/api/pricing-rules/preview",
                             json={"price": 0.30}).json()["calculated_price"], 2.49)
        self.assertEqual(self.client.get("/api/pricing-rules").json()["rules"],
                         before["rules"], "the admin's rules must not move")

        # Reset gives the inherited set back.
        undone = other.post("/api/pricing-rules/reset").json()
        self.assertFalse(undone["is_own"])
        self.assertEqual(
            other.post("/api/pricing-rules/preview",
                       json={"price": 0.30}).json()["calculated_price"], 2.49)

        # Anonymous callers are still refused.
        anon = TestClient(app)
        self.assertEqual(anon.post("/api/pricing-rules",
                                   json={"rules": []}).status_code, 401)

    def test_15b_listing_settings_merge_per_user(self):
        """One override must not hide the inherited keys, and reset undoes it."""
        other = self.approved_non_admin_client()
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")

        baseline_zip = db.get_listing_setting("seller_postal_code")

        self.assertEqual(
            other.post("/api/listing-settings",
                       json={"settings": {"seller_postal_code": "10001"}}
                       ).status_code, 200, "a non-admin may save their own")

        mine = other.get("/api/listing-settings").json()
        theirs = self.client.get("/api/listing-settings").json()
        self.assertEqual(mine["settings"]["seller_postal_code"], "10001")
        self.assertIn("seller_postal_code", mine["own_keys"])
        self.assertNotIn("seller_postal_code", theirs["own_keys"])
        self.assertEqual(theirs["settings"]["seller_postal_code"], baseline_zip)

        # Same key set: overriding one field does not drop the inherited rest.
        self.assertEqual(set(mine["settings"]), set(theirs["settings"]))

        reset = other.post("/api/listing-settings/reset").json()
        self.assertEqual(reset["own_keys"], [])
        self.assertEqual(reset["settings"]["seller_postal_code"], baseline_zip)

    # -- database downloads ------------------------------------------------

    def test_16_every_database_file_is_downloadable_by_admin(self):
        """Each database downloads as a real SQLite snapshot, plus a zip of all."""
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")

        listing = self.client.get("/api/database/files")
        self.assertEqual(listing.status_code, 200)
        names = {f["name"] for f in listing.json()["files"]}
        self.assertEqual(names, {"inventory", "users"})
        for entry in listing.json()["files"]:
            for key in ("label", "description", "filename", "size_bytes",
                        "summary"):
                self.assertIn(key, entry)

        magic = b"SQLite format 3" + bytes([0])
        for name in ("inventory", "users"):
            res = self.client.get("/api/database/download/" + name)
            self.assertEqual(res.status_code, 200, name)
            self.assertTrue(res.content.startswith(magic), name)
            self.assertIn("attachment",
                          res.headers.get("content-disposition", ""))

        self.assertEqual(
            self.client.get("/api/database/download/nope").status_code, 404)

        bundle = self.client.get("/api/database/bundle")
        self.assertEqual(bundle.status_code, 200)
        self.assertEqual(bundle.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            self.assertIsNone(archive.testzip())
            members = archive.namelist()
            self.assertIn("README.txt", members)
            dbs = [m for m in members if m.endswith(".db")]
            self.assertEqual(len(dbs), 2)
            for member in dbs:
                self.assertTrue(archive.read(member).startswith(magic), member)

    def test_17_database_downloads_require_admin(self):
        """Approved is not enough; every download is administrative."""
        other = self.approved_non_admin_client()

        for path in ("/api/database/files",
                     "/api/database/download/inventory",
                     "/api/database/download/users",
                     "/api/database/bundle",
                     "/api/inventory/database"):
            self.assertEqual(other.get(path).status_code, 403, path)

        anon = TestClient(app)
        for path in ("/api/database/files", "/api/database/bundle"):
            self.assertEqual(anon.get(path).status_code, 401, path)

        # Restore stays admin-only too.
        self.assertEqual(
            other.post("/api/inventory/database",
                       files={"file": ("x.db", b"x", "application/octet-stream")},
                       data={"confirm": "false"}).status_code, 403)

        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")

if __name__ == "__main__":
    unittest.main()
