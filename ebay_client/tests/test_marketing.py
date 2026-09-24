"""
Tests for the Promoted Listings calls.

The property that matters most is not here but at the call site: promoting
must never fail a listing. What is pinned here is that a bad bid is
refused while we still know which value it was, rather than arriving as a
400 against a listing that has just gone live.
"""

import unittest

from ebay_client import marketing


class FakeTransport:
    def __init__(self, get_response=None, post_response=None):
        self._get = get_response if get_response is not None else {}
        self._post = post_response if post_response is not None else {}
        self.gets = []
        self.posts = []

    def get(self, path):
        self.gets.append(path)
        return self._get

    def post(self, path, payload):
        self.posts.append((path, payload))
        return self._post


class FormatBidTests(unittest.TestCase):
    def test_it_renders_two_decimals(self):
        self.assertEqual(marketing.format_bid(2.1), "2.10")
        self.assertEqual(marketing.format_bid("2.1"), "2.10")
        self.assertEqual(marketing.format_bid(10), "10.00")

    def test_the_bounds_are_ebays_own(self):
        self.assertEqual(marketing.format_bid(2.0), "2.00")
        self.assertEqual(marketing.format_bid(100), "100.00")

    def test_below_the_minimum_is_refused_by_value(self):
        with self.assertRaises(ValueError) as caught:
            marketing.format_bid(1.5)
        self.assertIn("1.5", str(caught.exception))

    def test_above_the_maximum_is_refused(self):
        with self.assertRaises(ValueError):
            marketing.format_bid(101)

    def test_something_that_is_not_a_number_is_refused_by_value(self):
        with self.assertRaises(ValueError) as caught:
            marketing.format_bid("two percent")
        self.assertIn("two percent", str(caught.exception))


class CampaignTests(unittest.TestCase):
    def test_campaigns_are_read_out_of_the_envelope(self):
        transport = FakeTransport(get_response={"campaigns": [
            {"campaignId": "1", "campaignName": "Cards", "campaignStatus": "RUNNING"},
        ]})
        found = marketing.get_campaigns(transport, marketplace_id="EBAY_US")
        self.assertEqual(len(found), 1)
        self.assertIn("marketplace_id=EBAY_US", transport.gets[0])

    def test_a_response_with_no_campaigns_is_an_empty_list(self):
        self.assertEqual(marketing.get_campaigns(FakeTransport()), [])

    def test_only_usable_campaigns_are_offered(self):
        """
        An ended or paused campaign accepts an ad and promotes nothing,
        which is the kind of success worth filtering out before it is
        offered as a choice.
        """
        campaigns = [
            {"campaignId": "1", "campaignStatus": "RUNNING"},
            {"campaignId": "2", "campaignStatus": "ENDED"},
            {"campaignId": "3", "campaignStatus": "PAUSED"},
            {"campaignId": "4", "campaignStatus": "SCHEDULED"},
        ]
        self.assertEqual(
            [c["campaignId"] for c in marketing.running_campaigns(campaigns)],
            ["1", "4"],
        )


class CreateAdTests(unittest.TestCase):
    def test_it_addresses_the_listing_and_sends_the_bid(self):
        transport = FakeTransport(post_response={"adId": "99"})
        ad_id = marketing.create_ad(transport, "CAMP1", "227536445754", "2.10")
        path, payload = transport.posts[0]
        self.assertEqual(path, "/sell/marketing/v1/ad_campaign/CAMP1/ad")
        self.assertEqual(payload, {
            "listingId": "227536445754", "bidPercentage": "2.10",
        })
        self.assertEqual(ad_id, "99")

    def test_an_empty_body_is_not_a_failure(self):
        """
        eBay answers 201 with a Location header and sometimes no body. The
        ad exists; we just did not learn its id.
        """
        self.assertIsNone(
            marketing.create_ad(FakeTransport(), "CAMP1", "123", "2.10")
        )


if __name__ == "__main__":
    unittest.main()
