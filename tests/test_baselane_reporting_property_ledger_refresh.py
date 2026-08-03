import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import baselane_reporting_property_ledger_refresh as refresh


class ReportingPropertyLedgerRefreshTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, Path, Path, Path]:
        source = root / "reporting.csv"
        source.write_text("Date,Property,Amount\n2026-07-01,Test Property,100\n", encoding="utf-8")
        real_estate = root / "Real Estate"
        real_estate.mkdir()
        authority = root / "authority.json"
        authority.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "issue_count": 0,
                    "reporting_ledger": str(source),
                    "reporting_ledger_sha256": refresh.sha256_file(source),
                }
            ),
            encoding="utf-8",
        )
        return source, real_estate, authority, root / "split.json", root / "refresh.json"

    @staticmethod
    def split_report(source: Path, *, current: bool, write_attempted: bool) -> dict:
        return {
            "status": "ok",
            "issue_count": 0,
            "source": str(source),
            "source_sha256": refresh.sha256_file(source),
            "total_row_count": 1,
            "deduped_row_count": 1,
            "exact_duplicate_extra_row_count": 0,
            "planned_write_count": 1,
            "planned_row_count": 1,
            "output_plan_digest": "a" * 64,
            "output_current_count": 1 if current else 0,
            "output_missing_count": 0 if current else 1,
            "output_stale_count": 0,
            "output_unreadable_count": 0,
            "output_mismatch_count": 0 if current else 1,
            "unresolved_property_count": 0,
            "deferred_acquisition_property_count": 0,
            "excluded_write_skipped_count": 0,
            "eco_company_revenue_excluded_row_count": 0,
            "citadel_statement_split_applied_count": 0,
            "write_attempted": write_attempted,
            "delete_attempted": False,
        }

    def test_apply_requires_matching_authority_and_verifies_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source, real_estate, authority, split_report, _report = self.fixture(Path(tmpdir))
            preview = self.split_report(source, current=False, write_attempted=False)
            final = self.split_report(source, current=True, write_attempted=True)

            def fake_split_main(argv):
                report_path = Path(argv[argv.index("--report") + 1])
                report_path.write_text(json.dumps(final), encoding="utf-8")
                return 0

            with patch.object(refresh.split, "build_report", return_value=preview), patch.object(
                refresh.split, "main", side_effect=fake_split_main
            ):
                report = refresh.build_refresh_report(
                    source=source,
                    real_estate_root=real_estate,
                    authority_report=authority,
                    split_report=split_report,
                    apply=True,
                )

            self.assertEqual(report["status"], "ok")
            self.assertTrue(report["authority_verified"])
            self.assertTrue(report["apply_attempted"])
            self.assertEqual(report["preview"]["output_mismatch_count"], 1)
            self.assertEqual(report["final"]["output_mismatch_count"], 0)

    def test_authority_digest_mismatch_blocks_property_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source, real_estate, authority, split_report, _report = self.fixture(Path(tmpdir))
            payload = json.loads(authority.read_text(encoding="utf-8"))
            payload["reporting_ledger_sha256"] = "0" * 64
            authority.write_text(json.dumps(payload), encoding="utf-8")

            with patch.object(refresh.split, "main") as split_main:
                report = refresh.build_refresh_report(
                    source=source,
                    real_estate_root=real_estate,
                    authority_report=authority,
                    split_report=split_report,
                    apply=True,
                )

            self.assertEqual(report["status"], "review")
            self.assertFalse(report["apply_attempted"])
            split_main.assert_not_called()
            self.assertIn("authority reporting-ledger digest", " ".join(report["issues"]))


if __name__ == "__main__":
    unittest.main()
