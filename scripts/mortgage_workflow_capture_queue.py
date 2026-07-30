#!/usr/bin/env python3
"""Build an idempotent operator queue for mortgage workflow HAR captures."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

SCRIPT_PATH = Path(__file__).absolute()
WORKSPACE_ROOT = SCRIPT_PATH.parents[1]
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import audit_mortgage_downloader_coverage as coverage
from stable_json_report import stable_report_digest, write_json_report

DEFAULT_REPORT_DIR = WORKSPACE_ROOT / "reports"
DEFAULT_HANDOFF_INDEX = DEFAULT_REPORT_DIR / "mortgage_workflow_evidence_handoff_index.json"
DEFAULT_EVIDENCE_REFRESH_REPORT = DEFAULT_REPORT_DIR / "mortgage_workflow_evidence_refresh_report.json"
DEFAULT_TOKENOMICS_REPORT = DEFAULT_REPORT_DIR / "coownership_mortgage_tokenomics_update_report.json"
DEFAULT_DOWNLOADER_REPORT = DEFAULT_REPORT_DIR / "mortgage_statement_downloaders_report.json"
DEFAULT_SUPPLEMENTAL_DOWNLOADER_REPORTS = (
    DEFAULT_REPORT_DIR / "mortgage_statement_downloaders_live_cdp_summary_report.json",
)
DEFAULT_REPORT = DEFAULT_REPORT_DIR / "mortgage_workflow_capture_queue.json"
DEFAULT_MARKDOWN = DEFAULT_REPORT_DIR / "mortgage_workflow_capture_queue.md"
DEFAULT_INTAKE = WORKSPACE_ROOT / "config" / "mortgage_downloader_intake.json"
DEFAULT_PROCESSOR_SCRIPT = SCRIPTS_DIR / "process_mortgage_capture_queue.py"
DEFAULT_PROCESSOR_REPORT = DEFAULT_REPORT_DIR / "mortgage_workflow_capture_queue_processor.json"
DEFAULT_PROCESSOR_MARKDOWN = DEFAULT_REPORT_DIR / "mortgage_workflow_capture_queue_processor.md"
DEFAULT_REGISTRY = WORKSPACE_ROOT / "config" / "mortgage_statement_downloaders.json"
DEFAULT_COVERAGE_REPORT = DEFAULT_REPORT_DIR / "mortgage_downloader_coverage_report.json"
BODY_RECAPTURE_CAPTURE_METHOD = "visible_cdp_capture_helper_required"
BODY_RECAPTURE_CAPTURE_INSTRUCTION = (
    "Run capture_command before opening/downloading the target statement PDF in the visible browser; "
    "the helper records Network.getResponseBody so binary PDF payloads are retained."
)
BODY_RECAPTURE_MANUAL_HAR_EXPORT_WARNING = (
    "A browser DevTools HAR export may show HTTP 200 document responses while omitting content.text for "
    "application/octet-stream PDF bodies."
)

CAPTURE_ACTIONS = {
    "capture_workflow_har_with_full_response_bodies",
    "capture_full_response_bodies",
    "capture_target_month_statement",
    "recapture_workflow_har_with_full_response_bodies",
}
EXISTING_HAR_BODY_REASONS = {
    "candidate_endpoints_missing_response_bodies",
    "missing_response_bodies",
    "needs_full_response_bodies",
}
TARGET_MONTH_PDF_RECAPTURE_REASONS = {
    "embedded_statement_pdf_payload_missing",
    "metadata_without_embedded_pdf_payload",
    "target_month_statement_pdf_payload_missing",
}
DOWNLOADER_GAP_REASONS = {
    "target_month_statement_unavailable",
    "target_month_statement_pdf_payload_missing",
}
DOWNLOADER_AUTH_CONTEXT_FIELDS = (
    "credentials_available",
    "credential_source",
    "credential_lookup_status",
    "credential_lookup_failure_reason",
    "credential_lookup_item_name",
    "credential_lookup_expected_folder_name",
    "credential_lookup_uri_host",
    "credential_lookup_uri_host_aliases",
    "credential_lookup_search_terms",
    "credential_lookup_candidate_search_term_count",
    "credential_lookup_candidate_search_terms",
    "credential_lookup_candidate_count",
    "credential_lookup_candidate_items",
    "credential_lookup_unguarded_candidate_count",
    "credential_lookup_unguarded_candidate_items",
    "credential_lookup_misfiled_candidate_count",
    "credential_lookup_absent",
    "credential_lookup_scope",
    "credential_lookup_repair_action",
    "capture_auth_blocker_reason",
    "auth_issue",
    "auth_issue_text",
    "manual_auth_reason",
    "manual_auth_portal_url",
    "auth_failure_reason",
    "auth_failure_visible_reason",
    "auth_mfa_reached",
    "auth_state",
    "auth_stage",
    "credential_login_failure_suspected",
    "credential_login_failure_suspected_reason",
    "credential_login_hint_mismatch",
    "credential_login_hint_mismatch_overridden",
    "auto_login_attempted",
    "auto_login_status",
    "auto_login_blocked_reason",
    "auto_login_force_enabled",
    "auto_login_input_method",
    "auto_login_step",
    "auto_login_username_available",
    "auto_login_password_available",
    "auto_login_username_typed",
    "auto_login_password_typed",
    "auto_otp_attempted",
    "auto_otp_status",
    "auto_otp_code_available",
    "auto_otp_source",
    "auto_otp_fetch_enabled",
    "auto_otp_fetch_attempted",
    "auto_otp_fetch_attempt_count",
    "auto_otp_fetch_status",
    "auto_otp_fetch_exit_code",
    "auto_otp_fetch_report",
)
TARGET_MONTH_PATTERN = re.compile(r"^20\d{2}-(0[1-9]|1[0-2])$")
YEAR_MONTH_TEXT_PATTERN = re.compile(r"20\d{2}-(0[1-9]|1[0-2])")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def load_intake_by_property(path: Path) -> dict[str, dict[str, Any]]:
    data = load_json(path)
    properties = data.get("properties")
    if not isinstance(properties, list):
        return {}
    return {
        normalize_property(item.get("property")): item
        for item in properties
        if isinstance(item, dict) and str(item.get("property") or "").strip()
    }


def intake_workflow_har_path(intake: dict[str, Any]) -> str | None:
    evidence = intake.get("workflow_evidence")
    if not isinstance(evidence, list):
        return None
    for value in reversed(evidence):
        text = str(value or "").strip()
        if text:
            return text
    return None


def canonical_downloader_gap_reason(reason: str, recapture_reason: str) -> str:
    if recapture_reason == "metadata_without_embedded_pdf_payload":
        return "embedded_statement_pdf_payload_missing"
    if recapture_reason:
        return recapture_reason
    return reason


def capture_auth_blocker_reason(auth_context: dict[str, Any]) -> str | None:
    explicit_blocker = str(auth_context.get("capture_auth_blocker_reason") or "").strip()
    if explicit_blocker:
        return explicit_blocker
    auth_failure_reason = str(auth_context.get("auth_failure_reason") or "").strip()
    if auth_failure_reason in {"credential_rejected_before_mfa", "mfa_source_unavailable"}:
        return auth_failure_reason
    auth_issue = str(auth_context.get("auth_issue") or "").strip()
    auth_state = str(auth_context.get("auth_state") or "").strip()
    manual_auth_reason = str(auth_context.get("manual_auth_reason") or "").strip()
    auto_login_blocked = str(auth_context.get("auto_login_blocked_reason") or "").strip()
    if auth_issue in {
        "account_locked",
        "previous_account_locked",
        "login_rejected",
        "login_still_required_after_submit",
        "credential_login_hint_mismatch",
    }:
        return auth_issue
    if auto_login_blocked in {"blocked_account_locked", "blocked_previous_account_locked", "credential_login_hint_mismatch"}:
        return auto_login_blocked
    if auth_issue == "credentials_unavailable":
        return "credentials_unavailable"
    if auth_issue == "not_authenticated" and str(auth_context.get("auth_state") or "").strip() == "login":
        return "visible_loandepot_tab_not_authenticated"
    if auth_state == "login_required" or manual_auth_reason in {"login_required", "authenticated_browser_required"}:
        return "authenticated_browser_required"
    return None


def auth_blocker_next_action(reason: str) -> str:
    if reason == "visible_loandepot_tab_not_authenticated":
        return "Authenticate loanDepot in the visible browser tab, then rerun the live-CDP downloader profile."
    if reason == "credentials_unavailable":
        return "Add or repair the mortgage portal credentials in Ops Bitwarden or runtime env, then rerun the live-CDP downloader profile."
    if reason == "login_still_required_after_submit":
        return (
            "Verify the MortgageQuestions credentials in the visible portal or update the Ops Bitwarden item before "
            "rerunning; an OTP cannot be submitted until the portal reaches MFA."
        )
    if reason == "credential_login_hint_mismatch":
        return (
            "Update the Ops Bitwarden MortgageQuestions item so its username/login field matches the configured "
            "property login hint, then rerun the live-CDP downloader profile."
        )
    if reason == "credential_rejected_before_mfa":
        return (
            "Verify or update the MortgageQuestions credentials in Ops Bitwarden before rerunning; "
            "the portal did not reach MFA after password submission."
        )
    if reason == "mfa_source_unavailable":
        return "Repair the configured MFA/OTP source before rerunning the live-CDP downloader."
    if reason == "authenticated_browser_required":
        return "Log in through the visible MortgageQuestions portal, then rerun the downloader before capturing the target-month statement PDF."
    if reason == "login_rejected":
        return (
            "Recover or update the MortgageQuestions credentials in the visible portal before rerunning; "
            "an OTP cannot be submitted until the portal reaches MFA."
        )
    if reason in {"account_locked", "previous_account_locked", "blocked_account_locked", "blocked_previous_account_locked"}:
        return "Recover or unlock the MortgageQuestions account in the visible portal before capturing the target-month statement PDF."
    return "Resolve portal authentication before capturing the target-month statement PDF."


def auth_blocker_suggested_next_action(entry: dict[str, Any], reason: str) -> str:
    if reason == "credentials_unavailable":
        value = str(entry.get("source_downloader_suggested_next_action") or "").strip()
        if value and " " not in value:
            return value
        return "repair_portal_auth_before_capture"
    for key in ("source_downloader_operator_next_action", "source_downloader_suggested_next_action"):
        value = str(entry.get(key) or "").strip()
        if value and " " not in value:
            return value
    if reason == "visible_loandepot_tab_not_authenticated":
        return "authenticate_visible_loandepot_tab_then_run_live_cdp"
    return "repair_portal_auth_before_capture"


def workflow_precondition_fields(
    *,
    missing_har: bool,
    auth_blocker_reason: str | None = None,
    override_status: str | None = None,
    override_reason: str | None = None,
    override_next_action: str | None = None,
) -> dict[str, Any]:
    if auth_blocker_reason:
        return {
            "command_precondition_status": "blocked_by_auth",
            "command_precondition_reason": auth_blocker_reason,
            "command_precondition_next_action": auth_blocker_next_action(auth_blocker_reason),
        }
    if override_status:
        fields: dict[str, Any] = {"command_precondition_status": override_status}
        if override_reason:
            fields["command_precondition_reason"] = override_reason
        if override_next_action:
            fields["command_precondition_next_action"] = override_next_action
        return fields
    if missing_har:
        return {
            "command_precondition_status": "missing_workflow_har",
            "command_precondition_next_action": "place_har_at_suggested_workflow_har_path",
        }
    return {}


def auth_blocker_blocks_target_pdf_capture(reason: str | None, auth_context: dict[str, Any]) -> bool:
    reason_text = str(reason or "").strip()
    if not reason_text:
        return False
    if reason_text != "authenticated_browser_required":
        return True
    concrete_auth_issue = str(auth_context.get("auth_issue") or "").strip()
    if concrete_auth_issue and concrete_auth_issue not in {"authenticated_browser_required", "not_authenticated"}:
        return True
    return False


def apply_capture_auth_blocker(entry: dict[str, Any], reason: str) -> dict[str, Any]:
    blocked = dict(entry)
    suggested_next_action = auth_blocker_suggested_next_action(blocked, reason)
    blocked["capture_auth_blocker"] = True
    blocked["capture_auth_blocker_reason"] = reason
    blocked["capture_auth_next_action"] = blocked.get("capture_auth_next_action") or auth_blocker_next_action(reason)
    blocked["suggested_next_action"] = suggested_next_action
    blocked["next_action"] = suggested_next_action
    blocked["capture_command_ready_to_run_now"] = False
    blocked["capture_command_blocked_reason"] = reason
    blocked["register_workflow_evidence_ready_to_run_now"] = False
    blocked["advance_workflow_evidence_ready_to_run_now"] = False
    blocked.update(workflow_precondition_fields(missing_har=False, auth_blocker_reason=reason))
    blocked["safe_to_run_automatically"] = False
    return blocked


def normalize_property(value: object) -> str:
    return coverage.normalize_property(value)


def unique_strings(values: list[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def unique_present_values(values: list[object]) -> list[object]:
    seen: set[str] = set()
    result: list[object] = []
    for value in values:
        if value in (None, "", [], {}):
            continue
        marker = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def default_body_recapture_value(key: str, required_capture_quality: object) -> str | None:
    if str(required_capture_quality or "").strip() != "target_month_statement_pdf":
        return None
    if key == "body_recapture_capture_method":
        return BODY_RECAPTURE_CAPTURE_METHOD
    if key == "body_recapture_capture_instruction":
        return BODY_RECAPTURE_CAPTURE_INSTRUCTION
    if key == "body_recapture_manual_har_export_warning":
        return BODY_RECAPTURE_MANUAL_HAR_EXPORT_WARNING
    return None


def handoffs_from_index(index: dict[str, Any]) -> list[dict[str, Any]]:
    handoffs = index.get("handoffs")
    if not isinstance(handoffs, list):
        return []
    return [item for item in handoffs if isinstance(item, dict)]


def rejected_candidates_by_property(refresh: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_property: dict[str, list[dict[str, Any]]] = {}
    results = refresh.get("results")
    if not isinstance(results, list):
        return by_property
    for result in results:
        if not isinstance(result, dict):
            continue
        prop = str(result.get("property") or "").strip()
        if not prop:
            continue
        rejected = [
            candidate
            for candidate in result.get("discovered_har_candidates") or []
            if isinstance(candidate, dict) and not candidate.get("safe_workflow_candidate")
        ]
        if rejected:
            by_property[normalize_property(prop)] = rejected
    return by_property


def tokenomics_gap_properties(tokenomics: dict[str, Any]) -> list[str]:
    for key in ("current_month_statement_gap_properties", "missing_co_owner_paid_statement_properties"):
        values = tokenomics.get(key)
        if isinstance(values, list):
            return unique_strings(values)
    return []


def tokenomics_write_blockers(tokenomics: dict[str, Any]) -> list[dict[str, Any]]:
    blockers = tokenomics.get("workbook_write_blockers")
    if not isinstance(blockers, list):
        return []
    return [item for item in blockers if isinstance(item, dict)]


def compact_remediation_action(action: object) -> dict[str, Any] | None:
    if not isinstance(action, dict):
        return None
    return {
        "type": action.get("type"),
        "property": action.get("property"),
        "reason": action.get("reason"),
        "servicer_hint": action.get("servicer_hint"),
        "portal_url": action.get("portal_url"),
        "suggested_workflow_har_path": action.get("suggested_workflow_har_path"),
        "required_capture_quality": action.get("required_capture_quality"),
        "capture_prepare_command": action.get("capture_prepare_command")
        or capture_prepare_command(action.get("capture_command")),
        "capture_command": action.get("capture_command"),
        "post_capture_check_command": action.get("post_capture_check_command"),
        "post_capture_check_script_path": action.get("post_capture_check_script_path"),
        "handoff_command": action.get("handoff_command"),
        "handoff_path": action.get("handoff_path"),
        "handoff_markdown_path": action.get("handoff_markdown_path"),
        "analysis_command": action.get("analysis_command"),
        "safe_to_run_automatically": action.get("safe_to_run_automatically"),
    }


def compact_tokenomics_write_blocker(blocker: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "type": blocker.get("type"),
        "property": blocker.get("property"),
        "reason": blocker.get("reason"),
        "statement_month": blocker.get("statement_month"),
        "statement_month_status": blocker.get("statement_month_status"),
        "latest_statement_date": blocker.get("latest_statement_date"),
        "latest_statement_filename": blocker.get("latest_statement_filename"),
        "payment_due_date": blocker.get("payment_due_date"),
        "payment_due_month": blocker.get("payment_due_month"),
        "archive_current_month_statement_available": blocker.get("archive_current_month_statement_available"),
        "remediation_source": blocker.get("remediation_source"),
        "remediation_reason": blocker.get("remediation_reason"),
        "remediation_action_available": blocker.get("remediation_action_available"),
        "safe_to_run_automatically": blocker.get("safe_to_run_automatically"),
    }
    remediation = compact_remediation_action(blocker.get("remediation_action"))
    if remediation:
        compact["remediation_action"] = remediation
    return compact


def tokenomics_write_blockers_by_property(tokenomics: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_property: dict[str, list[dict[str, Any]]] = {}
    for blocker in tokenomics_write_blockers(tokenomics):
        prop = str(blocker.get("property") or "").strip()
        if not prop:
            continue
        by_property.setdefault(normalize_property(prop), []).append(compact_tokenomics_write_blocker(blocker))
    return by_property


def should_queue_handoff(handoff: dict[str, Any]) -> bool:
    if str(handoff.get("status") or "").strip() == "ok":
        return False
    action = str(handoff.get("suggested_next_action") or "").strip()
    reason = str(handoff.get("reason") or "").strip()
    if handoff.get("capture_required_before_offline_next_step") is True:
        return True
    if action in CAPTURE_ACTIONS:
        return True
    if reason in EXISTING_HAR_BODY_REASONS or reason == "har_missing":
        return True
    return str(handoff.get("status") or "").strip() == "review"


def capture_prepare_command(command: object) -> str | None:
    text = str(command or "").strip()
    if not text:
        return None
    if "MORTGAGE_WORKFLOW_CAPTURE_PREPARE_ONLY=" in text or "CITADEL_CAPTURE_PREPARE_ONLY=" in text:
        return text
    if "scripts/citadel_manual_capture_har.sh" in text:
        return f"CITADEL_CAPTURE_PREPARE_ONLY=1 {text}"
    return f"MORTGAGE_WORKFLOW_CAPTURE_PREPARE_ONLY=1 {text}"


def clean_target_month(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text if TARGET_MONTH_PATTERN.fullmatch(text) else None


def command_with_target_month(command: object, target_month: str | None) -> str | None:
    text = str(command or "").strip()
    if not text:
        return None
    if not target_month:
        return text
    pattern = re.compile(r"(--target-month(?:=|\s+))(['\"]?)(20\d{2}-(?:0[1-9]|1[0-2]))\2")
    updated, count = pattern.subn(
        lambda match: f"{match.group(1)}{match.group(2)}{target_month}{match.group(2)}",
        text,
    )
    if count:
        return updated
    if "advance_mortgage_workflow_evidence.py" in text:
        return f"{text} --target-month {target_month}"
    return text


def capture_requirements_for_entry(requirements: object, target_month: str | None) -> list[str]:
    if not isinstance(requirements, list):
        return []
    normalized: list[str] = []
    saw_helper_instruction = False
    saw_manual_export_warning = False
    for item in requirements:
        text = str(item or "").strip()
        if not text:
            continue
        if target_month:
            text = YEAR_MONTH_TEXT_PATTERN.sub(target_month, text)
        if text == "Export a HAR with response bodies preserved.":
            text = (
                "Keep capture_command running while opening/downloading the statement; "
                "the CDP helper records Network.getResponseBody so binary PDF payloads are retained."
            )
        if "Network.getResponseBody" in text:
            saw_helper_instruction = True
        if "manual DevTools HAR export" in text:
            saw_manual_export_warning = True
        normalized.append(text)
    if saw_helper_instruction and not saw_manual_export_warning:
        normalized.append(
            "Do not rely on a manual DevTools HAR export for binary PDF statements; "
            "it can show HTTP 200 responses while omitting content.text."
        )
    return normalized


def manual_capture_command(
    property_name: str,
    portal_url: object,
    har_path: object,
    required_response_paths: list[str] | None = None,
    expected_document_ids: list[str] | None = None,
) -> str | None:
    portal = str(portal_url or "").strip()
    har = str(har_path or "").strip()
    if not property_name or not portal or not har:
        return None
    required_paths = unique_strings(required_response_paths or [])
    parts = [
        f"MORTGAGE_WORKFLOW_PROPERTY={shlex.quote(property_name)}",
        f"MORTGAGE_WORKFLOW_PORTAL_URL={shlex.quote(portal)}",
        f"MORTGAGE_WORKFLOW_HAR_PATH={shlex.quote(har)}",
    ]
    if required_paths:
        parts.append(f"MORTGAGE_WORKFLOW_REQUIRED_RESPONSE_PATHS={shlex.quote(','.join(required_paths))}")
    expected_ids = unique_strings(expected_document_ids or [])
    if expected_ids:
        parts.append(f"MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS={shlex.quote(','.join(expected_ids))}")
    parts.append("scripts/mortgage_manual_capture_har.sh")
    return " ".join(parts)


def live_cdp_downloader_command(property_name: str, target_month: str | None, downloader_id: str | None) -> str:
    parts = ["MORTGAGE_DOWNLOADER_PROFILE=live_cdp"]
    if target_month:
        parts.append(f"MORTGAGE_STATEMENT_TARGET_MONTH={shlex.quote(target_month)}")
        parts.append("MORTGAGE_DOWNLOADER_EXACT_TARGET_MONTH=1")
    if downloader_id:
        parts.append(f"MORTGAGE_DOWNLOADER_IDS={shlex.quote(downloader_id)}")
    if property_name:
        parts.append(f"MORTGAGE_WORKFLOW_PROPERTY={shlex.quote(property_name)}")
    parts.append("python3 scripts/run_mortgage_statement_downloaders.py")
    return " ".join(parts)


def live_cdp_profile_downloader_ids(registry: Path = DEFAULT_REGISTRY) -> set[str]:
    data = load_json(registry)
    ids: set[str] = set()
    downloaders = data.get("downloaders")
    if not isinstance(downloaders, list):
        return ids
    for entry in downloaders:
        if not isinstance(entry, dict):
            continue
        downloader_id = str(entry.get("id") or "").strip()
        profiles = entry.get("profiles")
        if downloader_id and isinstance(profiles, dict) and isinstance(profiles.get("live_cdp"), dict):
            ids.add(downloader_id)
    return ids


def should_emit_live_cdp_downloader_command(
    next_action: object,
    downloader_id: object = None,
    live_cdp_downloader_ids: set[str] | None = None,
) -> bool:
    if live_cdp_downloader_ids and str(downloader_id or "").strip() in live_cdp_downloader_ids:
        return True
    text = str(next_action or "").strip()
    lowered = text.lower()
    if text == "authenticate_visible_loandepot_tab_then_run_live_cdp" or "rerun_live_cdp" in text:
        return True
    rerun_requested = "rerun" in lowered or "rerunning" in lowered
    portal_or_credential_repair = any(
        token in lowered
        for token in (
            "bitwarden",
            "credential",
            "credentials",
            "visible portal",
            "visible browser",
            "portal reaches mfa",
        )
    )
    return rerun_requested and portal_or_credential_repair


def capture_command_with_expected_document_ids(command: object, expected_document_ids: list[str] | None) -> str | None:
    text = str(command or "").strip()
    if not text:
        return None
    ids = unique_strings(expected_document_ids or [])
    if not ids or "MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS=" in text:
        return text
    return f"MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS={shlex.quote(','.join(ids))} {text}"


def replace_capture_command_expected_document_ids(
    command: object,
    expected_document_ids: list[str] | None,
) -> str | None:
    text = str(command or "").strip()
    if not text:
        return None
    ids = unique_strings(expected_document_ids or [])
    if not ids or "scripts/mortgage_manual_capture_har.sh" not in text:
        return text
    replacement = f"MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS={shlex.quote(','.join(ids))}"
    if "MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS=" not in text:
        return f"{replacement} {text}"
    return re.sub(
        r"MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS=(?:'[^']*'|\"[^\"]*\"|\S+)",
        replacement,
        text,
        count=1,
    )


def expected_document_ids_from_capture_command(command: object) -> list[str]:
    text = str(command or "").strip()
    if not text:
        return []
    try:
        tokens = shlex.split(text)
    except ValueError:
        return []
    prefix = "MORTGAGE_WORKFLOW_EXPECTED_DOCUMENT_IDS="
    values: list[str] = []
    for token in tokens:
        if not token.startswith(prefix):
            continue
        raw = token[len(prefix) :]
        values.extend(part.strip() for part in raw.split(",") if part.strip())
    return unique_strings(values)


def required_response_paths_from_capture_command(command: object) -> list[str]:
    text = str(command or "").strip()
    if not text:
        return []
    try:
        tokens = shlex.split(text)
    except ValueError:
        return []
    prefix = "MORTGAGE_WORKFLOW_REQUIRED_RESPONSE_PATHS="
    values: list[str] = []
    for token in tokens:
        if not token.startswith(prefix):
            continue
        raw_value = token[len(prefix) :].strip()
        if raw_value:
            values.extend(part.strip() for part in raw_value.split(","))
    return unique_strings(values)


def command_text(command: list[str]) -> str:
    return " ".join(json.dumps(part) for part in command)


def post_capture_apply_command(
    *,
    property_name: str,
    target_month: str | None,
    process_existing_body_recaptures: bool,
) -> str | None:
    if not property_name:
        return None
    command = [
        sys.executable,
        str(DEFAULT_PROCESSOR_SCRIPT),
        "--queue",
        str(DEFAULT_REPORT),
        "--report",
        str(DEFAULT_PROCESSOR_REPORT),
        "--markdown",
        str(DEFAULT_PROCESSOR_MARKDOWN),
        "--report-dir",
        str(DEFAULT_REPORT_DIR),
        "--advance-script",
        str(WORKSPACE_ROOT / "scripts" / "advance_mortgage_workflow_evidence.py"),
        "--intake",
        str(DEFAULT_INTAKE),
        "--registry",
        str(DEFAULT_REGISTRY),
        "--coverage-report",
        str(DEFAULT_COVERAGE_REPORT),
        "--workspace-root",
        str(WORKSPACE_ROOT),
        "--property",
        property_name,
        "--timeout-seconds",
        "180",
    ]
    target = str(target_month or "").strip()
    if target:
        command.extend(["--target-month", target])
    command.append("--apply")
    if process_existing_body_recaptures:
        command.append("--process-existing-body-recaptures")
    command.append("--enable-verified-registry-entry")
    return command_text(command)


def required_response_paths_for_downloader_gap(gap: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    values: list[object] = []
    for source in (gap, summary):
        values.extend(source.get("missing_response_body_paths") or [])
        requirements = source.get("response_body_requirements")
        if isinstance(requirements, list):
            values.extend(item.get("path") for item in requirements if isinstance(item, dict))
    paths = unique_strings(values)
    servicer = str(gap.get("servicer") or summary.get("servicer") or "").strip().casefold()
    recapture_reason = str(gap.get("target_month_recapture_reason") or "").strip()
    reason = str(gap.get("reason") or "").strip()
    required_quality = str(gap.get("required_capture_quality") or "").strip()
    if (
        "loandepot" in servicer
        and required_quality == "target_month_statement_pdf"
        and (
            recapture_reason == "metadata_without_embedded_pdf_payload"
            or reason == "target_month_statement_pdf_payload_missing"
        )
    ):
        paths.append("/SEDMDownloadDocumentV1/api/DocumentDownload")
    if (
        (
            "mortgagequestions" in servicer
            or "onity" in servicer
            or "phh" in servicer
            or "ecmdocs" in str(gap.get("har_path") or summary.get("har_path") or "").casefold()
        )
        and required_quality == "target_month_statement_pdf"
        and (
            recapture_reason == "metadata_without_embedded_pdf_payload"
            or recapture_reason == "target_month_statement_pdf_payload_missing"
            or reason == "target_month_statement_pdf_payload_missing"
        )
    ):
        paths.append("/LISViewDoc.aspx?enc")
    return unique_strings(paths)


def queue_reason(handoff: dict[str, Any]) -> str:
    reason = str(handoff.get("reason") or "").strip()
    action = str(handoff.get("suggested_next_action") or "").strip()
    if reason:
        return reason
    if action:
        return action
    return "workflow_capture_required"


def classify_entry(handoff: dict[str, Any]) -> tuple[bool, bool, bool]:
    har_exists = handoff.get("suggested_workflow_har_path_exists") is True
    action = str(handoff.get("suggested_next_action") or "").strip()
    reason = str(handoff.get("reason") or "").strip()
    required_quality = str(handoff.get("required_capture_quality") or "").strip()
    missing_har = (not har_exists) or handoff.get("capture_required_before_offline_next_step") is True
    existing_har_needs_bodies = (
        har_exists
        and (
            action in {"capture_full_response_bodies", "recapture_workflow_har_with_full_response_bodies"}
            or reason in EXISTING_HAR_BODY_REASONS
        )
    )
    existing_har_needs_target_pdf = (
        har_exists
        and (
            action == "capture_target_month_statement"
            or reason == "embedded_statement_pdf_payload_missing"
            or required_quality == "target_month_statement_pdf"
        )
    )
    return missing_har, existing_har_needs_bodies, existing_har_needs_target_pdf


def compact_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": candidate.get("path"),
        "name": candidate.get("name"),
        "match_score": candidate.get("match_score"),
        "match_reasons": candidate.get("match_reasons"),
        "analysis_status": candidate.get("analysis_status"),
        "analysis_reason": candidate.get("analysis_reason"),
        "analysis_suggested_next_action": candidate.get("analysis_suggested_next_action"),
        "candidate_endpoint_count": candidate.get("candidate_endpoint_count"),
        "actionable_missing_response_body_count": candidate.get("actionable_missing_response_body_count"),
        "missing_response_body_paths": candidate.get("missing_response_body_paths"),
        "rejected_reason": candidate.get("rejected_reason"),
        "command_status": candidate.get("command_status"),
    }


def build_entry(
    handoff: dict[str, Any],
    rejected_by_property: dict[str, list[dict[str, Any]]],
    tokenomics_gap_set: set[str],
    tokenomics_blockers_by_property: dict[str, list[dict[str, Any]]],
    target_month: str | None = None,
) -> dict[str, Any]:
    prop = str(handoff.get("property") or "").strip()
    missing_har, existing_har_needs_bodies, existing_har_needs_target_pdf = classify_entry(handoff)
    rejected = rejected_by_property.get(normalize_property(prop), [])
    normalized_prop = normalize_property(prop)
    tokenomics_write_blockers_for_entry = tokenomics_blockers_by_property.get(normalized_prop, [])
    tokenomics_blocking = normalized_prop in tokenomics_gap_set or bool(tokenomics_write_blockers_for_entry)
    suggested_next_action = handoff.get("suggested_next_action")
    required_capture_quality = handoff.get("required_capture_quality") or "full_response_bodies"
    entry_target_month = target_month or clean_target_month(handoff.get("target_statement_month"))
    target_month_document_identifiers = handoff.get("target_month_document_identifiers")
    expected_document_ids = unique_strings(target_month_document_identifiers or [])
    capture_command = capture_command_with_expected_document_ids(
        handoff.get("capture_command"),
        expected_document_ids,
    )
    command_document_ids = expected_document_ids_from_capture_command(capture_command)
    if command_document_ids:
        target_month_document_identifiers = command_document_ids
        expected_document_ids = command_document_ids
    prepare_command = handoff.get("capture_prepare_command") or capture_prepare_command(capture_command)
    required_response_paths = unique_strings(
        [
            *(handoff.get("required_response_paths") or []),
            *required_response_paths_from_capture_command(capture_command),
        ]
    )
    advance_command = command_with_target_month(handoff.get("advance_workflow_evidence_command"), target_month)
    advance_apply_command = command_with_target_month(
        handoff.get("advance_workflow_evidence_apply_command"),
        target_month,
    )
    entry_reason = queue_reason(handoff)
    post_capture_apply = post_capture_apply_command(
        property_name=prop,
        target_month=entry_target_month,
        process_existing_body_recaptures=entry_reason in (EXISTING_HAR_BODY_REASONS | TARGET_MONTH_PDF_RECAPTURE_REASONS),
    )
    return {
        "property": prop,
        "servicer_hint": handoff.get("servicer_hint"),
        "portal_url": handoff.get("portal_url"),
        "status": handoff.get("status"),
        "reason": entry_reason,
        "type": suggested_next_action,
        "suggested_next_action": suggested_next_action,
        "target_statement_month": entry_target_month,
        "target_statement_dir": handoff.get("target_statement_dir"),
        "suggested_workflow_har_path": handoff.get("suggested_workflow_har_path"),
        "har_path": handoff.get("suggested_workflow_har_path"),
        "suggested_workflow_har_path_exists": handoff.get("suggested_workflow_har_path_exists"),
        "har_path_exists": handoff.get("suggested_workflow_har_path_exists"),
        "suggested_workflow_har_path_size": handoff.get("suggested_workflow_har_path_size"),
        "existing_workflow_har_path": handoff.get("existing_workflow_har_path"),
        "existing_workflow_har_path_exists": handoff.get("existing_workflow_har_path_exists"),
        "existing_workflow_har_path_size": handoff.get("existing_workflow_har_path_size"),
        "missing_har": missing_har,
        "existing_har_needs_response_bodies": existing_har_needs_bodies,
        "existing_har_needs_target_month_statement_pdf": existing_har_needs_target_pdf,
        "required_capture_quality": required_capture_quality,
        "required_response_paths": required_response_paths,
        "statement_document_candidate_count": handoff.get("statement_document_candidate_count"),
        "statement_document_metadata_only_count": handoff.get("statement_document_metadata_only_count"),
        "statement_document_payload_count": handoff.get("statement_document_payload_count"),
        "statement_document_metadata_only_months": handoff.get("statement_document_metadata_only_months"),
        "statement_document_payload_months": handoff.get("statement_document_payload_months"),
        "latest_statement_document_metadata_only_month": handoff.get("latest_statement_document_metadata_only_month"),
        "target_month_document_identifiers": target_month_document_identifiers,
        "expected_document_ids": expected_document_ids,
        "body_recapture_capture_method": handoff.get("body_recapture_capture_method")
        or default_body_recapture_value("body_recapture_capture_method", required_capture_quality),
        "body_recapture_capture_instruction": handoff.get("body_recapture_capture_instruction")
        or default_body_recapture_value("body_recapture_capture_instruction", required_capture_quality),
        "body_recapture_manual_har_export_warning": handoff.get("body_recapture_manual_har_export_warning")
        or default_body_recapture_value("body_recapture_manual_har_export_warning", required_capture_quality),
        "target_month_statement_candidates": handoff.get("target_month_statement_candidates"),
        "capture_required_before_offline_next_step": handoff.get("capture_required_before_offline_next_step"),
        "capture_requirements": capture_requirements_for_entry(
            handoff.get("capture_requirements"),
            entry_target_month,
        ),
        "tokenomics_blocking": tokenomics_blocking,
        "tokenomics_statement_gap_blocking": tokenomics_blocking,
        "workbook_write_blocking": tokenomics_blocking,
        "tokenomics_workbook_write_blocking": tokenomics_blocking,
        "tokenomics_capture_priority": "blocking_workbook_write" if tokenomics_blocking else "automation_repair_only",
        "tokenomics_write_blocker_count": len(tokenomics_write_blockers_for_entry),
        "tokenomics_write_blocker_reasons": unique_strings(
            [item.get("reason") for item in tokenomics_write_blockers_for_entry]
        ),
        "tokenomics_write_blocker_statement_month_statuses": unique_strings(
            [item.get("statement_month_status") for item in tokenomics_write_blockers_for_entry]
        ),
        "tokenomics_write_blockers": tokenomics_write_blockers_for_entry,
        "handoff_path": handoff.get("handoff_path"),
        "handoff_markdown_path": handoff.get("handoff_markdown_path"),
        "post_capture_check_script_path": handoff.get("post_capture_check_script_path"),
        "capture_prepare_command": prepare_command,
        "capture_command": capture_command,
        "capture_command_ready_to_run_now": handoff.get("capture_command_ready_to_run_now"),
        "capture_command_safe_to_run_automatically": handoff.get("capture_command_safe_to_run_automatically"),
        "post_capture_check_command": handoff.get("post_capture_check_command"),
        "validation_command": handoff.get("post_capture_check_command"),
        "post_capture_apply_command": post_capture_apply,
        "post_capture_apply_command_safe_after_har_capture": bool(post_capture_apply),
        "post_capture_check_ready_to_run_now": handoff.get("post_capture_check_ready_to_run_now"),
        "register_workflow_evidence_command": handoff.get("register_workflow_evidence_command"),
        "register_workflow_evidence_apply_command": handoff.get("register_workflow_evidence_apply_command"),
        "register_workflow_evidence_ready_to_run_now": handoff.get("register_workflow_evidence_ready_to_run_now"),
        "advance_workflow_evidence_command": advance_command,
        "advance_command": advance_command,
        "advance_workflow_evidence_apply_command": advance_apply_command,
        "advance_apply_command": advance_apply_command,
        "advance_workflow_evidence_ready_to_run_now": handoff.get("advance_workflow_evidence_ready_to_run_now"),
        **workflow_precondition_fields(
            missing_har=missing_har,
            override_status=str(handoff.get("command_precondition_status") or "").strip() or None,
            override_reason=str(handoff.get("command_precondition_reason") or "").strip() or None,
            override_next_action=str(handoff.get("command_precondition_next_action") or "").strip() or None,
        ),
        "safe_to_run_automatically": False,
        "rejected_discovered_har_candidates": [compact_candidate(item) for item in rejected],
        "rejected_discovered_har_candidate_paths": unique_strings([item.get("path") for item in rejected]),
    }


def downloader_gap_entry(
    gap: dict[str, Any],
    summary_by_id: dict[str, dict[str, Any]],
    intake_by_property: dict[str, dict[str, Any]],
    tokenomics_gap_set: set[str],
    tokenomics_blockers_by_property: dict[str, list[dict[str, Any]]],
    target_month: str | None = None,
    live_cdp_downloader_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    prop = str(gap.get("property") or "").strip()
    if not prop:
        return None
    report_status = str(gap.get("report_status") or "").strip()
    effective_status = str(gap.get("effective_status") or gap.get("status") or "").strip()
    target_month_recapture_required = gap.get("target_month_recapture_required") is True
    required_quality = str(gap.get("required_capture_quality") or "").strip()
    if (
        report_status != "target_month_missing"
        and effective_status != "target_month_missing"
        and not target_month_recapture_required
    ):
        return None
    reason = str(gap.get("reason") or "").strip()
    recapture_reason = str(gap.get("target_month_recapture_reason") or "").strip()
    if reason not in DOWNLOADER_GAP_REASONS and recapture_reason not in TARGET_MONTH_PDF_RECAPTURE_REASONS:
        return None
    summary = summary_by_id.get(str(gap.get("id") or "").strip()) or {}

    normalized_prop = normalize_property(prop)
    intake = intake_by_property.get(normalized_prop, {})
    har_path = str(summary.get("har_path") or gap.get("har_path") or intake_workflow_har_path(intake) or "").strip()
    if not har_path:
        return None
    entry_target_month = target_month or clean_target_month(gap.get("expected_target_month")) or clean_target_month(
        gap.get("target_month")
    )
    tokenomics_write_blockers_for_entry = tokenomics_blockers_by_property.get(normalized_prop, [])
    tokenomics_blocking = normalized_prop in tokenomics_gap_set or bool(tokenomics_write_blockers_for_entry)
    required_response_paths = required_response_paths_for_downloader_gap(gap, summary)
    if required_quality == "full_response_bodies":
        suggested_action = "capture_workflow_har_with_full_response_bodies"
        entry_reason = canonical_downloader_gap_reason(reason, recapture_reason)
    else:
        required_quality = "target_month_statement_pdf"
        suggested_action = "capture_target_month_statement"
        entry_reason = canonical_downloader_gap_reason(reason, recapture_reason)
    target_month_document_identifiers = (
        gap.get("target_month_document_identifiers")
        or summary.get("target_month_document_identifiers")
    )
    expected_document_ids = unique_strings(target_month_document_identifiers or [])
    capture_command = manual_capture_command(
        prop,
        intake.get("portal_url"),
        har_path,
        required_response_paths,
        expected_document_ids,
    )
    command_document_ids = expected_document_ids_from_capture_command(capture_command)
    if command_document_ids:
        target_month_document_identifiers = command_document_ids
        expected_document_ids = command_document_ids
    prepare_command = capture_prepare_command(capture_command)
    post_capture_apply = post_capture_apply_command(
        property_name=prop,
        target_month=entry_target_month,
        process_existing_body_recaptures=entry_reason in (EXISTING_HAR_BODY_REASONS | TARGET_MONTH_PDF_RECAPTURE_REASONS),
    )
    target_month_statement_candidates = (
        gap.get("target_month_statement_candidates")
        or summary.get("target_month_statement_candidates")
    )
    auth_context = {
        key: gap.get(key) if present_context_value(key, gap.get(key)) else summary.get(key)
        for key in DOWNLOADER_AUTH_CONTEXT_FIELDS
    }
    auth_blocker_reason = capture_auth_blocker_reason(auth_context)
    if required_quality == "target_month_statement_pdf" and not auth_blocker_blocks_target_pdf_capture(
        auth_blocker_reason,
        auth_context,
    ):
        auth_blocker_reason = None
    source_suggested_next_action = gap.get("suggested_next_action") or summary.get("suggested_next_action")
    source_operator_next_action = gap.get("operator_next_action") or summary.get("operator_next_action")
    live_cdp_next_action = source_operator_next_action or source_suggested_next_action
    live_cdp_command = None
    source_downloader_id = str(gap.get("id") or summary.get("id") or "").strip() or None
    if should_emit_live_cdp_downloader_command(
        live_cdp_next_action,
        source_downloader_id,
        live_cdp_downloader_ids,
    ):
        live_cdp_command = live_cdp_downloader_command(
            prop,
            entry_target_month,
            source_downloader_id,
        )
    har = Path(har_path)
    entry = {
        "property": prop,
        "servicer_hint": gap.get("servicer") or summary.get("servicer") or intake.get("servicer_hint"),
        "portal_url": intake.get("portal_url"),
        "status": "review",
        "reason": entry_reason,
        "type": suggested_action,
        "suggested_next_action": suggested_action,
        "next_action": suggested_action,
        "target_statement_month": entry_target_month,
        "target_statement_dir": intake.get("target_statement_dir"),
        "suggested_workflow_har_path": har_path,
        "har_path": har_path,
        "suggested_workflow_har_path_exists": har.exists(),
        "har_path_exists": har.exists(),
        "suggested_workflow_har_path_size": har.stat().st_size if har.exists() else None,
        "missing_har": not har.exists(),
        "existing_har_needs_response_bodies": har.exists() and required_quality == "full_response_bodies",
        "existing_har_needs_target_month_statement_pdf": har.exists() and required_quality == "target_month_statement_pdf",
        "required_capture_quality": required_quality,
        "statement_document_candidate_count": summary.get("candidate_count"),
        "statement_document_metadata_only_count": None,
        "statement_document_payload_count": len(summary.get("downloadable_statement_months") or []),
        "statement_document_metadata_only_months": summary.get("metadata_only_statement_months"),
        "statement_document_payload_months": summary.get("downloadable_statement_months"),
        "latest_statement_document_metadata_only_month": None,
        "latest_statement_month": summary.get("latest_statement_month"),
        "latest_downloadable_statement_month": summary.get("latest_downloadable_statement_month"),
        "downloadable_statement_months": summary.get("downloadable_statement_months"),
        "available_statement_months": summary.get("available_statement_months"),
        "target_month_candidate_count": summary.get("target_month_candidate_count"),
        "target_month_document_identifiers": target_month_document_identifiers,
        "expected_document_ids": expected_document_ids,
        "body_recapture_capture_method": gap.get("body_recapture_capture_method")
        or summary.get("body_recapture_capture_method")
        or default_body_recapture_value("body_recapture_capture_method", required_quality),
        "body_recapture_capture_instruction": gap.get("body_recapture_capture_instruction")
        or summary.get("body_recapture_capture_instruction")
        or default_body_recapture_value("body_recapture_capture_instruction", required_quality),
        "body_recapture_manual_har_export_warning": gap.get("body_recapture_manual_har_export_warning")
        or summary.get("body_recapture_manual_har_export_warning")
        or default_body_recapture_value("body_recapture_manual_har_export_warning", required_quality),
        "target_month_statement_candidates": target_month_statement_candidates,
        "target_month_downloadable_count": summary.get("target_month_downloadable_count"),
        "capture_required_before_offline_next_step": True,
        "required_response_paths": required_response_paths,
        "capture_requirements": [
            "Use the visible browser session, not a headless browser.",
            "Navigate to the target-month statement and save a HAR with full response bodies.",
            f"Target statement month: {entry_target_month or 'unknown'}",
        ],
        "source": "mortgage_statement_downloader_gap",
        "source_downloader_id": gap.get("id"),
        "source_downloader_status": gap.get("effective_status") or gap.get("status"),
        "source_downloader_report_status": gap.get("report_status"),
        "source_downloader_runtime_status": gap.get("runtime_status"),
        "source_downloader_manual_auth_required": gap.get("manual_auth_required"),
        "source_downloader_suggested_next_action": source_suggested_next_action,
        "source_downloader_operator_next_action": source_operator_next_action,
        "live_cdp_downloader_command": live_cdp_command,
        "live_cdp_downloader_ready_after_visible_auth": bool(live_cdp_command),
        **auth_context,
        "capture_auth_blocker": bool(auth_blocker_reason),
        "capture_auth_blocker_reason": auth_blocker_reason,
        "capture_auth_next_action": auth_blocker_next_action(auth_blocker_reason) if auth_blocker_reason else None,
        "target_month_recapture_required": target_month_recapture_required,
        "target_month_recapture_reason": recapture_reason or None,
        "tokenomics_blocking": tokenomics_blocking,
        "tokenomics_statement_gap_blocking": tokenomics_blocking,
        "workbook_write_blocking": tokenomics_blocking,
        "tokenomics_workbook_write_blocking": tokenomics_blocking,
        "tokenomics_capture_priority": "blocking_workbook_write" if tokenomics_blocking else "automation_repair_only",
        "tokenomics_write_blocker_count": len(tokenomics_write_blockers_for_entry),
        "tokenomics_write_blocker_reasons": unique_strings(
            [item.get("reason") for item in tokenomics_write_blockers_for_entry]
        ),
        "tokenomics_write_blocker_statement_month_statuses": unique_strings(
            [item.get("statement_month_status") for item in tokenomics_write_blockers_for_entry]
        ),
        "tokenomics_write_blockers": tokenomics_write_blockers_for_entry,
        "handoff_path": None,
        "handoff_markdown_path": None,
        "post_capture_check_script_path": None,
        "capture_prepare_command": prepare_command,
        "capture_command": capture_command,
        "capture_command_ready_to_run_now": bool(capture_command) and not auth_blocker_reason,
        "capture_command_blocked_reason": auth_blocker_reason,
        "capture_command_safe_to_run_automatically": False,
        "post_capture_check_command": None,
        "validation_command": None,
        "post_capture_apply_command": post_capture_apply,
        "post_capture_apply_command_safe_after_har_capture": bool(post_capture_apply),
        "post_capture_check_ready_to_run_now": False,
        "register_workflow_evidence_command": None,
        "register_workflow_evidence_apply_command": None,
        "register_workflow_evidence_ready_to_run_now": False,
        "advance_workflow_evidence_command": None,
        "advance_command": None,
        "advance_workflow_evidence_apply_command": None,
        "advance_apply_command": None,
        "advance_workflow_evidence_ready_to_run_now": False,
        **workflow_precondition_fields(missing_har=not har.exists(), auth_blocker_reason=auth_blocker_reason),
        "safe_to_run_automatically": False,
        "rejected_discovered_har_candidates": [],
        "rejected_discovered_har_candidate_paths": [],
    }
    if auth_blocker_reason:
        entry = apply_capture_auth_blocker(entry, auth_blocker_reason)
    return entry


def downloader_gap_entries(
    downloader_report: dict[str, Any],
    intake_by_property: dict[str, dict[str, Any]],
    tokenomics_gap_set: set[str],
    tokenomics_blockers_by_property: dict[str, list[dict[str, Any]]],
    target_month: str | None,
    live_cdp_downloader_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    gaps = downloader_report.get("target_month_statement_gaps")
    summaries = downloader_report.get("downloader_summaries")
    if not isinstance(gaps, list) or not isinstance(summaries, list):
        return []
    summary_by_id = {
        str(item.get("id") or "").strip(): item
        for item in summaries
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    entries: list[dict[str, Any]] = []
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        entry = downloader_gap_entry(
            gap,
            summary_by_id,
            intake_by_property,
            tokenomics_gap_set,
            tokenomics_blockers_by_property,
            target_month,
            live_cdp_downloader_ids,
        )
        if entry:
            entries.append(entry)
    return entries


def should_queue_coverage_gap(record: dict[str, Any]) -> bool:
    coverage_status = str(record.get("coverage_status") or "").strip()
    operational_status = str(record.get("operational_status") or "").strip()
    if coverage_status not in {"missing_downloader", "missing_workflow_evidence"} and operational_status not in {
        "missing_downloader",
        "missing_workflow_evidence",
    }:
        return False
    if record.get("capture_required_before_offline_next_step") is True:
        return True
    return bool(str(record.get("capture_command") or "").strip())


def existing_workflow_har_evidence(record: dict[str, Any]) -> tuple[str | None, int | None]:
    workflow_evidence = record.get("workflow_evidence")
    if not isinstance(workflow_evidence, list):
        return None, None
    for value in reversed(workflow_evidence):
        path_text = str(value or "").strip()
        if not path_text:
            continue
        path = Path(path_text)
        if path.exists():
            return path_text, path.stat().st_size
    return None, None


def coverage_gap_entries(
    coverage_report: dict[str, Any],
    rejected_by_property: dict[str, list[dict[str, Any]]],
    tokenomics_gap_set: set[str],
    tokenomics_blockers_by_property: dict[str, list[dict[str, Any]]],
    target_month: str | None,
) -> list[dict[str, Any]]:
    records = coverage_report.get("coverage_records")
    if not isinstance(records, list):
        return []
    entries: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or not should_queue_coverage_gap(record):
            continue
        handoff = dict(record)
        handoff["status"] = handoff.get("status") or "review"
        handoff["reason"] = (
            handoff.get("reason")
            or handoff.get("missing_reason")
            or handoff.get("coverage_status")
            or handoff.get("operational_status")
        )
        handoff["suggested_next_action"] = (
            handoff.get("suggested_next_action")
            or (
                "capture_target_month_statement"
                if str(handoff.get("required_capture_quality") or "").strip() == "target_month_statement_pdf"
                else "capture_workflow_har_with_full_response_bodies"
            )
        )
        existing_har_path, existing_har_size = existing_workflow_har_evidence(record)
        suggested_har_exists = record.get("suggested_workflow_har_path_exists") is True
        if existing_har_path and not suggested_har_exists:
            handoff["existing_workflow_har_path"] = existing_har_path
            handoff["existing_workflow_har_path_exists"] = True
            handoff["existing_workflow_har_path_size"] = existing_har_size
            handoff["command_precondition_status"] = "target_month_workflow_har_capture_required"
            handoff["command_precondition_reason"] = "existing_workflow_har_lacks_target_month"
            handoff["command_precondition_next_action"] = (
                "Capture a fresh target-month workflow HAR at the suggested property-specific path; "
                "prior HAR evidence exists but does not satisfy the current month."
            )
        entry = build_entry(
            handoff,
            rejected_by_property,
            tokenomics_gap_set,
            tokenomics_blockers_by_property,
            target_month,
        )
        entry["source"] = "mortgage_downloader_coverage_gap"
        entry["source_coverage_status"] = record.get("coverage_status")
        entry["source_operational_status"] = record.get("operational_status")
        entry["source_missing_reason"] = record.get("missing_reason")
        entries.append(entry)
    return entries


def downloader_context_by_property(downloader_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    context: dict[str, dict[str, Any]] = {}
    summaries = downloader_report.get("downloader_summaries")
    if isinstance(summaries, list):
        for summary in summaries:
            if not isinstance(summary, dict):
                continue
            prop = str(summary.get("property") or "").strip()
            if not prop:
                continue
            context.setdefault(normalize_property(prop), {}).update(
                source_downloader_id=summary.get("id"),
                source_downloader_target_month=summary.get("target_month") or summary.get("expected_target_month"),
                target_month_document_identifiers=summary.get("target_month_document_identifiers"),
                target_month_statement_candidates=summary.get("target_month_statement_candidates"),
                target_month_candidate_count=summary.get("target_month_candidate_count"),
                target_month_downloadable_count=summary.get("target_month_downloadable_count"),
                available_statement_months=summary.get("available_statement_months"),
                downloadable_statement_months=summary.get("downloadable_statement_months"),
                latest_statement_month=summary.get("latest_statement_month"),
                latest_downloadable_statement_month=summary.get("latest_downloadable_statement_month"),
                **{key: summary.get(key) for key in DOWNLOADER_AUTH_CONTEXT_FIELDS},
            )
    gaps = downloader_report.get("target_month_statement_gaps")
    if isinstance(gaps, list):
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            prop = str(gap.get("property") or "").strip()
            if not prop:
                continue
            context.setdefault(normalize_property(prop), {}).update(
                source_downloader_id=gap.get("id")
                or context.get(normalize_property(prop), {}).get("source_downloader_id"),
                source_downloader_target_month=gap.get("target_month")
                or gap.get("expected_target_month")
                or context.get(normalize_property(prop), {}).get("source_downloader_target_month"),
                target_month_document_identifiers=gap.get("target_month_document_identifiers")
                or context.get(normalize_property(prop), {}).get("target_month_document_identifiers"),
                target_month_statement_candidates=gap.get("target_month_statement_candidates")
                or context.get(normalize_property(prop), {}).get("target_month_statement_candidates"),
                downloader_gap_reason=gap.get("reason"),
                downloader_gap_required_capture_quality=gap.get("required_capture_quality"),
                downloader_gap_suggested_next_action=gap.get("suggested_next_action"),
                source_downloader_suggested_next_action=gap.get("suggested_next_action"),
                source_downloader_operator_next_action=gap.get("operator_next_action"),
                **{
                    key: gap.get(key)
                    if present_context_value(key, gap.get(key))
                    else context.get(normalize_property(prop), {}).get(key)
                    for key in DOWNLOADER_AUTH_CONTEXT_FIELDS
                },
            )
    return context


def coverage_context_by_property(coverage_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    context: dict[str, dict[str, Any]] = {}
    for section in ("downloader_target_month_statement_gaps", "next_action_plan"):
        items = coverage_report.get(section)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            prop = str(item.get("property") or "").strip()
            if not prop:
                continue
            key = normalize_property(prop)
            target = context.setdefault(key, {})
            target.update(
                source_downloader_id=item.get("downloader_id") or item.get("id") or target.get("source_downloader_id"),
                source_downloader_target_month=item.get("target_month")
                or item.get("expected_target_month")
                or target.get("source_downloader_target_month"),
                target_month_document_identifiers=item.get("target_month_document_identifiers")
                or target.get("target_month_document_identifiers"),
                target_month_statement_candidates=item.get("target_month_statement_candidates")
                or target.get("target_month_statement_candidates"),
                target_month_candidate_count=item.get("target_month_candidate_count")
                or target.get("target_month_candidate_count"),
                target_month_downloadable_count=item.get("target_month_downloadable_count")
                if item.get("target_month_downloadable_count") is not None
                else target.get("target_month_downloadable_count"),
                downloader_gap_reason=item.get("reason") or target.get("downloader_gap_reason"),
                downloader_gap_required_capture_quality=item.get("required_capture_quality")
                or target.get("downloader_gap_required_capture_quality"),
                downloader_gap_suggested_next_action=item.get("suggested_next_action")
                or target.get("downloader_gap_suggested_next_action"),
                source_downloader_suggested_next_action=item.get("suggested_next_action")
                or target.get("source_downloader_suggested_next_action"),
                source_downloader_operator_next_action=item.get("latest_report_operator_next_action")
                or item.get("operator_next_action")
                or target.get("source_downloader_operator_next_action"),
                post_capture_check_command=item.get("post_capture_check_command")
                or target.get("post_capture_check_command"),
                post_capture_check_script_path=item.get("post_capture_check_script_path")
                or target.get("post_capture_check_script_path"),
                post_capture_check_ready_to_run_now=item.get("post_capture_check_ready_to_run_now")
                if item.get("post_capture_check_ready_to_run_now") is not None
                else target.get("post_capture_check_ready_to_run_now"),
                post_capture_check_safe_to_run_after_har_capture=item.get(
                    "post_capture_check_safe_to_run_after_har_capture"
                )
                if item.get("post_capture_check_safe_to_run_after_har_capture") is not None
                else target.get("post_capture_check_safe_to_run_after_har_capture"),
            )
            for field in DOWNLOADER_AUTH_CONTEXT_FIELDS:
                value = item.get(field)
                if present_context_value(field, value):
                    target[field] = value
            context[key] = normalize_downloader_context_availability(target)
    return context


def explicit_pdf_available_candidate_count(candidates: Any) -> int | None:
    if not isinstance(candidates, list):
        return None
    saw_explicit_pdf_state = False
    available_count = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if "pdf_available" in candidate:
            saw_explicit_pdf_state = True
            if candidate.get("pdf_available") is True:
                available_count += 1
        elif candidate.get("pdf_payload_status") in {
            "downloaded_or_existing_pdf",
            "live_cdp_download_failed",
            "metadata_only",
            "metadata_without_embedded_pdf_payload",
            "download_response_body_omitted_from_har",
            "download_response_without_pdf_payload",
        }:
            saw_explicit_pdf_state = True
            if candidate.get("pdf_payload_status") == "downloaded_or_existing_pdf":
                available_count += 1
    return available_count if saw_explicit_pdf_state else None


def candidate_payload_diagnostic_score(candidates: Any) -> int:
    if not isinstance(candidates, list):
        return 0
    score = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        status = candidate.get("pdf_payload_status")
        if status == "downloaded_or_existing_pdf" or candidate.get("pdf_available") is True:
            score = max(score, 50)
        elif status == "download_response_body_omitted_from_har":
            score = max(score, 40)
        elif status == "metadata_without_embedded_pdf_payload":
            score = max(score, 30)
        elif status == "metadata_only":
            score = max(score, 20)
        elif status == "live_cdp_download_failed":
            score = max(score, 10)
    return score


def recapture_reason_from_candidates(candidates: Any) -> str | None:
    if not isinstance(candidates, list):
        return None
    statuses = {
        candidate.get("pdf_payload_status")
        for candidate in candidates
        if isinstance(candidate, dict)
    }
    if "html_viewer_shell" in statuses:
        return "html_viewer_shell_without_pdf_payload"
    if "download_response_body_omitted_from_har" in statuses:
        return "download_response_body_omitted_from_har"
    if "metadata_without_embedded_pdf_payload" in statuses or "metadata_only" in statuses:
        return "metadata_without_embedded_pdf_payload"
    return None


def normalize_downloader_context_availability(context: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(context)
    if normalized.get("credential_login_hint_mismatch") in (None, "", []):
        if any(
            str(normalized.get(field) or "").strip() == "credential_login_hint_mismatch"
            for field in ("auth_issue", "manual_auth_reason", "auto_login_blocked_reason")
        ) or str(normalized.get("auto_login_status") or "").strip() == "blocked_credential_login_hint_mismatch":
            normalized["credential_login_hint_mismatch"] = True
    available_count = explicit_pdf_available_candidate_count(normalized.get("target_month_statement_candidates"))
    if available_count is not None:
        current_count = normalized.get("target_month_downloadable_count")
        try:
            current_count_int = int(current_count)
        except (TypeError, ValueError):
            current_count_int = None
        if current_count_int is None or current_count_int > available_count:
            normalized["target_month_downloadable_count"] = available_count
    return normalized


def merge_downloader_contexts(*contexts: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    last_index = len(contexts) - 1
    for context_index, context in enumerate(contexts):
        if not isinstance(context, dict):
            continue
        auth_priority = 20 if 0 < context_index < last_index else 10
        if context_index == last_index and context_index > 0:
            auth_priority = 5
        for prop, values in context.items():
            if not isinstance(values, dict):
                continue
            target = merged.setdefault(prop, {})
            for key, value in values.items():
                if present_context_value(key, value):
                    if key in DOWNLOADER_AUTH_CONTEXT_FIELDS:
                        current_priority = int(target.get("_auth_context_priority") or 0)
                        if (
                            key == "credential_lookup_failure_reason"
                            and credential_lookup_failure_score(value)
                            > credential_lookup_failure_score(target.get(key))
                        ):
                            current_priority = auth_priority
                        if key in {"auth_issue", "manual_auth_reason"} and auth_blocker_score(
                            target.get(key)
                        ) > auth_blocker_score(value):
                            continue
                        if target.get(key) not in (None, [], "") and current_priority > auth_priority:
                            continue
                        target["_auth_context_priority"] = auth_priority
                    if (
                        key == "target_month_statement_candidates"
                        and candidate_payload_diagnostic_score(target.get(key))
                        > candidate_payload_diagnostic_score(value)
                    ):
                        continue
                    target[key] = value
            normalized = normalize_downloader_context_availability(target)
            if "_auth_context_priority" in target:
                normalized["_auth_context_priority"] = target["_auth_context_priority"]
            merged[prop] = normalized
    return merged


def credential_lookup_failure_score(value: object) -> int:
    reason = str(value or "").strip()
    if not reason:
        return 0
    return {
        "lookup_timeout": 10,
        "helper_failed_without_stderr": 15,
        "helper_failed": 20,
        "session_unavailable": 25,
        "session_locked_or_unavailable": 25,
        "bw_cli_missing": 30,
        "jq_missing": 30,
        "item_not_found": 40,
        "expected_metadata_mismatch": 45,
    }.get(reason, 20)


def auth_blocker_score(value: object) -> int:
    reason = str(value or "").strip()
    if not reason:
        return 0
    return {
        "not_authenticated": 10,
        "visible_loandepot_tab_not_authenticated": 10,
        "login_required": 10,
        "authenticated_browser_required": 10,
        "loandepot_credentials_unavailable": 30,
        "credentials_unavailable": 30,
        "login_still_required_after_submit": 35,
        "credential_login_hint_mismatch": 38,
        "loandepot_api_auth_failed": 40,
    }.get(reason, 20)


def enrich_entry_with_downloader_context(
    entry: dict[str, Any],
    context_by_property: dict[str, dict[str, Any]],
    live_cdp_downloader_ids: set[str] | None = None,
) -> dict[str, Any]:
    context = normalize_downloader_context_availability(context_by_property.get(normalize_property(entry.get("property")), {}))
    if not context:
        return entry
    enriched = dict(entry)
    context_auth_blocker_reason = capture_auth_blocker_reason({key: context.get(key) for key in DOWNLOADER_AUTH_CONTEXT_FIELDS})
    auth_context_has_blocker = bool(context_auth_blocker_reason)
    if context_auth_blocker_reason:
        enriched["capture_auth_blocker_reason"] = context_auth_blocker_reason
    for key in (
        "target_month_document_identifiers",
        "target_month_statement_candidates",
        "target_month_candidate_count",
        "target_month_downloadable_count",
        "available_statement_months",
        "downloadable_statement_months",
        "latest_statement_month",
        "latest_downloadable_statement_month",
        "source_downloader_id",
        "source_downloader_suggested_next_action",
        "source_downloader_operator_next_action",
        "post_capture_check_command",
        "post_capture_check_script_path",
        "post_capture_check_ready_to_run_now",
        "post_capture_check_safe_to_run_after_har_capture",
        *DOWNLOADER_AUTH_CONTEXT_FIELDS,
    ):
        if (
            auth_context_has_blocker
            and key in {
                *DOWNLOADER_AUTH_CONTEXT_FIELDS,
                "source_downloader_suggested_next_action",
                "source_downloader_operator_next_action",
            }
            and present_context_value(key, context.get(key))
        ):
            enriched[key] = context.get(key)
        elif (
            key
            in {
                "post_capture_check_ready_to_run_now",
                "post_capture_check_safe_to_run_after_har_capture",
            }
            and context.get(key) is True
        ):
            enriched[key] = True
        elif enriched.get(key) in (None, [], "") and present_context_value(key, context.get(key)):
            enriched[key] = context.get(key)
    if enriched.get("post_capture_check_command") and not enriched.get("validation_command"):
        enriched["validation_command"] = enriched.get("post_capture_check_command")
    context_target_month = str(context.get("source_downloader_target_month") or "").strip()
    entry_target_month = str(enriched.get("target_statement_month") or "").strip()
    if context_target_month and entry_target_month == context_target_month:
        context_ids = unique_strings(context.get("target_month_document_identifiers") or [])
        if context_ids:
            enriched["target_month_document_identifiers"] = context_ids
            enriched["expected_document_ids"] = context_ids
            enriched["capture_command"] = replace_capture_command_expected_document_ids(
                enriched.get("capture_command"),
                context_ids,
            )
            enriched["capture_prepare_command"] = replace_capture_command_expected_document_ids(
                enriched.get("capture_prepare_command"),
                context_ids,
            )
        for key in (
            "target_month_statement_candidates",
            "target_month_recapture_reason",
            "target_month_recapture_required",
            "target_month_candidate_count",
            "target_month_downloadable_count",
        ):
            if context.get(key) not in (None, [], ""):
                enriched[key] = context.get(key)
    live_cdp_next_action = enriched.get("source_downloader_operator_next_action") or enriched.get(
        "source_downloader_suggested_next_action"
    )
    if (
        not enriched.get("live_cdp_downloader_command")
        and should_emit_live_cdp_downloader_command(
            live_cdp_next_action,
            enriched.get("source_downloader_id"),
            live_cdp_downloader_ids,
        )
    ):
        enriched["live_cdp_downloader_command"] = live_cdp_downloader_command(
            str(enriched.get("property") or "").strip(),
            str(enriched.get("target_statement_month") or "").strip() or None,
            str(enriched.get("source_downloader_id") or "").strip() or None,
        )
        enriched["live_cdp_downloader_ready_after_visible_auth"] = True
    auth_context = {key: enriched.get(key) for key in DOWNLOADER_AUTH_CONTEXT_FIELDS}
    auth_blocker_reason = capture_auth_blocker_reason(auth_context)
    if str(enriched.get("required_capture_quality") or "").strip() == "target_month_statement_pdf" and not auth_blocker_blocks_target_pdf_capture(
        auth_blocker_reason,
        auth_context,
    ):
        auth_blocker_reason = None
    if auth_blocker_reason:
        enriched = apply_capture_auth_blocker(enriched, auth_blocker_reason)
    if not present(enriched.get("target_month_recapture_reason")):
        derived_recapture_reason = recapture_reason_from_candidates(enriched.get("target_month_statement_candidates"))
        if derived_recapture_reason:
            enriched["target_month_recapture_reason"] = derived_recapture_reason
            enriched["target_month_recapture_required"] = True
    return enriched


def entry_identity(entry: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        normalize_property(entry.get("property")),
        str(entry.get("suggested_next_action") or "").strip(),
        str(entry.get("reason") or "").strip(),
        str(entry.get("suggested_workflow_har_path") or "").strip(),
        str(entry.get("target_statement_month") or "").strip(),
    )


def identity_label(identity: tuple[str, str, str, str, str]) -> str:
    prop, action, reason, har_path, month = identity
    return "|".join([prop, action, reason, har_path, month])


def entry_work_unit_key(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    prop, action, reason, har_path, _month = entry_identity(entry)
    return prop, action, reason, har_path


def present(value: Any) -> bool:
    return value not in (None, "", [])


def present_context_value(key: str, value: Any) -> bool:
    if key == "credential_lookup_candidate_items":
        return value not in (None, "")
    return present(value)


def merge_duplicate_entry(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    live_cdp_downloader_ids: set[str] | None = None,
) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if not present(merged.get(key)) and present(value):
            merged[key] = value

    required_response_paths = unique_strings(
        [
            *(merged.get("required_response_paths") or []),
            *(incoming.get("required_response_paths") or []),
            *required_response_paths_from_capture_command(merged.get("capture_command")),
            *required_response_paths_from_capture_command(incoming.get("capture_command")),
        ]
    )
    if required_response_paths:
        merged["required_response_paths"] = required_response_paths
        incoming_command = str(incoming.get("capture_command") or "")
        if "MORTGAGE_WORKFLOW_REQUIRED_RESPONSE_PATHS=" in incoming_command:
            merged["capture_command"] = incoming.get("capture_command")
            merged["capture_prepare_command"] = incoming.get("capture_prepare_command") or capture_prepare_command(
                incoming.get("capture_command")
            )

    existing_months = [
        merged.get("target_statement_month"),
        *(merged.get("duplicate_target_statement_months") or []),
        incoming.get("target_statement_month"),
    ]
    months = unique_strings(existing_months)
    if months:
        merged["duplicate_target_statement_months"] = months

    # Downloader gaps are derived from the statement downloader's target-month
    # evidence, while handoff rows may carry the broader workflow/payment month.
    if incoming.get("source") == "mortgage_statement_downloader_gap" and present(
        incoming.get("target_statement_month")
    ):
        merged["target_statement_month"] = incoming.get("target_statement_month")
        for key in (
            "source_downloader_id",
            "source_downloader_suggested_next_action",
            "source_downloader_operator_next_action",
        ):
            if present(incoming.get(key)):
                merged[key] = incoming.get(key)
        for key in (
            "target_month_document_identifiers",
            "expected_document_ids",
            "target_month_statement_candidates",
            "target_month_candidate_count",
            "target_month_downloadable_count",
            "statement_document_candidate_count",
            "statement_document_metadata_only_count",
            "statement_document_metadata_only_months",
            "statement_document_payload_count",
            "statement_document_payload_months",
            "latest_statement_document_metadata_only_month",
            "latest_statement_month",
            "latest_downloadable_statement_month",
            "downloadable_statement_months",
            "available_statement_months",
            "capture_command",
            "capture_prepare_command",
        ):
            if present(incoming.get(key)):
                if (
                    key == "target_month_statement_candidates"
                    and candidate_payload_diagnostic_score(merged.get(key))
                    > candidate_payload_diagnostic_score(incoming.get(key))
                ):
                    continue
                merged[key] = incoming.get(key)
        merged["post_capture_apply_command"] = post_capture_apply_command(
            property_name=str(merged.get("property") or "").strip(),
            target_month=str(merged.get("target_statement_month") or "").strip() or None,
            process_existing_body_recaptures=str(merged.get("reason") or "").strip()
            in (EXISTING_HAR_BODY_REASONS | TARGET_MONTH_PDF_RECAPTURE_REASONS),
        )
        merged["post_capture_apply_command_safe_after_har_capture"] = bool(
            merged.get("post_capture_apply_command")
        )
        live_cdp_next_action = merged.get("source_downloader_operator_next_action") or merged.get(
            "source_downloader_suggested_next_action"
        )
        if should_emit_live_cdp_downloader_command(
            live_cdp_next_action,
            merged.get("source_downloader_id"),
            live_cdp_downloader_ids,
        ):
            merged["live_cdp_downloader_command"] = live_cdp_downloader_command(
                str(merged.get("property") or "").strip(),
                str(merged.get("target_statement_month") or "").strip() or None,
                str(merged.get("source_downloader_id") or "").strip() or None,
            )
            merged["live_cdp_downloader_ready_after_visible_auth"] = True

    if incoming.get("capture_auth_blocker") is True:
        for key in (
            "capture_auth_blocker",
            "capture_auth_blocker_reason",
            "capture_auth_next_action",
            "capture_command_ready_to_run_now",
            "capture_command_blocked_reason",
            "register_workflow_evidence_ready_to_run_now",
            "advance_workflow_evidence_ready_to_run_now",
            "source_downloader_operator_next_action",
            "source_downloader_suggested_next_action",
            "credential_lookup_status",
            "credential_lookup_failure_reason",
            "credential_lookup_item_name",
            "credential_lookup_expected_folder_name",
            "credential_lookup_uri_host",
            "credential_lookup_uri_host_aliases",
            "credential_lookup_search_terms",
            "credential_lookup_candidate_search_term_count",
            "credential_lookup_candidate_search_terms",
            "credential_lookup_candidate_count",
            "credential_lookup_candidate_items",
            "credential_lookup_unguarded_candidate_count",
            "credential_lookup_unguarded_candidate_items",
            "credential_lookup_misfiled_candidate_count",
            "credential_lookup_absent",
            "credential_lookup_scope",
            "credential_lookup_repair_action",
            "credential_repair_instruction",
            "command_precondition_status",
            "command_precondition_reason",
            "command_precondition_next_action",
            "safe_to_run_automatically",
        ):
            if key in incoming:
                merged[key] = incoming.get(key)
    command_document_ids = expected_document_ids_from_capture_command(merged.get("capture_command"))
    if command_document_ids:
        merged["target_month_document_identifiers"] = command_document_ids
        merged["expected_document_ids"] = command_document_ids
    if not present(merged.get("target_month_recapture_reason")):
        derived_recapture_reason = recapture_reason_from_candidates(merged.get("target_month_statement_candidates"))
        if derived_recapture_reason:
            merged["target_month_recapture_reason"] = derived_recapture_reason
            merged["target_month_recapture_required"] = True
    return merged


def dedupe_entries(
    entries: list[dict[str, Any]],
    live_cdp_downloader_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    seen: dict[tuple[str, str, str, str], int] = {}
    kept: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for entry in entries:
        key = entry_work_unit_key(entry)
        identity = entry_identity(entry)
        identity_text = identity_label(identity)
        if key in seen:
            duplicate = dict(entry)
            duplicate["capture_identity"] = identity_text
            duplicate["duplicate_capture_identity"] = identity_text
            duplicates.append(duplicate)
            kept_index = seen[key]
            kept[kept_index] = merge_duplicate_entry(
                kept[kept_index],
                duplicate,
                live_cdp_downloader_ids,
            )
            continue
        seen[key] = len(kept)
        kept_entry = dict(entry)
        kept.append(kept_entry)
    for kept_entry in kept:
        kept_entry["capture_identity"] = identity_label(entry_identity(kept_entry))
    return kept, duplicates


def supersession_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        normalize_property(entry.get("property")),
        str(entry.get("suggested_workflow_har_path") or entry.get("har_path") or "").strip(),
        str(entry.get("target_statement_month") or "").strip(),
    )


def capture_requirements_specificity(requirements: object) -> int:
    if not isinstance(requirements, list):
        return 0
    text = " ".join(str(item or "") for item in requirements)
    score = 0
    if "document_identifier" in text:
        score += 10
    if "Same-month alternate" in text:
        score += 10
    if "Network.getResponseBody" in text:
        score += 5
    if "Target statement month:" in text and score == 0:
        score -= 1
    return score


def suppress_handoffs_superseded_by_downloader_gaps(
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    handoff_requirements_by_key: dict[tuple[str, str, str], list[str]] = {}
    for entry in entries:
        if entry.get("source") == "mortgage_statement_downloader_gap":
            continue
        requirements = entry.get("capture_requirements")
        if not isinstance(requirements, list):
            continue
        key = supersession_key(entry)
        current = handoff_requirements_by_key.get(key)
        if capture_requirements_specificity(requirements) > capture_requirements_specificity(current):
            handoff_requirements_by_key[key] = requirements
    downloader_gap_keys = {
        supersession_key(entry)
        for entry in entries
        if entry.get("source") == "mortgage_statement_downloader_gap"
        and (
            entry.get("required_response_paths")
            or entry.get("target_month_document_identifiers")
            or entry.get("capture_auth_blocker") is True
        )
    }
    if not downloader_gap_keys:
        return entries, []
    kept: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for entry in entries:
        if (
            supersession_key(entry) in downloader_gap_keys
            and entry.get("source") != "mortgage_statement_downloader_gap"
        ):
            duplicate = dict(entry)
            identity_text = identity_label(entry_identity(entry))
            duplicate["capture_identity"] = identity_text
            duplicate["duplicate_capture_identity"] = identity_text
            duplicate["duplicate_suppressed_by"] = "mortgage_statement_downloader_gap"
            suppressed.append(duplicate)
            continue
        if entry.get("source") == "mortgage_statement_downloader_gap":
            key = supersession_key(entry)
            handoff_requirements = handoff_requirements_by_key.get(key)
            if capture_requirements_specificity(handoff_requirements) > capture_requirements_specificity(
                entry.get("capture_requirements")
            ):
                entry = dict(entry)
                entry["capture_requirements"] = handoff_requirements
        kept.append(entry)
    return kept, suppressed


def priority_rank(entry: dict[str, Any]) -> tuple[int, int, str]:
    if entry.get("tokenomics_statement_gap_blocking") is True:
        priority = 0
    elif entry.get("tokenomics_workbook_write_blocking") is True:
        priority = 1
    elif entry.get("existing_har_needs_response_bodies") is True:
        priority = 2
    else:
        priority = 3
    missing_har_rank = 0 if entry.get("missing_har") is True else 1
    return (priority, missing_har_rank, str(entry.get("property") or ""))


def build_capture_queue(
    *,
    handoff_index: Path,
    evidence_refresh_report: Path,
    tokenomics_report: Path = DEFAULT_TOKENOMICS_REPORT,
    downloader_report: Path = DEFAULT_DOWNLOADER_REPORT,
    coverage_report: Path = DEFAULT_COVERAGE_REPORT,
    registry: Path = DEFAULT_REGISTRY,
    supplemental_downloader_reports: list[Path] | None = None,
    intake: Path = DEFAULT_INTAKE,
    target_month: str | None = None,
    workflow_target_month: str | None = None,
) -> dict[str, Any]:
    index = load_json(handoff_index)
    refresh = load_json(evidence_refresh_report)
    tokenomics = load_json(tokenomics_report)
    downloader = load_json(downloader_report)
    coverage_report_data = load_json(coverage_report)
    live_cdp_downloader_ids = live_cdp_profile_downloader_ids(registry)
    if supplemental_downloader_reports is not None:
        supplemental_downloader_reports = list(supplemental_downloader_reports)
    elif downloader_report == DEFAULT_DOWNLOADER_REPORT:
        supplemental_downloader_reports = list(DEFAULT_SUPPLEMENTAL_DOWNLOADER_REPORTS)
    else:
        supplemental_downloader_reports = []
    supplemental_downloaders = [load_json(path) for path in supplemental_downloader_reports if path.exists()]
    intake_by_property = load_intake_by_property(intake)
    downloader_context = merge_downloader_contexts(
        downloader_context_by_property(downloader),
        *[downloader_context_by_property(report) for report in supplemental_downloaders],
        coverage_context_by_property(coverage_report_data),
    )
    target_month = clean_target_month(target_month)
    workflow_target_month = clean_target_month(workflow_target_month)
    rejected_by_property = rejected_candidates_by_property(refresh)
    tokenomics_gaps = tokenomics_gap_properties(tokenomics)
    tokenomics_gap_set = {normalize_property(prop) for prop in tokenomics_gaps}
    tokenomics_blockers_by_property = tokenomics_write_blockers_by_property(tokenomics)
    handoff_candidate_entries = [
        build_entry(
            handoff,
            rejected_by_property,
            tokenomics_gap_set,
            tokenomics_blockers_by_property,
            target_month,
        )
        for handoff in handoffs_from_index(index)
        if should_queue_handoff(handoff)
    ]
    downloader_candidate_entries = downloader_gap_entries(
        downloader,
        intake_by_property,
        tokenomics_gap_set,
        tokenomics_blockers_by_property,
        target_month,
        live_cdp_downloader_ids,
    )
    coverage_candidate_entries = coverage_gap_entries(
        coverage_report_data,
        rejected_by_property,
        tokenomics_gap_set,
        tokenomics_blockers_by_property,
        target_month,
    )
    candidate_entries = [
        enrich_entry_with_downloader_context(entry, downloader_context, live_cdp_downloader_ids)
        for entry in (handoff_candidate_entries + downloader_candidate_entries + coverage_candidate_entries)
    ]
    candidate_entries, superseded_entries = suppress_handoffs_superseded_by_downloader_gaps(candidate_entries)
    entries, duplicate_entries = dedupe_entries(candidate_entries, live_cdp_downloader_ids)
    duplicate_entries.extend(superseded_entries)
    entries = sorted(entries, key=priority_rank)
    rejected_paths = unique_strings(
        [
            path
            for entry in entries
            for path in entry.get("rejected_discovered_har_candidate_paths") or []
        ]
    )
    missing_har_entries = [entry for entry in entries if entry.get("missing_har")]
    existing_har_body_entries = [entry for entry in entries if entry.get("existing_har_needs_response_bodies")]
    existing_har_target_pdf_entries = [
        entry for entry in entries if entry.get("existing_har_needs_target_month_statement_pdf")
    ]
    target_month_workflow_har_capture_required_entries = [
        entry
        for entry in entries
        if entry.get("command_precondition_status") == "target_month_workflow_har_capture_required"
    ]
    automatic_entries = [entry for entry in entries if entry.get("safe_to_run_automatically") is True]
    manual_entries = [entry for entry in entries if entry.get("safe_to_run_automatically") is not True]
    tokenomics_blocking_entries = [entry for entry in entries if entry.get("tokenomics_statement_gap_blocking") is True]
    tokenomics_non_blocking_entries = [entry for entry in entries if entry.get("tokenomics_statement_gap_blocking") is not True]
    status = "review" if entries else "ok"
    return {
        "job": "mortgage-workflow-capture-queue",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reason": "workflow_capture_required" if entries else None,
        "handoff_index": str(handoff_index),
        "handoff_index_exists": handoff_index.exists(),
        "evidence_refresh_report": str(evidence_refresh_report),
        "evidence_refresh_report_exists": evidence_refresh_report.exists(),
        "evidence_refresh_status": refresh.get("status"),
        "evidence_refresh_reason": refresh.get("reason"),
        "tokenomics_report": str(tokenomics_report),
        "tokenomics_report_exists": tokenomics_report.exists(),
        "downloader_report": str(downloader_report),
        "downloader_report_exists": downloader_report.exists(),
        "registry": str(registry),
        "registry_exists": registry.exists(),
        "live_cdp_profile_downloader_ids": sorted(live_cdp_downloader_ids),
        "live_cdp_profile_downloader_count": len(live_cdp_downloader_ids),
        "coverage_report": str(coverage_report),
        "coverage_report_exists": coverage_report.exists(),
        "coverage_report_status": coverage_report_data.get("status"),
        "downloader_report_status": downloader.get("status"),
        "supplemental_downloader_reports": [str(path) for path in supplemental_downloader_reports],
        "supplemental_downloader_report_exists": [path.exists() for path in supplemental_downloader_reports],
        "supplemental_downloader_report_statuses": [report.get("status") for report in supplemental_downloaders],
        "intake": str(intake),
        "intake_exists": intake.exists(),
        "target_month": workflow_target_month or target_month,
        "target_statement_month_override": target_month,
        "tokenomics_status": tokenomics.get("status"),
        "tokenomics_current_month": tokenomics.get("current_month"),
        "tokenomics_current_month_statement_gap_properties": tokenomics_gaps,
        "tokenomics_workbook_write_blocker_count": tokenomics.get("workbook_write_blocker_count")
        if isinstance(tokenomics.get("workbook_write_blocker_count"), int)
        else len(tokenomics_write_blockers(tokenomics)),
        "tokenomics_workbook_write_blocker_properties": tokenomics.get("workbook_write_blocker_properties")
        if isinstance(tokenomics.get("workbook_write_blocker_properties"), list)
        else unique_strings([item.get("property") for item in tokenomics_write_blockers(tokenomics)]),
        "tokenomics_workbook_write_blocker_reasons": tokenomics.get("workbook_write_blocker_reasons")
        if isinstance(tokenomics.get("workbook_write_blocker_reasons"), list)
        else unique_strings([item.get("reason") for item in tokenomics_write_blockers(tokenomics)]),
        "capture_count": len(entries),
        "capture_properties": [entry.get("property") for entry in entries],
        "candidate_capture_handoff_count": len(candidate_entries),
        "handoff_candidate_capture_count": len(handoff_candidate_entries),
        "downloader_gap_candidate_capture_count": len(downloader_candidate_entries),
        "downloader_gap_candidate_capture_properties": [
            entry.get("property") for entry in downloader_candidate_entries
        ],
        "coverage_gap_candidate_capture_count": len(coverage_candidate_entries),
        "coverage_gap_candidate_capture_properties": [
            entry.get("property") for entry in coverage_candidate_entries
        ],
        "duplicate_capture_handoff_count": len(duplicate_entries),
        "duplicate_capture_handoff_properties": unique_strings(
            [entry.get("property") for entry in duplicate_entries]
        ),
        "duplicate_capture_handoff_identities": unique_strings(
            [entry.get("duplicate_capture_identity") for entry in duplicate_entries]
        ),
        "duplicate_capture_handoffs_suppressed": len(duplicate_entries) > 0,
        "missing_har_count": len(missing_har_entries),
        "missing_har_properties": [entry.get("property") for entry in missing_har_entries],
        "existing_har_needs_bodies_count": len(existing_har_body_entries),
        "existing_har_needs_bodies_properties": [
            entry.get("property") for entry in existing_har_body_entries
        ],
        "existing_har_needs_target_month_statement_pdf_count": len(existing_har_target_pdf_entries),
        "existing_har_needs_target_month_statement_pdf_properties": [
            entry.get("property") for entry in existing_har_target_pdf_entries
        ],
        "target_month_workflow_har_capture_required_count": len(
            target_month_workflow_har_capture_required_entries
        ),
        "target_month_workflow_har_capture_required_properties": [
            entry.get("property") for entry in target_month_workflow_har_capture_required_entries
        ],
        "existing_workflow_har_paths": unique_strings(
            [entry.get("existing_workflow_har_path") for entry in entries]
        ),
        "existing_workflow_har_path_sizes": unique_present_values(
            [entry.get("existing_workflow_har_path_size") for entry in entries]
        ),
        "automatic_capture_count": len(automatic_entries),
        "automatic_capture_properties": [entry.get("property") for entry in automatic_entries],
        "manual_capture_count": len(manual_entries),
        "manual_capture_properties": [entry.get("property") for entry in manual_entries],
        "tokenomics_blocking_count": len(tokenomics_blocking_entries),
        "tokenomics_blocking_properties": [entry.get("property") for entry in tokenomics_blocking_entries],
        "tokenomics_non_blocking_count": len(tokenomics_non_blocking_entries),
        "tokenomics_non_blocking_properties": [entry.get("property") for entry in tokenomics_non_blocking_entries],
        "rejected_discovered_har_candidate_count": len(rejected_paths),
        "rejected_discovered_har_candidate_paths": rejected_paths,
        "entries": entries,
        "actions": entries,
        "safe_to_run_automatically": not manual_entries,
    }


def markdown_value(value: object) -> str:
    if value in (None, "", [], {}):
        return "none"
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Mortgage Workflow Capture Queue",
        "",
        f"- status: `{markdown_value(report.get('status'))}`",
        f"- reason: `{markdown_value(report.get('reason'))}`",
        f"- candidate_capture_handoff_count: `{markdown_value(report.get('candidate_capture_handoff_count'))}`",
        f"- handoff_candidate_capture_count: `{markdown_value(report.get('handoff_candidate_capture_count'))}`",
        f"- downloader_gap_candidate_capture_count: `{markdown_value(report.get('downloader_gap_candidate_capture_count'))}`",
        f"- downloader_gap_candidate_capture_properties: `{markdown_value(report.get('downloader_gap_candidate_capture_properties'))}`",
        f"- coverage_gap_candidate_capture_count: `{markdown_value(report.get('coverage_gap_candidate_capture_count'))}`",
        f"- coverage_gap_candidate_capture_properties: `{markdown_value(report.get('coverage_gap_candidate_capture_properties'))}`",
        f"- capture_count: `{markdown_value(report.get('capture_count'))}`",
        f"- duplicate_capture_handoff_count: `{markdown_value(report.get('duplicate_capture_handoff_count'))}`",
        f"- duplicate_capture_handoff_identities: `{markdown_value(report.get('duplicate_capture_handoff_identities'))}`",
        f"- safe_to_run_automatically: `{markdown_value(report.get('safe_to_run_automatically'))}`",
        f"- manual_capture_count: `{markdown_value(report.get('manual_capture_count'))}`",
        f"- automatic_capture_count: `{markdown_value(report.get('automatic_capture_count'))}`",
        f"- tokenomics_blocking_count: `{markdown_value(report.get('tokenomics_blocking_count'))}`",
        f"- tokenomics_blocking_properties: `{markdown_value(report.get('tokenomics_blocking_properties'))}`",
        f"- tokenomics_non_blocking_count: `{markdown_value(report.get('tokenomics_non_blocking_count'))}`",
        f"- tokenomics_non_blocking_properties: `{markdown_value(report.get('tokenomics_non_blocking_properties'))}`",
        f"- tokenomics_workbook_write_blocker_count: `{markdown_value(report.get('tokenomics_workbook_write_blocker_count'))}`",
        f"- tokenomics_workbook_write_blocker_properties: `{markdown_value(report.get('tokenomics_workbook_write_blocker_properties'))}`",
        f"- tokenomics_workbook_write_blocker_reasons: `{markdown_value(report.get('tokenomics_workbook_write_blocker_reasons'))}`",
        f"- missing_har_count: `{markdown_value(report.get('missing_har_count'))}`",
        f"- existing_har_needs_bodies_count: `{markdown_value(report.get('existing_har_needs_bodies_count'))}`",
        f"- existing_har_needs_target_month_statement_pdf_count: `{markdown_value(report.get('existing_har_needs_target_month_statement_pdf_count'))}`",
        f"- target_month_workflow_har_capture_required_count: `{markdown_value(report.get('target_month_workflow_har_capture_required_count'))}`",
        f"- target_month_workflow_har_capture_required_properties: `{markdown_value(report.get('target_month_workflow_har_capture_required_properties'))}`",
        f"- existing_workflow_har_paths: `{markdown_value(report.get('existing_workflow_har_paths'))}`",
        f"- rejected_discovered_har_candidate_count: `{markdown_value(report.get('rejected_discovered_har_candidate_count'))}`",
        "",
    ]
    entries = report.get("entries") if isinstance(report.get("entries"), list) else []
    if not entries:
        lines.extend(["No mortgage workflow HAR captures are queued.", ""])
        return "\n".join(lines)
    for index, entry in enumerate(entries, start=1):
        lines.extend(
            [
                f"## {index}. {entry.get('property')}",
                "",
                f"- servicer_hint: `{markdown_value(entry.get('servicer_hint'))}`",
                f"- portal_url: `{markdown_value(entry.get('portal_url'))}`",
                f"- target_statement_month: `{markdown_value(entry.get('target_statement_month'))}`",
                f"- capture_identity: `{markdown_value(entry.get('capture_identity'))}`",
                f"- reason: `{markdown_value(entry.get('reason'))}`",
                f"- type: `{markdown_value(entry.get('type'))}`",
                f"- suggested_next_action: `{markdown_value(entry.get('suggested_next_action'))}`",
                f"- suggested_workflow_har_path: `{markdown_value(entry.get('suggested_workflow_har_path'))}`",
                f"- har_path: `{markdown_value(entry.get('har_path'))}`",
                f"- suggested_workflow_har_path_exists: `{markdown_value(entry.get('suggested_workflow_har_path_exists'))}`",
                f"- existing_workflow_har_path: `{markdown_value(entry.get('existing_workflow_har_path'))}`",
                f"- existing_workflow_har_path_exists: `{markdown_value(entry.get('existing_workflow_har_path_exists'))}`",
                f"- existing_workflow_har_path_size: `{markdown_value(entry.get('existing_workflow_har_path_size'))}`",
                f"- required_capture_quality: `{markdown_value(entry.get('required_capture_quality'))}`",
                f"- target_month_document_identifiers: `{markdown_value(entry.get('target_month_document_identifiers'))}`",
                f"- expected_document_ids: `{markdown_value(entry.get('expected_document_ids'))}`",
                f"- body_recapture_capture_method: `{markdown_value(entry.get('body_recapture_capture_method'))}`",
                f"- body_recapture_capture_instruction: `{markdown_value(entry.get('body_recapture_capture_instruction'))}`",
                f"- body_recapture_manual_har_export_warning: `{markdown_value(entry.get('body_recapture_manual_har_export_warning'))}`",
                f"- credentials_available: `{markdown_value(entry.get('credentials_available'))}`",
                f"- credential_source: `{markdown_value(entry.get('credential_source'))}`",
                f"- credential_lookup_status: `{markdown_value(entry.get('credential_lookup_status'))}`",
                f"- credential_lookup_failure_reason: `{markdown_value(entry.get('credential_lookup_failure_reason'))}`",
                f"- credential_lookup_item_name: `{markdown_value(entry.get('credential_lookup_item_name'))}`",
                f"- credential_lookup_expected_folder_name: `{markdown_value(entry.get('credential_lookup_expected_folder_name'))}`",
                f"- credential_lookup_uri_host: `{markdown_value(entry.get('credential_lookup_uri_host'))}`",
                f"- credential_login_hint_mismatch: `{markdown_value(entry.get('credential_login_hint_mismatch'))}`",
                f"- credential_login_hint_mismatch_overridden: `{markdown_value(entry.get('credential_login_hint_mismatch_overridden'))}`",
                f"- auth_issue: `{markdown_value(entry.get('auth_issue'))}`",
                f"- manual_auth_reason: `{markdown_value(entry.get('manual_auth_reason'))}`",
                f"- manual_auth_portal_url: `{markdown_value(entry.get('manual_auth_portal_url'))}`",
                f"- auth_state: `{markdown_value(entry.get('auth_state'))}`",
                f"- auto_login_status: `{markdown_value(entry.get('auto_login_status'))}`",
                f"- auto_login_blocked_reason: `{markdown_value(entry.get('auto_login_blocked_reason'))}`",
                f"- capture_auth_blocker: `{markdown_value(entry.get('capture_auth_blocker'))}`",
                f"- capture_auth_blocker_reason: `{markdown_value(entry.get('capture_auth_blocker_reason'))}`",
                f"- capture_auth_next_action: `{markdown_value(entry.get('capture_auth_next_action'))}`",
                f"- command_precondition_status: `{markdown_value(entry.get('command_precondition_status'))}`",
                f"- command_precondition_reason: `{markdown_value(entry.get('command_precondition_reason'))}`",
                f"- command_precondition_next_action: `{markdown_value(entry.get('command_precondition_next_action'))}`",
                f"- auto_otp_fetch_enabled: `{markdown_value(entry.get('auto_otp_fetch_enabled'))}`",
                f"- auto_otp_fetch_attempted: `{markdown_value(entry.get('auto_otp_fetch_attempted'))}`",
                f"- auto_otp_fetch_status: `{markdown_value(entry.get('auto_otp_fetch_status'))}`",
                f"- live_cdp_downloader_ready_after_visible_auth: `{markdown_value(entry.get('live_cdp_downloader_ready_after_visible_auth'))}`",
                f"- live_cdp_downloader_command: `{markdown_value(entry.get('live_cdp_downloader_command'))}`",
                f"- tokenomics_blocking: `{markdown_value(entry.get('tokenomics_blocking'))}`",
                f"- tokenomics_statement_gap_blocking: `{markdown_value(entry.get('tokenomics_statement_gap_blocking'))}`",
                f"- workbook_write_blocking: `{markdown_value(entry.get('workbook_write_blocking'))}`",
                f"- tokenomics_workbook_write_blocking: `{markdown_value(entry.get('tokenomics_workbook_write_blocking'))}`",
                f"- tokenomics_capture_priority: `{markdown_value(entry.get('tokenomics_capture_priority'))}`",
                f"- tokenomics_write_blocker_count: `{markdown_value(entry.get('tokenomics_write_blocker_count'))}`",
                f"- tokenomics_write_blocker_reasons: `{markdown_value(entry.get('tokenomics_write_blocker_reasons'))}`",
                f"- tokenomics_write_blocker_statement_month_statuses: `{markdown_value(entry.get('tokenomics_write_blocker_statement_month_statuses'))}`",
                f"- capture_prepare_command: `{markdown_value(entry.get('capture_prepare_command'))}`",
                f"- capture_command: `{markdown_value(entry.get('capture_command'))}`",
                f"- post_capture_check_command: `{markdown_value(entry.get('post_capture_check_command'))}`",
                f"- validation_command: `{markdown_value(entry.get('validation_command'))}`",
                f"- post_capture_apply_command: `{markdown_value(entry.get('post_capture_apply_command'))}`",
                f"- post_capture_apply_command_safe_after_har_capture: `{markdown_value(entry.get('post_capture_apply_command_safe_after_har_capture'))}`",
                f"- register_workflow_evidence_command: `{markdown_value(entry.get('register_workflow_evidence_command'))}`",
                f"- register_workflow_evidence_apply_command: `{markdown_value(entry.get('register_workflow_evidence_apply_command'))}`",
                f"- advance_workflow_evidence_command: `{markdown_value(entry.get('advance_workflow_evidence_command'))}`",
                f"- advance_command: `{markdown_value(entry.get('advance_command'))}`",
                f"- advance_workflow_evidence_apply_command: `{markdown_value(entry.get('advance_workflow_evidence_apply_command'))}`",
                f"- advance_apply_command: `{markdown_value(entry.get('advance_apply_command'))}`",
            ]
        )
        rejected_paths = entry.get("rejected_discovered_har_candidate_paths")
        if isinstance(rejected_paths, list) and rejected_paths:
            lines.append(f"- rejected_discovered_har_candidate_paths: `{markdown_value(rejected_paths)}`")
        rejected_candidates = entry.get("rejected_discovered_har_candidates")
        if isinstance(rejected_candidates, list) and rejected_candidates:
            lines.extend(["", "Rejected discovered HAR candidates:"])
            for candidate in rejected_candidates:
                if not isinstance(candidate, dict):
                    continue
                lines.append(
                    "- "
                    f"path: `{markdown_value(candidate.get('path'))}`; "
                    f"reason: `{markdown_value(candidate.get('rejected_reason') or candidate.get('analysis_reason'))}`; "
                    f"command_status: `{markdown_value(candidate.get('command_status'))}`; "
                    f"match_reasons: `{markdown_value(candidate.get('match_reasons'))}`"
                )
        requirements = entry.get("capture_requirements")
        if isinstance(requirements, list) and requirements:
            lines.extend(["", "Capture requirements:"])
            lines.extend(f"- {item}" for item in requirements if item)
        lines.append("")
    return "\n".join(lines)


def write_text_if_changed(path: Path, text: str) -> None:
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return
    except Exception:
        pass
    path.write_text(text, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff-index", type=Path, default=DEFAULT_HANDOFF_INDEX)
    parser.add_argument("--evidence-refresh-report", type=Path, default=DEFAULT_EVIDENCE_REFRESH_REPORT)
    parser.add_argument("--tokenomics-report", type=Path, default=DEFAULT_TOKENOMICS_REPORT)
    parser.add_argument("--downloader-report", type=Path, default=DEFAULT_DOWNLOADER_REPORT)
    parser.add_argument("--coverage-report", type=Path, default=DEFAULT_COVERAGE_REPORT)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument(
        "--supplemental-downloader-report",
        action="append",
        type=Path,
        default=None,
        help="Optional additional downloader report used only to enrich per-property context.",
    )
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--target-month", help="Override queued target statement month as YYYY-MM.")
    parser.add_argument(
        "--workflow-target-month",
        help="Record the workflow/payment month as YYYY-MM without overriding per-entry statement months.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    report = build_capture_queue(
        handoff_index=args.handoff_index,
        evidence_refresh_report=args.evidence_refresh_report,
        tokenomics_report=args.tokenomics_report,
        downloader_report=args.downloader_report,
        coverage_report=args.coverage_report,
        registry=args.registry,
        supplemental_downloader_reports=args.supplemental_downloader_report,
        intake=args.intake,
        target_month=args.target_month,
        workflow_target_month=args.workflow_target_month,
    )
    report = dict(report)
    report["idempotency_digest"] = stable_report_digest(report)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    report = write_json_report(args.report, report)
    report_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    markdown_text = build_markdown(report)
    write_text_if_changed(args.markdown, markdown_text)
    print(report_text, file=stdout, end="")
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
