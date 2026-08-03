import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import baselane_export_all_transactions_cdp as export  # noqa: E402


class FullTransactionExportTests(unittest.TestCase):
    def test_rows_preserve_planner_and_apply_fingerprint_fields(self):
        rows = export.csv_rows(
            [
                {
                    "id": "42",
                    "date": "2026-07-30",
                    "amount": -80.26,
                    "merchantName": "Morgan Linen",
                    "description": "",
                    "name": "Purchase",
                    "propertyId": "88",
                    "tagId": "9",
                    "unitId": "4",
                    "note": {"text": "pending split source"},
                }
            ],
            {"88": "88 Madison Ave"},
            {"9": ("Operating Expenses", "Cleaning & Maintenance")},
        )

        self.assertEqual(
            rows,
            [
                {
                    "Account": "",
                    "Date": "July 30, 2026",
                    "Merchant": "Morgan Linen",
                    "Description": "Purchase",
                    "Amount": -80.26,
                    "Type": "Operating Expenses",
                    "Category": "Cleaning & Maintenance",
                    "Sub-category": "",
                    "Property": "88 Madison Ave",
                    "Unit": "4",
                    "Notes": "pending split source",
                }
            ],
        )

    def test_unknown_metadata_remains_explicitly_blank(self):
        rows = export.csv_rows(
            [
                {
                    "date": "not-a-date",
                    "amount": 1,
                    "merchantName": "Unknown",
                    "propertyId": "missing",
                    "tagId": "missing",
                }
            ],
            {},
            {},
        )

        self.assertEqual(rows[0]["Date"], "not-a-date")
        self.assertEqual(rows[0]["Property"], "")
        self.assertEqual(rows[0]["Category"], "")

    def test_source_index_preserves_live_ids_account_owner_and_pending_state(self):
        rows = export.source_index_rows(
            [
                {
                    "id": "42",
                    "date": "2026-07-30",
                    "amount": -80.26,
                    "merchantName": "Morgan Linen",
                    "propertyId": "88",
                    "tagId": "9",
                    "tagIdSource": "AI",
                    "propertyTagIdSource": "USER",
                    "bankAccountId": "38968",
                    "pending": True,
                }
            ],
            {"88": "88 Madison Ave"},
            {"9": ("Operating Expenses", "Cleaning & Maintenance")},
            {"38968": "ECO Systems, LLC-ECO Systems Operations-2624"},
        )

        self.assertEqual(rows[0]["BaselaneId"], "42")
        self.assertEqual(rows[0]["ISODate"], "2026-07-30")
        self.assertEqual(rows[0]["PropertyId"], "88")
        self.assertEqual(rows[0]["TagId"], "9")
        self.assertEqual(rows[0]["BankAccountId"], "38968")
        self.assertEqual(rows[0]["Pending"], "true")
        self.assertEqual(
            rows[0]["Account"],
            "ECO Systems, LLC-ECO Systems Operations-2624",
        )

    def test_archived_property_aliases_cover_reviewed_live_ids(self):
        aliases = export.load_archived_property_aliases()

        self.assertEqual(aliases["33733"], "90 Madison Ave")
        self.assertEqual(aliases["33734"], "1 Coolwood Dr.")
        self.assertEqual(aliases["34289"], "88 Madison Ave")
        self.assertEqual(aliases["55871"], "3560 Saint Albans Rd")
        self.assertEqual(aliases["56668"], "724 3rd Ave")
        self.assertEqual(aliases["78308"], "84 Madison Ave")

    def test_archived_property_alias_cannot_override_different_live_name(self):
        with self.assertRaisesRegex(RuntimeError, "conflicts with live metadata"):
            export.merge_property_metadata(
                {"33733": "Different Property"},
                {"33733": "90 Madison Ave"},
            )


if __name__ == "__main__":
    unittest.main()
