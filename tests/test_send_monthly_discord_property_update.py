import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "send_monthly_discord_property_update.py"
sys.path.insert(0, str(ROOT / "scripts"))

from discord_summary_routing_policy import review_destination, review_route  # noqa: E402


class MonthlyDiscordPropertyUpdateSendTests(unittest.TestCase):
    def write_openclaw(self, root: Path) -> tuple[Path, Path]:
        calls = root / "calls.jsonl"
        openclaw = root / "openclaw"
        openclaw.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"with open({str(calls)!r}, 'a') as handle: handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print(json.dumps({'ok': True, 'messageId': '1528381121837142079'}))\n",
            encoding="utf-8",
        )
        openclaw.chmod(0o755)
        return openclaw, calls

    def write_guild_report(self, root: Path) -> tuple[Path, Path, str]:
        message = "Property Update: 917 Pawnee Ave\n\nFinancial summary.\n"
        message_file = root / "message.md"
        guild_report = root / "guild.json"
        message_file.write_text(message, encoding="utf-8")
        guild_report.write_text(
            json.dumps(
                {
                    "status": "prepared_not_posted",
                    "run_month": "2026-07",
                    "guild_id": "847877825373012018",
                    "target": "channel:1362189256163856594",
                    "message_file": str(message_file),
                    "selected": {"property_name": "917 Pawnee Ave, Flint, MI"},
                }
            ),
            encoding="utf-8",
        )
        return guild_report, message_file, message

    def test_live_all_plan_send_is_forbidden_without_invoking_openclaw(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            openclaw, calls = self.write_openclaw(root)
            report = root / "report.json"
            plan = root / "plan.json"
            message = "Property Update: Test House\n\nFacts."
            destination = review_destination()
            plan.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "run_month": "2026-07",
                        "review_destination": destination,
                        **destination,
                        "records": [
                            {
                                "property_name": "Test House",
                                "message": message,
                                "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
                                **review_route("Test House"),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--plan",
                    str(plan),
                    "--report",
                    str(report),
                    "--openclaw-bin",
                    str(openclaw),
                    "--send",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(calls.exists())
            self.assertEqual(
                json.loads(report.read_text())["issue"],
                "live_all_plan_send_forbidden_use_earlcoin_review_agent",
            )

    def test_live_lofty_publication_requires_human_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            openclaw, calls = self.write_openclaw(root)
            guild_report, _message_file, _message = self.write_guild_report(root)
            report = root / "report.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--guild-report",
                    str(guild_report),
                    "--report",
                    str(report),
                    "--openclaw-bin",
                    str(openclaw),
                    "--send",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(calls.exists())
            payload = json.loads(report.read_text())
            self.assertEqual(payload["issue"], "lofty_publication_human_approval_required")
            self.assertEqual(payload["required_approval"]["approval_scope"], "lofty_guild_financial_summary_publish")

    def test_wrong_message_digest_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            openclaw, calls = self.write_openclaw(root)
            guild_report, _message_file, _message = self.write_guild_report(root)
            report = root / "report.json"
            approval = root / "approval.json"
            approval.write_text(
                json.dumps(
                    {
                        "approved": True,
                        "approval_scope": "lofty_guild_financial_summary_publish",
                        "guild_id": "847877825373012018",
                        "run_month": "2026-07",
                        "property_name": "917 Pawnee Ave, Flint, MI",
                        "target": "channel:1362189256163856594",
                        "message_sha256": "0" * 64,
                        "approved_by": "operator",
                        "approved_at": "2026-08-02T12:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--guild-report",
                    str(guild_report),
                    "--report",
                    str(report),
                    "--approval-file",
                    str(approval),
                    "--openclaw-bin",
                    str(openclaw),
                    "--send",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(calls.exists())
            self.assertIn("approval_message_sha256_mismatch", json.loads(report.read_text())["issues"])

    def test_hash_bound_approval_allows_one_lofty_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            openclaw, calls = self.write_openclaw(root)
            guild_report, _message_file, message = self.write_guild_report(root)
            report = root / "report.json"
            approval = root / "approval.json"
            approval.write_text(
                json.dumps(
                    {
                        "approved": True,
                        "approval_scope": "lofty_guild_financial_summary_publish",
                        "guild_id": "847877825373012018",
                        "run_month": "2026-07",
                        "property_name": "917 Pawnee Ave, Flint, MI",
                        "target": "channel:1362189256163856594",
                        "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
                        "approved_by": "operator",
                        "approved_at": "2026-08-02T12:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--guild-report",
                    str(guild_report),
                    "--report",
                    str(report),
                    "--approval-file",
                    str(approval),
                    "--openclaw-bin",
                    str(openclaw),
                    "--send",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(calls.read_text().splitlines()), 1)
            payload = json.loads(report.read_text())
            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["approval_verified"])


if __name__ == "__main__":
    unittest.main()
