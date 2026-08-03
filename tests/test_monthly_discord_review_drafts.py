import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SENDER = ROOT / "scripts" / "send_monthly_discord_review_drafts.py"
BRIDGE = ROOT / "scripts" / "run_monthly_discord_review_via_agent.py"
DEDUPE = ROOT / "scripts" / "dedupe_monthly_discord_review_drafts.py"
RECONCILE = ROOT / "scripts" / "reconcile_monthly_discord_review_drafts.py"
sys.path.insert(0, str(ROOT / "scripts"))

from discord_summary_routing_policy import (  # noqa: E402
    EARLCOIN_GUILD_ID,
    EARLCOIN_REVIEW_DISCORD_ACCOUNT,
    review_delivery_report_issues,
    review_destination,
    review_route,
)
from cleanup_monthly_discord_review_drafts import (  # noqa: E402
    is_old_batch_message,
    receipt_id,
    receipt_timestamp_ms,
)
from reconcile_monthly_discord_review_drafts import (  # noqa: E402
    fragments_match,
    reconciliation_status,
    split_for_fragment_count,
    transport_equivalent,
)


def write_plan(path: Path) -> None:
    message = "Property Update: Test House\n\nMonthly facts."
    import hashlib

    destination = review_destination()
    path.write_text(
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


class MonthlyDiscordReviewDraftTests(unittest.TestCase):
    def test_gateway_receipt_uses_last_fragment_as_anchor(self):
        receipt = {
            "raw": {
                "result": {
                    "receipt": {
                        "platformMessageIds": ["fragment-1", "fragment-2", "fragment-3"],
                        "sentAt": 1_754_000_123_456,
                    }
                }
            }
        }

        self.assertEqual(receipt_id(receipt), "fragment-3")
        self.assertEqual(receipt_timestamp_ms(receipt), 1_754_000_123_456)

    def test_reconcile_rebalances_three_discord_fragments_without_text_loss(self):
        text = "\n".join(
            [
                "[DRAFT FOR REVIEW - NOT EMAILED]",
                "",
                "Property Update: Test House",
                "",
                "## Cash Flow Snapshot (2026-07)",
                "",
                "Revenue: $1,000.00",
                "",
                "## Monthly Cash Position (2026-07)",
                "",
                "Actual July 31 position:",
                "",
                "- Spendable cash: $500.00",
                "- DAO A/P - Due to ECO: $100.00",
                "",
                "If anything looks wrong, please DM @earlvanze on Discord.",
            ]
        )
        parts = split_for_fragment_count(text, 3)
        self.assertEqual(len(parts), 3)
        self.assertEqual("\n".join(parts), text)

    def test_reconcile_accepts_discord_trailing_fragment_whitespace_normalization(self):
        expected = ["first section\n", "\nsecond section", "third section\n\n"]
        live = ["first section", "second section", "third section"]

        self.assertTrue(fragments_match(live, expected))
        self.assertFalse(fragments_match(["first section", "changed", "third section"], expected))

    def test_reconcile_accepts_transport_only_fragment_boundary_shifts(self):
        expected = "first line\nsecond line\nthird line"

        self.assertTrue(
            transport_equivalent(
                ["first line\nsecond", "line\nthird line"],
                expected,
            )
        )
        self.assertFalse(
            transport_equivalent(
                ["first line\nsecond", "line\nchanged line"],
                expected,
            )
        )

    def test_reconcile_accepts_a_verified_subset_of_the_authoritative_plan(self):
        receipts = [{"property_name": f"Property {index}"} for index in range(20)]
        checked = [{"property_name": f"Property {index}"} for index in range(20)]

        self.assertEqual(reconciliation_status(receipts, checked, []), "ok")
        self.assertEqual(reconciliation_status(receipts, checked[:-1], []), "review")
        self.assertEqual(reconciliation_status(receipts, checked, ["conflict"]), "review")
        self.assertEqual(reconciliation_status([], [], []), "review")

    def test_reconcile_property_filter_rejects_name_missing_from_send_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous_plan = root / "previous-plan.json"
            current_plan = root / "current-plan.json"
            send_report = root / "send-report.json"
            report = root / "report.json"
            calls = root / "calls"
            openclaw = root / "openclaw"
            import hashlib

            destination = review_destination()
            property_names = ["Test House"] + [
                f"Property {index:02d}" for index in range(1, 30)
            ]
            plan_payload = {
                "status": "ok",
                "run_month": "2026-07",
                "authoritative_active_property_count": 32,
                "authoritative_reporting_target_count": 30,
                "review_destination": destination,
                **destination,
                "records": [],
            }
            for property_name in property_names:
                message = (
                    f"Property Update: {property_name}\n\n"
                    "Monthly facts.\n\n## Monthly Cash Position\n\nCash facts."
                )
                plan_payload["records"].append(
                    {
                        "property_name": property_name,
                        "message": message,
                        "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
                        **review_route(property_name),
                    }
                )
            previous_plan.write_text(json.dumps(plan_payload), encoding="utf-8")
            current_plan.write_text(json.dumps(plan_payload), encoding="utf-8")
            send_report.write_text(
                json.dumps(
                    {
                        "review_destination": destination,
                        **destination,
                        "destination_class": "earlcoin_operator_review",
                        "delivery_mode": "earlcoin_operator_review_drafts",
                        "results": [
                            {
                                "property_name": property_name,
                                **review_route(property_name),
                                "forum_target": destination["target"],
                            }
                            for property_name in property_names
                        ],
                    }
                ),
                encoding="utf-8",
            )
            openclaw.write_text(
                "#!/bin/sh\n"
                f"touch {str(calls)!r}\n"
                "exit 99\n",
                encoding="utf-8",
            )
            openclaw.chmod(0o755)

            result = subprocess.run(
                [
                    sys.executable,
                    str(RECONCILE),
                    "--previous-plan",
                    str(previous_plan),
                    "--current-plan",
                    str(current_plan),
                    "--send-report",
                    str(send_report),
                    "--report",
                    str(report),
                    "--openclaw-bin",
                    str(openclaw),
                    "--property",
                    "Missing House",
                ],
                check=False,
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 2)
            self.assertFalse(calls.exists())
            payload = json.loads(report.read_text())
            self.assertEqual(
                payload["issue"], "requested_property_missing_from_send_report"
            )
            self.assertEqual(payload["portfolio_send_report_record_count"], 30)

    def test_cleanup_does_not_match_a_later_corrected_batch_by_prefix(self):
        row = {
            "id": "current-header",
            "timestampMs": 2_000_000,
            "content": "July 31 close draft for Test House is ready for review.",
            "author": {"bot": True},
        }

        self.assertFalse(
            is_old_batch_message(
                row,
                property_name="Test House",
                expected_ids={"old-header", "old-body"},
                expected_texts=["old draft"],
                sent_at_ms=1_000_000,
            )
        )

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
                "args = sys.argv[1:]\n"
                "if args[0:3] == ['message', 'thread', 'list']:\n"
                "    archived = '--include-archived' in args\n"
                "    print(json.dumps({'threads': [] if archived else [{'id': '1528381121837142079', 'name': 'Test House'}]}))\n"
                "else:\n"
                "    print(json.dumps({'ok': True, 'messageId': str(len(open(p).readlines()))}))\n",
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
            self.assertEqual(len(sent), 4)
            self.assertEqual(sent[0][0:3], ["message", "thread", "list"])
            self.assertEqual(sent[1][0:3], ["message", "thread", "list"])
            self.assertIn("--include-archived", sent[1])
            self.assertEqual(sent[2][sent[2].index("--account") + 1], "default")
            self.assertEqual(sent[3][sent[3].index("--account") + 1], "default")
            self.assertIn("each require separate human approval", sent[2][sent[2].index("--message") + 1])
            self.assertIn("No owner email or Lofty guild summary has been sent", sent[2][sent[2].index("--message") + 1])
            self.assertTrue(sent[3][sent[3].index("--message") + 1].startswith("[DRAFT FOR REVIEW - NOT EMAILED]"))
            self.assertEqual(sent[2][sent[2].index("--target") + 1], "channel:1528381121837142079")

            second = subprocess.run(command + ["--resume-report", str(report)], check=False, text=True, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(len(calls.read_text().splitlines()), 4)
            report_payload = json.loads(report.read_text())
            self.assertEqual(report_payload["results"][0]["status"], "ok_previous")
            self.assertFalse(report_payload["discord_all_property_owner_email_review_complete"])
            self.assertFalse(report_payload["human_approval_received"])
            self.assertFalse(report_payload["lofty_publication_approved"])
            self.assertFalse(report_payload["owner_email_approved"])
            self.assertEqual(review_delivery_report_issues(report_payload), [])

    def test_sender_resumes_a_live_verified_corrected_body_without_reposting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            report = root / "report.json"
            calls = root / "calls.jsonl"
            openclaw = root / "openclaw"
            write_plan(plan)
            plan_payload = json.loads(plan.read_text())
            record = plan_payload["records"][0]
            destination = review_destination()
            report.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "property_name": "Test House",
                                "status": "ok_previous",
                                "guild_id": destination["guild_id"],
                                "forum_id": destination["forum_id"],
                                "forum_target": destination["target"],
                                "discord_account_id": destination["discord_account_id"],
                                "thread_name": "Test House",
                                "thread_name_normalized": "test house",
                                "thread_id": "1528381121837142079",
                                "target": "channel:1528381121837142079",
                                "review_digest": "0" * 64,
                                "message_sha256": record["message_sha256"],
                                "header_receipt": {"messageId": "header"},
                                "body_receipt": {"messageId": "body"},
                                "current_plan_binding": {
                                    "method": "discord_live_body_read",
                                    "classification": "body:current",
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            openclaw.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                f"Path({str(calls)!r}).write_text('called')\n",
                encoding="utf-8",
            )
            openclaw.chmod(0o755)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SENDER),
                    "--plan",
                    str(plan),
                    "--report",
                    str(report),
                    "--resume-report",
                    str(report),
                    "--openclaw-bin",
                    str(openclaw),
                    "--account",
                    "default",
                    "--send",
                ],
                check=False,
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(calls.exists())
            payload = json.loads(report.read_text())
            self.assertEqual(payload["results"][0]["status"], "ok_previous")
            self.assertEqual(payload["results"][0]["resume_match"], "live_verified_current_message")
            self.assertEqual(payload["record_count"], 1)

    def test_sender_rejects_non_review_discord_account_before_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            report = root / "report.json"
            write_plan(plan)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SENDER),
                    "--plan",
                    str(plan),
                    "--report",
                    str(report),
                    "--openclaw-bin",
                    str(root / "does-not-exist"),
                    "--account",
                    "vanze",
                    "--send",
                ],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            payload = json.loads(report.read_text())
            self.assertEqual(payload["issue"], "earlcoin_review_discord_account_invalid")
            self.assertEqual(payload["expected_account"], EARLCOIN_REVIEW_DISCORD_ACCOUNT)
            self.assertEqual(payload["posted_or_verified_property_count"], 0)

    def test_sender_posts_financially_blocked_draft_only_to_operator_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            validation = root / "validation.json"
            report = root / "report.json"
            calls = root / "calls.jsonl"
            openclaw = root / "openclaw"
            write_plan(plan)
            payload = json.loads(plan.read_text())
            payload.update(
                {
                    "status": "review",
                    "global_financial_review_issue_count": 1,
                    "financial_review_issues": ["transfer_reconciliation_not_final"],
                }
            )
            payload["records"][0].update(
                {
                    "financial_review_blocked": True,
                    "financial_review_blockers": ["transfer_reconciliation_not_final"],
                }
            )
            plan.write_text(json.dumps(payload), encoding="utf-8")
            validation.write_text(
                json.dumps(
                    {
                        "discord_review_ready": True,
                        "earlcoin_review_route_ok": True,
                        "unmapped_count": 0,
                        "stale_route_count": 0,
                        "missing_financial_summary_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            openclaw.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"p={str(calls)!r}\n"
                "with open(p, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "args = sys.argv[1:]\n"
                "if args[0:3] == ['message', 'thread', 'list']:\n"
                "    print(json.dumps({'threads': [{'id': '1528381121837142079', 'name': 'Test House'}]}))\n"
                "else:\n"
                "    print(json.dumps({'ok': True, 'messageId': str(len(open(p).readlines()))}))\n",
                encoding="utf-8",
            )
            openclaw.chmod(0o755)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SENDER),
                    "--plan",
                    str(plan),
                    "--plan-validation",
                    str(validation),
                    "--report",
                    str(report),
                    "--openclaw-bin",
                    str(openclaw),
                    "--send",
                ],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            sent = [json.loads(line) for line in calls.read_text().splitlines()]
            header = sent[-2][sent[-2].index("--message") + 1]
            self.assertIn("Financial readiness remains on hold", header)
            self.assertIn("this is not publication approval", header)
            report_payload = json.loads(report.read_text())
            self.assertEqual(report_payload["status"], "ok")
            self.assertEqual(report_payload["posted_with_financial_review_blocker_count"], 1)
            self.assertTrue(report_payload["all_property_review_drafts_posted"])
            self.assertFalse(report_payload["human_approval_received"])
            self.assertFalse(report_payload["lofty_publication_approved"])
            self.assertFalse(report_payload["owner_email_approved"])

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
            self.assertEqual(payload["guild_id"], EARLCOIN_GUILD_ID)

    def test_bridge_delegated_command_uses_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            report = root / "report.json"
            agent_report = root / "agent.json"
            sender = root / "scripts" / "sender.py"
            task_capture = root / "task.md"
            openclaw = root / "openclaw"
            write_plan(plan)
            sender.parent.mkdir()
            sender.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            openclaw.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                f"report = pathlib.Path({str(report)!r})\n"
                f"capture = pathlib.Path({str(task_capture)!r})\n"
                "args = sys.argv[1:]\n"
                "task = pathlib.Path(args[args.index('--message-file') + 1])\n"
                "capture.write_text(task.read_text())\n"
                "report.write_text(json.dumps({'status': 'ok', 'posted_or_verified_property_count': 1}))\n"
                "print(json.dumps({'status': 'ok'}))\n",
                encoding="utf-8",
            )
            openclaw.chmod(0o755)
            result = subprocess.run(
                [
                    sys.executable,
                    str(BRIDGE),
                    "--plan",
                    plan.name,
                    "--report",
                    report.name,
                    "--agent-report",
                    agent_report.name,
                    "--sender-script",
                    str(sender.relative_to(root)),
                    "--openclaw-bin",
                    str(openclaw),
                    "--send",
                ],
                cwd=root,
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            task = task_capture.read_text()
            for path in (plan, report, sender):
                self.assertIn(str(path.resolve()), task)
            self.assertIn("All filesystem paths are absolute", task)

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

    def test_sender_refuses_lofty_guild_route_without_invoking_openclaw(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            report = root / "report.json"
            calls = root / "calls"
            openclaw = root / "openclaw"
            write_plan(plan)
            payload = json.loads(plan.read_text())
            payload["guild_id"] = "847877825373012018"
            payload["review_destination"]["guild_id"] = "847877825373012018"
            payload["records"][0]["guild_id"] = "847877825373012018"
            plan.write_text(json.dumps(payload), encoding="utf-8")
            openclaw.write_text(
                "#!/bin/sh\n"
                f"touch {str(calls)!r}\n"
                "exit 99\n",
                encoding="utf-8",
            )
            openclaw.chmod(0o755)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SENDER),
                    "--plan",
                    str(plan),
                    "--report",
                    str(report),
                    "--openclaw-bin",
                    str(openclaw),
                    "--send",
                ],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(calls.exists())
            payload = json.loads(report.read_text())
            self.assertEqual(payload["issue"], "earlcoin_review_route_invalid")
            self.assertGreater(payload["destination_issue_count"], 0)

    def test_bridge_refuses_lofty_guild_route_without_invoking_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            report = root / "report.json"
            agent_report = root / "agent.json"
            calls = root / "calls"
            openclaw = root / "openclaw"
            write_plan(plan)
            payload = json.loads(plan.read_text())
            payload["records"][0]["guild_id"] = "847877825373012018"
            plan.write_text(json.dumps(payload), encoding="utf-8")
            openclaw.write_text(
                "#!/bin/sh\n"
                f"touch {str(calls)!r}\n"
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
            self.assertFalse(calls.exists())
            payload = json.loads(agent_report.read_text())
            self.assertFalse(payload["agent_invoked"])
            self.assertEqual(payload["issue"], "earlcoin_review_route_invalid")

    def test_maintenance_tools_refuse_stale_lofty_receipts_before_openclaw(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous_plan = root / "previous-plan.json"
            current_plan = root / "current-plan.json"
            send_report = root / "lofty-send-report.json"
            calls = root / "calls"
            openclaw = root / "openclaw"
            write_plan(previous_plan)
            write_plan(current_plan)
            send_report.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "property_name": "Test House",
                                "target": "channel:1328523757966590014",
                                "header_receipt": {"messageId": "1528381121837142079"},
                                "body_receipt": {"messageId": "1528381121837142080"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            openclaw.write_text(
                "#!/bin/sh\n"
                f"touch {str(calls)!r}\n"
                "exit 99\n",
                encoding="utf-8",
            )
            openclaw.chmod(0o755)

            commands = (
                [
                    sys.executable,
                    str(DEDUPE),
                    "--plan",
                    str(current_plan),
                    "--send-report",
                    str(send_report),
                ],
                [
                    sys.executable,
                    str(RECONCILE),
                    "--previous-plan",
                    str(previous_plan),
                    "--current-plan",
                    str(current_plan),
                    "--send-report",
                    str(send_report),
                ],
            )
            for index, base_command in enumerate(commands):
                with self.subTest(command=base_command[1]):
                    report = root / f"maintenance-{index}.json"
                    result = subprocess.run(
                        [
                            *base_command,
                            "--report",
                            str(report),
                            "--openclaw-bin",
                            str(openclaw),
                            "--apply",
                        ],
                        check=False,
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 2)
                    payload = json.loads(report.read_text())
                    self.assertEqual(payload["issue"], "earlcoin_review_route_invalid")
                    self.assertGreater(payload["route_issue_count"], 0)
            self.assertFalse(calls.exists())


if __name__ == "__main__":
    unittest.main()
