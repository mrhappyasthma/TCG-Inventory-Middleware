"""
Tests for the Inventory API write surface.

Weighted towards the ways a push can appear to succeed and not have. The
bulk calls answer HTTP 200 and report failure per SKU inside the body, so the
tests that matter most are the ones asserting a 200 containing errors is
recognised as a failure -- that is the shape that would otherwise mark a card
"pushed" while eBay kept the old price.

Nothing here reaches the network: every call goes through an injected opener.
"""

import json
import unittest

from ebay_client import inventory
from ebay_client.errors import ApiError
from ebay_client.transport import Response, Transport


class RecordingOpener:
    """Replays queued responses and records every request made."""

    def __init__(self, *responses):
        self.queued = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({
            "method": method,
            "url": url,
            "body": json.loads(body.decode("utf-8")) if body else None,
        })
        if self.queued:
            return self.queued.pop(0)
        return Response(200, {}, b"{}")


def ok(payload):
    return Response(200, {"content-type": "application/json"},
                    json.dumps(payload).encode("utf-8"))


def transport_with(*responses):
    opener = RecordingOpener(*responses)
    return Transport(
        "https://api.example",
        token_provider=lambda force_refresh=False: "token",
        opener=opener,
    ), opener


class SkuValidationTests(unittest.TestCase):
    def test_a_blank_sku_is_refused(self):
        with self.assertRaises(ValueError):
            inventory.validate_sku("  ")

    def test_an_over_long_sku_names_itself(self):
        # Caught here rather than as a generic 400 on a 25-record batch,
        # where finding which SKU was at fault is the expensive part.
        with self.assertRaises(ValueError) as caught:
            inventory.validate_sku("X" * 51)
        self.assertIn("51 characters", str(caught.exception))


class ChunkingTests(unittest.TestCase):
    def test_work_is_split_at_the_limit(self):
        batches = list(inventory.chunked(list(range(60)), 25))
        self.assertEqual([len(b) for b in batches], [25, 25, 10])

    def test_an_oversized_batch_is_refused_rather_than_truncated(self):
        # Sending 26 fails the whole call at eBay. Silently dropping the
        # surplus would be worse: the push would report success for cards it
        # never sent.
        transport, _ = transport_with()
        with self.assertRaises(ValueError) as caught:
            inventory.bulk_update_price_quantity(
                transport,
                [inventory.price_quantity_request(f"ID{i}", quantity=1)
                 for i in range(26)],
            )
        self.assertIn("25", str(caught.exception))


class InventoryItemTests(unittest.TestCase):
    def test_create_or_replace_puts_to_the_sku_url(self):
        transport, opener = transport_with(Response(204, {}, b""))
        inventory.create_or_replace_inventory_item(
            transport, "ID1443", {"condition": "LIKE_NEW"}
        )
        call = opener.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertTrue(call["url"].endswith("/inventory_item/ID1443"))
        self.assertEqual(call["body"], {"condition": "LIKE_NEW"})


class OfferTests(unittest.TestCase):
    def test_create_offer_returns_the_offer_id(self):
        transport, _ = transport_with(ok({"offerId": "9876"}))
        self.assertEqual(inventory.create_offer(transport, {"sku": "ID1"}), "9876")

    def test_an_offer_created_without_an_id_is_an_error(self):
        # A 200 with no offerId leaves nothing to publish or revise later;
        # returning None here would defer the failure to a confusing point.
        transport, _ = transport_with(ok({"warnings": [{"message": "odd"}]}))
        with self.assertRaises(ApiError):
            inventory.create_offer(transport, {"sku": "ID1"})

    def test_bulk_create_returns_an_offer_id_per_sku(self):
        # 25 offers in one call rather than 25 calls. The ids in the response
        # are the only handles that can change those cards' prices later, so
        # the caller has to be able to read them back per SKU.
        transport, opener = transport_with(ok({"responses": [
            {"sku": "ID1443", "statusCode": 200, "offerId": "9001"},
            {"sku": "ID1451", "statusCode": 400,
             "errors": [{"longMessage": "no inventory item"}]},
        ]}))
        rows = inventory.bulk_create_offer(transport, [
            {"sku": "ID1443", "marketplaceId": "EBAY_US", "format": "FIXED_PRICE"},
            {"sku": "ID1451", "marketplaceId": "EBAY_US", "format": "FIXED_PRICE"},
        ])
        self.assertEqual(rows[0]["offerId"], "9001")
        self.assertEqual(
            [sku for sku, _ in
             [(r.get("sku"), r) for r in inventory.failed_statuses(rows)]],
            ["ID1451"],
        )
        self.assertTrue(opener.calls[0]["url"].endswith("/bulk_create_offer"))

    def test_an_oversized_offer_batch_is_refused(self):
        transport, _ = transport_with()
        with self.assertRaises(ValueError):
            inventory.bulk_create_offer(transport, [
                {"sku": f"ID{i}", "marketplaceId": "EBAY_US",
                 "format": "FIXED_PRICE"}
                for i in range(26)
            ])

    def test_no_offers_for_a_sku_is_an_empty_list(self):
        # This is what every File Exchange listing looks like through the
        # Inventory API: absent, not empty-stock. The distinction decides
        # whether a push creates a duplicate listing.
        transport, _ = transport_with(ok({}))
        self.assertEqual(inventory.get_offers(transport, "ID1443"), [])

    def test_publish_returns_the_listing_id(self):
        transport, _ = transport_with(ok({"listingId": "227511361186"}))
        self.assertEqual(
            inventory.publish_offer(transport, "9876"), "227511361186"
        )


class GroupTests(unittest.TestCase):
    def test_group_publish_posts_the_key_and_marketplace(self):
        transport, opener = transport_with(ok({"listingId": "2275"}))
        listing = inventory.publish_offer_by_inventory_item_group(
            transport, "BaseSet|NM", "EBAY_US"
        )
        self.assertEqual(listing, "2275")
        self.assertEqual(opener.calls[0]["body"], {
            "inventoryItemGroupKey": "BaseSet|NM",
            "marketplaceId": "EBAY_US",
        })

    def test_a_group_publish_with_no_listing_id_is_an_error(self):
        transport, _ = transport_with(ok({}))
        with self.assertRaises(ApiError):
            inventory.publish_offer_by_inventory_item_group(
                transport, "BaseSet|NM", "EBAY_US"
            )


class PriceQuantityRequestTests(unittest.TestCase):
    def test_quantity_goes_on_the_inventory_item(self):
        request = inventory.price_quantity_request("ID1443", quantity=4)
        self.assertEqual(
            request, {"sku": "ID1443",
                      "shipToLocationAvailability": {"quantity": 4}}
        )

    def test_price_goes_on_the_offer_and_needs_an_offer_id(self):
        request = inventory.price_quantity_request(
            "ID1443", offer_id="99", price=1.9899
        )
        self.assertEqual(request["offers"], [
            {"offerId": "99", "price": {"value": "1.99", "currency": "USD"}}
        ])
        # Price lives on the offer, so without an offer id there is nothing
        # to address -- caught here rather than as an opaque eBay 400.
        with self.assertRaises(ValueError):
            inventory.price_quantity_request("ID1443", price=1.99)

    def test_a_zero_out_leaves_the_price_alone(self):
        # Out of stock is not a sale. An omitted field is untouched.
        request = inventory.price_quantity_request("ID1443", quantity=0)
        self.assertNotIn("offers", request)
        self.assertEqual(request["shipToLocationAvailability"]["quantity"], 0)

    def test_a_request_that_changes_nothing_is_refused(self):
        with self.assertRaises(ValueError):
            inventory.price_quantity_request("ID1443")

    def test_a_negative_quantity_is_refused(self):
        with self.assertRaises(ValueError):
            inventory.price_quantity_request("ID1443", quantity=-1)


class BulkOutcomeTests(unittest.TestCase):
    def test_a_200_carrying_per_sku_errors_is_a_partial_failure(self):
        """
        The failure mode this module exists to prevent.

        eBay answers 200 for the batch and reports the outcome per SKU. Read
        as "HTTP 200, therefore done", every card is marked pushed while some
        of them kept their old price -- and the mirror then disagrees with
        the store with nothing to show why.
        """
        transport, _ = transport_with(ok({"responses": [
            {"sku": "ID1443", "statusCode": 200},
            {"sku": "ID1451", "statusCode": 400,
             "errors": [{"errorId": 25710, "longMessage": "SKU not found"}]},
        ]}))
        rows = inventory.bulk_update_price_quantity(transport, [
            inventory.price_quantity_request("ID1443", quantity=4),
            inventory.price_quantity_request("ID1451", quantity=3),
        ])
        failures = inventory.failed_statuses(rows)
        self.assertEqual(len(failures), 1)
        self.assertEqual(
            inventory.describe_failure(failures[0]), "ID1451: SKU not found"
        )

    def test_both_envelope_names_are_understood(self):
        # eBay calls it "responses" on one endpoint and "results" on another.
        # Guessing wrong yields an empty list, which reads as total success.
        self.assertEqual(
            inventory.bulk_statuses({"results": [{"sku": "A"}]}),
            [{"sku": "A"}],
        )
        self.assertEqual(
            inventory.bulk_statuses({"responses": [{"sku": "B"}]}),
            [{"sku": "B"}],
        )

    def test_a_row_with_only_warnings_is_a_success(self):
        # eBay warns about things like a picture it could not fetch. Failing
        # the push over that would block a listing that went up correctly.
        row = {"sku": "ID1", "statusCode": 200,
               "warnings": [{"message": "picture skipped"}]}
        self.assertFalse(inventory.status_failed(row))

    def test_a_row_with_errors_and_no_status_code_is_a_failure(self):
        self.assertTrue(
            inventory.status_failed({"sku": "ID1", "errors": [{"errorId": 1}]})
        )

    def test_an_empty_batch_makes_no_call(self):
        transport, opener = transport_with()
        self.assertEqual(inventory.bulk_update_price_quantity(transport, []), [])
        self.assertEqual(opener.calls, [])


class OfferListingIdTests(unittest.TestCase):
    """
    Where getOffers puts the listing id, which is not where the other calls
    put it. Reading the wrong level reported a successful migration as a
    failure.
    """

    def test_the_id_is_read_from_the_nested_listing(self):
        offer = {
            "offerId": "9001",
            "status": "PUBLISHED",
            "listing": {"listingId": "227513097221", "listingStatus": "ACTIVE"},
        }
        self.assertEqual(inventory.offer_listing_id(offer), "227513097221")

    def test_a_top_level_id_is_still_honoured(self):
        # publishOffer and bulkMigrateListing do report it at the top level.
        self.assertEqual(
            inventory.offer_listing_id({"listingId": "227513097221"}),
            "227513097221",
        )

    def test_an_unpublished_offer_has_no_listing(self):
        for offer in ({}, {"offerId": "9001"}, {"listing": {}},
                      {"listing": None}, {"listingId": ""}):
            with self.subTest(offer=offer):
                self.assertEqual(inventory.offer_listing_id(offer), "")


if __name__ == "__main__":
    unittest.main()
