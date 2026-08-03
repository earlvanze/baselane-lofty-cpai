from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from baselane_monthly_readiness_report import (
    comms_root_for,
    live_capture_reconcile_action,
    monthly_run_is_prior_run_evidence,
    report_target_matches_run_month,
    report_target_month,
    scheduler_communication_observability_issues,
    scheduler_financial_pipeline_issues,
)


class BaselaneMonthlyReadinessReportTests(unittest.TestCase):
    def test_live_capture_count_is_scoped_to_mutation_ready_lofty_targets(self):
        action = live_capture_reconcile_action(
            {
                "target_count": 20,
                "check_ok_count": 20,
                "mismatch_count": 0,
                "records": [],
            },
            "UPDATES.md",
        )

        self.assertIn("20/20 mutation-ready Lofty checks pass", action)
        self.assertNotIn("20 active properties", action)

    def test_prior_failed_run_is_identified_for_active_run(self):
        report = {
            "status": "failed",
            "started_at": "2026-08-02T14:30:00Z",
            "generated_at": "2026-08-02T14:40:00Z",
        }

        self.assertTrue(
            monthly_run_is_prior_run_evidence(report, "2026-08-02T14:44:30Z")
        )

    def test_current_run_report_is_not_identified_as_prior(self):
        report = {
            "status": "failed",
            "started_at": "2026-08-02T14:44:30Z",
            "generated_at": "2026-08-02T14:44:30Z",
        }

        self.assertFalse(
            monthly_run_is_prior_run_evidence(report, "2026-08-02T14:44:30Z")
        )

    def test_generated_at_fallback_handles_legacy_report(self):
        report = {
            "status": "review",
            "generated_at": "2026-08-02T14:40:00Z",
        }

        self.assertTrue(
            monthly_run_is_prior_run_evidence(report, "2026-08-02T14:44:30Z")
        )

    def test_missing_or_invalid_run_boundary_fails_closed(self):
        report = {
            "status": "failed",
            "started_at": "2026-08-02T14:30:00Z",
            "generated_at": "2026-08-02T14:40:00Z",
        }

        self.assertFalse(monthly_run_is_prior_run_evidence(report, None))
        self.assertFalse(monthly_run_is_prior_run_evidence(report, "not-a-timestamp"))

    def test_report_target_month_prefers_explicit_month(self):
        report = {"month": "2026-07", "run_month": "2026-08"}

        self.assertEqual(report_target_month(report), "2026-07")
        self.assertTrue(report_target_matches_run_month(report, "2026-07"))
        self.assertFalse(report_target_matches_run_month(report, "2026-08"))

    def test_report_target_month_supports_statement_target(self):
        report = {"target_year": 2026, "target_month": 7}

        self.assertEqual(report_target_month(report), "2026-07")
        self.assertTrue(report_target_matches_run_month(report, "2026-07"))

    def test_scheduler_eod_delivery_proof_is_nonblocking_for_financial_pipeline(self):
        report = {
            "status": "review",
            "issues": [
                "eod_telegram:unexpected_report_status:failed",
                "eod_telegram:report_unexpected_value:telegram_send_ok=False",
            ],
            "jobs": [
                {"name": "daily_sync", "issues": []},
                {"name": "weekly_file_updates", "issues": []},
                {"name": "monthly_financials", "issues": []},
            ],
        }

        self.assertEqual(scheduler_financial_pipeline_issues(report), [])
        self.assertEqual(
            scheduler_communication_observability_issues(report),
            [
                "eod_telegram:report_unexpected_value:telegram_send_ok=False",
                "eod_telegram:unexpected_report_status:failed",
            ],
        )

    def test_scheduler_job_issue_remains_financial_pipeline_blocker(self):
        report = {
            "status": "review",
            "issues": ["daily_sync:unexpected_report_status:failed"],
            "jobs": [
                {"name": "daily_sync", "issues": ["unexpected_report_status:failed"]},
                {"name": "weekly_file_updates", "issues": []},
                {"name": "monthly_financials", "issues": []},
            ],
        }

        self.assertEqual(
            scheduler_financial_pipeline_issues(report),
            ["daily_sync:unexpected_report_status:failed"],
        )

    def test_missing_scheduler_jobs_fail_closed(self):
        report = {"status": "ok", "issues": [], "jobs": []}

        self.assertEqual(
            scheduler_financial_pipeline_issues(report),
            [
                "daily_sync:missing_scheduler_audit_job",
                "monthly_financials:missing_scheduler_audit_job",
                "weekly_file_updates:missing_scheduler_audit_job",
            ],
        )

    def test_comms_root_prefers_active_openclaw_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            openclaw = Path(tmp) / ".openclaw"
            repo_parent = openclaw / "workspace" / "repos"
            stale_repo_workspace = repo_parent / "workspace-lofty-vp" / "updates"
            active_workspace = openclaw / "workspace-lofty-vp" / "updates"
            stale_repo_workspace.mkdir(parents=True)
            active_workspace.mkdir(parents=True)

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("COMMS_WORKSPACE", None)
                self.assertEqual(comms_root_for(repo_parent), active_workspace.parent)


if __name__ == "__main__":
    unittest.main()
