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
from .feed import (
    ACTIVE_INVENTORY_REPORT,
    FeedError,
    download_active_inventory_report,
)
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
from .inventory import (
    BULK_INVENTORY_ITEM_LIMIT,
    BULK_PRICE_QUANTITY_LIMIT,
    bulk_statuses,
    bulk_update_price_quantity,
    chunked,
    create_offer,
    create_or_replace_inventory_item,
    create_or_replace_inventory_item_group,
    describe_failure,
    failed_statuses,
    get_inventory_item,
    get_inventory_item_group,
    get_offers,
    price_quantity_request,
    publish_offer,
    publish_offer_by_inventory_item_group,
    update_offer,
    withdraw_offer,
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
    "ACTIVE_INVENTORY_REPORT",
    "FeedError",
    "download_active_inventory_report",
    "PRODUCTION",
    "SANDBOX",
    "DEFAULT_SCOPES",
    "APP_SCOPES",
    "DEFAULT_MARKETPLACE_ID",
    "OAuthClient",
    "TokenStore",
    "InMemoryTokenStore",
    "Transport",
    "BULK_INVENTORY_ITEM_LIMIT",
    "BULK_PRICE_QUANTITY_LIMIT",
    "chunked",
    "create_or_replace_inventory_item",
    "get_inventory_item",
    "create_offer",
    "update_offer",
    "get_offers",
    "publish_offer",
    "withdraw_offer",
    "create_or_replace_inventory_item_group",
    "get_inventory_item_group",
    "publish_offer_by_inventory_item_group",
    "bulk_update_price_quantity",
    "price_quantity_request",
    "bulk_statuses",
    "failed_statuses",
    "describe_failure",
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
