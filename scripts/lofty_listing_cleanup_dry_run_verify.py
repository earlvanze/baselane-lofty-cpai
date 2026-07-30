#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_LISTING_UPDATE_CHARS = 3500
MAX_LISTING_UPDATE_LINES = 80


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not object"}


def parse_json_stream(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    index = 0
    while index < len(text):
        match = re.search(r"[\[{]", text[index:])
        if not match:
            break
        start = index + match.start()
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, dict):
            objects.append(value)
        index = start + end
    return objects


def hex64(value: object) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "")))


def extract_command_digest(command_file: Path | None) -> str:
    if not command_file or not command_file.is_file():
        return ""
    match = re.search(r"ready_cleanup_idempotency_digest=([0-9a-f]{64})", command_file.read_text(encoding="utf-8", errors="replace"))
    return match.group(1) if match else ""


def executable_command_lines(command_file: Path) -> list[str]:
    lines: list[str] = []
    for raw_line in command_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("#!") or line.startswith("set "):
            continue
        if line.startswith("[[ ") and "LOFTY_LISTING_CLEANUP_APPLY_APPROVED" in line:
            continue
        if line.startswith("[[ ") and "LOFTY_LISTING_CLEANUP_APPLY_DIGEST" in line:
            continue
        if "lofty_listing_cleanup_apply_preflight.py" in line:
            continue
        lines.append(line)
    return lines


def arg_value(parts: list[str], flag: str) -> str:
    try:
        index = parts.index(flag)
    except ValueError:
        return ""
    if index + 1 >= len(parts):
        return ""
    return parts[index + 1]


def property_path_issues(path_text: str) -> list[str]:
    normalized = str(path_text or "").replace("\\", "/")
    lowered = normalized.lower()
    parts = [part for part in lowered.split("/") if part]
    issues: list[str] = []
    if not normalized:
        issues.append("property_path_missing")
        return issues
    if "/mnt/f/.openclaw" in lowered or "/home/umbrel" in lowered or "workspace-discord-public" in lowered:
        issues.append("property_path_forbidden_workspace_or_host")
    if "/dropbox/real estate/" not in lowered:
        issues.append("property_path_not_dropbox_real_estate")
    canonical_updates = (
        len(parts) >= 3
        and parts[-1] == "updates.md"
        and parts[-2] == "00 - readme & property snapshot"
        and (parts[-3] == "public" or parts[-3].endswith(" public"))
    )
    if not canonical_updates:
        issues.append("property_path_not_canonical_updates_md")
    if "/financials/" in lowered:
        issues.append("property_path_legacy_financials_folder")
    return issues


def validate_command_file(
    command_file: Path | None,
    *,
    expected_count: int,
    expected_digest: str,
    manifest_properties: list[str],
    mode: str,
) -> tuple[list[str], dict[str, Any]]:
    issues: list[str] = []
    details: dict[str, Any] = {
        "path": str(command_file) if command_file else None,
        "command_count": 0,
        "digest": "",
        "bad_command_count": 0,
        "bad_commands_bounded": [],
    }
    if not command_file or not command_file.is_file():
        return [f"{mode}_command_file_missing"], details

    text = command_file.read_text(encoding="utf-8", errors="replace")
    command_digest = extract_command_digest(command_file)
    commands = executable_command_lines(command_file)
    details["digest"] = command_digest
    details["command_count"] = len(commands)
    if expected_digest and command_digest != expected_digest:
        issues.append(f"{mode}_command_digest_mismatch")
    if len(commands) != expected_count:
        issues.append(f"{mode}_command_count={len(commands)}!=ready_count={expected_count}")
    if mode == "live_apply":
        if "requires-explicit-approval" not in command_file.name:
            issues.append("live_apply_command_file_missing_explicit_approval_filename")
        if "DO NOT RUN WITHOUT EXPLICIT OPERATOR APPROVAL" not in text:
            issues.append("live_apply_command_file_missing_explicit_approval_banner")
        if "LOFTY_LISTING_CLEANUP_APPLY_APPROVED" not in text:
            issues.append("live_apply_command_file_missing_approval_env_gate")
        if "LOFTY_LISTING_CLEANUP_APPLY_DIGEST" not in text:
            issues.append("live_apply_command_file_missing_approval_digest_gate")
        approval_digest_lines = [
            line
            for line in text.splitlines()
            if "LOFTY_LISTING_CLEANUP_APPLY_DIGEST" in line and line.strip().startswith("[[ ")
        ]
        if expected_digest and not any(expected_digest in line for line in approval_digest_lines):
            issues.append("live_apply_command_file_missing_approval_digest_value")
        if "lofty_listing_cleanup_apply_preflight.py" not in text:
            issues.append("live_apply_command_file_missing_dry_run_preflight")
        if "LOFTY_LISTING_CLEANUP_DRY_RUN_VERIFY_REPORT" not in text:
            issues.append("live_apply_command_file_missing_dry_run_verify_report_env")
        if "LOFTY_LISTING_CLEANUP_APPLY_PREFLIGHT_REPORT" not in text:
            issues.append("live_apply_command_file_missing_apply_preflight_report_env")

    command_properties: list[str] = []
    bad_commands: list[dict[str, Any]] = []
    for index, command in enumerate(commands, start=1):
        command_issues: list[str] = []
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            parts = []
            command_issues.append(f"unparseable_command:{exc}")
        property_path = arg_value(parts, "--property") if parts else ""
        if property_path:
            command_properties.append(property_path)
        command_issues.extend(property_path_issues(property_path))
        if "--skip-send" not in parts:
            command_issues.append("skip_send_flag_missing")
        unsafe_flags = {"--send", "--publish-history-to-listing", "--send-unsent-history", "--listing-field-current-only"}
        present_unsafe = sorted(flag for flag in unsafe_flags if flag in parts)
        if present_unsafe:
            command_issues.append("unsafe_flags_present=" + ",".join(present_unsafe))
        if mode == "dry_run" and "--dry-run" not in parts:
            command_issues.append("dry_run_flag_missing")
        if mode == "live_apply" and "--dry-run" in parts:
            command_issues.append("live_apply_contains_dry_run_flag")
        if command_issues:
            bad_commands.append({"index": index, "property": property_path, "issues": command_issues})
    if bad_commands:
        issues.append(f"{mode}_bad_command_count={len(bad_commands)}")
    if manifest_properties and command_properties != manifest_properties:
        issues.append(f"{mode}_command_properties_do_not_match_ready_manifest")
    details["bad_command_count"] = len(bad_commands)
    details["bad_commands_bounded"] = bad_commands[:25]
    details["properties_bounded"] = command_properties[:25]
    return issues, details


def verify_report(queue_report_path: Path, stdout_log: Path, stderr_log: Path | None) -> dict[str, Any]:
    queue = read_json(queue_report_path)
    stdout_text = stdout_log.read_text(encoding="utf-8", errors="replace") if stdout_log.is_file() else ""
    stderr_text = stderr_log.read_text(encoding="utf-8", errors="replace") if stderr_log and stderr_log.is_file() else ""
    objects = parse_json_stream(stdout_text)
    final_records = [
        obj
        for obj in objects
        if isinstance(obj, dict)
        and "state_file" in obj
        and "listing_update_scope" in obj
        and "dry_run" in obj
    ]
    send_steps = [
        obj
        for obj in objects
        if obj.get("step") == "send"
    ]
    ready_count = int(queue.get("ready_listing_cleanup_count") or 0)
    requires_monthly_financial_summary = queue.get("requires_monthly_financial_summary") is True
    expected_listing_update_scope = str(queue.get("listing_update_scope") or "full_history")
    dry_run_command_count = int(queue.get("dry_run_command_count") or ready_count)
    live_apply_command_count = int(queue.get("live_apply_command_requires_explicit_approval_count") or ready_count)
    queue_digest = str(queue.get("ready_cleanup_idempotency_digest") or "")
    dry_run_command_file = Path(str(queue.get("dry_run_commands_file") or ""))
    live_apply_command_file = Path(str(queue.get("live_apply_commands_requires_explicit_approval_file") or ""))
    command_digest = extract_command_digest(dry_run_command_file)
    manifest = queue.get("ready_cleanup_manifest") if isinstance(queue.get("ready_cleanup_manifest"), list) else []
    manifest_properties = [str(record.get("updates_md") or "") for record in manifest if isinstance(record, dict)]
    issues: list[str] = []
    if queue.get("status") in {"missing", "unreadable"}:
        issues.append(f"queue_report_{queue.get('status')}")
    if dry_run_command_count != ready_count:
        issues.append(f"dry_run_command_count={dry_run_command_count}!=ready_count={ready_count}")
    if live_apply_command_count != ready_count:
        issues.append(f"live_apply_command_count={live_apply_command_count}!=ready_count={ready_count}")
    if len(manifest_properties) != ready_count:
        issues.append(f"ready_cleanup_manifest_count={len(manifest_properties)}!=ready_count={ready_count}")
    if not hex64(queue_digest):
        issues.append("ready_cleanup_idempotency_digest_missing_or_invalid")
    if command_digest and queue_digest and command_digest != queue_digest:
        issues.append("dry_run_command_digest_mismatch")
    dry_run_command_issues, dry_run_command_details = validate_command_file(
        dry_run_command_file,
        expected_count=ready_count,
        expected_digest=queue_digest,
        manifest_properties=manifest_properties,
        mode="dry_run",
    )
    live_apply_command_issues, live_apply_command_details = validate_command_file(
        live_apply_command_file,
        expected_count=ready_count,
        expected_digest=queue_digest,
        manifest_properties=manifest_properties,
        mode="live_apply",
    )
    issues.extend(dry_run_command_issues)
    issues.extend(live_apply_command_issues)
    if not stdout_text and ready_count > 0:
        issues.append("stdout_log_missing_or_empty")
    if stderr_text.strip():
        issues.append("stderr_log_not_empty")
    if len(final_records) != ready_count:
        issues.append(f"verified_record_count={len(final_records)}!=ready_count={ready_count}")
    bad_records: list[dict[str, Any]] = []
    for index, record in enumerate(final_records, start=1):
        record_issues: list[str] = []
        if record.get("dry_run") is not True:
            record_issues.append("dry_run_not_true")
        if record.get("skip_send") is not True:
            record_issues.append("skip_send_not_true")
        if record.get("will_send") is not False:
            record_issues.append("will_send_not_false")
        if record.get("listing_update_scope") != expected_listing_update_scope:
            record_issues.append(f"listing_update_scope_not_{expected_listing_update_scope}")
        if record.get("listing_update_guard_ok") is not True:
            record_issues.append("listing_update_guard_not_ok")
        if requires_monthly_financial_summary and record.get("financial_summary_enriched") is not True:
            record_issues.append("financial_summary_enriched_not_true")
        if requires_monthly_financial_summary and record.get("require_monthly_financial_summary") is not True:
            record_issues.append("require_monthly_financial_summary_not_true")
        if record.get("publish_history_to_listing") is not False:
            record_issues.append("publish_history_to_listing_not_false")
        if record.get("send_unsent_history") is not False:
            record_issues.append("send_unsent_history_not_false")
        if int(record.get("listing_update_char_count") or 0) > MAX_LISTING_UPDATE_CHARS:
            record_issues.append("listing_update_char_count_too_high")
        if int(record.get("listing_update_line_count") or 0) > MAX_LISTING_UPDATE_LINES:
            record_issues.append("listing_update_line_count_too_high")
        if not hex64(record.get("latest_digest")):
            record_issues.append("latest_digest_missing_or_invalid")
        if not hex64(record.get("field_digest")):
            record_issues.append("field_digest_missing_or_invalid")
        if record_issues:
            bad_records.append(
                {
                    "index": index,
                    "state_file": record.get("state_file"),
                    "issues": record_issues,
                }
            )
    if bad_records:
        issues.append(f"bad_verified_record_count={len(bad_records)}")
    unsafe_send_steps = [step for step in send_steps if not (step.get("skipped") is True and step.get("reason") == "skip-send")]
    if unsafe_send_steps:
        issues.append(f"unsafe_send_step_count={len(unsafe_send_steps)}")
    return {
        "generated_at": iso_z(),
        "status": "ok" if not issues else "review",
        "issue_count": len(issues),
        "issues": issues,
        "queue_report": str(queue_report_path),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log) if stderr_log else None,
        "mutates_lofty_listing": False,
        "sends_owner_email": False,
        "dry_run_only": True,
        "requires_monthly_financial_summary": requires_monthly_financial_summary,
        "ready_listing_cleanup_count": ready_count,
        "dry_run_command_count": dry_run_command_count,
        "live_apply_command_requires_explicit_approval_count": live_apply_command_count,
        "verified_record_count": len(final_records),
        "send_step_count": len(send_steps),
        "unsafe_send_step_count": len(unsafe_send_steps),
        "bad_verified_record_count": len(bad_records),
        "bad_verified_records_bounded": bad_records[:25],
        "ready_cleanup_idempotency_digest": queue_digest,
        "expected_listing_update_scope": expected_listing_update_scope,
        "dry_run_command_file_digest": command_digest,
        "ready_cleanup_manifest_count": len(manifest_properties),
        "dry_run_command_file": dry_run_command_details,
        "live_apply_command_file": live_apply_command_details,
        "listing_update_scope": expected_listing_update_scope,
        "max_listing_update_chars": MAX_LISTING_UPDATE_CHARS,
        "max_listing_update_lines": MAX_LISTING_UPDATE_LINES,
        "verified_records_bounded": [
            {
                "state_file": record.get("state_file"),
                "latest_digest": record.get("latest_digest"),
                "field_digest": record.get("field_digest"),
                "listing_update_char_count": record.get("listing_update_char_count"),
                "listing_update_line_count": record.get("listing_update_line_count"),
                "will_send": record.get("will_send"),
                "skip_send": record.get("skip_send"),
                "dry_run": record.get("dry_run"),
                "listing_update_scope": record.get("listing_update_scope"),
                "financial_summary_enriched": record.get("financial_summary_enriched"),
                "require_monthly_financial_summary": record.get("require_monthly_financial_summary"),
            }
            for record in final_records[:25]
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Lofty listing cleanup dry-run logs publish cleaned listing history and no owner email.")
    parser.add_argument("--queue-report", required=True, type=Path)
    parser.add_argument("--stdout-log", required=True, type=Path)
    parser.add_argument("--stderr-log", type=Path, default=None)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    report = verify_report(args.queue_report, args.stdout_log, args.stderr_log)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "issue_count", "ready_listing_cleanup_count", "verified_record_count")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
