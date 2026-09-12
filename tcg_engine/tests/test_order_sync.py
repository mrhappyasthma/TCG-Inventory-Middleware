"""
Tests for deducting sold cards from the catalogue.

The behaviours worth the most scrutiny are the two that cost real stock when
wrong, and they pull against each other.

**Not twice.** The poller re-reads a window of orders it has already seen,
because overlapping is how a sale is not missed. So the same line arriving a
second time must deduct nothing, and that has to hold even for a line the
poller has never recorded reading -- the claim lives in the database, not in
the poller's memory.

**Not never.** A cancellation must not be treated as a sale, a SKU that
matches nothing must not be silently dropped, and a catalogue that held less
than eBay sold must say so rather than going negative.
"""

import os
import tempfile
import unittest

from tcg_engine.db import Database
from tcg_engine.order_sync import (
    REQUIRED_LINE_FIELDS,
    STATUS_ACTIVE,
    STATUS_CANCELED,
    STATUS_REFUNDED,
    OrderSyncError,
    sync_orders,
)


# One of ours by default: the fixture seeds this listing in setUp, so a
# line is "from a listing we manage" unless a test says otherwise.
OUR_LISTING = "227511361186"
THEIR_LISTING = "227496856039"


def line(order="12-345", item="L1", sku="ID1001", quantity=1,
         status=STATUS_ACTIVE, sold_at="2026-09-11T09:00:00Z",
         item_id=OUR_LISTING):
    """One projected line, in exactly the shape the app layer hands over."""
    return {
        "order_id": order,
        "line_item_id": item,
        "sku": sku,
        "legacy_item_id": item_id,
        "quantity": quantity,
        "status": status,
        "sold_at": sold_at,
    }


class OrderSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "orders.db"))
        self.db.insert_manifest("ID1001", "Charizard", "Base Set", "Near Mint",
                                "Holofoil", card_number="004/102")
        self.db.set_manifest_quantity("ID1001", 5)
        # Linked to one of our listings, so an unresolved SKU on it is a
        # genuine fault rather than somebody else's sale.
        self.db.upsert_variation("ID1001", OUR_LISTING, 5,
                                 custom_label="ID1001")

    def tearDown(self):
        self.temp_dir.cleanup()

    def held(self, manifest_id="ID1001"):
        return self.db.get_manifest_by_id(manifest_id)["quantity"]

    # -- the happy path ---------------------------------------------------

    def test_a_sale_comes_off_the_catalogue(self):
        result = sync_orders(self.db, [line(quantity=2)])
        self.assertEqual(result["deducted"], 1)
        self.assertEqual(result["deducted_cards"], 2)
        self.assertEqual(self.held(), 3)

    def test_a_multi_quantity_line_deducts_its_quantity(self):
        # A buyer taking three of one card is one line item with quantity 3,
        # not three line items. Counting lines would deduct 1.
        sync_orders(self.db, [line(quantity=3)])
        self.assertEqual(self.held(), 2)

    def test_several_lines_in_one_order_each_deduct(self):
        self.db.insert_manifest("ID1002", "Blastoise", "Base Set", "Near Mint",
                                "Holofoil", card_number="002/102")
        self.db.set_manifest_quantity("ID1002", 4)
        result = sync_orders(self.db, [
            line(item="L1", sku="ID1001", quantity=2),
            line(item="L2", sku="ID1002", quantity=1),
        ])
        self.assertEqual(result["deducted"], 2)
        self.assertEqual(self.held("ID1001"), 3)
        self.assertEqual(self.held("ID1002"), 3)

    def test_the_bin_suffix_is_stripped_to_find_the_card(self):
        # eBay knows the variation as ID1001-Bin_A-12; the bin is not part of
        # the card's identity.
        sync_orders(self.db, [line(sku="ID1001-Bin_A-12", quantity=1)])
        self.assertEqual(self.held(), 4)

    # -- exactly once -----------------------------------------------------

    def test_the_same_line_twice_deducts_once(self):
        """The overlap window means this is the normal case, not an edge."""
        sync_orders(self.db, [line(quantity=2)])
        self.assertEqual(self.held(), 3)

        result = sync_orders(self.db, [line(quantity=2)])
        self.assertEqual(result["deducted"], 0)
        self.assertEqual(result["already"], 1)
        self.assertEqual(self.held(), 3, "a re-read must not deduct again")

    def test_the_claim_is_in_the_database_not_in_memory(self):
        """
        Two pollers, or a restart mid-poll, must not double-deduct.

        Simulated by syncing the same line through a second Database handle
        on the same file: nothing is shared in process, so if the guard were
        an in-memory set it would deduct twice.
        """
        sync_orders(self.db, [line(quantity=2)])
        other = Database(self.db.db_path)
        result = sync_orders(other, [line(quantity=2)])
        self.assertEqual(result["deducted"], 0)
        self.assertEqual(self.held(), 3)

    def test_a_line_seen_but_not_deducted_is_deducted_later(self):
        # Recorded and deducted are different facts. A line first seen while
        # its card was missing must still deduct once the card exists.
        first = sync_orders(self.db, [line(sku="ID9999", quantity=1)])
        self.assertEqual(first["unmatched"], 1)

        self.db.insert_manifest("ID9999", "Pikachu", "Base Set", "Near Mint",
                                "Normal", card_number="058/102")
        self.db.set_manifest_quantity("ID9999", 2)
        second = sync_orders(self.db, [line(sku="ID9999", quantity=1)])
        self.assertEqual(second["deducted"], 1)
        self.assertEqual(self.held("ID9999"), 1)

    # -- not a sale -------------------------------------------------------

    def test_a_cancelled_line_deducts_nothing(self):
        result = sync_orders(self.db, [line(quantity=2, status=STATUS_CANCELED)])
        self.assertEqual(result["deducted"], 0)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(self.held(), 5)
        self.assertTrue(any("cancel" in l["message"].lower()
                            for l in result["logs"]))

    def test_a_refunded_line_deducts_nothing(self):
        result = sync_orders(self.db, [line(quantity=1, status=STATUS_REFUNDED)])
        self.assertEqual(result["deducted"], 0)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(self.held(), 5)

    def test_a_cancellation_after_a_deduction_is_reported_not_reversed(self):
        """
        Money coming back does not put a card on the shelf.

        It may already have shipped, so the stock is not restored
        automatically -- it is raised with a person, which is the same
        asymmetry the repricer applies to a price drop.
        """
        sync_orders(self.db, [line(quantity=2)])
        self.assertEqual(self.held(), 3)

        result = sync_orders(self.db, [line(quantity=2, status=STATUS_CANCELED)])
        self.assertEqual(self.held(), 3, "stock must not be restored silently")
        self.assertTrue(
            any("already deducted" in l["message"] for l in result["logs"]),
            "the operator has to be told it needs a decision",
        )

    def test_an_unmatched_sku_is_named_once_then_tallied(self):
        """
        Still reported on every poll, but not re-named on every poll.

        An unmatched line is never claimed, so it comes back forever. At
        a poll every fifteen minutes, naming it each time is ninety-six
        identical lines a day -- and a real first poll produced
        forty-five in one go, which buried the summary explaining them.
        So the first sighting names it and later ones are counted.
        """
        first = sync_orders(self.db, [line(sku="ID7777")])
        self.assertEqual(first["unmatched"], 1)
        self.assertEqual(first["deducted"], 0)
        self.assertTrue(any("no catalogued card matches" in l["message"]
                            for l in first["logs"]),
                        "the first sighting must name it")

        second = sync_orders(self.db, [line(sku="ID7777")])
        self.assertEqual(second["unmatched"], 1, "still counted")
        self.assertEqual(second["repeated_unmatched"], 1)
        self.assertFalse(any("no catalogued card matches" in l["message"]
                             for l in second["logs"]),
                         "but not named a second time")
        self.assertTrue(any("still match no catalogued card" in l["message"]
                            for l in second["logs"]),
                        "the tally has to be impossible to miss")

    def test_a_blank_sku_is_reported_rather_than_guessed_at(self):
        result = sync_orders(self.db, [line(sku="")])
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(self.held(), 5)

    # -- arithmetic that disagrees with reality ---------------------------

    def test_selling_more_than_we_hold_clamps_at_zero_and_says_so(self):
        self.db.set_manifest_quantity("ID1001", 1)
        result = sync_orders(self.db, [line(quantity=3)])
        self.assertEqual(self.held(), 0, "never negative")
        self.assertEqual(result["deducted_cards"], 1, "only what was there")
        self.assertTrue(
            any("held only" in l["message"] for l in result["logs"]),
            "a count that was already short must be surfaced",
        )
        # And it is still marked deducted, so the next poll does not retry it.
        repeat = sync_orders(self.db, [line(quantity=3)])
        self.assertEqual(repeat["already"], 1)

    def test_a_zero_quantity_line_changes_nothing(self):
        result = sync_orders(self.db, [line(quantity=0)])
        self.assertEqual(result["deducted"], 0)
        self.assertEqual(self.held(), 5)

    def test_an_empty_batch_is_not_an_error(self):
        result = sync_orders(self.db, [])
        self.assertEqual(result["seen"], 0)
        self.assertEqual(result["deducted"], 0)

    # -- whose sale is it anyway ------------------------------------------

    def test_a_sale_from_a_listing_we_do_not_manage_is_not_a_warning(self):
        """
        The bug a real first poll surfaced, 46 times over.

        Every warning was an ordinary sale from a listing made by hand -- a
        promo single, an empty Elite Trainer Box, a Gamecube case. No SKU, no
        catalogued card, nothing to deduct, and nothing wrong. Reporting those
        as faults buried the one line that explained them.
        """
        result = sync_orders(self.db, [
            line(sku="", item_id=THEIR_LISTING),
        ])
        self.assertEqual(result["foreign"], 1)
        self.assertEqual(result["unmatched"], 0, "not a fault")
        self.assertEqual(result["deducted"], 0)
        self.assertEqual(self.held(), 5)
        self.assertFalse(
            any(l["level"] == "WARN" for l in result["logs"]),
            "somebody else's sale must not warn",
        )

    def test_a_foreign_sale_is_never_reconsidered(self):
        # Terminal: there is no future in which it becomes deductible, so
        # leaving it unclaimed would mean re-deciding it every fifteen
        # minutes forever.
        sync_orders(self.db, [line(sku="", item_id=THEIR_LISTING)])
        row = self.db.get_recent_order_lines()[0]
        self.assertIsNotNone(row["deducted_at"])
        self.assertEqual(row["deducted_qty"], 0)

        again = sync_orders(self.db, [line(sku="", item_id=THEIR_LISTING)])
        self.assertEqual(again["already"], 1)
        self.assertEqual(again["foreign"], 0, "not counted twice")

    def test_an_unresolved_sku_on_our_own_listing_is_still_a_fault(self):
        """
        The case the classification must not swallow.

        A listing we have a record of, whose SKU matches no card, means a card
        catalogued under another id -- and that is worth naming every time a
        new one appears.
        """
        result = sync_orders(self.db, [
            line(sku="ID7777", item_id=OUR_LISTING),
        ])
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(result["foreign"], 0)
        self.assertTrue(any(l["level"] == "WARN" for l in result["logs"]))

    def test_a_line_with_no_item_number_is_treated_as_ours(self):
        # Unidentifiable, so it gets the cautious reading: named rather than
        # dismissed. Better a warning about a sale that was not ours than
        # silence about one that was.
        result = sync_orders(self.db, [line(sku="", item_id="")])
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(result["foreign"], 0)

    def test_the_item_number_is_recorded(self):
        sync_orders(self.db, [line(item_id=OUR_LISTING)])
        self.assertEqual(
            self.db.get_recent_order_lines()[0]["legacy_item_id"],
            OUR_LISTING,
        )

    # -- the first poll ---------------------------------------------------

    def test_the_first_poll_adopts_history_without_deducting_it(self):
        """
        eBay serves ninety days of orders, all of them already accounted for.

        Deducting that history on the first run would take three months of
        sales off the shelf a second time. So the first run records every line
        as handled and removes nothing.
        """
        result = sync_orders(self.db, [
            line(item="L1", quantity=2),
            line(item="L2", quantity=1),
        ], adopt=True)

        self.assertEqual(result["adopted"], 2)
        self.assertEqual(result["deducted"], 0)
        self.assertEqual(self.held(), 5, "no stock may move on a first poll")
        self.assertTrue(any("First poll" in l["message"]
                            for l in result["logs"]))

    def test_an_adopted_line_is_never_deducted_later(self):
        # The whole point: it is claimed, so the next real poll skips it.
        sync_orders(self.db, [line(quantity=2)], adopt=True)
        result = sync_orders(self.db, [line(quantity=2)])
        self.assertEqual(result["deducted"], 0)
        self.assertEqual(result["already"], 1)
        self.assertEqual(self.held(), 5)

    def test_a_sale_after_adoption_does_deduct(self):
        sync_orders(self.db, [line(item="OLD", quantity=2)], adopt=True)
        result = sync_orders(self.db, [line(item="NEW", quantity=1)])
        self.assertEqual(result["deducted"], 1)
        self.assertEqual(self.held(), 4)

    def test_adoption_records_zero_as_the_quantity_taken(self):
        # The stored record has to be honest: seen, accounted for, nothing
        # removed. A recorded 2 would read as a deduction that happened.
        sync_orders(self.db, [line(quantity=2)], adopt=True)
        row = self.db.get_recent_order_lines()[0]
        self.assertIsNotNone(row["deducted_at"])
        self.assertEqual(row["deducted_qty"], 0)

    def test_adoption_claims_a_line_whose_sku_matches_nothing(self):
        """
        The hazard the first version of this walked straight into.

        A real first poll saw 85 order lines and adopted none of them, because
        every SKU was blank and the unmatched check skipped the claim. Every
        one of those sales was then lying in wait: catalogue a card under that
        id, or fix whatever made the SKU blank, and the next poll would deduct
        three months of sales in one go.
        """
        result = sync_orders(self.db, [
            line(item="L1", sku=""),
            line(item="L2", sku="ID7777"),
        ], adopt=True)
        self.assertEqual(result["adopted"], 2,
                         "every line seen must be claimed, matched or not")

        # Now make both resolvable. They must still never deduct.
        self.db.insert_manifest("ID7777", "Sneasel", "Base Set", "Near Mint",
                                "Normal")
        self.db.set_manifest_quantity("ID7777", 3)
        later = sync_orders(self.db, [
            line(item="L1", sku="ID7777"),
            line(item="L2", sku="ID7777"),
        ])
        self.assertEqual(later["deducted"], 0)
        self.assertEqual(self.held("ID7777"), 3,
                         "adopted history must never deduct, ever")

    def test_adoption_claims_a_cancelled_line_too(self):
        sync_orders(self.db, [line(status=STATUS_CANCELED)], adopt=True)
        row = self.db.get_recent_order_lines()[0]
        self.assertIsNotNone(row["deducted_at"])

    def test_adoption_still_reports_an_unmatched_sku(self):
        # Claiming it silently would hide the thing worth investigating: a
        # blank SKU on every order is a configuration problem, not a quiet
        # week.
        result = sync_orders(self.db, [line(sku="")], adopt=True)
        self.assertEqual(result["unmatched"], 1)

    # -- the compliance boundary ------------------------------------------

    def test_a_line_carrying_anything_extra_is_refused(self):
        """
        The projection is a compliance boundary, so a breach fails loudly.

        An extra field is not ignored, because a field that is merely unused
        today is one somebody persists tomorrow -- and the fields getOrders
        actually carries are a buyer's name, address, email and phone.
        """
        intruder = line()
        intruder["buyer_email"] = "someone@example.com"
        with self.assertRaises(OrderSyncError) as caught:
            sync_orders(self.db, [intruder])
        self.assertIn("buyer_email", str(caught.exception))
        self.assertIn("projection", str(caught.exception))
        self.assertEqual(self.held(), 5, "nothing may move on a refused batch")

    def test_a_line_missing_a_field_is_refused(self):
        incomplete = line()
        del incomplete["quantity"]
        with self.assertRaises(OrderSyncError) as caught:
            sync_orders(self.db, [incomplete])
        self.assertIn("quantity", str(caught.exception))

    def test_the_allowed_fields_are_exactly_seven(self):
        # Pinned as a number as well as a set: adding one should be a
        # decision somebody makes deliberately, with this test in front of
        # them, rather than a diff nobody notices. legacy_item_id was
        # added that way: a public listing number, needed to tell our
        # sales from the ones listed by hand.
        self.assertEqual(len(REQUIRED_LINE_FIELDS), 7)
        self.assertEqual(REQUIRED_LINE_FIELDS, {
            "order_id", "line_item_id", "sku", "legacy_item_id",
            "quantity", "sold_at", "status",
        })

    def test_nothing_about_the_buyer_reaches_the_database(self):
        sync_orders(self.db, [line(quantity=1)])
        with self.db.get_connection() as conn:
            columns = {
                row["name"] for row in
                conn.execute("PRAGMA table_info(ebay_order_line)")
            }
        for forbidden in ("buyer", "username", "email", "phone", "address",
                          "name", "postal", "recipient"):
            matches = [c for c in columns if forbidden in c.lower()]
            self.assertEqual(
                matches, [],
                f"ebay_order_line has a column that looks like personal "
                f"data: {matches}",
            )


if __name__ == "__main__":
    unittest.main()
