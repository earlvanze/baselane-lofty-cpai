#!/usr/bin/env python3
"""Audit downloader coverage for co-owner-paid mortgage properties."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlparse

SCRIPT_PATH = Path(__file__).absolute()
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", SCRIPT_PATH.parents[1]))
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import update_coownership_mortgage_tokenomics as tokenomics
from stable_json_report import write_json_report

DEFAULT_CONFIG = WORKSPACE_ROOT / "config" / "mortgage_statement_downloaders.json"
DEFAULT_INTAKE = WORKSPACE_ROOT / "config" / "mortgage_downloader_intake.json"
DEFAULT_REPORT = WORKSPACE_ROOT / "reports" / "mortgage_downloader_coverage_report.json"
DEFAULT_HANDOFF_DIR = WORKSPACE_ROOT / "reports"
DEFAULT_TOKENOMICS_REPORT = WORKSPACE_ROOT / "reports" / "coownership_mortgage_tokenomics_update_report.json"
DEFAULT_HAR_ROOT = Path(os.environ.get("MORTGAGE_WORKFLOW_HAR_ROOT", "/mnt/f/har"))
TARGET_MONTH_ENV_KEYS = (
    "MORTGAGE_STATEMENT_TARGET_MONTH",
    "BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH",
    "MORTGAGE_WORKFLOW_TARGET_MONTH",
)
CAPTURE_PRECONDITION_KEYS = [
    "capture_precondition_status",
    "capture_precondition_blocker",
    "capture_precondition_reason",
    "capture_precondition_manual_auth_required",
    "capture_precondition_next_action",
    "capture_precondition_safe_to_capture_now",
    "capture_precondition_prepare_command",
    "capture_precondition_safe_to_prepare_now",
]
AUTH_BLOCKER_REASONS = {
    "account_locked",
    "blocked_account_locked",
    "blocked_previous_account_locked",
    "password_reset_in_progress",
    "previous_account_locked",
    "authenticated_browser_required",
    "credential_rejected_before_mfa",
    "credentials_unavailable",
    "login_rejected",
    "login_still_required_after_submit",
    "visible_loandepot_tab_not_authenticated",
}
WORKFLOW_QUEUE_CONTEXT_KEYS = (
    "capture_auth_blocker",
    "capture_auth_blocker_reason",
    "capture_auth_next_action",
    "capture_command_blocked_reason",
    "capture_command_ready_to_run_now",
    "command_precondition_status",
    "command_precondition_reason",
    "command_precondition_next_action",
    "live_cdp_downloader_command",
    "live_cdp_downloader_ready_after_visible_auth",
    "source_downloader_id",
    "source_downloader_status",
    "source_downloader_report_status",
    "source_downloader_runtime_status",
    "source_downloader_manual_auth_required",
    "source_downloader_operator_next_action",
    "source_downloader_suggested_next_action",
)
LATEST_REPORT_AUTH_CONTEXT_KEYS = (
    "manual_auth_reason",
    "auth_failure_reason",
    "auth_failure_visible_reason",
    "auth_mfa_reached",
    "auth_issue",
    "auth_issue_text",
    "auth_state",
    "auth_stage",
    "login_mode",
    "auto_login_attempted",
    "auto_login_status",
    "auto_login_step",
    "auto_login_blocked_reason",
    "credentials_available",
    "credential_source",
    "credential_lookup_status",
    "credential_lookup_failure_reason",
    "credential_lookup_item_name",
    "credential_lookup_expected_folder_name",
    "credential_lookup_uri_host",
    "credential_lookup_candidate_count",
    "credential_lookup_candidate_items",
    "credential_lookup_unguarded_candidate_count",
    "credential_lookup_unguarded_candidate_items",
    "credential_lookup_misfiled_candidate_count",
    "credential_lookup_absent",
    "credential_lookup_scope",
    "credential_lookup_repair_action",
    "credential_login_failure_suspected",
    "credential_login_failure_suspected_reason",
)
LATEST_REPORT_AUTH_CONTEXT_LIST_KEYS = {
    "manual_auth_reason": "manual_auth_reasons",
    "auth_failure_reason": "auth_failure_reasons",
    "auth_failure_visible_reason": "auth_failure_visible_reasons",
    "auth_mfa_reached": "auth_mfa_reached_values",
    "auth_issue": "auth_issues",
    "auth_issue_text": "auth_issue_texts",
    "auth_state": "auth_states",
    "auth_stage": "auth_stages",
    "login_mode": "login_modes",
    "auto_login_attempted": "auto_login_attempted_values",
    "auto_login_status": "auto_login_statuses",
    "auto_login_step": "auto_login_steps",
    "auto_login_blocked_reason": "auto_login_blocked_reasons",
    "credentials_available": "credentials_available_values",
    "credential_source": "credential_sources",
    "credential_lookup_status": "credential_lookup_statuses",
    "credential_lookup_failure_reason": "credential_lookup_failure_reasons",
    "credential_lookup_item_name": "credential_lookup_item_names",
    "credential_lookup_expected_folder_name": "credential_lookup_expected_folder_names",
    "credential_lookup_uri_host": "credential_lookup_uri_hosts",
    "credential_lookup_candidate_count": "credential_lookup_candidate_counts",
    "credential_lookup_candidate_items": "credential_lookup_candidate_items",
    "credential_lookup_unguarded_candidate_count": "credential_lookup_unguarded_candidate_counts",
    "credential_lookup_unguarded_candidate_items": "credential_lookup_unguarded_candidate_items",
    "credential_lookup_misfiled_candidate_count": "credential_lookup_misfiled_candidate_counts",
    "credential_lookup_absent": "credential_lookup_absent_values",
    "credential_lookup_scope": "credential_lookup_scopes",
    "credential_lookup_repair_action": "credential_lookup_repair_actions",
    "credential_login_failure_suspected": "credential_login_failure_suspected_values",
    "credential_login_failure_suspected_reason": "credential_login_failure_suspected_reasons",
}


def latest_report_auth_context(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in LATEST_REPORT_AUTH_CONTEXT_KEYS:
        value = item.get(f"latest_report_{key}")
        if value not in (None, "", [], {}):
            result[key] = value
    return result


def latest_report_auth_context_lists(items: list[dict[str, Any]]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for key in LATEST_REPORT_AUTH_CONTEXT_KEYS:
        values: list[Any] = []
        for item in items:
            value = item.get(f"latest_report_{key}")
            if value in (None, "", [], {}):
                continue
            if value not in values:
                values.append(value)
        if values:
            result[LATEST_REPORT_AUTH_CONTEXT_LIST_KEYS[key]] = values
    return result


def normalize_property(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def slugify(value: object) -> str:
    text = normalize_property(value)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "unknown"


def default_target_month() -> str:
    for key in TARGET_MONTH_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if re.fullmatch(r"\d{4}-\d{2}", value):
            return value
    return tokenomics.current_month_name()


def portal_host(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown-portal"
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = parsed.netloc or parsed.path.split("/")[0]
    return host.lower().removeprefix("www.") or "unknown-portal"


def suggested_workflow_har_path(property_name: object, portal_url: object, har_root: Path = DEFAULT_HAR_ROOT) -> str:
    return str(har_root / f"{slugify(property_name)}_{slugify(portal_host(portal_url))}.har")


def workflow_evidence_handoff_command(property_name: object) -> str:
    return f"python3 scripts/mortgage_workflow_evidence_handoff.py --property {shlex.quote(str(property_name or ''))}"


def workflow_evidence_handoff_path(property_name: object, report_dir: Path = DEFAULT_HANDOFF_DIR) -> str:
    return str(report_dir / f"mortgage_workflow_evidence_handoff_{slugify(property_name)}.json")


def workflow_evidence_handoff_markdown_path(property_name: object, report_dir: Path = DEFAULT_HANDOFF_DIR) -> str:
    return str(report_dir / f"mortgage_workflow_evidence_handoff_{slugify(property_name)}.md")


def workflow_evidence_check_path(property_name: object, report_dir: Path = DEFAULT_HANDOFF_DIR) -> str:
    return str(report_dir / f"mortgage_workflow_evidence_check_{slugify(property_name)}.sh")


def workflow_evidence_check_command(property_name: object, report_dir: Path = DEFAULT_HANDOFF_DIR) -> str:
    return f"bash {shlex.quote(workflow_evidence_check_path(property_name, report_dir))}"


def workflow_evidence_analysis_command(property_name: object) -> str:
    return f"python3 scripts/mortgage_workflow_har_intake_analyzer.py --property {shlex.quote(str(property_name or ''))}"


def workflow_evidence_capture_command(
    property_name: object,
    portal_url: object,
    har_path: object,
    required_response_paths: list[str] | None = None,
    expected_document_ids: list[str] | None = None,
) -> str:
    parts = [
        f"MORTGAGE_WORKFLOW_PROPERTY={shlex.quote(str(property_name or ''))}",
        f"MORTGAGE_WORKFLOW_PORTAL_URL={shlex.quote(str(portal_url or ''))}",
        f"MORTGAGE_WORKFLOW_HAR_PATH={shlex.quote(str(har_path or ''))}",
    ]
    paths = [str(item).strip() for item in (required_response_paths or []) if str(item or "").strip()]
    if paths:
        parts.append(f"MORTGAGE_WORKFLOW_REQUIRED_RESPONSE_PATHS={shlex.quote(','.join(paths))}")
    document_ids = [str(item).strip() for item in (expected_document_ids or []) if str(item or "").strip()]
    if document_ids:
        parts.append(f"MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS={shlex.quote(','.join(document_ids))}")
    parts.append("scripts/mortgage_manual_capture_har.sh")
    return " ".join(parts)


def render_shell_token(token: object) -> str:
    text = str(token or "")
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", text):
        key, value = text.split("=", 1)
        return f"{key}={shlex.quote(value)}"
    return shlex.quote(text)


def capture_command_with_expected_document_ids(command: object, expected_document_ids: list[str] | None) -> str | None:
    text = str(command or "").strip()
    if not text:
        return None
    document_ids = [str(item).strip() for item in (expected_document_ids or []) if str(item or "").strip()]
    if "scripts/mortgage_manual_capture_har.sh" not in text:
        return text
    if not document_ids:
        return text
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = []
    document_ids_token = f"MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS={shlex.quote(','.join(document_ids))}"
    if tokens and any(token.startswith("MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS=") for token in tokens):
        rewritten: list[str] = []
        for token in tokens:
            if token.startswith("MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS="):
                rewritten.append(document_ids_token)
            else:
                rewritten.append(render_shell_token(token))
        return " ".join(rewritten)
    if "MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS=" in text:
        return re.sub(
            r"MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS=(?:'[^']*'|\"[^\"]*\"|\\\S|\S)+",
            document_ids_token,
            text,
            count=1,
        )
    return f"MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS={shlex.quote(','.join(document_ids))} {text}"


def command_with_target_month(command: object, target_month: object) -> str | None:
    text = str(command or "").strip()
    month = str(target_month or "").strip()
    if not text:
        return None
    if not month:
        return text
    env_rewritten = text
    env_replaced = False
    for key in TARGET_MONTH_ENV_KEYS:
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(key)}=(?:'[^']*'|\"[^\"]*\"|\\\S|\S)+"
        replacement = f"{key}={shlex.quote(month)}"
        env_rewritten, count = re.subn(pattern, replacement, env_rewritten)
        env_replaced = env_replaced or count > 0
    if env_replaced:
        text = env_rewritten
    if "--target-month" not in text:
        return text
    try:
        tokens = shlex.split(text)
    except ValueError:
        return re.sub(r"--target-month(?:=|\s+)(?:'[^']*'|\"[^\"]*\"|\S+)", f"--target-month {shlex.quote(month)}", text)
    rewritten: list[str] = []
    skip_next = False
    replaced = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token == "--target-month":
            rewritten.extend(["--target-month", shlex.quote(month)])
            skip_next = True
            replaced = True
            continue
        if token.startswith("--target-month="):
            rewritten.append(f"--target-month={shlex.quote(month)}")
            replaced = True
            continue
        rewritten.append(render_shell_token(token))
    return " ".join(rewritten) if replaced else text


def required_response_paths_from_capture_command(command: object) -> list[str]:
    text = str(command or "")
    if not text or "MORTGAGE_WORKFLOW_REQUIRED_RESPONSE_PATHS=" not in text:
        return []
    try:
        tokens = shlex.split(text)
    except ValueError:
        return []
    prefix = "MORTGAGE_WORKFLOW_REQUIRED_RESPONSE_PATHS="
    for token in tokens:
        if token.startswith(prefix):
            return [item.strip() for item in token[len(prefix) :].split(",") if item.strip()]
    return []


def statement_month_from_date(value: object) -> str | None:
    match = re.match(r"^(20\d{2})-(\d{2})", str(value or "").strip())
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}"


def statement_month_status(record: dict[str, Any], month: str) -> dict[str, Any]:
    statement_found = record.get("statement_found") is True
    statement_month = statement_month_from_date(record.get("statement_date"))
    payment_due_month = statement_month_from_date(record.get("payment_due_date"))
    if not statement_found:
        status = "missing"
        basis = None
    elif not statement_month:
        status = "unknown"
        basis = None
    elif statement_month == month:
        status = "current"
        basis = "statement_date"
    elif payment_due_month == month:
        status = "current"
        basis = "payment_due_date"
    elif statement_month < month:
        status = "stale"
        basis = "statement_date"
    else:
        status = "future"
        basis = "statement_date"
    return {
        "current_month": month,
        "statement_month": statement_month,
        "payment_due_date": record.get("payment_due_date"),
        "payment_due_month": payment_due_month,
        "statement_month_status": status,
        "current_month_basis": basis,
        "current_month_statement_available": status == "current",
    }


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "JSON root is not an object"
    return data, None


def fallback_tokenomics_report(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if not path:
        return None, "fallback_report_not_configured"
    if not path.exists():
        return None, "fallback_report_missing"
    data, error = load_json(path)
    if error or data is None:
        return None, error or "fallback_report_unreadable"
    records = data.get("statement_records")
    if not isinstance(records, list) or not records:
        return None, "fallback_report_has_no_statement_records"
    return data, None


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def merge_unique_strings(*values: object) -> list[str]:
    merged: list[str] = []
    for value in values:
        for item in string_list(value):
            if item not in merged:
                merged.append(item)
    return merged


def citadel_har_diagnostics_path(entry: dict[str, Any], report_path: Path | None) -> Path | None:
    marker = " ".join(str(entry.get(key) or "") for key in ("id", "servicer", "script")).casefold()
    if "citadel" not in marker and "loansphere" not in marker:
        return None
    if report_path:
        return report_path.parent / "citadel_har_workflow_diagnostics_report.json"
    return WORKSPACE_ROOT / "reports" / "citadel_har_workflow_diagnostics_report.json"


def citadel_har_next_action(entry: dict[str, Any], report_path: Path | None) -> dict[str, Any]:
    diagnostics_path = citadel_har_diagnostics_path(entry, report_path)
    if not diagnostics_path or not diagnostics_path.exists():
        return {}
    data, error = load_json(diagnostics_path)
    if error or data is None:
        return {}
    next_action = data.get("next_action")
    return next_action if isinstance(next_action, dict) else {}


def citadel_har_diagnostics(entry: dict[str, Any], report_path: Path | None) -> dict[str, Any]:
    diagnostics_path = citadel_har_diagnostics_path(entry, report_path)
    if not diagnostics_path or not diagnostics_path.exists():
        return {}
    data, error = load_json(diagnostics_path)
    if error or data is None:
        return {}
    return data


def is_citadel_entry(entry: dict[str, Any]) -> bool:
    marker = " ".join(str(entry.get(key) or "") for key in ("id", "servicer", "script")).casefold()
    return "citadel" in marker or "loansphere" in marker


def citadel_latest_live_auth_summary(entry: dict[str, Any], report_path: Path | None) -> dict[str, Any]:
    if not is_citadel_entry(entry):
        return {}
    report_dir = report_path.parent if report_path else WORKSPACE_ROOT / "reports"
    candidates = sorted(report_dir.glob("citadel_live_login_attempt_*.json"))
    if not candidates:
        return {}
    latest = max(candidates, key=lambda path: (path.stat().st_mtime, path.name))
    data, error = load_json(latest)
    if error or data is None:
        return {
            "latest_live_auth_report": str(latest),
            "latest_live_auth_status": "unreadable",
            "latest_live_auth_error": error,
        }
    safe_keys = [
        "status",
        "started_at",
        "ended_at",
        "login_mode",
        "tab_source",
        "credentials_available",
        "auth_failure_reason",
        "auth_failure_visible_reason",
        "credential_state_drift_suspected",
        "login_form_last_result",
        "login_form_submitted",
        "manual_auth_required",
        "manual_auth_reason",
        "manual_auth_portal_url",
        "manual_auth_file",
        "oauth_password_grant_failure_count",
        "oauth_password_grant_error_codes",
        "otp_wait_ms",
        "otp_gws_enabled",
        "otp_gws_last_status",
        "otp_gws_otp_written",
    ]
    summary = {
        "latest_live_auth_report": str(latest),
        "latest_live_auth_status": data.get("status"),
    }
    for key in safe_keys:
        if key in data:
            summary[f"latest_live_auth_{key}"] = data.get(key)
    direct_auth = data.get("direct_auth") if isinstance(data.get("direct_auth"), dict) else {}
    direct_first_error = direct_auth.get("first_error") if isinstance(direct_auth.get("first_error"), dict) else {}
    direct_error_codes = string_list(direct_first_error.get("error_message_codes"))
    direct_status = direct_auth.get("status")
    if direct_status:
        summary["latest_live_auth_direct_auth_status"] = direct_status
    if direct_auth.get("password_token_status") is not None:
        summary["latest_live_auth_password_token_status"] = direct_auth.get("password_token_status")
    if direct_auth.get("otp_request_shape_matches_har_success") is not None:
        summary["latest_live_auth_direct_otp_request_shape_matches_har_success"] = direct_auth.get(
            "otp_request_shape_matches_har_success"
        )
    if direct_error_codes and "latest_live_auth_oauth_password_grant_error_codes" not in summary:
        summary["latest_live_auth_oauth_password_grant_error_codes"] = direct_error_codes
    direct_live_auth_blockers = {
        "credential_rejected_before_mfa",
        "mfa_source_unavailable",
        "otp_request_failed",
        "password_token_failed",
    }
    if direct_status in {"credential_rejected_before_mfa", "mfa_source_unavailable"}:
        summary["latest_live_auth_auth_failure_reason"] = summary.get(
            "latest_live_auth_auth_failure_reason"
        ) or direct_status
        summary["latest_live_auth_auth_failure_visible_reason"] = summary.get(
            "latest_live_auth_auth_failure_visible_reason"
        ) or direct_status
        summary["latest_live_auth_manual_auth_required"] = summary.get("latest_live_auth_manual_auth_required")
        if summary["latest_live_auth_manual_auth_required"] is None:
            summary["latest_live_auth_manual_auth_required"] = True
    if data.get("auth_failure_reason"):
        summary["latest_live_auth_blocker"] = data.get("auth_failure_reason")
    elif data.get("manual_auth_reason"):
        summary["latest_live_auth_blocker"] = data.get("manual_auth_reason")
    elif direct_status in direct_live_auth_blockers:
        summary["latest_live_auth_blocker"] = direct_status
    elif data.get("status") not in {None, "ok"}:
        summary["latest_live_auth_blocker"] = data.get("status")
    return summary


def citadel_latest_credential_reconcile_summary(entry: dict[str, Any], report_path: Path | None) -> dict[str, Any]:
    if not is_citadel_entry(entry):
        return {}
    reconcile_path = (report_path.parent if report_path else WORKSPACE_ROOT / "reports") / "citadel_bw_password_reconcile_report.json"
    data, error = load_json(reconcile_path)
    if error or data is None:
        return {
            "latest_credential_reconcile_report": str(reconcile_path),
            "latest_credential_reconcile_status": None,
            "latest_credential_reconcile_error": error,
        }
    summary: dict[str, Any] = {
        "latest_credential_reconcile_report": str(reconcile_path),
        "latest_credential_reconcile_status": data.get("status"),
    }
    for key in [
        "reason",
        "successful_password_request_found",
        "bw_session_status",
        "bw_item_found",
        "bw_item_name",
        "bw_item_uri_host_match",
        "username_matches_har",
        "password_matched_before_update",
        "password_updated",
        "apply_update",
    ]:
        if key in data:
            summary[f"latest_credential_reconcile_{key}"] = data.get(key)
    return summary


def credential_verified_against_successful_har(state: dict[str, Any]) -> bool:
    return (
        state.get("latest_credential_reconcile_status") == "ok"
        and state.get("latest_credential_reconcile_successful_password_request_found") is True
        and state.get("latest_credential_reconcile_username_matches_har") is True
        and state.get("latest_credential_reconcile_password_matched_before_update") is True
    )


def citadel_capture_precondition_summary(state: dict[str, Any]) -> dict[str, Any]:
    prepare_command = "CITADEL_CAPTURE_PREPARE_ONLY=1 scripts/citadel_manual_capture_har.sh"
    capture_required = state.get("latest_report_capture_required")
    if capture_required is False:
        return {
            "capture_precondition_status": "not_required",
            "capture_precondition_blocker": None,
            "capture_precondition_reason": None,
            "capture_precondition_manual_auth_required": False,
            "capture_precondition_next_action": None,
            "capture_precondition_safe_to_capture_now": True,
            "capture_precondition_prepare_command": None,
            "capture_precondition_safe_to_prepare_now": False,
        }
    status = state.get("latest_live_auth_status")
    blocker = state.get("latest_live_auth_blocker")
    visible_reason = state.get("latest_live_auth_auth_failure_visible_reason")
    auth_reason = state.get("latest_live_auth_auth_failure_reason") or state.get("latest_live_auth_manual_auth_reason")
    direct_status = state.get("latest_live_auth_direct_auth_status")
    direct_otp_shape_matches_har = state.get("latest_live_auth_direct_otp_request_shape_matches_har_success") is True
    manual_required = state.get("latest_live_auth_manual_auth_required") is True
    if not status:
        return {
            "capture_precondition_status": "auth_not_verified",
            "capture_precondition_blocker": "latest_live_auth_missing",
            "capture_precondition_reason": "no_latest_live_auth_attempt_report",
            "capture_precondition_manual_auth_required": True,
            "capture_precondition_next_action": "run_live_auth_check_before_capture",
            "capture_precondition_safe_to_capture_now": False,
            "capture_precondition_prepare_command": prepare_command,
            "capture_precondition_safe_to_prepare_now": True,
        }
    if status == "unreadable":
        return {
            "capture_precondition_status": "auth_status_unreadable",
            "capture_precondition_blocker": "latest_live_auth_report_unreadable",
            "capture_precondition_reason": state.get("latest_live_auth_error"),
            "capture_precondition_manual_auth_required": True,
            "capture_precondition_next_action": "repair_latest_live_auth_report_before_capture",
            "capture_precondition_safe_to_capture_now": False,
            "capture_precondition_prepare_command": prepare_command,
            "capture_precondition_safe_to_prepare_now": True,
        }
    if status == "auth_failed" or blocker in {"credential_rejected_before_mfa", "mfa_source_unavailable"}:
        if (
            (blocker == "otp_request_failed" or auth_reason == "otp_request_failed" or direct_status == "otp_request_failed")
            and direct_otp_shape_matches_har
        ):
            return {
                "capture_precondition_status": "blocked_by_retryable_portal_auth",
                "capture_precondition_blocker": "otp_request_matches_successful_har_but_portal_rejected",
                "capture_precondition_reason": "live_otp_request_shape_matched_successful_har_but_portal_returned_unauthorized",
                "capture_precondition_manual_auth_required": manual_required,
                "capture_precondition_next_action": "retry_visible_citadel_auth_or_check_portal_status_before_capture",
                "capture_precondition_safe_to_capture_now": False,
                "capture_precondition_prepare_command": prepare_command,
                "capture_precondition_safe_to_prepare_now": True,
            }
        if (
            (blocker == "credential_rejected_before_mfa" or auth_reason == "credential_rejected_before_mfa")
            and credential_verified_against_successful_har(state)
        ):
            return {
                "capture_precondition_status": "blocked_by_retryable_portal_auth",
                "capture_precondition_blocker": "credential_verified_but_portal_rejected_before_mfa",
                "capture_precondition_reason": "credential_verified_against_successful_har_but_portal_rejected_before_mfa",
                "capture_precondition_manual_auth_required": manual_required,
                "capture_precondition_next_action": "retry_visible_citadel_auth_or_check_portal_status_before_capture",
                "capture_precondition_safe_to_capture_now": False,
                "capture_precondition_prepare_command": prepare_command,
                "capture_precondition_safe_to_prepare_now": True,
            }
        return {
            "capture_precondition_status": "blocked_by_live_auth",
            "capture_precondition_blocker": blocker or status,
            "capture_precondition_reason": visible_reason or auth_reason or blocker or status,
            "capture_precondition_manual_auth_required": manual_required,
            "capture_precondition_next_action": "repair_citadel_portal_login_before_capture",
            "capture_precondition_safe_to_capture_now": False,
            "capture_precondition_prepare_command": prepare_command,
            "capture_precondition_safe_to_prepare_now": True,
        }
    if status == "auth_required" or blocker == "auth_required" or manual_required:
        return {
            "capture_precondition_status": "blocked_by_manual_auth",
            "capture_precondition_blocker": blocker or status,
            "capture_precondition_reason": visible_reason or auth_reason or blocker or status,
            "capture_precondition_manual_auth_required": True,
            "capture_precondition_next_action": "complete_visible_citadel_auth_before_capture",
            "capture_precondition_safe_to_capture_now": False,
            "capture_precondition_prepare_command": prepare_command,
            "capture_precondition_safe_to_prepare_now": True,
        }
    if status == "ok":
        return {
            "capture_precondition_status": "ready",
            "capture_precondition_blocker": None,
            "capture_precondition_reason": None,
            "capture_precondition_manual_auth_required": False,
            "capture_precondition_next_action": "run_visible_capture",
            "capture_precondition_safe_to_capture_now": True,
            "capture_precondition_prepare_command": prepare_command,
            "capture_precondition_safe_to_prepare_now": True,
        }
    return {
        "capture_precondition_status": "live_auth_attempt_recorded",
        "capture_precondition_blocker": None,
        "capture_precondition_reason": status,
        "capture_precondition_manual_auth_required": manual_required,
        "capture_precondition_next_action": "inspect_latest_live_auth_before_capture",
        "capture_precondition_safe_to_capture_now": False,
        "capture_precondition_prepare_command": prepare_command,
        "capture_precondition_safe_to_prepare_now": True,
    }


def configured_downloader_blocker_reasons(item: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for key in (
        "latest_report_next_action_reason",
        "latest_report_har_replay_blocker",
        "latest_report_auth_failure_reason",
        "latest_report_auth_issue",
        "automation_blocker",
        "latest_report_reason",
    ):
        value = str(item.get(key) or "").strip()
        if value and value not in reasons:
            reasons.append(value)
    if reasons:
        return reasons
    for key in (
        "latest_report_reason",
        "latest_report_status",
    ):
        value = str(item.get(key) or "").strip()
        if value and value not in reasons:
            reasons.append(value)
    return reasons


def configured_downloader_primary_blocker_reason(item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    auth_failure = str(item.get("latest_report_auth_failure_reason") or "").strip()
    if auth_failure:
        return auth_failure
    auth_issue = str(item.get("latest_report_auth_issue") or "").strip()
    if auth_issue:
        return auth_issue
    for reason in configured_downloader_blocker_reasons(item):
        if reason:
            return reason
    return None


def configured_downloader_auth_blocker_reason(item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    explicit_auth_failure = str(item.get("latest_report_auth_failure_reason") or "").strip()
    if explicit_auth_failure:
        return explicit_auth_failure if explicit_auth_failure in AUTH_BLOCKER_REASONS else None
    explicit_auth_issue = str(item.get("latest_report_auth_issue") or "").strip()
    if explicit_auth_issue:
        return explicit_auth_issue if explicit_auth_issue in AUTH_BLOCKER_REASONS else None
    for key in (
        "latest_live_auth_blocker",
        "capture_precondition_blocker",
        "capture_precondition_reason",
    ):
        value = str(item.get(key) or "").strip()
        if value in AUTH_BLOCKER_REASONS:
            return value
    return None


def first_auth_blocker(items: list[dict[str, Any]]) -> str | None:
    for item in items:
        reason = configured_downloader_auth_blocker_reason(item)
        if reason:
            return reason
    return None


def apply_auth_blocker_to_workflow_fields(fields: dict[str, Any], reason: str | None) -> dict[str, Any]:
    if not reason:
        return fields
    blocked = dict(fields)
    blocked["capture_auth_blocker"] = True
    blocked["capture_auth_blocker_reason"] = reason
    blocked["capture_command_ready_to_run_now"] = False
    blocked["capture_command_blocked_reason"] = reason
    blocked["register_workflow_evidence_ready_to_run_now"] = False
    blocked["advance_workflow_evidence_ready_to_run_now"] = False
    blocked["command_precondition_status"] = "blocked_by_auth"
    blocked["command_precondition_reason"] = reason
    if not blocked.get("command_precondition_next_action"):
        if reason == "visible_loandepot_tab_not_authenticated":
            blocked["command_precondition_next_action"] = (
                "Authenticate loanDepot in the visible browser tab, then rerun the live-CDP downloader profile."
            )
        elif reason == "login_still_required_after_submit":
            blocked["command_precondition_next_action"] = (
                "Verify the MortgageQuestions credentials in the visible portal or update the Ops Bitwarden item "
                "before rerunning; an OTP cannot be submitted until the portal reaches MFA."
            )
        elif reason == "login_rejected":
            blocked["command_precondition_next_action"] = (
                "Recover or update the MortgageQuestions credentials in the visible portal before rerunning; "
                "an OTP cannot be submitted until the portal reaches MFA."
            )
        else:
            blocked["command_precondition_next_action"] = (
                "Recover or unlock the MortgageQuestions account in the visible portal before capturing the "
                "target-month statement PDF."
            )
    blocked["safe_to_run_automatically"] = False
    return blocked


def auth_blocker_suggested_next_action(
    reason: str | None,
    workflow_fields: dict[str, Any] | None = None,
    item: dict[str, Any] | None = None,
) -> str | None:
    if not reason:
        return None
    workflow_fields = workflow_fields or {}
    item = item or {}
    generic_capture_actions = {
        "capture_target_month_statement",
        "capture_target_month_statement_pdf",
        "recapture_target_month_statement_pdf",
    }
    if reason == "credentials_unavailable":
        repair_action = str(item.get("latest_report_credential_lookup_repair_action") or "").strip()
        if repair_action:
            return repair_action
        credential_lookup_failure = str(item.get("latest_report_credential_lookup_failure_reason") or "").strip()
        credential_item_name = str(item.get("latest_report_credential_lookup_item_name") or "").strip()
        credential_folder_name = str(item.get("latest_report_credential_lookup_expected_folder_name") or "").strip()
        misfiled_count = item.get("latest_report_credential_lookup_misfiled_candidate_count")
        absent = item.get("latest_report_credential_lookup_absent") is True
        if (
            credential_lookup_failure == "item_not_found"
            and credential_item_name == "consumerportal.loandepot.com"
            and credential_folder_name == "Ops"
        ):
            if isinstance(misfiled_count, int) and misfiled_count > 0:
                return "move_existing_loandepot_credential_to_Ops_or_set_LOANDEPOT_BW_EXPECTED_FOLDER_then_rerun_live_cdp"
            if absent:
                return "create_loandepot_credential_in_Ops_bitwarden_item_consumerportal.loandepot.com_then_rerun_live_cdp"
            return "add_or_move_loandepot_credentials_to_Ops_bitwarden_item_consumerportal.loandepot.com_then_rerun_live_cdp"
    for key in (
        "suggested_next_action",
        "source_downloader_operator_next_action",
        "source_downloader_suggested_next_action",
    ):
        value = str(workflow_fields.get(key) or "").strip()
        if value and " " not in value and value not in generic_capture_actions:
            return str(value)
    latest_suggested = str(item.get("latest_report_suggested_next_action") or "").strip()
    if latest_suggested and " " not in latest_suggested and latest_suggested not in generic_capture_actions:
        return latest_suggested
    if reason == "credentials_unavailable":
        return "add_loandepot_credentials_to_ops_bitwarden_or_set_env_then_rerun_live_cdp"
    if reason == "visible_loandepot_tab_not_authenticated":
        return "authenticate_visible_loandepot_tab_then_run_live_cdp"
    if reason in {"login_rejected", "login_still_required_after_submit"}:
        return "repair_portal_auth_before_capture"
    if reason in AUTH_BLOCKER_REASONS:
        return "repair_portal_auth_before_capture"
    return None


def configured_downloader_suggested_next_action(
    item: dict[str, Any] | None,
    workflow_fields: dict[str, Any] | None = None,
) -> str | None:
    item = item or {}
    workflow_fields = workflow_fields or {}
    auth_reason = None
    workflow_auth_reason = str(workflow_fields.get("capture_auth_blocker_reason") or "").strip()
    if workflow_fields.get("capture_auth_blocker") is True and workflow_auth_reason in AUTH_BLOCKER_REASONS:
        auth_reason = workflow_auth_reason
    if not auth_reason:
        auth_reason = configured_downloader_auth_blocker_reason(item)
    if auth_reason:
        action = auth_blocker_suggested_next_action(auth_reason, workflow_fields, item)
        if action:
            return action
    for key in ("workflow_evidence_handoff_suggested_next_action",):
        value = workflow_fields.get(key)
        if value not in (None, "", [], {}):
            return str(value)
    value = item.get("latest_report_suggested_next_action")
    if value not in (None, "", [], {}):
        return str(value)
    return None


def config_entries(config_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state: dict[str, Any] = {
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "config_readable": False,
        "config_error": None,
    }
    if not config_path.exists():
        state["config_error"] = "missing"
        return [], state
    data, error = load_json(config_path)
    if error:
        state["config_error"] = error
        return [], state
    state["config_readable"] = True
    entries = data.get("downloaders") if data else []
    if not isinstance(entries, list):
        state["config_error"] = "downloaders is not a list"
        return [], state
    return [entry for entry in entries if isinstance(entry, dict)], state


def intake_entries(intake_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    state: dict[str, Any] = {
        "intake_path": str(intake_path),
        "intake_exists": intake_path.exists(),
        "intake_readable": False,
        "intake_error": None,
    }
    if not intake_path.exists():
        state["intake_error"] = "missing"
        return {}, state
    data, error = load_json(intake_path)
    if error:
        state["intake_error"] = error
        return {}, state
    state["intake_readable"] = True
    properties = data.get("properties") if data else []
    if not isinstance(properties, list):
        state["intake_error"] = "properties is not a list"
        return {}, state
    indexed: dict[str, dict[str, Any]] = {}
    for item in properties:
        if not isinstance(item, dict):
            continue
        prop = str(item.get("property") or "").strip()
        if prop:
            indexed[normalize_property(prop)] = item
    return indexed, state


def script_state(entry: dict[str, Any]) -> dict[str, Any]:
    script_value = str(entry.get("script") or "")
    script_path = Path(script_value)
    if script_value and not script_path.is_absolute():
        script_path = WORKSPACE_ROOT / script_path
    return {
        "script": script_value or None,
        "script_path": str(script_path) if script_value else None,
        "script_exists": bool(script_value and script_path.exists()),
    }


def report_path_for(entry: dict[str, Any]) -> Path | None:
    report_value = str(entry.get("report") or "")
    if not report_value:
        return None
    report_path = Path(report_value)
    if not report_path.is_absolute():
        report_path = WORKSPACE_ROOT / report_path
    return report_path


def workspace_path_for(value: object) -> Path | None:
    path_value = str(value or "").strip()
    if not path_value:
        return None
    path = Path(path_value)
    return path if path.is_absolute() else WORKSPACE_ROOT / path


def profile_report_paths_for(entry: dict[str, Any]) -> list[Path]:
    profiles = entry.get("profiles") if isinstance(entry.get("profiles"), dict) else {}
    paths: list[Path] = []
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        path = workspace_path_for(profile.get("report"))
        if path and path not in paths:
            paths.append(path)
    return paths


def configured_har_path_for(entry: dict[str, Any]) -> Path | None:
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    for key in ("MORTGAGE_GENERATED_HAR_PATH", "CITADEL_HAR_PATH", "MORTGAGE_WORKFLOW_HAR_PATH"):
        value = str(env.get(key) or "").strip()
        if not value:
            continue
        path = Path(value)
        return path if path.is_absolute() else WORKSPACE_ROOT / path
    return None


def normalized_path_for_compare(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = WORKSPACE_ROOT / path
    return os.path.normcase(os.path.normpath(str(path.expanduser().resolve(strict=False))))


def har_diagnostics_match_configured_har(
    diagnostics: dict[str, Any],
    configured_har_path: Path | None,
) -> bool:
    diagnostic_har_path = normalized_path_for_compare(diagnostics.get("har_path"))
    configured = normalized_path_for_compare(configured_har_path) if configured_har_path else None
    if not diagnostic_har_path or not configured:
        return True
    return diagnostic_har_path == configured


GENERIC_CAPTURE_ACTIONS = {
    "capture_target_month_statement",
    "capture_target_month_statement_pdf",
    "recapture_target_month_statement_pdf",
}


def report_data_has_auth_context(data: dict[str, Any]) -> bool:
    credential_diagnostics = data.get("credential_diagnostics") if isinstance(data.get("credential_diagnostics"), dict) else {}
    for key in (
        "manual_auth_reason",
        "auth_failure_reason",
        "auth_failure_visible_reason",
        "auth_mfa_reached",
        "auth_issue",
        "auth_issue_text",
        "auth_state",
        "auth_stage",
        "login_mode",
        "auto_login_attempted",
        "auto_login_status",
        "auto_login_step",
        "auto_login_blocked_reason",
        "credentials_available",
        "credential_source",
        "credential_lookup_status",
        "credential_lookup_failure_reason",
        "credential_lookup_item_name",
        "credential_lookup_expected_folder_name",
        "credential_lookup_uri_host",
        "credential_login_failure_suspected",
        "credential_login_failure_suspected_reason",
    ):
        if data.get(key) not in (None, "", [], {}) or credential_diagnostics.get(key) not in (None, "", [], {}):
            return True
    return False


def overlay_auth_context_from_report(
    state: dict[str, Any],
    data: dict[str, Any],
    source_path: Path,
) -> None:
    if not report_data_has_auth_context(data):
        return
    supplemental_target_month = str(data.get("target_month") or "").strip()
    state_target_month = str(state.get("latest_report_target_month") or "").strip()
    if supplemental_target_month and state_target_month and supplemental_target_month != state_target_month:
        return
    for state_key, report_key in (
        ("latest_report_target_month_document_identifiers", "target_month_document_identifiers"),
        ("latest_report_target_month_candidate_count", "target_month_candidate_count"),
        ("latest_report_target_month_downloadable_count", "target_month_downloadable_count"),
        ("latest_report_required_capture_quality", "required_capture_quality"),
        ("latest_report_target_month_recapture_required", "target_month_recapture_required"),
        ("latest_report_target_month_recapture_reason", "target_month_recapture_reason"),
    ):
        value = string_list(data.get(report_key)) if report_key == "target_month_document_identifiers" else data.get(report_key)
        if value not in (None, "", [], {}):
            state[state_key] = value
    credential_diagnostics = data.get("credential_diagnostics") if isinstance(data.get("credential_diagnostics"), dict) else {}
    for state_key, report_key in (
        ("latest_report_auth_state", "auth_state"),
        ("latest_report_auth_stage", "auth_stage"),
        ("latest_report_manual_auth_reason", "manual_auth_reason"),
        ("latest_report_auth_failure_reason", "auth_failure_reason"),
        ("latest_report_auth_failure_visible_reason", "auth_failure_visible_reason"),
        ("latest_report_auth_mfa_reached", "auth_mfa_reached"),
        ("latest_report_auth_issue", "auth_issue"),
        ("latest_report_auth_issue_text", "auth_issue_text"),
        ("latest_report_login_mode", "login_mode"),
        ("latest_report_auto_login_attempted", "auto_login_attempted"),
        ("latest_report_auto_login_status", "auto_login_status"),
        ("latest_report_auto_login_step", "auto_login_step"),
        ("latest_report_auto_login_blocked_reason", "auto_login_blocked_reason"),
        ("latest_report_credentials_available", "credentials_available"),
        ("latest_report_credential_source", "credential_source"),
        ("latest_report_credential_lookup_status", "credential_lookup_status"),
        ("latest_report_credential_lookup_failure_reason", "credential_lookup_failure_reason"),
        ("latest_report_credential_lookup_item_name", "credential_lookup_item_name"),
        ("latest_report_credential_lookup_expected_folder_name", "credential_lookup_expected_folder_name"),
        ("latest_report_credential_lookup_uri_host", "credential_lookup_uri_host"),
        ("latest_report_credential_login_failure_suspected", "credential_login_failure_suspected"),
        ("latest_report_credential_login_failure_suspected_reason", "credential_login_failure_suspected_reason"),
    ):
        value = data.get(report_key)
        if value in (None, "", [], {}):
            value = credential_diagnostics.get(report_key)
        if state.get(state_key) in (None, "", [], {}) and value not in (None, "", [], {}):
            state[state_key] = value
    auth_blocker = str(state.get("latest_report_auth_failure_reason") or state.get("latest_report_auth_issue") or "").strip()
    if auth_blocker in AUTH_BLOCKER_REASONS:
        state["latest_report_supplemental_auth_report"] = str(source_path)
        if state.get("latest_report_safe_to_run_automatically") is None:
            state["latest_report_safe_to_run_automatically"] = False
        for state_key, report_key in (
            ("latest_report_operator_next_action", "operator_next_action"),
            ("latest_report_suggested_next_action", "suggested_next_action"),
        ):
            value = data.get(report_key)
            current = str(state.get(state_key) or "").strip()
            if value not in (None, "", [], {}) and (not current or current in GENERIC_CAPTURE_ACTIONS):
                state[state_key] = value


def downloader_report_state(entry: dict[str, Any]) -> dict[str, Any]:
    report_path = report_path_for(entry)
    configured_har_path = configured_har_path_for(entry)
    state: dict[str, Any] = {
        "report": str(entry.get("report") or "") or None,
        "report_path": str(report_path) if report_path else None,
        "report_exists": bool(report_path and report_path.exists()),
        "latest_report_status": None,
        "latest_report_reason": None,
        "latest_report_auth_state": None,
        "latest_report_auth_stage": None,
        "latest_report_manual_auth_reason": None,
        "latest_report_auth_issue": None,
        "latest_report_auth_issue_text": None,
        "latest_report_login_mode": None,
        "latest_report_auto_login_attempted": None,
        "latest_report_auto_login_status": None,
        "latest_report_auto_login_step": None,
        "latest_report_auto_login_blocked_reason": None,
        "latest_report_operator_next_action": None,
        "latest_report_credentials_available": None,
        "latest_report_credential_source": None,
        "latest_report_credential_lookup_status": None,
        "latest_report_credential_lookup_failure_reason": None,
        "latest_report_credential_lookup_item_name": None,
        "latest_report_credential_lookup_expected_folder_name": None,
        "latest_report_credential_lookup_uri_host": None,
        "latest_report_safe_to_run_automatically": None,
        "latest_report_target_month_recapture_required": None,
        "latest_report_required_capture_quality": None,
        "latest_report_target_month_recapture_reason": None,
        "latest_report_suggested_next_action": None,
        "latest_report_error": None,
        "latest_report_har_path": str(configured_har_path) if configured_har_path else None,
        "latest_report_har_path_exists": configured_har_path.exists() if configured_har_path else None,
        "latest_report_candidate_count": None,
        "latest_report_target_month_candidate_count": None,
        "latest_report_target_month_downloadable_count": None,
        "latest_report_idempotency_digest": None,
        "latest_report_next_action_status": None,
        "latest_report_next_action_reason": None,
        "latest_report_next_action_command": None,
        "latest_report_next_action_capture_command": None,
        "latest_report_capture_required": None,
        "latest_report_required_response_paths": None,
        "latest_report_target_month": None,
        "latest_report_target_month_statement_available": None,
        "latest_report_target_month_existing_count": None,
        "latest_report_target_month_downloaded_count": None,
        "latest_report_target_month_skipped_count": None,
        "latest_report_target_month_document_identifiers": None,
        "latest_report_existing_target_month_files": None,
        "latest_report_downloaded_target_month_files": None,
        "latest_report_skipped_target_month_files": None,
        "latest_report_idempotent_skip": None,
        "latest_report_idempotent_skip_reason": None,
        "latest_report_har_capture_quality_status": None,
        "latest_report_har_replay_blocker": None,
        "latest_report_har_target_month": None,
        "latest_report_har_target_month_replayable_document_available": None,
        "latest_report_har_target_month_replayable_document_payload_count": None,
        "latest_report_har_replayable_statement_months": None,
        "latest_report_har_statement_document_months": None,
        "latest_report_supplemental_auth_report": None,
        "latest_live_auth_report": None,
        "latest_live_auth_status": None,
        "latest_live_auth_blocker": None,
        "latest_live_auth_started_at": None,
        "latest_live_auth_ended_at": None,
        "latest_live_auth_login_mode": None,
        "latest_live_auth_tab_source": None,
        "latest_live_auth_credentials_available": None,
        "latest_live_auth_auth_failure_reason": None,
        "latest_live_auth_auth_failure_visible_reason": None,
        "latest_live_auth_credential_state_drift_suspected": None,
        "latest_live_auth_login_form_last_result": None,
        "latest_live_auth_login_form_submitted": None,
        "latest_live_auth_manual_auth_required": None,
        "latest_live_auth_manual_auth_reason": None,
        "latest_live_auth_manual_auth_portal_url": None,
        "latest_live_auth_manual_auth_file": None,
        "latest_live_auth_oauth_password_grant_failure_count": None,
        "latest_live_auth_oauth_password_grant_error_codes": None,
        "latest_live_auth_otp_wait_ms": None,
        "latest_live_auth_otp_gws_enabled": None,
        "latest_live_auth_otp_gws_last_status": None,
        "latest_live_auth_otp_gws_otp_written": None,
        "latest_live_auth_direct_auth_status": None,
        "latest_live_auth_direct_otp_request_shape_matches_har_success": None,
        "latest_credential_reconcile_report": None,
        "latest_credential_reconcile_status": None,
        "latest_credential_reconcile_error": None,
        "latest_credential_reconcile_reason": None,
        "latest_credential_reconcile_successful_password_request_found": None,
        "latest_credential_reconcile_bw_session_status": None,
        "latest_credential_reconcile_bw_item_found": None,
        "latest_credential_reconcile_bw_item_name": None,
        "latest_credential_reconcile_bw_item_uri_host_match": None,
        "latest_credential_reconcile_username_matches_har": None,
        "latest_credential_reconcile_password_matched_before_update": None,
        "latest_credential_reconcile_password_updated": None,
        "latest_credential_reconcile_apply_update": None,
        "capture_precondition_status": None,
        "capture_precondition_blocker": None,
        "capture_precondition_reason": None,
        "capture_precondition_manual_auth_required": None,
        "capture_precondition_next_action": None,
        "capture_precondition_safe_to_capture_now": None,
        "capture_precondition_prepare_command": None,
        "capture_precondition_safe_to_prepare_now": None,
        "automation_ready": None,
        "automation_blocker": None,
    }
    if not report_path:
        state["automation_ready"] = None
        state["automation_blocker"] = "report_not_configured"
        return state
    if not report_path.exists():
        state["automation_ready"] = None
        state["automation_blocker"] = "latest_report_missing"
        return state
    data, error = load_json(report_path)
    if error or data is None:
        state["latest_report_error"] = error
        state["automation_ready"] = False
        state["automation_blocker"] = "latest_report_unreadable"
        return state
    status = data.get("status")
    state["latest_report_status"] = status
    state["latest_report_reason"] = data.get("reason") or data.get("error")
    state["latest_report_auth_state"] = data.get("auth_state")
    state["latest_report_auth_stage"] = data.get("auth_stage")
    state["latest_report_manual_auth_reason"] = data.get("manual_auth_reason")
    state["latest_report_auth_failure_reason"] = data.get("auth_failure_reason")
    state["latest_report_auth_failure_visible_reason"] = data.get("auth_failure_visible_reason")
    state["latest_report_auth_mfa_reached"] = data.get("auth_mfa_reached")
    state["latest_report_auth_issue"] = data.get("auth_issue")
    state["latest_report_auth_issue_text"] = data.get("auth_issue_text")
    state["latest_report_login_mode"] = data.get("login_mode")
    state["latest_report_auto_login_attempted"] = data.get("auto_login_attempted")
    state["latest_report_auto_login_status"] = data.get("auto_login_status")
    state["latest_report_auto_login_step"] = data.get("auto_login_step")
    state["latest_report_auto_login_blocked_reason"] = data.get("auto_login_blocked_reason")
    state["latest_report_operator_next_action"] = data.get("operator_next_action")
    state["latest_report_credentials_available"] = data.get("credentials_available")
    state["latest_report_credential_source"] = data.get("credential_source")
    state["latest_report_credential_login_failure_suspected"] = data.get("credential_login_failure_suspected")
    state["latest_report_credential_login_failure_suspected_reason"] = data.get(
        "credential_login_failure_suspected_reason"
    )
    credential_diagnostics = data.get("credential_diagnostics") if isinstance(data.get("credential_diagnostics"), dict) else {}
    state["latest_report_credential_lookup_status"] = data.get("credential_lookup_status") or credential_diagnostics.get("credential_lookup_status")
    state["latest_report_credential_lookup_failure_reason"] = data.get("credential_lookup_failure_reason") or credential_diagnostics.get("credential_lookup_failure_reason")
    state["latest_report_credential_lookup_item_name"] = data.get("credential_lookup_item_name") or credential_diagnostics.get("credential_lookup_item_name")
    state["latest_report_credential_lookup_expected_folder_name"] = data.get("credential_lookup_expected_folder_name") or credential_diagnostics.get("credential_lookup_expected_folder_name")
    state["latest_report_credential_lookup_uri_host"] = data.get("credential_lookup_uri_host") or credential_diagnostics.get("credential_lookup_uri_host")
    state["latest_report_credential_lookup_candidate_count"] = data.get("credential_lookup_candidate_count") if data.get("credential_lookup_candidate_count") is not None else credential_diagnostics.get("credential_lookup_candidate_count")
    state["latest_report_credential_lookup_candidate_items"] = data.get("credential_lookup_candidate_items") or credential_diagnostics.get("credential_lookup_candidate_items")
    state["latest_report_credential_lookup_unguarded_candidate_count"] = data.get("credential_lookup_unguarded_candidate_count") if data.get("credential_lookup_unguarded_candidate_count") is not None else credential_diagnostics.get("credential_lookup_unguarded_candidate_count")
    state["latest_report_credential_lookup_unguarded_candidate_items"] = data.get("credential_lookup_unguarded_candidate_items") or credential_diagnostics.get("credential_lookup_unguarded_candidate_items")
    state["latest_report_credential_lookup_misfiled_candidate_count"] = data.get("credential_lookup_misfiled_candidate_count") if data.get("credential_lookup_misfiled_candidate_count") is not None else credential_diagnostics.get("credential_lookup_misfiled_candidate_count")
    state["latest_report_credential_lookup_absent"] = data.get("credential_lookup_absent") if data.get("credential_lookup_absent") is not None else credential_diagnostics.get("credential_lookup_absent")
    state["latest_report_credential_lookup_scope"] = data.get("credential_lookup_scope") or credential_diagnostics.get("credential_lookup_scope")
    state["latest_report_credential_lookup_repair_action"] = data.get("credential_lookup_repair_action") or credential_diagnostics.get("credential_lookup_repair_action")
    state["latest_report_safe_to_run_automatically"] = data.get("safe_to_run_automatically")
    state["latest_report_target_month_recapture_required"] = data.get("target_month_recapture_required")
    state["latest_report_required_capture_quality"] = data.get("required_capture_quality")
    state["latest_report_target_month_recapture_reason"] = data.get("target_month_recapture_reason")
    state["latest_report_suggested_next_action"] = data.get("suggested_next_action")
    state["latest_report_har_path"] = data.get("har_path") or state.get("latest_report_har_path")
    if data.get("har_path_exists") is not None:
        state["latest_report_har_path_exists"] = data.get("har_path_exists")
    state["latest_report_candidate_count"] = data.get("candidate_count")
    state["latest_report_target_month_candidate_count"] = data.get("target_month_candidate_count")
    state["latest_report_target_month_downloadable_count"] = data.get("target_month_downloadable_count")
    state["latest_report_idempotency_digest"] = data.get("idempotency_digest")
    state["latest_report_target_month"] = data.get("target_month")
    state["latest_report_target_month_statement_available"] = data.get("target_month_statement_available")
    state["latest_report_target_month_existing_count"] = data.get("target_month_existing_count")
    state["latest_report_target_month_downloaded_count"] = data.get("target_month_downloaded_count")
    state["latest_report_target_month_skipped_count"] = data.get("target_month_skipped_count")
    state["latest_report_target_month_document_identifiers"] = string_list(
        data.get("target_month_document_identifiers")
    )
    if not state["latest_report_target_month_document_identifiers"]:
        prop_key = normalize_property(entry.get("property"))
        report_target_month = str(state.get("latest_report_target_month") or "").strip()
        for gap in data.get("target_month_statement_gaps") or []:
            if not isinstance(gap, dict):
                continue
            if normalize_property(gap.get("property")) != prop_key:
                continue
            gap_month = str(gap.get("target_month") or gap.get("expected_target_month") or "").strip()
            if report_target_month and gap_month and gap_month != report_target_month:
                continue
            gap_ids = string_list(gap.get("target_month_document_identifiers"))
            if not gap_ids:
                continue
            state["latest_report_target_month_document_identifiers"] = gap_ids
            for state_key, gap_key in (
                ("latest_report_target_month_candidate_count", "target_month_candidate_count"),
                ("latest_report_target_month_downloadable_count", "target_month_downloadable_count"),
                ("latest_report_required_capture_quality", "required_capture_quality"),
                ("latest_report_target_month_recapture_required", "target_month_recapture_required"),
                ("latest_report_target_month_recapture_reason", "target_month_recapture_reason"),
                ("latest_report_suggested_next_action", "suggested_next_action"),
                ("latest_report_operator_next_action", "operator_next_action"),
                ("latest_report_auth_state", "auth_state"),
                ("latest_report_auth_stage", "auth_stage"),
                ("latest_report_manual_auth_reason", "manual_auth_reason"),
                ("latest_report_auth_issue", "auth_issue"),
                ("latest_report_auth_issue_text", "auth_issue_text"),
                ("latest_report_login_mode", "login_mode"),
                ("latest_report_auto_login_attempted", "auto_login_attempted"),
                ("latest_report_auto_login_status", "auto_login_status"),
                ("latest_report_auto_login_step", "auto_login_step"),
                ("latest_report_auto_login_blocked_reason", "auto_login_blocked_reason"),
                ("latest_report_credentials_available", "credentials_available"),
                ("latest_report_credential_source", "credential_source"),
                ("latest_report_credential_lookup_status", "credential_lookup_status"),
                ("latest_report_credential_lookup_failure_reason", "credential_lookup_failure_reason"),
                ("latest_report_credential_lookup_item_name", "credential_lookup_item_name"),
                ("latest_report_credential_lookup_expected_folder_name", "credential_lookup_expected_folder_name"),
                ("latest_report_credential_lookup_uri_host", "credential_lookup_uri_host"),
            ):
                if state.get(state_key) in (None, [], "") and gap.get(gap_key) not in (None, [], ""):
                    state[state_key] = gap.get(gap_key)
            break
    state["latest_report_existing_target_month_files"] = data.get("existing_target_month_files")
    state["latest_report_downloaded_target_month_files"] = data.get("downloaded_target_month_files")
    state["latest_report_skipped_target_month_files"] = data.get("skipped_target_month_files")
    state["latest_report_idempotent_skip"] = data.get("idempotent_skip")
    state["latest_report_idempotent_skip_reason"] = data.get("idempotent_skip_reason")
    next_action = data.get("next_action") if isinstance(data.get("next_action"), dict) else {}
    har_diagnostics = data.get("har_workflow_diagnostics")
    if isinstance(har_diagnostics, dict) and isinstance(har_diagnostics.get("next_action"), dict):
        next_action = har_diagnostics["next_action"]
    external_har_diagnostics = citadel_har_diagnostics(entry, report_path)
    if isinstance(har_diagnostics, dict):
        state["latest_report_har_capture_quality_status"] = har_diagnostics.get("capture_quality_status")
        state["latest_report_har_replay_blocker"] = har_diagnostics.get("replay_blocker") or har_diagnostics.get("reason")
        state["latest_report_har_target_month"] = har_diagnostics.get("target_month")
        state["latest_report_har_target_month_replayable_document_available"] = har_diagnostics.get(
            "target_month_replayable_document_available"
        )
        state["latest_report_har_target_month_replayable_document_payload_count"] = har_diagnostics.get(
            "target_month_replayable_document_payload_count"
        )
        state["latest_report_har_replayable_statement_months"] = string_list(
            har_diagnostics.get("replayable_statement_months")
        )
        state["latest_report_har_statement_document_months"] = string_list(
            har_diagnostics.get("statement_document_months")
        )
        if state.get("latest_report_target_month") is None:
            state["latest_report_target_month"] = har_diagnostics.get("target_month")
    if external_har_diagnostics:
        external_replay_blocker = external_har_diagnostics.get("replay_blocker") or external_har_diagnostics.get("reason")
        external_matches_configured_har = har_diagnostics_match_configured_har(
            external_har_diagnostics,
            configured_har_path,
        )
        external_overrides_stale_embedded_har_status = (
            external_matches_configured_har
            and external_har_diagnostics.get("status") == "ok"
            and external_har_diagnostics.get("capture_quality_status") == "replayable"
            and not external_replay_blocker
        )
        state["latest_report_har_capture_quality_status"] = (
            external_har_diagnostics.get("capture_quality_status")
            or state.get("latest_report_har_capture_quality_status")
        )
        if external_overrides_stale_embedded_har_status:
            state["latest_report_har_replay_blocker"] = None
        else:
            state["latest_report_har_replay_blocker"] = (
                external_replay_blocker
                or state.get("latest_report_har_replay_blocker")
            )
        state["latest_report_har_target_month"] = (
            external_har_diagnostics.get("target_month")
            or state.get("latest_report_har_target_month")
        )
        if external_overrides_stale_embedded_har_status or (
            state.get("latest_report_har_target_month_replayable_document_available") is None
        ):
            state["latest_report_har_target_month_replayable_document_available"] = external_har_diagnostics.get(
                "target_month_replayable_document_available"
            )
        if external_overrides_stale_embedded_har_status or (
            state.get("latest_report_har_target_month_replayable_document_payload_count") is None
        ):
            state["latest_report_har_target_month_replayable_document_payload_count"] = external_har_diagnostics.get(
                "target_month_replayable_document_payload_count"
            )
        state["latest_report_har_replayable_statement_months"] = merge_unique_strings(
            state.get("latest_report_har_replayable_statement_months"),
            external_har_diagnostics.get("replayable_statement_months"),
        )
        state["latest_report_har_statement_document_months"] = merge_unique_strings(
            state.get("latest_report_har_statement_document_months"),
            external_har_diagnostics.get("statement_document_months"),
        )
        if state.get("latest_report_target_month") is None:
            state["latest_report_target_month"] = external_har_diagnostics.get("target_month")
    primary_report_compare = normalized_path_for_compare(report_path) if report_path else None
    for profile_report_path in profile_report_paths_for(entry):
        if normalized_path_for_compare(profile_report_path) == primary_report_compare:
            continue
        if not profile_report_path.exists():
            continue
        profile_data, profile_error = load_json(profile_report_path)
        if profile_error or profile_data is None:
            continue
        overlay_auth_context_from_report(state, profile_data, profile_report_path)
    state["latest_report_next_action_status"] = next_action.get("status") if isinstance(next_action, dict) else None
    state["latest_report_next_action_reason"] = next_action.get("reason") if isinstance(next_action, dict) else None
    state["latest_report_next_action_command"] = next_action.get("next_command") if isinstance(next_action, dict) else None
    state["latest_report_next_action_capture_command"] = (
        next_action.get("capture_command") if isinstance(next_action, dict) else None
    )
    state["latest_report_capture_required"] = next_action.get("capture_required") if isinstance(next_action, dict) else None
    state["latest_report_required_response_paths"] = (
        string_list(next_action.get("required_response_paths")) if isinstance(next_action, dict) else []
    )
    for key in [
        "capture_plan_report",
        "capture_plan_markdown",
        "capture_har_path",
        "capture_report",
        "post_capture_diagnostics_command",
        "validate_existing_capture_command",
        "advance_capture_workflow_evidence_dry_run_command",
        "advance_capture_workflow_evidence_apply_command",
        "workflow_evidence_advance_report",
        "advance_workflow_evidence_command",
        "advance_workflow_evidence_apply_command",
    ]:
        state[f"latest_report_{key}"] = next_action.get(key) if isinstance(next_action, dict) else None
    if isinstance(next_action, dict):
        for key in CAPTURE_PRECONDITION_KEYS:
            state[key] = next_action.get(key)
    har_next_action = citadel_har_next_action(entry, report_path)
    if har_next_action:
        state["latest_report_next_action_status"] = (
            state["latest_report_next_action_status"] or har_next_action.get("status")
        )
        state["latest_report_next_action_reason"] = (
            state["latest_report_next_action_reason"] or har_next_action.get("reason")
        )
        state["latest_report_next_action_command"] = (
            state["latest_report_next_action_command"] or har_next_action.get("next_command")
        )
        state["latest_report_next_action_capture_command"] = (
            state["latest_report_next_action_capture_command"] or har_next_action.get("capture_command")
        )
        if state["latest_report_capture_required"] is None:
            state["latest_report_capture_required"] = har_next_action.get("capture_required")
        state["latest_report_required_response_paths"] = merge_unique_strings(
            state.get("latest_report_required_response_paths"),
            har_next_action.get("required_response_paths"),
        )
        for key in [
            "capture_plan_report",
            "capture_plan_markdown",
            "capture_har_path",
            "capture_report",
            "post_capture_diagnostics_command",
            "validate_existing_capture_command",
            "advance_capture_workflow_evidence_dry_run_command",
            "advance_capture_workflow_evidence_apply_command",
            "workflow_evidence_advance_report",
            "advance_workflow_evidence_command",
            "advance_workflow_evidence_apply_command",
        ]:
            state_key = f"latest_report_{key}"
            state[state_key] = state.get(state_key) or har_next_action.get(key)
        for key in CAPTURE_PRECONDITION_KEYS:
            if key in har_next_action:
                state[key] = har_next_action.get(key)
    state.update(citadel_latest_live_auth_summary(entry, report_path))
    state.update(citadel_latest_credential_reconcile_summary(entry, report_path))
    if is_citadel_entry(entry) and not state.get("capture_precondition_status"):
        state.update(citadel_capture_precondition_summary(state))
    har_replay_blocker = state.get("latest_report_har_replay_blocker")
    if status == "ok" and not har_replay_blocker:
        state["automation_ready"] = True
        state["automation_blocker"] = None
    else:
        state["automation_ready"] = False
        state["automation_blocker"] = (
            har_replay_blocker
            or state["latest_report_reason"]
            or status
            or "latest_report_not_ok"
        )
    return state


def downloader_mode(entry: dict[str, Any]) -> dict[str, Any]:
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    marker = " ".join(
        str(entry.get(key) or "")
        for key in ("id", "servicer", "runtime", "script")
    ).casefold()
    if env.get("MORTGAGE_GENERATED_HAR_PATH") or "generated_mortgage_downloaders" in marker:
        return {
            "downloader_mode": "har_backed_offline_extractor",
            "downloader_mode_detail": "reads embedded statement PDFs from captured full-response HAR evidence",
            "uses_workflow_har_evidence": True,
            "requires_live_portal_auth": False,
        }
    if is_citadel_entry(entry):
        return {
            "downloader_mode": "live_cdp_portal_downloader",
            "downloader_mode_detail": "uses a CDP-visible authenticated lender portal session",
            "uses_workflow_har_evidence": True,
            "requires_live_portal_auth": True,
        }
    return {
        "downloader_mode": "custom_script",
        "downloader_mode_detail": "script-defined downloader behavior",
        "uses_workflow_har_evidence": configured_har_path_for(entry) is not None,
        "requires_live_portal_auth": None,
    }


def workflow_evidence_items(intake: dict[str, Any]) -> list[str]:
    evidence = intake.get("workflow_evidence") or []
    if not isinstance(evidence, list):
        return []
    return [str(item) for item in evidence if str(item or "").strip()]


def workflow_evidence_capture_item(
    prop: str,
    intake: dict[str, Any],
    record: dict[str, Any],
    handoff_dir: Path = DEFAULT_HANDOFF_DIR,
) -> dict[str, Any]:
    portal_url = intake.get("portal_url")
    suggested_har = suggested_workflow_har_path(prop, portal_url)
    suggested_har_exists = Path(suggested_har).exists()
    return {
        "property": prop,
        "servicer_hint": intake.get("servicer_hint"),
        "portal_url": portal_url,
        "target_statement_dir": intake.get("target_statement_dir"),
        "latest_statement_date": record.get("statement_date"),
        "latest_statement_filename": record.get("statement_filename"),
        "suggested_workflow_har_path": suggested_har,
        "suggested_workflow_har_path_exists": suggested_har_exists,
        "register_workflow_evidence_ready_to_run_now": suggested_har_exists,
        "advance_workflow_evidence_ready_to_run_now": suggested_har_exists,
        "capture_required_before_offline_next_step": not suggested_har_exists,
        "handoff_path": workflow_evidence_handoff_path(prop, handoff_dir),
        "handoff_markdown_path": workflow_evidence_handoff_markdown_path(prop, handoff_dir),
        "handoff_command": workflow_evidence_handoff_command(prop),
        "post_capture_check_script_path": workflow_evidence_check_path(prop, handoff_dir),
        "post_capture_check_command": workflow_evidence_check_command(prop, handoff_dir),
        "post_capture_check_safe_to_run_after_har_capture": True,
        "capture_command": workflow_evidence_capture_command(prop, portal_url, suggested_har),
        "capture_command_ready_to_run_now": bool(portal_url and suggested_har),
        "capture_command_safe_to_run_automatically": False,
        "analysis_command": workflow_evidence_analysis_command(prop),
        "required_capture_quality": "full_response_bodies",
        "command_precondition_status": "ready" if suggested_har_exists else "missing_workflow_har",
        "command_precondition_next_action": None if suggested_har_exists else "place_har_at_suggested_workflow_har_path",
        "safe_to_run_automatically": False,
    }


def workflow_evidence_handoff_fields(prop: str, handoff_dir: Path = DEFAULT_HANDOFF_DIR) -> dict[str, Any]:
    path = Path(workflow_evidence_handoff_path(prop, handoff_dir))
    fields: dict[str, Any] = {
        "handoff_path": str(path),
        "handoff_markdown_path": workflow_evidence_handoff_markdown_path(prop, handoff_dir),
        "handoff_command": workflow_evidence_handoff_command(prop),
        "post_capture_check_script_path": workflow_evidence_check_path(prop, handoff_dir),
        "post_capture_check_command": workflow_evidence_check_command(prop, handoff_dir),
        "post_capture_check_safe_to_run_after_har_capture": True,
        "workflow_evidence_handoff_exists": path.exists(),
        "workflow_evidence_handoff_readable": False,
        "workflow_evidence_handoff_error": None,
    }
    if not path.exists():
        return fields
    data, error = load_json(path)
    if error or data is None:
        fields["workflow_evidence_handoff_error"] = error or "unreadable"
        return fields
    fields.update(
        {
            "workflow_evidence_handoff_readable": True,
            "workflow_evidence_handoff_status": data.get("status"),
            "workflow_evidence_handoff_reason": data.get("reason"),
            "workflow_evidence_handoff_suggested_next_action": data.get("suggested_next_action"),
            "required_capture_quality": data.get("required_capture_quality"),
            "suggested_workflow_har_path": data.get("suggested_workflow_har_path"),
            "suggested_workflow_har_path_exists": data.get("suggested_workflow_har_path_exists"),
            "capture_required_before_offline_next_step": data.get("capture_required_before_offline_next_step"),
            "capture_command": data.get("capture_command"),
            "capture_command_ready_to_run_now": data.get("capture_command_ready_to_run_now"),
            "capture_command_safe_to_run_automatically": data.get("capture_command_safe_to_run_automatically"),
            "required_response_paths": string_list(data.get("required_response_paths")),
            "post_capture_check_script_path": data.get("post_capture_check_script_path")
            or fields["post_capture_check_script_path"],
            "post_capture_check_command": data.get("post_capture_check_command") or fields["post_capture_check_command"],
            "post_capture_check_safe_to_run_after_har_capture": data.get(
                "post_capture_check_safe_to_run_after_har_capture"
            )
            if data.get("post_capture_check_safe_to_run_after_har_capture") is not None
            else fields["post_capture_check_safe_to_run_after_har_capture"],
            "analysis_command": data.get("analysis_command"),
            "analysis_report_path": data.get("analysis_report_path"),
            "register_workflow_evidence_command": data.get("register_workflow_evidence_command"),
            "register_workflow_evidence_apply_command": data.get("register_workflow_evidence_apply_command"),
            "register_workflow_evidence_ready_to_run_now": data.get("register_workflow_evidence_ready_to_run_now"),
            "advance_workflow_evidence_command": data.get("advance_workflow_evidence_command"),
            "advance_workflow_evidence_apply_command": data.get("advance_workflow_evidence_apply_command"),
            "advance_workflow_evidence_ready_to_run_now": data.get("advance_workflow_evidence_ready_to_run_now"),
            "workflow_evidence_path_count": data.get("workflow_evidence_path_count"),
            "workflow_evidence_paths": data.get("workflow_evidence_paths"),
            "target_statement_month": data.get("target_statement_month"),
            "target_month_document_identifiers": string_list(data.get("target_month_document_identifiers")),
            "latest_statement_document_metadata_only_month": data.get(
                "latest_statement_document_metadata_only_month"
            ),
            "capture_auth_blocker": data.get("capture_auth_blocker"),
            "capture_auth_blocker_reason": data.get("capture_auth_blocker_reason"),
            "capture_auth_next_action": data.get("capture_auth_next_action"),
            "capture_command_blocked_reason": data.get("capture_command_blocked_reason"),
            "command_precondition_status": data.get("command_precondition_status"),
            "command_precondition_reason": data.get("command_precondition_reason"),
            "command_precondition_next_action": data.get("command_precondition_next_action"),
            "live_cdp_downloader_command": data.get("live_cdp_downloader_command"),
            "live_cdp_downloader_ready_after_visible_auth": data.get("live_cdp_downloader_ready_after_visible_auth"),
            "source_downloader_id": data.get("source_downloader_id"),
            "source_downloader_status": data.get("source_downloader_status"),
            "source_downloader_report_status": data.get("source_downloader_report_status"),
            "source_downloader_runtime_status": data.get("source_downloader_runtime_status"),
            "source_downloader_manual_auth_required": data.get("source_downloader_manual_auth_required"),
            "source_downloader_operator_next_action": data.get("source_downloader_operator_next_action"),
            "source_downloader_suggested_next_action": data.get("source_downloader_suggested_next_action"),
        }
    )
    if fields.get("suggested_workflow_har_path_exists") is False:
        fields["register_workflow_evidence_ready_to_run_now"] = False
        fields["advance_workflow_evidence_ready_to_run_now"] = False
        fields["command_precondition_status"] = "missing_workflow_har"
        fields["command_precondition_next_action"] = "place_har_at_suggested_workflow_har_path"
    queue_path = handoff_dir / "mortgage_workflow_capture_queue.json"
    queue_data, queue_error = load_json(queue_path)
    if not queue_error and isinstance(queue_data, dict):
        for entry in queue_data.get("entries") or []:
            if not isinstance(entry, dict) or normalize_property(entry.get("property")) != normalize_property(prop):
                continue
            for key in WORKFLOW_QUEUE_CONTEXT_KEYS:
                if (
                    entry.get("capture_auth_blocker") is True
                    or fields.get(key) in (None, "", [])
                ) and entry.get(key) not in (None, "", []):
                    fields[key] = entry.get(key)
            break
    return fields


def workflow_target_month_for_item(item: dict[str, Any], workflow_fields: dict[str, Any]) -> object:
    workflow_target_month = workflow_fields.get("target_statement_month")
    report_target_month = item.get("latest_report_target_month")
    if workflow_target_month and report_target_month and workflow_target_month != report_target_month:
        report_ids = string_list(item.get("latest_report_target_month_document_identifiers"))
        workflow_ids = string_list(workflow_fields.get("target_month_document_identifiers"))
        if workflow_ids and not report_ids and str(workflow_target_month) < str(report_target_month):
            return workflow_target_month
    return report_target_month or workflow_target_month


def workflow_target_document_ids_for_item(item: dict[str, Any], workflow_fields: dict[str, Any]) -> list[str]:
    report_ids = string_list(item.get("latest_report_target_month_document_identifiers"))
    workflow_ids = string_list(workflow_fields.get("target_month_document_identifiers"))
    return report_ids or workflow_ids


def workflow_capture_command_for_item(item: dict[str, Any], workflow_fields: dict[str, Any]) -> str | None:
    return capture_command_with_expected_document_ids(
        item.get("latest_report_next_action_capture_command") or workflow_fields.get("capture_command"),
        workflow_target_document_ids_for_item(item, workflow_fields),
    )


def workflow_advance_command_for_item(
    item: dict[str, Any],
    workflow_fields: dict[str, Any],
    key: str,
) -> str | None:
    return command_with_target_month(workflow_fields.get(key) or item.get(f"latest_report_{key}"), workflow_target_month_for_item(item, workflow_fields))


def normalize_workflow_fields_for_item(
    item: dict[str, Any] | None,
    workflow_fields: dict[str, Any],
) -> dict[str, Any]:
    if not item or not workflow_fields:
        return workflow_fields
    fields = dict(workflow_fields)
    target_month = workflow_target_month_for_item(item, fields)
    target_document_ids = workflow_target_document_ids_for_item(item, fields)
    if target_month:
        fields["target_statement_month"] = target_month
    if target_document_ids:
        fields["target_month_document_identifiers"] = target_document_ids
    fields["capture_command"] = capture_command_with_expected_document_ids(
        fields.get("capture_command"),
        target_document_ids,
    )
    for key in ("advance_workflow_evidence_command", "advance_workflow_evidence_apply_command"):
        fields[key] = command_with_target_month(fields.get(key), target_month)
    fields["live_cdp_downloader_command"] = command_with_target_month(
        fields.get("live_cdp_downloader_command"),
        target_month,
    )
    return fields


def workflow_required_response_paths_for_item(item: dict[str, Any], workflow_fields: dict[str, Any]) -> list[str]:
    capture_command = workflow_capture_command_for_item(item, workflow_fields)
    return merge_unique_strings(
        workflow_fields.get("required_response_paths"),
        item.get("latest_report_required_response_paths"),
        required_response_paths_from_capture_command(capture_command),
    )


def blocked_statement_coverage_context(
    active_downloaders: list[dict[str, Any]],
    blocked_downloaders: list[dict[str, Any]],
    freshness: dict[str, Any],
) -> dict[str, Any]:
    has_blocked_downloader = bool(blocked_downloaders)
    current_statement_available = freshness.get("current_month_statement_available") is True
    report_statement_available = any(
        item.get("latest_report_target_month_statement_available") is True for item in active_downloaders
    )
    statement_coverage_satisfied = current_statement_available or report_statement_available
    return {
        "statement_coverage_satisfied": statement_coverage_satisfied,
        "statement_coverage_satisfied_by_current_month_file": current_statement_available,
        "statement_coverage_satisfied_by_downloader_report": report_statement_available,
        "automation_blocker_blocks_statement_coverage": has_blocked_downloader and not statement_coverage_satisfied,
        "tokenomics_statement_gap_blocking": not current_statement_available,
        "automation_repair_priority": (
            "automation_repair_not_statement_gap"
            if has_blocked_downloader and current_statement_available
            else "statement_gap_and_automation_repair"
            if has_blocked_downloader
            else None
        ),
    }


def build_report(
    real_estate_root: Path,
    workbook_path: Path,
    config_path: Path,
    month: str,
    intake_path: Path = DEFAULT_INTAKE,
    handoff_dir: Path = DEFAULT_HANDOFF_DIR,
    tokenomics_report_path: Path | None = DEFAULT_TOKENOMICS_REPORT,
) -> dict[str, Any]:
    workbook = tokenomics.build_report(real_estate_root, workbook_path, month)
    tokenomics_source = "workbook"
    tokenomics_fallback_error = None
    tokenomics_fallback_current_month = None
    if not workbook.get("statement_records"):
        fallback, tokenomics_fallback_error = fallback_tokenomics_report(tokenomics_report_path)
        if fallback:
            tokenomics_fallback_current_month = fallback.get("current_month")
            fallback_workbook = dict(workbook)
            fallback_workbook["statement_records"] = fallback.get("statement_records") or []
            fallback_workbook["property_column_count"] = fallback.get("property_column_count")
            fallback_workbook["co_owner_paid_property_count"] = fallback.get("co_owner_paid_property_count")
            fallback_workbook["statement_found_count"] = fallback.get("statement_found_count")
            fallback_workbook["status"] = fallback.get("status") or fallback.get("classification") or workbook.get("status")
            fallback_workbook["current_month"] = fallback.get("current_month") or workbook.get("current_month")
            workbook = fallback_workbook
            tokenomics_source = "tokenomics_report_fallback"
    entries, config_state = config_entries(config_path)
    intake_by_property, intake_state = intake_entries(intake_path)

    configured_by_property: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        prop = str(entry.get("property") or "").strip()
        if not prop:
            continue
        normalized = normalize_property(prop)
        configured_by_property.setdefault(normalized, []).append(
            {
                "id": entry.get("id"),
                "property": prop,
                "servicer": entry.get("servicer"),
                "enabled": bool(entry.get("enabled", True)),
                "co_owner_paid_mortgage": bool(entry.get("co_owner_paid_mortgage")),
                "runtime": entry.get("runtime"),
                **downloader_mode(entry),
                **script_state(entry),
                **downloader_report_state(entry),
            }
        )

    property_records = workbook.get("statement_records") or []
    if not isinstance(property_records, list):
        property_records = []

    coverage_records: list[dict[str, Any]] = []
    missing_downloaders: list[dict[str, Any]] = []
    blocked_configured_downloaders: list[dict[str, Any]] = []
    next_action_plan: list[dict[str, Any]] = []
    workflow_evidence_capture_plan: list[dict[str, Any]] = []
    current_month_statement_gaps: list[dict[str, Any]] = []
    downloader_target_month_statement_gaps: list[dict[str, Any]] = []
    active_downloader_mode_counts: dict[str, int] = {}
    automation_ready_downloader_mode_counts: dict[str, int] = {}
    operationally_ready_count = 0
    configured_but_not_ready_count = 0
    covered_count = 0
    eligible_count = 0
    for record in property_records:
        if not isinstance(record, dict) or not record.get("co_owner_paid_mortgage"):
            continue
        prop = str(record.get("property") or "").strip()
        if not prop:
            continue
        eligible_count += 1
        configured = configured_by_property.get(normalize_property(prop), [])
        intake = intake_by_property.get(normalize_property(prop), {})
        evidence = workflow_evidence_items(intake)
        active = [
            item
            for item in configured
            if item.get("enabled") and item.get("co_owner_paid_mortgage") and item.get("script_exists")
        ]
        ready = [item for item in active if item.get("automation_ready") is True]
        blocked = [item for item in active if item.get("automation_ready") is False]
        for item in active:
            mode = str(item.get("downloader_mode") or "unknown")
            active_downloader_mode_counts[mode] = active_downloader_mode_counts.get(mode, 0) + 1
        for item in ready:
            mode = str(item.get("downloader_mode") or "unknown")
            automation_ready_downloader_mode_counts[mode] = automation_ready_downloader_mode_counts.get(mode, 0) + 1
        coverage_status = "covered" if active else "missing_downloader"
        freshness = statement_month_status(record, month)
        blocked_coverage_context = blocked_statement_coverage_context(active, blocked, freshness)
        missing_reason = None
        if not active:
            missing_reason = "workflow_evidence_missing" if not evidence else "no_enabled_co_owner_paid_downloader_with_existing_script"
        operational_status = "ready" if ready else ("blocked" if blocked else ("unknown" if active else "missing_downloader"))
        capture_item = workflow_evidence_capture_item(prop, intake, record, handoff_dir) if not active else None
        handoff_fields = workflow_evidence_handoff_fields(prop, handoff_dir)
        active_workflow_fields = handoff_fields if active and handoff_fields.get("workflow_evidence_handoff_exists") else {}
        active_auth_blocker_reason = first_auth_blocker(blocked)
        active_workflow_fields = apply_auth_blocker_to_workflow_fields(
            active_workflow_fields,
            active_auth_blocker_reason,
        )
        workflow_context_item = (blocked or active or [None])[0]
        active_workflow_fields = normalize_workflow_fields_for_item(workflow_context_item, active_workflow_fields)
        missing_workflow_fields = (
            {
                "target_statement_dir": capture_item["target_statement_dir"],
                "suggested_workflow_har_path": capture_item["suggested_workflow_har_path"],
                "suggested_workflow_har_path_exists": capture_item["suggested_workflow_har_path_exists"],
                "register_workflow_evidence_ready_to_run_now": capture_item[
                    "register_workflow_evidence_ready_to_run_now"
                ],
                "advance_workflow_evidence_ready_to_run_now": capture_item[
                    "advance_workflow_evidence_ready_to_run_now"
                ],
                "capture_required_before_offline_next_step": capture_item[
                    "capture_required_before_offline_next_step"
                ],
                "handoff_path": capture_item["handoff_path"],
                "handoff_markdown_path": capture_item["handoff_markdown_path"],
                "handoff_command": capture_item["handoff_command"],
                "post_capture_check_script_path": capture_item["post_capture_check_script_path"],
                "post_capture_check_command": capture_item["post_capture_check_command"],
                "post_capture_check_safe_to_run_after_har_capture": capture_item[
                    "post_capture_check_safe_to_run_after_har_capture"
                ],
                "capture_command": capture_item["capture_command"],
                "capture_command_ready_to_run_now": capture_item["capture_command_ready_to_run_now"],
                "capture_command_safe_to_run_automatically": capture_item[
                    "capture_command_safe_to_run_automatically"
                ],
                "analysis_command": capture_item["analysis_command"],
                "required_capture_quality": capture_item["required_capture_quality"],
                "command_precondition_status": capture_item["command_precondition_status"],
                "command_precondition_next_action": capture_item["command_precondition_next_action"],
            }
            if capture_item
            else {}
        )
        coverage = {
            "property": prop,
            "statement_found": record.get("statement_found"),
            **freshness,
            "latest_statement_date": record.get("statement_date"),
            "latest_statement_path": record.get("statement_path"),
            "latest_statement_filename": record.get("statement_filename"),
            "selected_statement_directory": record.get("selected_directory"),
            "servicer_hint": intake.get("servicer_hint"),
            "portal_url": intake.get("portal_url"),
            "intake_status": intake.get("status"),
            "workflow_evidence_count": len(evidence),
            "workflow_evidence": evidence,
            "configured_downloader_count": len(configured),
            "active_downloader_count": len(active),
            "automation_ready_downloader_count": len(ready),
            "automation_blocked_downloader_count": len(blocked),
            "operational_status": operational_status,
            "statement_coverage_satisfied": blocked_coverage_context["statement_coverage_satisfied"],
            "statement_coverage_satisfied_by_current_month_file": blocked_coverage_context[
                "statement_coverage_satisfied_by_current_month_file"
            ],
            "statement_coverage_satisfied_by_downloader_report": blocked_coverage_context[
                "statement_coverage_satisfied_by_downloader_report"
            ],
            "automation_blocker_blocks_statement_coverage": blocked_coverage_context[
                "automation_blocker_blocks_statement_coverage"
            ],
            "tokenomics_statement_gap_blocking": blocked_coverage_context["tokenomics_statement_gap_blocking"],
            "automation_repair_priority": blocked_coverage_context["automation_repair_priority"],
            "configured_downloaders": configured,
            "coverage_status": coverage_status,
            "missing_reason": missing_reason,
            **active_workflow_fields,
            **missing_workflow_fields,
        }
        coverage_records.append(coverage)
        if freshness["current_month_statement_available"] is not True:
            current_month_statement_gaps.append(
                {
                    "property": prop,
                    "statement_month_status": freshness["statement_month_status"],
                    "statement_month": freshness["statement_month"],
                    "payment_due_date": freshness["payment_due_date"],
                    "payment_due_month": freshness["payment_due_month"],
                    "current_month_basis": freshness["current_month_basis"],
                    "latest_statement_date": record.get("statement_date"),
                    "latest_statement_filename": record.get("statement_filename"),
                    "coverage_status": coverage_status,
                    "operational_status": operational_status,
                    "remediation_reason": (
                        configured_downloader_primary_blocker_reason(blocked[0] if blocked else None)
                        or
                        active_workflow_fields.get("workflow_evidence_handoff_reason")
                        or missing_reason
                    ),
                    "latest_report_auth_state": blocked[0].get("latest_report_auth_state") if blocked else None,
                    "latest_report_auth_stage": blocked[0].get("latest_report_auth_stage") if blocked else None,
                    "latest_report_auth_issue": blocked[0].get("latest_report_auth_issue") if blocked else None,
                    "latest_report_auth_issue_text": blocked[0].get("latest_report_auth_issue_text") if blocked else None,
                    "latest_report_operator_next_action": (
                        blocked[0].get("latest_report_operator_next_action") if blocked else None
                    ),
                    "suggested_next_action": configured_downloader_suggested_next_action(
                        blocked[0] if blocked else None,
                        active_workflow_fields,
                    ),
                    **missing_workflow_fields,
                    **active_workflow_fields,
                }
            )
        for item in active:
            if item.get("latest_report_target_month_statement_available") is not False:
                continue
            action_reasons = configured_downloader_blocker_reasons(item)
            target_document_ids = workflow_target_document_ids_for_item(item, active_workflow_fields)
            capture_command = workflow_capture_command_for_item(item, active_workflow_fields)
            required_response_paths = workflow_required_response_paths_for_item(item, active_workflow_fields)
            downloader_target_month_statement_gaps.append(
                {
                    "property": prop,
                    "servicer_hint": intake.get("servicer_hint"),
                    "downloader_id": item.get("id"),
                    "downloader_mode": item.get("downloader_mode"),
                    "statement_month": freshness["statement_month"],
                    "payment_due_date": freshness["payment_due_date"],
                    "payment_due_month": freshness["payment_due_month"],
                    "current_month_basis": freshness["current_month_basis"],
                    "current_month_statement_available": freshness["current_month_statement_available"],
                    **blocked_coverage_context,
                    "latest_report_status": item.get("latest_report_status"),
                    "latest_report_reason": item.get("latest_report_reason"),
                    "latest_report_auth_state": item.get("latest_report_auth_state"),
                    "latest_report_auth_stage": item.get("latest_report_auth_stage"),
                    "latest_report_manual_auth_reason": item.get("latest_report_manual_auth_reason"),
                    "latest_report_auth_issue": item.get("latest_report_auth_issue"),
                    "latest_report_auth_issue_text": item.get("latest_report_auth_issue_text"),
                    "latest_report_operator_next_action": item.get("latest_report_operator_next_action"),
                    **latest_report_auth_context(item),
                    "latest_report_safe_to_run_automatically": item.get(
                        "latest_report_safe_to_run_automatically"
                    ),
                    "target_month": workflow_target_month_for_item(item, active_workflow_fields),
                    "target_month_statement_available": item.get(
                        "latest_report_target_month_statement_available"
                    ),
                    "target_month_existing_count": item.get("latest_report_target_month_existing_count"),
                    "target_month_downloaded_count": item.get("latest_report_target_month_downloaded_count"),
                    "target_month_skipped_count": item.get("latest_report_target_month_skipped_count"),
                    "target_month_recapture_required": item.get("latest_report_target_month_recapture_required"),
                    "target_month_recapture_reason": item.get("latest_report_target_month_recapture_reason"),
                    "candidate_count": item.get("latest_report_candidate_count"),
                    "target_month_candidate_count": item.get("latest_report_target_month_candidate_count"),
                    "target_month_downloadable_count": item.get("latest_report_target_month_downloadable_count"),
                    "target_month_document_identifiers": target_document_ids,
                    "har_path": item.get("latest_report_har_path"),
                    "har_path_exists": item.get("latest_report_har_path_exists"),
                    "blocker": item.get("automation_blocker"),
                    "reason": action_reasons[0] if action_reasons else item.get("automation_blocker"),
                    "reasons": action_reasons,
                    "suggested_next_action": configured_downloader_suggested_next_action(
                        item,
                        active_workflow_fields,
                    ),
                    "required_capture_quality": (
                        active_workflow_fields.get("required_capture_quality")
                        or item.get("latest_report_required_capture_quality")
                    ),
                    "command_precondition_status": (
                        active_workflow_fields.get("command_precondition_status")
                        or item.get("capture_precondition_status")
                    ),
                    "command_precondition_reason": (
                        active_workflow_fields.get("command_precondition_reason")
                        or item.get("capture_precondition_reason")
                    ),
                    "command_precondition_next_action": (
                        active_workflow_fields.get("command_precondition_next_action")
                        or item.get("capture_precondition_next_action")
                    ),
                    "capture_command": capture_command,
                    "capture_command_ready_to_run_now": active_workflow_fields.get(
                        "capture_command_ready_to_run_now"
                    ),
                    "capture_command_safe_to_run_automatically": active_workflow_fields.get(
                        "capture_command_safe_to_run_automatically"
                    ),
                    "post_capture_check_script_path": active_workflow_fields.get(
                        "post_capture_check_script_path"
                    )
                    or workflow_evidence_check_path(prop, handoff_dir),
                    "post_capture_check_command": active_workflow_fields.get("post_capture_check_command")
                    or workflow_evidence_check_command(prop, handoff_dir),
                    "post_capture_check_safe_to_run_after_har_capture": active_workflow_fields.get(
                        "post_capture_check_safe_to_run_after_har_capture"
                    )
                    if active_workflow_fields.get("post_capture_check_safe_to_run_after_har_capture")
                    is not None
                    else True,
                    "required_response_paths": required_response_paths,
                }
	            )
        if active:
            covered_count += 1
        if ready:
            operationally_ready_count += 1
        if blocked:
            configured_but_not_ready_count += 1
            blocked_configured_downloaders.append(
                {
                    "property": prop,
                    "latest_statement_date": record.get("statement_date"),
                    "statement_month": freshness["statement_month"],
                    "payment_due_date": freshness["payment_due_date"],
                    "payment_due_month": freshness["payment_due_month"],
                    "statement_month_status": freshness["statement_month_status"],
                    "current_month_basis": freshness["current_month_basis"],
                    "current_month_statement_available": freshness["current_month_statement_available"],
                    **blocked_coverage_context,
                    "servicer_hint": intake.get("servicer_hint"),
                    "downloader_ids": [item.get("id") for item in blocked],
                    "downloader_modes": [item.get("downloader_mode") for item in blocked],
                    "uses_workflow_har_evidence": [
                        item.get("uses_workflow_har_evidence") for item in blocked
                    ],
                    "requires_live_portal_auth": [
                        item.get("requires_live_portal_auth") for item in blocked
                    ],
                    "latest_report_statuses": [item.get("latest_report_status") for item in blocked],
                    "latest_report_auth_states": [
                        item.get("latest_report_auth_state")
                        for item in blocked
                        if item.get("latest_report_auth_state")
                    ],
                    "latest_report_auth_stages": [
                        item.get("latest_report_auth_stage")
                        for item in blocked
                        if item.get("latest_report_auth_stage")
                    ],
                    "latest_report_auth_issues": [
                        item.get("latest_report_auth_issue")
                        for item in blocked
                        if item.get("latest_report_auth_issue")
                    ],
                    "latest_report_auth_issue_texts": [
                        item.get("latest_report_auth_issue_text")
                        for item in blocked
                        if item.get("latest_report_auth_issue_text")
                    ],
                    "latest_report_operator_next_actions": [
                        item.get("latest_report_operator_next_action")
                        for item in blocked
                        if item.get("latest_report_operator_next_action")
                    ],
                    "latest_report_credentials_available": [
                        item.get("latest_report_credentials_available")
                        for item in blocked
                        if item.get("latest_report_credentials_available") is not None
                    ],
                    "latest_report_credential_sources": [
                        item.get("latest_report_credential_source")
                        for item in blocked
                        if item.get("latest_report_credential_source")
                    ],
                    **latest_report_auth_context_lists(blocked),
                    "reasons": merge_unique_strings(
                        *[configured_downloader_blocker_reasons(item) for item in blocked]
                    ),
                    "automation_blockers": [item.get("automation_blocker") for item in blocked],
                    "har_capture_quality_statuses": [
                        item.get("latest_report_har_capture_quality_status") for item in blocked
                    ],
                    "har_replay_blockers": [item.get("latest_report_har_replay_blocker") for item in blocked],
                    "har_target_months": [item.get("latest_report_har_target_month") for item in blocked],
                    "har_target_month_replayable_document_available": [
                        item.get("latest_report_har_target_month_replayable_document_available") for item in blocked
                    ],
                    "har_target_month_replayable_document_payload_counts": [
                        item.get("latest_report_har_target_month_replayable_document_payload_count") for item in blocked
                    ],
                    "har_replayable_statement_months": [
                        month
                        for item in blocked
                        for month in (item.get("latest_report_har_replayable_statement_months") or [])
                    ],
                    "har_statement_document_months": [
                        month
                        for item in blocked
                        for month in (item.get("latest_report_har_statement_document_months") or [])
                    ],
                    "live_auth_reports": [
                        item.get("latest_live_auth_report") for item in blocked if item.get("latest_live_auth_report")
                    ],
                    "live_auth_statuses": [
                        item.get("latest_live_auth_status") for item in blocked if item.get("latest_live_auth_status")
                    ],
                    "live_auth_blockers": [
                        item.get("latest_live_auth_blocker") for item in blocked if item.get("latest_live_auth_blocker")
                    ],
                    "live_auth_failure_visible_reasons": [
                        item.get("latest_live_auth_auth_failure_visible_reason")
                        for item in blocked
                        if item.get("latest_live_auth_auth_failure_visible_reason")
                    ],
                    "live_auth_manual_auth_required": [
                        item.get("latest_live_auth_manual_auth_required")
                        for item in blocked
                        if item.get("latest_live_auth_manual_auth_required") is not None
                    ],
                    "live_auth_manual_auth_files": [
                        item.get("latest_live_auth_manual_auth_file")
                        for item in blocked
                        if item.get("latest_live_auth_manual_auth_file")
                    ],
                    "live_auth_oauth_error_codes": [
                        code
                        for item in blocked
                        for code in (item.get("latest_live_auth_oauth_password_grant_error_codes") or [])
                    ],
                    "credential_reconcile_reports": [
                        item.get("latest_credential_reconcile_report")
                        for item in blocked
                        if item.get("latest_credential_reconcile_report")
                    ],
                    "credential_reconcile_statuses": [
                        item.get("latest_credential_reconcile_status")
                        for item in blocked
                        if item.get("latest_credential_reconcile_status")
                    ],
                    "credential_reconcile_successful_password_request_found": [
                        item.get("latest_credential_reconcile_successful_password_request_found")
                        for item in blocked
                        if item.get("latest_credential_reconcile_successful_password_request_found") is not None
                    ],
                    "credential_reconcile_username_matches_har": [
                        item.get("latest_credential_reconcile_username_matches_har")
                        for item in blocked
                        if item.get("latest_credential_reconcile_username_matches_har") is not None
                    ],
                    "credential_reconcile_password_matched_before_update": [
                        item.get("latest_credential_reconcile_password_matched_before_update")
                        for item in blocked
                        if item.get("latest_credential_reconcile_password_matched_before_update") is not None
                    ],
                    "target_months": [
                        workflow_target_month_for_item(item, active_workflow_fields) for item in blocked
                    ],
                    "target_month_statement_available": [
                        item.get("latest_report_target_month_statement_available") for item in blocked
                    ],
                    "target_month_existing_counts": [
                        item.get("latest_report_target_month_existing_count") for item in blocked
                    ],
                    "target_month_downloaded_counts": [
                        item.get("latest_report_target_month_downloaded_count") for item in blocked
                    ],
                    "target_month_skipped_counts": [
                        item.get("latest_report_target_month_skipped_count") for item in blocked
                    ],
                    "target_month_document_identifiers": merge_unique_strings(
                        *[
                            workflow_target_document_ids_for_item(item, active_workflow_fields)
                            for item in blocked
                        ]
                    ),
                    "har_paths": [item.get("latest_report_har_path") for item in blocked],
                    "har_path_exists": [item.get("latest_report_har_path_exists") for item in blocked],
                    "candidate_counts": [item.get("latest_report_candidate_count") for item in blocked],
                    "target_month_candidate_counts": [
                        item.get("latest_report_target_month_candidate_count") for item in blocked
                    ],
                    "target_month_downloadable_counts": [
                        item.get("latest_report_target_month_downloadable_count") for item in blocked
                    ],
                    "next_action_statuses": [item.get("latest_report_next_action_status") for item in blocked],
                    "suggested_next_actions": merge_unique_strings(
                        *[
                            [configured_downloader_suggested_next_action(item, active_workflow_fields)]
                            for item in blocked
                        ]
                    ),
                    "next_actions": merge_unique_strings(
                        *[
                            [configured_downloader_suggested_next_action(item, active_workflow_fields)]
                            for item in blocked
                        ]
                    ),
                    "next_action_commands": [item.get("latest_report_next_action_command") for item in blocked],
                    "next_action_capture_commands": [
                        workflow_capture_command_for_item(item, active_workflow_fields) for item in blocked
                    ],
                    "validate_existing_capture_commands": [
                        item.get("latest_report_validate_existing_capture_command") for item in blocked
                    ],
                    "advance_capture_workflow_evidence_dry_run_commands": [
                        item.get("latest_report_advance_capture_workflow_evidence_dry_run_command") for item in blocked
                    ],
                    "advance_capture_workflow_evidence_apply_commands": [
                        item.get("latest_report_advance_capture_workflow_evidence_apply_command") for item in blocked
                    ],
                    "handoff_path": workflow_evidence_handoff_path(prop, handoff_dir),
                    "handoff_markdown_path": workflow_evidence_handoff_markdown_path(prop, handoff_dir),
                    "handoff_command": workflow_evidence_handoff_command(prop),
                    "post_capture_check_script_path": workflow_evidence_check_path(prop, handoff_dir),
                    "post_capture_check_command": workflow_evidence_check_command(prop, handoff_dir),
                    "post_capture_check_safe_to_run_after_har_capture": True,
                    "required_response_paths": merge_unique_strings(
                        *[workflow_required_response_paths_for_item(item, active_workflow_fields) for item in blocked]
                    ),
                    "capture_required": any(item.get("latest_report_capture_required") is True for item in blocked),
                    "capture_precondition_prepare_commands": [
                        item.get("capture_precondition_prepare_command")
                        for item in blocked
                        if item.get("capture_precondition_prepare_command")
                    ],
                    "capture_precondition_safe_to_prepare_now": [
                        item.get("capture_precondition_safe_to_prepare_now")
                        for item in blocked
                        if item.get("capture_precondition_safe_to_prepare_now") is not None
                    ],
                }
            )
            for item in blocked:
                    action_reasons = configured_downloader_blocker_reasons(item)
                    target_document_ids = workflow_target_document_ids_for_item(item, active_workflow_fields)
                    capture_command = workflow_capture_command_for_item(item, active_workflow_fields)
                    required_response_paths = workflow_required_response_paths_for_item(item, active_workflow_fields)
                    next_action_plan.append(
                        {
                        "type": "resolve_configured_downloader_blocker",
                        "property": prop,
                        "servicer_hint": intake.get("servicer_hint"),
                        "downloader_id": item.get("id"),
                        "downloader_mode": item.get("downloader_mode"),
                        "uses_workflow_har_evidence": item.get("uses_workflow_har_evidence"),
                        "requires_live_portal_auth": item.get("requires_live_portal_auth"),
                        "statement_month": freshness["statement_month"],
                        "payment_due_date": freshness["payment_due_date"],
                        "payment_due_month": freshness["payment_due_month"],
                        "statement_month_status": freshness["statement_month_status"],
                        "current_month_basis": freshness["current_month_basis"],
                        "current_month_statement_available": freshness["current_month_statement_available"],
                        **blocked_coverage_context,
                        "blocker": item.get("automation_blocker"),
                        "reason": action_reasons[0] if action_reasons else None,
                        "reasons": action_reasons,
                        "live_auth_report": item.get("latest_live_auth_report"),
                        "live_auth_status": item.get("latest_live_auth_status"),
                        "live_auth_blocker": item.get("latest_live_auth_blocker"),
                        "live_auth_failure_visible_reason": item.get("latest_live_auth_auth_failure_visible_reason"),
                        "live_auth_manual_auth_required": item.get("latest_live_auth_manual_auth_required"),
                        "live_auth_manual_auth_file": item.get("latest_live_auth_manual_auth_file"),
                        "live_auth_oauth_error_codes": item.get("latest_live_auth_oauth_password_grant_error_codes"),
                        "credential_reconcile_report": item.get("latest_credential_reconcile_report"),
                        "credential_reconcile_status": item.get("latest_credential_reconcile_status"),
                        "credential_reconcile_successful_password_request_found": item.get(
                            "latest_credential_reconcile_successful_password_request_found"
                        ),
                        "credential_reconcile_username_matches_har": item.get(
                            "latest_credential_reconcile_username_matches_har"
                        ),
                        "credential_reconcile_password_matched_before_update": item.get(
                            "latest_credential_reconcile_password_matched_before_update"
                        ),
                        "capture_precondition_status": item.get("capture_precondition_status"),
                        "capture_precondition_blocker": item.get("capture_precondition_blocker"),
                        "capture_precondition_reason": item.get("capture_precondition_reason"),
                        "capture_precondition_manual_auth_required": item.get(
                            "capture_precondition_manual_auth_required"
                        ),
                        "capture_precondition_next_action": item.get("capture_precondition_next_action"),
                        "capture_precondition_safe_to_capture_now": item.get(
                            "capture_precondition_safe_to_capture_now"
                        ),
                        "capture_precondition_prepare_command": item.get("capture_precondition_prepare_command"),
                        "capture_precondition_safe_to_prepare_now": item.get(
                            "capture_precondition_safe_to_prepare_now"
                        ),
                        "latest_report_status": item.get("latest_report_status"),
                        "latest_report_auth_state": item.get("latest_report_auth_state"),
                        "latest_report_auth_stage": item.get("latest_report_auth_stage"),
                        "latest_report_manual_auth_reason": item.get("latest_report_manual_auth_reason"),
                        "latest_report_auth_issue": item.get("latest_report_auth_issue"),
                        "latest_report_auth_issue_text": item.get("latest_report_auth_issue_text"),
                        "latest_report_operator_next_action": item.get("latest_report_operator_next_action"),
                        "latest_report_credentials_available": item.get("latest_report_credentials_available"),
                        "latest_report_credential_source": item.get("latest_report_credential_source"),
                        **latest_report_auth_context(item),
                        "latest_report_safe_to_run_automatically": item.get(
                            "latest_report_safe_to_run_automatically"
                        ),
                        "latest_report_target_month_recapture_required": item.get(
                            "latest_report_target_month_recapture_required"
                        ),
                        "latest_report_required_capture_quality": item.get("latest_report_required_capture_quality"),
                        "latest_report_target_month_recapture_reason": item.get(
                            "latest_report_target_month_recapture_reason"
                        ),
                        "latest_report_suggested_next_action": item.get("latest_report_suggested_next_action"),
                        "suggested_next_action": configured_downloader_suggested_next_action(
                            item,
                            active_workflow_fields,
                        ),
                        "required_capture_quality": (
                            active_workflow_fields.get("required_capture_quality")
                            or item.get("latest_report_required_capture_quality")
                        ),
                        "target_month_recapture_required": item.get("latest_report_target_month_recapture_required"),
                        "target_month_recapture_reason": item.get("latest_report_target_month_recapture_reason"),
                        "suggested_workflow_har_path": active_workflow_fields.get("suggested_workflow_har_path"),
                        "suggested_workflow_har_path_exists": active_workflow_fields.get(
                            "suggested_workflow_har_path_exists"
                        ),
                        "capture_command_ready_to_run_now": active_workflow_fields.get(
                            "capture_command_ready_to_run_now"
                        ),
                        "capture_command_blocked_reason": active_workflow_fields.get(
                            "capture_command_blocked_reason"
                        ),
                        "capture_command_safe_to_run_automatically": active_workflow_fields.get(
                            "capture_command_safe_to_run_automatically"
                        ),
                        "capture_auth_blocker": active_workflow_fields.get("capture_auth_blocker"),
                        "capture_auth_blocker_reason": active_workflow_fields.get("capture_auth_blocker_reason"),
                        "capture_auth_next_action": active_workflow_fields.get("capture_auth_next_action"),
                        "live_cdp_downloader_command": active_workflow_fields.get("live_cdp_downloader_command"),
                        "live_cdp_downloader_ready_after_visible_auth": active_workflow_fields.get(
                            "live_cdp_downloader_ready_after_visible_auth"
                        ),
                        "source_downloader_id": active_workflow_fields.get("source_downloader_id"),
                        "source_downloader_status": active_workflow_fields.get("source_downloader_status"),
                        "source_downloader_report_status": active_workflow_fields.get(
                            "source_downloader_report_status"
                        ),
                        "source_downloader_runtime_status": active_workflow_fields.get(
                            "source_downloader_runtime_status"
                        ),
                        "source_downloader_manual_auth_required": active_workflow_fields.get(
                            "source_downloader_manual_auth_required"
                        ),
                        "source_downloader_operator_next_action": active_workflow_fields.get(
                            "source_downloader_operator_next_action"
                        ),
                        "source_downloader_suggested_next_action": active_workflow_fields.get(
                            "source_downloader_suggested_next_action"
                        ),
                        "register_workflow_evidence_ready_to_run_now": active_workflow_fields.get(
                            "register_workflow_evidence_ready_to_run_now"
                        ),
                        "advance_workflow_evidence_ready_to_run_now": active_workflow_fields.get(
                            "advance_workflow_evidence_ready_to_run_now"
                        ),
                        "command_precondition_status": active_workflow_fields.get("command_precondition_status"),
                        "command_precondition_reason": active_workflow_fields.get("command_precondition_reason"),
                        "command_precondition_next_action": active_workflow_fields.get(
                            "command_precondition_next_action"
                        ),
                        "target_month": workflow_target_month_for_item(item, active_workflow_fields),
                        "target_month_statement_available": item.get(
                            "latest_report_target_month_statement_available"
                        ),
                        "target_month_existing_count": item.get("latest_report_target_month_existing_count"),
                        "target_month_downloaded_count": item.get("latest_report_target_month_downloaded_count"),
                        "target_month_skipped_count": item.get("latest_report_target_month_skipped_count"),
                            "target_month_document_identifiers": target_document_ids,
                        "har_path": item.get("latest_report_har_path"),
                        "har_path_exists": item.get("latest_report_har_path_exists"),
                        "candidate_count": item.get("latest_report_candidate_count"),
                        "target_month_candidate_count": item.get("latest_report_target_month_candidate_count"),
                        "target_month_downloadable_count": item.get("latest_report_target_month_downloadable_count"),
                        "latest_report_idempotency_digest": item.get("latest_report_idempotency_digest"),
                        "target_month_replayable_document_available": item.get(
                            "latest_report_har_target_month_replayable_document_available"
                        ),
                        "target_month_replayable_document_payload_count": item.get(
                            "latest_report_har_target_month_replayable_document_payload_count"
                        ),
                        "replayable_statement_months": item.get("latest_report_har_replayable_statement_months"),
                        "statement_document_months": item.get("latest_report_har_statement_document_months"),
                        "existing_target_month_files": item.get("latest_report_existing_target_month_files"),
                        "downloaded_target_month_files": item.get("latest_report_downloaded_target_month_files"),
                        "skipped_target_month_files": item.get("latest_report_skipped_target_month_files"),
                        "next_action_status": item.get("latest_report_next_action_status"),
                        "next_command": item.get("latest_report_next_action_command"),
                            "capture_command": capture_command,
                        "capture_plan_report": item.get("latest_report_capture_plan_report"),
                        "capture_plan_markdown": item.get("latest_report_capture_plan_markdown"),
                        "capture_har_path": item.get("latest_report_capture_har_path"),
                        "capture_report": item.get("latest_report_capture_report"),
                        "post_capture_diagnostics_command": item.get("latest_report_post_capture_diagnostics_command"),
                        "validate_existing_capture_command": item.get("latest_report_validate_existing_capture_command"),
                        "advance_capture_workflow_evidence_dry_run_command": command_with_target_month(
                            item.get("latest_report_advance_capture_workflow_evidence_dry_run_command"),
                            workflow_target_month_for_item(item, active_workflow_fields),
                        ),
                        "advance_capture_workflow_evidence_apply_command": command_with_target_month(
                            item.get("latest_report_advance_capture_workflow_evidence_apply_command"),
                            workflow_target_month_for_item(item, active_workflow_fields),
                        ),
                        "workflow_evidence_advance_report": item.get("latest_report_workflow_evidence_advance_report"),
                        "advance_workflow_evidence_command": command_with_target_month(
                            item.get("latest_report_advance_workflow_evidence_command"),
                            workflow_target_month_for_item(item, active_workflow_fields),
                        ),
                        "advance_workflow_evidence_apply_command": command_with_target_month(
                            item.get("latest_report_advance_workflow_evidence_apply_command"),
                            workflow_target_month_for_item(item, active_workflow_fields),
                        ),
                        "handoff_path": workflow_evidence_handoff_path(prop, handoff_dir),
                        "handoff_markdown_path": workflow_evidence_handoff_markdown_path(prop, handoff_dir),
                        "handoff_command": workflow_evidence_handoff_command(prop),
                        "post_capture_check_script_path": workflow_evidence_check_path(prop, handoff_dir),
                        "post_capture_check_command": workflow_evidence_check_command(prop, handoff_dir),
                        "post_capture_check_safe_to_run_after_har_capture": True,
                        "capture_required": item.get("latest_report_capture_required"),
                            "required_response_paths": required_response_paths,
                        "safe_to_run_automatically": (
                            item.get("latest_report_safe_to_run_automatically") is not False
                            and item.get("latest_report_capture_required") is not True
                            and not item.get("latest_report_auth_issue")
                            and not item.get("latest_report_operator_next_action")
                            and active_workflow_fields.get("capture_required_before_offline_next_step") is not True
                            and active_workflow_fields.get("command_precondition_status")
                            not in {"missing_workflow_har", "blocked_by_auth"}
                        ),
                    }
                )
        if not active:
            required_response_paths = [
                path
                for item in blocked
                for path in (item.get("latest_report_required_response_paths") or [])
            ]
            missing = {
                "property": prop,
                "latest_statement_date": record.get("statement_date"),
                "statement_month": freshness["statement_month"],
                "payment_due_date": freshness["payment_due_date"],
                "payment_due_month": freshness["payment_due_month"],
                "statement_month_status": freshness["statement_month_status"],
                "current_month_basis": freshness["current_month_basis"],
                "current_month_statement_available": freshness["current_month_statement_available"],
                "latest_statement_path": record.get("statement_path"),
                "latest_statement_filename": record.get("statement_filename"),
                "servicer_hint": intake.get("servicer_hint"),
                "portal_url": intake.get("portal_url"),
                "intake_status": intake.get("status"),
                "workflow_evidence_count": len(evidence),
                "suggested_workflow_har_path": capture_item["suggested_workflow_har_path"],
                "suggested_workflow_har_path_exists": capture_item["suggested_workflow_har_path_exists"],
                "register_workflow_evidence_ready_to_run_now": capture_item[
                    "register_workflow_evidence_ready_to_run_now"
                ],
                "advance_workflow_evidence_ready_to_run_now": capture_item[
                    "advance_workflow_evidence_ready_to_run_now"
                ],
                "capture_required_before_offline_next_step": capture_item[
                    "capture_required_before_offline_next_step"
                ],
                "handoff_path": workflow_evidence_handoff_path(prop, handoff_dir),
                "handoff_markdown_path": workflow_evidence_handoff_markdown_path(prop, handoff_dir),
                "handoff_command": workflow_evidence_handoff_command(prop),
                "post_capture_check_script_path": workflow_evidence_check_path(prop, handoff_dir),
                "post_capture_check_command": workflow_evidence_check_command(prop, handoff_dir),
                "post_capture_check_safe_to_run_after_har_capture": True,
                "capture_command": capture_item["capture_command"],
                "capture_command_ready_to_run_now": capture_item["capture_command_ready_to_run_now"],
                "capture_command_safe_to_run_automatically": False,
                "analysis_command": workflow_evidence_analysis_command(prop),
                "required_response_paths": required_response_paths,
                "command_precondition_status": capture_item["command_precondition_status"],
                "command_precondition_next_action": capture_item["command_precondition_next_action"],
                "reason": missing_reason,
            }
            missing_downloaders.append(missing)
            workflow_evidence_capture_plan.append(capture_item)
            next_action_plan.append(
                {
                    "type": "collect_workflow_evidence",
                    "property": prop,
                    "servicer_hint": intake.get("servicer_hint"),
                    "portal_url": intake.get("portal_url"),
                    "target_statement_dir": intake.get("target_statement_dir"),
                    "statement_month": freshness["statement_month"],
                    "payment_due_date": freshness["payment_due_date"],
                    "payment_due_month": freshness["payment_due_month"],
                    "statement_month_status": freshness["statement_month_status"],
                    "current_month_basis": freshness["current_month_basis"],
                    "current_month_statement_available": freshness["current_month_statement_available"],
                    "suggested_workflow_har_path": capture_item["suggested_workflow_har_path"],
                    "suggested_workflow_har_path_exists": capture_item["suggested_workflow_har_path_exists"],
                    "register_workflow_evidence_ready_to_run_now": capture_item[
                        "register_workflow_evidence_ready_to_run_now"
                    ],
                    "advance_workflow_evidence_ready_to_run_now": capture_item[
                        "advance_workflow_evidence_ready_to_run_now"
                    ],
                    "capture_required_before_offline_next_step": capture_item[
                        "capture_required_before_offline_next_step"
                    ],
                    "handoff_path": capture_item["handoff_path"],
                    "handoff_markdown_path": capture_item["handoff_markdown_path"],
                    "handoff_command": capture_item["handoff_command"],
                    "post_capture_check_script_path": capture_item["post_capture_check_script_path"],
                    "post_capture_check_command": capture_item["post_capture_check_command"],
                    "post_capture_check_safe_to_run_after_har_capture": True,
                    "capture_command": capture_item["capture_command"],
                    "capture_command_ready_to_run_now": capture_item["capture_command_ready_to_run_now"],
                    "capture_command_safe_to_run_automatically": False,
                    "analysis_command": capture_item["analysis_command"],
                    "required_capture_quality": capture_item["required_capture_quality"],
                    "required_response_paths": required_response_paths,
                    "command_precondition_status": capture_item["command_precondition_status"],
                    "command_precondition_next_action": capture_item["command_precondition_next_action"],
                    "reason": missing_reason,
                    "workflow_evidence_count": len(evidence),
                    "safe_to_run_automatically": False,
                }
            )

    unexpected_configured = []
    workbook_props = {
        normalize_property(record.get("property"))
        for record in property_records
        if isinstance(record, dict) and record.get("property")
    }
    for normalized, items in configured_by_property.items():
        if normalized not in workbook_props:
            unexpected_configured.extend(items)
    unexpected_configured_properties = list(
        dict.fromkeys(str(item.get("property") or "").strip() for item in unexpected_configured if item.get("property"))
    )

    current_month_statement_gap_properties = [item["property"] for item in current_month_statement_gaps]
    stale_statement_properties = [
        item["property"] for item in current_month_statement_gaps if item.get("statement_month_status") == "stale"
    ]
    missing_current_month_statement_properties = [
        item["property"]
        for item in current_month_statement_gaps
        if item.get("statement_month_status") in {"missing", "unknown"}
    ]
    downloader_target_month_statement_gap_properties = list(
        dict.fromkeys(item["property"] for item in downloader_target_month_statement_gaps)
    )
    blocked_configured_downloader_properties = [
        item["property"] for item in blocked_configured_downloaders
    ]
    for action in [*downloader_target_month_statement_gaps, *next_action_plan]:
        if not isinstance(action, dict) or action.get("next_action"):
            continue
        suggested = str(action.get("suggested_next_action") or "").strip()
        if suggested and " " not in suggested:
            action["next_action"] = suggested

    status = "ok" if config_state.get("config_readable") and not missing_downloaders else "review"
    if (
        blocked_configured_downloaders
        or current_month_statement_gaps
        or downloader_target_month_statement_gaps
        or unexpected_configured
    ):
        status = "review"
    next_action_manual_count = sum(1 for item in next_action_plan if not item.get("safe_to_run_automatically"))
    next_action_safe_count = sum(1 for item in next_action_plan if item.get("safe_to_run_automatically") is True)
    safe_to_run_automatically = next_action_manual_count == 0 and (status == "ok" or next_action_safe_count > 0)
    configured_downloaders = [
        downloader
        for record in coverage_records
        for downloader in (record.get("configured_downloaders") or [])
        if isinstance(downloader, dict)
    ]
    active_configured_downloaders = [
        item
        for item in configured_downloaders
        if item.get("enabled") and item.get("co_owner_paid_mortgage") and item.get("script_exists")
    ]
    automation_ready_configured_downloaders = [
        item for item in active_configured_downloaders if item.get("automation_ready") is True
    ]
    automation_blocked_configured_downloaders = [
        item for item in active_configured_downloaders if item.get("automation_ready") is False
    ]
    return {
        "job": "mortgage-downloader-coverage-audit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "month": month,
        "workbook_path": str(workbook_path),
        "real_estate_root": str(real_estate_root),
        **config_state,
        **intake_state,
        "tokenomics_status": workbook.get("status"),
        "tokenomics_source": tokenomics_source,
        "tokenomics_workbook_load_ok": workbook.get("workbook_load_ok"),
        "tokenomics_workbook_load_error": workbook.get("workbook_load_error"),
        "tokenomics_fallback_report": str(tokenomics_report_path) if tokenomics_report_path else None,
        "tokenomics_fallback_report_used": tokenomics_source == "tokenomics_report_fallback",
        "tokenomics_fallback_error": tokenomics_fallback_error,
        "tokenomics_fallback_current_month": tokenomics_fallback_current_month,
        "tokenomics_current_month": workbook.get("current_month"),
        "tokenomics_property_count": workbook.get("property_column_count"),
        "co_owner_paid_property_count": eligible_count,
        "covered_co_owner_paid_property_count": covered_count,
        "automation_ready_co_owner_paid_property_count": operationally_ready_count,
        "configured_but_not_ready_co_owner_paid_property_count": configured_but_not_ready_count,
        "configured_downloader_count": len(configured_downloaders),
        "configured_downloader_ids": merge_unique_strings([item.get("id") for item in configured_downloaders]),
        "configured_downloader_properties": merge_unique_strings(
            [item.get("property") for item in configured_downloaders]
        ),
        "configured_downloader_latest_report_statuses": merge_unique_strings(
            [item.get("latest_report_status") for item in configured_downloaders]
        ),
        "configured_downloader_latest_report_reasons": merge_unique_strings(
            [item.get("latest_report_reason") for item in configured_downloaders]
        ),
        "configured_downloader_blocker_reasons": merge_unique_strings(
            [
                reason
                for item in automation_blocked_configured_downloaders
                for reason in configured_downloader_blocker_reasons(item)
                if reason not in ("ok", "ready")
            ]
        ),
        "active_configured_downloader_count": len(active_configured_downloaders),
        "active_configured_downloader_ids": merge_unique_strings(
            [item.get("id") for item in active_configured_downloaders]
        ),
        "automation_ready_configured_downloader_count": len(automation_ready_configured_downloaders),
        "automation_ready_configured_downloader_ids": merge_unique_strings(
            [item.get("id") for item in automation_ready_configured_downloaders]
        ),
        "automation_blocked_configured_downloader_count": len(automation_blocked_configured_downloaders),
        "automation_blocked_configured_downloader_ids": merge_unique_strings(
            [item.get("id") for item in automation_blocked_configured_downloaders]
        ),
        "active_downloader_mode_counts": active_downloader_mode_counts,
        "automation_ready_downloader_mode_counts": automation_ready_downloader_mode_counts,
        "active_har_backed_offline_extractor_count": active_downloader_mode_counts.get(
            "har_backed_offline_extractor", 0
        ),
        "automation_ready_har_backed_offline_extractor_count": automation_ready_downloader_mode_counts.get(
            "har_backed_offline_extractor", 0
        ),
        "active_live_cdp_portal_downloader_count": active_downloader_mode_counts.get(
            "live_cdp_portal_downloader", 0
        ),
        "automation_ready_live_cdp_portal_downloader_count": automation_ready_downloader_mode_counts.get(
            "live_cdp_portal_downloader", 0
        ),
        "current_month_statement_available_count": eligible_count - len(current_month_statement_gaps),
        "current_month_statement_gap_count": len(current_month_statement_gaps),
        "current_month_statement_gap_properties": current_month_statement_gap_properties,
        "current_month_statement_gaps": current_month_statement_gaps,
        "downloader_target_month_statement_gap_count": len(downloader_target_month_statement_gaps),
        "downloader_target_month_statement_gap_properties": downloader_target_month_statement_gap_properties,
        "downloader_target_month_statement_gaps": downloader_target_month_statement_gaps,
        "stale_statement_count": len(stale_statement_properties),
        "stale_statement_properties": stale_statement_properties,
        "missing_current_month_statement_count": len(missing_current_month_statement_properties),
        "missing_current_month_statement_properties": missing_current_month_statement_properties,
        "blocked_configured_downloader_count": len(blocked_configured_downloaders),
        "blocked_configured_downloader_properties": blocked_configured_downloader_properties,
        "blocked_configured_downloaders": blocked_configured_downloaders,
        "missing_downloader_count": len(missing_downloaders),
        "missing_downloader_properties": [item["property"] for item in missing_downloaders],
        "missing_downloaders": missing_downloaders,
        "workflow_evidence_capture_plan_count": len(workflow_evidence_capture_plan),
        "workflow_evidence_capture_plan": workflow_evidence_capture_plan,
        "next_action_plan_count": len(next_action_plan),
        "next_action_plan": next_action_plan,
        "actions": next_action_plan,
        "next_action_manual_count": next_action_manual_count,
        "next_action_safe_to_run_automatically_count": next_action_safe_count,
        "unexpected_configured_downloader_count": len(unexpected_configured),
        "unexpected_configured_downloader_properties": unexpected_configured_properties,
        "unexpected_configured_downloaders": unexpected_configured,
        "coverage_records": coverage_records,
        "records": coverage_records,
        "safe_to_run_automatically": safe_to_run_automatically,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-estate-root", type=Path, default=tokenomics.REAL_ESTATE_ROOT)
    parser.add_argument("--workbook", type=Path, default=tokenomics.WORKBOOK_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE)
    parser.add_argument("--handoff-dir", type=Path)
    parser.add_argument("--tokenomics-report", type=Path, default=DEFAULT_TOKENOMICS_REPORT)
    parser.add_argument("--month", default=default_target_month())
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    handoff_dir = args.handoff_dir or args.report.parent
    report = build_report(
        args.real_estate_root,
        args.workbook,
        args.config,
        args.month,
        args.intake,
        handoff_dir,
        args.tokenomics_report,
    )
    report = write_json_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
