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
    # -- quantity mode -----------------------------------------------------

    def test_18_batch_quantity_mode_is_validated_and_defaults_to_set(self):
        """
        The default must be the full-dump reading, and a bad value must be
        refused rather than guessed at -- the wrong arithmetic silently
        doubles live eBay stock on every upload.
        """
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")

        res = self.client.post(
            "/api/process/batch",
            files={"file": ("dump.csv", BATCH_CSV, "text/csv")},
            data={"force": "true"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["quantity_mode"], "set")

        bad = self.client.post(
            "/api/process/batch",
            files={"file": ("dump.csv", BATCH_CSV, "text/csv")},
            data={"quantity_mode": "increment", "force": "true"},
        )
        self.assertEqual(bad.status_code, 400)
        self.assertIn("quantity_mode", bad.json()["detail"])

        ok = self.client.post(
            "/api/process/batch",
            files={"file": ("scan.csv", BATCH_CSV, "text/csv")},
            data={"quantity_mode": "add", "force": "true"},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["quantity_mode"], "add")

    def test_19_reuploading_a_dump_does_not_inflate_quantities(self):
        """The reported bug: a full dump added to itself on every upload."""
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")

        quantities = []
        for n in range(3):
            self.client.post(
                "/api/process/batch",
                files={"file": (f"dump{n}.csv", BATCH_CSV, "text/csv")},
                data={"force": "true"},
            )
            rows = self.client.get("/api/inventory",
                                   params={"limit": 200}).json()["items"]
            match = [r for r in rows if "Deerling" in r["product_name"]]
            self.assertTrue(match)
            quantities.append(match[0]["quantity"])

        self.assertEqual(len(set(quantities)), 1,
                         f"quantity drifted across uploads: {quantities}")

    # -- asset cache coherence ---------------------------------------------

    def test_20_dashboard_is_never_cached_and_assets_are_versioned(self):
        """
        A browser holding a cached index.html from an earlier deploy pairs old
        markup with a new script, which is not a slow page but a broken one:
        renaming one element id makes the script dereference null. The HTML is
        therefore no-store, and the assets it names carry a content hash so
        the pair always matches.
        """
        import re

        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("no-store", res.headers.get("cache-control", ""))

        html = res.text
        for asset in ("/static/app.js", "/static/style.css"):
            match = re.search(re.escape(asset) + r"[?]v=([0-9a-f]{12})", html)
            self.assertIsNotNone(match, asset + " should be versioned")
            # The versioned URL must actually serve.
            served = self.client.get(asset + "?v=" + match.group(1))
            self.assertEqual(served.status_code, 200, asset)

    def test_21_asset_version_tracks_content(self):
        """A hash that does not change on edit would defeat the point."""
        import os
        from app.main import _asset_version, static_dir

        path = os.path.join(static_dir, "app.js")
        with open(path, "r", encoding="utf-8") as handle:
            original = handle.read()

        before = _asset_version("/static/app.js")
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(original + chr(10) + "// touched by a test" + chr(10))
            self.assertNotEqual(_asset_version("/static/app.js"), before)
        finally:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(original)
        self.assertEqual(_asset_version("/static/app.js"), before,
                         "restoring the file must restore the hash")

        # A file that is not there must not raise; it is simply not versioned.
        self.assertEqual(_asset_version("/static/does-not-exist.js"), "")
    # -- security hardening ------------------------------------------------

    def test_22_status_and_role_are_closed_sets(self):
        """
        A free-string status is stored verbatim and then compared against
        "active" on every request, so an arbitrary value locks the account out
        in a way the UI cannot express -- and it is rendered back into the
        admin table, which made it an injection sink too.
        """
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        users = self.client.get("/api/admin/users").json()["users"]
        target = next(u for u in users if u["username"] == "Second User")

        for bad in ("<img src=x onerror=alert(1)>", "ACTIVE", "", "deleted"):
            res = self.client.post(
                f"/api/admin/users/{target['id']}/status", json={"status": bad}
            )
            self.assertEqual(res.status_code, 422, f"status={bad!r} was accepted")

        for bad in ("<script>", "superadmin", "Admin"):
            res = self.client.post(
                f"/api/admin/users/{target['id']}/role", json={"role": bad}
            )
            self.assertEqual(res.status_code, 422, f"role={bad!r} was accepted")

        # The legitimate values still work, and nothing was corrupted.
        ok = self.client.post(
            f"/api/admin/users/{target['id']}/status", json={"status": "active"}
        )
        self.assertEqual(ok.status_code, 200)
        after = self.client.get("/api/admin/users").json()["users"]
        self.assertEqual(
            next(u for u in after if u["id"] == target["id"])["status"], "active"
        )

    def test_23_oversized_uploads_are_refused(self):
        """
        Every CSV endpoint reads the body into memory, so an unbounded upload
        is a one-request memory exhaustion.
        """
        from app.main import MAX_UPLOAD_BYTES

        oversized = b"a,b,c" + b"x" * (MAX_UPLOAD_BYTES + 1024)
        for path in ("/api/process/batch", "/api/process/sync",
                     "/api/process/orders"):
            res = self.client.post(
                path, files={"file": ("big.csv", oversized, "text/csv")}
            )
            self.assertEqual(res.status_code, 413, path)
            self.assertIn("upload limit", res.json()["detail"])

        # The database restore streams to disk and is capped separately.
        res = self.client.post(
            "/api/inventory/database",
            files={"file": ("big.db", oversized, "application/octet-stream")},
        )
        self.assertEqual(res.status_code, 413)

    def test_24_security_headers_are_present(self):
        """Baseline headers on both the page and the API."""
        for path in ("/", "/api/health"):
            res = self.client.get(path)
            self.assertEqual(res.headers.get("X-Content-Type-Options"),
                             "nosniff", path)
            self.assertEqual(res.headers.get("X-Frame-Options"), "DENY", path)
            self.assertEqual(res.headers.get("Referrer-Policy"),
                             "no-referrer", path)
            csp = res.headers.get("Content-Security-Policy-Report-Only", "")
            self.assertIn("frame-ancestors 'none'", csp, path)
            self.assertIn("object-src 'none'", csp, path)
            # Google Identity Services must stay reachable or sign-in breaks.
            self.assertIn("https://accounts.google.com", csp, path)

    def test_25_health_check_does_not_leak_internals(self):
        """
        The probe is unauthenticated, so a failure must not hand back an
        exception string containing absolute paths.
        """
        from unittest.mock import patch

        with patch("app.main.db.get_stats",
                   side_effect=RuntimeError(
                       "unable to open database file /volume1/docker/secret.db")):
            res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 503)
        body = res.json()
        self.assertEqual(body["status"], "degraded")
        self.assertNotIn("detail", body)
        self.assertNotIn("/volume1", str(body))

    def test_26_manifest_export_neutralises_spreadsheet_formulas(self):
        """
        The export exists to be opened in Excel, where a cell starting with =
        is evaluated. The eBay files must NOT get this treatment, since eBay
        parses them as data.
        """
        from app.main import _csv_safe

        for dangerous in ("=1+1", "+1", "-1", "@SUM(A1)"):
            self.assertTrue(_csv_safe(dangerous).startswith("'"), dangerous)
        for safe in ("Ledyba", "004/198", "1.99", ""):
            self.assertEqual(_csv_safe(safe), safe)
        # Non-strings pass through untouched.
        self.assertEqual(_csv_safe(3), 3)
        self.assertIsNone(_csv_safe(None))

        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        res = self.client.get("/api/export/manifest")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res.headers["content-type"])

    def test_27_session_cookie_is_hardened(self):
        """HttpOnly stops script theft; SameSite blocks cross-site writes."""
        with mock.patch("app.main.verify_google_id_token",
                        return_value=google_claims("google-sub-admin",
                                                   "admin@example.com",
                                                   "Admin User")):
            res = self.client.post("/api/auth/google",
                                   json={"id_token": "stub-token"})
        raw = res.headers.get("set-cookie", "")
        self.assertIn("session_token=", raw)
        self.assertIn("HttpOnly", raw)
        self.assertIn("SameSite=lax", raw.replace("samesite", "SameSite"))

    def test_28_forged_and_tampered_session_tokens_are_rejected(self):
        """The cookie is the only credential, so its signature must hold."""
        import base64
        import json as _json

        from app.auth import create_jwt_token

        good = create_jwt_token({"user_id": 1})
        header_b64, payload_b64, sig = good.split(".")

        # Same payload, no signature.
        forged = f"{header_b64}.{payload_b64}."
        # Payload swapped to another user id, original signature kept.
        tampered_payload = base64.urlsafe_b64encode(
            _json.dumps({"user_id": 999, "exp": 9999999999}).encode()
        ).decode().rstrip("=")
        tampered = f"{header_b64}.{tampered_payload}.{sig}"
        # alg=none header, a classic JWT confusion attempt.
        none_header = base64.urlsafe_b64encode(
            _json.dumps({"alg": "none", "typ": "JWT"}).encode()
        ).decode().rstrip("=")
        alg_none = f"{none_header}.{payload_b64}."

        for label, token in (("unsigned", forged), ("tampered", tampered),
                             ("alg=none", alg_none), ("garbage", "a.b.c")):
            client = TestClient(app)
            client.cookies.set("session_token", token)
            res = client.get("/api/inventory")
            self.assertIn(res.status_code, (401, 403),
                          f"{label} token was accepted")

if __name__ == "__main__":
    unittest.main()
