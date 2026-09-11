"""
Tests for reading the seller's own account.

The point of this module is that business policy ids exist nowhere a human can
see them -- not in Seller Hub -- so the dashboard has to ask for them. What
the tests protect is that asking is done correctly: per marketplace, because
policies are scoped that way and omitting the marketplace is an error rather
than a wildcard; and that an account with no inventory location is reported as
such rather than looking broken, since that is the normal state of a seller
who has only ever listed through File Exchange.
"""

import json
import unittest

from ebay_client import account
from ebay_client.transport import Response, Transport


class RecordingOpener:
    def __init__(self, *responses):
        self.queued = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url,
                           "body": json.loads(body.decode()) if body else None})
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


class PolicyTests(unittest.TestCase):
    def test_all_three_kinds_are_read_for_the_marketplace(self):
        transport, opener = transport_with(
            ok({"fulfillmentPolicies": [
                {"fulfillmentPolicyId": "6001",
                 "name": "Free Shipping Cards", "description": "letter"}
            ]}),
            ok({"paymentPolicies": [
                {"paymentPolicyId": "6002", "name": "Immediate Payment"}
            ]}),
            ok({"returnPolicies": [
                {"returnPolicyId": "6003", "name": "No Returns"}
            ]}),
        )

        policies = account.get_policies(transport, "EBAY_US")

        self.assertEqual(policies["fulfillment"][0]["id"], "6001")
        self.assertEqual(policies["payment"][0]["name"], "Immediate Payment")
        self.assertEqual(policies["return"][0]["id"], "6003")
        # Policies are per marketplace and eBay requires the parameter, so
        # every one of the three calls has to carry it.
        for call in opener.calls:
            self.assertIn("marketplace_id=EBAY_US", call["url"])

    def test_a_policy_without_an_id_is_skipped(self):
        # An entry we cannot address is worse than absent: offered in a
        # chooser it would be selected and then rejected at push time.
        transport, _ = transport_with(
            ok({"fulfillmentPolicies": [{"name": "Broken"}]}),
            ok({}),
            ok({}),
        )
        self.assertEqual(account.get_policies(transport)["fulfillment"], [])

    def test_an_account_with_no_policies_returns_empty_lists(self):
        transport, _ = transport_with(ok({}), ok({}), ok({}))
        policies = account.get_policies(transport)
        self.assertEqual(
            policies, {"fulfillment": [], "payment": [], "return": []}
        )


class SuggestionTests(unittest.TestCase):
    def setUp(self):
        self.policies = {
            "fulfillment": [{"id": "6001", "name": "Free Shipping Cards"},
                            {"id": "6009", "name": "Calculated"}],
            "payment": [{"id": "6002", "name": "Immediate Payment"}],
            "return": [{"id": "6003", "name": "No Returns"}],
        }

    def test_the_csv_names_already_configured_are_matched_to_ids(self):
        # These names have been going out in File Exchange files for months,
        # so they are the best hint at which policy is meant -- turning setup
        # into "confirm these three" instead of "find three ids".
        suggested = account.suggest_policy_ids(
            self.policies,
            shipping_name="Free Shipping Cards",
            return_name="No Returns",
            payment_name="Immediate Payment",
        )
        self.assertEqual(suggested,
                         {"fulfillment": "6001", "return": "6003",
                          "payment": "6002"})

    def test_matching_ignores_case_and_surrounding_space(self):
        suggested = account.suggest_policy_ids(
            self.policies, shipping_name="  free shipping cards "
        )
        self.assertEqual(suggested["fulfillment"], "6001")

    def test_a_near_miss_suggests_nothing(self):
        # Listing against the wrong shipping policy costs real money, so a
        # partial match is refused rather than guessed.
        suggested = account.suggest_policy_ids(
            self.policies, shipping_name="Free Shipping"
        )
        self.assertIsNone(suggested["fulfillment"])


class LocationTests(unittest.TestCase):
    def test_locations_are_flattened_to_what_a_chooser_needs(self):
        transport, _ = transport_with(ok({"locations": [{
            "merchantLocationKey": "home",
            "name": "Home",
            "merchantLocationStatus": "ENABLED",
            "location": {"address": {"postalCode": "94305", "country": "US"}},
        }]}))
        locations = account.get_inventory_locations(transport)
        self.assertEqual(locations, [{
            "key": "home", "name": "Home", "status": "ENABLED",
            "postal_code": "94305", "country": "US",
        }])

    def test_no_locations_is_an_empty_list_not_an_error(self):
        # The normal state of an account that has only used File Exchange.
        # eBay still refuses to publish an offer without one, so this is the
        # signal to create it -- not a sign anything is wrong.
        transport, _ = transport_with(ok({}))
        self.assertEqual(account.get_inventory_locations(transport), [])

    def test_a_warehouse_location_needs_no_street_address(self):
        transport, opener = transport_with(Response(204, {}, b""))
        key = account.create_inventory_location(
            transport, "home", name="Home", postal_code="94305"
        )
        self.assertEqual(key, "home")
        call = opener.calls[0]
        self.assertTrue(call["url"].endswith("/location/home"))
        self.assertEqual(call["body"]["location"]["address"],
                         {"postalCode": "94305", "country": "US"})
        self.assertEqual(call["body"]["locationTypes"], ["WAREHOUSE"])
        self.assertNotIn("addressLine1", call["body"]["location"]["address"])

    def test_an_over_long_key_is_refused_before_it_is_permanent(self):
        # eBay does not allow a location key to change once set, so a bad one
        # is forever. Caught here rather than as a 400.
        transport, _ = transport_with()
        with self.assertRaises(ValueError):
            account.create_inventory_location(
                transport, "x" * 51, name="Home", postal_code="94305"
            )

    def test_a_location_without_a_postal_code_is_refused(self):
        transport, _ = transport_with()
        with self.assertRaises(ValueError):
            account.create_inventory_location(
                transport, "home", name="Home", postal_code=" "
            )


if __name__ == "__main__":
    unittest.main()
