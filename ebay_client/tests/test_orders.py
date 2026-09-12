"""
Tests for reading orders.

Weighted towards pagination, because a truncated result here does not look
like a failure: it looks like a quieter week. Every sale it loses leaves a
card on the shelf that has actually gone, and the next draft offers it again.

Nothing reaches the network; every call goes through an injected opener.
"""

import json
import unittest

from ebay_client import orders
from ebay_client.transport import Response, Transport


class RecordingOpener:
    def __init__(self, *responses):
        self.queued = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url})
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


def page(order_ids, total, limit=50, offset=0):
    return ok({
        "total": total,
        "limit": limit,
        "offset": offset,
        "orders": [
            {
                "orderId": oid,
                "lastModifiedDate": "2026-09-11T09:00:00.000Z",
                "orderPaymentStatus": "PAID",
                "cancelStatus": {"cancelState": "NONE_REQUESTED"},
                "lineItems": [
                    {"lineItemId": f"{oid}-1", "sku": "ID1435", "quantity": 1,
                     "lineItemFulfillmentStatus": "NOT_STARTED"},
                ],
            }
            for oid in order_ids
        ],
    })


class FilterTests(unittest.TestCase):
    def test_the_filter_is_an_open_ended_range(self):
        self.assertEqual(
            orders.modified_since_filter("2026-09-11T08:00:00.000Z"),
            "lastmodifieddate:[2026-09-11T08:00:00.000Z..]",
        )

    def test_a_blank_timestamp_is_refused(self):
        # Silently omitting the filter would ask for ninety days of orders on
        # every poll, which is a different call than the caller asked for.
        with self.assertRaises(ValueError):
            orders.modified_since_filter("  ")

    def test_the_filter_survives_url_encoding(self):
        # Its syntax depends on brackets, colons and "..": percent-encode them
        # and eBay rejects the request.
        transport, opener = transport_with(page(["1-1"], 1))
        orders.get_orders(transport, modified_since="2026-09-11T08:00:00.000Z")
        url = opener.calls[0]["url"]
        self.assertIn("lastmodifieddate:[2026-09-11T08:00:00.000Z..]", url)


class PaginationTests(unittest.TestCase):
    def test_a_single_page_is_returned_whole(self):
        transport, opener = transport_with(page(["1-1", "1-2"], 2))
        result = orders.get_orders(transport)
        self.assertEqual([o["orderId"] for o in result], ["1-1", "1-2"])
        self.assertEqual(len(opener.calls), 1, "one page needs one call")

    def test_every_page_is_fetched(self):
        # The failure this module exists to prevent: 50 of 120 sales read,
        # reported as though that were all of them.
        transport, opener = transport_with(
            page([f"a{i}" for i in range(50)], 120, offset=0),
            page([f"b{i}" for i in range(50)], 120, offset=50),
            page([f"c{i}" for i in range(20)], 120, offset=100),
        )
        result = orders.get_orders(transport)
        self.assertEqual(len(result), 120)
        self.assertEqual(len(opener.calls), 3)
        # The offset advances by what was actually received, not by the
        # requested page size.
        self.assertIn("offset=50", opener.calls[1]["url"])
        self.assertIn("offset=100", opener.calls[2]["url"])

    def test_an_empty_page_ends_it(self):
        # eBay's total is a count taken at the first page. An order modified
        # mid-pagination can move it, so the empty page is what is trusted.
        transport, opener = transport_with(
            page(["a"], 99),
            ok({"total": 99, "orders": []}),
        )
        self.assertEqual(len(orders.get_orders(transport)), 1)
        self.assertEqual(len(opener.calls), 2)

    def test_a_response_with_no_orders_key_is_not_a_crash(self):
        transport, _ = transport_with(ok({"total": 0}))
        self.assertEqual(orders.get_orders(transport), [])

    def test_pagination_that_will_not_end_raises(self):
        # Better a loud failure than a poller quietly spending its rate limit
        # forever, and better than returning what it happens to have.
        pages = [page([f"x{i}"], 10_000) for i in range(5)]
        transport, opener = transport_with(*pages)
        with self.assertRaises(orders.OrderPageError):
            orders.get_orders(transport, max_pages=3)
        self.assertEqual(len(opener.calls), 3)

    def test_a_partial_result_is_never_returned(self):
        # Restating the point of the exception: the caller must not be able to
        # mistake a truncated read for a complete one.
        transport, _ = transport_with(*[page([f"x{i}"], 500) for i in range(4)])
        try:
            orders.get_orders(transport, max_pages=2)
        except orders.OrderPageError as exc:
            self.assertIn("partial", str(exc))
        else:
            self.fail("a truncated read must raise")


class OrderShapeTests(unittest.TestCase):
    def test_line_items_tolerates_a_missing_or_odd_field(self):
        self.assertEqual(orders.line_items({}), [])
        self.assertEqual(orders.line_items({"lineItems": None}), [])
        self.assertEqual(orders.line_items({"lineItems": ["not a dict"]}), [])
        self.assertEqual(
            orders.line_items({"lineItems": [{"sku": "ID1"}]}), [{"sku": "ID1"}]
        )

    def test_a_cancelled_order_is_recognised(self):
        self.assertTrue(
            orders.is_cancelled({"cancelStatus": {"cancelState": "CANCELED"}})
        )
        # Both spellings, because eBay's enums are not consistent about it.
        self.assertTrue(
            orders.is_cancelled({"cancelStatus": {"cancelState": "CANCELLED"}})
        )

    def test_a_requested_cancellation_is_not_a_cancellation(self):
        # The buyer asking is not the same as it happening, and treating it as
        # such would put a card back on sale that is still going out.
        self.assertFalse(
            orders.is_cancelled(
                {"cancelStatus": {"cancelState": "CANCEL_REQUESTED"}}
            )
        )
        self.assertFalse(
            orders.is_cancelled(
                {"cancelStatus": {"cancelState": "NONE_REQUESTED"}}
            )
        )
        self.assertFalse(orders.is_cancelled({}))

    def test_a_full_refund_is_recognised(self):
        self.assertTrue(
            orders.is_fully_refunded({"orderPaymentStatus": "FULLY_REFUNDED"})
        )
        self.assertFalse(
            orders.is_fully_refunded({"orderPaymentStatus": "PAID"})
        )
        self.assertFalse(
            orders.is_fully_refunded(
                {"orderPaymentStatus": "PARTIALLY_REFUNDED"}
            )
        )


if __name__ == "__main__":
    unittest.main()
