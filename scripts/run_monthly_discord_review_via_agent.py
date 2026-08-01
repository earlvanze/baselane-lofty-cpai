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
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--account", default="")
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    plan = read_json(args.plan)
    validation = read_json(args.plan_validation) if args.plan_validation else {}
    review_ready = bool(
        plan.get("status") == "ok"
        or (
            plan.get("status") == "review"
            and validation.get("discord_review_ready") is True
            and int(validation.get("unmapped_count") or 0) == 0
            and int(validation.get("stale_route_count") or 0) == 0
            and int(validation.get("missing_financial_summary_count") or 0) == 0
        )
    )
    if not review_ready:
        sender_command = [
            args.python_bin,
            str(args.sender_script),
            "--plan",
            str(args.plan),
            "--report",
            str(args.report),
            "--send",
        ]
        if args.plan_validation:
            sender_command.extend(["--plan-validation", str(args.plan_validation)])
        if args.account:
            sender_command.extend(["--account", args.account])
        sender = subprocess.run(sender_command, check=False, text=True, capture_output=True)
        report = {
            "generated_at": iso_z(),
            "status": "review",
            "agent": "discord-public",
            "agent_invoked": False,
            "plan": str(args.plan),
            "plan_status": plan.get("status"),
            "sender_report": str(args.report),
            "sender_returncode": sender.returncode,
            "sender_status": read_json(args.report).get("status"),
            "issue": f"plan_status_not_ok:{plan.get('status')}",
            "owner_email_sent": False,
        }
        write_json(args.agent_report, report)
        return 2

    if args.dry_run or not args.send:
        command = [args.python_bin, str(args.sender_script), "--plan", str(args.plan), "--report", str(args.report), "--dry-run"]
        if args.plan_validation:
            command.extend(["--plan-validation", str(args.plan_validation)])
        if args.account:
            command.extend(["--account", args.account])
        return subprocess.run(command, check=False).returncode

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
        "--send",
    ]
    if args.plan_validation:
        sender_command.extend(["--plan-validation", str(args.plan_validation)])
    if args.account:
        sender_command.extend(["--account", args.account])
    command_text = shlex.join(sender_command)
    task_file = args.agent_report.with_suffix(".task.md")
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(
        "You are executing an explicitly operator-approved monthly investor review-draft workflow.\n"
        "Run the exact command below with the exec tool. Do not rewrite any draft, do not send owner emails, "
        "and do not publish or mutate Lofty listings. The command posts a brief header followed by the exact "
        "standalone property draft to each routed Discord forum thread and records receipts idempotently. "
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
    status = "ok" if completed.returncode == 0 and sender_report.get("status") == "ok" else "review"
    report = {
        "generated_at": iso_z(),
        "status": status,
        "agent": "discord-public",
        "session_key": session_key,
        "plan": str(args.plan),
        "sender_report": str(args.report),
        "sender_status": sender_report.get("status"),
        "sender_posted_or_verified_property_count": sender_report.get("posted_or_verified_property_count"),
        "agent_returncode": completed.returncode,
        "agent_stdout": agent_stdout,
        "agent_stderr": completed.stderr.strip() or None,
        "owner_email_sent": False,
    }
    write_json(args.agent_report, report)
    print(json.dumps({"status": status, "agent_returncode": completed.returncode, "sender_status": sender_report.get("status")}, indent=2))
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
