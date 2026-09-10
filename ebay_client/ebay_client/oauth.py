"""
eBay OAuth: the user-consent flow, and the application-only flow.

eBay has two token types and they are not interchangeable.

* A **user token** acts as the seller. Everything that touches listings or
  orders needs one. It is obtained once through a consent screen and then kept
  alive by a refresh token that lasts about eighteen months, after which the
  seller must consent again.
* An **application token** (client credentials) acts as the developer account.
  Notification subscriptions and the public keys used to verify callbacks use
  this one. There is no refresh token; you simply ask for another.

The refresh token is the only long-lived secret here and it must be persisted,
because losing it means an interactive re-consent -- which on a headless NAS
deployment means noticing, opening the dashboard and clicking through Google
and then eBay. Persistence is delegated to a :class:`TokenStore` so this
library stays free of any opinion about SQLite.
"""

import base64
import json
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from .config import APP_SCOPES, EbayConfig
from .errors import AuthError, TransportError
from .transport import USER_AGENT, Response, _urlopen_adapter

# Refresh this many seconds before the access token actually expires. A token
# that is valid when the request is built can still be expired by the time it
# arrives, and a push that dies half way through a variation group is far more
# annoying than a slightly early refresh.
EXPIRY_SKEW_SECONDS = 300


class TokenStore:
    """
    Somewhere durable to keep the refresh token.

    Two methods, deliberately. The library never inspects the storage or
    assumes a schema, so the web app can keep tokens in the users database
    beside the account they belong to.
    """

    def load(self) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def save(self, token: Dict[str, Any]) -> None:
        raise NotImplementedError

    def clear(self) -> None:
        self.save({})


class InMemoryTokenStore(TokenStore):
    """For tests, and for a CLI run that does not need to outlive itself."""

    def __init__(self, token: Optional[Dict[str, Any]] = None):
        self._token = dict(token or {})

    def load(self) -> Optional[Dict[str, Any]]:
        return dict(self._token) if self._token else None

    def save(self, token: Dict[str, Any]) -> None:
        self._token = dict(token or {})


class TokenProvider:
    """
    A callable that yields a currently-valid access token.

    Handed to :class:`~ebay_client.transport.Transport`, which calls it once
    per attempt. It also carries ``force_refresh`` so the transport can react
    to a 401 on a token it believed was good -- see the 401 handling there.
    """

    def __init__(self, refresh: Callable[[bool], str]):
        self._refresh = refresh

    def __call__(self) -> str:
        return self._refresh(False)

    def force_refresh(self) -> str:
        return self._refresh(True)


class OAuthClient:
    """
    Obtains and renews tokens for one eBay environment.

    Thread safety: the access token is cached in memory and a refresh may be
    triggered from two threads at once. That is harmless -- both refreshes
    succeed and the later one wins -- so this is deliberately not locked. What
    must not happen is losing the *refresh* token, and that is only ever
    written after a successful exchange.
    """

    def __init__(
        self,
        config: EbayConfig,
        store: Optional[TokenStore] = None,
        opener: Callable[..., Response] = _urlopen_adapter,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.store = store or InMemoryTokenStore()
        self._opener = opener
        self._clock = clock
        self._user_access: Optional[Dict[str, Any]] = None
        self._app_access: Optional[Dict[str, Any]] = None

    # -- consent --------------------------------------------------------

    def authorization_url(self, state: str, scopes: Optional[List[str]] = None) -> str:
        """
        The URL to send the seller to in order to grant access.

        ``state`` is not decoration: it is what ties the callback back to the
        signed-in dashboard session, and the callback handler must reject a
        state it did not issue. Without that check anyone can deliver an
        authorization code of their choosing to the redirect endpoint.
        """
        query = urllib.parse.urlencode(
            {
                "client_id": self.config.client_id,
                "response_type": "code",
                "redirect_uri": self.config.redirect_uri,
                "scope": " ".join(scopes or self.config.scopes),
                "state": state,
                # Forces the consent screen even if the seller has already
                # approved, which is how a re-consent after an expired refresh
                # token is made to work at all.
                "prompt": "login",
            }
        )
        return f"{self.config.authorize_url}?{query}"

    def exchange_code(self, code: str) -> Dict[str, Any]:
        """
        Turn a consent code into a token pair and persist the refresh token.

        The code eBay puts in the callback query string is URL-encoded and
        must be decoded before use; sending it as received fails with an
        opaque ``invalid_grant``.
        """
        payload = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": urllib.parse.unquote(code),
                "redirect_uri": self.config.redirect_uri,
            }
        )
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise AuthError("eBay returned no refresh token for the consent code")

        now = self._clock()
        record = {
            "refresh_token": refresh_token,
            "refresh_expires_at": now + float(
                payload.get("refresh_token_expires_in") or 0
            ),
            "scopes": list(self.config.scopes),
            "connected_at": now,
        }
        self.store.save(record)
        self._user_access = {
            "access_token": payload["access_token"],
            "expires_at": now + float(payload.get("expires_in") or 0),
        }
        return record

    def is_connected(self) -> bool:
        token = self.store.load() or {}
        return bool(token.get("refresh_token"))

    def refresh_expires_at(self) -> Optional[float]:
        token = self.store.load() or {}
        return token.get("refresh_expires_at")

    def disconnect(self) -> None:
        self.store.clear()
        self._user_access = None

    # -- tokens ---------------------------------------------------------

    def user_token_provider(self) -> TokenProvider:
        return TokenProvider(self.user_access_token)

    def app_token_provider(self) -> TokenProvider:
        return TokenProvider(self.app_access_token)

    def user_access_token(self, force: bool = False) -> str:
        """A valid seller access token, refreshing it when needed."""
        cached = self._user_access
        if not force and cached and self._still_valid(cached):
            return cached["access_token"]

        token = self.store.load() or {}
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise AuthError(
                "no eBay account is connected; the seller must grant access first"
            )

        expires_at = token.get("refresh_expires_at")
        if expires_at and expires_at <= self._clock():
            raise AuthError(
                "the eBay refresh token has expired; the seller must reconnect"
            )

        payload = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                # eBay requires the scope list on a refresh, and it must be a
                # subset of what was originally consented to. Replaying the
                # stored list rather than the current config avoids a silent
                # failure after someone edits DEFAULT_SCOPES.
                "scope": " ".join(token.get("scopes") or self.config.scopes),
            }
        )
        self._user_access = {
            "access_token": payload["access_token"],
            "expires_at": self._clock() + float(payload.get("expires_in") or 0),
        }
        return self._user_access["access_token"]

    def app_access_token(self, force: bool = False) -> str:
        """
        A valid application token.

        Cached in memory only. There is nothing to persist -- no refresh token
        exists for this grant -- and a process restart can simply ask again.
        """
        cached = self._app_access
        if not force and cached and self._still_valid(cached):
            return cached["access_token"]
        payload = self._token_request(
            {
                "grant_type": "client_credentials",
                "scope": " ".join(APP_SCOPES),
            }
        )
        self._app_access = {
            "access_token": payload["access_token"],
            "expires_at": self._clock() + float(payload.get("expires_in") or 0),
        }
        return self._app_access["access_token"]

    # -- internals ------------------------------------------------------

    def _still_valid(self, cached: Dict[str, Any]) -> bool:
        return cached.get("expires_at", 0) - EXPIRY_SKEW_SECONDS > self._clock()

    def _basic_auth(self) -> str:
        raw = f"{self.config.client_id}:{self.config.client_secret}".encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _token_request(self, form: Dict[str, str]) -> Dict[str, Any]:
        """
        Post to the token endpoint.

        Form-encoded with HTTP Basic credentials, which is why this does not
        go through Transport: that layer is JSON and bearer-token shaped, and
        bending it to cover the one endpoint that is neither would make it
        worse at its actual job.
        """
        body = urllib.parse.urlencode(form).encode("utf-8")
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Authorization": f"Basic {self._basic_auth()}",
        }
        response = self._opener(
            "POST", self.config.token_url, headers, body, 30.0
        )
        try:
            payload = response.json() or {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise TransportError("the eBay token endpoint returned non-JSON") from exc

        if response.status >= 400 or "access_token" not in payload:
            detail = payload.get("error_description") or payload.get("error") or ""
            raise AuthError(
                f"eBay token request failed ({response.status}) {detail}".strip()
            )
        return payload
