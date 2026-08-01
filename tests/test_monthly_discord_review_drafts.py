import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SENDER = ROOT / "scripts" / "send_monthly_discord_review_drafts.py"
BRIDGE = ROOT / "scripts" / "run_monthly_discord_review_via_agent.py"


def write_plan(path: Path) -> None:
    message = "Property Update: Test House\n\nMonthly facts."
    import hashlib

    path.write_text(
        json.dumps(
            {
                "status": "ok",
                "run_month": "2026-07",
                "records": [
                    {
                        "property_name": "Test House",
                        "target": "channel:123",
                        "message": message,
                        "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class MonthlyDiscordReviewDraftTests(unittest.TestCase):
    def test_sender_posts_header_then_standalone_draft_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            report = root / "report.json"
            calls = root / "calls.jsonl"
            openclaw = root / "openclaw"
            write_plan(plan)
            openclaw.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"p={str(calls)!r}\n"
                "with open(p, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "print(json.dumps({'ok': True, 'messageId': str(len(open(p).readlines()))}))\n",
                encoding="utf-8",
            )
            openclaw.chmod(0o755)
            command = [
                sys.executable,
                str(SENDER),
                "--plan",
                str(plan),
                "--report",
                str(report),
                "--openclaw-bin",
                str(openclaw),
                "--account",
                "default",
                "--send",
            ]
            first = subprocess.run(command, check=False, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            sent = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertEqual(len(sent), 2)
            self.assertEqual(sent[0][sent[0].index("--account") + 1], "default")
            self.assertEqual(sent[1][sent[1].index("--account") + 1], "default")
            self.assertIn("No owner email has been sent", sent[0][sent[0].index("--message") + 1])
            self.assertTrue(sent[1][sent[1].index("--message") + 1].startswith("[DRAFT FOR REVIEW - NOT EMAILED]"))

            second = subprocess.run(command + ["--resume-report", str(report)], check=False, text=True, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(len(calls.read_text().splitlines()), 2)
            self.assertEqual(json.loads(report.read_text())["results"][0]["status"], "ok_previous")

    def test_bridge_dry_run_does_not_invoke_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            report = root / "report.json"
            agent_report = root / "agent.json"
            write_plan(plan)
            result = subprocess.run(
                [
                    sys.executable,
                    str(BRIDGE),
                    "--plan",
                    str(plan),
                    "--report",
                    str(report),
                    "--agent-report",
                    str(agent_report),
                    "--sender-script",
                    str(SENDER),
                    "--dry-run",
                ],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(report.read_text())
            self.assertEqual(payload["status"], "ok_dry_run")
            self.assertFalse(payload["owner_email_sent"])

    def test_sender_refuses_review_plan_without_posting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            report = root / "report.json"
            write_plan(plan)
            payload = json.loads(plan.read_text())
            payload["status"] = "review"
            plan.write_text(json.dumps(payload), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SENDER), "--plan", str(plan), "--report", str(report), "--send"],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(report.read_text())["issue"], "plan_status_not_ok:review")

    def test_bridge_refuses_review_plan_without_invoking_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            report = root / "report.json"
            agent_report = root / "agent.json"
            openclaw_calls = root / "openclaw-calls"
            openclaw = root / "openclaw"
            write_plan(plan)
            payload = json.loads(plan.read_text())
            payload["status"] = "review"
            plan.write_text(json.dumps(payload), encoding="utf-8")
            openclaw.write_text(
                "#!/bin/sh\n"
                f"touch {str(openclaw_calls)!r}\n"
                "exit 99\n",
                encoding="utf-8",
            )
            openclaw.chmod(0o755)
            result = subprocess.run(
                [
                    sys.executable,
                    str(BRIDGE),
                    "--plan",
                    str(plan),
                    "--report",
                    str(report),
                    "--agent-report",
                    str(agent_report),
                    "--sender-script",
                    str(SENDER),
                    "--openclaw-bin",
                    str(openclaw),
                    "--send",
                ],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(openclaw_calls.exists())
            agent_payload = json.loads(agent_report.read_text())
            self.assertFalse(agent_payload["agent_invoked"])
            self.assertEqual(agent_payload["sender_status"], "review")


if __name__ == "__main__":
    unittest.main()
