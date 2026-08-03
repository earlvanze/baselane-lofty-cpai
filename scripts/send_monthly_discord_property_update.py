#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OPENCLAW_SCRIPTS = Path(
    os.environ.get("OPENCLAW_WORKSPACE", Path(__file__).resolve().parents[3])
) / "scripts"
if OPENCLAW_SCRIPTS.is_dir():
    # Keep this repository's monthly modules authoritative; shared OpenClaw
    # scripts are fallback helpers (for example, the Discord route resolver).
    sys.path.append(str(OPENCLAW_SCRIPTS))

import post_property_update_discord as discord_route
from discord_summary_routing_policy import LOFTY_PUBLICATION_APPROVAL_SCOPE


DISCORD_MISSING_TOKEN_RE = re.compile(r'Discord bot token missing for account "([^"]+)"')
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def message_sha256(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def selected_property_name(guild_report: dict[str, Any]) -> str:
    selected = guild_report.get("selected") if isinstance(guild_report.get("selected"), dict) else {}
    return str(
        selected.get("property_name")
        or selected.get("route_property_name")
        or guild_report.get("property_name")
        or ""
    ).strip()


def lofty_approval_template(
    guild_report: dict[str, Any],
    *,
    property_name: str,
    target: str,
    message: str,
) -> dict[str, Any]:
    return {
        "approved": True,
        "approval_scope": LOFTY_PUBLICATION_APPROVAL_SCOPE,
        "guild_id": discord_route.LOFTY_GUILD_ID,
        "run_month": str(guild_report.get("run_month") or "").strip(),
        "property_name": property_name,
        "target": target,
        "message_sha256": message_sha256(message),
        "approved_by": "",
        "approved_at": "",
    }


def valid_iso_timestamp(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def lofty_approval_issues(approval: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field in (
        "approved",
        "approval_scope",
        "guild_id",
        "run_month",
        "property_name",
        "target",
        "message_sha256",
    ):
        if approval.get(field) != expected.get(field):
            issues.append(f"approval_{field}_mismatch")
    if not str(approval.get("approved_by") or "").strip():
        issues.append("approval_approved_by_missing")
    if not valid_iso_timestamp(approval.get("approved_at")):
        issues.append("approval_approved_at_missing_or_invalid")
    return issues


def discord_review_plan_digest(plan: dict[str, Any]) -> str:
    records = plan.get("records") if isinstance(plan.get("records"), list) else []
    canonical_records = []
    for record in records:
        if not isinstance(record, dict):
            continue
        canonical_records.append(
            {
                "property_name": record.get("property_name"),
                "target": record.get("target"),
                "message": record.get("message"),
                "message_sha256": record.get("message_sha256"),
                "financials_md_summary_sha256": record.get("financials_md_summary_sha256"),
            }
        )
    return stable_digest(
        {
            "run_month": plan.get("run_month"),
            "record_count": len(canonical_records),
            "financial_review_issue_count": plan.get("financial_review_issue_count"),
            "financial_review_issues": plan.get("financial_review_issues") if isinstance(plan.get("financial_review_issues"), list) else [],
            "transfer_reconciliation_status": plan.get("transfer_reconciliation_status"),
            "transfer_reconciliation_recommended_total_is_final": plan.get("transfer_reconciliation_recommended_total_is_final"),
            "records": canonical_records,
        }
    )


def plan_review_summary(plan: dict[str, Any]) -> dict[str, Any]:
    records = plan.get("records") if isinstance(plan.get("records"), list) else []
    valid_records = [record for record in records if isinstance(record, dict)]
    target_properties: dict[str, list[str]] = {}
    for record in valid_records:
        target = str(record.get("target") or "").strip()
        if not target:
            continue
        target_properties.setdefault(target, []).append(str(record.get("property_name") or ""))
    duplicate_targets = [
        {"target": target, "property_names": property_names}
        for target, property_names in sorted(target_properties.items())
        if len(property_names) > 1 and not discord_route.shared_target_allowed(target, property_names)
    ]
    return {
        "financial_review_issue_count": int(plan.get("financial_review_issue_count") or 0),
        "financial_review_issues": plan.get("financial_review_issues")
        if isinstance(plan.get("financial_review_issues"), list)
        else [],
        "records_with_financial_summary_count": sum(
            1 for record in valid_records if str(record.get("financials_md_summary_sha256") or "").strip()
        ),
        "records_missing_financial_summary_count": sum(
            1 for record in valid_records if not str(record.get("financials_md_summary_sha256") or "").strip()
        ),
        "records_with_message_digest_count": sum(
            1 for record in valid_records if str(record.get("message_sha256") or "").strip()
        ),
        "unique_target_count": len({record.get("target") for record in valid_records if record.get("target")}),
        "duplicate_target_count": len(duplicate_targets),
        "duplicate_targets": duplicate_targets[:25],
    }


def discord_send_issue(stderr: str) -> dict[str, Any]:
    missing_token = DISCORD_MISSING_TOKEN_RE.search(stderr)
    if missing_token:
        account_id = missing_token.group(1)
        return {
            "status": "review",
            "issue": "discord_bot_token_missing",
            "issue_summary": (
                f"Discord bot token missing for account {account_id}; configure the OpenClaw "
                "Discord account token or DISCORD_BOT_TOKEN, then rerun this send step."
            ),
            "discord_account_id": account_id,
            "next_action": (
                "Restore the Discord bot token secret reference for the target OpenClaw account, "
                "rerun this script, then rerun the guild test post evidence and owner email send guard."
            ),
        }
    return {}


def live_send_gate_issues(plan: dict[str, Any]) -> list[str]:
    """Return source-integrity blockers that must stop every live post."""
    issues: list[str] = []
    plan_status = str(plan.get("status") or "").strip()
    if plan_status and plan_status not in {"ok", "ok_partial"}:
        issues.append(f"plan_status_not_publishable:{plan_status}")
    if int(plan.get("global_financial_review_issue_count") or 0) > 0:
        issues.append("global_financial_review_issues_present")
    transfer_status = plan.get("transfer_reconciliation_status")
    if transfer_status is not None and str(transfer_status) != "ok":
        issues.append(f"transfer_reconciliation_status:{transfer_status}")
    if plan.get("transfer_reconciliation_recommended_total_is_final") is False:
        issues.append("transfer_reconciliation_not_final")
    readiness_status = plan.get("monthly_readiness_status")
    if readiness_status is not None and str(readiness_status) != "ok":
        issues.append(f"monthly_readiness_status:{readiness_status}")
    return issues


def previous_successes(report_path: Path | None) -> dict[tuple[str, str], dict[str, Any]]:
    if not report_path or not report_path.is_file():
        return {}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    records = report.get("results") if isinstance(report, dict) and isinstance(report.get("results"), list) else []
    successes: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "")
        target = str(record.get("target") or "")
        digest = str(record.get("message_sha256") or "")
        if status not in {"ok", "ok_previous"} or not target or not SHA256_RE.fullmatch(digest):
            continue
        successes[(target, digest)] = record
    return successes


def send_message(
    *,
    openclaw_bin: str,
    account: str | None,
    target: str,
    message: str,
    dry_run: bool,
) -> tuple[int, str, str, Any]:
    command = [
        openclaw_bin,
        "message",
        "send",
        "--json",
        "--channel",
        "discord",
        "--target",
        target,
        "--message",
        message,
    ]
    if account:
        command[3:3] = ["--account", account]
    if dry_run:
        command.append("--dry-run")
        send_payload = {"dry_run": True, "target": target, "account": account}
        return 0, json.dumps(send_payload), "", send_payload
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    try:
        send_payload = json.loads(stdout) if stdout else None
    except json.JSONDecodeError:
        send_payload = None
    return completed.returncode, stdout, stderr, send_payload


def send_plan(
    plan: dict[str, Any],
    *,
    plan_path: Path,
    report_path: Path,
    openclaw_bin: str,
    account: str | None,
    dry_run: bool,
    resume_report: Path | None = None,
) -> int:
    records = plan.get("records") if isinstance(plan.get("records"), list) else []
    previous = previous_successes(resume_report)
    results: list[dict[str, Any]] = []
    issue_counts: dict[str, int] = {}
    global_gate_issues = [] if dry_run else live_send_gate_issues(plan)
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        property_name = str(record.get("property_name") or "")
        target = str(record.get("target") or "")
        message = str(record.get("message") or "")
        if global_gate_issues:
            results.append(
                {
                    "index": index,
                    "property_name": property_name,
                    "target": target,
                    "status": "held_global_financial_review",
                    "financial_review_blockers": global_gate_issues,
                    "message_bytes": len(message.encode("utf-8")),
                }
            )
            continue
        if record.get("financial_review_blocked") is True:
            results.append(
                {
                    "index": index,
                    "property_name": property_name,
                    "target": target,
                    "status": "held_financial_review",
                    "financial_review_blockers": record.get("financial_review_blockers") or [],
                    "message_bytes": len(message.encode("utf-8")),
                }
            )
            continue
        if not target.startswith("channel:") or not message.strip():
            issue = "invalid_plan_record"
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
            results.append(
                {
                    "index": index,
                    "property_name": property_name,
                    "target": target,
                    "status": "failed",
                    "issue": issue,
                    "message_bytes": len(message.encode("utf-8")),
                }
            )
            continue
        message_sha256 = str(record.get("message_sha256") or "")
        previous_record = previous.get((target, message_sha256))
        if previous_record and not dry_run:
            results.append(
                {
                    "index": index,
                    "property_name": property_name,
                    "target": target,
                    "status": "ok_previous",
                    "returncode": 0,
                    "message_bytes": len(message.encode("utf-8")),
                    "message_sha256": message_sha256,
                    "send_stdout_json": previous_record.get("send_stdout_json"),
                    "send_stdout": previous_record.get("send_stdout"),
                    "send_stderr": previous_record.get("send_stderr"),
                    "previous_report": str(resume_report),
                    "previous_index": previous_record.get("index"),
                    "previous_property_name": previous_record.get("property_name"),
                }
            )
            continue
        returncode, stdout, stderr, send_payload = send_message(
            openclaw_bin=openclaw_bin,
            account=account,
            target=target,
            message=message,
            dry_run=dry_run,
        )
        issue = {} if returncode == 0 else discord_send_issue(stderr)
        if issue.get("issue"):
            issue_counts[str(issue["issue"])] = issue_counts.get(str(issue["issue"]), 0) + 1
        elif returncode != 0:
            issue_counts["send_failed"] = issue_counts.get("send_failed", 0) + 1
        result_status = (
            "ok_dry_run"
            if dry_run and returncode == 0
            else ("ok" if returncode == 0 else (str(issue.get("status")) if issue else "failed"))
        )
        results.append(
            {
                "index": index,
                "property_name": property_name,
                "target": target,
                "status": result_status,
                "returncode": returncode,
                "message_bytes": len(message.encode("utf-8")),
                "message_sha256": message_sha256 or record.get("message_sha256"),
                "send_stdout_json": send_payload,
                "send_stdout": stdout if send_payload is None else None,
                "send_stderr": stderr,
                **issue,
            }
        )
        if returncode != 0 and not dry_run:
            break
    ok_statuses = {"ok", "ok_dry_run", "ok_previous"}
    ok_count = sum(1 for item in results if item.get("status") in ok_statuses)
    held_count = sum(1 for item in results if item.get("status") == "held_financial_review")
    held_global_count = sum(1 for item in results if item.get("status") == "held_global_financial_review")
    failed_count = sum(
        1
        for item in results
        if item.get("status") not in {*ok_statuses, "held_financial_review", "held_global_financial_review"}
    )
    eligible_count = len(records) - held_count - held_global_count
    status = (
        "ok_partial_dry_run"
        if dry_run and held_count and eligible_count and ok_count == eligible_count and failed_count == 0
        else (
            "ok_partial"
            if not dry_run and held_count and eligible_count and ok_count == eligible_count and failed_count == 0
            else (
                "ok_dry_run"
                if dry_run and records and ok_count == len(records) and failed_count == 0
                else ("ok" if records and ok_count == len(records) and failed_count == 0 else "review")
            )
        )
    )
    report = {
        "generated_at": iso_z(),
        "status": status,
        "dry_run": dry_run,
        "mode": "all_plan",
        "account": account,
        "resume_report": str(resume_report) if resume_report else None,
        "resumed_success_count": sum(1 for item in results if item.get("status") == "ok_previous"),
        "plan": str(plan_path),
        "plan_status": plan.get("status"),
        "plan_digest": discord_review_plan_digest(plan),
        "plan_review_summary": plan_review_summary(plan),
        "run_month": plan.get("run_month"),
        "record_count": len(records),
        "eligible_record_count": eligible_count,
        "held_financial_review_count": held_count,
        "held_global_financial_review_count": held_global_count,
        "live_send_gate_blocked": bool(global_gate_issues),
        "live_send_gate_issues": global_gate_issues,
        "sent_or_verified_count": ok_count,
        "failed_count": failed_count,
        "issue_counts": dict(sorted(issue_counts.items())),
        "all_property_discord_review_proof_ok": bool(records and ok_count == len(records) and failed_count == 0),
        "eligible_property_discord_review_proof_ok": bool(
            eligible_count and ok_count == eligible_count and failed_count == 0
        ),
        "discord_all_property_dry_run_verified": bool(
            dry_run and records and ok_count == len(records) and failed_count == 0
        ),
        "discord_all_property_live_post_ok": bool(
            not dry_run and status == "ok" and records and ok_count == len(records) and failed_count == 0
        ),
        "discord_eligible_property_live_post_ok": bool(
            not dry_run and status in {"ok", "ok_partial"} and eligible_count and ok_count == eligible_count and failed_count == 0
        ),
        "discord_all_property_owner_email_review_complete": bool(
            not dry_run and status == "ok" and records and ok_count == len(records) and failed_count == 0
        ),
        "results": results,
    }
    write_json(report_path, report)
    print(json.dumps({"status": status, "record_count": len(records), "sent_or_verified_count": ok_count, "failed_count": failed_count}, indent=2, sort_keys=True))
    return 0 if status in {"ok", "ok_dry_run", "ok_partial", "ok_partial_dry_run"} else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send prepared monthly Lofty property update to Discord.")
    parser.add_argument("--guild-report", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--account", help="OpenClaw Discord account id/name to use for sends")
    parser.add_argument("--resume-report", type=Path, help="Reuse successful sends from this prior all-plan report")
    parser.add_argument(
        "--approval-file",
        type=Path,
        help="Hash-bound human approval required for a live Lofty Investors guild publication",
    )
    parser.add_argument("--send", action="store_true", help="Post live messages after all validation gates pass")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    # A direct invocation must be review-only unless the caller explicitly opts in.
    dry_run = bool(args.dry_run or not args.send)

    if bool(args.guild_report) == bool(args.plan):
        result = {
            "generated_at": iso_z(),
            "status": "failed",
            "issue": "exactly one of --guild-report or --plan is required",
        }
        write_json(args.report, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1
    if args.plan:
        plan = read_json(args.plan)
        if not dry_run:
            result = {
                "generated_at": iso_z(),
                "status": "review",
                "mode": "all_plan",
                "issue": "live_all_plan_send_forbidden_use_earlcoin_review_agent",
                "plan": str(args.plan),
                "next_action": (
                    "Use run_monthly_discord_review_via_agent.py for EARLCoin review drafts. "
                    "Lofty Investors guild financial publications require a separate approved guild report."
                ),
                "owner_email_sent": False,
            }
            write_json(args.report, result)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 2
        return send_plan(
            plan,
            plan_path=args.plan,
            report_path=args.report,
            openclaw_bin=args.openclaw_bin,
            account=args.account,
            dry_run=dry_run,
            resume_report=args.resume_report,
        )

    guild_report = read_json(args.guild_report)
    guild_status = str(guild_report.get("status") or "")
    if guild_status == "review":
        result = {
            "generated_at": iso_z(),
            "status": "review",
            "issue": "guild report is review; refusing Discord send",
            "guild_report": str(args.guild_report),
            "guild_report_issues": guild_report.get("issues") or [],
            "guild_report_next_action": guild_report.get("next_action"),
        }
        write_json(args.report, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2
    message_file = Path(str(guild_report.get("message_file") or ""))
    target = str(guild_report.get("target") or "")
    if not message_file.is_file():
        result = {"generated_at": iso_z(), "status": "failed", "issue": f"message_file missing: {message_file}"}
        write_json(args.report, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1
    if not target.startswith("channel:"):
        result = {"generated_at": iso_z(), "status": "failed", "issue": f"invalid Discord target: {target}"}
        write_json(args.report, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1

    message = message_file.read_text(encoding="utf-8")
    property_name = selected_property_name(guild_report)
    expected_channel, route_matched = discord_route.channel_for_property(property_name)
    expected_target = f"channel:{expected_channel}"
    guild_id = str(guild_report.get("guild_id") or "")
    route_issues: list[str] = []
    if not property_name:
        route_issues.append("lofty_publication_property_name_missing")
    if not route_matched:
        route_issues.append("lofty_publication_property_route_unmatched")
    if target != expected_target:
        route_issues.append(f"lofty_publication_target_mismatch:{target}:{expected_target}")
    if guild_id != discord_route.LOFTY_GUILD_ID:
        route_issues.append(
            f"lofty_publication_guild_mismatch:{guild_id or 'missing'}:{discord_route.LOFTY_GUILD_ID}"
        )
    if route_issues:
        result = {
            "generated_at": iso_z(),
            "status": "review",
            "mode": "guild_report",
            "issue": "lofty_publication_route_invalid",
            "issues": route_issues,
            "guild_report": str(args.guild_report),
            "property_name": property_name,
            "target": target,
            "expected_target": expected_target,
            "guild_id": guild_id or None,
            "expected_guild_id": discord_route.LOFTY_GUILD_ID,
            "human_approval_required": True,
        }
        write_json(args.report, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2

    approval_expected = lofty_approval_template(
        guild_report,
        property_name=property_name,
        target=target,
        message=message,
    )
    approval: dict[str, Any] = {}
    approval_issues: list[str] = []
    if not dry_run:
        if not args.approval_file or not args.approval_file.is_file():
            approval_issues.append("lofty_publication_approval_file_missing")
        else:
            try:
                approval = read_json(args.approval_file)
            except Exception as exc:  # noqa: BLE001
                approval_issues.append(f"lofty_publication_approval_file_unreadable:{exc}")
            else:
                approval_issues.extend(lofty_approval_issues(approval, approval_expected))
    if approval_issues:
        result = {
            "generated_at": iso_z(),
            "status": "review",
            "mode": "guild_report",
            "issue": "lofty_publication_human_approval_required",
            "issues": approval_issues,
            "guild_report": str(args.guild_report),
            "approval_file": str(args.approval_file) if args.approval_file else None,
            "required_approval": approval_expected,
            "property_name": property_name,
            "target": target,
            "guild_id": discord_route.LOFTY_GUILD_ID,
            "guild_name": discord_route.LOFTY_GUILD_NAME,
            "destination_purpose": discord_route.DESTINATION_PURPOSE,
            "human_approval_required": True,
        }
        write_json(args.report, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2

    returncode, stdout, stderr, send_payload = send_message(
        openclaw_bin=args.openclaw_bin,
        account=args.account,
        target=target,
        message=message,
        dry_run=dry_run,
    )

    issue = {} if returncode == 0 else discord_send_issue(stderr)
    status = (
        "ok_dry_run"
        if dry_run and returncode == 0
        else ("ok" if returncode == 0 else (str(issue.get("status")) if issue else "failed"))
    )
    result = {
        "generated_at": iso_z(),
        "status": status,
        "returncode": returncode,
        "send_requested": bool(args.send),
        "dry_run": dry_run,
        "mode": "guild_report",
        "account": args.account,
        "property_name": property_name,
        "target": target,
        "guild_id": discord_route.LOFTY_GUILD_ID,
        "guild_name": discord_route.LOFTY_GUILD_NAME,
        "destination_purpose": discord_route.DESTINATION_PURPOSE,
        "human_approval_required": True,
        "approval_scope": LOFTY_PUBLICATION_APPROVAL_SCOPE,
        "approval_file": str(args.approval_file) if args.approval_file else None,
        "approval_verified": bool(not dry_run and not approval_issues),
        "message_sha256": message_sha256(message),
        "message_file": str(message_file),
        "message_bytes": len(message.encode("utf-8")),
        "guild_report": str(args.guild_report),
        "send_stdout_json": send_payload,
        "send_stdout": stdout if send_payload is None else None,
        "send_stderr": stderr,
        **issue,
    }
    write_json(args.report, result)
    print(json.dumps({"status": status, "returncode": returncode, "target": target}, indent=2, sort_keys=True))
    return 0 if returncode == 0 else returncode


if __name__ == "__main__":
    raise SystemExit(main())
