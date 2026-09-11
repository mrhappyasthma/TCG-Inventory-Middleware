"""
Tests for pushing an approved plan to eBay.

Driven by a fake standing in for the eBay library, which is why the push takes
its API surface as an argument. The behaviours under test are the ones that
decide whether the store and our mirror still agree afterwards, and every one
of them is a way a push can *appear* to succeed:

* eBay answers HTTP 200 to a bulk call and reports failure per SKU inside the
  body, so one card failing must not mark its neighbours pushed;
* a listing created through File Exchange is invisible to this API, so pushing
  its cards would create a duplicate rather than update it;
* a card already pushed must not be pushed again, or a second listing appears;
* the mirror is written from what eBay confirmed, never from what we intended.
"""

import os
import tempfile
import unittest

from tcg_engine.db import Database, SHARED_SCOPE
from tcg_engine.plans import (
    ACTION_UPDATE,
    STATUS_DEFERRED,
    STATUS_FAILED,
    STATUS_PUSHED,
    approve_plan,
    build_plan,
)
from tcg_engine.push import (
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
        self.withdrawn = []
        self.next_offer = 1000
        self.next_listing = 220000

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

    def offer_ids_for(self, sku):
        self.calls.append(("offer_ids_for", sku))
        return [
            oid for oid, payload in self.offers.items()
            if payload.get("sku") == sku
        ]

    def update_price_quantity(self, requests):
        self.calls.append(("update_price_quantity", [r["sku"] for r in requests]))
        rows = []
        for request in requests:
            sku = request["sku"]
            if sku in self.fail_skus:
                rows.append({
                    "sku": sku, "statusCode": 400,
                    "errors": [{"longMessage": self.fail_skus[sku]}],
                })
            else:
                rows.append({"sku": sku, "statusCode": 200})
        return rows

    def upsert_group(self, group_key, payload):
        self.calls.append(("upsert_group", group_key))
        self.groups[group_key] = payload

    def publish_group(self, group_key):
        self.calls.append(("publish_group", group_key))
        self.next_listing += 1
        return str(self.next_listing)

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
        self.assertEqual(
            api.kinds(),
            ["upsert_items", "create_offers", "upsert_group", "publish_group"],
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
    def test_a_file_exchange_listing_is_left_for_the_csv_path(self):
        """
        The guard that stops a push duplicating a live listing.

        eBay has the listing, but the Inventory API cannot see it, so writing
        a group for these SKUs would publish a *second* listing beside the one
        already selling. Deferring is the only safe answer until it is
        migrated -- and the CSV files still cover it meanwhile.
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
            any("File Exchange" in entry["message"] for entry in result["logs"])
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
        # Nothing that could change what is on sale.
        self.assertNotIn("update_price_quantity", api.kinds())
        self.assertNotIn("publish_group", api.kinds())
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

    def test_a_csv_listing_cannot_be_refreshed(self):
        # It is invisible to this API; pushing at it would create a duplicate.
        self.add_card("ID1001", "Charizard", "004/102")
        self.db.upsert_variation("ID1001", "227511361186", 2,
                                 custom_label="ID1001")
        with self.assertRaises(PushError) as caught:
            refresh_listing(self.db, FakeEbay(), "227511361186",
                            user_id=SHARED_SCOPE)
        self.assertIn("CSV path", str(caught.exception))


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
