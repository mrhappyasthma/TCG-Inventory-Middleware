"""
The adapter binding ``tcg_engine.push`` to ``ebay_client``.

It exists because neither side may import the other. ``ebay_client`` speaks
SKUs, offers and inventory item groups and knows nothing about manifest ids,
pricing rules or SortSwift; ``tcg_engine`` holds no dependency on the eBay
library at all, which is what lets the whole push be exercised against a fake
with no network, no credentials and no quota. Translation has to happen
somewhere, and the web layer -- which already owns the client instance and the
signed-in user -- is that somewhere.

It is deliberately thin. Anything with a decision in it belongs on one side or
the other: payload shapes in ``tcg_engine.push`` where the card vocabulary
lives, HTTP behaviour in ``ebay_client`` where the retries and tokens live.
"""

from typing import Any, Dict, List, Sequence, Tuple

from ebay_client import inventory


class InventoryApiAdapter:
    """
    The surface ``push_plan`` expects, backed by the real eBay API.

    One transport, the seller's: every call here acts as the connected seller.
    Handing it the application transport instead produces a 403 whose message
    does not mention tokens at all.
    """

    def __init__(self, client):
        self.transport = client.seller
        self.marketplace_id = client.config.marketplace_id

    def upsert_items(
        self, items: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        return inventory.bulk_create_or_replace_inventory_item(
            self.transport, items
        )

    def create_offer(self, payload: Dict[str, Any]) -> str:
        return inventory.create_offer(self.transport, payload)

    def update_price_quantity(
        self, requests: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        return inventory.bulk_update_price_quantity(self.transport, requests)

    def upsert_group(self, group_key: str, payload: Dict[str, Any]) -> None:
        inventory.create_or_replace_inventory_item_group(
            self.transport, group_key, payload
        )

    def get_group(self, group_key: str) -> Dict[str, Any]:
        """
        Read a group back before overwriting it.

        Writing a group is a full replace, so anything we do not send is
        removed. This is how a repair preserves what it does not manage --
        a cover photo set outside this application, for instance -- rather
        than silently discarding it.
        """
        return inventory.get_inventory_item_group(self.transport, group_key)

    def publish_group(self, group_key: str) -> str:
        return inventory.publish_offer_by_inventory_item_group(
            self.transport, group_key, self.marketplace_id
        )

    def publish_offer(self, offer_id: str) -> str:
        return inventory.publish_offer(self.transport, offer_id)

    def withdraw_offer(self, offer_id: str) -> None:
        inventory.withdraw_offer(self.transport, offer_id)

    def failures(
        self, rows: Sequence[Dict[str, Any]]
    ) -> List[Tuple[str, str]]:
        """
        The rows eBay refused, as (sku, reason).

        This is the whole reason the adapter is not just ``getattr``: a bulk
        call answers HTTP 200 and reports the outcome *per SKU* inside the
        body, so "did it work" is a question about the payload rather than
        the status code.
        """
        return [
            (str(row.get("sku") or ""), inventory.describe_failure(row))
            for row in inventory.failed_statuses(rows)
        ]
