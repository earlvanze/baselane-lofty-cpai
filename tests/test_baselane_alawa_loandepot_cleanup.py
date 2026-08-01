import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import baselane_alawa_loandepot_cleanup as cleanup


class AlawaLoanDepotCleanupTests(unittest.TestCase):
    def test_july_statement_split_balances(self):
        target = cleanup.TARGET_SPLITS["321949940"]
        self.assertEqual(sum(cleanup.decimal_amount(row["amount"]) for row in target), Decimal("-3612.70"))
        self.assertEqual(
            [(row["propertyId"], row["tagId"]) for row in target],
            [("37648", "11"), ("37648", "20"), ("73461", "15"), ("73461", "8"), ("37648", "130")],
        )

    def test_named_components_follow_ownership_policy(self):
        cases = {
            "85-104 Alawa Pl Mortgage Interest": ("37648", "11"),
            "85-104 Alawa Pl Mortgage Principal": ("37648", "20"),
            "85-104 Alawa Pl Mortgage Escrow - Property Taxes": ("73461", "15"),
            "85-104 Alawa Pl Mortgage Escrow - Insurance": ("73461", "8"),
            "85-104 Alawa Pl Mortgage Escrow - General": ("37648", "130"),
        }
        for merchant, expected in cases.items():
            with self.subTest(merchant=merchant):
                self.assertEqual(cleanup.expected_component({"merchantName": merchant}), expected)

    def test_legacy_return_and_debit_use_same_component(self):
        for amount in ("141.56", "-141.56"):
            row = {"merchantName": "LOANDEPOT", "parentId": "parent", "amount": amount}
            self.assertEqual(cleanup.expected_component(row), ("73461", "95"))

    def test_unrecognized_direct_payment_is_not_changed(self):
        row = {"merchantName": "LOANDEPOT", "amount": "-3437.09", "parentId": None}
        self.assertIsNone(cleanup.expected_component(row))

    def test_complete_target_plus_old_children_deletes_only_extras(self):
        target = cleanup.TARGET_SPLITS["321949940"]
        children = [dict(row, id=str(index)) for index, row in enumerate(target, 1)]
        children.append(
            {
                "id": "99",
                "amount": -1479.65,
                "tagId": "24",
                "propertyId": cleanup.ALAWA_PROPERTY_ID,
                "merchantName": "85-104 Alawa Pl Mortgage Interest",
            }
        )
        self.assertEqual(cleanup.duplicate_child_ids({"splitTransactions": children}, target), ["99"])


if __name__ == "__main__":
    unittest.main()
