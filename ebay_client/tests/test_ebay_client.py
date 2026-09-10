"""
Tests for the eBay client library.

Nothing here touches the network. Every test injects an ``opener`` with the
same signature as the real one, which is the reason that seam exists: a test
that could reach eBay would either be slow and flaky or would spend the
application's daily call quota, and a push worker is exactly the kind of code
that must be exercised heavily.
"""

import base64
import json
import time
import unittest

from ebay_client import feed
from ebay_client import (
    ApiError,
    AuthError,
    ConfigError,
    EbayClient,
    EbayConfig,
    InMemoryTokenStore,
    OAuthClient,
    PublicKeyCache,
    RateLimited,
    SignatureError,
    Transport,
    challenge_response,
    parse_signature_header,
    payload_topic,
    verify_signature,
)
from ebay_client.transport import Response


def make_config(**overrides):
    kwargs = dict(
        client_id="cid",
        client_secret="secret",
        redirect_uri="Mark-RuName",
        environment="sandbox",
    )
    kwargs.update(overrides)
    return EbayConfig(**kwargs)


class RecordingOpener:
    """An opener that replays queued responses and records every call."""

    def __init__(self, *responses):
        self.queued = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        if not self.queued:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        nxt = self.queued.pop(0)
        return nxt() if callable(nxt) else nxt


def json_response(status, payload, headers=None):
    return Response(
        status,
        headers or {"Content-Type": "application/json"},
        json.dumps(payload).encode("utf-8"),
    )


# ---------------------------------------------------------------- config


class ConfigTests(unittest.TestCase):
    def test_sandbox_and_production_have_different_hosts(self):
        self.assertIn("sandbox", make_config(environment="sandbox").api_base)
        self.assertNotIn(
            "sandbox", make_config(environment="production").api_base
        )

    def test_unknown_environment_is_refused(self):
        with self.assertRaises(ConfigError):
            make_config(environment="staging")

    def test_missing_credentials_are_refused(self):
        with self.assertRaises(ConfigError):
            EbayConfig(client_id="", client_secret="s", redirect_uri="r")

    def test_from_env_names_every_missing_variable(self):
        with self.assertRaises(ConfigError) as caught:
            EbayConfig.from_env({})
        message = str(caught.exception)
        for name in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REDIRECT_URI"):
            self.assertIn(name, message)

    def test_from_env_reads_scopes_as_whitespace_separated(self):
        config = EbayConfig.from_env(
            {
                "EBAY_CLIENT_ID": "a",
                "EBAY_CLIENT_SECRET": "b",
                "EBAY_REDIRECT_URI": "c",
                "EBAY_SCOPES": "scope/one scope/two",
            }
        )
        self.assertEqual(config.scopes, ["scope/one", "scope/two"])

    def test_an_app_id_declaring_the_wrong_environment_is_reported(self):
        # eBay's own error for this is "client authentication failed", which
        # blames the secret and sends you hunting in the wrong place.
        config = make_config(
            client_id="MarkKlar-PokemonI-PRD-25fddd70c-24ee0a2c",
            environment="sandbox",
        )
        message = config.environment_mismatch()
        self.assertIsNotNone(message)
        self.assertIn("production", message)
        self.assertEqual(config.app_id_environment(), "production")

    def test_a_production_app_id_with_a_sandbox_cert_id_is_reported(self):
        """
        The real misconfiguration this check was written for.

        Both halves carry the marker, EBAY_ENVIRONMENT agreed with the App ID,
        and eBay still answered "client authentication failed" -- because the
        Cert ID came from the other keyset. Checking only the App ID missed it.
        """
        config = make_config(
            client_id="MarkKlar-PokemonI-PRD-25fddd70c-24ee0a2c",
            client_secret="SBX-5fea985bb357-9ca3-4765-aca9-53bc",
            environment="production",
        )
        message = config.environment_mismatch()
        self.assertIsNotNone(message)
        self.assertIn("same keyset", message)
        self.assertEqual(config.cert_id_environment(), "sandbox")
        # The message is rendered in the dashboard, so it must not carry the
        # secret it is complaining about.
        self.assertNotIn("5fea985bb357", message)

    def test_the_pair_mismatch_is_reported_before_the_environment_one(self):
        # Different fixes: fetch the other Cert ID, versus change
        # EBAY_ENVIRONMENT. The more specific diagnosis has to win.
        message = make_config(
            client_id="MarkKlar-PokemonI-PRD-abc",
            client_secret="SBX-def",
            environment="sandbox",
        ).environment_mismatch()
        self.assertIn("same keyset", message)

    def test_a_matching_pair_reports_no_mismatch(self):
        self.assertIsNone(
            make_config(
                client_id="MarkKlar-PokemonI-PRD-abc",
                client_secret="PRD-def",
                environment="production",
            ).environment_mismatch()
        )

    def test_a_cert_id_alone_disagreeing_with_the_environment_is_reported(self):
        message = make_config(
            client_id="opaque-app-id",
            client_secret="SBX-def",
            environment="production",
        ).environment_mismatch()
        self.assertIsNotNone(message)
        self.assertIn("Cert ID", message)

    def test_a_matching_app_id_reports_no_mismatch(self):
        self.assertIsNone(
            make_config(
                client_id="MarkKlar-PokemonI-SBX-abc-def", environment="sandbox"
            ).environment_mismatch()
        )

    def test_an_app_id_without_a_marker_is_not_second_guessed(self):
        # Not every keyset name carries the marker, and inventing a mismatch
        # would block a working configuration.
        config = make_config(client_id="some-other-shape")
        self.assertIsNone(config.app_id_environment())
        self.assertIsNone(config.environment_mismatch())

    def test_windows_line_endings_do_not_reach_the_credentials(self):
        """
        In the container these arrive through docker-compose's .env
        substitution, and compose does not strip a trailing CR -- so a .env
        saved on Windows produces a secret ending in "\\r", and eBay answers
        "client authentication failed" naming nothing useful.
        """
        config = EbayConfig.from_env(
            {
                "EBAY_CLIENT_ID": "cid\r",
                "EBAY_CLIENT_SECRET": '  "secret"\r\n',
                "EBAY_REDIRECT_URI": "Mark-RuName\r",
                "EBAY_ENVIRONMENT": "production\r",
            }
        )
        self.assertEqual(config.client_id, "cid")
        self.assertEqual(config.client_secret, "secret")
        self.assertEqual(config.redirect_uri, "Mark-RuName")
        self.assertEqual(config.environment, "production")

    def test_is_configured_does_not_raise_on_a_bare_environment(self):
        self.assertFalse(EbayConfig.is_configured({}))
        self.assertTrue(
            EbayConfig.is_configured(
                {
                    "EBAY_CLIENT_ID": "a",
                    "EBAY_CLIENT_SECRET": "b",
                    "EBAY_REDIRECT_URI": "c",
                }
            )
        )


# ------------------------------------------------------------- transport


class TransportTests(unittest.TestCase):
    def transport(self, *responses, **kwargs):
        opener = RecordingOpener(*responses)
        kwargs.setdefault("token_provider", lambda: "tok")
        kwargs.setdefault("sleep", lambda _s: None)
        kwargs.setdefault("spacing", 0)
        return Transport("https://api.example", opener=opener, **kwargs), opener

    def test_get_returns_decoded_body_and_sends_bearer_token(self):
        transport, opener = self.transport(json_response(200, {"ok": True}))
        self.assertEqual(transport.get("/thing"), {"ok": True})
        self.assertEqual(opener.calls[0]["headers"]["Authorization"], "Bearer tok")

    def test_marketplace_and_language_headers_are_sent(self):
        transport, opener = self.transport(
            json_response(200, {}), marketplace_id="EBAY_US"
        )
        transport.get("/thing")
        headers = opener.calls[0]["headers"]
        self.assertEqual(headers["X-EBAY-C-MARKETPLACE-ID"], "EBAY_US")
        self.assertEqual(headers["Content-Language"], "en-US")

    def test_empty_body_decodes_to_none(self):
        transport, _ = self.transport(Response(204, {}, b""))
        self.assertIsNone(transport.post("/thing", {"a": 1}))

    def test_filter_syntax_survives_query_encoding(self):
        # eBay's date filters contain brackets, colons and dots. Percent-
        # encoding them produces a filter eBay rejects, and its error does not
        # mention the filter.
        transport, opener = self.transport(json_response(200, {}))
        transport.get(
            "/order",
            params={"filter": "lastmodifieddate:[2026-01-01T00:00:00.000Z..]"},
        )
        url = opener.calls[0]["url"]
        self.assertIn("lastmodifieddate:[2026-01-01T00:00:00.000Z..]", url)

    def test_none_valued_params_are_dropped(self):
        transport, opener = self.transport(json_response(200, {}))
        transport.get("/order", params={"limit": 50, "offset": None})
        self.assertIn("limit=50", opener.calls[0]["url"])
        self.assertNotIn("offset", opener.calls[0]["url"])

    def test_transient_failure_is_retried_then_succeeds(self):
        transport, opener = self.transport(
            json_response(503, {}), json_response(200, {"ok": 1})
        )
        self.assertEqual(transport.get("/thing"), {"ok": 1})
        self.assertEqual(len(opener.calls), 2)

    def test_retry_after_is_honoured(self):
        slept = []
        transport, _ = self.transport(
            json_response(429, {}, {"Retry-After": "7"}),
            json_response(200, {"ok": 1}),
            sleep=slept.append,
        )
        transport.get("/thing")
        self.assertIn(7.0, slept)

    def test_persistent_rate_limit_raises_rate_limited(self):
        transport, _ = self.transport(*[json_response(429, {})] * 4)
        with self.assertRaises(RateLimited):
            transport.get("/thing")

    def test_api_error_preserves_ebay_error_ids(self):
        transport, _ = self.transport(
            json_response(
                400,
                {
                    "errors": [
                        {"errorId": 21916664, "longMessage": "variation trouble"}
                    ]
                },
            )
        )
        with self.assertRaises(ApiError) as caught:
            transport.post("/offer", {})
        self.assertEqual(caught.exception.error_ids, [21916664])
        self.assertIn("variation trouble", str(caught.exception))

    def test_a_401_triggers_exactly_one_forced_refresh(self):
        refreshes = []

        class Provider:
            def __call__(self):
                return "stale" if not refreshes else "fresh"

            def force_refresh(self):
                refreshes.append(1)
                return "fresh"

        transport, opener = self.transport(
            json_response(401, {}),
            json_response(200, {"ok": 1}),
            token_provider=Provider(),
        )
        self.assertEqual(transport.get("/thing"), {"ok": 1})
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(opener.calls[1]["headers"]["Authorization"], "Bearer fresh")

    def test_a_second_401_is_an_auth_error_rather_than_a_loop(self):
        class Provider:
            def __call__(self):
                return "stale"

            def force_refresh(self):
                return "stale"

        transport, opener = self.transport(
            json_response(401, {}),
            json_response(401, {}),
            token_provider=Provider(),
        )
        with self.assertRaises(AuthError):
            transport.get("/thing")
        self.assertEqual(len(opener.calls), 2)

    def test_authenticated_request_without_a_provider_is_refused(self):
        transport = Transport("https://api.example", opener=RecordingOpener())
        with self.assertRaises(AuthError):
            transport.get("/thing")


# ----------------------------------------------------------------- oauth


class OAuthTests(unittest.TestCase):
    def test_authorization_url_carries_state_and_scopes(self):
        client = OAuthClient(make_config(scopes=["a/scope"]))
        url = client.authorization_url("state-123")
        self.assertIn("auth.sandbox.ebay.com", url)
        self.assertIn("state=state-123", url)
        self.assertIn("a%2Fscope", url)
        self.assertIn("client_id=cid", url)

    def test_exchange_code_persists_the_refresh_token(self):
        store = InMemoryTokenStore()
        opener = RecordingOpener(
            json_response(
                200,
                {
                    "access_token": "at",
                    "expires_in": 7200,
                    "refresh_token": "rt",
                    "refresh_token_expires_in": 47304000,
                },
            )
        )
        client = OAuthClient(
            make_config(), store=store, opener=opener, clock=lambda: 1000.0
        )
        client.exchange_code("a%2Fcode")

        self.assertEqual(store.load()["refresh_token"], "rt")
        self.assertTrue(client.is_connected())
        # The code arrives URL-encoded in the callback and must be decoded, or
        # eBay answers with an opaque invalid_grant.
        self.assertIn("code=a%2Fcode", opener.calls[0]["body"].decode())
        # Basic auth, not a bearer token, on the token endpoint.
        self.assertTrue(
            opener.calls[0]["headers"]["Authorization"].startswith("Basic ")
        )

    def test_exchange_without_a_refresh_token_is_an_auth_error(self):
        opener = RecordingOpener(
            json_response(200, {"access_token": "at", "expires_in": 7200})
        )
        client = OAuthClient(make_config(), opener=opener)
        with self.assertRaises(AuthError):
            client.exchange_code("code")

    def test_access_token_is_cached_then_refreshed_near_expiry(self):
        now = [1000.0]
        opener = RecordingOpener(
            json_response(200, {"access_token": "first", "expires_in": 7200}),
            json_response(200, {"access_token": "second", "expires_in": 7200}),
        )
        client = OAuthClient(
            make_config(),
            store=InMemoryTokenStore(
                {"refresh_token": "rt", "scopes": ["a/scope"]}
            ),
            opener=opener,
            clock=lambda: now[0],
        )
        self.assertEqual(client.user_access_token(), "first")
        self.assertEqual(client.user_access_token(), "first")
        self.assertEqual(len(opener.calls), 1)

        # Inside the skew window the cached token is treated as spent.
        now[0] += 7200 - 60
        self.assertEqual(client.user_access_token(), "second")
        # The consented scope list is replayed, not the current config's.
        self.assertIn("a%2Fscope", opener.calls[1]["body"].decode())

    def test_no_connected_account_is_an_auth_error(self):
        client = OAuthClient(make_config(), opener=RecordingOpener())
        with self.assertRaises(AuthError):
            client.user_access_token()

    def test_an_expired_refresh_token_asks_for_reconsent(self):
        client = OAuthClient(
            make_config(),
            store=InMemoryTokenStore(
                {"refresh_token": "rt", "refresh_expires_at": 500.0}
            ),
            opener=RecordingOpener(),
            clock=lambda: 1000.0,
        )
        with self.assertRaises(AuthError) as caught:
            client.user_access_token()
        self.assertIn("reconnect", str(caught.exception))

    def test_application_token_uses_client_credentials(self):
        opener = RecordingOpener(
            json_response(200, {"access_token": "app", "expires_in": 7200})
        )
        client = OAuthClient(make_config(), opener=opener)
        self.assertEqual(client.app_access_token(), "app")
        self.assertIn("grant_type=client_credentials", opener.calls[0]["body"].decode())

    def test_a_rejected_token_request_surfaces_ebays_description(self):
        opener = RecordingOpener(
            json_response(
                400, {"error": "invalid_grant", "error_description": "token revoked"}
            )
        )
        client = OAuthClient(
            make_config(),
            store=InMemoryTokenStore({"refresh_token": "rt"}),
            opener=opener,
        )
        with self.assertRaises(AuthError) as caught:
            client.user_access_token()
        self.assertIn("token revoked", str(caught.exception))

    def test_disconnect_forgets_the_refresh_token(self):
        store = InMemoryTokenStore({"refresh_token": "rt"})
        client = OAuthClient(make_config(), store=store)
        client.disconnect()
        self.assertFalse(client.is_connected())


# --------------------------------------------------------- notifications


class ChallengeTests(unittest.TestCase):
    def test_hash_order_is_code_token_then_url(self):
        import hashlib

        expected = hashlib.sha256(b"CODEtokenhttps://host/hook").hexdigest()
        self.assertEqual(
            challenge_response("CODE", "token", "https://host/hook"), expected
        )

    def test_a_different_endpoint_url_changes_the_response(self):
        # The commonest cause of failed endpoint validation: the URL eBay
        # called is not the URL the app sees behind a reverse proxy.
        self.assertNotEqual(
            challenge_response("C", "t", "https://host/hook"),
            challenge_response("C", "t", "https://host/hook/"),
        )


class SignatureHeaderTests(unittest.TestCase):
    def pack(self, payload):
        return base64.b64encode(json.dumps(payload).encode()).decode()

    def test_header_decodes_to_key_id_and_signature(self):
        header = self.pack(
            {"alg": "ECDSA", "kid": "key-1", "signature": "sig", "digest": "SHA1"}
        )
        parsed = parse_signature_header(header)
        self.assertEqual(parsed["kid"], "key-1")

    def test_an_absent_header_is_refused(self):
        with self.assertRaises(SignatureError):
            parse_signature_header("")

    def test_garbage_is_refused(self):
        with self.assertRaises(SignatureError):
            parse_signature_header("not base64 json at all !!")

    def test_a_header_without_a_key_id_is_refused(self):
        with self.assertRaises(SignatureError):
            parse_signature_header(self.pack({"signature": "sig"}))


class PublicKeyCacheTests(unittest.TestCase):
    def test_a_burst_of_notifications_costs_one_fetch(self):
        fetches = []

        def fetch(key_id):
            fetches.append(key_id)
            return "KEY"

        cache = PublicKeyCache(fetch, ttl=100, clock=lambda: 0.0)
        for _ in range(10):
            self.assertEqual(cache.get("k"), "KEY")
        self.assertEqual(len(fetches), 1)

    def test_the_key_is_refetched_after_the_ttl(self):
        now = [0.0]
        fetches = []

        def fetch(key_id):
            fetches.append(key_id)
            return "KEY"

        cache = PublicKeyCache(fetch, ttl=100, clock=lambda: now[0])
        cache.get("k")
        now[0] = 101.0
        cache.get("k")
        self.assertEqual(len(fetches), 2)


class VerifySignatureTests(unittest.TestCase):
    """
    Round-trip against a real EC key.

    Signing locally with the same algorithm eBay uses is the only way to test
    this without a live notification, and it catches the mistakes that matter:
    a wrong curve, a wrong digest, or verifying a re-serialised body.
    """

    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        cls.hashes = hashes
        cls.ec = ec
        cls.private_key = ec.generate_private_key(ec.SECP256R1())
        cls.pem = (
            cls.private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )

    def sign(self, body: bytes) -> str:
        signature = self.private_key.sign(
            body, self.ec.ECDSA(self.hashes.SHA1())
        )
        return base64.b64encode(
            json.dumps(
                {
                    "alg": "ECDSA",
                    "kid": "key-1",
                    "signature": base64.b64encode(signature).decode(),
                    "digest": "SHA1",
                }
            ).encode()
        ).decode()

    def cache(self, key=None):
        return PublicKeyCache(lambda _kid: key if key is not None else self.pem)

    def test_a_genuine_notification_verifies(self):
        body = b'{"metadata":{"topic":"MARKETPLACE_ACCOUNT_DELETION"}}'
        verify_signature(body, self.sign(body), self.cache())

    def test_a_tampered_body_is_rejected(self):
        body = b'{"quantity":1}'
        header = self.sign(body)
        with self.assertRaises(SignatureError):
            verify_signature(b'{"quantity":9999}', header, self.cache())

    def test_reserialising_the_body_breaks_verification(self):
        # Documents why the endpoint must verify the raw request bytes: even a
        # semantically identical re-encoding changes the signed material.
        # eBay sends compact JSON; json.dumps re-inserts spaces after the
        # separators, which is enough to change the signed bytes.
        body = b'{"a":1,"b":2}'
        header = self.sign(body)
        reserialised = json.dumps(json.loads(body)).encode()
        self.assertNotEqual(body, reserialised)
        with self.assertRaises(SignatureError):
            verify_signature(reserialised, header, self.cache())

    def test_a_key_without_pem_armour_is_accepted(self):
        bare = "".join(
            line
            for line in self.pem.splitlines()
            if not line.startswith("-----")
        )
        body = b'{"ok":true}'
        verify_signature(body, self.sign(body), self.cache(bare))

    def test_an_empty_key_is_refused(self):
        body = b'{"ok":true}'
        with self.assertRaises(SignatureError):
            verify_signature(body, self.sign(body), self.cache(""))


class FeedReportTests(unittest.TestCase):
    """
    The Feed API's asynchronous report flow.

    Used instead of the Inventory API because the Inventory API can only see
    listings created through it -- a store built with File Exchange has no
    offers at all -- and instead of GetSellerList because that needs a
    120-day start-time window and returns XML.
    """

    def transport(self, *responses):
        opener = RecordingOpener(*responses)
        return (
            Transport(
                "https://api.example",
                token_provider=lambda: "tok",
                opener=opener,
                sleep=lambda _s: None,
                spacing=0,
            ),
            opener,
        )

    def test_the_task_id_is_read_from_the_location_header(self):
        # It is not in the body. Reading the body -- the obvious guess --
        # yields None and fails confusingly two calls later.
        transport, _ = self.transport(
            Response(
                201,
                {"Location": "/sell/feed/v1/inventory_task/task-42"},
                b"",
            )
        )
        self.assertEqual(feed.create_inventory_task(transport), "task-42")

    def test_a_task_id_in_the_body_is_accepted_as_a_fallback(self):
        transport, _ = self.transport(json_response(201, {"taskId": "task-7"}))
        self.assertEqual(feed.create_inventory_task(transport), "task-7")

    def test_no_task_id_anywhere_is_an_error(self):
        transport, _ = self.transport(Response(201, {}, b""))
        with self.assertRaises(feed.FeedError):
            feed.create_inventory_task(transport)

    def test_the_requested_feed_type_is_the_active_inventory_report(self):
        transport, opener = self.transport(
            Response(201, {"Location": "/x/task-1"}, b"")
        )
        feed.create_inventory_task(transport)
        body = json.loads(opener.calls[0]["body"].decode())
        self.assertEqual(body["feedType"], "LMS_ACTIVE_INVENTORY_REPORT")

    def test_polling_continues_until_the_task_completes(self):
        transport, _ = self.transport(
            json_response(200, {"status": "IN_PROGRESS"}),
            json_response(200, {"status": "IN_PROGRESS"}),
            json_response(200, {"status": "COMPLETED"}),
        )
        seen = []
        task = feed.wait_for_task(
            transport,
            "task-1",
            poll_interval=0,
            sleep=lambda _s: None,
            on_status=seen.append,
        )
        self.assertEqual(task["status"], "COMPLETED")
        self.assertEqual(seen, ["IN_PROGRESS", "COMPLETED"])

    def test_a_partial_report_is_still_usable(self):
        # COMPLETED_WITH_ERROR produces a file. Refusing it would trade a
        # partial sync for no sync, which is the worse outcome.
        transport, _ = self.transport(
            json_response(200, {"status": "COMPLETED_WITH_ERROR"})
        )
        task = feed.wait_for_task(transport, "t", sleep=lambda _s: None)
        self.assertEqual(task["status"], "COMPLETED_WITH_ERROR")

    def test_a_failed_task_raises(self):
        transport, _ = self.transport(json_response(200, {"status": "FAILED"}))
        with self.assertRaises(feed.FeedError):
            feed.wait_for_task(transport, "t", sleep=lambda _s: None)

    def test_polling_gives_up_rather_than_looping_forever(self):
        now = [0.0]
        transport, _ = self.transport(*[json_response(200, {"status": "IN_PROGRESS"})] * 3)
        with self.assertRaises(feed.FeedError) as caught:
            feed.wait_for_task(
                transport,
                "t",
                poll_interval=1,
                timeout=2,
                sleep=lambda s: now.__setitem__(0, now[0] + s),
                clock=lambda: now[0],
            )
        self.assertIn("try again", str(caught.exception))

    def test_a_gzipped_report_is_decompressed(self):
        import gzip

        payload = b"ItemID,SKU,Quantity,Price\n1234,ID1001,3,2.50\n"
        transport, _ = self.transport(
            Response(200, {}, gzip.compress(payload))
        )
        self.assertEqual(feed.get_result_file(transport, "t"), payload)

    def test_a_zipped_report_is_decompressed(self):
        import io as _io
        import zipfile as _zip

        payload = b"ItemID,SKU\n1,ID1001\n"
        buffer = _io.BytesIO()
        with _zip.ZipFile(buffer, "w") as archive:
            archive.writestr("report.csv", payload)
        self.assertEqual(feed.decompress(buffer.getvalue()), payload)

    # The real report, in the shape that cost a live store mirror: an LMS feed
    # type returns eBay XML, not CSV, and reading it as CSV does not fail --
    # every line becomes a row, no row carries a SKU column, and the sync then
    # looks exactly like a store that has ended every listing.
    REAL_REPORT = b"""<?xml version="1.0" encoding="UTF-8"?>
<ActiveInventoryReport xmlns="urn:ebay:apis:eBLBaseComponents">
  <Timestamp>2026-09-09T19:00:00.000Z</Timestamp>
  <Ack>Success</Ack>
  <SKUDetails>
    <ItemID>227511361186</ItemID>
    <SKU>ID1050-Bin_A12</SKU>
    <Price>1.99</Price>
    <Quantity>1</Quantity>
    <SiteID>US</SiteID>
  </SKUDetails>
  <SKUDetails>
    <ItemID>227511361186</ItemID>
    <SKU>ID1048-Bin_A12</SKU>
    <Price>2.49</Price>
    <Quantity>2</Quantity>
    <SiteID>US</SiteID>
  </SKUDetails>
</ActiveInventoryReport>
"""

    # A multi-variation listing. Quantity and price are reported per
    # variation, nested inside the item's block, and the item level carries no
    # SKU at all -- a blank SKU there is how eBay identifies the parent.
    # Reading only the direct children yielded one blank-SKU row per listing,
    # which matched nothing.
    VARIATION_REPORT = b"""<?xml version="1.0" encoding="UTF-8"?>
<ActiveInventoryReport xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SKUDetails>
    <ItemID>227511361186</ItemID>
    <SKU></SKU>
    <Variations>
      <Variation>
        <SKU>ID1050-Bin_A12</SKU>
        <StartPrice>1.99</StartPrice>
        <Quantity>1</Quantity>
      </Variation>
      <Variation>
        <SKU>ID1048-Bin_A12</SKU>
        <StartPrice>2.49</StartPrice>
        <Quantity>2</Quantity>
      </Variation>
    </Variations>
  </SKUDetails>
  <SKUDetails>
    <ItemID>227599999999</ItemID>
    <SKU></SKU>
    <Price>5.00</Price>
    <Quantity>1</Quantity>
  </SKUDetails>
</ActiveInventoryReport>
"""

    def test_variations_are_flattened_one_record_per_variation(self):
        records = feed.parse_active_inventory_report(self.VARIATION_REPORT)
        # Two variations plus one single listing, not two listing-level rows.
        self.assertEqual(len(records), 3)
        variation_skus = [r["SKU"] for r in records if r["SKU"]]
        self.assertEqual(variation_skus, ["ID1050-Bin_A12", "ID1048-Bin_A12"])

    def test_a_variation_inherits_its_parents_item_id(self):
        records = feed.parse_active_inventory_report(self.VARIATION_REPORT)
        # The variation carries no ItemID of its own, and the mirror is keyed
        # on the listing, so it must come from the enclosing block.
        self.assertEqual(records[0]["ItemID"], "227511361186")
        self.assertEqual(records[1]["ItemID"], "227511361186")

    def test_start_price_is_accepted_where_price_is_absent(self):
        # The merchant-data schema uses both spellings in different places.
        records = feed.parse_active_inventory_report(self.VARIATION_REPORT)
        self.assertEqual(records[0]["Price"], "1.99")
        self.assertEqual(records[1]["Quantity"], "2")

    def test_a_single_listing_still_reads_from_the_item_level(self):
        records = feed.parse_active_inventory_report(self.VARIATION_REPORT)
        single = records[2]
        self.assertEqual(single["ItemID"], "227599999999")
        self.assertEqual(single["Price"], "5.00")
        # No SKU, so the reconciler will ignore it as unmanaged -- which is
        # correct for a listing this tool did not create.
        self.assertEqual(single["SKU"], "")

    def test_the_outline_names_elements_and_no_values(self):
        outline = feed.report_outline(self.VARIATION_REPORT)
        self.assertIn("ActiveInventoryReport/SKUDetails/Variations/Variation/SKU", outline)
        # Values must never appear: the outline exists to be pasted.
        joined = " ".join(outline)
        self.assertNotIn("ID1050", joined)
        self.assertNotIn("1.99", joined)

    def test_the_outline_of_unparseable_bytes_is_empty_rather_than_raising(self):
        self.assertEqual(feed.report_outline(b"not xml"), [])

    def test_the_report_is_recognised_as_xml(self):
        self.assertTrue(feed.looks_like_xml(self.REAL_REPORT))
        self.assertFalse(feed.looks_like_xml(b"ItemID,SKU\n1,ID1001\n"))

    def test_the_xml_report_yields_one_record_per_sku(self):
        records = feed.parse_active_inventory_report(self.REAL_REPORT)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["SKU"], "ID1050-Bin_A12")
        self.assertEqual(records[0]["ItemID"], "227511361186")
        self.assertEqual(records[1]["Quantity"], "2")

    def test_records_render_as_csv_the_existing_parser_accepts(self):
        csv_text = feed.records_to_csv(
            feed.parse_active_inventory_report(self.REAL_REPORT)
        )
        lines = csv_text.strip().splitlines()
        self.assertEqual(lines[0], "ItemID,SKU,Price,Quantity")
        self.assertEqual(lines[1], "227511361186,ID1050-Bin_A12,1.99,1")

    def test_a_namespace_change_does_not_silently_empty_the_report(self):
        # Matching the namespace exactly would turn an eBay revision into
        # another zero-row parse, which is the failure being fixed.
        renamespaced = self.REAL_REPORT.replace(
            b"urn:ebay:apis:eBLBaseComponents", b"urn:ebay:apis:v2"
        )
        self.assertEqual(len(feed.parse_active_inventory_report(renamespaced)), 2)

    def test_a_report_with_no_sku_entries_raises_rather_than_returning_empty(self):
        # An empty list is indistinguishable from "the store has nothing
        # listed", and acting on that is what zeroed the mirror.
        with self.assertRaises(feed.FeedError) as caught:
            feed.parse_active_inventory_report(
                b'<?xml version="1.0"?><SomethingElse><Ack>Success</Ack></SomethingElse>'
            )
        self.assertIn("SomethingElse", str(caught.exception))

    def test_an_ebay_failure_document_raises_with_ebays_message(self):
        payload = (
            b'<?xml version="1.0"?><ActiveInventoryReport>'
            b"<Ack>Failure</Ack><Errors><LongMessage>Not authorised"
            b"</LongMessage></Errors></ActiveInventoryReport>"
        )
        with self.assertRaises(feed.FeedError) as caught:
            feed.parse_active_inventory_report(payload)
        self.assertIn("Not authorised", str(caught.exception))

    def test_unparseable_bytes_raise(self):
        with self.assertRaises(feed.FeedError):
            feed.parse_active_inventory_report(b"<?xml version='1.0'?><broken")

    def test_an_empty_report_raises(self):
        with self.assertRaises(feed.FeedError):
            feed.parse_active_inventory_report(b"")

    def test_a_plain_report_passes_through(self):
        self.assertEqual(feed.decompress(b"ItemID,SKU\n"), b"ItemID,SKU\n")

    def test_an_empty_payload_is_empty_rather_than_an_error(self):
        self.assertEqual(feed.decompress(b""), b"")


class PayloadTopicTests(unittest.TestCase):
    def test_topic_is_read_from_metadata(self):
        self.assertEqual(
            payload_topic({"metadata": {"topic": "ITEM_SOLD"}}), "ITEM_SOLD"
        )

    def test_a_top_level_topic_is_also_accepted(self):
        self.assertEqual(payload_topic({"topic": "ITEM_SOLD"}), "ITEM_SOLD")

    def test_an_unrecognisable_payload_yields_none(self):
        self.assertIsNone(payload_topic(None))
        self.assertIsNone(payload_topic({"notification": {}}))


# ---------------------------------------------------------------- client


class ClientTests(unittest.TestCase):
    def test_public_key_lookup_uses_the_application_token(self):
        opener = RecordingOpener(
            json_response(200, {"access_token": "app", "expires_in": 7200}),
            json_response(200, {"key": "KEYDATA", "algorithm": "ECDSA"}),
        )
        client = EbayClient(make_config(), opener=opener)
        self.assertEqual(client.public_keys.get("k1"), "KEYDATA")
        self.assertIn("client_credentials", opener.calls[0]["body"].decode())
        self.assertIn("/commerce/notification/v1/public_key/k1", opener.calls[1]["url"])

    def test_status_reports_connection_without_leaking_tokens(self):
        client = EbayClient(
            make_config(),
            store=InMemoryTokenStore(
                {"refresh_token": "rt", "refresh_expires_at": 99.0}
            ),
        )
        status = client.status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["refresh_expires_at"], 99.0)
        self.assertNotIn("refresh_token", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
