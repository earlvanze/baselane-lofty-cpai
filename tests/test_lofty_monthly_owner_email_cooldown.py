import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lofty_monthly_owner_email_packet as owner_email


def packet(property_id: str, property_name: str) -> dict:
    return {
        "property_ids": [property_id],
        "source_properties": [
            {
                "property_id": property_id,
                "property_name": property_name,
            }
        ],
    }


class OwnerEmailCooldownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    def state_for(self, target: dict, sent_at: datetime) -> dict:
        key = owner_email.packet_property_send_keys(target)[0]
        return {"property_sent_at": {key: sent_at.isoformat()}}

    def test_property_is_held_until_seven_full_days_have_elapsed(self) -> None:
        target = packet("101", "101 Main St")
        state = self.state_for(target, self.now - timedelta(days=7) + timedelta(seconds=1))

        issues = owner_email.property_cooldown_issues([target], state, self.now)

        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0].startswith("property_email_cooldown_active:"))

    def test_property_is_released_at_exactly_seven_days(self) -> None:
        target = packet("101", "101 Main St")
        state = self.state_for(target, self.now - timedelta(days=7))

        self.assertEqual(owner_email.property_cooldown_issues([target], state, self.now), [])

    def test_cooldown_is_scoped_to_each_property(self) -> None:
        held = packet("101", "101 Main St")
        eligible = packet("202", "202 Main St")
        state = self.state_for(held, self.now - timedelta(days=1))

        issues, hold_keys = owner_email.property_cooldown_hold_keys(
            [held, eligible], state, self.now
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(hold_keys, set(owner_email.packet_property_send_keys(held)))
        self.assertTrue(hold_keys.isdisjoint(owner_email.packet_property_send_keys(eligible)))

    def test_stale_partial_runtime_map_is_reported_as_scope_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_map = root / owner_email.CANONICAL_RUNTIME_MAP_NAME
            candidate_packet = root / "candidate.json"
            runtime_map.write_text(
                json.dumps(
                    {
                        "properties": [
                            {"property_name": f"Property {index}", "lofty_property_id": str(index)}
                            for index in range(13)
                        ],
                        "records": [
                            {"property_name": f"Property {index}", "status": "mapped"}
                            for index in range(13)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            candidate_packet.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "run_month": "2026-07",
                        "authoritative_active_property_count": 32,
                        "authoritative_reporting_target_count": 30,
                        "property_count": 30,
                        "records": [],
                    }
                ),
                encoding="utf-8",
            )

            report = owner_email.build_report(
                runtime_map=runtime_map,
                recipients_csv=root / "recipients.csv",
                run_month="2026-07",
                sent_state_file=root / "sent.json",
                out_dir=root / "previews",
                subject="July update",
                dry_run=True,
                send=False,
                allow_portfolio_recipients=False,
                candidate_packet_report=candidate_packet,
                native_only=True,
            )

            self.assertEqual(report["status"], "review")
            self.assertEqual(report["authoritative_active_physical_property_count"], 32)
            self.assertEqual(report["authoritative_reporting_target_count"], 30)
            self.assertEqual(report["runtime_map_reporting_target_count"], 13)
            self.assertFalse(report["runtime_map_scope_coverage_ok"])
            self.assertTrue(
                any(issue.startswith("runtime_scope_coverage_mismatch:") for issue in report["issues"])
            )

    def test_exact_canonical_runtime_scope_supersedes_stale_lifecycle_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_map = root / owner_email.CANONICAL_RUNTIME_MAP_NAME
            candidate_packet = root / "candidate.json"
            runtime_map.write_text(
                json.dumps(
                    {
                        "portfolio_physical_property_count": 32,
                        "portfolio_reporting_target_count": 30,
                        "native_live_target_count": 26,
                        "native_unavailable_count": 4,
                        "properties": [
                            {"property_name": f"Property {index}", "lofty_property_id": str(index)}
                            for index in range(26)
                        ],
                        "records": [
                            {"property_name": f"Property {index}", "status": "mapped"}
                            for index in range(26)
                        ]
                        + [
                            {
                                "property_name": f"Non-native {index}",
                                "status": "unavailable_no_live_property_id",
                            }
                            for index in range(4)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            candidate_packet.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "run_month": "2026-07",
                        "authoritative_active_property_count": 32,
                        "authoritative_reporting_target_count": 30,
                        "property_count": 30,
                        "records": [],
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(owner_email.runtime_scope_is_authoritative(runtime_map, candidate_packet))

    def test_live_listing_guard_is_not_required_when_manager_action_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "live-update-capture.json"
            report_path.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "records": [
                            {
                                "property_name": "Known ID outside manager scope",
                                "lofty_property_id": "KNOWN-ID",
                                "status": "not_applicable_not_current_manager_property",
                                "live_capture_guard_applicable": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            statuses, issues, required = owner_email.load_live_update_guard_statuses(report_path)

            self.assertEqual(issues, [])
            self.assertTrue(required)
            self.assertTrue(statuses["KNOWN-ID"]["guard_ok"])
            self.assertFalse(statuses["KNOWN-ID"]["guard_applicable"])

    def test_approved_artifact_precedes_review_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approval = root / "2026-07-owner-update-approved.md"
            candidate = root / "2026-07-owner-update-review-candidate.md"
            approval.write_text("approved\n", encoding="utf-8")
            candidate.write_text("candidate\n", encoding="utf-8")
            summary = {
                "candidate_update_approval_target": str(approval),
                "candidate_update_source": str(candidate),
            }

            text, path, source_type, approved = owner_email.candidate_update_text(summary)

            self.assertEqual(text, "approved\n")
            self.assertEqual(path, str(approval))
            self.assertEqual(source_type, "approved_update_artifact")
            self.assertTrue(approved)

    def test_review_candidate_is_preview_source_but_not_send_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            candidate = Path(temp_dir) / "2026-07-owner-update-review-candidate.md"
            candidate.write_text("candidate\n", encoding="utf-8")
            summary = {"candidate_update_source": str(candidate)}

            text, path, source_type, approved = owner_email.candidate_update_text(summary)

            self.assertEqual(text, "candidate\n")
            self.assertEqual(path, str(candidate))
            self.assertEqual(source_type, "review_candidate_artifact")
            self.assertFalse(approved)
            self.assertEqual(owner_email.approved_update_text(summary), (None, None))

    def test_current_candidate_supersedes_stale_dropbox_update_for_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updates = root / "UPDATES.md"
            candidate = root / "2026-07-owner-update-review-candidate.md"
            runtime_map = root / owner_email.CANONICAL_RUNTIME_MAP_NAME
            candidate_packet = root / "candidate.json"
            updates.write_text(
                "## 2026-06-30\n\n- Property Update (06/30/2026):\nStale June update.\n",
                encoding="utf-8",
            )
            candidate.write_text(
                "## 2026-07-31\n\n"
                "- Property Update (07/31/2026):\n"
                "Current July candidate.\n\n"
                "Financial detail:\n\n"
                "## Monthly Cash Position (2026-07)\n\n"
                "Actual July 31 position:\n"
                "- Lofty Operating Reserve: $25.00\n"
                "- ECO Net DAO Funds (spendable cash held by ECO): $100.00\n",
                encoding="utf-8",
            )
            summary = {
                "eco_gl_column_e_status": "ok",
                "eco_gl_column_e_sum": 100.0,
                "total_dao_spendable_cash_status": "ok",
                "total_dao_spendable_cash": 100.0,
                "eco_held_unrestricted_cash_status": "ok",
                "eco_held_unrestricted_cash": 100.0,
                "open_accrued_obligations_status": "ok",
                "open_accrued_obligations": 0.0,
                "open_accrued_obligations_by_kind": {},
                "intercompany_payable_status": "ok_no_open_position",
                "dao_accounts_payable_to_eco": 0.0,
                "eco_accounts_receivable_from_dao": 0.0,
            }
            runtime_map.write_text(
                json.dumps(
                    {
                        "portfolio_physical_property_count": 1,
                        "portfolio_reporting_target_count": 1,
                        "native_live_target_count": 1,
                        "native_unavailable_count": 0,
                        "properties": [
                            {
                                "property_name": "101 Main St",
                                "lofty_property_id": "101",
                                "updates_md": str(updates),
                            }
                        ],
                        "records": [{"property_name": "101 Main St", "status": "mapped"}],
                    }
                ),
                encoding="utf-8",
            )
            candidate_packet.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "run_month": "2026-07",
                        "authoritative_active_property_count": 1,
                        "authoritative_reporting_target_count": 1,
                        "property_count": 1,
                        "manifest_record_count": 1,
                        "records": [
                            {
                                "property_name": "101 Main St",
                                "lofty_property_id": "101",
                                "update_candidate": str(candidate),
                                "update_approval_target": str(root / "missing-approved.md"),
                                "monthly_financial_summary": summary,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            properties, issues, unavailable, count = owner_email.load_properties(
                runtime_map,
                candidate_packet_report=candidate_packet,
                run_month="2026-07",
            )

            self.assertEqual(issues, [])
            self.assertEqual(unavailable, [])
            self.assertEqual(count, 1)
            self.assertEqual(len(properties), 1)
            self.assertEqual(properties[0]["latest_update_date"], "2026-07-31")
            self.assertEqual(properties[0]["latest_update_source_type"], "review_candidate_artifact")
            self.assertFalse(properties[0]["latest_update_approved"])
            self.assertIn("Current July candidate.", properties[0]["latest_update_body"])
            self.assertNotIn("Stale June update.", properties[0]["latest_update_body"])

            report = owner_email.build_report(
                runtime_map=runtime_map,
                recipients_csv=root / "recipients.csv",
                run_month="2026-07",
                sent_state_file=root / "sent.json",
                out_dir=root / "previews",
                subject="July update",
                dry_run=True,
                send=False,
                allow_portfolio_recipients=False,
                candidate_packet_report=candidate_packet,
                native_only=True,
            )

            self.assertEqual(report["native_lofty_owner_email_preview_count"], 1)
            self.assertTrue(report["native_lofty_owner_email_property_coverage_ok"])
            self.assertEqual(report["native_lofty_owner_email_approval_pending_property_count"], 1)
            self.assertEqual(report["native_lofty_owner_email_eligible_property_count"], 0)
            self.assertEqual(
                report["primary_blocker"]["class"],
                "exact_current_owner_update_approval_missing",
            )


if __name__ == "__main__":
    unittest.main()
