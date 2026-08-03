#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EOD_TELEGRAM_SEND_APPROVAL_ENV = "BASELANE_EOD_TELEGRAM_SEND_APPROVED"
REQUIRE_EOD_TELEGRAM_SCHEDULE_ENV = "BASELANE_SCHEDULER_REQUIRE_EOD_TELEGRAM"

JOB_SPECS = [
    {
        "name": "daily_sync",
        "script": "scripts/baselane_cron_run.sh",
        "report": "reports/baselane_daily_run_report.json",
        "expected_statuses": {"ok"},
        "max_report_age_hours": 36,
        "scheduler_terms": ["baselane_cron_run.sh"],
        "required_scheduler_line_fragment_sets": [
            [
                "0 9 * * *",
                '"BASELANE_CRON_HUMAN_PACED_FALLBACK": "1"',
                "flock -n",
                "timeout",
                "/home/digit/.openclaw/workspace/repos/baselane-lofty-cpai/scripts/baselane_cron_run.sh",
            ],
            [
                "15 6 * * *",
                '"BASELANE_CRON_HUMAN_PACED_FALLBACK": "1"',
                "flock -n",
                "timeout",
                "/home/digit/.openclaw/workspace/repos/baselane-lofty-cpai/scripts/baselane_cron_run.sh",
            ],
        ],
        "required_report_fields": [
            "ended_at",
            "duration_seconds",
            "return_code",
            "failed_step",
            "sync_report_status",
            "steps.deterministic_sync",
            "steps.human_paced_sync_fallback",
            "steps.local_model_preflight",
            "human_paced_backup_policy",
            "human_paced_backup_enabled",
            "human_paced_backup_script_exists",
        ],
        "required_report_values": {
            "return_code": 0,
            "sync_report_status": "ok",
            "human_paced_backup_policy": "deterministic_primary_human_paced_backup",
            "human_paced_backup_script_exists": True,
        },
        "non_negative_int_report_fields": ["duration_seconds"],
    },
    {
        "name": "weekly_file_updates",
        "script": "scripts/baselane_weekly_file_updates_cron.sh",
        "report": "reports/baselane_weekly_file_updates_run_report.json",
        "expected_statuses": {"ok", "review"},
        "max_report_age_hours": 24 * 8,
        "scheduler_terms": ["baselane_weekly_file_updates_cron.sh"],
        "required_scheduler_line_fragment_sets": [
            ["45 6 * * 5", "flock -n", "timeout", "baselane_weekly_file_updates_cron.sh"],
        ],
        "required_report_fields": [
            "ended_at",
            "duration_seconds",
            "return_code",
            "deterministic_verification_idempotent",
        ],
        "required_report_values": {"return_code": 0},
        "non_negative_int_report_fields": ["duration_seconds"],
    },
    {
        "name": "monthly_financials",
        "script": "scripts/baselane_financials_monthly_cron.sh",
        "report": "reports/baselane_financials_monthly_run_report.json",
        "expected_statuses": {"ok", "review"},
        "max_report_age_hours": 24 * 35,
        "scheduler_terms": ["baselane_financials_monthly_cron.sh", "baselane_monthly_statements_idempotent.sh"],
        "required_scheduler_line_fragment_sets": [
            ["0 3 15 * *", "flock -n", "timeout", "baselane_financials_monthly_cron.sh"],
        ],
        "required_report_fields": [
            "ended_at",
            "duration_seconds",
            "return_code",
            "failed_step",
            "steps.monthly_readiness",
        ],
        "required_report_values": {"return_code": 0},
        "non_negative_int_report_fields": ["duration_seconds"],
    },
    {
        "name": "eod_telegram",
        "script": "scripts/baselane_eod_telegram_report.py",
        "report": "reports/baselane_eod_telegram_report.json",
        "expected_statuses": {"ok"},
        "max_report_age_hours": 36,
        "scheduler_terms": ["baselane_eod_telegram_report.py"],
        "required_scheduler_line_fragment_sets": [
            [
                "55 21 * * *",
                "flock -n",
                f"{EOD_TELEGRAM_SEND_APPROVAL_ENV}=1",
                "timeout",
                "baselane_eod_telegram_report.py",
                "--send",
            ],
        ],
        "required_report_fields": [
            "generated_at",
            "dry_run",
            "send_requested",
            "telegram_send_ok",
            "telegram_http_statuses",
            "message",
            "message_character_count",
            "message_chunk_count",
        ],
        "required_report_values": {"dry_run": False, "send_requested": True, "telegram_send_ok": True},
        "allow_empty_report_fields_when": {
            "telegram_http_statuses": {"dry_run": True, "telegram_send_ok": False},
        },
    },
]
if os.environ.get(REQUIRE_EOD_TELEGRAM_SCHEDULE_ENV, "0") != "1":
    JOB_SPECS = [spec for spec in JOB_SPECS if spec.get("name") != "eod_telegram"]
EOD_ACTIONABLE_MESSAGE_MAX_LINES = 8
EOD_ACTIONABLE_MESSAGE_MAX_CHARS = 520
EOD_SEND_PROOF_DUE_HOUR = 22
EOD_SEND_PROOF_DUE_MINUTE = 15
EOD_SCHEDULE_HOUR = 21
EOD_SCHEDULE_MINUTE = 55
EOD_NOISE_MARKERS = ("ALSO:", "Citadel", "Marlowe", "/mnt/f", "parity=", "qwen=", "paths=", "goal=")
OPENCLAW_BASELANE_CRON_TOKENS = (
    "baselane_cron_run.sh",
    "baselane_financials_monthly_cron.sh",
    "baselane_financials_monthly_recovery_cron.sh",
    "baselane_weekly_file_updates_cron.sh",
    "baselane_local_model_preflight.py",
)
OPENCLAW_ALLOWED_BASELANE_BACKUP_JOB_IDS = {
    "baselane-daily-sync",
    "baselane-financials-monthly",
    "baselane-financials-monthly-recovery",
    "baselane-local-model-preflight",
}
OPENCLAW_ALLOWED_BASELANE_DECLARATION_KEYS = {
    "baselane-financials-monthly",
    "baselane-financials-monthly-recovery",
    "baselane-weekly-file-updates",
}
OPENCLAW_CANONICAL_EXT4_REPO = "/home/digit/.openclaw/workspace/repos/baselane-lofty-cpai"
OPENCLAW_MONTHLY_DISCORD_CODE_ROOT_ENV = "BASELANE_MONTHLY_DISCORD_CODE_ROOT"
STALE_SYSTEMD_PATH_MARKERS = (
    "/home/umbrel/.openclaw",
    "/home/umbrel/Dropbox",
)
SCHEDULER_REMEDIATION_APPROVAL_ENV = "BASELANE_SCHEDULER_REMEDIATION_APPROVED"
SCHEDULER_REMEDIATION_DIGEST_ENV = "BASELANE_SCHEDULER_REMEDIATION_DIGEST"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def sha256ish(value: object) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "")))


def default_root() -> Path:
    env_root = os.environ.get("WORKSPACE_ROOT")
    if env_root:
        return Path(env_root)
    cwd = Path.cwd()
    if (cwd / "scripts").is_dir() and (cwd / "reports").is_dir():
        return cwd
    return Path(__file__).absolute().parents[1]


def resolve_openclaw_root(root: Path) -> Path:
    configured = str(os.environ.get("OPENCLAW_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser().absolute()
    absolute_root = root.expanduser().absolute()
    for candidate in (absolute_root, *absolute_root.parents):
        if candidate.name == ".openclaw":
            return candidate
    default = Path.home() / ".openclaw"
    if default.is_dir():
        return default
    return absolute_root.parent


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing", "path": str(path)}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}


def file_age_hours(path: Path, now_epoch: float) -> float | None:
    try:
        return round((now_epoch - path.stat().st_mtime) / 3600, 2)
    except OSError:
        return None


def nested_value(data: dict[str, Any], dotted_key: str) -> tuple[bool, Any]:
    current: Any = data
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and value >= 0


def eod_message_quality(report: dict[str, Any]) -> dict[str, Any]:
    message = str(report.get("message") or "")
    line_count = len([line for line in message.splitlines() if line.strip()])
    character_count = len(message)
    noise_markers = [marker for marker in EOD_NOISE_MARKERS if marker in message]
    issues: list[str] = []
    if line_count > EOD_ACTIONABLE_MESSAGE_MAX_LINES:
        issues.append(f"too_many_lines={line_count}")
    if character_count > EOD_ACTIONABLE_MESSAGE_MAX_CHARS:
        issues.append(f"too_many_chars={character_count}")
    if noise_markers:
        issues.append(f"noise_markers={','.join(noise_markers)}")
    return {
        "ok": not issues,
        "line_count": line_count,
        "character_count": character_count,
        "max_lines": EOD_ACTIONABLE_MESSAGE_MAX_LINES,
        "max_chars": EOD_ACTIONABLE_MESSAGE_MAX_CHARS,
        "noise_markers": noise_markers,
        "issues": issues,
    }


def eod_send_state_source_report_scope_ok(send_state: dict[str, Any], root: Path) -> bool:
    source_report = str(send_state.get("source_report") or "").strip()
    if not source_report:
        return False
    root = root.expanduser().absolute()
    source_path = Path(source_report).expanduser()
    if not source_path.is_absolute():
        source_path = root / source_path
    try:
        source_path.absolute().relative_to(root)
    except ValueError:
        return False
    return True


def eod_send_state_usable_for_scheduler(
    send_state: dict[str, Any],
    send_state_quality: dict[str, Any],
    send_state_scope_ok: bool,
) -> bool:
    message = str(send_state.get("message") or "")
    digest = str(send_state.get("source_report_message_sha256") or "")
    sent_digest = str(send_state.get("telegram_sent_message_sha256") or "")
    digest_ok = (
        sha256ish(digest)
        and digest == sha256_text(message)
        and (not sent_digest or sent_digest == digest)
    )
    return (
        send_state.get("status") == "ok"
        and send_state.get("dry_run") is False
        and send_state.get("send_requested") is True
        and send_state.get("telegram_send_ok") is True
        and bool(send_state.get("telegram_http_statuses") or [])
        and send_state_quality["ok"]
        and send_state_scope_ok
        and digest_ok
        and bool(str(send_state.get("source_report_generated_at") or "").strip())
    )


def eod_send_proof_due(now: datetime | None = None) -> bool:
    local_now = (now or datetime.now().astimezone()).astimezone()
    return (local_now.hour, local_now.minute) >= (EOD_SEND_PROOF_DUE_HOUR, EOD_SEND_PROOF_DUE_MINUTE)


def parse_iso_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone()


def eod_send_state_covers_report_schedule_window(report: dict[str, Any], send_state: dict[str, Any]) -> bool:
    """Require a same-cycle proof when a report was generated after the EOD cron window."""
    report_time = parse_iso_datetime(report.get("generated_at"))
    if report_time is None:
        return True
    schedule_time = report_time.replace(
        hour=EOD_SCHEDULE_HOUR,
        minute=EOD_SCHEDULE_MINUTE,
        second=0,
        microsecond=0,
    )
    if report_time < schedule_time:
        return True
    sent_time = parse_iso_datetime(
        send_state.get("last_successful_send_at") or send_state.get("generated_at")
    )
    return sent_time is not None and sent_time >= schedule_time


def report_completeness_issues(report: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if report.get("status") in {"missing", "unreadable"}:
        return issues
    allow_empty_when = spec.get("allow_empty_report_fields_when") or {}
    for field in spec.get("required_report_fields") or []:
        present, value = nested_value(report, field)
        if not present:
            issues.append(f"report_missing_field:{field}")
        elif value == "" or value == [] or value == {}:
            conditions = allow_empty_when.get(field)
            allowed_empty = isinstance(conditions, dict) and all(
                nested_value(report, key) == (True, expected_value)
                for key, expected_value in conditions.items()
            )
            if not allowed_empty:
                issues.append(f"report_empty_field:{field}")
    for field, expected_value in (spec.get("required_report_values") or {}).items():
        present, value = nested_value(report, field)
        if present and value != expected_value:
            if (
                spec.get("name") == "monthly_financials"
                and field == "return_code"
                and report.get("status") == "review"
                and value == 2
            ):
                continue
            issues.append(f"report_unexpected_value:{field}={value}")
    for field in spec.get("non_negative_int_report_fields") or []:
        present, value = nested_value(report, field)
        if present and not non_negative_int(value):
            issues.append(f"report_invalid_non_negative_int:{field}")
    return issues


def effective_report_for_job(
    root: Path, spec: dict[str, Any], report: dict[str, Any], report_path: Path
) -> tuple[dict[str, Any], Path, list[str]]:
    """Return the scheduler-audit report source after safe reconciliation.

    Wrapper reports can lag behind the more specific canonical reports they
    invoke. The scheduler audit should flag scheduler/reporting health issues,
    not keep a stale wrapper failure active after its failing child gate has
    already been re-run and verified.
    """
    name = str(spec.get("name") or "")
    notes: list[str] = []
    if name == "daily_sync":
        canonical_path = root / "reports" / "baselane_daily_sync_report.json"
        canonical = read_json(canonical_path)
        if (
            canonical.get("effective_status", canonical.get("status")) == "ok"
            and int(canonical.get("issue_count") or 0) == 0
            and (canonical.get("effective_return_code", canonical.get("return_code")) == 0)
            and canonical.get("sync_report_status") == "ok"
        ):
            notes.append("using_canonical_daily_sync_report")
            effective = dict(canonical)
            effective["return_code"] = canonical.get("effective_return_code", canonical.get("return_code"))
            effective["failed_step"] = canonical.get("effective_failed_step", canonical.get("failed_step"))
            effective["steps"] = {
                "deterministic_sync": "ok",
                "human_paced_sync_fallback": str(
                    canonical.get("steps", {}).get("human_paced_sync_fallback")
                    if isinstance(canonical.get("steps"), dict)
                    else "not_started"
                ),
                "local_model_preflight": "ok" if canonical.get("local_model_ready") is True else "review",
            }
            effective["human_paced_backup_policy"] = canonical.get("human_paced_backup_policy")
            effective["human_paced_backup_enabled"] = canonical.get("human_paced_backup_enabled")
            effective["human_paced_backup_script_exists"] = canonical.get("human_paced_backup_script_exists")
            return effective, canonical_path, notes
        if (
            canonical.get("effective_status") == "ok"
            and canonical.get("effective_return_code", canonical.get("return_code")) == 0
            and canonical.get("sync_report_status") == "ok"
            and canonical.get("local_model_ready") is False
        ):
            notes.append("daily_sync_scheduler_ok_local_model_review_nonfatal")
            effective = dict(canonical)
            effective["status"] = "ok"
            effective["return_code"] = 0
            effective["failed_step"] = None
            effective["raw_status"] = canonical.get("status")
            effective["raw_issue_count"] = canonical.get("issue_count")
            effective["local_model_preflight_scheduler_status"] = "review_nonfatal"
            effective["steps"] = dict(canonical.get("steps") or {})
            effective["steps"]["deterministic_sync"] = "ok"
            effective["steps"]["local_model_preflight"] = str(
                effective["steps"].get("local_model_preflight") or "review_nonfatal"
            )
            return effective, canonical_path, notes
        if (
            report.get("effective_status") == "ok"
            and report.get("effective_return_code", report.get("return_code")) == 0
            and str(report.get("failed_step") or "") == "baselane_local_model_preflight"
        ):
            notes.append("daily_sync_wrapper_ok_except_local_model_review_nonfatal")
            effective = dict(report)
            effective["status"] = "ok"
            effective["return_code"] = 0
            effective["failed_step"] = None
            effective["sync_report_status"] = "ok"
            effective["raw_status"] = report.get("status")
            effective["local_model_preflight_scheduler_status"] = "review_nonfatal"
            return effective, report_path, notes
    if name == "monthly_financials":
        gate_path = root / "reports" / "baselane_monthly_statements_idempotent_report.json"
        gate = read_json(gate_path)
        guarded_apply_path = root / "reports" / "baselane_financials_monthly_guarded_apply.json"
        guarded_apply = read_json(guarded_apply_path)
        readiness_path = root / "reports" / "baselane_financials_monthly_readiness.json"
        readiness = read_json(readiness_path)
        owner_gate_path = root / "reports" / "baselane_monthly_owner_review_gate.json"
        owner_gate = read_json(owner_gate_path)
        cf_consistency_path = root / "reports" / "baselane_cf_balance_sheet_consistency_audit.json"
        cf_consistency = read_json(cf_consistency_path)
        gate_mtime = file_age_hours(gate_path, datetime.now(timezone.utc).timestamp())
        report_mtime = file_age_hours(report_path, datetime.now(timezone.utc).timestamp())
        cf_consistency_mtime = file_age_hours(cf_consistency_path, datetime.now(timezone.utc).timestamp())
        gate_newer_or_same = gate_path.exists() and report_path.exists() and gate_path.stat().st_mtime >= report_path.stat().st_mtime
        gate_fresh = gate_mtime is not None and gate_mtime <= 36
        cf_consistency_fresh = cf_consistency_mtime is not None and cf_consistency_mtime <= 36
        if (
            report.get("status") == "review"
            and report.get("return_code") == 124
            and report.get("failed_step") == "cf_balance_sheet_consistency"
            and cf_consistency.get("status") in {"ok", "review"}
            and cf_consistency.get("workbook_audit_status") != "timeout"
            and cf_consistency_fresh
        ):
            effective = dict(report)
            effective["return_code"] = 0
            effective["failed_step"] = None
            effective["reconciled_failed_step"] = "cf_balance_sheet_consistency"
            effective["reconciled_cf_consistency_report"] = str(cf_consistency_path)
            effective["reconciled_cf_consistency_status"] = cf_consistency.get("status")
            effective["reconciled_cf_consistency_issue_count"] = int(cf_consistency.get("issue_count") or 0)
            effective["reconciled_cf_consistency_age_hours"] = cf_consistency_mtime
            effective["reconciled_wrapper_age_hours"] = report_mtime
            notes.append("monthly_cf_balance_sheet_review_reconciled_after_wrapper_interrupt")
            return effective, report_path, notes
        if (
            report.get("status") == "failed"
            and report.get("failed_step") == "baselane_monthly_statements_idempotent"
            and gate.get("status") == "ok"
            and gate.get("monthly_script_return_code") == 0
            and int(gate.get("operator_issue_count") or 0) == 0
            and gate.get("download_ok") is True
            and (gate_newer_or_same or gate_fresh)
        ):
            effective = dict(report)
            effective["status"] = "review"
            effective["return_code"] = 0
            effective["failed_step"] = None
            effective["reconciled_failed_step"] = "baselane_monthly_statements_idempotent"
            effective["reconciled_gate_report"] = str(gate_path)
            effective["reconciled_gate_status"] = gate.get("status")
            effective["reconciled_gate_age_hours"] = gate_mtime
            effective["reconciled_wrapper_age_hours"] = report_mtime
            notes.append("monthly_statement_gate_resolved_after_wrapper_failure")
            return effective, report_path, notes
        if (
            report.get("status") == "failed"
            and report.get("failed_step") == "lofty_guarded_apply"
            and report.get("return_code") in {1, 2}
            and gate.get("status") == "ok"
            and guarded_apply.get("status") == "failed"
            and (readiness.get("status") == "review" or owner_gate.get("status") == "review")
        ):
            effective = dict(report)
            effective["status"] = "review"
            effective["return_code"] = 0
            effective["failed_step"] = None
            effective["reconciled_failed_step"] = "lofty_guarded_apply"
            effective["reconciled_gate_report"] = str(gate_path)
            effective["reconciled_guarded_apply_report"] = str(guarded_apply_path)
            effective["reconciled_readiness_report"] = str(readiness_path)
            effective["reconciled_owner_gate_report"] = str(owner_gate_path)
            effective["reconciled_gate_status"] = gate.get("status")
            effective["reconciled_guarded_apply_status"] = guarded_apply.get("status")
            effective["reconciled_readiness_status"] = readiness.get("status")
            effective["reconciled_owner_gate_status"] = owner_gate.get("status")
            effective["reconciled_gate_age_hours"] = gate_mtime
            effective["reconciled_wrapper_age_hours"] = report_mtime
            notes.append("monthly_guarded_apply_review_reconciled")
            return effective, report_path, notes
        if (
            report.get("status") == "failed"
            and report.get("failed_step") == "coownership_gl_policy_validation"
            and report.get("return_code") == 2
            and readiness.get("status") == "review"
        ):
            effective = dict(report)
            effective["status"] = "review"
            effective["return_code"] = 0
            effective["failed_step"] = None
            effective["reconciled_failed_step"] = "coownership_gl_policy_validation"
            effective["reconciled_readiness_report"] = str(readiness_path)
            effective["reconciled_readiness_status"] = readiness.get("status")
            effective["reconciled_gate_age_hours"] = gate_mtime
            effective["reconciled_wrapper_age_hours"] = report_mtime
            notes.append("monthly_coownership_policy_review_reconciled")
            return effective, report_path, notes
        effective_status = str(report.get("effective_status") or "").strip()
        if effective_status and effective_status != str(report.get("status") or "").strip():
            effective = dict(report)
            effective["status"] = effective_status
            effective["raw_status"] = report.get("status")
            notes.append("using_monthly_effective_status")
            return effective, report_path, notes
    if name == "eod_telegram":
        send_state_path = root / "reports" / "baselane_eod_telegram_send_state.json"
        send_state = read_json(send_state_path)
        send_state_quality = eod_message_quality(send_state)
        send_state_scope_ok = eod_send_state_source_report_scope_ok(send_state, root)
        send_state_usable = eod_send_state_usable_for_scheduler(
            send_state,
            send_state_quality,
            send_state_scope_ok,
        )
        report_needs_send_state = (
            report.get("dry_run") is True
            or report.get("send_requested") is not True
            or report.get("telegram_send_ok") is not True
        )
        if report_needs_send_state and send_state_usable and eod_send_state_covers_report_schedule_window(report, send_state):
            notes.append("using_last_successful_eod_telegram_send_state")
            send_state = dict(send_state)
            send_state["message_quality"] = send_state_quality
            return send_state, send_state_path, notes
        if report_needs_send_state and send_state_usable:
            notes.append("last_successful_eod_telegram_send_state_rejected_stale_schedule_cycle")
        if report_needs_send_state and send_state.get("status") == "ok" and not send_state_quality["ok"]:
            notes.append("last_successful_eod_telegram_send_state_rejected_message_quality")
        if report_needs_send_state and send_state.get("status") == "ok" and not send_state_scope_ok:
            notes.append("last_successful_eod_telegram_send_state_rejected_foreign_source_report")
        if report_needs_send_state and send_state.get("status") == "ok" and send_state.get("send_requested") is not True:
            notes.append("last_successful_eod_telegram_send_state_rejected_missing_send_request")
        if report_needs_send_state and send_state.get("status") == "ok":
            message = str(send_state.get("message") or "")
            digest = str(send_state.get("source_report_message_sha256") or "")
            sent_digest = str(send_state.get("telegram_sent_message_sha256") or "")
            if not (sha256ish(digest) and digest == sha256_text(message) and (not sent_digest or sent_digest == digest)):
                notes.append("last_successful_eod_telegram_send_state_rejected_message_digest")
            if not str(send_state.get("source_report_generated_at") or "").strip():
                notes.append("last_successful_eod_telegram_send_state_rejected_missing_source_report_generated_at")
    return report, report_path, notes


def scheduler_report_completeness_issues(
    spec: dict[str, Any],
    report: dict[str, Any],
    reconciliation_notes: list[str],
) -> list[str]:
    issues = report_completeness_issues(report, spec)
    if spec.get("name") != "eod_telegram" or report.get("dry_run") is not True:
        return issues
    if "using_last_successful_eod_telegram_send_state" in reconciliation_notes:
        return issues
    rejection_notes = [
        note.removeprefix("last_successful_eod_telegram_send_state_rejected_")
        for note in reconciliation_notes
        if note.startswith("last_successful_eod_telegram_send_state_rejected_")
    ]
    reason = ",".join(rejection_notes) if rejection_notes else "missing_or_unusable"
    if reason == "missing_or_unusable" and not eod_send_proof_due():
        reconciliation_notes.append("eod_telegram_send_proof_not_due_yet")
        return []
    return [f"last_successful_send_state_unusable:{reason}"]


def accepted_conditional_status(spec: dict[str, Any], report: dict[str, Any]) -> str | None:
    name = str(spec.get("name") or "")
    status = report.get("status")
    if name == "weekly_file_updates":
        if (
            status == "skipped_not_friday"
            and report.get("reason") == "not_friday"
            and report.get("return_code") == 0
            and report.get("day_of_week") != 5
        ):
            return "weekly_non_friday_skip_accepted"
        if (
            status == "already_done_for_week"
            and report.get("reason") == "state_file_matches_iso_week"
            and report.get("return_code") == 0
            and report.get("iso_week")
            and report.get("last_completed_week") == report.get("iso_week")
        ):
            return "weekly_already_done_skip_accepted"
    return None


def crontab_text() -> tuple[str, str | None]:
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return "", f"crontab read failed: {exc.__class__.__name__}: {exc}"
    if result.returncode not in {0, 1}:
        return result.stdout or "", f"crontab returned {result.returncode}: {result.stderr[-500:]}"
    return result.stdout or "", None


def systemd_user_timer_text() -> tuple[str, str | None]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-timers", "--all", "--no-pager", "--no-legend"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        return "", f"systemd user timer read failed: {exc.__class__.__name__}: {exc}"
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "Failed to connect to bus" in stderr or "No medium found" in stderr:
            return "", None
        return "", f"systemd user timer read returned {result.returncode}: {stderr[-500:]}"
    timer_units: list[str] = []
    for line in (result.stdout or "").splitlines():
        if not line.strip():
            continue
        for token in line.split():
            if token.endswith(".timer") and (
                token.startswith("baselane-") or token.startswith("lofty-cdp-")
            ):
                timer_units.append(token)
    chunks: list[str] = []
    seen: set[str] = set()
    for timer_unit in timer_units:
        if timer_unit in seen:
            continue
        seen.add(timer_unit)
        service_unit = timer_unit.removesuffix(".timer") + ".service"
        for unit in (timer_unit, service_unit):
            try:
                cat = subprocess.run(
                    ["systemctl", "--user", "cat", unit],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except Exception as exc:  # noqa: BLE001
                chunks.append(f"# systemd_user_timer_error {unit}: {exc.__class__.__name__}: {exc}")
                continue
            if cat.returncode == 0:
                chunks.append(f"# systemd_user_unit {unit}")
                chunks.append(cat.stdout or "")
            else:
                chunks.append(f"# systemd_user_timer_error {unit}: {(cat.stderr or '').strip()[-300:]}")
    return "\n".join(chunks), None


def systemd_user_unit_stale_path_issues() -> list[str]:
    unit_dir = Path(
        os.environ.get(
            "BASELANE_SCHEDULER_AUDIT_SYSTEMD_USER_DIR",
            str(Path.home() / ".config" / "systemd" / "user"),
        )
    )
    if not unit_dir.is_dir():
        return []
    issues: list[str] = []
    patterns = ("baselane-*.service", "baselane-*.timer", "lofty-cdp-*.service", "lofty-cdp-*.timer")
    for pattern in patterns:
        for path in sorted(unit_dir.glob(pattern)):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                issues.append(f"systemd_user_unit_unreadable:{path.name}:{exc.__class__.__name__}")
                continue
            stale_markers = [marker for marker in STALE_SYSTEMD_PATH_MARKERS if marker in text]
            if stale_markers:
                issues.append(f"systemd_user_stale_path:{path.name}:{','.join(stale_markers)}")
    return issues


def openclaw_runtime_config_issues(openclaw_root: Path) -> list[str]:
    """Detect an unusable local OpenClaw runtime configuration without exposing it."""
    config_path = openclaw_root / "openclaw.json"
    if not config_path.exists():
        return []
    try:
        if config_path.stat().st_size == 0:
            return ["openclaw_runtime_config:empty"]
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ["openclaw_runtime_config:invalid_json"]
    except OSError as exc:
        return [f"openclaw_runtime_config:unreadable:{exc.__class__.__name__}"]
    if not isinstance(payload, dict):
        return ["openclaw_runtime_config:invalid_root"]
    return []


def openclaw_runtime_config_required(normalized_job_texts: list[str]) -> bool:
    """An empty runtime config matters only when OpenClaw owns an enabled job."""
    return any(text.strip() for text in normalized_job_texts)


def normalize_openclaw_cron_jobs(raw: str, path: Path) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Return enabled OpenClaw cron evidence and guard issues.

    Baselane/Lofty PM scheduling is owned by the OpenClaw cron-network agent.
    Disabled historical records must not pollute scheduler references and enabled
    Baselane records outside the canonical agent jobs should fail loudly.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw, [], []
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        return raw, [], []
    lines: list[str] = []
    issues: list[str] = []
    ignored_records: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_text = json.dumps(job, sort_keys=True)
        job_id = str(job.get("id") or "")
        job_name = str(job.get("name") or "")
        job_label = job_id or job_name or "unknown"
        lower_label = f"{job_id} {job_name}".lower()
        lower_text = job_text.lower()
        if job.get("enabled") is False:
            if (
                "end-of-day summary" in lower_label
                or job_id == "eod-summary"
                or any(token.lower() in lower_text for token in OPENCLAW_BASELANE_CRON_TOKENS)
            ):
                ignored_records.append(
                    {
                        "id": job_id,
                        "name": job_name,
                        "enabled": False,
                        "schedule": job.get("schedule"),
                        "source": str(path),
                        "reason": "disabled_non_owner_openclaw_job_ignored",
                    }
                )
            continue
        if "end-of-day summary" in lower_label or job_id == "eod-summary":
            issues.append(f"openclaw_dense_eod_summary_enabled:{job_label}")
        elif any(token.lower() in lower_text for token in OPENCLAW_BASELANE_CRON_TOKENS) and not openclaw_baselane_backup_job_allowed(job):
            issues.append(f"openclaw_baselane_duplicate_enabled:{job_label}")
        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        schedule_expr = str(schedule.get("expr") or "").strip()
        schedule_kind = str(schedule.get("kind") or "").strip().lower()
        normalized_record = {
            "id": job_id,
            "declarationKey": job.get("declarationKey"),
            "name": job_name,
            "enabled": True,
            "agentId": job.get("agentId"),
            "sessionTarget": job.get("sessionTarget"),
            "schedule": schedule,
            "payload": job.get("payload"),
            "delivery": job.get("delivery"),
            "source": str(path),
        }
        if schedule_expr and schedule_kind in {"", "cron"}:
            lines.append(f"{schedule_expr} {json.dumps(normalized_record, sort_keys=True)}")
        else:
            lines.append(json.dumps(normalized_record, sort_keys=True))
    return "\n".join(lines), issues, ignored_records


def openclaw_payload_env_value(payload: dict[str, Any], key: str) -> str:
    env = payload.get("env") if isinstance(payload.get("env"), dict) else {}
    if key in env:
        return str(env[key]).strip()

    command_parts = [str(payload.get("message") or "")]
    if isinstance(payload.get("argv"), list):
        command_parts.extend(str(value) for value in payload["argv"])
    command_text = " ".join(command_parts)
    match = re.search(
        rf"(?:^|\s){re.escape(key)}=(?:['\"])?([^\s'\";]+)",
        command_text,
    )
    return match.group(1) if match else ""


def openclaw_monthly_comms_policy_allowed(payload: dict[str, Any]) -> bool:
    required = {
        "SEND_MONTHLY_DISCORD_REVIEW_DRAFTS": "1",
        "SEND_MONTHLY_DISCORD_PROPERTY_UPDATE": "0",
        "SEND_OWNER_EMAILS": "0",
        "SEND_NATIVE_LOFTY_OWNER_EMAILS": "0",
        "SEND_NON_NATIVE_OWNER_EMAILS": "0",
        "SEND_TRANSFER_RECONCILIATION_TELEGRAM": "0",
    }
    return all(openclaw_payload_env_value(payload, key) == expected for key, expected in required.items())


def openclaw_ext4_execution_allowed(payload: dict[str, Any], script_name: str) -> bool:
    script_path = f"{OPENCLAW_CANONICAL_EXT4_REPO}/scripts/{script_name}"
    payload_text = json.dumps(payload, sort_keys=True)
    return (
        str(payload.get("cwd") or "").rstrip("/") == OPENCLAW_CANONICAL_EXT4_REPO
        and openclaw_payload_env_value(payload, "WORKSPACE_ROOT").rstrip("/")
        == OPENCLAW_CANONICAL_EXT4_REPO
        and script_path in payload_text
    )


def openclaw_monthly_ext4_execution_allowed(payload: dict[str, Any], script_name: str) -> bool:
    return (
        openclaw_ext4_execution_allowed(payload, script_name)
        and openclaw_payload_env_value(payload, OPENCLAW_MONTHLY_DISCORD_CODE_ROOT_ENV).rstrip("/")
        == OPENCLAW_CANONICAL_EXT4_REPO
    )


def openclaw_baselane_backup_job_allowed(job: dict[str, Any]) -> bool:
    job_id = str(job.get("id") or "")
    declaration_key = str(job.get("declarationKey") or "")
    if (
        job_id not in OPENCLAW_ALLOWED_BASELANE_BACKUP_JOB_IDS
        and declaration_key not in OPENCLAW_ALLOWED_BASELANE_DECLARATION_KEYS
    ):
        return False
    delivery = job.get("delivery") if isinstance(job.get("delivery"), dict) else {}
    if str(delivery.get("mode") or "").lower() not in {"", "none"}:
        return False
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    payload_text = json.dumps(payload, sort_keys=True).lower()
    if job_id == "baselane-daily-sync":
        base_allowed = (
            job.get("agentId") == "cron-network"
            and job.get("sessionTarget") == "isolated"
            and "baselane_cron_run.sh" in payload_text
            and openclaw_payload_env_value(payload, "BASELANE_CRON_HUMAN_PACED_FALLBACK") == "1"
            and openclaw_ext4_execution_allowed(payload, "baselane_cron_run.sh")
        )
        if payload.get("kind") == "command":
            return base_allowed
        return bool(
            base_allowed
            and "no_reply" in payload_text
            and "do not use message tool" in payload_text
            and "reports/baselane_daily_run_report.json" in payload_text
        )
    if job_id == "baselane-financials-monthly" or declaration_key == "baselane-financials-monthly":
        base_allowed = (
            job.get("agentId") == "cron-network"
            and job.get("sessionTarget") == "isolated"
            and "baselane_financials_monthly_cron.sh" in payload_text
            and openclaw_payload_env_value(payload, "DRY_RUN") == "0"
            and openclaw_payload_env_value(payload, "BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED") == "1"
            and openclaw_payload_env_value(payload, "AUTO_APPROVE_SAFE_REVIEW_CANDIDATES") == "1"
            and openclaw_payload_env_value(payload, "APPLY_LOFTY_GUARDED_UPDATES") == "1"
            and openclaw_payload_env_value(payload, "YHOME_GSHEET_APPLY") == "1"
            and openclaw_payload_env_value(payload, "YHOME_GSHEET_WRITE_ENABLED") == "1"
            and openclaw_monthly_comms_policy_allowed(payload)
            and openclaw_monthly_ext4_execution_allowed(
                payload, "baselane_financials_monthly_cron.sh"
            )
        )
        if payload.get("kind") == "command":
            return base_allowed
        return bool(
            base_allowed
            and job.get("sessionTarget") == "isolated"
            and "must not downgrade" in payload_text
            and "no_reply" in payload_text
            and "do not use message tool" in payload_text
            and "reports/baselane_financials_monthly_run_report.json" in payload_text
        )
    if (
        job_id == "baselane-financials-monthly-recovery"
        or declaration_key == "baselane-financials-monthly-recovery"
    ):
        base_allowed = (
            job.get("agentId") == "cron-network"
            and job.get("sessionTarget") == "isolated"
            and "baselane_financials_monthly_recovery_cron.sh" in payload_text
            and openclaw_payload_env_value(payload, "DRY_RUN") == "0"
            and openclaw_payload_env_value(payload, "BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED") == "1"
            and openclaw_payload_env_value(payload, "AUTO_APPROVE_SAFE_REVIEW_CANDIDATES") == "1"
            and openclaw_payload_env_value(payload, "APPLY_LOFTY_GUARDED_UPDATES") == "1"
            and openclaw_payload_env_value(payload, "YHOME_GSHEET_APPLY") == "1"
            and openclaw_payload_env_value(payload, "YHOME_GSHEET_WRITE_ENABLED") == "1"
            and openclaw_monthly_comms_policy_allowed(payload)
            and openclaw_monthly_ext4_execution_allowed(
                payload, "baselane_financials_monthly_recovery_cron.sh"
            )
        )
        if payload.get("kind") == "command":
            return base_allowed
        return bool(
            base_allowed
            and job.get("sessionTarget") == "isolated"
            and "must not downgrade" in payload_text
            and "no_reply" in payload_text
            and "do not use message tool" in payload_text
        )
    if job_id == "baselane-local-model-preflight":
        return (
            payload.get("kind") == "command"
            and "baselane_local_model_preflight.py" in payload_text
            and "outputmaxbytes" in payload_text
        )
    if declaration_key == "baselane-weekly-file-updates":
        base_allowed = (
            job.get("agentId") == "cron-network"
            and job.get("sessionTarget") == "isolated"
            and "baselane_weekly_file_updates_cron.sh" in payload_text
            and openclaw_ext4_execution_allowed(payload, "baselane_weekly_file_updates_cron.sh")
        )
        if payload.get("kind") == "command":
            return base_allowed
        return bool(
            base_allowed
            and "no_reply" in payload_text
            and "do not use message tool" in payload_text
        )
    return False


def read_openclaw_sqlite_cron_jobs(openclaw_root: Path) -> tuple[str | None, Path, list[str]]:
    sqlite_path = Path(
        os.environ.get("OPENCLAW_CRON_SQLITE_PATH")
        or openclaw_root / "state" / "openclaw.sqlite"
    ).expanduser()
    if not sqlite_path.is_file():
        return None, sqlite_path, []
    store_key = str(openclaw_root / "cron" / "jobs.json")
    try:
        connection = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT job_json FROM cron_jobs WHERE store_key = ? ORDER BY sort_order, updated_at, job_id",
                (store_key,),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        return None, sqlite_path, [f"{sqlite_path} unreadable: {exc.__class__.__name__}"]
    if not rows:
        return None, sqlite_path, []
    jobs: list[dict[str, Any]] = []
    for (raw_job,) in rows:
        try:
            job = json.loads(raw_job)
        except (TypeError, json.JSONDecodeError):
            return None, sqlite_path, [f"{sqlite_path} invalid cron job JSON"]
        if isinstance(job, dict):
            jobs.append(job)
    return json.dumps({"jobs": jobs}), sqlite_path, []


def scheduler_remediation_records(issues: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in issues:
        match = re.fullmatch(r"scheduler_source:(.+):(openclaw_[a-z_]+):(.*)", str(item))
        if not match:
            continue
        source, issue_class, job_id = match.groups()
        if issue_class not in {"openclaw_baselane_duplicate_enabled", "openclaw_dense_eod_summary_enabled"}:
            continue
        key = (source, issue_class, job_id)
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "action": "disable_openclaw_job",
                "requires_explicit_approval": True,
                "source": source,
                "issue_class": issue_class,
                "job_id": job_id,
            }
        )
    return records


def remediation_digest(records: list[dict[str, Any]]) -> str:
    stable_records = [
        {
            "action": str(record.get("action") or ""),
            "requires_explicit_approval": record.get("requires_explicit_approval") is True,
            "source": str(record.get("source") or ""),
            "job_id": str(record.get("job_id") or ""),
        }
        for record in records
    ]
    return sha256_text(json.dumps(stable_records, sort_keys=True, separators=(",", ":")))


def scheduler_only_eod_telegram_issues(issues: list[str]) -> bool:
    return bool(issues) and all(str(issue).startswith("eod_telegram:") for issue in issues)


def scheduler_only_daily_sync_issues(issues: list[str]) -> bool:
    return bool(issues) and all(str(issue).startswith("daily_sync:") for issue in issues)


def scheduler_openclaw_runtime_config_issues(issues: list[str]) -> list[str]:
    return [
        str(issue)
        for issue in issues
        if str(issue).startswith("scheduler_source:openclaw_runtime_config:")
    ]


def eod_telegram_credentials_missing(root: Path) -> bool:
    report = read_json(root / "reports" / "baselane_eod_telegram_report.json")
    return (
        report.get("telegram_token_present") is False
        or report.get("telegram_chat_id_present") is False
    )


def daily_sync_report_blocker(root: Path, issues: list[str]) -> dict[str, Any] | None:
    report_path = root / "reports" / "baselane_daily_sync_report.json"
    report = read_json(report_path)
    next_action = str(report.get("next_action") or "").strip()
    if not next_action:
        return None
    artifact = report_path
    disk_report = str(report.get("disk_space_preflight_report") or "").strip()
    blocker_id = "daily_sync_report_not_ok"
    blocker_class = "scheduler.daily_sync_report_not_ok"
    summary = "Daily Baselane sync report not OK"
    blocker = "Daily Baselane sync report not OK"
    disk_issues = []
    if report.get("disk_space_preflight_status") == "review" and disk_report:
        artifact = Path(disk_report)
        if not artifact.is_absolute():
            artifact = root / artifact
        blocker_id = "daily_sync_disk_space_preflight"
        blocker_class = "scheduler.daily_sync_disk_space_preflight"
        disk_issues = [str(issue) for issue in report.get("disk_space_preflight_issues") or [] if str(issue)]
        if disk_issues:
            summary = f"Daily Baselane sync blocked by low local disk space ({disk_issues[0]})"
        else:
            summary = "Daily Baselane sync blocked by low local disk space"
        blocker = "Low local disk space blocks daily Baselane sync"
    return {
        "id": blocker_id,
        "class": blocker_class,
        "summary": summary,
        "blocker": blocker,
        "count": len(issues),
        "artifact": str(artifact),
        "source_report": str(report_path),
        "disk_space_preflight_issues": disk_issues,
        "next_action": next_action,
        "hold": "weekly/monthly document updates",
    }


def scheduler_actionable_summary(root: Path, issues: list[str]) -> dict[str, Any]:
    records = scheduler_remediation_records(issues)
    if records:
        digest = remediation_digest(records)
        script_path = root / "reports" / "baselane_scheduler_remediation.requires-explicit-approval.sh"
        job_labels = ", ".join(record["job_id"] for record in records[:4])
        if len(records) > 4:
            job_labels += f", +{len(records) - 4}"
        primary = {
            "id": "scheduler_stale_openclaw_jobs",
            "class": "scheduler.openclaw_stale_enabled_jobs",
            "summary": f"{len(records)} stale enabled OpenClaw scheduler job(s): {job_labels}",
            "blocker": f"{len(records)} stale enabled OpenClaw scheduler job(s)",
            "count": len(records),
            "artifact": str(script_path),
            "next_action": (
                f"Review {script_path}; if approved, run it with "
                f"{SCHEDULER_REMEDIATION_APPROVAL_ENV}=1 and {SCHEDULER_REMEDIATION_DIGEST_ENV}={digest}."
            ),
            "hold": "weekly/monthly document updates and EOD clarity",
        }
        return {
            "actionable_blocker_count": 1,
            "primary_blocker": primary,
            "remediation_record_count": len(records),
            "remediation_records": records,
            "remediation_digest": digest,
            "remediation_commands_file": str(script_path),
            "requires_explicit_approval": True,
            "approval_env_var": SCHEDULER_REMEDIATION_APPROVAL_ENV,
            "approval_env_required_value": "1",
            "approval_digest_env_var": SCHEDULER_REMEDIATION_DIGEST_ENV,
            "approval_digest_required_value": digest,
        }
    if scheduler_only_eod_telegram_issues(issues):
        credentials_missing = eod_telegram_credentials_missing(root)
        blocker = "EOD Telegram credentials/proof" if credentials_missing else "EOD Telegram send proof"
        blocker_id = "eod_telegram_credentials_or_proof" if credentials_missing else "eod_telegram_send_proof"
        blocker_class = (
            "scheduler.eod_telegram_credentials_or_proof"
            if credentials_missing
            else "scheduler.eod_telegram_send_proof"
        )
        artifact = (
            root / "reports" / "baselane_eod_telegram_report.json"
            if credentials_missing
            else root / "reports" / "baselane_eod_telegram_send_state.json"
        )
        next_action = (
            "Set TELEGRAM_BOT_TOKEN and BASELANE_EOD_TELEGRAM_CHAT_ID, send one concise non-dry-run EOD, then rerun scheduler audit."
            if credentials_missing
            else "Send one concise non-dry-run EOD Telegram report, then rerun scheduler audit."
        )
        return {
            "actionable_blocker_count": 1,
            "primary_blocker": {
                "id": blocker_id,
                "class": blocker_class,
                "summary": blocker,
                "blocker": blocker,
                "count": len(issues),
                "artifact": str(artifact),
                "next_action": next_action,
                "hold": "failure visibility until Telegram send is proven",
            },
        }
    runtime_config_issues = scheduler_openclaw_runtime_config_issues(issues)
    if runtime_config_issues:
        config_path = resolve_openclaw_root(root) / "openclaw.json"
        return {
            "actionable_blocker_count": 1,
            "primary_blocker": {
                "id": "openclaw_runtime_config_unusable",
                "class": "scheduler.openclaw_runtime_config",
                "summary": "OpenClaw runtime configuration is unusable",
                "blocker": "OpenClaw runtime configuration",
                "count": len(runtime_config_issues),
                "artifact": str(config_path),
                "next_action": (
                    f"Restore or re-render {config_path} using the existing config workflow, "
                    "then rerun scheduler audit; keep Baselane/Lofty sends blocked until it passes."
                ),
                "hold": "Baselane/Lofty scheduled syncs and downstream sends",
            },
        }
    if scheduler_only_daily_sync_issues(issues):
        primary = daily_sync_report_blocker(root, issues)
        if primary:
            return {
                "actionable_blocker_count": 1,
                "primary_blocker": primary,
            }
    if issues:
        return {
            "actionable_blocker_count": 1,
            "primary_blocker": {
                "id": "scheduler_audit_issue",
                "class": "scheduler.audit_issue",
                "summary": f"{len(issues)} scheduler audit issue(s)",
                "blocker": f"{len(issues)} scheduler audit issue(s)",
                "count": len(issues),
                "artifact": str(root / "reports" / "baselane_scheduler_audit_report.json"),
                "next_action": "Open the scheduler audit report and fix the listed scheduler issue(s), then rerun the audit.",
                "hold": "weekly/monthly document updates",
            },
        }
    return {"actionable_blocker_count": 0, "primary_blocker": None}


def write_scheduler_remediation_script(root: Path, actionable_summary: dict[str, Any]) -> None:
    records = actionable_summary.get("remediation_records")
    if not isinstance(records, list) or not records:
        return
    commands_file = Path(str(actionable_summary.get("remediation_commands_file") or ""))
    if not commands_file:
        return
    commands_file.parent.mkdir(parents=True, exist_ok=True)
    digest = str(actionable_summary.get("remediation_digest") or "")
    job_ids = [str(record.get("job_id") or "") for record in records if record.get("job_id")]
    sources = sorted({str(record.get("source") or "") for record in records if record.get("source")})
    jobs_json = (
        sources[0]
        if len(sources) == 1
        else str(resolve_openclaw_root(root) / "cron" / "jobs.json")
    )
    job_args = " ".join(f"--disable-job-id {json.dumps(job_id)}" for job_id in job_ids)
    commands_file.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "# Generated by baselane_scheduler_audit.py. Does not run unless explicit approval env vars match.",
                f'[[ "${{{SCHEDULER_REMEDIATION_APPROVAL_ENV}:-}}" == "1" ]] || '
                + "{ echo 'Refusing scheduler remediation: explicit approval env var is required.' >&2; exit 64; }",
                f'[[ "${{{SCHEDULER_REMEDIATION_DIGEST_ENV}:-}}" == "{digest}" ]] || '
                + "{ echo 'Refusing scheduler remediation: digest env var does not match this report.' >&2; exit 65; }",
                "cd " + json.dumps(str(root)),
                (
                    "python3 scripts/baselane_scheduler_remediate.py "
                    f"--jobs-json {json.dumps(jobs_json)} "
                    f"{job_args} "
                    f"--expected-digest {json.dumps(digest)} "
                    "--apply "
                    "--report reports/baselane_scheduler_remediation_report.json"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    commands_file.chmod(0o755)


def read_scheduler_sources(root: Path) -> tuple[list[dict[str, str]], list[str], list[dict[str, Any]]]:
    sources: list[dict[str, str]] = []
    errors: list[str] = []
    ignored_records: list[dict[str, Any]] = []
    text, error = crontab_text()
    sources.append({"name": "user_crontab", "path": "crontab -l", "text": text})
    if error:
        errors.append(error)

    systemd_text, systemd_error = systemd_user_timer_text()
    if systemd_text:
        sources.append({"name": "systemd_user_timers", "path": "systemctl --user list-timers", "text": systemd_text})
    if systemd_error:
        errors.append(systemd_error)
    errors.extend(systemd_user_unit_stale_path_issues())

    openclaw_root = resolve_openclaw_root(root)
    sqlite_raw, sqlite_path, sqlite_errors = read_openclaw_sqlite_cron_jobs(openclaw_root)
    errors.extend(sqlite_errors)
    source_paths = []
    if sqlite_raw is None:
        source_paths.extend(
            [
                openclaw_root / "cron" / "jobs.json",
                openclaw_root / "cron" / "jobs.primary.json",
            ]
        )
    extra_sources = os.environ.get("BASELANE_SCHEDULER_AUDIT_EXTRA_CRON_SOURCES", "")
    for extra in extra_sources.split(os.pathsep):
        extra = extra.strip()
        if extra:
            source_paths.append(Path(extra))

    seen_paths: set[Path] = set()
    seen_openclaw_job_payloads: set[str] = set()
    normalized_openclaw_job_texts: list[str] = []
    if sqlite_raw is not None:
        source_text, guard_issues, ignored = normalize_openclaw_cron_jobs(sqlite_raw, sqlite_path)
        errors.extend(f"{sqlite_path}:{item}" for item in guard_issues)
        ignored_records.extend(ignored)
        normalized_openclaw_job_texts.append(source_text)
        sources.append({"name": "openclaw_cron_sqlite", "path": str(sqlite_path), "text": source_text})
    for path in source_paths:
        normalized_path = path.expanduser()
        if normalized_path in seen_paths:
            continue
        seen_paths.add(normalized_path)
        if not normalized_path.exists():
            continue
        try:
            raw = normalized_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{normalized_path} unreadable: {exc}")
            continue
        if raw in seen_openclaw_job_payloads:
            continue
        seen_openclaw_job_payloads.add(raw)
        source_text, guard_issues, ignored = normalize_openclaw_cron_jobs(raw, normalized_path)
        errors.extend(f"{normalized_path}:{item}" for item in guard_issues)
        ignored_records.extend(ignored)
        normalized_openclaw_job_texts.append(source_text)
        sources.append({"name": "openclaw_cron_jobs", "path": str(normalized_path), "text": source_text})
    if openclaw_runtime_config_required(normalized_openclaw_job_texts):
        errors.extend(openclaw_runtime_config_issues(openclaw_root))
    return sources, errors, ignored_records


def scheduler_mentions(sources: list[dict[str, str]], terms: list[str]) -> list[dict[str, str]]:
    matches = []
    for source in sources:
        text = source.get("text") or ""
        source_matches = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            lower_line = stripped.lower()
            matched_terms = [term for term in terms if term.lower() in lower_line]
            if matched_terms:
                source_matches.append(
                    {
                        "line": stripped[:4000],
                        "matched_terms": ", ".join(matched_terms),
                    }
                )
        if source_matches:
            matches.append(
                {
                    "source": source.get("name") or "",
                    "path": source.get("path") or "",
                    "matched_terms": ", ".join(sorted({term for match in source_matches for term in match["matched_terms"].split(", ")})),
                    "matching_lines": source_matches,
                }
            )
    return matches


def scheduler_line_guard_matches(
    mentions: list[dict[str, Any]], fragment_sets: list[list[str]]
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for mention in mentions:
        for line_match in mention.get("matching_lines") or []:
            line = str(line_match.get("line") or "")
            for fragments in fragment_sets:
                missing = [fragment for fragment in fragments if fragment not in line]
                if not missing:
                    matches.append(
                        {
                            "source": mention.get("source") or "",
                            "path": mention.get("path") or "",
                            "line": line,
                            "fragment_set": fragments,
                        }
                    )
    return matches


def scheduler_owner_reference_lines(mentions: list[dict[str, Any]]) -> list[dict[str, str]]:
    owner_lines: list[dict[str, str]] = []
    for mention in mentions:
        source = str(mention.get("source") or "")
        path = str(mention.get("path") or "")
        for line_match in mention.get("matching_lines") or []:
            line = str(line_match.get("line") or "").strip()
            if line:
                owner_lines.append({"source": source, "path": path, "line": line})
    return owner_lines


def build_report(root: Path) -> dict[str, Any]:
    now = datetime.now(timezone.utc).timestamp()
    scheduler_source_result = read_scheduler_sources(root)
    if len(scheduler_source_result) == 2:
        scheduler_sources, scheduler_errors = scheduler_source_result
        ignored_scheduler_records = []
    else:
        scheduler_sources, scheduler_errors, ignored_scheduler_records = scheduler_source_result
    jobs = []
    issues = []
    for spec in JOB_SPECS:
        script = root / spec["script"]
        report_path = root / spec["report"]
        report = read_json(report_path)
        report, effective_report_path, reconciliation_notes = effective_report_for_job(root, spec, report, report_path)
        status = report.get("status")
        conditional_status_note = accepted_conditional_status(spec, report)
        if conditional_status_note:
            reconciliation_notes.append(conditional_status_note)
        age_hours = file_age_hours(effective_report_path, now)
        mentions = scheduler_mentions(scheduler_sources, spec["scheduler_terms"])
        owner_reference_lines = scheduler_owner_reference_lines(mentions)
        required_fragment_sets = list(spec.get("required_scheduler_line_fragment_sets") or [])
        line_guard_matches = scheduler_line_guard_matches(mentions, required_fragment_sets)
        completeness_issues = scheduler_report_completeness_issues(spec, report, reconciliation_notes)
        job_issues = []
        if not script.is_file():
            job_issues.append("script_missing")
        if report.get("status") == "missing":
            job_issues.append("report_missing")
        elif status not in spec["expected_statuses"] and not conditional_status_note:
            job_issues.append(f"unexpected_report_status:{status}")
        if age_hours is None:
            job_issues.append("report_age_unknown")
        elif age_hours > float(spec["max_report_age_hours"]):
            job_issues.append(f"report_stale_hours:{age_hours}")
        if not mentions:
            job_issues.append("scheduler_reference_not_found")
        elif required_fragment_sets and not line_guard_matches:
            job_issues.append("scheduler_schedule_mismatch")
        if len(owner_reference_lines) > 1:
            job_issues.append(f"duplicate_scheduler_references:{len(owner_reference_lines)}")
        job_issues.extend(completeness_issues)
        issues.extend(f"{spec['name']}:{issue}" for issue in job_issues)
        jobs.append(
            {
                "name": spec["name"],
                "script": str(script),
                "script_exists": script.is_file(),
                "report": str(report_path),
                "effective_report": str(effective_report_path),
                "effective_report_reconciliation": reconciliation_notes,
                "report_exists": report_path.is_file(),
                "report_status": status,
                "report_age_hours": age_hours,
                "max_report_age_hours": spec["max_report_age_hours"],
                "expected_statuses": sorted(spec["expected_statuses"]),
                "scheduler_reference_count": len(mentions),
                "scheduler_references": mentions,
                "scheduler_owner_reference_count": len(owner_reference_lines),
                "scheduler_owner_reference_lines": owner_reference_lines,
                "scheduler_line_guard_ok": bool(line_guard_matches) if required_fragment_sets else None,
                "scheduler_line_guard_matches": line_guard_matches,
                "required_scheduler_line_fragment_sets": required_fragment_sets,
                "report_completeness_ok": not completeness_issues,
                "required_report_fields": list(spec.get("required_report_fields") or []),
                "required_report_values": spec.get("required_report_values") or {},
                "non_negative_int_report_fields": list(spec.get("non_negative_int_report_fields") or []),
                "issues": job_issues,
            }
        )
    if scheduler_errors:
        issues.extend(f"scheduler_source:{error}" for error in scheduler_errors)
    actionable_summary = scheduler_actionable_summary(root, issues)
    active_owner_sources = sorted(
        {
            str(line.get("source") or "")
            for job in jobs
            for line in (job.get("scheduler_owner_reference_lines") or [])
            if isinstance(line, dict) and line.get("source")
        }
    )
    return {
        "generated_at": iso_z(),
        "status": "ok" if not issues else "review",
        "issue_count": len(issues),
        "issues": issues,
        "actionable_summary": actionable_summary,
        "primary_blocker": actionable_summary.get("primary_blocker"),
        "next_action": (
            actionable_summary.get("primary_blocker", {}).get("next_action")
            if isinstance(actionable_summary.get("primary_blocker"), dict)
            else None
        ),
        "scheduler_source_count": len(scheduler_sources),
        "scheduler_source_errors": scheduler_errors,
        "active_scheduler_owner_sources": active_owner_sources,
        "ignored_scheduler_record_count": len(ignored_scheduler_records),
        "ignored_scheduler_records": ignored_scheduler_records,
        "jobs": jobs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only audit for Baselane scheduled reporting jobs.")
    parser.add_argument("--root", default=str(default_root()))
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    report = build_report(root)
    write_scheduler_remediation_script(root, report.get("actionable_summary") if isinstance(report.get("actionable_summary"), dict) else {})
    report_path = Path(args.report).expanduser() if args.report else root / "reports" / "baselane_scheduler_audit_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "issue_count", "scheduler_source_count")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
