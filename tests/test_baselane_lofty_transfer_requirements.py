import sys
import tempfile
import unittest
from csv import DictWriter
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import baselane_lofty_transfer_requirements as transfers
import baselane_cf_balance_sheet_consistency_audit as cf_audit
from coownership_reserve_policy import (
    LOCAL_FINANCIALS_ONLY_PROPERTIES,
    canonical_property,
)


class BaselaneLoftyTransferRequirementsTests(unittest.TestCase):
    def test_transfer_planner_uses_combined_eco_cash_and_lofty_or_floor(self):
        summary = {
            "eco_gl_column_e_sum": 2500.00,
            "eco_gl_column_e_status": "ok",
            "eco_gl_column_e_scope": "all_property_split_rows",
            "eco_gl_column_e_source_mode": "canonical_property_split_gl",
            "eco_operating_cash": 2500.00,
            "eco_operating_cash_status": "ok",
            "eco_operating_cash_source_mode": "verified_eco_cash_custody_reconciliation",
            "lofty_curr_maintenance_reserve": 1000.00,
        }
        property_name = "88 Madison Ave"
        cf_index = {
            transfers.normalize_property_name(property_name): {
                "eco_balance_semantics": "spendable_eco_cash",
                "eco_operating_cash_expected": 2500.00,
                "eco_operating_cash_actual": 2500.00,
            }
        }

        with patch.object(transfers, "financial_summary", return_value=summary):
            rows = transfers.build_rows(
                records=[
                    {
                        "property_name": property_name,
                        "property_path": "/tmp/Real Estate/NY/88 Madison Ave Public",
                    }
                ],
                cf_index=cf_index,
                inactive_rows={},
                states={"NY"},
                eco_minimum=3000.00,
                global_source_blockers=[],
                coownership_policy_blockers={},
                property_cash_review_policy_blockers={},
                property_cash_review_details_by_property=[],
                monthly_accruals_report={"status": "ok"},
            )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["combined_reserve_liquidity"], 3500.00)
        self.assertEqual(row["combined_reserve_surplus_above_floor"], 500.00)
        self.assertEqual(row["sendable_eco_cash_above_combined_floor"], 500.00)
        self.assertEqual(row["combined_reserve_shortfall_to_floor"], 0.00)
        self.assertEqual(row["action"], "send_to_lofty")
        self.assertEqual(row["bank_transfer_amount"], 500.00)

    def test_cf_audit_accepts_dated_gl_cash_basis_fallback(self):
        source = {
            "eco_operating_cash": 18296.21,
            "eco_operating_cash_status": "ok",
            "eco_operating_cash_source_mode": "property_gl_cash_basis_net_open_obligations",
            "eco_operating_cash_as_of_date": "2026-07-31",
        }

        result = cf_audit.validate_operating_cash_authority(
            source,
            requested_month="2026-07",
            source_cash_mode="as_of_month_end",
        )

        self.assertEqual(result["eco_operating_cash_authority_status"], "ok")
        self.assertEqual(result["eco_operating_cash"], 18296.21)

    def test_financial_summary_preserves_verified_spendable_cash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            public = Path(temp_dir) / "Real Estate" / "NY" / "88 Madison Ave Public"
            ledger = public / "07 - P&L & Owner Statements" / "ECO Systems General Ledger - 88 Madison Ave.csv"
            ledger.parent.mkdir(parents=True)
            with ledger.open("w", encoding="utf-8", newline="") as handle:
                writer = DictWriter(handle, fieldnames=["Amount", "Type"])
                writer.writeheader()
                writer.writerow({"Amount": "19539.24", "Type": "Cash"})
            record = {
                "monthly_financial_summary": {
                    "eco_gl_column_e_source": str(ledger),
                    "eco_operating_cash": -19744.68,
                    "eco_operating_cash_status": "ok",
                    "eco_operating_cash_source_mode": "verified_eco_cash_custody_reconciliation",
                    "eco_operating_cash_balance_scope": "eco_held_unrestricted_cash_only",
                    "eco_operating_cash_as_of_date": "2026-07-31",
                }
            }

            summary = transfers.financial_summary(record)

            self.assertEqual(summary["eco_gl_column_e_sum"], 19539.24)
            self.assertEqual(summary["eco_operating_cash"], -19744.68)
            self.assertEqual(
                summary["eco_operating_cash_source_mode"],
                "verified_eco_cash_custody_reconciliation",
            )

    def test_cf_cross_check_keeps_gl_and_spendable_cash_separate(self):
        cash_rows = [
            {
                "property": "88 Madison Ave",
                "eco_operating_cash": -19744.68,
                "eco_gl_column_e_sum": 19539.24,
            }
        ]
        cf_index = {
            transfers.normalize_property_name("88 Madison Ave"): {
                "eco_balance_semantics": "full_canonical_property_general_ledger_net_position_including_accruals",
                "eco_general_ledger_expected": 19539.24,
                "eco_general_ledger_actual": 19539.24,
                "eco_operating_cash_expected": -19744.68,
                "eco_operating_cash_actual": -19744.68,
                "workbook_audit_status": "enabled",
            }
        }

        self.assertEqual(
            transfers.cf_balance_cross_artifact_mismatches(cash_rows, cf_index),
            [],
        )

    def test_sold_property_does_not_block_for_missing_accrual_template(self):
        report = {
            "status": "review",
            "active_without_accrual_template_count": 1,
            "active_without_accrual_templates": [
                {
                    "property_name": "6914 Polonia Ave, Cleveland",
                    "full_address": "6914 Polonia Ave, Cleveland, OH 44105",
                }
            ],
        }
        inactive_rows = {
            transfers.normalize_property_name("6914 Polonia Ave, Cleveland, OH 44105"): {
                "status": "YHOME (Sold)"
            }
        }

        blockers = transfers.monthly_accrual_blockers(report, inactive_rows)

        self.assertNotIn(
            "monthly_accruals_active_without_accrual_template_count=1",
            blockers,
        )

    def test_ohio_three_package_is_local_financials_only(self):
        canonical = canonical_property("Ohio 3 Property Package")

        self.assertEqual(canonical, "Ohio 3-Property Package")
        self.assertIn(canonical, LOCAL_FINANCIALS_ONLY_PROPERTIES)


if __name__ == "__main__":
    unittest.main()
