#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from discord_summary_routing_policy import (
    EARLCOIN_GUILD_ID,
    EARLCOIN_REVIEW_DISCORD_ACCOUNT,
    EARLCOIN_REVIEW_FORUM_ID,
    EARLCOIN_REVIEW_FORUM_NAME,
    LOFTY_GUILD_ID,
    review_destination,
    review_route_issues,
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Delegate deterministic monthly Discord review posts to agent:discord-public.")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--agent-report", required=True, type=Path)
    parser.add_argument("--sender-script", required=True, type=Path)
    parser.add_argument("--plan-validation", type=Path)
    parser.add_argument("--thread-inventory", type=Path)
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--account", default=EARLCOIN_REVIEW_DISCORD_ACCOUNT)
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--property",
        action="append",
        default=[],
        help=(
            "Send only the exact named property from an otherwise fully "
            "validated portfolio plan; repeat as needed."
        ),
    )
    args = parser.parse_args()

    # The delegated agent runs from its own workspace, so every filesystem
    # argument in its command must remain valid outside this repository CWD.
    args.plan = args.plan.resolve()
    args.report = args.report.resolve()
    args.agent_report = args.agent_report.resolve()
    args.sender_script = args.sender_script.resolve()
    if args.plan_validation:
        args.plan_validation = args.plan_validation.resolve()
    if args.thread_inventory:
        args.thread_inventory = args.thread_inventory.resolve()

    plan = read_json(args.plan)
    validation = read_json(args.plan_validation) if args.plan_validation else {}
    destination = review_destination()
    destination_issues = review_route_issues(plan)
    if args.account != EARLCOIN_REVIEW_DISCORD_ACCOUNT:
        destination_issues.append(
            {
                "scope": "dispatch",
                "field": "discord_account_id",
                "actual": args.account,
                "expected": EARLCOIN_REVIEW_DISCORD_ACCOUNT,
                "issue": "dispatch.discord_account_id_mismatch",
            }
        )
    review_ready = bool(
        not destination_issues
        and (
            plan.get("status") in {"ok", "ok_partial"}
        or (
            plan.get("status") == "review"
            and validation.get("discord_review_ready") is True
            and validation.get("earlcoin_review_route_ok") is True
            and int(validation.get("unmapped_count") or 0) == 0
            and int(validation.get("stale_route_count") or 0) == 0
            and int(validation.get("missing_financial_summary_count") or 0) == 0
        )
        )
    )
    if not review_ready:
        issue = "earlcoin_review_route_invalid" if destination_issues else f"plan_status_not_ok:{plan.get('status')}"
        sender_report = {
            "generated_at": iso_z(),
            "status": "review",
            "mode": "all_plan",
            "delivery_mode": "earlcoin_operator_review_drafts",
            "plan": str(args.plan),
            "plan_status": plan.get("status"),
            "issue": issue,
            "destination_issue_count": len(destination_issues),
            "destination_issues": destination_issues,
            "review_destination": destination,
            **destination,
            "record_count": len(plan.get("records") or []),
            "posted_or_verified_property_count": 0,
            "failed_count": 0,
            "owner_email_sent": False,
        }
        write_json(args.report, sender_report)
        report = {
            "generated_at": iso_z(),
            "status": "review",
            "agent": "discord-public",
            "agent_invoked": False,
            "plan": str(args.plan),
            "plan_status": plan.get("status"),
            "sender_report": str(args.report),
            "sender_returncode": 2,
            "sender_status": "review",
            "issue": issue,
            "destination_issue_count": len(destination_issues),
            "destination_issues": destination_issues,
            "review_destination": destination,
            **destination,
            "owner_email_sent": False,
        }
        write_json(args.agent_report, report)
        return 2

    if args.dry_run or not args.send:
        command = [
            args.python_bin,
            str(args.sender_script),
            "--plan",
            str(args.plan),
            "--report",
            str(args.report),
            "--openclaw-bin",
            args.openclaw_bin,
            "--dry-run",
        ]
        if args.plan_validation:
            command.extend(["--plan-validation", str(args.plan_validation)])
        if args.thread_inventory:
            command.extend(["--thread-inventory", str(args.thread_inventory)])
        if args.account:
            command.extend(["--account", args.account])
        for property_name in args.property:
            command.extend(["--property", property_name])
        sender = subprocess.run(command, check=False)
        sender_report = read_json(args.report)
        write_json(
            args.agent_report,
            {
                "generated_at": iso_z(),
                "status": sender_report.get("status") or "review",
                "agent": "discord-public",
                "agent_invoked": False,
                "dry_run": True,
                "plan": str(args.plan),
                "sender_report": str(args.report),
                "sender_returncode": sender.returncode,
                "sender_status": sender_report.get("status"),
                "review_destination": destination,
                **destination,
                "owner_email_sent": False,
            },
        )
        return sender.returncode

    digest = hashlib.sha256(args.plan.read_bytes()).hexdigest()
    session_key = f"agent:discord-public:monthly-review-drafts-{digest[:16]}"
    sender_command = [
        args.python_bin,
        str(args.sender_script),
        "--plan",
        str(args.plan),
        "--report",
        str(args.report),
        "--resume-report",
        str(args.report),
        "--openclaw-bin",
        args.openclaw_bin,
        "--send",
    ]
    if args.plan_validation:
        sender_command.extend(["--plan-validation", str(args.plan_validation)])
    if args.thread_inventory:
        sender_command.extend(["--thread-inventory", str(args.thread_inventory)])
    if args.account:
        sender_command.extend(["--account", args.account])
    for property_name in args.property:
        sender_command.extend(["--property", property_name])
    command_text = shlex.join(sender_command)
    task_file = args.agent_report.with_suffix(".task.md")
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(
        "You are executing an explicitly operator-approved monthly investor review-draft workflow.\n"
        f"Route these drafts only to the EARLCoin guild ({EARLCOIN_GUILD_ID}) "
        f"{EARLCOIN_REVIEW_FORUM_NAME} forum ({EARLCOIN_REVIEW_FORUM_ID}). Never post these review drafts "
        f"to the Lofty Investors guild ({LOFTY_GUILD_ID}).\n"
        "Run the exact command below with the exec tool. Do not rewrite any draft, send owner emails, "
        "publish a Lofty guild financial summary, or mutate Lofty listings. The command posts a brief header "
        "followed by the exact standalone property draft to each EARLCoin forum thread and records receipts idempotently. "
        "All filesystem paths are absolute because your workspace is different from the reconciliation repository. "
        "After it finishes, inspect the JSON report and reply with only its status and counts.\n\n"
        f"```bash\n{command_text}\n```\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            args.openclaw_bin,
            "agent",
            "--agent",
            "discord-public",
            "--session-key",
            session_key,
            "--message-file",
            str(task_file),
            "--thinking",
            "low",
            "--timeout",
            "1800",
            "--json",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    try:
        agent_stdout: Any = json.loads(completed.stdout) if completed.stdout.strip() else None
    except json.JSONDecodeError:
        agent_stdout = completed.stdout.strip() or None
    sender_report: dict[str, Any] = {}
    if args.report.is_file():
        try:
            sender_report = json.loads(args.report.read_text(encoding="utf-8"))
        except Exception:
            sender_report = {}
    sender_status = str(sender_report.get("status") or "")
    status = (
        sender_status
        if completed.returncode == 0 and sender_status in {"ok", "ok_partial"}
        else "review"
    )
    report = {
        "generated_at": iso_z(),
        "status": status,
        "agent": "discord-public",
        "agent_invoked": True,
        "session_key": session_key,
        "plan": str(args.plan),
        "sender_report": str(args.report),
        "sender_status": sender_report.get("status"),
        "sender_posted_or_verified_property_count": sender_report.get("posted_or_verified_property_count"),
        "agent_returncode": completed.returncode,
        "agent_stdout": agent_stdout,
        "agent_stderr": completed.stderr.strip() or None,
        "review_destination": destination,
        **destination,
        "owner_email_sent": False,
    }
    write_json(args.agent_report, report)
    print(json.dumps({"status": status, "agent_returncode": completed.returncode, "sender_status": sender_report.get("status")}, indent=2))
    return 0 if status in {"ok", "ok_partial"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
