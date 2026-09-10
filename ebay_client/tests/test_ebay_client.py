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
