"""
Tests for pushing an approved plan to eBay.

Driven by a fake standing in for the eBay library, which is why the push takes
its API surface as an argument. The behaviours under test are the ones that
decide whether the store and our mirror still agree afterwards, and every one
of them is a way a push can *appear* to succeed:

* eBay answers HTTP 200 to a bulk call and reports failure per SKU inside the
  body, so one card failing must not mark its neighbours pushed;
* a listing this API cannot see is invisible to it, so pushing its cards
  would create a duplicate rather than update it;
* a card already pushed must not be pushed again, or a second listing appears;
* the mirror is written from what eBay confirmed, never from what we intended.
"""

import os
import tempfile
import unittest

from tcg_engine.db import Database, SHARED_SCOPE
from tcg_engine.plans import (
    ACTION_END,
    ACTION_REMOVE,
    ACTION_UPDATE,
    STATUS_DEFERRED,
    STATUS_FAILED,
    STATUS_PUSHED,
    approve_plan,
    build_plan,
)
from tcg_engine.push import (
    _apply_price_quantity,
    is_single,
    PushError,
    inventory_group_key,
    push_plan,
    refresh_listing,
)

COVER = "https://cdn.example.com/set-logo.png"

COMPLETE_SETTINGS = {
    "category_id": "183454",
    "seller_postal_code": "94305",
    "default_game": "Pokémon TCG",
    "shipping_profile_name": "Free Shipping Cards",
    "return_profile_name": "No Returns",
    "payment_profile_name": "Immediate Payment",
    "variation_title_template": "{set_name}: Pick Your Card - {condition}",
}


class FakeEbay:
    """
    Stands in for the eBay Inventory API.

    Records every call so a test can assert on ordering -- items before
    offers, offers before the group -- and can be told to fail a specific SKU
    the way eBay does: inside a 200 response, per record.
    """

    def __init__(self, fail_skus=None, offer_error_skus=None):
        self.fail_skus = dict(fail_skus or {})
        self.offer_error_skus = set(offer_error_skus or ())
        self.calls = []
        self.items = {}
        self.offers = {}
        self.groups = {}
        # Every price/quantity request as sent, so a test can assert which
        # destination a quantity was addressed to rather than only that a
        # call happened.
        self.price_quantity_requests = []
        # group key -> the listing id it was published at, so republishing
        # returns the same id the way eBay does.
        self.group_listings = {}
        self.withdrawn = []
        self.next_offer = 1000
        self.next_listing = 220000
        # SKUs eBay already has a published listing for, as it would report
        # after a publish whose reply never reached us.
        self.published_skus = {}

    # -- the surface push_plan expects ---------------------------------

    def upsert_items(self, items):
        self.calls.append(("upsert_items", [i["sku"] for i in items]))
        rows = []
        for entry in items:
            sku = entry["sku"]
            if sku in self.fail_skus:
                rows.append({
                    "sku": sku, "statusCode": 400,
                    "errors": [{"errorId": 25002,
                                "longMessage": self.fail_skus[sku]}],
                })
                continue
            self.items[sku] = entry
            rows.append({"sku": sku, "statusCode": 200})
        return rows

    def create_offer(self, payload):
        sku = payload["sku"]
        self.calls.append(("create_offer", sku))
        if sku in self.offer_error_skus:
            raise RuntimeError(f"offer refused for {sku}")
        self.next_offer += 1
        offer_id = str(self.next_offer)
        self.offers[offer_id] = payload
        return offer_id

    def create_offers(self, payloads):
        self.calls.append(("create_offers", [p["sku"] for p in payloads]))
        rows = []
        for payload in payloads:
            sku = payload["sku"]
            if sku in self.offer_error_skus:
                rows.append({
                    "sku": sku, "statusCode": 400,
                    "errors": [{"longMessage": f"offer refused for {sku}"}],
                })
                continue
            self.next_offer += 1
            offer_id = str(self.next_offer)
            self.offers[offer_id] = payload
            rows.append({"sku": sku, "statusCode": 200, "offerId": offer_id})
        return rows

    def published_listing_id(self, sku):
        self.calls.append(("published_listing_id", sku))
        return self.published_skus.get(sku, "")

    def offer_ids_for(self, sku):
        self.calls.append(("offer_ids_for", sku))
        return [
            oid for oid, payload in self.offers.items()
            if payload.get("sku") == sku
        ]

    def update_price_quantity(self, requests):
        """
        Apply an update the way eBay does, field by field.

        Faithful rather than a bare acknowledgement, because the bug this
        guards against is a request eBay *accepts* while the listing goes on
        selling the old quantity. A fake that only answered 200 asserted our
        intent and proved nothing: quantity was being set on the inventory
        item alone, and a published listing serves the quantity held on its
        offer. So both destinations are recorded separately here, and
        ``offer_quantity`` reads back the one a buyer sees.
        """
        self.calls.append(("update_price_quantity", [r["sku"] for r in requests]))
        self.price_quantity_requests.extend(requests)
        rows = []
        for request in requests:
            sku = request["sku"]
            if sku in self.fail_skus:
                rows.append({
                    "sku": sku, "statusCode": 400,
                    "errors": [{"longMessage": self.fail_skus[sku]}],
                })
                continue
            ship_to = request.get("shipToLocationAvailability")
            if ship_to is not None and sku in self.items:
                self.items[sku].setdefault("availability", {})[
                    "shipToLocationAvailability"
                ] = dict(ship_to)
            for entry in request.get("offers") or []:
                offer = self.offers.get(str(entry.get("offerId")))
                if offer is None:
                    continue
                if "availableQuantity" in entry:
                    offer["availableQuantity"] = entry["availableQuantity"]
                if "price" in entry:
                    offer["price"] = entry["price"]
            rows.append({"sku": sku, "statusCode": 200})
        return rows

    def offer_quantity(self, sku):
        """
        What ``getOffers`` would report for this SKU, or None if it has no
        offer. This is the number the live listing sells from, and the one
        ``scripts/inspect_listing.py`` prints as ``eBay qty``.
        """
        for payload in self.offers.values():
            if payload.get("sku") == sku:
                return payload.get("availableQuantity")
        return None

    def upsert_group(self, group_key, payload):
        self.calls.append(("upsert_group", group_key))
        self.groups[group_key] = payload

    def get_group(self, group_key):
        """
        What eBay would report holding, which is what it last accepted.

        Faithful rather than fixed, so the read-back that confirms a
        cover photo is actually exercised. Without this the check lands
        in its 'could not ask eBay' branch and proves nothing.
        """
        self.calls.append(("get_group", group_key))
        return self.groups.get(group_key, {})

    def publish_group(self, group_key):
        """
        Publish a group, returning the listing id it lives at.

        Republishing is not a new listing: eBay returns the *same* id,
        because the group is already associated with it. This was observed
        directly when a 123-card listing whose offers had all been ended was
        republished and came back as the id it already had. A fake that
        invented a fresh id each time would have made a second listing look
        like the normal outcome, and the code that checks for exactly that
        would have looked broken.
        """
        self.calls.append(("publish_group", group_key))
        if group_key not in self.group_listings:
            self.next_listing += 1
            self.group_listings[group_key] = str(self.next_listing)
        return self.group_listings[group_key]

    def publish_offer(self, offer_id):
        self.calls.append(("publish_offer", offer_id))
        self.next_listing += 1
        return str(self.next_listing)

    def withdraw_offer(self, offer_id):
        self.calls.append(("withdraw_offer", offer_id))
        self.withdrawn.append(offer_id)

    def failures(self, rows):
        out = []
        for row in rows:
            code = row.get("statusCode")
            if isinstance(code, int) and not 200 <= code < 300:
                message = "; ".join(
                    str(e.get("longMessage") or e.get("message"))
                    for e in row.get("errors") or []
                )
                out.append((row.get("sku"), message or "rejected"))
        return out

    def kinds(self):
        return [name for name, _ in self.calls]


class PushTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "push.db"))
        self.db.set_listing_settings(COMPLETE_SETTINGS, user_id=SHARED_SCOPE)

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_card(self, manifest_id, name, card_number, price=1.99, qty=2):
        self.db.insert_manifest(
            manifest_id, name, "Base Set", "Near Mint", "Holofoil",
            card_number=card_number, language="English",
            cdn_image=f"https://cdn.example.com/{manifest_id}.jpg",
        )
        self.db.set_manifest_quantity(manifest_id, qty)
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE manifest SET price = ? WHERE manifest_id = ?",
                (price, manifest_id),
            )
            conn.commit()
        self.db.set_manifest_ebay_fields(manifest_id, {
            "item_specifics": {"C:Card Type": "Pokémon", "C:Graded": "No"},
            "condition_descriptor": "Near mint or better - (ID: 400010)",
        })

    def approved_plan(self):
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        approve_plan(self.db, plan_id, approved_by=1)
        return plan_id

    def items_by_sku(self, plan_id):
        return {
            (row.get("custom_label") or row["manifest_id"]): row
            for row in self.db.get_plan_items(plan_id)
        }


class CreateListingTests(PushTestCase):
    def test_a_new_variation_listing_is_created_in_order(self):
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "002/102")
        plan_id = self.approved_plan()
        api = FakeEbay()

        result = push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)

        self.assertEqual(result["pushed"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["listings_created"], 1)
        # Items, then offers, then the group, then publish. The group is what
        # makes the listing visible, so anything it references must exist by
        # the time it is written.
        # Items, then offers, then one question, then the group, then
        # publish. The question -- does eBay already have a listing for these
        # cards? -- is asked because a publish whose reply was lost looks
        # locally identical to one that never happened, and guessing wrong
        # creates a second live listing. One SKU answers for the whole group.
        self.assertEqual(
            api.kinds(),
            ["upsert_items", "create_offers", "published_listing_id",
             "upsert_group", "publish_group"],
        )

        # The listing is now ours to manage through the API, and the eBay item
        # number is on record -- without which the next push could not tell an
        # update from a create.
        managed = self.db.get_managed_listing("Base Set|Near Mint")
        self.assertEqual(managed["ebay_parent_id"], "220001")
        self.assertEqual(
            managed["inventory_item_group_key"],
            inventory_group_key("Base Set|Near Mint"),
        )
        self.assertEqual(managed["managed_by"], "api")

        # And the mirror carries what eBay confirmed.
        live = {row["manifest_id"]: row for row in self.db.get_live_variations()}
        self.assertEqual(live["ID1001"]["ebay_parent_id"], "220001")
        self.assertEqual(live["ID1001"]["last_known_qty"], 2)

    def test_the_card_condition_descriptor_is_sent_as_a_numeric_id(self):
        # File Exchange accepts "Near mint or better - (ID: 400010)". The API
        # rejects prose and wants the bare id, so the stored CSV rendering
        # cannot simply be forwarded.
        self.add_card("ID1001", "Charizard", "004/102")
        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        item = api.items["ID1001"]
        self.assertEqual(item["conditionDescriptors"],
                         [{"name": "40001", "values": ["400010"]}])
        self.assertEqual(item["condition"], "USED_VERY_GOOD")

    def test_every_bulk_record_carries_its_locale(self):
        """
        Required per record, not just per request.

        Without it eBay answers 400 with "Valid SKU and locale information are
        required for all the InventoryItems in the request", which reads as
        though the SKU is at fault when the SKU is present and correct. The
        Content-Language header the transport sends describes the request; this
        field describes the record, and both are needed.
        """
        self.add_card("ID1001", "Charizard", "004/102")
        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)
        self.assertEqual(api.items["ID1001"]["locale"], "en_US")

    def test_aspects_lose_the_csv_column_prefix(self):
        # "C:Game" is File Exchange's column naming. eBay's aspects are
        # unprefixed names mapping to lists.
        self.add_card("ID1001", "Charizard", "004/102")
        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        aspects = api.items["ID1001"]["product"]["aspects"]
        self.assertEqual(aspects["Game"], ["Pokémon TCG"])
        self.assertNotIn("C:Game", aspects)

    def test_a_group_states_only_the_aspects_every_card_shares(self):
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "002/102")
        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        group = api.groups[inventory_group_key("Base Set|Near Mint")]
        self.assertEqual(group["aspects"]["Set"], ["Base Set"])
        # Card Name differs between the two cards, so the variation axis
        # expresses it rather than the listing.
        self.assertNotIn("Card Name", group["aspects"])
        self.assertEqual(sorted(group["variantSKUs"]), ["ID1001", "ID1002"])


class PartialFailureTests(PushTestCase):
    def test_one_card_failing_inside_a_200_does_not_push_the_others(self):
        """
        The failure mode the whole module is arranged around.

        eBay reports per-SKU outcomes inside a 200. Read as a single verdict,
        every card is marked pushed while some kept their old state, and the
        mirror then disagrees with the store with nothing to explain why.
        """
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "002/102")
        plan_id = self.approved_plan()
        api = FakeEbay(fail_skus={"ID1002": "Invalid aspect: Card Size"})

        result = push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)

        self.assertEqual(result["pushed"], 1)
        self.assertEqual(result["failed"], 1)
        items = self.items_by_sku(plan_id)
        self.assertEqual(items["ID1001"]["status"], STATUS_PUSHED)
        self.assertEqual(items["ID1002"]["status"], STATUS_FAILED)
        # eBay's own words are kept against the card, because they are the
        # only thing that says what to fix.
        self.assertIn("Card Size", items["ID1002"]["validation"])
        # The failed card is not in the listing, and its mirror row was never
        # written -- claiming eBay holds it would be a lie.
        group = api.groups[inventory_group_key("Base Set|Near Mint")]
        self.assertEqual(group["variantSKUs"], ["ID1001"])
        self.assertEqual(
            [r["manifest_id"] for r in self.db.get_live_variations()], ["ID1001"]
        )

    def test_an_offer_that_cannot_be_created_fails_only_its_own_card(self):
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "002/102")
        plan_id = self.approved_plan()
        api = FakeEbay(offer_error_skus={"ID1001"})

        result = push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)

        self.assertEqual((result["pushed"], result["failed"]), (1, 1))
        items = self.items_by_sku(plan_id)
        self.assertEqual(items["ID1001"]["status"], STATUS_FAILED)
        self.assertEqual(items["ID1002"]["status"], STATUS_PUSHED)

    def test_a_partial_push_leaves_the_plan_partial(self):
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "002/102")
        plan_id = self.approved_plan()
        push_plan(self.db, FakeEbay(fail_skus={"ID1002": "nope"}), plan_id,
                  user_id=SHARED_SCOPE)
        self.assertEqual(self.db.get_plan(plan_id)["status"], "partial")

    def test_a_clean_push_leaves_the_plan_pushed(self):
        self.add_card("ID1001", "Charizard", "004/102")
        plan_id = self.approved_plan()
        push_plan(self.db, FakeEbay(), plan_id, user_id=SHARED_SCOPE)
        self.assertEqual(self.db.get_plan(plan_id)["status"], "pushed")


class LegacyListingTests(PushTestCase):
    def test_a_listing_this_api_cannot_see_is_skipped(self):
        """
        The guard that stops a push duplicating a live listing.

        eBay has the listing, but the Inventory API cannot see it, so writing
        a group for these SKUs would publish a *second* listing beside the one
        already selling. Skipping is the only safe answer.

        Every listing has now been migrated, so this should never fire in
        practice. It stays because the cost of being wrong is a duplicate live
        listing, and because a listing created outside this application would
        look exactly like this.
        """
        self.add_card("ID1001", "Charizard", "004/102", qty=5)
        # Module B has linked this card to a listing we did not create.
        self.db.upsert_variation("ID1001", "227511361186", 2,
                                 custom_label="ID1001", last_known_price=1.99)
        plan_id = self.approved_plan()
        api = FakeEbay()

        result = push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)

        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["pushed"], 0)
        self.assertEqual(api.calls, [], "nothing should have been sent to eBay")
        self.assertEqual(
            self.items_by_sku(plan_id)["ID1001"]["status"], STATUS_DEFERRED
        )
        self.assertTrue(
            any("would create a duplicate" in entry["message"]
                for entry in result["logs"])
        )

    def test_a_listing_we_created_is_updated_rather_than_recreated(self):
        self.add_card("ID1001", "Charizard", "004/102", qty=2)
        first = self.approved_plan()
        api = FakeEbay()
        push_plan(self.db, api, first, user_id=SHARED_SCOPE)
        listing_id = self.db.get_managed_listing("Base Set|Near Mint")["ebay_parent_id"]

        # Stock changes, so a second plan proposes an update.
        self.db.set_manifest_quantity("ID1001", 7)
        second = self.approved_plan()
        api2 = FakeEbay()
        result = push_plan(self.db, api2, second, user_id=SHARED_SCOPE)

        self.assertEqual(result["listings_created"], 0)
        self.assertEqual(result["listings_updated"], 1)
        self.assertNotIn("publish_group", api2.kinds())
        # The existing offer is reused: a second offer for the same SKU is an
        # error, and the offer id is the only way to change a price.
        self.assertNotIn("create_offers", api2.kinds())
        self.assertIn("update_price_quantity", api2.kinds())
        live = {r["manifest_id"]: r for r in self.db.get_live_variations()}
        self.assertEqual(live["ID1001"]["last_known_qty"], 7)
        self.assertEqual(live["ID1001"]["ebay_parent_id"], listing_id)


class ResumeTests(PushTestCase):
    def test_an_already_pushed_card_is_not_pushed_twice(self):
        # A push that died halfway must be resumable. Repeating a create is
        # how a duplicate listing appears.
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "002/102")
        plan_id = self.approved_plan()
        push_plan(self.db, FakeEbay(), plan_id, user_id=SHARED_SCOPE)

        api = FakeEbay()
        result = push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)
        self.assertEqual(result["pushed"], 0)
        self.assertEqual(api.calls, [])

    def test_a_push_that_does_nothing_says_so_and_says_why(self):
        """
        Doing nothing must never report success.

        "0 pushed, 0 failed" logged as SUCCESS is indistinguishable from a
        push that worked, and it sent someone to eBay's active listings
        hunting for a listing that had never been attempted. The reason has to
        come with it, because the three causes -- everything already pushed,
        everything excluded, an empty plan -- need different responses.
        """
        self.add_card("ID1001", "Charizard", "004/102")
        plan_id = self.approved_plan()
        push_plan(self.db, FakeEbay(), plan_id, user_id=SHARED_SCOPE)

        api = FakeEbay()
        result = push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)
        self.assertFalse(result["attempted"])
        self.assertIn("already pushed", result["reason"])
        self.assertEqual(api.calls, [])
        self.assertTrue(
            all(entry["level"] != "SUCCESS" for entry in result["logs"]),
            result["logs"],
        )

    def test_an_empty_plan_reports_that_rather_than_succeeding(self):
        self.add_card("ID1001", "Charizard", "004/102")
        plan_id = self.approved_plan()
        # The cards a plan referred to can leave the catalogue between
        # approval and push, which empties the plan without emptying the
        # table it is stored in.
        self.db.delete_manifest("ID1001")
        result = push_plan(self.db, FakeEbay(), plan_id, user_id=SHARED_SCOPE)
        self.assertFalse(result["attempted"])
        self.assertIn("no items", result["reason"])

    def test_a_retry_after_a_failure_completes_the_listing(self):
        """
        The bug that cost a real push, reproduced.

        The first attempt failed at the bulk inventory-item call, so every card
        was marked ``failed``. The second attempt -- with the cause fixed --
        got the items up and created the offers, and then dropped all five
        cards because they *still said* ``failed`` from last time: this module
        reads the item's own status to decide whether a card is still in play,
        and a stale verdict makes that check answer yes before anything has
        been attempted.

        The result was the worst possible shape: offers created on eBay, no
        group written, nothing published, nothing marked, and a run reporting
        "0 pushed, 0 failed".
        """
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "002/102")
        plan_id = self.approved_plan()

        first = push_plan(self.db, FakeEbay(fail_skus={
            "ID1001": "locale missing", "ID1002": "locale missing",
        }), plan_id, user_id=SHARED_SCOPE)
        self.assertEqual(first["failed"], 2)

        # The cause is fixed; nothing else changed.
        api = FakeEbay()
        result = push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)

        self.assertEqual(result["pushed"], 2, result["logs"])
        self.assertEqual(result["listings_created"], 1)
        self.assertIn("publish_group", api.kinds())
        self.assertIsNotNone(
            self.db.get_managed_listing("Base Set|Near Mint")["ebay_parent_id"]
        )
        # And the stale reasons are gone, so the drafts page stops showing a
        # blocker for a problem that no longer exists.
        for row in self.db.get_plan_items(plan_id):
            self.assertEqual(row["status"], STATUS_PUSHED)
            self.assertIsNone(row["validation"])

    def test_an_error_on_a_retry_is_recorded_rather_than_discarded(self):
        # The same stale-status hole had a second mouth: the group-level
        # handler refused to overwrite a verdict that already said failed, so
        # a retry's own error was thrown away and the run reported nothing.
        self.add_card("ID1001", "Charizard", "004/102")
        plan_id = self.approved_plan()
        push_plan(self.db, FakeEbay(fail_skus={"ID1001": "first reason"}),
                  plan_id, user_id=SHARED_SCOPE)

        class Exploding(FakeEbay):
            def upsert_group(self, group_key, payload):
                raise RuntimeError("second reason")

        result = push_plan(self.db, Exploding(), plan_id, user_id=SHARED_SCOPE)
        self.assertEqual(result["failed"], 1)
        item = self.db.get_plan_items(plan_id)[0]
        self.assertIn("second reason", item["validation"])
        self.assertNotIn("first reason", item["validation"])

    def test_a_draft_cannot_be_pushed(self):
        self.add_card("ID1001", "Charizard", "004/102")
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        with self.assertRaises(PushError):
            push_plan(self.db, FakeEbay(), plan_id, user_id=SHARED_SCOPE)


class ZeroOutTests(PushTestCase):
    def test_a_zero_out_does_not_touch_the_price(self):
        # Out of stock is not a sale. Sending a price here would reprice a
        # card on its way off the shelf.
        self.add_card("ID1001", "Charizard", "004/102", qty=3)
        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        self.db.set_manifest_quantity("ID1001", 0)
        plan_id = self.approved_plan()
        items = self.db.get_plan_items(plan_id)
        self.assertTrue(items)
        api2 = FakeEbay()
        push_plan(self.db, api2, plan_id, user_id=SHARED_SCOPE)

        sent = [c for c in api2.calls if c[0] == "update_price_quantity"]
        self.assertTrue(sent)
        request = api2.calls[[c[0] for c in api2.calls].index(
            "update_price_quantity")]
        self.assertEqual(request[1], ["ID1001"])


class ScopedPushTests(PushTestCase):
    def test_one_listing_can_be_pushed_without_the_others(self):
        """
        What makes a first push testable.

        A create cannot be undone by pressing the button again, so the sane
        way to begin is one small listing, checked in Seller Hub, before
        several hundred cards go live in a single call. The rest of the plan
        must be left exactly as it was -- still approved, still pushable.
        """
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "002/102")
        # A second listing, in a different set.
        self.db.insert_manifest("ID2001", "Pikachu", "Jungle", "Near Mint",
                                "Holofoil", card_number="060/064")
        self.db.set_manifest_quantity("ID2001", 1)
        with self.db.get_connection() as conn:
            conn.execute("UPDATE manifest SET price = 1.99 WHERE manifest_id = ?",
                         ("ID2001",))
            conn.commit()
        self.db.set_manifest_ebay_fields("ID2001", {
            "item_specifics": {"C:Graded": "No"},
        })

        plan_id = self.approved_plan()
        api = FakeEbay()
        result = push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE,
                           group_keys=["Jungle|Near Mint"])

        self.assertEqual(result["pushed"], 1)
        self.assertEqual(result["listings_created"], 1)
        # Only the requested listing was touched.
        self.assertEqual(
            [key for _, key in api.calls if key == "Base Set|Near Mint"], []
        )
        self.assertIsNone(self.db.get_managed_listing("Base Set|Near Mint"))
        # The rest of the plan is untouched and still pushable, and the plan
        # reads as partial rather than finished.
        items = self.items_by_sku(plan_id)
        self.assertEqual(items["ID1001"]["status"], "pending")
        self.assertEqual(self.db.get_plan(plan_id)["status"], "partial")

        # Pushing the remainder finishes it.
        rest = push_plan(self.db, FakeEbay(), plan_id, user_id=SHARED_SCOPE,
                         group_keys=["Base Set|Near Mint"])
        self.assertEqual(rest["pushed"], 2)
        self.assertEqual(self.db.get_plan(plan_id)["status"], "pushed")

    def test_pushing_the_same_listing_twice_creates_nothing(self):
        """
        The question anyone will ask after their first successful push.

        Pressing Push again on a listing that went up must not produce a
        second one, and must not read like an error either -- "this plan has
        no listing called X" was the old answer, which is alarming and wrong.
        """
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "002/102")
        # A second listing, so the plan still has work left and this exercises
        # the per-listing answer rather than the whole-plan one.
        self.db.insert_manifest("ID2001", "Pikachu", "Jungle", "Near Mint",
                                "Holofoil", card_number="060/064")
        self.db.set_manifest_quantity("ID2001", 1)
        self.db.set_manifest_ebay_fields("ID2001", {
            "item_specifics": {"C:Graded": "No"},
        })
        with self.db.get_connection() as conn:
            conn.execute("UPDATE manifest SET price = 1.99 WHERE manifest_id = ?",
                         ("ID2001",))
            conn.commit()
        plan_id = self.approved_plan()
        first = push_plan(self.db, FakeEbay(), plan_id, user_id=SHARED_SCOPE,
                          group_keys=["Base Set|Near Mint"])
        listing_id = self.db.get_managed_listing(
            "Base Set|Near Mint"
        )["ebay_parent_id"]

        api = FakeEbay()
        again = push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE,
                          group_keys=["Base Set|Near Mint"])

        self.assertFalse(again["attempted"])
        self.assertIn("already been pushed", again["reason"])
        self.assertIn("Refresh", again["reason"])
        self.assertEqual(api.calls, [], "nothing may be sent to eBay")
        # One listing, still the same one.
        self.assertEqual(
            self.db.get_managed_listing("Base Set|Near Mint")["ebay_parent_id"],
            listing_id,
        )
        self.assertEqual(first["listings_created"], 1)

    def test_a_push_interrupted_after_publishing_resumes_as_an_update(self):
        """
        Why an interrupted push cannot duplicate a listing.

        The eBay item number is recorded the instant eBay issues it, so a
        retry sees the listing as already published and takes the update path
        -- no second publish, no second listing.
        """
        self.add_card("ID1001", "Charizard", "004/102")
        plan_id = self.approved_plan()

        # eBay publishes, and the reply never arrives.
        class DiesAfterPublish(FakeEbay):
            def publish_group(self, group_key):
                listing = super().publish_group(group_key)
                raise RuntimeError(f"connection lost after publishing {listing}")

        push_plan(self.db, DiesAfterPublish(), plan_id, user_id=SHARED_SCOPE)
        # Locally this is indistinguishable from never having published.
        self.assertIsNone(
            self.db.get_managed_listing("Base Set|Near Mint")["ebay_parent_id"]
        )

        # But eBay knows, and is asked before anything is created again.
        api = FakeEbay()
        api.published_skus = {"ID1001": "227999111222"}
        result = push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)
        self.assertEqual(result["pushed"], 1)
        self.assertNotIn("publish_group", api.kinds())
        self.assertEqual(result["listings_created"], 0)
        self.assertEqual(
            self.db.get_managed_listing("Base Set|Near Mint")["ebay_parent_id"],
            "227999111222",
            "the listing eBay already had must be adopted, not duplicated",
        )

    def test_an_unknown_listing_is_refused_rather_than_pushing_nothing(self):
        # Silently pushing nothing would look like success and leave the user
        # believing a listing went live.
        self.add_card("ID1001", "Charizard", "004/102")
        plan_id = self.approved_plan()
        with self.assertRaises(PushError):
            push_plan(self.db, FakeEbay(), plan_id, user_id=SHARED_SCOPE,
                      group_keys=["No Such Set|NM"])


class BulkOfferTests(PushTestCase):
    def test_offers_are_created_in_batches_of_twenty_five(self):
        """
        One call per card is what made a hundred-card listing take minutes.

        The offer loop was 94% of the requests; eBay takes 25 at a time.
        """
        for index in range(30):
            self.add_card(f"ID{2000 + index}", f"Card {index}",
                          f"{index:03d}/102")
        api = FakeEbay()
        result = push_plan(self.db, api, self.approved_plan(),
                           user_id=SHARED_SCOPE)

        self.assertEqual(result["pushed"], 30)
        batches = [args for name, args in api.calls if name == "create_offers"]
        self.assertEqual([len(b) for b in batches], [25, 5])
        # Every card still has its own offer id recorded, which is the only
        # handle that can change its price later.
        offers = {
            row["manifest_id"]: row["offer_id"]
            for row in self.db.get_live_variations()
        }
        self.assertEqual(len(offers), 30)
        self.assertTrue(all(offers.values()))

    def test_one_rejected_offer_fails_only_its_own_card(self):
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "002/102")
        plan_id = self.approved_plan()
        api = FakeEbay(offer_error_skus={"ID1001"})

        result = push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)

        self.assertEqual((result["pushed"], result["failed"]), (1, 1))
        items = self.items_by_sku(plan_id)
        self.assertEqual(items["ID1001"]["status"], STATUS_FAILED)
        self.assertEqual(items["ID1002"]["status"], STATUS_PUSHED)
        group = list(api.groups.values())[0]
        self.assertEqual(group["variantSKUs"], ["ID1002"])

    def test_an_offer_id_missing_from_the_response_is_read_back(self):
        """
        The one thing a bulk create can lose.

        An offer id is the only handle that can change a card's price. A row
        eBay reports as created but returns no id for would otherwise leave
        the card listed with a handle nobody holds -- and the next push would
        try to create a second offer for that SKU, which eBay refuses.
        """
        self.add_card("ID1001", "Charizard", "004/102")

        class Forgetful(FakeEbay):
            def create_offers(self, payloads):
                rows = super().create_offers(payloads)
                for row in rows:
                    row.pop("offerId", None)
                return rows

        api = Forgetful()
        result = push_plan(self.db, api, self.approved_plan(),
                           user_id=SHARED_SCOPE)

        self.assertEqual(result["pushed"], 1)
        self.assertIn("offer_ids_for", api.kinds())
        recorded = self.db.get_live_variations()[0]["offer_id"]
        self.assertTrue(recorded)

    def test_a_card_ebay_says_nothing_about_is_not_a_success(self):
        # Silence is not consent: if the response omits a SKU, whether its
        # offer exists is unknown, and listing it would be a guess.
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "002/102")
        plan_id = self.approved_plan()

        class Silent(FakeEbay):
            def create_offers(self, payloads):
                rows = super().create_offers(payloads)
                return [r for r in rows if r["sku"] != "ID1002"]

            def offer_ids_for(self, sku):
                return []

        result = push_plan(self.db, Silent(), plan_id, user_id=SHARED_SCOPE)
        items = self.items_by_sku(plan_id)
        self.assertEqual(items["ID1002"]["status"], STATUS_FAILED)
        self.assertIn("did not mention", items["ID1002"]["validation"])
        self.assertEqual(result["pushed"], 1)


class VariationOrderTests(PushTestCase):
    def test_the_dropdown_is_ordered_by_card_number(self):
        """
        This order *is* what a buyer sees in the variation dropdown.

        Left in plan order it comes out sorted by manifest id -- the order the
        cards happened to be catalogued in, which is meaningless to someone
        looking for 045/132. The CSV path has always sorted here; the API path
        did not, and the first listings went up scrambled.
        """
        # Catalogued in an order that has nothing to do with card numbers.
        self.add_card("ID1001", "Zapdos", "045/132")
        self.add_card("ID1002", "Bulbasaur", "001/132")
        self.add_card("ID1003", "Mew", "151/132")
        self.add_card("ID1004", "Ivysaur", "002/132")

        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        group = list(api.groups.values())[0]
        self.assertEqual(
            group["variesBy"]["specifications"][0]["values"],
            ["Bulbasaur (001/132)", "Ivysaur (002/132)",
             "Zapdos (045/132)", "Mew (151/132)"],
        )
        # The SKU list has to agree with it, or the dropdown labels and the
        # cards behind them come apart.
        self.assertEqual(
            group["variantSKUs"], ["ID1002", "ID1004", "ID1001", "ID1003"]
        )

    def test_a_refresh_fixes_the_order_on_a_live_listing(self):
        # Which is what makes the listings already up repairable rather than
        # having to be ended and recreated.
        self.add_card("ID1001", "Zapdos", "045/132")
        self.add_card("ID1002", "Bulbasaur", "001/132")
        plan_id = self.approved_plan()
        push_plan(self.db, FakeEbay(), plan_id, user_id=SHARED_SCOPE)
        listing_id = self.db.get_managed_listing(
            "Base Set|Near Mint"
        )["ebay_parent_id"]

        api = FakeEbay()
        refresh_listing(self.db, api, listing_id, user_id=SHARED_SCOPE)
        group = list(api.groups.values())[0]
        self.assertEqual(
            group["variesBy"]["specifications"][0]["values"],
            ["Bulbasaur (001/132)", "Zapdos (045/132)"],
        )

    def test_a_card_with_no_usable_number_sorts_last(self):
        # Rather than being scattered through the list.
        self.add_card("ID1001", "Zapdos", "045/132")
        self.add_card("ID1002", "Promo Card", "")
        self.add_card("ID1003", "Bulbasaur", "001/132")

        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)
        group = list(api.groups.values())[0]
        self.assertEqual(group["variantSKUs"][-1], "ID1002")


class ImageTests(PushTestCase):
    def test_only_the_cards_own_front_scan_is_sent(self):
        """
        The first real listing went live with the card backs on it.

        Every card in a set has a near-identical back, so sending them puts
        what look like duplicate photos on a listing -- and a buyer choosing
        between a hundred variations gains nothing from more pictures of the
        same card back. The stock photo is a second view of the same front,
        which reads as another duplicate.
        """
        self.add_card("ID1001", "Charizard", "004/102")
        self.db.set_manifest_ebay_fields("ID1001", {
            "cdn_back_image": "https://cdn.example.com/back.jpg",
            "stock_image": "https://cdn.example.com/stock.jpg",
        })
        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        images = api.items["ID1001"]["product"]["imageUrls"]
        self.assertEqual(images, ["https://cdn.example.com/ID1001.jpg"])


    def test_a_card_with_no_scan_is_listed_with_the_catalogue_photo(self):
        """
        A listing with no photograph is a listing nobody clicks.

        The stock photo is excluded when we have a scan, because it is a
        second view of the same front. When we do not, it is the only
        picture there is, and a generic photo of the right card beats an
        empty frame.
        """
        self.add_card("ID1001", "Charizard", "004/102")
        with self.db.get_connection() as conn:
            conn.execute(
                """
                UPDATE manifest
                   SET cdn_image = NULL,
                       stock_image = 'https://stock.example.com/charizard.jpg'
                 WHERE manifest_id = 'ID1001'
                """
            )
            conn.commit()

        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        images = api.items["ID1001"]["product"]["imageUrls"]
        self.assertEqual(
            images, ["https://stock.example.com/charizard.jpg"]
        )

    def test_the_group_cover_falls_back_to_a_catalogue_photo_too(self):
        """
        The listing's own picture is picked from the cards in it, so a
        group whose cards are all unscanned would otherwise have no cover
        at all -- which is the one image a browsing buyer actually sees.
        """
        self.add_card("ID1001", "Charizard", "004/102")
        with self.db.get_connection() as conn:
            conn.execute(
                """
                UPDATE manifest
                   SET cdn_image = NULL,
                       stock_image = 'https://stock.example.com/charizard.jpg'
                """
            )
            conn.commit()

        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        group = api.groups[next(iter(api.groups))]
        self.assertEqual(
            group["imageUrls"], ["https://stock.example.com/charizard.jpg"]
        )


class RefreshPutsTheListingBackOnSaleTests(PushTestCase):
    """
    Refresh has to publish the group, not just rewrite it.

    Withdrawing an offer keeps the offer and leaves its status at
    UNPUBLISHED, and naming its SKU in the group again does not revive it.
    A live 123-card listing was found in exactly that state -- group complete,
    every offer holding the right quantity and pictures, every offer
    unpublished, one card visible to a buyer. Refresh rebuilt the group and
    changed nothing anyone could see, because nothing in the app published
    anything once a listing was already published. Recovery took a script.

    The state is reachable without a bug: ending a card withdraws its offer,
    and restocking it later produces an update, which never publishes.
    """

    def live_listing(self):
        self.add_card("ID1001", "Charizard", "004/102", qty=3)
        self.add_card("ID1002", "Blastoise", "002/102", qty=3)
        push_plan(self.db, FakeEbay(), self.approved_plan(),
                  user_id=SHARED_SCOPE)
        return self.db.get_managed_listing("Base Set|Near Mint")[
            "ebay_parent_id"
        ]

    def test_a_refresh_publishes_the_group(self):
        listing_id = self.live_listing()
        api = FakeEbay()
        result = refresh_listing(
            self.db, api, listing_id, user_id=SHARED_SCOPE
        )

        self.assertIn("publish_group", api.kinds())
        self.assertIs(result["republished"], True)
        # And it happens after the group write: publishing a group eBay has
        # not been given yet would publish the old contents.
        kinds = api.kinds()
        self.assertLess(
            kinds.index("upsert_group"), kinds.index("publish_group"),
            "the group must be written before it is published",
        )

    def test_republishing_does_not_move_the_listing(self):
        """
        The same group publishes to the same listing, so the mirror stands.
        """
        listing_id = self.live_listing()
        api = FakeEbay()
        result = refresh_listing(
            self.db, api, listing_id, user_id=SHARED_SCOPE
        )

        self.assertIs(result["republished"], True)
        self.assertEqual(
            self.db.get_managed_listing("Base Set|Near Mint")
                   ["ebay_parent_id"],
            listing_id,
            "republishing the same group must not repoint the mirror",
        )
        self.assertEqual(
            [entry["level"] for entry in result["logs"]
             if entry["level"] == "ERROR"], [],
            "a normal republish is not an error",
        )
        self.assertTrue(
            any(entry["level"] == "SUCCESS" for entry in result["logs"])
        )

    def test_a_refused_publish_is_not_reported_as_a_success(self):
        """
        Publishing is all-or-nothing, so one invalid card blocks the group.

        The items and the group were still written, so this is not a failed
        refresh -- it is a refresh that could not put the result back on
        sale, and saying SUCCESS over it is the thing that hid the original
        damage for days.
        """
        listing_id = self.live_listing()

        class RefusingEbay(FakeEbay):
            def publish_group(self, group_key):
                self.calls.append(("publish_group", group_key))
                raise RuntimeError("25002: offer for ID1002 has no price")

        api = RefusingEbay()
        result = refresh_listing(
            self.db, api, listing_id, user_id=SHARED_SCOPE
        )

        self.assertIs(result["republished"], False)
        self.assertEqual(result["failed"], 0, "the re-send itself worked")
        levels = [entry["level"] for entry in result["logs"]]
        self.assertIn("ERROR", levels)
        self.assertNotIn(
            "SUCCESS", levels,
            "a listing left off sale must not be summarised as a success",
        )
        # eBay's own message names the card, so it must survive into the log.
        self.assertTrue(
            any("ID1002" in entry["message"] for entry in result["logs"]),
            f"the blocking card should be named: {result['logs']}",
        )

    def test_a_publish_landing_on_another_listing_is_an_error(self):
        """
        A different listing id means eBay made a second listing.

        The mirror follows eBay, because leaving it on the old id would send
        every later write to a listing eBay no longer associates with these
        offers -- but this is still an error, not a success, because there are
        now two listings for one group and only the owner can decide which
        one to end.
        """
        listing_id = self.live_listing()

        class MovingEbay(FakeEbay):
            def publish_group(self, group_key):
                self.calls.append(("publish_group", group_key))
                return "999888777666"

        api = MovingEbay()
        result = refresh_listing(
            self.db, api, listing_id, user_id=SHARED_SCOPE
        )

        levels = [entry["level"] for entry in result["logs"]]
        self.assertIn("ERROR", levels)
        self.assertNotIn("SUCCESS", levels)
        self.assertTrue(
            any("999888777666" in entry["message"]
                for entry in result["logs"]),
            "the new listing id has to be in the log to be actionable",
        )
        self.assertEqual(
            self.db.get_managed_listing("Base Set|Near Mint")
                   ["ebay_parent_id"],
            "999888777666",
            "the mirror must follow eBay to where the group now lives",
        )

    def test_a_restocked_card_can_be_put_back_on_sale_by_a_refresh(self):
        """
        The cycle that reaches this state with nothing going wrong.

        Ending a card withdraws its offer. Restocking it later produces an
        update against the offer that still exists, and an update never
        publishes -- so the card sits in the group, with stock, off sale.
        Refresh is the way back, and this is the regression that matters,
        because it needs no incident to happen.
        """
        listing_id = self.live_listing()

        # End one card, which withdraws its offer rather than deleting it.
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        self.db.add_plan_items(plan_id, [{
            "manifest_id": "ID1002",
            "group_key": "Base Set|Near Mint",
            "action": ACTION_END,
            "proposed_qty": 0,
            "proposed_price": None,
        }])
        approve_plan(self.db, plan_id, approved_by=1)
        ending = FakeEbay()
        push_plan(self.db, ending, plan_id, user_id=SHARED_SCOPE)
        self.assertTrue(
            ending.withdrawn, "ending a card should withdraw its offer"
        )

        # Restocked later. The push updates the surviving offer and rewrites
        # the group, and publishes nothing -- which is the gap.
        self.db.set_manifest_quantity("ID1002", 4)
        restock = FakeEbay()
        push_plan(self.db, restock, self.approved_plan(),
                  user_id=SHARED_SCOPE)
        self.assertNotIn(
            "publish_group", restock.kinds(),
            "a push to a live listing does not publish, which is why "
            "Refresh has to",
        )

        api = FakeEbay()
        result = refresh_listing(
            self.db, api, listing_id, user_id=SHARED_SCOPE
        )
        self.assertIs(result["republished"], True)
        self.assertIn("publish_group", api.kinds())


class RefreshTests(PushTestCase):
    def test_a_live_listing_can_be_repaired_without_moving_stock_or_price(self):
        """
        The route to a picture or specifics correction on a live listing.

        A plan is a diff of quantities and prices, so a picture change
        produces no diff and can never reach eBay through the drafts page.
        This does, and by design it cannot reprice or restock: quantity is
        re-sent as what eBay is already known to hold and no price is sent.
        """
        self.add_card("ID1001", "Charizard", "004/102", qty=3)
        self.add_card("ID1002", "Blastoise", "002/102", qty=3)
        plan_id = self.approved_plan()
        push_plan(self.db, FakeEbay(), plan_id, user_id=SHARED_SCOPE)
        listing_id = self.db.get_managed_listing(
            "Base Set|Near Mint"
        )["ebay_parent_id"]

        # The catalogue has moved on since the push; a repair must ignore it.
        self.db.set_manifest_quantity("ID1001", 99)

        api = FakeEbay()
        result = refresh_listing(self.db, api, listing_id, user_id=SHARED_SCOPE)

        self.assertEqual(result["refreshed"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertIn("upsert_items", api.kinds())
        self.assertIn("upsert_group", api.kinds())
        # Nothing that could change the stock or the money.
        self.assertNotIn("update_price_quantity", api.kinds())
        # Publishing, on the other hand, is now expected, and this
        # expectation was the opposite until a live listing proved it wrong.
        # It used to assert publish_group was *not* called, on the reasoning
        # that a repair must not change what is on sale. But publishing does
        # not change what is on sale: it puts the offers the group already
        # names back on sale holding the quantities and prices they already
        # hold. What the old expectation actually protected was a listing
        # stuck off sale, with no route back through the app.
        self.assertIn("publish_group", api.kinds())
        self.assertEqual(
            api.items["ID1001"]["availability"]
               ["shipToLocationAvailability"]["quantity"],
            3,
            "a repair must re-send the quantity eBay already holds",
        )

    def test_a_pushed_cover_is_recorded_against_the_listing(self):
        """
        The staged cover has to cross from the plan to the listing.

        It lives in listing_plan_group, keyed by plan. Nothing carried it
        across, so after the push the eBay Listings tab showed the listing as
        having no cover -- and the refresh below, finding none recorded,
        replaced the cover on eBay with the first card's photo.
        """
        self.add_card("ID1001", "Charizard", "004/102")
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        self.db.set_plan_group_cover(plan_id, "Base Set|Near Mint", COVER)
        approve_plan(self.db, plan_id, approved_by=1)
        push_plan(self.db, FakeEbay(), plan_id, user_id=SHARED_SCOPE)

        listing_id = self.db.get_managed_listing(
            "Base Set|Near Mint"
        )["ebay_parent_id"]
        self.assertEqual(self.db.get_listing_cover_image(listing_id), COVER)

        # And a later repair keeps it rather than overwriting it.
        api = FakeEbay()
        refresh_listing(self.db, api, listing_id, user_id=SHARED_SCOPE)
        group = list(api.groups.values())[0]
        self.assertEqual(group["imageUrls"], [COVER])

    def test_a_refresh_keeps_a_cover_only_ebay_knows_about(self):
        # Writing a group is a full replace, so a refresh that guesses does
        # not leave the cover alone -- it overwrites it. Ask eBay first.
        self.add_card("ID1001", "Charizard", "004/102")
        plan_id = self.approved_plan()
        push_plan(self.db, FakeEbay(), plan_id, user_id=SHARED_SCOPE)
        listing_id = self.db.get_managed_listing(
            "Base Set|Near Mint"
        )["ebay_parent_id"]
        self.assertEqual(self.db.get_listing_cover_image(listing_id), "")

        class WithCover(FakeEbay):
            def get_group(self, group_key):
                return {"imageUrls": ["https://cdn.example.com/seller-set.jpg"]}

        api = WithCover()
        refresh_listing(self.db, api, listing_id, user_id=SHARED_SCOPE)
        group = list(api.groups.values())[0]
        self.assertEqual(
            group["imageUrls"], ["https://cdn.example.com/seller-set.jpg"]
        )
        # Learned, so the next refresh need not ask again.
        self.assertEqual(
            self.db.get_listing_cover_image(listing_id),
            "https://cdn.example.com/seller-set.jpg",
        )

    def test_an_unmanaged_listing_cannot_be_refreshed(self):
        # It is invisible to this API; pushing at it would create a duplicate.
        self.add_card("ID1001", "Charizard", "004/102")
        self.db.upsert_variation("ID1001", "227511361186", 2,
                                 custom_label="ID1001")
        with self.assertRaises(PushError) as caught:
            refresh_listing(self.db, FakeEbay(), "227511361186",
                            user_id=SHARED_SCOPE)
        self.assertIn("not managed through this API", str(caught.exception))


class PartialPushKeepsTheWholeListingTests(PushTestCase):
    """
    A plan touching one card must not take the rest of the listing off sale.

    Writing an inventory item group is a full replace: a SKU absent from
    variantSKUs is a card removed from the live listing, immediately, with no
    staged state to inspect. `_group_payload` says so in its own docstring and
    `refresh_listing` was built around it -- but the push was not, and built
    the group from the plan's items alone.

    The cost, in production: a quantity change approved on one card replaced a
    35-card listing with a 1-card listing. Nothing failed and the log said
    success. Nothing in this suite caught it, which is the more useful fact.
    """

    def live_three_card_listing(self):
        """Three cards, pushed and published, as the starting state."""
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "009/102")
        self.add_card("ID1003", "Venusaur", "015/102")
        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)
        group = list(api.groups.values())[0]
        self.assertEqual(
            len(group["variantSKUs"]), 3, "fixture should list three cards"
        )
        return api

    def test_changing_one_card_keeps_the_other_variations(self):
        self.live_three_card_listing()

        # One card's stock changes, so the rebuilt plan holds one item.
        self.db.set_manifest_quantity("ID1002", 7)
        plan_id = self.approved_plan()
        items = self.db.get_plan_items(plan_id)
        self.assertEqual(
            [row["manifest_id"] for row in items], ["ID1002"],
            "the plan should be a diff of one card",
        )

        api = FakeEbay()
        push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)

        group = list(api.groups.values())[0]
        self.assertEqual(
            sorted(group["variantSKUs"]), ["ID1001", "ID1002", "ID1003"],
            "the cards the plan did not mention must survive it",
        )
        # And the dropdown still offers all three, in card-number order.
        options = group["variesBy"]["specifications"][0]["values"]
        self.assertEqual(len(options), 3)
        self.assertTrue(
            options[0].endswith("(004/102)"), f"unsorted: {options}"
        )

    def test_the_listing_level_aspects_are_not_narrowed_to_one_card(self):
        """
        Aspects are computed from the cards in the group, so deriving them
        from a one-card plan would rewrite the whole listing's specifics from
        a sample of one.
        """
        self.live_three_card_listing()
        self.db.set_manifest_quantity("ID1002", 7)

        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)
        group = list(api.groups.values())[0]
        # Whatever the aspects are, they must have been derived from three
        # cards rather than one -- the option list is the visible proxy.
        self.assertEqual(
            len(group["variesBy"]["specifications"][0]["values"]), 3
        )

    def test_a_removed_card_is_the_one_thing_that_does_come_off(self):
        """
        The mechanism is not disabled, only made deliberate: a card the plan
        removes is meant to leave the listing.
        """
        self.live_three_card_listing()
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        items = {
            row["manifest_id"]: row for row in self.db.get_plan_items(plan_id)
        }
        if "ID1003" not in items:
            # Nothing to change for it, so stage a removal directly.
            self.db.add_plan_items(plan_id, [{
                "manifest_id": "ID1003",
                "group_key": "Base Set|Near Mint",
                "action": ACTION_REMOVE,
                "proposed_qty": 0,
                "proposed_price": None,
            }])
        approve_plan(self.db, plan_id, approved_by=1)

        api = FakeEbay()
        push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)
        group = list(api.groups.values())[0]
        self.assertNotIn(
            "ID1003", group["variantSKUs"],
            "a card the plan removes should leave the listing",
        )
        self.assertIn("ID1001", group["variantSKUs"])
        self.assertIn("ID1002", group["variantSKUs"])

    def test_a_brand_new_listing_sends_only_its_own_cards(self):
        """
        There is nothing to preserve before the listing exists, so a create
        must not pick up unrelated cards.
        """
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "009/102")
        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)
        group = list(api.groups.values())[0]
        self.assertEqual(sorted(group["variantSKUs"]), ["ID1001", "ID1002"])


class AddingToALiveListingTests(PushTestCase):
    """
    A card added to a listing that already exists has to be put on sale.

    Naming a SKU in ``variantSKUs`` does not publish its offer -- an offer's
    status is its own, and a new one starts UNPUBLISHED. eBay then hides the
    variation, so the listing goes on showing the cards it already had.

    Seen in production: five cards failed a 152-card push with an eBay system
    error, the draft was rebuilt, the retry put them in the group and marked
    them pushed, and the live listing still showed 147 variations. Nothing
    failed and the log said success. `refresh_listing` had been given this
    publish after the same thing happened there; the push had not.
    """

    def live_listing(self):
        """A published two-card listing, as the starting state."""
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "009/102")
        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)
        self.assertIn("publish_group", api.kinds())
        return api

    def test_a_card_added_later_is_published(self):
        api = self.live_listing()
        api.calls.clear()

        self.add_card("ID1003", "Venusaur", "015/102")
        result = push_plan(
            self.db, api, self.approved_plan(), user_id=SHARED_SCOPE
        )

        self.assertEqual(result["pushed"], 1)
        self.assertEqual(result["failed"], 0)
        # No second listing: the group is republished, which returns the id
        # it already has.
        self.assertEqual(result["listings_created"], 0)
        self.assertIn(
            "publish_group", api.kinds(),
            "a new variation is invisible until its offer is published",
        )
        group = list(api.groups.values())[0]
        self.assertEqual(len(group["variantSKUs"]), 3)

    def test_an_update_alone_does_not_republish(self):
        """
        Narrow on purpose. Publishing is all-or-nothing, so doing it on every
        push would let one invalid card start failing pushes that work today.
        A quantity change creates no offer and needs no publish.
        """
        api = self.live_listing()
        api.calls.clear()

        self.db.set_manifest_quantity("ID1002", 9)
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        self.assertNotIn("publish_group", api.kinds())
        self.assertIn("update_price_quantity", api.kinds())

    def test_a_refused_publish_fails_the_cards_rather_than_claiming_them(self):
        """
        The card is in the listing but nobody can buy it, so recording it as
        pushed would put stock in the mirror that does not exist at eBay --
        and the next draft would suppress the very change that is missing.
        """
        api = self.live_listing()

        class RefusesPublish(FakeEbay):
            def publish_group(self, group_key):
                self.calls.append(("publish_group", group_key))
                raise RuntimeError("25002: another card in this group is invalid")

        retry = RefusesPublish()
        retry.groups = api.groups
        retry.group_listings = api.group_listings
        self.add_card("ID1003", "Venusaur", "015/102")
        plan_id = self.approved_plan()
        result = push_plan(self.db, retry, plan_id, user_id=SHARED_SCOPE)

        self.assertEqual(result["pushed"], 0)
        self.assertEqual(result["failed"], 1)
        item = next(
            row for row in self.db.get_plan_items(plan_id)
            if row["manifest_id"] == "ID1003"
        )
        self.assertEqual(item["status"], STATUS_FAILED)
        self.assertIn("not on sale", item["validation"])
        self.assertIn("Refresh", item["validation"])
        # And the mirror does not claim eBay is holding it.
        live = {r["manifest_id"] for r in self.db.get_live_variations()}
        self.assertNotIn("ID1003", live)

    def test_a_publish_landing_elsewhere_is_reported(self):
        # The same check the refresh makes: two listings for one group is
        # worse than an unpublished offer, and the mirror has to follow eBay.
        api = self.live_listing()

        class MovesTheListing(FakeEbay):
            def publish_group(self, group_key):
                self.calls.append(("publish_group", group_key))
                return "999999999"

        moved = MovesTheListing()
        moved.groups = api.groups
        self.add_card("ID1003", "Venusaur", "015/102")
        result = push_plan(
            self.db, moved, self.approved_plan(), user_id=SHARED_SCOPE
        )

        joined = " ".join(e["message"] for e in result["logs"])
        self.assertIn("999999999", joined)
        self.assertIn("two listings", joined)
        self.assertEqual(
            self.db.get_managed_listing("Base Set|Near Mint")["ebay_parent_id"],
            "999999999",
            "the mirror follows eBay, or every later write goes to the wrong "
            "listing",
        )


class QuantityReachesTheOfferTests(PushTestCase):
    """
    A quantity change has to arrive where eBay serves it from: the offer.

    Setting only the inventory item's ``shipToLocationAvailability`` fails in
    the worst available way. eBay accepts the request, answers 200 for every
    SKU, and the live listing goes on selling the previous quantity -- so the
    push logs a clean success, the mirror records the new number as eBay's,
    and the next draft is empty because our two figures now agree with each
    other and not with eBay.

    Observed on a live 64-card listing: eight quantity changes reported
    "8 card(s) pushed, 0 failed", and ``getOffers`` afterwards still reported
    all eight of the old numbers. The 56 cards nobody had touched agreed only
    because their offers still carried what they were created with.
    ``scripts/inspect_listing.py`` prints that field as ``eBay qty``, and its
    own module docstring already said quantity lives on the offer.
    """

    def live_listing(self):
        """Two cards at two copies each, pushed and published."""
        self.add_card("ID1001", "Charizard", "004/102", qty=2)
        self.add_card("ID1002", "Blastoise", "009/102", qty=2)
        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)
        return api

    def test_a_changed_quantity_reaches_the_offer(self):
        api = self.live_listing()
        self.assertEqual(api.offer_quantity("ID1002"), 2)

        self.db.set_manifest_quantity("ID1002", 7)
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        self.assertEqual(
            api.offer_quantity("ID1002"), 7,
            "the offer is the quantity a buyer sees; an inventory-item-only "
            "update is accepted by eBay and changes nothing",
        )
        # And the card the plan never mentioned keeps what it had.
        self.assertEqual(api.offer_quantity("ID1001"), 2)

    def test_both_destinations_travel_in_one_request(self):
        """
        Both, not either. The item-level figure is what a Refresh re-sends
        and what ``getInventoryItem`` reports, so letting the two drift would
        leave a later repair pushing a stale quantity back onto the listing.
        """
        api = self.live_listing()
        self.db.set_manifest_quantity("ID1002", 7)
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        sent = [
            r for r in api.price_quantity_requests if r["sku"] == "ID1002"
        ]
        self.assertEqual(len(sent), 1, "one call, not two")
        self.assertEqual(sent[0]["shipToLocationAvailability"]["quantity"], 7)
        self.assertEqual(sent[0]["offers"][0]["availableQuantity"], 7)

    def test_a_zero_out_sends_the_quantity_but_still_no_price(self):
        api = self.live_listing()
        self.db.set_manifest_quantity("ID1002", 0)
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        self.assertEqual(api.offer_quantity("ID1002"), 0)
        sent = [
            r for r in api.price_quantity_requests if r["sku"] == "ID1002"
        ]
        self.assertNotIn(
            "price", sent[0]["offers"][0],
            "out of stock is not a sale: a price on this row would reprice a "
            "card on its way off the shelf",
        )

    def test_a_card_with_no_offer_is_deferred_rather_than_sent(self):
        """
        Without an offer id there is no way to move what the listing sells,
        so the request must not be sent at all. Sending the item half alone
        is the silent no-op this class exists for, and it would be counted a
        success.

        Reached directly: ``_push_group`` creates a missing offer before this
        runs, so the guard is defence against that order changing.
        """
        api = self.live_listing()
        self.db.set_manifest_quantity("ID1002", 7)
        plan_id = self.approved_plan()
        item = next(
            row for row in self.db.get_plan_items(plan_id)
            if row["manifest_id"] == "ID1002"
        )
        item = dict(item, offer_id=None)

        counts = {"pushed": 0, "failed": 0, "deferred": 0}
        logs = []
        _apply_price_quantity(
            self.db, api, [item], counts,
            lambda level, message: logs.append((level, message)),
        )

        self.assertEqual(counts["deferred"], 1)
        self.assertEqual(api.price_quantity_requests, [])
        self.assertEqual(item["status"], STATUS_DEFERRED)
        persisted = {
            row["manifest_id"]: row["status"]
            for row in self.db.get_plan_items(plan_id)
        }
        self.assertEqual(persisted["ID1002"], STATUS_DEFERRED)
        self.assertTrue(logs, "a card that could not be sent must say so")
        self.assertEqual(logs[0][0], "WARN")
        self.assertIn("offer", logs[0][1])


class PushKeepsTheListingsCoverTests(PushTestCase):
    """
    A push must not replace the cover photo of a listing it is updating.

    Writing the inventory item group is a full replace, so the cover is
    decided on every push whether or not anybody asked for one. With no cover
    staged on the draft, the push fell straight through to the first card's
    scan -- so approving eight quantity changes replaced the gallery image of
    a live 64-card listing, silently, on a run whose log said success.

    ``refresh_listing`` had already been given the right precedence after the
    same thing happened to it, in ``_cover_for_refresh``. The push shares the
    group write and did not, and the cover recorded against a listing had
    exactly one reader in the codebase: the refresh.
    """

    def live_listing_with_a_cover(self):
        """One card, pushed with a cover staged on the drafts page."""
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "009/102")
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        self.db.set_plan_group_cover(plan_id, "Base Set|Near Mint", COVER)
        approve_plan(self.db, plan_id, approved_by=1)
        api = FakeEbay()
        push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)
        listing_id = self.db.get_managed_listing(
            "Base Set|Near Mint"
        )["ebay_parent_id"]
        self.assertEqual(self.db.get_listing_cover_image(listing_id), COVER)
        return api

    def test_a_quantity_only_push_keeps_the_recorded_cover(self):
        self.live_listing_with_a_cover()

        self.db.set_manifest_quantity("ID1002", 7)
        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        group = list(api.groups.values())[0]
        self.assertEqual(
            group["imageUrls"], [COVER],
            "the cover recorded against the listing must survive a push "
            "that was never about pictures",
        )

    def test_a_push_keeps_a_cover_only_ebay_knows_about(self):
        """
        The same question the refresh asks. A cover set in Seller Hub, or by
        a build predating any record of it, is still the listing's cover --
        and eBay is the only place it exists.
        """
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "009/102")
        push_plan(self.db, FakeEbay(), self.approved_plan(),
                  user_id=SHARED_SCOPE)
        listing_id = self.db.get_managed_listing(
            "Base Set|Near Mint"
        )["ebay_parent_id"]
        self.assertEqual(self.db.get_listing_cover_image(listing_id), "")

        seller_cover = "https://cdn.example.com/seller-set.jpg"

        class WithCover(FakeEbay):
            def get_group(self, group_key):
                return {"imageUrls": [seller_cover]}

        self.db.set_manifest_quantity("ID1002", 7)
        api = WithCover()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        group = api.groups[next(iter(api.groups))]
        self.assertEqual(group["imageUrls"], [seller_cover])
        # Learned, so the next write need not ask again.
        self.assertEqual(
            self.db.get_listing_cover_image(listing_id), seller_cover
        )

    def test_a_staged_cover_still_wins(self):
        # The one case where replacing it is the whole point: somebody chose
        # a new cover for this listing on the drafts page.
        self.live_listing_with_a_cover()
        chosen = "https://cdn.example.com/chosen.png"

        self.db.set_manifest_quantity("ID1002", 7)
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        self.db.set_plan_group_cover(plan_id, "Base Set|Near Mint", chosen)
        approve_plan(self.db, plan_id, approved_by=1)
        api = FakeEbay()
        push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)

        group = list(api.groups.values())[0]
        self.assertEqual(group["imageUrls"], [chosen])
        listing_id = self.db.get_managed_listing(
            "Base Set|Near Mint"
        )["ebay_parent_id"]
        self.assertEqual(
            self.db.get_listing_cover_image(listing_id), chosen,
            "and the new choice replaces the recorded one",
        )

    def test_the_account_default_does_not_override_one_listings_cover(self):
        """
        The account-wide cover is a default for listings being *created*.
        Letting it win here would rewrite every listing's own cover on the
        next push that touched it -- which is the opposite of what the eBay
        Listings tab offers it for.
        """
        self.live_listing_with_a_cover()
        self.db.set_listing_settings(
            {"cover_image_url": "https://cdn.example.com/account-wide.png"},
            user_id=SHARED_SCOPE,
        )

        self.db.set_manifest_quantity("ID1002", 7)
        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        group = list(api.groups.values())[0]
        self.assertEqual(group["imageUrls"], [COVER])

    def test_a_new_listing_still_takes_the_account_default(self):
        self.db.set_listing_settings(
            {"cover_image_url": "https://cdn.example.com/account-wide.png"},
            user_id=SHARED_SCOPE,
        )
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "009/102")
        api = FakeEbay()
        push_plan(self.db, api, self.approved_plan(), user_id=SHARED_SCOPE)

        group = list(api.groups.values())[0]
        self.assertEqual(
            group["imageUrls"], ["https://cdn.example.com/account-wide.png"]
        )


class CoverVerificationTests(PushTestCase):
    """
    A cover photo is confirmed against eBay, not against eBay's response.

    The per-variation upserts were always checked per SKU, because a bulk
    call answers 200 with failures in the body. The **group** write -- the
    one that actually carries imageUrls -- had its return value discarded,
    and the count reported afterwards counts item upserts. So a group write
    that did not take still read as success, and the log line said "N
    variation(s) re-sent" either way.
    """

    def live_listing(self):
        """A pushed, API-managed variation listing to refresh."""
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "009/102")
        push_plan(self.db, FakeEbay(), self.approved_plan(),
                  user_id=SHARED_SCOPE)
        return self.db.get_managed_listing(
            "Base Set|Near Mint"
        )["ebay_parent_id"]

    def test_a_cover_eBay_reports_back_is_confirmed(self):
        listing_id = self.live_listing()
        self.db.set_listing_cover_image(
            listing_id, "https://cdn.example.com/cover.jpg"
        )

        result = refresh_listing(
            self.db, FakeEbay(), listing_id, user_id=SHARED_SCOPE
        )
        self.assertEqual(result["cover_sent"], "https://cdn.example.com/cover.jpg")
        self.assertIs(result["cover_verified"], True)
        self.assertTrue(
            any(l["level"] == "SUCCESS" for l in result["logs"]),
            "a confirmed cover should report success",
        )

    def test_a_cover_eBay_has_not_reported_back_is_not_claimed_applied(self):
        """
        eBay accepts the call and still reports its old picture. Usually
        that is propagation -- its view of a listing lags by minutes -- and
        occasionally it is a picture eBay refused to fetch. Either way the
        one thing this must not do is claim the cover was applied.
        """
        listing_id = self.live_listing()
        self.db.set_listing_cover_image(
            listing_id, "https://cdn.example.com/too-small.jpg"
        )

        class Ignores(FakeEbay):
            def get_group(self, group_key):
                # Accepted the write, kept its own picture.
                return {"imageUrls": ["https://i.ebayimg.com/old.jpg"]}

        result = refresh_listing(
            self.db, Ignores(), listing_id, user_id=SHARED_SCOPE
        )
        self.assertIs(result["cover_verified"], False)
        warnings = [l["message"] for l in result["logs"] if l["level"] == "WARN"]
        self.assertTrue(warnings, "a cover that did not take must warn")
        joined = " ".join(warnings)
        self.assertIn("not reported the new cover photo back yet", joined)
        self.assertIn("too-small.jpg", joined, "says what we sent")
        self.assertIn("old.jpg", joined, "and what eBay holds")
        # The refresh itself did succeed -- every variation went up -- so the
        # summary stays a success. What must not happen is the cover being
        # reported as confirmed when eBay has not said so.
        self.assertNotIn(
            "eBay confirms the cover photo",
            " ".join(l["message"] for l in result["logs"]),
        )

    def test_a_read_back_that_fails_is_unconfirmed_not_failed(self):
        """
        Not being able to ask is not evidence of a failed write, and
        reporting it as one would send somebody chasing a change that
        landed.
        """
        listing_id = self.live_listing()
        self.db.set_listing_cover_image(
            listing_id, "https://cdn.example.com/cover.jpg"
        )

        class Unreadable(FakeEbay):
            def get_group(self, group_key):
                raise RuntimeError("eBay is having a moment")

        result = refresh_listing(
            self.db, Unreadable(), listing_id, user_id=SHARED_SCOPE
        )
        self.assertIsNone(
            result["cover_verified"], "unknown, rather than True or False"
        )
        joined = " ".join(
            l["message"] for l in result["logs"] if l["level"] == "WARN"
        )
        self.assertIn("unconfirmed", joined)

    def test_a_single_listing_has_no_group_to_confirm(self):
        """
        A single is one inventory item with no group, so there is no group
        write and nothing to read back -- which is unknown, not a failure.
        """
        self.db.set_listing_settings(
            {"group_by_set": "false"}, user_id=SHARED_SCOPE
        )
        try:
            self.add_card("ID1003", "Venusaur", "015/102")
            push_plan(self.db, FakeEbay(), self.approved_plan(),
                      user_id=SHARED_SCOPE)
            managed = self.db.get_managed_listings()
            single = next(
                (m for m in managed if is_single(m["group_key"])), None
            )
            if single is None:
                self.skipTest("no single listing was produced")
            result = refresh_listing(
                self.db, FakeEbay(), single["ebay_parent_id"],
                user_id=SHARED_SCOPE,
            )
            self.assertIsNone(result["cover_verified"])
        finally:
            self.db.set_listing_settings(
                {"group_by_set": "true"}, user_id=SHARED_SCOPE
            )


class DraftEditTests(unittest.TestCase):
    """
    An edit made on the drafts page is what reaches eBay.

    This is the entire point of the approval gate, and it used to be tested
    through the Add file -- so when the File Exchange output was deleted the
    property lost its only test. It belongs here: the destination changed, the
    invariant did not. Before the gate existed, Module A built its files at
    upload time, *before* the draft, so a regrouped card, an edited price or
    an excluded card could never reach eBay at all.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "edits.db"))
        self.db.set_listing_settings(COMPLETE_SETTINGS, user_id=SHARED_SCOPE)
        for manifest_id, name, number in (
            ("ID1001", "Charizard", "004/102"),
            ("ID1002", "Blastoise", "002/102"),
        ):
            self.db.insert_manifest(
                manifest_id, name, "Base Set", "Near Mint", "Holofoil",
                card_number=number, language="English",
                cdn_image=f"https://cdn.example.com/{manifest_id}.jpg",
            )
            self.db.set_manifest_quantity(manifest_id, 2)
            with self.db.get_connection() as conn:
                conn.execute("UPDATE manifest SET price = 1.99 "
                             "WHERE manifest_id = ?", (manifest_id,))
                conn.commit()
            self.db.set_manifest_ebay_fields(manifest_id, {
                "item_specifics": {"C:Graded": "No"},
            })

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_an_edited_quantity_and_price_are_what_get_sent(self):
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        target = next(i for i in self.db.get_plan_items(plan_id)
                      if i["manifest_id"] == "ID1001")
        self.db.update_plan_item(target["id"], proposed_qty=7,
                                 proposed_price=3.50)
        approve_plan(self.db, plan_id, approved_by=1)

        api = FakeEbay()
        push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)

        item = api.items["ID1001"]
        self.assertEqual(
            item["availability"]["shipToLocationAvailability"]["quantity"], 7
        )
        offer = next(o for o in api.offers.values() if o["sku"] == "ID1001")
        self.assertEqual(offer["pricingSummary"]["price"]["value"], "3.50")

        # The card that was not edited keeps the catalogue's figures, so an
        # edit cannot leak sideways onto its neighbours.
        other = api.items["ID1002"]
        self.assertEqual(
            other["availability"]["shipToLocationAvailability"]["quantity"], 2
        )

    def test_an_excluded_card_is_not_sent_at_all(self):
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        target = next(i for i in self.db.get_plan_items(plan_id)
                      if i["manifest_id"] == "ID1001")
        self.db.update_plan_item(target["id"], status="excluded")
        approve_plan(self.db, plan_id, approved_by=1)

        api = FakeEbay()
        push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)

        self.assertNotIn("ID1001", api.items)
        self.assertIn("ID1002", api.items)


class DerivedSpecificsTests(unittest.TestCase):
    """
    The specifics a card's own columns can supply, on the inventory item.

    These used to be tested through the File Exchange Add file, which no
    longer exists; the derivation moved here with the only path that still
    uses it. Worth keeping because eBay marks around twenty specifics
    required on a card listing and silently accepts a listing missing them --
    the failure is a listing nobody finds, not an error.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "specifics.db"))
        self.db.set_listing_settings(COMPLETE_SETTINGS, user_id=SHARED_SCOPE)

    def tearDown(self):
        self.temp_dir.cleanup()

    def push_one(self, specifics):
        self.db.insert_manifest(
            "ID1001", "Charizard", "Base Set", "Near Mint", "Holofoil",
            card_number="004/102", language="English",
            cdn_image="https://cdn.example.com/ID1001.jpg",
        )
        self.db.set_manifest_quantity("ID1001", 1)
        with self.db.get_connection() as conn:
            conn.execute("UPDATE manifest SET price = 25.00 "
                         "WHERE manifest_id = ?", ("ID1001",))
            conn.commit()
        self.db.set_manifest_ebay_fields("ID1001", {
            "item_specifics": specifics,
        })
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        approve_plan(self.db, plan_id, approved_by=1)
        api = FakeEbay()
        push_plan(self.db, api, plan_id, user_id=SHARED_SCOPE)
        return api.items["ID1001"]["product"]["aspects"]

    def test_the_card_supplies_what_the_export_did_not(self):
        aspects = self.push_one({"C:Graded": "No"})
        # Unprefixed names mapping to lists, which is eBay's shape.
        self.assertEqual(aspects["Set"], ["Base Set"])
        self.assertEqual(aspects["Card Name"], ["Charizard"])
        self.assertEqual(aspects["Card Number"], ["004/102"])
        self.assertEqual(aspects["Language"], ["English"])
        self.assertEqual(aspects["Finish"], ["Holofoil"])
        self.assertEqual(aspects["Graded"], ["No"])

    def test_the_export_wins_where_they_disagree(self):
        # The export is eBay's own vocabulary; our columns are SortSwift's.
        # Overriding it would reintroduce the mapping table this project
        # deliberately does not keep.
        aspects = self.push_one({
            "C:Set": "Base Set (Shadowless)", "C:Graded": "No",
        })
        self.assertEqual(aspects["Set"], ["Base Set (Shadowless)"])
        # And the derived ones still fill the gaps around it.
        self.assertEqual(aspects["Card Number"], ["004/102"])

    def test_the_configured_game_overrides_the_export(self):
        # eBay only accepts Game values from its own per-category list.
        aspects = self.push_one({"C:Game": "Pokemon", "C:Graded": "No"})
        self.assertEqual(aspects["Game"], ["Pokémon TCG"])


class GroupKeyTests(unittest.TestCase):
    def test_the_ebay_group_key_is_stable_and_path_safe(self):
        # Rebuilding a draft must map to the same eBay group, or a push would
        # create a second listing for one that already exists.
        first = inventory_group_key("ME01: Mega Evolution|NM")
        self.assertEqual(first, inventory_group_key("ME01: Mega Evolution|NM"))
        self.assertNotIn("|", first)
        self.assertNotIn(" ", first)
        self.assertNotEqual(first, inventory_group_key("ME01: Mega Evolution|LP"))


if __name__ == "__main__":
    unittest.main()
