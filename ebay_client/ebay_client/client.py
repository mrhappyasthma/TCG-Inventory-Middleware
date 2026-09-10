"""
The facade: one object that holds the configuration, the tokens and the
transports, so callers do not have to wire four collaborators together.

Two transports rather than one, because eBay's two token types are not
interchangeable. ``seller`` acts as the connected seller and is what listing
and order calls use; ``application`` acts as the developer account and is what
notification subscriptions and verification keys use. Handing a caller the
wrong one produces a 403 whose message does not mention tokens at all.

Resource wrappers (inventory items, offers, groups, orders) are deliberately
not here yet: they arrive with the step that first needs them, so that no
untested call surface accumulates ahead of a caller who could have proved it
works.
"""

import time
from typing import Any, Callable, Dict, Optional

from .config import EbayConfig
from .notifications import PublicKeyCache
from .oauth import OAuthClient, TokenStore
from .transport import Response, Transport, _urlopen_adapter


class EbayClient:
    def __init__(
        self,
        config: EbayConfig,
        store: Optional[TokenStore] = None,
        opener: Callable[..., Response] = _urlopen_adapter,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.oauth = OAuthClient(config, store=store, opener=opener, clock=clock)
        self.seller = Transport(
            config.api_base,
            token_provider=self.oauth.user_token_provider(),
            marketplace_id=config.marketplace_id,
            opener=opener,
        )
        self.application = Transport(
            config.api_base,
            token_provider=self.oauth.app_token_provider(),
            marketplace_id=config.marketplace_id,
            opener=opener,
        )
        self.public_keys = PublicKeyCache(self._fetch_public_key)

    # -- notifications --------------------------------------------------

    def _fetch_public_key(self, key_id: str) -> str:
        """
        Retrieve one notification verification key.

        Uses the application transport: this is not a seller-scoped call, and
        a deployment can still verify notifications when no seller has
        connected an account yet -- which is exactly the situation for the
        mandatory account-deletion endpoint.
        """
        payload = self.application.get(
            f"/commerce/notification/v1/public_key/{key_id}"
        ) or {}
        key = payload.get("key")
        if not key:
            raise KeyError(f"eBay returned no key for id {key_id!r}")
        return key

    # -- diagnostics ----------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """
        A summary safe to render in the dashboard.

        Deliberately carries no token material. The refresh expiry is included
        because an eBay refresh token dies after roughly eighteen months and
        the only cure is an interactive re-consent, so it is worth showing
        before it lapses rather than after.
        """
        return {
            "configured": True,
            "environment": self.config.environment,
            "marketplace_id": self.config.marketplace_id,
            "connected": self.oauth.is_connected(),
            "refresh_expires_at": self.oauth.refresh_expires_at(),
            # A mismatch can never authenticate, and eBay's own error for it
            # ("client authentication failed") points at the secret rather
            # than at the environment. Say so before a token is attempted.
            "misconfiguration": self.config.environment_mismatch(),
        }
