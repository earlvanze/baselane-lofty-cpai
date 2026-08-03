import csv
import importlib.util
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "baselane_ecogl_data_quality_autonomy.py"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("baselane_ecogl_data_quality_autonomy", SCRIPT)
assert SPEC and SPEC.loader
autonomy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(autonomy)


class FutureDatedSourceExceptionsTests(unittest.TestCase):
    def write_index(self, root: Path, rows: list[dict[str, str]]) -> None:
        reports = root / "reports"
        reports.mkdir()
        with (reports / autonomy.SOURCE_INDEX_NAME).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["BaselaneId", "ISODate", "Amount", "Merchant", "Notes"],
            )
            writer.writeheader()
            writer.writerows(rows)

    def test_ignores_post_cutoff_bank_rows_and_same_month_aops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_index(
                root,
                [
                    {
                        "BaselaneId": "bank",
                        "ISODate": "2026-07-31",
                        "Amount": "-25.83",
                        "Merchant": "AFFIRM",
                        "Notes": "SimpliSafe System",
                    },
                    {
                        "BaselaneId": "transfer",
                        "ISODate": "2026-07-31",
                        "Amount": "-500",
                        "Merchant": "INTERNAL_TRANSFER",
                        "Notes": "",
                    },
                    {
                        "BaselaneId": "same-month-aops",
                        "ISODate": "2026-07-31",
                        "Amount": "-100",
                        "Merchant": "July accrual",
                        "Notes": "AOPS-TEST|2026-07",
                    },
                ],
            )
            self.assertEqual(
                autonomy.future_dated_source_exceptions(root, today=date(2026, 7, 30)),
                [],
            )

    def test_blocks_future_month_aops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_index(
                root,
                [
                    {
                        "BaselaneId": "future-aops",
                        "ISODate": "2026-08-31",
                        "Amount": "-100",
                        "Merchant": "August accrual",
                        "Notes": "AOPS-TEST|2026-08",
                    }
                ],
            )
            exceptions = autonomy.future_dated_source_exceptions(root, today=date(2026, 7, 30))
            self.assertEqual(len(exceptions), 1)
            self.assertEqual(exceptions[0]["id"], "future-dated-source-future-aops")

    def test_stale_weekly_aggregate_does_not_create_circular_source_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            reports.mkdir()
            (reports / "baselane_weekly_cf_statement_sync_report.json").write_text(
                '{"status":"review","effective_status":"review"}\n',
                encoding="utf-8",
            )
            report = autonomy.build_report(root)
            self.assertEqual(report["exception_count"], 0)
            self.assertFalse(report["downstream_hold"])
            self.assertEqual(report["status"], "ok")
            self.assertTrue(report["weekly_cf_blocked_by_effective_gate"])


class NoDaoMortgageSourceGuardTests(unittest.TestCase):
    def test_explicit_generic_eco_property_does_not_inherit_account_policy(self):
        row = {
            "Account": "85-104 Alawa Pl OH",
            "Date": "July 29, 2026",
            "Merchant": "85-104 Alawa Pl Mortgage Principal",
            "Description": "loanDepot.com, L | Invoices",
            "Amount": "-1364.06",
            "Type": "Loan Payments & Capex",
            "Category": "Mortgage Principal Payments",
            "Property": "Mining, Sales, Consulting, and PM",
        }
        self.assertEqual(autonomy.raw_no_dao_mortgage_violation_reason(row), "")

    def test_blank_property_falls_back_to_covered_account(self):
        row = {
            "Account": "85-104 Alawa Pl OH",
            "Date": "July 29, 2026",
            "Merchant": "loanDepot.com, L",
            "Description": "Mortgage payment",
            "Amount": "-3144.52",
            "Type": "Loan Payments & Capex",
            "Category": "Mortgage Payments",
            "Property": "",
        }
        self.assertNotEqual(autonomy.raw_no_dao_mortgage_violation_reason(row), "")

    def test_accepts_approved_madison_principal_curtailment(self):
        row = {
            "Account": "90 Madison Ave",
            "Date": "July 21, 2025",
            "Merchant": "90 Madison | approved 2025-06 NOI principal curtailment",
            "Description": "CITADEL SERV PMT | MTGE PAYMT | 1910008671",
            "Amount": "-750",
            "Type": "Loan Payments & Capex",
            "Category": "Mortgage Principal Payments",
            "Property": "90 Madison Ave",
        }
        self.assertEqual(autonomy.raw_no_dao_mortgage_violation_reason(row), "")

    def test_accepts_canonical_madison_escrow_components(self):
        cases = (
            ("90 Madison | general escrow | 2024-11", "General Escrow Payments"),
            ("90 Madison | city/state/local tax escrow | 2024-11", "City, State, & Local Taxes"),
            ("90 Madison | rental dwelling escrow | 2024-11", "Rental Dwelling"),
        )
        for merchant, category in cases:
            with self.subTest(category=category):
                row = {
                    "Account": "90 Madison Ave",
                    "Date": "November 12, 2024",
                    "Merchant": merchant,
                    "Description": "CITADEL SERV PMT | MTGE PAYMT | 1910008671",
                    "Amount": "-100",
                    "Type": "Manual",
                    "Category": "Transfers & Other" if category == "General Escrow Payments" else "Operating Expenses",
                    "Sub-category": category,
                    "Property": "90 Madison Ave",
                }
                self.assertEqual(autonomy.raw_no_dao_mortgage_violation_reason(row), "")

    def test_accepts_canonical_alawa_escrow_components(self):
        cases = (
            ("85-104 Alawa Pl Mortgage Escrow - General", "General Escrow Payments"),
            ("85-104 Alawa Pl Mortgage Escrow - Property Taxes", "Taxes"),
            ("85-104 Alawa Pl Mortgage Escrow - Insurance", "Insurance"),
        )
        for merchant, category in cases:
            with self.subTest(category=category):
                row = {
                    "Account": "85-104 Alawa Pl Operations",
                    "Date": "July 29, 2026",
                    "Merchant": merchant,
                    "Description": "loanDepot.com, L | Invoices",
                    "Amount": "-100",
                    "Type": "Operating Expenses",
                    "Category": category,
                    "Property": "85-104 Alawa Pl",
                }
                self.assertEqual(autonomy.raw_no_dao_mortgage_violation_reason(row), "")

    def test_still_blocks_unsplit_madison_mortgage_row(self):
        row = {
            "Account": "90 Madison Ave",
            "Date": "November 12, 2024",
            "Merchant": "CITADEL SERV PMT",
            "Description": "MTGE PAYMT | 1910008671",
            "Amount": "-3355.98",
            "Type": "Loan Payments & Capex",
            "Category": "Mortgage Payments",
            "Property": "90 Madison Ave",
        }
        self.assertNotEqual(autonomy.raw_no_dao_mortgage_violation_reason(row), "")


if __name__ == "__main__":
    unittest.main()
