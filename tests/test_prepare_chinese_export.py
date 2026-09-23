"""
The export corrections applied before a Chinese import.

Two of the three fields this touches are part of a card's identity, so the
corrections have to happen in the file rather than in the catalogue. The
test that matters most is the negative one: the card name must be left
alone, because stripping the number out of it merges different cards.
"""

import csv
import io
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "prepare_chinese_export.py")

HEADER = [
    "*Action", "*Category", "*Title", "*ConditionID", "*C:Game", "*C:Set",
    "*C:Card Name", "*C:Card Number", "*C:Language", "Set Code", "Language",
    "Condition", "Printing", "*Quantity",
]


def row(set_name, code, card_name, number, language="Czech", lang_code="CS"):
    return {
        "*Action": "Add", "*Category": "183454",
        "*Title": f"{card_name} {set_name}", "*ConditionID": "4000",
        "*C:Game": "Pokemon Chinese", "*C:Set": set_name,
        "*C:Card Name": card_name, "*C:Card Number": number,
        "*C:Language": language, "Set Code": code, "Language": lang_code,
        "Condition": "NM", "Printing": "Normal", "*Quantity": "1",
    }


class PrepareChineseExportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.source = os.path.join(self.dir, "chinese.csv")
        self.out = os.path.join(self.dir, "corrected.csv")
        self.rows = [
            row("Gem Pack Vol 4", "Gem Pack 4", "Applin - 1902", "1902"),
            row("Gem Pack Vol 4", "Gem Pack 4", "Applin - 1903", "1903"),
            row("Gem Pack Volume 6", "Gem Pack 6", "Latios - 1301", "1301"),
            row("Terastal Gathering", "CSV9.5C", "Suicune - 40", "40"),
        ]
        with open(self.source, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=HEADER)
            writer.writeheader()
            writer.writerows(self.rows)

    def run_script(self, *args):
        result = subprocess.run(
            [sys.executable, SCRIPT, self.source, *args],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def corrected(self):
        self.run_script("-o", self.out)
        with open(self.out, encoding="utf-8-sig") as handle:
            return list(csv.DictReader(handle))

    def test_it_writes_nothing_without_an_output_path(self):
        output = self.run_script()
        self.assertIn("Nothing was written", output)
        self.assertFalse(os.path.exists(self.out))

    def test_the_placeholder_set_codes_are_replaced(self):
        out = self.corrected()
        self.assertEqual(
            [r["Set Code"] for r in out], ["CBB4C", "CBB4C", "CBB6C", "CSV9.5C"]
        )

    def test_the_set_names_are_made_consistent(self):
        out = self.corrected()
        self.assertEqual(out[0]["*C:Set"], "Gem Pack Volume 4")
        self.assertEqual(out[2]["*C:Set"], "Gem Pack Volume 6")

    def test_a_set_it_does_not_know_is_left_exactly_as_it_is(self):
        out = self.corrected()
        self.assertEqual(out[3]["*C:Set"], "Terastal Gathering")
        self.assertEqual(out[3]["Set Code"], "CSV9.5C")

    def test_the_language_aspect_is_corrected(self):
        out = self.corrected()
        self.assertTrue(all(r["*C:Language"] == "Chinese" for r in out))

    def test_the_plain_language_code_is_left_alone(self):
        """
        It becomes manifest.language and selects the title template, and CS
        is the code the Chinese template is stored under. "Tidying" it to
        "Chinese" drops every one of these listings onto the English format.
        """
        out = self.corrected()
        self.assertTrue(all(r["Language"] == "CS" for r in out))

    def test_the_card_name_keeps_its_number(self):
        """
        The natural key excludes the card number, so the number inside the
        name is the only thing separating Applin 1902 from Applin 1903.
        """
        out = self.corrected()
        self.assertEqual(out[0]["*C:Card Name"], "Applin - 1902")
        self.assertEqual(out[1]["*C:Card Name"], "Applin - 1903")

    def test_no_row_or_column_is_lost(self):
        out = self.corrected()
        self.assertEqual(len(out), len(self.rows))
        self.assertEqual(list(out[0]), HEADER)

    def test_nothing_but_the_three_fields_is_touched(self):
        out = self.corrected()
        untouched = [
            c for c in HEADER
            if c not in ("*C:Set", "Set Code", "*C:Language")
        ]
        for before, after in zip(self.rows, out):
            for column in untouched:
                self.assertEqual(before[column], after[column], column)

    def test_the_wrong_export_is_refused_by_name(self):
        """
        SortSwift's inventory export has none of these columns, and saying
        so beats writing a copy that corrected nothing.
        """
        thin = os.path.join(self.dir, "thin.csv")
        with open(thin, "w", encoding="utf-8", newline="") as handle:
            handle.write("Name,Quantity\nApplin,1\n")
        result = subprocess.run(
            [sys.executable, SCRIPT, thin], capture_output=True, text=True,
            cwd=REPO_ROOT,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("export_eBay_", result.stdout)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        first = self.corrected()
        second_out = os.path.join(self.dir, "twice.csv")
        subprocess.run(
            [sys.executable, SCRIPT, self.out, "-o", second_out],
            capture_output=True, text=True, cwd=REPO_ROOT, check=True,
        )
        with open(second_out, encoding="utf-8-sig") as handle:
            second = list(csv.DictReader(handle))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
