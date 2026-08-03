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
    active_portfolio_summary_population_issues,
    review_delivery_report_issues,
    review_destination,
    review_route_issues,
)


def iso_z() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Delegate guarded in-place monthly Discord draft reconciliation "
            "to agent:discord-public."
        )
    )
    parser.add_argument("--previous-plan", required=True, type=Path)
    parser.add_argument("--current-plan", required=True, type=Path)
    parser.add_argument("--send-report", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--agent-report", required=True, type=Path)
    parser.add_argument("--reconcile-script", required=True, type=Path)
    parser.add_argument("--property", action="append", required=True)
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--account", default=EARLCOIN_REVIEW_DISCORD_ACCOUNT)
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--agent-timeout", type=int, default=1200)
    args = parser.parse_args()

    for field in (
        "previous_plan",
        "current_plan",
        "send_report",
        "report",
        "agent_report",
        "reconcile_script",
    ):
        setattr(args, field, getattr(args, field).resolve())

    previous_plan = read_json(args.previous_plan)
    current_plan = read_json(args.current_plan)
    send_report = read_json(args.send_report)
    requested = [value.strip() for value in args.property if value.strip()]
    requested_keys = {value.casefold() for value in requested}
    available_sets = []
    for payload, field in (
        (previous_plan, "records"),
        (current_plan, "records"),
        (send_report, "results"),
    ):
        available_sets.append(
            {
                str(record.get("property_name") or "").strip().casefold()
                for record in payload.get(field) or []
                if isinstance(record, dict)
            }
        )

    issues: list[Any] = [
        *review_route_issues(previous_plan),
        *review_route_issues(current_plan),
        *review_delivery_report_issues(send_report),
        *active_portfolio_summary_population_issues(
            current_plan.get("authoritative_active_property_count"),
            current_plan.get("authoritative_reporting_target_count"),
            len(current_plan.get("records") or []),
        ),
    ]
    if previous_plan.get("status") not in {"ok", "ok_partial"}:
        issues.append(f"previous_plan_status_not_ready:{previous_plan.get('status')}")
    if current_plan.get("status") not in {"ok", "ok_partial"}:
        issues.append(f"current_plan_status_not_ready:{current_plan.get('status')}")
    if not requested or len(requested_keys) != len(requested):
        issues.append("requested_property_selection_empty_or_duplicate")
    for index, available in enumerate(available_sets):
        for missing in sorted(requested_keys - available):
            issues.append(f"requested_property_missing_from_source_{index}:{missing}")
    if args.account != EARLCOIN_REVIEW_DISCORD_ACCOUNT:
        issues.append(f"discord_account_mismatch:{args.account}")

    destination = review_destination()
    if issues:
        payload = {
            "generated_at": iso_z(),
            "status": "review",
            "agent": "discord-public",
            "agent_invoked": False,
            "issue": "earlcoin_review_reconciliation_preflight_failed",
            "issue_count": len(issues),
            "issues": issues,
            "requested_properties": requested,
            "review_destination": destination,
            **destination,
            "owner_email_sent": False,
            "lofty_guild_summary_sent": False,
        }
        write_json(args.agent_report, payload)
        return 2

    command = [
        args.python_bin,
        str(args.reconcile_script),
        "--previous-plan",
        str(args.previous_plan),
        "--current-plan",
        str(args.current_plan),
        "--send-report",
        str(args.send_report),
        "--report",
        str(args.report),
        "--openclaw-bin",
        args.openclaw_bin,
        "--account",
        args.account,
    ]
    for property_name in requested:
        command.extend(["--property", property_name])
    if args.apply and not args.dry_run:
        command.append("--apply")

    if args.dry_run or not args.apply:
        completed = subprocess.run(command, check=False)
        reconcile_report = read_json(args.report)
        write_json(
            args.agent_report,
            {
                "generated_at": iso_z(),
                "status": reconcile_report.get("status") or "review",
                "agent": "discord-public",
                "agent_invoked": False,
                "dry_run": True,
                "requested_properties": requested,
                "reconcile_report": str(args.report),
                "reconcile_returncode": completed.returncode,
                "review_destination": destination,
                **destination,
                "owner_email_sent": False,
                "lofty_guild_summary_sent": False,
            },
        )
        return completed.returncode

    digest_input = "\0".join(
        [
            hashlib.sha256(args.current_plan.read_bytes()).hexdigest(),
            *sorted(requested_keys),
        ]
    )
    digest = hashlib.sha256(digest_input.encode()).hexdigest()
    session_key = f"agent:discord-public:monthly-review-reconcile-{digest[:16]}"
    command_text = shlex.join(command)
    task_file = args.agent_report.with_suffix(".task.md")
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(
        "Execute this explicitly operator-approved Discord reconciliation command exactly once. "
        "Use the exec tool with a wait of at least 900 seconds, then inspect the JSON report. "
        f"The command is restricted to existing EARLCoin guild ({EARLCOIN_GUILD_ID}) "
        f"{EARLCOIN_REVIEW_FORUM_NAME} forum ({EARLCOIN_REVIEW_FORUM_ID}) threads. It validates "
        "the full 32-active-physical-property / 30-reporting-target portfolio route, edits only "
        "exact prior or prior/current-mixed body fragments, preserves headers and message IDs, "
        "and verifies live. Do not create posts, send owner emails, mutate Lofty, or publish to "
        f"the Lofty guild ({LOFTY_GUILD_ID}). Do not rerun the command. Reply only with status, "
        "checked-channel count, edited-fragment count, and issue count.\n\n"
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
            str(args.agent_timeout),
            "--json",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    reconcile_report = read_json(args.report)
    reconcile_status = str(reconcile_report.get("status") or "")
    status = (
        "ok"
        if completed.returncode == 0 and reconcile_status == "ok"
        else "review"
    )
    try:
        agent_stdout: Any = json.loads(completed.stdout) if completed.stdout.strip() else None
    except json.JSONDecodeError:
        agent_stdout = completed.stdout.strip() or None
    write_json(
        args.agent_report,
        {
            "generated_at": iso_z(),
            "status": status,
            "agent": "discord-public",
            "agent_invoked": True,
            "session_key": session_key,
            "requested_properties": requested,
            "reconcile_report": str(args.report),
            "reconcile_status": reconcile_report.get("status"),
            "checked_channel_count": reconcile_report.get("checked_channel_count"),
            "edited_fragment_count": reconcile_report.get("edited_fragment_count"),
            "issue_count": reconcile_report.get("issue_count"),
            "agent_returncode": completed.returncode,
            "agent_stdout": agent_stdout,
            "agent_stderr": completed.stderr.strip() or None,
            "review_destination": destination,
            **destination,
            "owner_email_sent": False,
            "lofty_guild_summary_sent": False,
        },
    )
    print(
        json.dumps(
            {
                "status": status,
                "agent_returncode": completed.returncode,
                "reconcile_status": reconcile_report.get("status"),
                "checked_channel_count": reconcile_report.get("checked_channel_count"),
                "edited_fragment_count": reconcile_report.get("edited_fragment_count"),
                "issue_count": reconcile_report.get("issue_count"),
            },
            indent=2,
        )
    )
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
