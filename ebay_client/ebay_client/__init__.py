"""
ebay-client: a standalone client for the eBay Sell APIs.

Kept as its own package, separate from both ``tcg_engine`` and the web app,
because it is the only component that talks to a remote system with
credentials, quotas, retries and signed callbacks. Holding that behind one
seam is what makes it testable without network access and replaceable when
eBay deprecates an API -- which this project has already lived through once,
since the File Exchange CSV flow it was originally built on is now a
deprecated path.

The boundary is a vocabulary boundary. This library speaks eBay's language:
SKUs, offers, inventory item groups, order line items. It knows nothing about
manifest IDs, pricing rules or SortSwift, and translation between the two
happens in the layer above so neither model leaks into the other.
"""

from .client import EbayClient
from .config import (
    APP_SCOPES,
    DEFAULT_MARKETPLACE_ID,
    DEFAULT_SCOPES,
    PRODUCTION,
    SANDBOX,
    EbayConfig,
)
from .errors import (
    ApiError,
    AuthError,
    ConfigError,
    EbayError,
    RateLimited,
    SignatureError,
    TransportError,
)
from .notifications import (
    PublicKeyCache,
    challenge_response,
    parse_signature_header,
    payload_topic,
    verify_signature,
)
from .oauth import InMemoryTokenStore, OAuthClient, TokenStore
from .transport import Transport

__all__ = [
    "EbayClient",
    "EbayConfig",
    "PRODUCTION",
    "SANDBOX",
    "DEFAULT_SCOPES",
    "APP_SCOPES",
    "DEFAULT_MARKETPLACE_ID",
    "OAuthClient",
    "TokenStore",
    "InMemoryTokenStore",
    "Transport",
    "PublicKeyCache",
    "verify_signature",
    "parse_signature_header",
    "challenge_response",
    "payload_topic",
    "EbayError",
    "ConfigError",
    "TransportError",
    "AuthError",
    "RateLimited",
    "ApiError",
    "SignatureError",
]

__version__ = "0.1.0"
