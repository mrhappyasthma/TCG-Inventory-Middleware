"""
Reading the seller's own account: business policies and inventory locations.

These exist because the Inventory API addresses both by **id**, while File
Exchange addressed policies by name. "Free Shipping Cards" is not something
the API understands, and the ids are not shown anywhere in Seller Hub -- the
only way to learn them is to ask. Making the dashboard ask, and letting the
seller pick from the answer, is the difference between configuration and a
scavenger hunt through developer tooling.

An inventory location is a harder requirement than it looks: eBay refuses to
publish an offer without one, and an account that has only ever listed through
File Exchange or the web flow may have none at all. So this module can create
one as well as read them. A warehouse location needs only a name and a postal
code and country -- no street address -- which matters for someone selling
from home.
"""

from typing import Any, Dict, List, Optional

ACCOUNT_BASE = "/sell/account/v1"
LOCATION_BASE = "/sell/inventory/v1/location"

# eBay's three policy kinds, each on its own resource. Named here so the
# caller loops rather than repeating the same call three times with a
# different string, which is how one of them ends up querying the wrong
# marketplace.
POLICY_RESOURCES = (
    ("fulfillment", "fulfillment_policy", "fulfillmentPolicies",
     "fulfillmentPolicyId"),
    ("payment", "payment_policy", "paymentPolicies", "paymentPolicyId"),
    ("return", "return_policy", "returnPolicies", "returnPolicyId"),
)

# A location key is permanent -- eBay does not allow it to be changed once
# set -- and is capped at 50 characters.
MAX_LOCATION_KEY_LENGTH = 50


def get_policies(
    transport, marketplace_id: str = "EBAY_US"
) -> Dict[str, List[Dict[str, str]]]:
    """
    Every business policy on the account, by kind.

    Returns ``{"fulfillment": [{"id": ..., "name": ...}], ...}`` -- just the
    id and the name, because that is all a chooser needs and the full payloads
    are large enough to be noise. ``marketplace_id`` is required by eBay on
    each of these calls; policies are per marketplace, and omitting it returns
    an error rather than everything.
    """
    result: Dict[str, List[Dict[str, str]]] = {}
    for kind, resource, container, id_field in POLICY_RESOURCES:
        payload = transport.get(
            f"{ACCOUNT_BASE}/{resource}", {"marketplace_id": marketplace_id}
        ) or {}
        result[kind] = [
            {
                "id": str(entry.get(id_field) or ""),
                "name": str(entry.get("name") or ""),
                "description": str(entry.get("description") or ""),
            }
            for entry in (payload.get(container) or [])
            if entry.get(id_field)
        ]
    return result


def get_inventory_locations(transport, limit: int = 100) -> List[Dict[str, Any]]:
    """
    The seller's inventory locations.

    An empty list is the common and confusing case: it does not mean the
    account is broken, it means no location has ever been created, which is
    normal for a seller who has only used File Exchange or eBay's own listing
    form. ``create_inventory_location`` is then the fix.
    """
    payload = transport.get(LOCATION_BASE, {"limit": limit}) or {}
    locations = []
    for entry in payload.get("locations") or []:
        address = ((entry.get("location") or {}).get("address") or {})
        locations.append({
            "key": str(entry.get("merchantLocationKey") or ""),
            "name": str(entry.get("name") or ""),
            "status": str(entry.get("merchantLocationStatus") or ""),
            "postal_code": str(address.get("postalCode") or ""),
            "country": str(address.get("country") or ""),
        })
    return locations


def create_inventory_location(
    transport,
    merchant_location_key: str,
    *,
    name: str,
    postal_code: str,
    country: str = "US",
) -> str:
    """
    Create a warehouse inventory location, returning its key.

    Only a name and a postal code and country are required: eBay does not
    need a street address for a warehouse location, which is the right shape
    for someone shipping from home and not publishing their address.

    ``locationTypes`` is stated explicitly even though eBay defaults to
    WAREHOUSE, because the default is a documented behaviour rather than an
    obvious one, and a STORE location carries different obligations.

    The key cannot be changed afterwards, so it is validated here rather than
    discovered to be too long by a 400.
    """
    key = str(merchant_location_key or "").strip()
    if not key:
        raise ValueError("a merchant location key is required")
    if len(key) > MAX_LOCATION_KEY_LENGTH:
        raise ValueError(
            f"location key {key!r} is {len(key)} characters; eBay allows "
            f"{MAX_LOCATION_KEY_LENGTH}, and it cannot be changed later"
        )
    if not str(postal_code or "").strip():
        raise ValueError("a postal code is required for a warehouse location")

    transport.post(f"{LOCATION_BASE}/{key}", {
        "name": name or key,
        "locationTypes": ["WAREHOUSE"],
        "merchantLocationStatus": "ENABLED",
        "location": {
            "address": {
                "postalCode": str(postal_code).strip(),
                "country": str(country or "US").strip().upper(),
            }
        },
    })
    return key


def suggest_policy_ids(
    policies: Dict[str, List[Dict[str, str]]],
    *,
    shipping_name: str = "",
    return_name: str = "",
    payment_name: str = "",
) -> Dict[str, Optional[str]]:
    """
    Match the policy *names* already configured for the CSV path to their ids.

    The names are what the seller has been uploading in File Exchange files
    for months, so they are the best available hint at which policy they mean
    -- and matching them turns the setup step from "choose three ids" into
    "confirm these three". Matching is exact after trimming and case folding:
    a near-miss guess here would list against the wrong shipping terms, which
    costs real money.
    """
    wanted = {
        "fulfillment": shipping_name,
        "return": return_name,
        "payment": payment_name,
    }
    suggested: Dict[str, Optional[str]] = {}
    for kind, name in wanted.items():
        target = str(name or "").strip().casefold()
        suggested[kind] = None
        if not target:
            continue
        for entry in policies.get(kind) or []:
            if entry["name"].strip().casefold() == target:
                suggested[kind] = entry["id"]
                break
    return suggested
