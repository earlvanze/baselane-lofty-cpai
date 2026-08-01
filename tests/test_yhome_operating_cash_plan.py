import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "baselane_cf_balance_sheet_consistency_audit.py"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("baselane_cf_balance_sheet_consistency_audit", SCRIPT)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class YhomeOperatingCashPlanTests(unittest.TestCase):
    def test_plan_csv_preserves_eco_cash_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.csv"
            audit.write_plan_csv(
                path,
                [
                    {
                        "property": "1432 Sara Ave",
                        "column": "ECO Net DAO Funds",
                        "eco_cash_policy": "eco_held_unrestricted_cash_v1",
                        "action": "update",
                    }
                ],
            )
            with path.open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(
                row["eco_cash_policy"],
                "eco_held_unrestricted_cash_v1",
            )

    def test_plan_uses_spendable_eco_cash_not_full_gl(self):
        records = [
            {
                "property_name": "1518 Dille Rd, Euclid, OH 44117",
                "property_path": "/Public/Ohio/1518 Dille Rd",
                "monthly_financial_summary": {
                    "lofty_curr_maintenance_reserve": 0,
                    "eco_operating_cash": 1140.96,
                    "eco_operating_cash_status": "ok",
                    "eco_operating_cash_source_mode": "verified_eco_cash_custody_reconciliation",
                    "eco_operating_cash_as_of_date": "2026-07-30",
                    "eco_general_ledger_sum": 4884.16,
                },
            }
        ]
        rows = [
            {
                "Property": "1518 Dille Rd. Euclid, OH 44117",
                "New PM": "ECO",
                "Lofty Operating Cash": "0",
                "ECO Net DAO Funds": "2925.17",
            }
        ]
        header_map = {name.lower(): name for name in rows[0]}

        plan, issues, missing, excluded = audit.build_yhome_plan(
            records,
            rows,
            header_map,
            requested_month="2026-07",
        )

        eco = next(row for row in plan if row["column"] == "ECO Net DAO Funds")
        self.assertEqual(eco["target_value"], 1140.96)
        self.assertEqual(eco["eco_cash_policy"], "eco_held_unrestricted_cash_v1")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["type"], "yhome_target_column_update_required")
        self.assertEqual(issues[0]["diff"], 1784.21)
        self.assertEqual(missing, [])
        self.assertEqual(excluded, [])


if __name__ == "__main__":
    unittest.main()
