import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import split_ledger_public_financials as split


class EcoCompanyRevenueClassificationTests(unittest.TestCase):
    def dao_fee_row(self, row_type: str) -> dict[str, str]:
        description = (
            "ECO Systems LLC DAO Registration Fee Revenue | "
            "4318 Clybourne Ave, Cleveland, OH 44109 | July 2026"
        )
        return {
            "Amount": "62.5",
            "Type": row_type,
            "Category": "Fees & Other Revenue",
            "Merchant": description,
            "Description": description,
            "Notes": (
                "AOPS-OHIL-ACCRUAL|dao_eco|4318 Clybourne Ave, Cleveland, OH 44109|"
                "2026-07|62.50 | ECO Systems LLC fixed DAO registration/admin fee revenue"
            ),
        }

    def test_generated_revenue_row_is_eco_company_revenue(self):
        self.assertTrue(split.is_eco_company_dao_fee_revenue(self.dao_fee_row("Revenue")))

    def test_baselane_manual_export_is_eco_company_revenue(self):
        row = self.dao_fee_row("Manual")
        row["Category"] = "Revenue"
        row["Sub-category"] = "Fees & Other Revenue"
        self.assertTrue(split.is_eco_company_dao_fee_revenue(row))

    def test_unrelated_manual_revenue_is_not_excluded(self):
        row = self.dao_fee_row("Manual")
        row["Notes"] = "ordinary property revenue"
        self.assertFalse(split.is_eco_company_dao_fee_revenue(row))


class PropertyRootOverrideTests(unittest.TestCase):
    def test_clybourne_uses_primary_property_folder(self):
        self.assertEqual(
            split.OVERRIDES["4318 Clybourne Ave"],
            "OH/4318 Clybourne Ave, Cleveland, OH 44109",
        )


if __name__ == "__main__":
    unittest.main()
