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


if __name__ == "__main__":
    unittest.main()
