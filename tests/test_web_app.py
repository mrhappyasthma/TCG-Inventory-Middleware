import hashlib
import io
import json
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
# The background price refresh makes outbound HTTP calls. Tests must
# never depend on the network, and never poke a third-party service.
os.environ["PRICE_REFRESH_ENABLED"] = "false"
# eBay credentials, so the notification endpoints are exercisable. These are
# not real and no test reaches the network: the client is replaced with one
# holding a stubbed key fetcher wherever a signature has to be verified.
os.environ["EBAY_CLIENT_ID"] = "test-ebay-client-id"
os.environ["EBAY_CLIENT_SECRET"] = "test-ebay-client-secret"
os.environ["EBAY_REDIRECT_URI"] = "Test-RuName-abc123"
os.environ["EBAY_ENVIRONMENT"] = "sandbox"
os.environ["EBAY_VERIFICATION_TOKEN"] = "t" * 40
os.environ["EBAY_NOTIFICATION_ENDPOINT"] = "https://cards.example.com/api/ebay/notifications"

from app import main  # noqa: E402
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

        # Binary, deliberately. This test rewrites a real source file, and
        # reading as text translates CRLF to \n while writing back with
        # newline="" does not translate it back -- so on a CRLF working copy
        # the "restore" silently converted app.js to LF, the hash no longer
        # matched, and the file was left modified. _asset_version hashes
        # bytes, so the test has to preserve bytes.
        path = os.path.join(static_dir, "app.js")
        with open(path, "rb") as handle:
            original = handle.read()

        before = _asset_version("/static/app.js")
        try:
            with open(path, "wb") as handle:
                handle.write(original + b"\n// touched by a test\n")
            self.assertNotEqual(_asset_version("/static/app.js"), before)
        finally:
            with open(path, "wb") as handle:
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

    # -- draft plans -------------------------------------------------------

    def test_29_a_draft_plan_can_be_built_edited_and_approved(self):
        """
        The whole drafts round trip, which is the staging path that replaces
        generating a CSV and uploading it by hand.
        """
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")

        # The catalogue already holds the card from the batch tests above.
        built = self.client.post("/api/plans/build", json={"source": "manual"})
        self.assertEqual(built.status_code, 200)
        plan_id = built.json()["plan_id"]
        self.assertGreater(built.json()["item_count"], 0)

        detail = self.client.get(f"/api/plans/{plan_id}")
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertEqual(body["plan"]["status"], "draft")
        self.assertTrue(body["items"])
        item = body["items"][0]

        # An edit re-validates immediately, so the page never shows a blocker
        # for a problem that has just been fixed.
        edited = self.client.patch(
            f"/api/plans/items/{item['id']}",
            json={"proposed_qty": 4, "proposed_price": 1.25},
        )
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json()["item"]["proposed_qty"], 4)
        self.assertAlmostEqual(edited.json()["item"]["proposed_price"], 1.25)

        approved = self.client.post(f"/api/plans/{plan_id}/approve")
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(
            self.client.get(f"/api/plans/{plan_id}").json()["plan"]["status"],
            "approved",
        )

    def test_30_an_approved_plan_can_no_longer_be_edited(self):
        """
        Approval is the authorisation record for an eBay write. Editing after
        it would change what gets pushed, making the approval a record of
        something that never happened.
        """
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        approved = [
            p for p in self.client.get("/api/plans").json()["plans"]
            if p["status"] == "approved"
        ]
        self.assertTrue(approved, "test 29 should have left an approved plan")
        plan_id = approved[0]["id"]

        items = self.client.get(f"/api/plans/{plan_id}").json()["items"]
        res = self.client.patch(
            f"/api/plans/items/{items[0]['id']}", json={"proposed_qty": 9}
        )
        self.assertEqual(res.status_code, 409)

    def test_30b_an_approved_plan_can_be_deleted_but_a_pushed_one_cannot(self):
        """
        The line is whether the plan reached eBay, not whether it was approved.

        An approval nobody acted on is a decision that was changed, and the
        drafts page accumulates them. A pushed plan is the only record of who
        authorised a live change, so deleting it would destroy the audit trail
        the approval exists to create.
        """
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        approved = [
            p for p in self.client.get("/api/plans").json()["plans"]
            if p["status"] == "approved"
        ]
        self.assertTrue(approved, "test 29 should have left an approved plan")
        plan_id = approved[0]["id"]

        # Pretend it was pushed. Refused, and the reason says why.
        db.set_plan_status(plan_id, "pushed")
        res = self.client.delete(f"/api/plans/{plan_id}")
        self.assertEqual(res.status_code, 409, res.text)
        self.assertIn("reached eBay", res.json()["detail"])

        # Back to approved: now it goes, and it goes out of the listing too.
        db.set_plan_status(plan_id, "approved")
        self.assertEqual(self.client.delete(f"/api/plans/{plan_id}").status_code, 200)
        remaining = [
            p["id"] for p in self.client.get("/api/plans").json()["plans"]
        ]
        self.assertNotIn(plan_id, remaining)
        # And it is gone for good rather than merely hidden.
        self.assertEqual(self.client.get(f"/api/plans/{plan_id}").status_code, 404)

    def test_30c_pushing_is_refused_for_a_draft_and_without_a_connection(self):
        """
        The push is the only endpoint that changes a live listing, so its
        preconditions are checked before anything is sent.

        A draft has no approval behind it, and an unconnected account means
        acting as a seller who has not consented -- eBay would refuse, but
        finding out from a 403 halfway through a 400-card push is far worse
        than refusing up front.
        """
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        plan_id = self.client.post(
            "/api/plans/build", json={"source": "manual"}
        ).json()["plan_id"]
        res = self.client.post(f"/api/plans/{plan_id}/push")
        self.assertEqual(res.status_code, 409, res.text)
        self.assertIn("Approve", res.json()["detail"])

        # Approved, but no eBay account is connected in this test database.
        approved = self.client.post(f"/api/plans/{plan_id}/approve")
        if approved.status_code == 200:
            res = self.client.post(f"/api/plans/{plan_id}/push")
            self.assertIn(res.status_code, (409, 503), res.text)
            self.assertNotEqual(
                self.client.get(f"/api/plans/{plan_id}").json()["plan"]["status"],
                "pushed",
                "nothing may be marked pushed when nothing was sent",
            )

    def test_30d_another_users_plan_cannot_be_pushed(self):
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        plan_id = self.client.post(
            "/api/plans/build", json={"source": "manual"}
        ).json()["plan_id"]
        other = self.approved_non_admin_client()
        self.assertEqual(
            other.post(f"/api/plans/{plan_id}/push").status_code, 404
        )

    def test_31_plan_item_edits_are_validated(self):
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        plan_id = self.client.post(
            "/api/plans/build", json={"source": "manual"}
        ).json()["plan_id"]
        items = self.client.get(f"/api/plans/{plan_id}").json()["items"]
        if not items:
            self.skipTest("nothing left to change after the previous approval")
        item_id = items[0]["id"]

        for payload in ({"proposed_qty": -1}, {"proposed_price": 0}):
            res = self.client.patch(f"/api/plans/items/{item_id}", json=payload)
            self.assertEqual(res.status_code, 400, f"{payload} was accepted")
        # An empty patch is a client bug, not a no-op success.
        self.assertEqual(
            self.client.patch(f"/api/plans/items/{item_id}", json={}).status_code,
            400,
        )
        # status is a closed set: a free string would be stored and rendered.
        self.assertEqual(
            self.client.patch(
                f"/api/plans/items/{item_id}", json={"status": "pushed"}
            ).status_code,
            422,
        )

    def test_32_plans_are_scoped_to_their_owner(self):
        """
        A plan carries whose intent it was and who approved it, so one user
        must not be able to read or approve another's draft.
        """
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        plan_id = self.client.post(
            "/api/plans/build", json={"source": "manual"}
        ).json()["plan_id"]

        # The second user approved back in test 04, on their own session.
        other = TestClient(app)
        with mock.patch(
            "app.main.verify_google_id_token",
            return_value=google_claims(
                "google-sub-second", "second@example.com", "Second User"
            ),
        ):
            signed_in = other.post("/api/auth/google", json={"id_token": "stub"})
        self.assertEqual(signed_in.json()["user"]["status"], "active")

        self.assertEqual(other.get(f"/api/plans/{plan_id}").status_code, 404)
        self.assertEqual(
            other.post(f"/api/plans/{plan_id}/approve").status_code, 404
        )
        self.assertEqual(other.delete(f"/api/plans/{plan_id}").status_code, 404)
        # And the other user's own list does not include it.
        self.assertNotIn(
            plan_id, [p["id"] for p in other.get("/api/plans").json()["plans"]]
        )

    def test_33_draft_endpoints_require_authentication(self):
        anonymous = TestClient(app)
        for method, path in (
            ("get", "/api/plans"),
            ("post", "/api/plans/build"),
            ("get", "/api/plans/1"),
            ("post", "/api/plans/1/approve"),
            ("delete", "/api/plans/1"),
            ("patch", "/api/plans/items/1"),
        ):
            # request() rather than the verb helpers: TestClient.get and
            # .delete do not take a json body.
            res = anonymous.request(method.upper(), path, json={})
            self.assertIn(
                res.status_code,
                (401, 403),
                f"{method.upper()} {path} was reachable without a session",
            )

    # -- eBay account deletion notifications -------------------------------

    def test_34_notification_challenge_hashes_the_configured_endpoint(self):
        """
        eBay validates the endpoint with a GET challenge. The URL hashed must
        be the configured public one, not what this process sees: behind a
        reverse proxy they differ, and the mismatch fails validation with an
        error that never explains itself.
        """
        anonymous = TestClient(app)
        res = anonymous.get(
            "/api/ebay/notifications", params={"challenge_code": "CODE-123"}
        )
        self.assertEqual(res.status_code, 200)

        expected = hashlib.sha256(
            b"CODE-123"
            + os.environ["EBAY_VERIFICATION_TOKEN"].encode()
            + os.environ["EBAY_NOTIFICATION_ENDPOINT"].encode()
        ).hexdigest()
        self.assertEqual(res.json()["challengeResponse"], expected)

    def test_35_challenge_requires_a_code(self):
        self.assertEqual(
            TestClient(app).get("/api/ebay/notifications").status_code, 400
        )

    def test_36_an_unsigned_notification_is_refused_with_412(self):
        """
        Anyone who learns this URL can post to it, so an unverified payload is
        an anonymous request that merely looks like eBay. It must never be
        acknowledged with a 200.
        """
        res = TestClient(app).post(
            "/api/ebay/notifications", json={"metadata": {"topic": "X"}}
        )
        self.assertEqual(res.status_code, 412)

    def test_37_a_genuinely_signed_notification_is_acknowledged(self):
        """
        Round-trips a real ECDSA signature against a locally generated key,
        with the key fetch stubbed so nothing touches the network.
        """
        import base64
        import json as _js

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        from ebay_client.notifications import PublicKeyCache

        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

        body = b'{"metadata":{"topic":"MARKETPLACE_ACCOUNT_DELETION"}}'
        signature = private_key.sign(body, ec.ECDSA(hashes.SHA1()))
        header = base64.b64encode(
            _js.dumps(
                {
                    "alg": "ECDSA",
                    "kid": "key-1",
                    "signature": base64.b64encode(signature).decode(),
                    "digest": "SHA1",
                }
            ).encode()
        ).decode()

        client = main.get_ebay_client()
        self.assertIsNotNone(client, "eBay env vars should make a client available")
        original = client.public_keys
        client.public_keys = PublicKeyCache(lambda _kid: pem)
        try:
            res = TestClient(app).post(
                "/api/ebay/notifications",
                content=body,
                headers={
                    "X-EBAY-SIGNATURE": header,
                    "Content-Type": "application/json",
                },
            )
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(res.json()["status"], "acknowledged")

            # A tampered body must not verify against the same signature.
            tampered = TestClient(app).post(
                "/api/ebay/notifications",
                content=b'{"metadata":{"topic":"SOMETHING_ELSE"}}',
                headers={
                    "X-EBAY-SIGNATURE": header,
                    "Content-Type": "application/json",
                },
            )
            self.assertEqual(tampered.status_code, 412)
        finally:
            client.public_keys = original

    def test_38_notification_endpoints_need_no_session(self):
        """
        eBay has no session with us, so these two must work unauthenticated --
        and must be the only endpoints that do besides the health probe.
        """
        anonymous = TestClient(app)
        self.assertNotIn(
            anonymous.get(
                "/api/ebay/notifications", params={"challenge_code": "C"}
            ).status_code,
            (401, 403),
        )
        self.assertNotIn(
            anonymous.post("/api/ebay/notifications", json={}).status_code,
            (401, 403),
        )

    def test_38b_the_challenge_works_before_any_keyset_credentials_exist(self):
        """
        The bootstrap order eBay forces on you: the keyset is disabled until
        this endpoint validates, and the RuName is registered later still. So
        the challenge must answer with only the token and the endpoint URL.
        """
        saved = main._ebay_client
        main._ebay_client = None
        try:
            with mock.patch.object(
                main.EbayConfig, "is_configured", return_value=False
            ):
                res = TestClient(app).get(
                    "/api/ebay/notifications", params={"challenge_code": "BOOT"}
                )
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(
                res.json()["challengeResponse"],
                hashlib.sha256(
                    b"BOOT"
                    + os.environ["EBAY_VERIFICATION_TOKEN"].encode()
                    + os.environ["EBAY_NOTIFICATION_ENDPOINT"].encode()
                ).hexdigest(),
            )
        finally:
            main._ebay_client = saved

    def test_39_an_unconfigured_deployment_reports_503_not_a_crash(self):
        """
        The app must keep working as a CSV tool with no eBay credentials, and
        say so plainly rather than raising.
        """
        saved_client = main._ebay_client
        saved_endpoint = main.EBAY_NOTIFICATION_ENDPOINT
        main._ebay_client = None
        main.EBAY_NOTIFICATION_ENDPOINT = ""
        try:
            with mock.patch.object(
                main.EbayConfig, "is_configured", return_value=False
            ):
                anonymous = TestClient(app)
                self.assertEqual(
                    anonymous.get(
                        "/api/ebay/notifications", params={"challenge_code": "C"}
                    ).status_code,
                    503,
                )
                self.assertEqual(
                    anonymous.post("/api/ebay/notifications", json={}).status_code,
                    503,
                )
        finally:
            main._ebay_client = saved_client
            main.EBAY_NOTIFICATION_ENDPOINT = saved_endpoint

    def test_39a_a_draft_cover_photo_is_staged_not_applied(self):
        """
        A cover chosen in a draft must not reach ebay_listing_overrides until
        the plan is approved -- that table is what the store currently has, so
        writing to it would apply the change before it was authorised.
        """
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        plan_id = self.client.post(
            "/api/plans/build", json={"source": "manual"}
        ).json()["plan_id"]
        groups = self.client.get(f"/api/plans/{plan_id}").json()["groups"]
        if not groups:
            self.skipTest("no groups in the current draft")
        group_key = groups[0]["group_key"]

        res = self.client.post(
            f"/api/plans/{plan_id}/cover",
            json={
                "group_key": group_key,
                "cover_image_url": "https://cdn.example.com/cover.jpg",
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        staged = next(
            g for g in res.json()["groups"] if g["group_key"] == group_key
        )
        self.assertEqual(staged["cover_image_url"], "https://cdn.example.com/cover.jpg")
        self.assertTrue(staged["cover_is_staged"])

        # The live store's own record is untouched.
        parent = staged.get("ebay_parent_id")
        if parent:
            self.assertNotEqual(
                db.get_listing_cover_image(parent),
                "https://cdn.example.com/cover.jpg",
            )

        # Clearing falls back rather than storing a blank.
        cleared = self.client.post(
            f"/api/plans/{plan_id}/cover",
            json={"group_key": group_key, "cover_image_url": ""},
        )
        self.assertEqual(cleared.status_code, 200)
        after = next(
            g for g in cleared.json()["groups"] if g["group_key"] == group_key
        )
        self.assertFalse(after["cover_is_staged"])

    def test_39b_a_cover_url_must_be_fetchable_by_ebay(self):
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        plan_id = self.client.post(
            "/api/plans/build", json={"source": "manual"}
        ).json()["plan_id"]
        groups = self.client.get(f"/api/plans/{plan_id}").json()["groups"]
        if not groups:
            self.skipTest("no groups in the current draft")
        res = self.client.post(
            f"/api/plans/{plan_id}/cover",
            json={
                "group_key": groups[0]["group_key"],
                # eBay fetches the picture itself, so a local path is useless
                # to it and would fail at upload rather than here.
                "cover_image_url": "C:\\Users\\Mark\\Desktop\\card.jpg",
            },
        )
        self.assertEqual(res.status_code, 400)

    def test_39b2_an_approved_plan_yields_the_files_it_authorised(self):
        """
        The whole point of approving. Before this existed, Module A generated
        the Add and Revise files at upload time -- before the draft -- so a
        regrouped card, an edited price or an excluded card could never reach
        eBay at all.
        """
        import csv as _csv

        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        plan_id = self.client.post(
            "/api/plans/build", json={"source": "manual"}
        ).json()["plan_id"]
        items = self.client.get(f"/api/plans/{plan_id}").json()["items"]
        if not items:
            self.skipTest("nothing to change in the current draft")

        # An edit that must survive into the file.
        self.client.patch(
            f"/api/plans/items/{items[0]['id']}",
            json={"proposed_qty": 7, "proposed_price": 3.5},
        )
        approved = self.client.post(f"/api/plans/{plan_id}/approve")
        self.assertEqual(approved.status_code, 200, approved.text)

        summary = self.client.get(f"/api/plans/{plan_id}/files")
        self.assertEqual(summary.status_code, 200, summary.text)
        body = summary.json()
        # Either file is a valid outcome: a plan over cards already on eBay
        # produces Revise rows and creates no listings at all. What must not
        # happen is a plan that authorised changes producing nothing.
        self.assertGreaterEqual(
            body["listing_count"] + body["revise_count"], 1,
            f"an approved plan produced no rows at all: {body}",
        )

        # Whichever file this plan produced must contain the edited figures.
        if body["add_card_count"]:
            res = self.client.get(f"/api/plans/{plan_id}/add.csv")
            self.assertEqual(res.status_code, 200)
            rows = list(_csv.DictReader(io.StringIO(res.text)))
            edited = [r for r in rows if r.get("Quantity") == "7"]
            self.assertTrue(edited, "the edited quantity is missing from the Add file")
            self.assertEqual(edited[0]["StartPrice"], "3.50")
        else:
            res = self.client.get(f"/api/plans/{plan_id}/revise.csv")
            self.assertEqual(res.status_code, 200)
            rows = list(_csv.DictReader(io.StringIO(res.text)))
            self.assertTrue([r for r in rows if r["Quantity"] == "7"])

    def test_39b3_plan_files_are_refused_for_a_draft(self):
        """Files for an unapproved plan would be something nobody signed off."""
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        plan_id = self.client.post(
            "/api/plans/build", json={"source": "manual"}
        ).json()["plan_id"]
        for path in ("files", "add.csv", "revise.csv"):
            with self.subTest(path=path):
                res = self.client.get(f"/api/plans/{plan_id}/{path}")
                self.assertEqual(res.status_code, 409)

    def test_39c_the_cover_file_is_only_available_after_approval(self):
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        plan_id = self.client.post(
            "/api/plans/build", json={"source": "manual"}
        ).json()["plan_id"]
        # A draft's files would be something that had not been authorised.
        res = self.client.get(f"/api/plans/{plan_id}/cover-revise.csv")
        self.assertEqual(res.status_code, 409)

    def test_39d_inventory_rows_carry_the_card_image(self):
        """
        The dashboard's hover preview needs cdn_image on every inventory row.

        It comes from the SortSwift export and is often the only picture of
        the actual card held. Absent from the query, the preview silently
        never appears -- there is nothing to error on.
        """
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        rows = self.client.get("/api/inventory").json()["items"]
        self.assertTrue(rows, "the batch tests should have left a card")
        self.assertIn("cdn_image", rows[0])
        # The batch fixture carries one, so this also proves it is populated
        # rather than merely present.
        self.assertEqual(rows[0]["cdn_image"], "https://cdn.example.com/deerling.jpg")

    # -- connecting the eBay account ---------------------------------------

    def signed_in_second_user(self):
        """A session for the non-admin account approved back in test 04."""
        other = TestClient(app)
        with mock.patch(
            "app.main.verify_google_id_token",
            return_value=google_claims(
                "google-sub-second", "second@example.com", "Second User"
            ),
        ):
            other.post("/api/auth/google", json={"id_token": "stub"})
        return other

    def test_40_status_reports_configuration_without_leaking_tokens(self):
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        res = self.client.get("/api/ebay/status")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["available"])
        self.assertTrue(body["configured"])
        self.assertEqual(body["environment"], "sandbox")
        # No token material may appear in a response the dashboard renders.
        serialised = json.dumps(body)
        self.assertNotIn("refresh_token", serialised)
        self.assertNotIn(os.environ["EBAY_CLIENT_SECRET"], serialised)

    def test_41_connecting_is_admin_only(self):
        # The inventory is shared and there is one eBay store behind it, so
        # the connection is infrastructure rather than a user preference.
        second = self.signed_in_second_user()
        self.assertEqual(second.post("/api/ebay/connect").status_code, 403)
        self.assertEqual(second.post("/api/ebay/disconnect").status_code, 403)
        # But a non-admin may still see whether eBay is connected.
        self.assertEqual(second.get("/api/ebay/status").status_code, 200)

    def test_42_connect_returns_a_consent_url_with_a_signed_state(self):
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        res = self.client.post("/api/ebay/connect")
        self.assertEqual(res.status_code, 200)
        url = res.json()["authorization_url"]
        self.assertIn("auth.sandbox.ebay.com", url)
        self.assertIn("client_id=test-ebay-client-id", url)

        state = url.split("state=")[1].split("&")[0]
        claims = main.decode_jwt_token(state)
        self.assertEqual(claims["purpose"], "ebay_oauth")
        self.assertIsNotNone(claims.get("user_id"))

    def test_43_the_callback_refuses_a_missing_or_forged_state(self):
        """
        Without this check anyone able to reach the callback could deliver an
        authorization code of their choosing and connect *their* eBay account
        to this deployment.
        """
        anonymous = TestClient(app)
        for label, params in (
            ("no state", {"code": "abc"}),
            ("garbage state", {"code": "abc", "state": "a.b.c"}),
            (
                "wrong purpose",
                {
                    "code": "abc",
                    "state": main.create_jwt_token({"purpose": "session"}),
                },
            ),
        ):
            with self.subTest(case=label):
                res = anonymous.get("/api/ebay/callback", params=params)
                self.assertEqual(res.status_code, 400)
                self.assertIn("no longer valid", res.text)

    def test_44_a_declined_consent_escapes_ebays_error_text(self):
        """
        error_description arrives in a query string, so it is controlled by
        anyone who can get a person to click a link. It is rendered into HTML.
        """
        res = TestClient(app).get(
            "/api/ebay/callback",
            params={
                "error": "access_denied",
                "error_description": "<script>alert(1)</script>",
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("<script>alert(1)</script>", res.text)
        self.assertIn("&lt;script&gt;", res.text)

    def test_45_a_valid_callback_stores_the_refresh_token(self):
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        url = self.client.post("/api/ebay/connect").json()["authorization_url"]
        state = url.split("state=")[1].split("&")[0]

        client = main.get_ebay_client()
        original_opener = client.oauth._opener

        def fake_opener(method, target, headers, body, timeout):
            from ebay_client.transport import Response

            return Response(
                200,
                {"Content-Type": "application/json"},
                json.dumps(
                    {
                        "access_token": "at",
                        "expires_in": 7200,
                        "refresh_token": "rt-from-consent",
                        "refresh_token_expires_in": 47304000,
                    }
                ).encode(),
            )

        client.oauth._opener = fake_opener
        try:
            res = TestClient(app).get(
                "/api/ebay/callback", params={"code": "the-code", "state": state}
            )
            self.assertEqual(res.status_code, 200, res.text)
            self.assertIn("connected", res.text)

            stored = user_db.get_ebay_token()
            self.assertEqual(stored["refresh_token"], "rt-from-consent")
            # The connection records who authorised it: approval to write to a
            # live storefront should not be anonymous.
            meta = user_db.get_ebay_connection_meta()
            self.assertEqual(meta["username"], "Admin User")

            self.assertTrue(self.client.get("/api/ebay/status").json()["connected"])
        finally:
            client.oauth._opener = original_opener

    def test_45b_the_api_sync_reconciles_the_real_xml_report(self):
        """
        The whole path, on the report shape eBay actually sends.

        This is the test that was missing. The report is XML, not CSV, and
        reading it as CSV does not fail: every line becomes a row, no row
        carries a SKU column, synced_count stays 0, and the delisting sweep
        then reads that as "the store ended every listing" and zeroes the
        mirror. It did exactly that to a live store.
        """
        from ebay_client.feed import (
            parse_active_inventory_report,
            records_to_csv,
        )

        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")

        # A card to reconcile against, already linked to a live listing.
        db.insert_manifest("ID4001", "Ledyba", "Chilling Reign", "NM", "Normal")
        db.upsert_variation("ID4001", "227511361186", 1, custom_label="ID4001-Bin_A12")

        report_xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<ActiveInventoryReport xmlns="urn:ebay:apis:eBLBaseComponents">'
            b"<Ack>Success</Ack>"
            b"<SKUDetails><ItemID>227511361186</ItemID>"
            b"<SKU>ID4001-Bin_A12</SKU><Price>3.75</Price>"
            b"<Quantity>5</Quantity><SiteID>US</SiteID></SKUDetails>"
            b"</ActiveInventoryReport>"
        )
        # Converted by the real adapter, so the test exercises the conversion
        # rather than a hand-written CSV that might not match it.
        csv_text = records_to_csv(parse_active_inventory_report(report_xml))

        user_db.save_ebay_token({"refresh_token": "rt"}, connected_by=1)
        try:
            with mock.patch.object(
                main,
                "download_active_inventory_report",
                return_value={
                    "task_id": "t-1",
                    "status": "COMPLETED",
                    "content": report_xml,
                    "was_xml": True,
                    "csv_text": csv_text,
                },
            ):
                res = self.client.post("/api/ebay/sync")

            self.assertEqual(res.status_code, 200, res.text)
            body = res.json()
            self.assertEqual(body["synced_count"], 1)
            self.assertFalse(body["delisting_skipped"])
            self.assertTrue(body["report"]["was_xml"])

            variation = db.get_variation("ID4001")
            self.assertEqual(variation["last_known_qty"], 5)
            self.assertAlmostEqual(variation["last_known_price"], 3.75)
        finally:
            user_db.save_ebay_token(None)
            db.delete_manifest("ID4001")

    def test_45c_a_report_that_matches_nothing_never_zeroes_the_mirror(self):
        """
        The guard, exercised through the endpoint rather than the engine.

        A report parsed into rows that match no card must not be read as an
        emptied store. Without this the remedy for an absent card and the
        symptom of an unreadable report are the same action, and one of them
        delists everything.
        """
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        db.insert_manifest("ID4002", "Heracross", "Chilling Reign", "NM", "Normal")
        db.upsert_variation("ID4002", "227511361186", 3, custom_label="ID4002-Bin_B01")

        user_db.save_ebay_token({"refresh_token": "rt"}, connected_by=1)
        try:
            with mock.patch.object(
                main,
                "download_active_inventory_report",
                return_value={
                    "task_id": "t-2",
                    "status": "COMPLETED",
                    "content": b"<?xml version='1.0'?><x/>",
                    "was_xml": True,
                    # Columns nothing recognises, which is what an
                    # unrecognised report degrades to.
                    "csv_text": "listing_ref,stock_code,units\n1,ID4002,3\n",
                },
            ):
                body = self.client.post("/api/ebay/sync").json()

            self.assertEqual(body["synced_count"], 0)
            self.assertTrue(body["delisting_skipped"])
            self.assertEqual(body["delisted_count"], 0)
            # Untouched, which is the entire point.
            self.assertEqual(db.get_variation("ID4002")["last_known_qty"], 3)
        finally:
            user_db.save_ebay_token(None)
            db.delete_manifest("ID4002")

    def test_46_disconnecting_forgets_the_token(self):
        self.sign_in("google-sub-admin", "admin@example.com", "Admin User")
        res = self.client.post("/api/ebay/disconnect")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["connected"])
        self.assertIsNone(user_db.get_ebay_token())
        self.assertFalse(self.client.get("/api/ebay/status").json()["connected"])

    def test_47_the_token_survives_an_inventory_restore(self):
        """
        The refresh token lives in the users database, not the inventory one.

        The inventory database is what the admin Database panel restores from a
        snapshot, and a restore must not silently cost the eBay connection --
        the refresh token is the only credential here that cannot be recreated
        without an interactive re-consent.
        """
        user_db.save_ebay_token({"refresh_token": "survivor"}, connected_by=1)
        try:
            with db.get_connection() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertNotIn("ebay_connection", tables)
            self.assertEqual(user_db.get_ebay_token()["refresh_token"], "survivor")
        finally:
            user_db.save_ebay_token(None)


if __name__ == "__main__":
    unittest.main()
