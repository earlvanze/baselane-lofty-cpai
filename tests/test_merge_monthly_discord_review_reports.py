import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from discord_summary_routing_policy import review_destination, review_route  # noqa: E402
from merge_monthly_discord_review_reports import (  # noqa: E402
    MergeValidationError,
    build_merged_report,
)
from send_monthly_discord_review_drafts import plan_digest  # noqa: E402


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def plan_payload() -> dict:
    destination = review_destination()
    records = []
    for property_name in ("Property A", "Property B"):
        message = f"Property Update: {property_name}\n\n## Monthly Cash Position (2026-07)\n\nCurrent."
        records.append(
            {
                "property_name": property_name,
                "message": message,
                "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
                "financial_review_blocked": False,
                "financial_review_blockers": [],
                **review_route(property_name),
            }
        )
    return {
        "generated_at": "2026-08-02T22:00:00Z",
        "status": "ok",
        "run_month": "2026-07",
        "record_count": 2,
        "authoritative_active_property_count": 3,
        "authoritative_reporting_target_count": 2,
        "review_destination": destination,
        **destination,
        "records": records,
    }


def roster_payload() -> dict:
    return {
        "status": "ok",
        "issue_count": 0,
        "run_month": "2026-07",
        "physical_property_count": 3,
        "authoritative_active_property_count": 3,
        "expected_active_property_count": 3,
        "reporting_target_count": 2,
        "authoritative_reporting_target_count": 2,
        "expected_reporting_target_count": 2,
        "physical_properties": [{}, {}, {}],
        "reporting_targets": [
            {"property_name": "Property A", "physical_property_count": 1},
            {"property_name": "Property B", "physical_property_count": 2},
        ],
    }


def coverage_payload() -> dict:
    destination = review_destination()
    names = ["Property A", "Property B"]
    return {
        "generated_at": "2026-08-02T23:00:00Z",
        "status": "ok",
        "issue_count": 0,
        "run_month": "2026-07",
        "authoritative_active_property_count": 3,
        "authoritative_reporting_target_count": 2,
        "current_plan_record_count": 2,
        "covered_reporting_target_count": 2,
        "planned_reporting_targets": names,
        "covered_reporting_targets": names,
        "approval_state": "awaiting_separate_exact_human_approval_per_property_or_dao",
        "destination_class": destination["destination_class"],
        "guild_id": destination["guild_id"],
        "forum_id": destination["forum_id"],
        "owner_email_sent": False,
        "lofty_guild_summary_sent": False,
    }


def receipt(property_name: str, thread_id: str, message_sha256: str, status: str = "ok") -> dict:
    destination = review_destination()
    return {
        "property_name": property_name,
        "status": status,
        "stage": "complete" if status == "ok" else "thread_create",
        "guild_id": destination["guild_id"],
        "forum_id": destination["forum_id"],
        "forum_target": destination["target"],
        "discord_account_id": destination["discord_account_id"],
        "thread_name": property_name,
        "thread_name_normalized": property_name.lower(),
        "thread_id": thread_id if status == "ok" else None,
        "target": f"channel:{thread_id}" if status == "ok" else None,
        "message_sha256": message_sha256,
        "header_receipt": {"messageId": f"h-{thread_id}"} if status == "ok" else None,
        "body_receipt": {"messageId": f"b-{thread_id}"} if status == "ok" else None,
    }


def source_report(results: list[dict], digest: str | None = None) -> dict:
    destination = review_destination()
    return {
        "status": "ok",
        "record_count": len(results),
        "plan_digest": digest,
        "review_destination": destination,
        **destination,
        "results": results,
    }


class MergeMonthlyDiscordReviewReportsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plan = self.root / "plan.json"
        self.roster = self.root / "roster.json"
        self.coverage = self.root / "coverage.json"
        self.old_source = self.root / "old.json"
        self.current_source = self.root / "current.json"
        self.verification = self.root / "verification.json"
        self.plan_data = plan_payload()
        write_json(self.plan, self.plan_data)
        write_json(self.roster, roster_payload())
        write_json(self.coverage, coverage_payload())
        old_hash = hashlib.sha256(b"old Property A body").hexdigest()
        write_json(self.old_source, source_report([receipt("Property A", "1528377998783418468", old_hash)]))
        current_hash = self.plan_data["records"][1]["message_sha256"]
        write_json(
            self.current_source,
            source_report(
                [
                    receipt("Property B", "1528378014755323935", current_hash, status="failed"),
                    receipt("Property B", "1528378014755323935", current_hash),
                ],
                plan_digest(self.plan_data),
            ),
        )
        write_json(
            self.verification,
            {
                "generated_at": "2026-08-02T22:30:00Z",
                "status": "ok",
                "issue_count": 0,
                "checked_channel_count": 1,
                "channels": [
                    {
                        "property_name": "Property A",
                        "target": "channel:1528377998783418468",
                        "classifications": ["header:receipt", "body:current"],
                        "issues": [],
                    }
                ],
            },
        )

    def tearDown(self):
        self.temp.cleanup()

    def build(self) -> dict:
        return build_merged_report(
            plan_path=self.plan,
            roster_path=self.roster,
            coverage_path=self.coverage,
            source_paths=[self.old_source, self.current_source],
            verification_paths=[self.verification],
            account="default",
            expected_physical_count=3,
            expected_reporting_count=2,
        )

    def test_merges_stale_live_verified_and_current_plan_bound_receipts(self):
        result = self.build()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["physical_property_count"], 3)
        self.assertEqual(result["reporting_target_count"], 2)
        self.assertEqual(result["record_count"], 2)
        self.assertEqual(result["sent_or_verified_count"], 2)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(
            result["results"][0]["current_plan_binding"]["method"],
            "discord_live_body_read",
        )
        self.assertEqual(
            result["results"][1]["current_plan_binding"]["method"],
            "source_plan_digest_and_message_sha256",
        )
        self.assertEqual(
            result["results"][0]["message_sha256"],
            self.plan_data["records"][0]["message_sha256"],
        )
        self.assertFalse(result["human_approval_received"])
        self.assertFalse(result["owner_email_approved"])
        self.assertFalse(result["owner_email_sent"])

    def test_rejects_stale_receipt_without_live_body_verification(self):
        with self.assertRaisesRegex(MergeValidationError, "stale_receipt_live_verification_missing:Property A"):
            build_merged_report(
                plan_path=self.plan,
                roster_path=self.roster,
                coverage_path=self.coverage,
                source_paths=[self.old_source, self.current_source],
                verification_paths=[],
                account="default",
                expected_physical_count=3,
                expected_reporting_count=2,
            )

    def test_rejects_coverage_that_omits_a_reporting_target(self):
        coverage = coverage_payload()
        coverage["covered_reporting_target_count"] = 1
        coverage["covered_reporting_targets"] = ["Property A"]
        write_json(self.coverage, coverage)

        with self.assertRaises(MergeValidationError) as raised:
            self.build()

        self.assertIn("coverage_covered_reporting_target_count_mismatch", raised.exception.issues)
        self.assertIn("coverage_covered_reporting_targets_mismatch", raised.exception.issues)


if __name__ == "__main__":
    unittest.main()
