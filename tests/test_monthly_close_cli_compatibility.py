import importlib.util
import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class MonthlyCloseCliCompatibilityTests(unittest.TestCase):
    def test_vendor_reconciliation_cutoff_parser(self):
        module = load_module(
            "vendor_reconciliation_cli_compat",
            "scripts/baselane_dao_vendor_property_reconciliation.py",
        )
        self.assertEqual(str(module.parse_iso_date("2026-07-30")), "2026-07-30")
        with self.assertRaises(argparse.ArgumentTypeError):
            module.parse_iso_date("07/30/2026")

    def test_transfer_report_carries_reporting_cutoff(self):
        source = (ROOT / "scripts" / "baselane_lofty_transfer_requirements.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"--reporting-cutoff-date"', source)
        self.assertIn('"reporting_cutoff_date": getattr(args, "reporting_cutoff_date", None)', source)

    def test_empty_nonproperty_plan_skips_live_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            report = Path(tmp) / "report.json"
            plan.write_text(json.dumps({"rows": []}), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "baselane_nonproperty_category_apply.py"),
                    "--plan",
                    str(plan),
                    "--report",
                    str(report),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["live_fetch_skipped_reason"], "empty_plan")

    def test_source_cash_cli_accepts_reporting_cutoff(self):
        module = load_module(
            "source_cash_cli_compat",
            "scripts/baselane_daily_source_cash_balance_audit.py",
        )
        report = {
            "status": "ok",
            "mode": "audit",
            "month": "2026-07",
            "checked_property_count": 0,
            "update_count": 0,
            "violation_count": 0,
        }
        with (
            patch.object(module, "build_report", return_value=report) as build_report,
            patch.object(module, "write_json"),
            patch.object(module, "clear_progress"),
            patch.object(
                sys,
                "argv",
                [
                    "audit",
                    "--month",
                    "2026-07",
                    "--reporting-cutoff-date",
                    "2026-07-30",
                ],
            ),
        ):
            self.assertEqual(module.main(), 0)
        self.assertEqual(
            build_report.call_args.kwargs["reporting_cutoff_date"],
            "2026-07-30",
        )

    def test_guarded_apply_cli_accepts_listing_policy(self):
        module = load_module(
            "guarded_apply_cli_compat",
            "scripts/lofty_monthly_guarded_apply.py",
        )
        policy = ROOT / "config" / "lofty_listing_update_policy.json"
        with patch.object(
            sys,
            "argv",
            [
                "guarded-apply",
                "--index-csv",
                "/missing/index.csv",
                "--run-month",
                "2026-07",
                "--updates-guard",
                "/missing/updates-guard.py",
                "--live-guard",
                "/missing/live-guard.py",
                "--report",
                "/tmp/guarded-apply-cli-test.json",
                "--listing-update-policy",
                str(policy),
            ],
        ):
            self.assertEqual(module.main(), 2)


if __name__ == "__main__":
    unittest.main()
