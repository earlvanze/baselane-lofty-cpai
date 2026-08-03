import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import baselane_reporting_ledger_authority as authority  # noqa: E402


FIELDS = [
    "Date",
    "Amount",
    "Merchant",
    "Description",
    "Account",
    "Property",
    "Type",
    "Category",
    "Sub-category",
    "Unit",
    "Notes",
]


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def digest(path: Path) -> str:
    value, _row_count = authority.csv_digest(path)
    return value


class BaselaneReportingLedgerAuthorityTests(unittest.TestCase):
    def build_chain(self, root: Path) -> tuple[Path, Path]:
        reports = root / "reports"
        raw = root / "ECO Systems General Ledger.csv"
        deduped = reports / "baselane_weekly_deduped_reporting_ledger.csv"
        safe = reports / "baselane_weekly_safe_category_reporting_ledger.csv"
        pm_clean = reports / "baselane_weekly_clean_reporting_ledger.csv"
        reporting = reports / "baselane_weekly_no_dao_mortgage_clean_reporting_ledger.csv"

        rent = {
            "Date": "2026-07-01",
            "Amount": "1000.00",
            "Merchant": "Tenant",
            "Description": "July rent",
            "Account": "90 Madison",
            "Property": "90 Madison Ave",
            "Type": "Transaction",
            "Category": "Operating Income",
            "Sub-category": "Rents",
            "Unit": "",
            "Notes": "",
        }
        forbidden_mortgage = {
            "Date": "2026-07-29",
            "Amount": "-300.66",
            "Merchant": "85-104 Alawa Pl Mortgage Escrow - General",
            "Description": "loanDepot.com, L | Invoices",
            "Account": "ECO Systems",
            "Property": "85-104 Alawa Pl",
            "Type": "Manual",
            "Category": "Transfers & Other",
            "Sub-category": "General Escrow Payments",
            "Unit": "",
            "Notes": "",
        }
        write_csv(raw, [rent, rent, forbidden_mortgage])
        write_csv(deduped, [rent, forbidden_mortgage])
        write_csv(safe, [rent, forbidden_mortgage])
        write_csv(pm_clean, [rent, forbidden_mortgage])
        write_csv(reporting, [rent])

        (reports / "baselane_monthly_reporting_raw_duplicate_report.json").write_text(
            json.dumps(
                {
                    "ledger": str(raw),
                    "ledger_rows": 3,
                    "deduped_reporting_ledger": str(deduped),
                    "deduped_reporting_ledger_row_count": 2,
                    "exact_duplicate_extra_row_count": 1,
                }
            ),
            encoding="utf-8",
        )
        (reports / "baselane_ecogl_safe_category_apply_report.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "apply",
                    "output_written": True,
                    "ledger": str(deduped),
                    "out_ledger": str(safe),
                    "input_digest": digest(deduped),
                    "output_digest": digest(safe),
                }
            ),
            encoding="utf-8",
        )
        (reports / "baselane_first_day_pm_fee_quarantine_report.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "apply",
                    "output_written": True,
                    "reporting_output_clean": True,
                    "remaining_first_day_pm_fee_count": 0,
                    "quarantined_row_count": 0,
                    "ledger_csv": str(safe),
                    "out_ledger": str(pm_clean),
                    "input_digest": digest(safe),
                    "output_digest": digest(pm_clean),
                }
            ),
            encoding="utf-8",
        )
        (reports / "baselane_no_dao_mortgage_reporting_quarantine.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "apply",
                    "output_written": True,
                    "reporting_output_clean": True,
                    "remaining_no_dao_mortgage_row_count": 0,
                    "quarantined_row_count": 1,
                    "ledger_csv": str(pm_clean),
                    "out_ledger": str(reporting),
                    "input_digest": digest(pm_clean),
                    "output_digest": digest(reporting),
                }
            ),
            encoding="utf-8",
        )
        return raw, reporting

    def test_accepts_current_verified_reporting_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw, reporting = self.build_chain(root)

            report = authority.audit_authority(
                root=root,
                raw_ledger=raw,
                reporting_ledger=reporting,
                refresh_attempted=False,
                refresh_results=[],
            )

            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["issue_count"], 0)
            self.assertEqual(report["raw_row_count"], 3)
            self.assertEqual(report["reporting_row_count"], 1)
            self.assertEqual(report["exact_duplicate_extra_row_count"], 1)
            self.assertEqual(report["no_dao_mortgage_quarantined_row_count"], 1)

    def test_rejects_reporting_ledger_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw, reporting = self.build_chain(root)
            rows = authority.read_csv(reporting)[1]
            rows[0]["Amount"] = "999.00"
            write_csv(reporting, rows)

            report = authority.audit_authority(
                root=root,
                raw_ledger=raw,
                reporting_ledger=reporting,
                refresh_attempted=False,
                refresh_results=[],
            )

            self.assertEqual(report["status"], "review")
            self.assertIn("no-DAO mortgage output digest mismatch", report["issues"])


if __name__ == "__main__":
    unittest.main()
