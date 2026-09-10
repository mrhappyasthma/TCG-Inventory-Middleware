"""
Tests for draft plans: the staging layer that replaces "generate a CSV and
upload it by hand".

The properties worth protecting here are the ones that decide whether a push
is safe. A plan must be a genuine diff, so an unchanged catalogue produces
nothing; it must treat an unknown eBay value as a change, so a real update is
never suppressed; and it must refuse approval while a card would fail eBay's
validation, because eBay fails the entire variation group for one bad offer
and does it after approval.
"""

import json
import os
import sqlite3
import tempfile
import unittest

from tcg_engine.db import Database, SHARED_SCOPE
from tcg_engine.plans import (
    ACTION_CREATE,
    ACTION_UPDATE,
    ACTION_ZERO_OUT,
    PLAN_APPROVED,
    PLAN_DRAFT,
    STATUS_EXCLUDED,
    PlanError,
    approve_plan,
    build_plan,
    derive_plan_items,
    group_key_for,
    is_single,
    plan_blockers,
    revalidate_item,
    single_group_key,
    variation_group_key,
)

# A settings map with everything an Add needs, so a test that is not about
# validation does not trip over it.
COMPLETE_SETTINGS = {
    "category_id": "183454",
    "seller_postal_code": "94305",
    "default_game": "Pokémon TCG",
    "shipping_profile_name": "Free Shipping Cards",
    "return_profile_name": "No Returns",
    "payment_profile_name": "Immediate Payment",
    "variation_title_template": "{set_name}: Pick Your Card - {condition}",
}


def card(**overrides):
    base = {
        "manifest_id": "ID1001",
        "product_name": "Charizard",
        "set_name": "Base Set",
        "condition": "Near Mint",
        "printing": "Holofoil",
        "quantity": 3,
        "price": 2.50,
        "market_price": 3.00,
        "ebay_parent_id": None,
        "last_known_qty": None,
        "last_known_price": None,
    }
    base.update(overrides)
    return base


class GroupKeyTests(unittest.TestCase):
    def test_grouping_key_is_set_and_condition(self):
        # eBay applies one ConditionID per listing, so a set holding NM and LP
        # has to become two listings.
        self.assertNotEqual(
            variation_group_key("Base Set", "Near Mint"),
            variation_group_key("Base Set", "Lightly Played"),
        )

    def test_a_card_at_the_threshold_becomes_a_single(self):
        key = group_key_for(card(price=5.00), True, 5.00)
        self.assertTrue(is_single(key))

    def test_a_card_below_the_threshold_is_grouped(self):
        key = group_key_for(card(price=4.99), True, 5.00)
        self.assertFalse(is_single(key))
        self.assertEqual(key, variation_group_key("Base Set", "Near Mint"))

    def test_grouping_disabled_makes_everything_a_single(self):
        key = group_key_for(card(price=0.10), False, 5.00)
        self.assertEqual(key, single_group_key("ID1001"))

    def test_an_unpriced_card_is_grouped_rather_than_singled(self):
        # It cannot be compared to the threshold, and a single listing for a
        # card with no price is the worse of the two guesses.
        self.assertFalse(is_single(group_key_for(card(price=None), True, 5.00)))


class DeriveItemsTests(unittest.TestCase):
    def derive(self, cards, **kwargs):
        kwargs.setdefault("group_by_set", True)
        kwargs.setdefault("single_threshold", 5.00)
        return derive_plan_items(cards, COMPLETE_SETTINGS, **kwargs)

    def test_a_new_card_with_stock_becomes_a_create(self):
        items = self.derive([card()])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"], ACTION_CREATE)
        self.assertEqual(items[0]["proposed_qty"], 3)
        self.assertEqual(items[0]["proposed_price"], 2.50)

    def test_a_new_card_with_no_stock_is_not_listed(self):
        # eBay rejects a zero-quantity Add, and offering to list stock that
        # does not exist is how a store oversells.
        self.assertEqual(self.derive([card(quantity=0)]), [])

    def test_an_unchanged_live_card_produces_nothing(self):
        unchanged = card(
            ebay_parent_id="1234567890",
            quantity=3,
            price=2.50,
            last_known_qty=3,
            last_known_price=2.50,
        )
        self.assertEqual(self.derive([unchanged]), [])

    def test_a_changed_quantity_produces_an_update(self):
        items = self.derive(
            [
                card(
                    ebay_parent_id="1234567890",
                    quantity=7,
                    price=2.50,
                    last_known_qty=3,
                    last_known_price=2.50,
                )
            ]
        )
        self.assertEqual(items[0]["action"], ACTION_UPDATE)
        self.assertEqual(items[0]["proposed_qty"], 7)
        self.assertEqual(items[0]["observed_qty"], 3)

    def test_a_changed_price_produces_an_update(self):
        items = self.derive(
            [
                card(
                    ebay_parent_id="1234567890",
                    quantity=3,
                    price=9.99,
                    last_known_qty=3,
                    last_known_price=2.50,
                )
            ]
        )
        self.assertEqual(items[0]["action"], ACTION_UPDATE)
        self.assertEqual(items[0]["proposed_price"], 9.99)

    def test_an_unknown_last_known_price_counts_as_changed(self):
        # Nothing may be suppressed against a value we never learned: until a
        # sync has run, eBay's price is unknown, and suppressing would drop a
        # real change silently.
        items = self.derive(
            [
                card(
                    ebay_parent_id="1234567890",
                    quantity=3,
                    price=2.50,
                    last_known_qty=3,
                    last_known_price=None,
                )
            ]
        )
        self.assertEqual(len(items), 1)

    def test_a_sold_out_live_card_is_zeroed_not_ended(self):
        items = self.derive(
            [
                card(
                    ebay_parent_id="1234567890",
                    quantity=0,
                    last_known_qty=4,
                )
            ]
        )
        self.assertEqual(items[0]["action"], ACTION_ZERO_OUT)
        self.assertEqual(items[0]["proposed_qty"], 0)

    def test_a_card_already_at_zero_on_ebay_is_not_re_zeroed(self):
        items = self.derive(
            [card(ebay_parent_id="1234567890", quantity=0, last_known_qty=0)]
        )
        self.assertEqual(items, [])

    def test_sub_cent_price_drift_is_not_a_change(self):
        # Floats accumulate error; without an epsilon a rebuilt plan would
        # show a permanent phantom price change on every card.
        items = self.derive(
            [
                card(
                    ebay_parent_id="1234567890",
                    quantity=3,
                    price=2.50,
                    last_known_qty=3,
                    last_known_price=2.5000001,
                )
            ]
        )
        self.assertEqual(items, [])

    def test_an_unpriced_card_is_flagged_rather_than_dropped(self):
        items = self.derive([card(price=None, market_price=0)])
        self.assertEqual(len(items), 1)
        problems = json.loads(items[0]["validation"])
        self.assertTrue(any("no price" in p for p in problems))

    def test_a_zero_out_is_not_blocked_by_missing_add_fields(self):
        # Otherwise the operation used to react to a problem is itself
        # blocked by that problem.
        items = derive_plan_items(
            [
                card(
                    ebay_parent_id="1234567890",
                    quantity=0,
                    price=None,
                    last_known_qty=4,
                )
            ],
            {},
        )
        self.assertEqual(items[0]["action"], ACTION_ZERO_OUT)
        self.assertIsNone(items[0]["validation"])

    def test_a_create_missing_seller_settings_is_flagged(self):
        items = derive_plan_items([card()], {})
        problems = json.loads(items[0]["validation"])
        joined = " ".join(problems)
        self.assertIn("category", joined)
        self.assertIn("postal code", joined)
        self.assertIn("Game", joined)


class PlanPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "plans.db"))
        self.db.set_listing_settings(COMPLETE_SETTINGS, user_id=SHARED_SCOPE)

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_card(self, manifest_id, name, quantity=3, price=2.50, **kwargs):
        self.db.insert_manifest(
            manifest_id,
            name,
            kwargs.get("set_name", "Base Set"),
            kwargs.get("condition", "Near Mint"),
            kwargs.get("printing", "Holofoil"),
        )
        self.db.set_manifest_quantity(manifest_id, quantity)
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE manifest SET price = ? WHERE manifest_id = ?",
                (price, manifest_id),
            )
            conn.commit()

    def test_build_plan_stores_items_and_reports_a_summary(self):
        self.add_card("ID1001", "Charizard")
        self.add_card("ID1002", "Blastoise")
        summary = build_plan(self.db, user_id=1, source="batch", source_ref="abc")

        self.assertEqual(summary["item_count"], 2)
        self.assertEqual(summary["actions"][ACTION_CREATE], 2)

        plan = self.db.get_plan(summary["plan_id"])
        self.assertEqual(plan["status"], PLAN_DRAFT)
        self.assertEqual(plan["source"], "batch")
        self.assertEqual(plan["source_ref"], "abc")
        self.assertEqual(len(self.db.get_plan_items(summary["plan_id"])), 2)

    def test_only_one_draft_may_be_open_per_user(self):
        self.add_card("ID1001", "Charizard")
        first = build_plan(self.db, user_id=1)["plan_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.create_plan(1)
        # Two users may each hold one.
        self.assertIsNotNone(self.db.create_plan(2))
        self.assertEqual(self.db.get_open_plan_id(1), first)

    def test_rebuilding_replaces_the_open_draft(self):
        self.add_card("ID1001", "Charizard")
        first = build_plan(self.db, user_id=1)["plan_id"]
        second = build_plan(self.db, user_id=1)["plan_id"]
        self.assertNotEqual(first, second)
        self.assertIsNone(self.db.get_plan(first))

    def test_rebuilding_can_be_refused_instead(self):
        self.add_card("ID1001", "Charizard")
        build_plan(self.db, user_id=1)
        with self.assertRaises(PlanError):
            build_plan(self.db, user_id=1, replace_open=False)

    def test_deleting_a_plan_takes_its_items(self):
        self.add_card("ID1001", "Charizard")
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        self.db.delete_plan(plan_id)
        self.assertEqual(self.db.get_plan_items(plan_id), [])

    def test_groups_are_summarised_one_row_per_listing(self):
        self.add_card("ID1001", "Charizard", price=1.00)
        self.add_card("ID1002", "Blastoise", price=1.00)
        self.add_card("ID1003", "Mewtwo", price=99.00)
        plan_id = build_plan(self.db, user_id=1)["plan_id"]

        groups = self.db.get_plan_groups(plan_id)
        # Two cheap cards share a variation listing; the expensive one is its
        # own single.
        self.assertEqual(len(groups), 2)
        by_count = sorted(g["item_count"] for g in groups)
        self.assertEqual(by_count, [1, 2])

    def test_editing_an_item_is_restricted_to_known_columns(self):
        self.add_card("ID1001", "Charizard")
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        item_id = self.db.get_plan_items(plan_id)[0]["id"]

        self.assertTrue(self.db.update_plan_item(item_id, proposed_qty=11))
        self.assertEqual(self.db.get_plan_item(item_id)["proposed_qty"], 11)
        # An unknown key is ignored rather than interpolated into SQL.
        self.assertFalse(self.db.update_plan_item(item_id, manifest_id="ID9999"))
        self.assertEqual(self.db.get_plan_item(item_id)["manifest_id"], "ID1001")

    def test_regrouping_moves_a_card_between_listings(self):
        self.add_card("ID1001", "Charizard", price=1.00)
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        item_id = self.db.get_plan_items(plan_id)[0]["id"]

        self.db.update_plan_item(item_id, group_key=single_group_key("ID1001"))
        groups = self.db.get_plan_groups(plan_id)
        self.assertTrue(is_single(groups[0]["group_key"]))


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "plans.db"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_card(self, manifest_id, name, quantity=3, price=2.50,
                 with_specifics=True):
        self.db.insert_manifest(
            manifest_id, name, "Base Set", "Near Mint", "Holofoil"
        )
        self.db.set_manifest_quantity(manifest_id, quantity)
        if with_specifics:
            # What cataloguing an eBay-flavoured export leaves behind. Most of
            # the specifics eBay marks required exist nowhere else, so a card
            # without them cannot be listed -- see the test below.
            self.db.set_manifest_ebay_fields(manifest_id, {
                "item_specifics": {
                    "C:Game": "Pokémon TCG",
                    "C:Card Type": "Pokémon",
                    "C:Manufacturer": "The Pokémon Company",
                    "C:Graded": "No",
                },
            })
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE manifest SET price = ? WHERE manifest_id = ?",
                (price, manifest_id),
            )
            conn.commit()

    def complete_settings(self):
        self.db.set_listing_settings(COMPLETE_SETTINGS, user_id=SHARED_SCOPE)

    def test_a_clean_plan_can_be_approved(self):
        self.complete_settings()
        self.add_card("ID1001", "Charizard")
        plan_id = build_plan(self.db, user_id=1)["plan_id"]

        result = approve_plan(self.db, plan_id, approved_by=1)
        self.assertEqual(result["approved_items"], 1)
        plan = self.db.get_plan(plan_id)
        self.assertEqual(plan["status"], PLAN_APPROVED)
        # The approval time is stamped by the database rather than the caller,
        # so an approved plan can never lack the record of who authorised it.
        self.assertIsNotNone(plan["approved_at"])
        self.assertEqual(plan["approved_by"], 1)

    def test_a_plan_with_a_broken_card_cannot_be_approved(self):
        # A realistic misconfiguration: the seeded defaults are complete, so
        # blank one required field. An Add with no item location is rejected
        # by eBay with error 10009 -- after approval, if we do not catch it.
        self.complete_settings()
        self.db.set_listing_settings(
            {"seller_postal_code": ""}, user_id=SHARED_SCOPE
        )
        self.add_card("ID1001", "Charizard")
        plan_id = build_plan(self.db, user_id=1)["plan_id"]

        blockers = plan_blockers(self.db, plan_id)
        self.assertEqual(len(blockers), 1)
        self.assertTrue(blockers[0]["problems"])
        with self.assertRaises(PlanError):
            approve_plan(self.db, plan_id, approved_by=1)

    def test_a_card_with_no_export_specifics_cannot_be_approved(self):
        # eBay marks around twenty item specifics required on a card listing,
        # and most of them -- Card Type, Manufacturer, Graded, Card Size,
        # Character, Stage, the Country fields -- come only from the eBay
        # export. A card catalogued without it builds an Add file that looks
        # plausible and is missing fields eBay demands, so the blocker names
        # the re-upload rather than letting the file out.
        self.complete_settings()
        self.add_card("ID1001", "Charizard", with_specifics=False)
        plan_id = build_plan(self.db, user_id=1)["plan_id"]

        blockers = plan_blockers(self.db, plan_id)
        self.assertEqual(len(blockers), 1)
        problems = [p["problem"] for p in blockers[0]["problems"]]
        self.assertTrue(
            any("item specifics" in p and "export_eBay_" in p for p in problems),
            problems,
        )
        with self.assertRaises(PlanError):
            approve_plan(self.db, plan_id, approved_by=1)

    def test_excluding_the_broken_card_unblocks_the_rest(self):
        self.complete_settings()
        self.add_card("ID1001", "Charizard", price=1.00)
        self.add_card("ID1002", "Blastoise", price=None)
        plan_id = build_plan(self.db, user_id=1)["plan_id"]

        with self.assertRaises(PlanError):
            approve_plan(self.db, plan_id, approved_by=1)

        broken = [
            item
            for item in self.db.get_plan_items(plan_id)
            if item["manifest_id"] == "ID1002"
        ][0]
        self.db.update_plan_item(broken["id"], status=STATUS_EXCLUDED)

        self.assertEqual(plan_blockers(self.db, plan_id), [])
        self.assertEqual(
            approve_plan(self.db, plan_id, approved_by=1)["approved_items"], 1
        )

    def test_an_all_excluded_plan_is_refused(self):
        self.complete_settings()
        self.add_card("ID1001", "Charizard")
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        for item in self.db.get_plan_items(plan_id):
            self.db.update_plan_item(item["id"], status=STATUS_EXCLUDED)

        with self.assertRaises(PlanError) as caught:
            approve_plan(self.db, plan_id, approved_by=1)
        self.assertIn("excluded", str(caught.exception))

    def test_a_plan_cannot_be_approved_twice(self):
        self.complete_settings()
        self.add_card("ID1001", "Charizard")
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        approve_plan(self.db, plan_id, approved_by=1)
        with self.assertRaises(PlanError):
            approve_plan(self.db, plan_id, approved_by=1)

    def test_an_overlong_title_blocks_the_group_not_a_card(self):
        self.db.set_listing_settings(
            dict(
                COMPLETE_SETTINGS,
                variation_title_template="{set_name}: " + "x" * 90,
            ),
            user_id=SHARED_SCOPE,
        )
        self.add_card("ID1001", "Charizard", price=1.00)
        plan_id = build_plan(self.db, user_id=1)["plan_id"]

        blockers = plan_blockers(self.db, plan_id)
        self.assertEqual(len(blockers), 1)
        problems = blockers[0]["problems"]
        self.assertTrue(any(p["manifest_id"] is None for p in problems))
        self.assertTrue(any("80-character" in p["problem"] for p in problems))

    def test_revalidating_after_a_price_fix_clears_the_blocker(self):
        self.complete_settings()
        self.add_card("ID1001", "Charizard", price=None)
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        item = self.db.get_plan_items(plan_id)[0]
        self.assertTrue(json.loads(item["validation"]))

        self.db.update_plan_item(item["id"], proposed_price=3.25)
        self.assertEqual(revalidate_item(self.db, item["id"]), [])
        self.assertEqual(plan_blockers(self.db, plan_id), [])

    def test_blockers_for_a_missing_plan_are_an_error(self):
        with self.assertRaises(PlanError):
            plan_blockers(self.db, 999)


class TcgcsvUrlTests(unittest.TestCase):
    """
    The URLs the price feed builds.

    ``last-updated.txt`` lives at the site root while everything else sits
    under ``/tcgplayer``. Expressing that as a relative "../last-updated.txt"
    did not work: urllib sends "..", it is not normalised, and TCGCSV answered
    404 -- so the gate that makes a same-day refresh cost one request instead
    of one per set was failing on every run, silently turning the cheap path
    into the expensive one.
    """

    def built_urls(self, call):
        import tcg_engine.pricing_feed as pf

        seen = []
        real_get, real_sleep = pf._http_get, pf.time.sleep
        pf._http_get = lambda url: (seen.append(url), "2026-01-01T00:00:00+0000")[1]
        pf.time.sleep = lambda _s: None
        try:
            call(pf)
        finally:
            pf._http_get, pf.time.sleep = real_get, real_sleep
        return seen

    def test_the_snapshot_timestamp_is_read_from_the_site_root(self):
        urls = self.built_urls(lambda pf: pf.fetch_last_updated())
        self.assertEqual(urls, ["https://tcgcsv.com/last-updated.txt"])

    def test_catalogue_paths_keep_the_tcgplayer_prefix(self):
        urls = self.built_urls(lambda pf: pf.default_fetcher("3/groups"))
        self.assertEqual(urls, ["https://tcgcsv.com/tcgplayer/3/groups"])

    def test_no_built_url_contains_a_relative_segment(self):
        # The specific defect: a ".." that never resolves because it is sent
        # rather than normalised.
        urls = self.built_urls(
            lambda pf: (
                pf.fetch_last_updated(),
                pf.default_fetcher("3/groups"),
                pf.default_fetcher("3/2464/prices"),
            )
        )
        for url in urls:
            self.assertNotIn("..", url)


if __name__ == "__main__":
    unittest.main()
