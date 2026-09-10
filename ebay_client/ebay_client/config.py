"""
Where the eBay endpoints are and which credentials to use.

Sandbox and production are entirely separate installations with separate
credentials, separate listings and separate order histories. Choosing between
them is one setting rather than a scatter of hostnames, because the failure
mode of getting it wrong is either "nothing works" (harmless) or "a test run
published to the real storefront" (not harmless).
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional

from .errors import ConfigError

PRODUCTION = "production"
SANDBOX = "sandbox"

_HOSTS = {
    PRODUCTION: {
        "api": "https://api.ebay.com",
        "auth": "https://auth.ebay.com/oauth2/authorize",
    },
    SANDBOX: {
        "api": "https://api.sandbox.ebay.com",
        "auth": "https://auth.sandbox.ebay.com/oauth2/authorize",
    },
}

# The scopes this application actually needs. Requesting more than is needed
# makes the consent screen scarier and widens the blast radius of a leaked
# token, so this list is the minimum for: reading and writing listings, reading
# orders, and reading the account's business policies.
DEFAULT_SCOPES = [
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
]

# Notification subscriptions and the account-deletion callback are application
# scoped rather than user scoped, so they use a client-credentials token.
APP_SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
]

DEFAULT_MARKETPLACE_ID = "EBAY_US"


@dataclass
class EbayConfig:
    client_id: str
    client_secret: str
    # eBay calls this the RuName, not a URL. It identifies the redirect
    # configured on the developer account; the actual redirect URL is set in
    # eBay's console and cannot be overridden per request.
    redirect_uri: str
    environment: str = PRODUCTION
    marketplace_id: str = DEFAULT_MARKETPLACE_ID
    scopes: List[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))
    # Echoed back to eBay when it validates the account-deletion endpoint.
    # eBay requires 32-80 characters of [A-Za-z0-9_-].
    verification_token: Optional[str] = None

    def __post_init__(self):
        if self.environment not in _HOSTS:
            raise ConfigError(
                f"environment must be one of {sorted(_HOSTS)}, "
                f"got {self.environment!r}"
            )
        if not self.client_id or not self.client_secret:
            raise ConfigError("client_id and client_secret are both required")

    @property
    def api_base(self) -> str:
        return _HOSTS[self.environment]["api"]

    @property
    def token_url(self) -> str:
        return f"{self.api_base}/identity/v1/oauth2/token"

    @property
    def authorize_url(self) -> str:
        return _HOSTS[self.environment]["auth"]

    @property
    def is_sandbox(self) -> bool:
        return self.environment == SANDBOX

    @classmethod
    def from_env(cls, env=None) -> "EbayConfig":
        """
        Build from environment variables, failing loudly on anything missing.

        Mirrors how GOOGLE_CLIENT_ID is handled: a half-configured integration
        that boots and then fails on first use is harder to diagnose than one
        that refuses to start.
        """
        env = os.environ if env is None else env
        missing = [
            name
            for name in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REDIRECT_URI")
            if not env.get(name)
        ]
        if missing:
            raise ConfigError(
                "eBay integration is not configured; missing "
                + ", ".join(missing)
            )
        raw_scopes = env.get("EBAY_SCOPES", "").strip()
        return cls(
            client_id=env["EBAY_CLIENT_ID"],
            client_secret=env["EBAY_CLIENT_SECRET"],
            redirect_uri=env["EBAY_REDIRECT_URI"],
            environment=env.get("EBAY_ENVIRONMENT", PRODUCTION).strip().lower(),
            marketplace_id=env.get("EBAY_MARKETPLACE_ID", DEFAULT_MARKETPLACE_ID),
            scopes=raw_scopes.split() if raw_scopes else list(DEFAULT_SCOPES),
            verification_token=env.get("EBAY_VERIFICATION_TOKEN") or None,
        )

    @classmethod
    def is_configured(cls, env=None) -> bool:
        """
        Whether an eBay integration could be built from this environment.

        The dashboard needs to hide or disable the eBay controls without
        raising, since the app must keep working as a CSV tool for anyone who
        has not connected an eBay account.
        """
        env = os.environ if env is None else env
        return all(
            env.get(name)
            for name in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REDIRECT_URI")
        )
