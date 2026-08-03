import csv
import json
import sys
import tempfile
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

    def test_pm_eco_revenue_is_excluded_from_dao_property_ledger(self):
        description = (
            "ECO Systems LLC PM Fee Revenue | "
            "1456 W 85th St, Cleveland, OH 44102 | July 2026"
        )
        row = {
            "Amount": "245.88",
            "Type": "Manual",
            "Category": "Revenue",
            "Sub-category": "Fees & Other Revenue",
            "Merchant": description,
            "Description": description,
            "Notes": (
                "AOPS-OHIL-ACCRUAL|pm_eco|1456 W 85th St, Cleveland, OH 44102|"
                "2026-07|245.88 | reciprocal ECO PM revenue"
            ),
        }
        self.assertTrue(split.is_eco_company_dao_fee_revenue(row))

    def test_pm_dao_expense_remains_in_dao_property_ledger(self):
        row = {
            "Amount": "-245.88",
            "Type": "Manual",
            "Category": "Operating Expenses",
            "Sub-category": "Property Management",
            "Merchant": "PM Fee Accrual | 1456 W 85th St | July 2026",
            "Description": "PM Fee Accrual | 1456 W 85th St | July 2026",
            "Notes": (
                "AOPS-OHIL-ACCRUAL|pm_dao|1456 W 85th St, Cleveland, OH 44102|"
                "2026-07|245.88 | DAO-side PM expense"
            ),
        }
        self.assertFalse(split.is_eco_company_dao_fee_revenue(row))

    def test_unrelated_manual_revenue_is_not_excluded(self):
        row = self.dao_fee_row("Manual")
        row["Notes"] = "ordinary property revenue"
        self.assertFalse(split.is_eco_company_dao_fee_revenue(row))


class PropertyRootOverrideTests(unittest.TestCase):
    def test_alcott_uses_canonical_public_property_folder(self):
        self.assertEqual(
            split.OVERRIDES["326 South Alcott Street"],
            "CO/326-332 S Alcott St Public",
        )

    def test_clybourne_uses_primary_property_folder(self):
        self.assertEqual(
            split.OVERRIDES["4318 Clybourne Ave"],
            "OH/4318 Clybourne Ave, Cleveland, OH 44109",
        )


class ExactDuplicateSourceTests(unittest.TestCase):
    def write_ledger(self, path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["Date", "Property", "Amount", "Description"])
            writer.writeheader()
            writer.writerows(
                [
                    {"Date": "2026-07-01", "Property": "Test Property", "Amount": "100", "Description": "Rent"},
                    {"Date": "2026-07-01", "Property": "Test Property", "Amount": "100", "Description": "Rent"},
                    {"Date": "2026-07-02", "Property": "Test Property", "Amount": "-25", "Description": "Repair"},
                ]
            )

    def test_read_ledger_groups_collapses_exact_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "ledger.csv"
            self.write_ledger(source)

            _fields, grouped, total_rows, missing_rows, duplicate_rows = split.read_ledger_groups(source)

            self.assertEqual(total_rows, 3)
            self.assertEqual(missing_rows, 0)
            self.assertEqual(duplicate_rows, 1)
            self.assertEqual(len(grouped["Test Property"]), 2)

    def test_json_preview_persists_source_digest_and_duplicate_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "ledger.csv"
            report_path = root / "preview.json"
            real_estate = root / "Real Estate"
            real_estate.mkdir()
            self.write_ledger(source)

            rc = split.main(
                [
                    "--json",
                    "--source",
                    str(source),
                    "--real-estate-base",
                    str(real_estate),
                    "--report",
                    str(report_path),
                ]
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(report["source_sha256"], split.file_digest(source))
            self.assertEqual(report["total_row_count"], 3)
            self.assertEqual(report["deduped_row_count"], 2)
            self.assertEqual(report["exact_duplicate_extra_row_count"], 1)
            self.assertFalse(report["write_attempted"])


if __name__ == "__main__":
    unittest.main()
