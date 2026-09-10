"""
Tests for the files an approved plan produces.

Two of these guard mistakes that had already shipped into a real Add file.

A cover photo staged on the drafts page reached only the cover-photo *Revise*
file, which by design covers listings that already exist. A cover for a
listing the plan is about to create therefore went nowhere: every parent row
fell back to the first card's picture, and the file gave no sign that a choice
had been made and dropped.

And the item specifics eBay marks required were reduced to ``C:Game`` alone,
because the export's ``C:`` columns had not been persisted when those cards
were catalogued and nothing here derived the ones a card's own identity can
supply.
"""

import csv
import io
import os
import tempfile
import unittest

from tcg_engine.db import Database, SHARED_SCOPE
from tcg_engine.plan_exports import build_plan_exports
from tcg_engine.plans import approve_plan, build_plan

COMPLETE_SETTINGS = {
    "category_id": "183454",
    "seller_postal_code": "94305",
    "default_game": "Pokémon TCG",
    "shipping_profile_name": "Free Shipping Cards",
    "return_profile_name": "No Returns",
    "payment_profile_name": "Immediate Payment",
    "variation_title_template": "{set_name}: Pick Your Card - {condition}",
}

COVER = "https://cdn.example.com/chosen-cover.jpg"


class PlanExportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "exports.db"))
        self.db.set_listing_settings(COMPLETE_SETTINGS, user_id=SHARED_SCOPE)

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_card(self, manifest_id, name, card_number, price=1.99,
                 specifics=None):
        self.db.insert_manifest(
            manifest_id,
            name,
            "Base Set",
            "Near Mint",
            "Holofoil",
            card_number=card_number,
            language="English",
            cdn_image=f"https://cdn.example.com/{manifest_id}.jpg",
        )
        self.db.set_manifest_quantity(manifest_id, 2)
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE manifest SET price = ? WHERE manifest_id = ?",
                (price, manifest_id),
            )
            conn.commit()
        self.db.set_manifest_ebay_fields(manifest_id, {
            "item_specifics": (
                {"C:Card Type": "Pokémon", "C:Graded": "No"}
                if specifics is None else specifics
            ),
        })

    def approved_plan(self):
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        approve_plan(self.db, plan_id, approved_by=1)
        return plan_id

    def add_rows(self, plan_id):
        built = build_plan_exports(self.db, plan_id, user_id=SHARED_SCOPE)
        return built, list(csv.DictReader(io.StringIO(built["add_csv"])))

    def test_a_staged_cover_travels_in_the_add_file(self):
        self.add_card("ID1001", "Charizard", "004/102")
        self.add_card("ID1002", "Blastoise", "002/102")
        plan_id = build_plan(self.db, user_id=1)["plan_id"]
        # The key the drafts page stages against: "<set>|<condition>".
        self.db.set_plan_group_cover(plan_id, "Base Set|Near Mint", COVER)
        approve_plan(self.db, plan_id, approved_by=1)

        built, rows = self.add_rows(plan_id)
        parents = [r for r in rows if r["Relationship"] == ""]
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0]["PicURL"], COVER)
        self.assertEqual(built["add_cover_count"], 1)

        # The children keep their own pictures: the cover is the listing's
        # gallery image, not a replacement for every variation's photo.
        children = [r for r in rows if r["Relationship"] == "Variation"]
        self.assertEqual(len(children), 2)
        for child in children:
            self.assertNotIn(COVER, child["PicURL"])
            self.assertIn("=https://cdn.example.com/ID100", child["PicURL"])

        # And it is not offered as a Revise, because there is no listing to
        # revise yet -- which is exactly why it had to travel in the Add file.
        self.assertEqual(self.db.get_plan_cover_revisions(plan_id), [])

    def test_without_a_staged_cover_the_first_card_is_used(self):
        self.add_card("ID1001", "Charizard", "004/102")
        built, rows = self.add_rows(self.approved_plan())
        parent = next(r for r in rows if r["Relationship"] == "")
        self.assertEqual(parent["PicURL"], "https://cdn.example.com/ID1001.jpg")
        self.assertEqual(built["add_cover_count"], 0)

    def test_group_specifics_are_derived_from_the_cards_when_absent(self):
        # An export that carried some C: columns but not these. A variation
        # listing states its specifics once, on the parent, so only the ones
        # the whole group agrees on can appear there -- Card Name and Card
        # Number differ per card and the variation axis expresses them
        # instead.
        self.add_card("ID1001", "Charizard", "004/102",
                      specifics={"C:Graded": "No"})
        self.add_card("ID1002", "Blastoise", "002/102",
                      specifics={"C:Graded": "No"})
        _, rows = self.add_rows(self.approved_plan())
        parent = next(r for r in rows if r["Relationship"] == "")
        self.assertEqual(parent["C:Set"], "Base Set")
        self.assertEqual(parent["C:Language"], "English")
        self.assertEqual(parent["C:Finish"], "Holofoil")
        self.assertEqual(parent["C:Graded"], "No")
        # Differing values are absent rather than picked from one card.
        self.assertEqual(parent.get("C:Card Name", ""), "")

    def test_a_single_listing_carries_the_card_level_specifics(self):
        # Above the single-listing threshold the card is its own listing, so
        # the specifics that identify one card do belong on it.
        self.add_card("ID1001", "Charizard", "004/102", price=25.00,
                      specifics={"C:Graded": "No"})
        _, rows = self.add_rows(self.approved_plan())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["C:Card Name"], "Charizard")
        self.assertEqual(rows[0]["C:Card Number"], "004/102")
        self.assertEqual(rows[0]["C:Set"], "Base Set")

    def test_the_export_wins_over_a_derived_value(self):
        # The export is eBay's own vocabulary; our columns are SortSwift's.
        # Where they disagree the export is right, and overriding it would
        # reintroduce the mapping table this project deliberately does not
        # keep.
        self.add_card(
            "ID1001",
            "Charizard",
            "004/102",
            price=25.00,
            specifics={"C:Set": "Base Set (Shadowless)", "C:Graded": "No"},
        )
        _, rows = self.add_rows(self.approved_plan())
        self.assertEqual(rows[0]["C:Set"], "Base Set (Shadowless)")
        # The derived ones still fill the gaps around it.
        self.assertEqual(rows[0]["C:Card Number"], "004/102")

    def test_the_descriptor_style_setting_reaches_the_file(self):
        # eBay's own docs give CD:40001 a numeric value, and reports differ on
        # whether it accepts the prose form, so the style is a setting. It was
        # being frozen at cataloguing time: the descriptor persisted with the
        # card won, and switching the setting changed nothing.
        self.add_card("ID1001", "Charizard", "004/102", price=25.00,
                      specifics={"C:Graded": "No"})
        _, rows = self.add_rows(self.approved_plan())
        self.assertEqual(rows[0]["CD:40001"], "Near mint or better - (ID: 400010)")

        self.db.set_listing_settings(
            {"condition_descriptor_style": "id"}, user_id=SHARED_SCOPE
        )
        _, rows = self.add_rows(self.approved_plan())
        self.assertEqual(rows[0]["CD:40001"], "400010")

    def test_a_descriptor_the_export_supplied_is_passed_through(self):
        # The export is eBay's own vocabulary. Re-rendering a value it stated
        # would be this project maintaining its own mapping instead.
        self.add_card("ID1001", "Charizard", "004/102", price=25.00,
                      specifics={"C:Graded": "No"})
        self.db.set_manifest_ebay_fields("ID1001", {
            "condition_descriptor": "Graded - (ID: 2750)",
            "condition_descriptor_from_export": True,
        })
        _, rows = self.add_rows(self.approved_plan())
        self.assertEqual(rows[0]["CD:40001"], "Graded - (ID: 2750)")

    def test_the_configured_game_overrides_the_export(self):
        # eBay only accepts Game values from its own per-category list.
        self.add_card(
            "ID1001", "Charizard", "004/102", price=25.00,
            specifics={"C:Game": "Pokemon", "C:Graded": "No"},
        )
        _, rows = self.add_rows(self.approved_plan())
        self.assertEqual(rows[0]["C:Game"], "Pokémon TCG")


if __name__ == "__main__":
    unittest.main()
