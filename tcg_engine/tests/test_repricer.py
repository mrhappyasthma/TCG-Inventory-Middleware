"""
Tests for the automatic repricer.

The two behaviours worth the most scrutiny are the ones that exist to stop it
doing damage unattended, so they are tested against the arithmetic that
motivated them rather than against round numbers.

The tier boundary: the shipped rules price a card at $1.99 up to a market
price of $0.25 and $2.49 above it. A card sitting at $0.25 therefore has a
25% price change one cent away in either direction, every day, forever. The
margin has to make that impossible while still letting a real move through.

The hold window: a fall must not be applied on the day it appears, and must
be applied once it has persisted. Both halves matter -- a hold that never
expires is just a refusal to reprice.
"""

import os
import tempfile
import unittest
from datetime import timedelta

from tcg_engine.db import Database, SHARED_SCOPE
from tcg_engine.repricer import (
    CAP_MINIMUM_CARDS,
    VERDICT_DROP,
    VERDICT_HOLD,
    VERDICT_RAISE,
    VERDICT_SKIPPED,
    VERDICT_UNCHANGED,
    RepriceError,
    decide_card,
    format_timestamp,
    plan_reprice,
    run_reprice,
    target_price,
    utcnow,
)

# The rules the application ships with, as the fixture prices against.
TIERS = [
    {"min_price": 0.0, "max_price": 0.25, "rule_type": "fixed",
     "rule_value": 1.99},
    {"min_price": 0.25, "max_price": 0.50, "rule_type": "fixed",
     "rule_value": 2.49},
    {"min_price": 0.50, "max_price": 1.00, "rule_type": "fixed",
     "rule_value": 2.99},
    {"min_price": 1.00, "max_price": None, "rule_type": "markup_fixed",
     "rule_value": 3.00},
]

GROUP_KEY = "Unified Minds|Near Mint"
PARENT = "227516467787"


class FakeEbay:
    """
    Records the bulkUpdatePriceQuantity requests it is given.

    Keeps the raw payloads rather than a summary, because one of the things
    being asserted is the *absence* of a field.
    """

    def __init__(self, refuse=None):
        self.requests = []
        self.refuse = dict(refuse or {})

    def update_price_quantity(self, requests):
        self.requests.extend(requests)
        return [
            {"sku": r["sku"],
             "statusCode": 400 if r["sku"] in self.refuse else 200,
             "errors": ([{"message": self.refuse[r["sku"]]}]
                        if r["sku"] in self.refuse else [])}
            for r in requests
        ]

    def failures(self, rows):
        return [
            (row["sku"], row["errors"][0]["message"])
            for row in rows
            if row.get("statusCode") != 200
        ]


def card(**overrides):
    """One row as get_managed_cards_for_repricing returns it."""
    base = {
        "manifest_id": "ID1435",
        "product_name": "Charizard",
        "card_number": "045/132",
        "set_name": "Unified Minds",
        "condition": "Near Mint",
        "market_price": 0.19,
        "custom_label": "ID1435",
        "offer_id": "263144059011",
        "ebay_parent_id": PARENT,
        "last_known_price": 2.49,
        "last_known_qty": 2,
        "hold_since": None,
    }
    base.update(overrides)
    return base


def decide(card_row, *, margin=0.10, hold_days=14.0, now=None):
    return decide_card(
        card_row,
        rules=TIERS,
        multipliers={},
        margin_fraction=margin,
        hold_days=hold_days,
        now=now or utcnow(),
    )


class BoundaryMarginTests(unittest.TestCase):
    """The tiers are cliffs. This is what stops a card falling off one daily."""

    def test_a_one_cent_move_up_across_a_boundary_does_not_reprice(self):
        # Listed at $1.99, which is the tier below $0.25. The market has just
        # crossed to $0.26 -- a 4% market move that would otherwise be a 25%
        # price rise.
        price, _, note = target_price(TIERS, 0.26, 1.99, 0.10)
        self.assertEqual(price, 1.99)
        self.assertIn("tier boundary", note)

    def test_a_one_cent_move_down_across_a_boundary_does_not_reprice(self):
        price, _, note = target_price(TIERS, 0.24, 2.49, 0.10)
        self.assertEqual(price, 2.49)
        self.assertIn("tier boundary", note)

    def test_a_move_clear_of_the_margin_does_reprice(self):
        # 10% past $0.25 is $0.275. At $0.28 the card genuinely belongs in the
        # tier above, and the damping must not become a refusal to ever move.
        price, _, note = target_price(TIERS, 0.28, 1.99, 0.10)
        self.assertEqual(price, 2.49)
        self.assertIsNone(note)

    def test_a_fall_clear_of_the_margin_does_reprice(self):
        price, _, _ = target_price(TIERS, 0.22, 2.49, 0.10)
        self.assertEqual(price, 1.99)

    def test_the_margin_is_measured_from_the_boundary_not_the_price(self):
        # Crossing upward out of the $0.50-$1.00 tier: the boundary is $1.00,
        # so the threshold is $1.10 regardless of the tier's own price.
        self.assertEqual(target_price(TIERS, 1.05, 2.99, 0.10)[0], 2.99)
        self.assertEqual(target_price(TIERS, 1.15, 2.99, 0.10)[0], 4.15)

    def test_movement_inside_a_tier_is_never_damped(self):
        # The top tier is a markup, so its price tracks the market
        # continuously. Damping here would freeze every expensive card.
        price, _, note = target_price(TIERS, 5.40, 8.00, 0.10)
        self.assertEqual(price, 8.40)
        self.assertIsNone(note)

    def test_a_zero_margin_turns_the_damping_off(self):
        price, _, note = target_price(TIERS, 0.251, 1.99, 0.0)
        self.assertEqual(price, 2.49)
        self.assertIsNone(note)

    def test_a_market_price_no_rule_covers_is_refused(self):
        # A rule set that starts above zero leaves a gap. Pricing from the
        # untiered fallback would list a 10-cent card at 10 cents.
        gapped = [{"min_price": 1.00, "max_price": None,
                   "rule_type": "markup_fixed", "rule_value": 3.00}]
        price, index, _ = target_price(gapped, 0.10, 1.99, 0.10)
        self.assertIsNone(price)
        self.assertIsNone(index)

        verdict = decide_card(
            card(market_price=0.10), rules=gapped, multipliers={},
            margin_fraction=0.10, hold_days=14.0, now=utcnow(),
        )
        self.assertEqual(verdict["verdict"], VERDICT_SKIPPED)
        self.assertIn("priced from nothing", verdict["reason"])


class HoldWindowTests(unittest.TestCase):
    """Up is cheap, down is not. The two directions are not symmetric."""

    def test_a_rise_applies_the_same_day(self):
        decision = decide(card(market_price=0.60, last_known_price=1.99))
        self.assertEqual(decision["verdict"], VERDICT_RAISE)
        self.assertEqual(decision["target_price"], 2.99)

    def test_a_fall_starts_a_hold_instead_of_dropping(self):
        decision = decide(card(market_price=0.10, last_known_price=2.49))
        self.assertEqual(decision["verdict"], VERDICT_HOLD)
        self.assertEqual(decision["target_price"], 1.99)
        self.assertEqual(decision["days_remaining"], 14.0)
        self.assertIsNotNone(decision["hold_since"])

    def test_a_fall_that_has_persisted_is_accepted(self):
        now = utcnow()
        started = format_timestamp(now - timedelta(days=14, hours=1))
        decision = decide(
            card(market_price=0.10, last_known_price=2.49, hold_since=started),
            now=now,
        )
        self.assertEqual(decision["verdict"], VERDICT_DROP)
        self.assertEqual(decision["target_price"], 1.99)
        self.assertTrue(decision["clear_hold"])

    def test_a_fall_one_day_short_is_still_held(self):
        now = utcnow()
        started = format_timestamp(now - timedelta(days=13))
        decision = decide(
            card(market_price=0.10, last_known_price=2.49, hold_since=started),
            now=now,
        )
        self.assertEqual(decision["verdict"], VERDICT_HOLD)
        self.assertEqual(decision["days_remaining"], 1.0)

    def test_a_recovery_clears_the_clock(self):
        # The window measures an unbroken run below the listed price. A day
        # back at the listed price has to reset it, or a card that dipped
        # months ago would be marked down on the strength of that dip.
        now = utcnow()
        started = format_timestamp(now - timedelta(days=10))
        decision = decide(
            card(market_price=0.30, last_known_price=2.49, hold_since=started),
            now=now,
        )
        self.assertEqual(decision["verdict"], VERDICT_UNCHANGED)
        self.assertTrue(decision["clear_hold"])

    def test_an_unreadable_hold_stamp_restarts_the_window(self):
        # Not treated as ancient: that would make a corrupt value look like an
        # expired hold and drop the price immediately.
        decision = decide(
            card(market_price=0.10, last_known_price=2.49,
                 hold_since="not a date")
        )
        self.assertEqual(decision["verdict"], VERDICT_HOLD)
        self.assertEqual(decision["days_remaining"], 14.0)

    def test_a_card_with_no_known_ebay_price_is_skipped(self):
        decision = decide(card(last_known_price=None))
        self.assertEqual(decision["verdict"], VERDICT_SKIPPED)
        self.assertIn("not known", decision["reason"])

    def test_a_card_with_no_market_price_is_skipped(self):
        decision = decide(card(market_price=0))
        self.assertEqual(decision["verdict"], VERDICT_SKIPPED)
        self.assertIn("nothing to price from", decision["reason"])

    def test_the_condition_multiplier_shifts_the_tier(self):
        decision = decide_card(
            card(market_price=0.60, condition="Lightly Played",
                 last_known_price=2.99),
            rules=TIERS, multipliers={"LP": 0.5},
            margin_fraction=0.0, hold_days=0.0, now=utcnow(),
        )
        # $0.60 x 0.5 = $0.30, which is the $2.49 tier.
        self.assertEqual(decision["target_price"], 2.49)


class RunTests(unittest.TestCase):
    """The database and eBay halves: what gets written, and what gets sent."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "reprice.db"))
        self.db.set_pricing_rules(
            [dict(t, sort_order=i) for i, t in enumerate(TIERS)],
            user_id=SHARED_SCOPE,
        )
        self.db.upsert_managed_listing(GROUP_KEY, ebay_parent_id=PARENT)

    def tearDown(self):
        self.temp_dir.cleanup()

    def add(self, manifest_id, market, listed, *, offer_id="9001",
            parent=PARENT, hold_since=None):
        self.db.insert_manifest(
            manifest_id, f"Card {manifest_id}", "Unified Minds", "Near Mint",
            "Holofoil", card_number="045/132",
        )
        self.db.record_market_prices([(manifest_id, market)])
        self.db.upsert_variation(manifest_id, parent, 2,
                                 custom_label=manifest_id,
                                 last_known_price=listed)
        if offer_id:
            self.db.set_variation_offer(manifest_id, offer_id)
        if hold_since:
            self.db.set_variation_hold_since(manifest_id, hold_since)

    def test_only_api_managed_listings_are_eligible(self):
        self.add("ID0001", 0.60, 1.99)
        # A File Exchange listing: linked, priced, but with no managed row and
        # no offer id. The Inventory API cannot see it, so repricing it would
        # address an offer that does not exist.
        self.add("ID0002", 0.60, 1.99, offer_id=None, parent="227511361186")

        planned = plan_reprice(self.db)
        self.assertEqual([d["manifest_id"] for d in planned["decisions"]],
                         ["ID0001"])

    def test_a_price_change_sends_no_quantity(self):
        # The whole safety argument for running this unattended is that it can
        # only touch one field.
        self.add("ID0001", 0.60, 1.99)
        api = FakeEbay()
        result = run_reprice(self.db, api)

        self.assertEqual(result["applied"], 1)
        self.assertEqual(len(api.requests), 1)
        request = api.requests[0]
        self.assertNotIn("shipToLocationAvailability", request)
        self.assertEqual(request["offers"][0]["price"]["value"], "2.99")
        self.assertEqual(request["offers"][0]["offerId"], "9001")
        self.assertNotIn("availableQuantity", request["offers"][0])

    def test_an_applied_price_is_recorded_and_the_hold_cleared(self):
        now = utcnow()
        self.add("ID0001", 0.10, 2.49,
                 hold_since=format_timestamp(now - timedelta(days=20)))
        run_reprice(self.db, FakeEbay(), now=now)

        rows = {c["manifest_id"]: c
                for c in self.db.get_managed_cards_for_repricing()}
        self.assertEqual(rows["ID0001"]["last_known_price"], 1.99)
        self.assertIsNone(rows["ID0001"]["hold_since"])

        history = self.db.get_reprice_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["verdict"], VERDICT_DROP)
        self.assertEqual(history[0]["old_price"], 2.49)
        self.assertEqual(history[0]["new_price"], 1.99)
        self.assertEqual(history[0]["applied"], 1)

    def test_a_hold_is_persisted_and_reported(self):
        self.add("ID0001", 0.10, 2.49)
        result = run_reprice(self.db, FakeEbay())

        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["held"], 1)
        rows = self.db.get_managed_cards_for_repricing()
        self.assertIsNotNone(rows[0]["hold_since"])
        # The yellow flag: a hold is a warning, not an informational note.
        holds = [l for l in result["logs"] if l["level"] == "WARN"]
        self.assertTrue(any("HOLD" in l["message"] for l in holds))
        self.assertEqual(self.db.get_reprice_history()[0]["verdict"],
                         VERDICT_HOLD)

    def test_a_refusal_from_ebay_does_not_record_the_new_price(self):
        # The mirror is only ever written from a confirmed outcome.
        self.add("ID0001", 0.60, 1.99)
        api = FakeEbay(refuse={"ID0001": "Offer not found."})
        result = run_reprice(self.db, api)

        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["failed"], 1)
        rows = self.db.get_managed_cards_for_repricing()
        self.assertEqual(rows[0]["last_known_price"], 1.99)
        self.assertTrue(any(l["level"] == "ERROR" for l in result["logs"]))

    def test_the_proportional_cap_refuses_the_whole_run(self):
        # Bad feed data moves everything at once, so the shape of the change
        # set is itself the signal.
        for index in range(CAP_MINIMUM_CARDS):
            market = 0.60 if index < 10 else 0.10
            self.add(f"ID{index:04d}", market, 1.99, offer_id=f"900{index}")

        api = FakeEbay()
        result = run_reprice(self.db, api)

        self.assertFalse(result["attempted"])
        self.assertEqual(result["applied"], 0)
        self.assertEqual(api.requests, [])
        self.assertTrue(any(l["level"] == "ERROR" and "cap" in l["message"]
                            for l in result["logs"]))
        # Nothing was written, including the hold clocks: a run refused for
        # untrustworthy data must not start a countdown from it.
        self.assertTrue(all(row["hold_since"] is None
                            for row in self.db.get_managed_cards_for_repricing()))

    def test_the_cap_does_not_apply_below_a_meaningful_sample(self):
        # One card out of two is 50% and means nothing.
        self.add("ID0001", 0.60, 1.99, offer_id="9001")
        self.add("ID0002", 0.19, 1.99, offer_id="9002")
        result = run_reprice(self.db, FakeEbay())
        self.assertTrue(result["attempted"])
        self.assertEqual(result["applied"], 1)

    def test_a_preview_sends_nothing(self):
        self.add("ID0001", 0.60, 1.99)
        api = FakeEbay()
        result = run_reprice(self.db, api, dry_run=True)

        self.assertFalse(result["attempted"])
        self.assertEqual(api.requests, [])
        self.assertEqual(result["changes"], 1)
        self.assertEqual(self.db.get_reprice_history(), [])
        self.assertIsNone(self.db.get_managed_cards_for_repricing()[0]["hold_since"])

    def test_a_change_with_no_connected_account_raises(self):
        self.add("ID0001", 0.60, 1.99)
        with self.assertRaises(RepriceError):
            run_reprice(self.db, None)

    def test_a_run_with_nothing_eligible_is_not_reported_as_success(self):
        result = run_reprice(self.db, FakeEbay())
        self.assertFalse(result["attempted"])
        self.assertEqual(result["reason"], "no eligible cards")

    def test_a_price_change_does_not_disturb_the_quantity(self):
        # set_variation_known_price exists for exactly this. upsert_variation
        # would rewrite last_known_qty from whatever the caller passed, and
        # that figure goes stale the moment a card sells -- so a reprice would
        # quietly overwrite eBay's own number with an older one.
        self.add("ID0001", 0.60, 1.99)
        run_reprice(self.db, FakeEbay())

        row = next(v for v in self.db.get_live_variations()
                   if v["manifest_id"] == "ID0001")
        self.assertEqual(row["last_known_qty"], 2, "quantity must be untouched")
        self.assertEqual(row["last_known_price"], 2.99)

    def test_the_settings_drive_the_thresholds(self):
        self.db.set_listing_settings(
            {"price_hold_days": "0", "price_boundary_margin_percent": "0"},
            user_id=SHARED_SCOPE,
        )
        self.add("ID0001", 0.24, 2.49)
        result = run_reprice(self.db, FakeEbay())
        # With both damping mechanisms off, a one-cent crossing drops at once.
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self.db.get_reprice_history()[0]["new_price"], 1.99)

    def test_an_unreadable_setting_falls_back_to_the_shipped_default(self):
        # A blank threshold must not read as "no threshold".
        self.db.set_listing_settings(
            {"price_hold_days": "", "reprice_max_change_percent": "oops"},
            user_id=SHARED_SCOPE,
        )
        config = plan_reprice(self.db)["config"]
        self.assertEqual(config["hold_days"], 14.0)
        self.assertEqual(config["cap_fraction"], 0.25)


if __name__ == "__main__":
    unittest.main()
