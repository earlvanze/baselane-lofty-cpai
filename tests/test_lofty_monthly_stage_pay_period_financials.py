import importlib.util
import csv
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lofty_monthly_stage_pay_period_financials.py"
SPEC = importlib.util.spec_from_file_location("stage_pay_period", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class LoftyPayPeriodStagingTests(unittest.TestCase):
    def build_record(self, ledger_path: Path) -> dict:
        with ledger_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["Date", "Merchant", "Amount", "Type", "Category", "Sub-category"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "Date": "July 1, 2026",
                    "Merchant": "Tenant",
                    "Amount": "1000.00",
                    "Type": "Revenue",
                    "Category": "Rent",
                }
            )
            writer.writerow(
                {
                    "Date": "July 2, 2026",
                    "Merchant": "Utility",
                    "Amount": "-100.00",
                    "Type": "Operating Expenses",
                    "Category": "Utilities",
                }
            )
        return {
            "financial_candidate_snapshot": {
                "status": "ok",
                "ledger_path": str(ledger_path),
                "revenue": 1000.0,
                "operating_expenses": -100.0,
                "noi": 900.0,
                "net_operating_cashflow": 800.0,
            },
            "financial_candidate_gate_issues": [],
        }

    def test_live_cutoff_selects_prior_month_before_cutoff_and_current_after(self):
        config = {"cutoffDay": 15, "cutoffTimeCt": "12:00", "timezone": "America/Chicago"}
        self.assertEqual(MODULE.upcoming_month(config, datetime(2026, 7, 10, tzinfo=timezone.utc)), (2026, 5))
        self.assertEqual(MODULE.upcoming_month(config, datetime(2026, 7, 31, tzinfo=timezone.utc)), (2026, 6))

    def test_86_madison_reconciles_noi_and_net_cashflow(self):
        with tempfile.TemporaryDirectory() as directory:
            record = self.build_record(Path(directory) / "86-madison.csv")
            entry, issues = MODULE.build_pl_entry(record, "2026-07")
        self.assertEqual(issues, [])
        self.assertEqual(entry["monthly_rent"], 1000.0)
        self.assertEqual(entry["or_replenishment"], 1200.0)
        self.assertEqual(entry["cash_flow"], 9600.0)

    def test_distribution_ineligible_positive_nocf_is_retained(self):
        eligibility = {
            "eligible": False,
            "reason": "approved governance update retains 100% of NOI for loan balances and capital calls",
        }

        with tempfile.TemporaryDirectory() as directory:
            record = self.build_record(Path(directory) / "1432-sara.csv")
            entry, issues = MODULE.build_pl_entry(record, "2026-07", eligibility)

        self.assertEqual(issues, [])
        self.assertEqual(entry["cash_flow"], 0.0)
        self.assertEqual(entry["or_replenishment"], 10800.0)
        self.assertIn("retained rather than distributed", entry["notes"])

    def test_distribution_override_matches_abbreviated_managed_name(self):
        index = MODULE.load_distribution_eligibility(
            Path(__file__).resolve().parents[1] / "config" / "lofty_distribution_eligibility_overrides.json",
            "2026-07",
        )

        match = MODULE.distribution_eligibility_for(
            {"property_name": "1432 Sara Ave, Akron, Ohio 44305", "managed_name": "1432 Sara Ave"},
            index,
        )

        self.assertIsNotNone(match)
        self.assertFalse(match["eligible"])


if __name__ == "__main__":
    unittest.main()
