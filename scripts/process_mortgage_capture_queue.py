#!/usr/bin/env python3
"""Idempotently process captured mortgage workflow HARs from the capture queue."""

from __future__ import annotations

import argparse
import json
import subprocess
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
import mortgage_workflow_har_intake_analyzer as analyzer
from stable_json_report import stable_report_digest, write_json_report

DEFAULT_REPORT_DIR = WORKSPACE_ROOT / "reports"
DEFAULT_QUEUE = DEFAULT_REPORT_DIR / "mortgage_workflow_capture_queue.json"
DEFAULT_REPORT = DEFAULT_REPORT_DIR / "mortgage_workflow_capture_queue_processor.json"
DEFAULT_MARKDOWN = DEFAULT_REPORT_DIR / "mortgage_workflow_capture_queue_processor.md"
DEFAULT_ADVANCE_SCRIPT = SCRIPTS_DIR / "advance_mortgage_workflow_evidence.py"
DEFAULT_INTAKE = WORKSPACE_ROOT / "config" / "mortgage_downloader_intake.json"
DEFAULT_REGISTRY = WORKSPACE_ROOT / "config" / "mortgage_statement_downloaders.json"
DEFAULT_COVERAGE_REPORT = DEFAULT_REPORT_DIR / "mortgage_downloader_coverage_report.json"

BODY_RECAPTURE_REASONS = {
    "candidate_endpoints_missing_response_bodies",
    "embedded_statement_pdf_payload_missing",
    "missing_response_bodies",
    "needs_full_response_bodies",
    "target_month_statement_unavailable",
    "target_month_statement_pdf_payload_missing",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def normalize_live_cdp_downloader_command(command: object) -> object:
    text = str(command or "").strip()
    if not text:
        return command
    if "MORTGAGE_STATEMENT_TARGET_MONTH=" not in text:
        return command
    if "MORTGAGE_DOWNLOADER_EXACT_TARGET_MONTH=" in text:
        return command
    needle = "MORTGAGE_DOWNLOADER_IDS="
    if needle in text:
        return text.replace(needle, "MORTGAGE_DOWNLOADER_EXACT_TARGET_MONTH=1 " + needle, 1)
    return text + " MORTGAGE_DOWNLOADER_EXACT_TARGET_MONTH=1"


def validate_har_file(path: Path) -> tuple[bool, str | None, int | None]:
    """Return whether a captured file is HAR-shaped enough to process."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False, "har_not_json", None
    except UnicodeDecodeError:
        return False, "har_not_utf8_json", None
    except OSError as exc:
        return False, f"har_unreadable:{exc.__class__.__name__}", None
    if not isinstance(data, dict):
        return False, "har_root_not_object", None
    log = data.get("log")
    if not isinstance(log, dict):
        return False, "har_log_missing", None
    entries = log.get("entries")
    if not isinstance(entries, list):
        return False, "har_log_entries_missing", None
    if not entries:
        return False, "har_log_entries_empty", 0
    return True, None, len(entries)


def entries_from_queue(queue: dict[str, Any], properties: list[str]) -> list[dict[str, Any]]:
    raw_entries = queue.get("entries")
    if not isinstance(raw_entries, list):
        return []
    entries = [item for item in raw_entries if isinstance(item, dict)]
    if not properties:
        return entries
    wanted = {coverage.normalize_property(value) for value in properties}
    return [item for item in entries if coverage.normalize_property(item.get("property")) in wanted]


def coverage_context_by_property(coverage_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    context: dict[str, dict[str, Any]] = {}
    for key in ("next_action_plan", "downloader_target_month_statement_gaps", "current_month_statement_gaps"):
        items = coverage_report.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            prop_key = coverage.normalize_property(item.get("property"))
            if not prop_key:
                continue
            existing = context.setdefault(prop_key, {})
            for field in WORKFLOW_HANDOFF_FIELDS:
                value = item.get(field)
                if value not in (None, "", []):
                    existing[field] = value
    return context


def merge_coverage_context_into_entry(entry: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not context:
        return entry
    merged = dict(entry)
    context_auth_blocked = context.get("capture_auth_blocker") is True
    entry_auth_blocked = entry.get("capture_auth_blocker") is True
    entry_auth_explicitly_clear = entry.get("capture_auth_blocker") is False
    for field in WORKFLOW_HANDOFF_FIELDS:
        value = context.get(field)
        if value in (None, "", []):
            continue
        if entry_auth_explicitly_clear and field in AUTH_BLOCKING_CONTEXT_FIELDS:
            continue
        if merged.get(field) in (None, "", []) or (context_auth_blocked and not entry_auth_blocked):
            merged[field] = value
    return merged


def item_report_path(report_dir: Path, property_name: str) -> Path:
    return report_dir / f"mortgage_workflow_capture_queue_processor_{coverage.slugify(property_name)}.json"


def advance_command(
    *,
    advance_script: Path,
    property_name: str,
    har_path: Path,
    intake: Path,
    registry: Path,
    coverage_report: Path,
    report_dir: Path,
    workspace_root: Path,
    report: Path,
    target_month: str | None,
    apply: bool,
    enable_verified_registry_entry: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(advance_script),
        "--property",
        property_name,
        "--har",
        f"{property_name}={har_path}",
        "--intake",
        str(intake),
        "--registry",
        str(registry),
        "--coverage-report",
        str(coverage_report),
        "--report-dir",
        str(report_dir),
        "--workspace-root",
        str(workspace_root),
        "--report",
        str(report),
        "--write-stubs",
        "--install-registry-entries",
    ]
    if target_month:
        command.extend(["--target-month", target_month])
    if apply:
        command.extend(["--apply-evidence", "--apply-registry"])
        if enable_verified_registry_entry:
            command.append("--enable-verified-registry-entry")
    return command


def command_text(command: list[str]) -> str:
    return " ".join(json.dumps(part) for part in command)


def processor_command(
    args: argparse.Namespace,
    *,
    property_name: str,
    apply: bool,
    process_existing_body_recaptures: bool = False,
    target_month: str | None = None,
) -> list[str]:
    command = [
        sys.executable,
        str(SCRIPT_PATH),
        "--queue",
        str(args.queue),
        "--report",
        str(args.report),
        "--markdown",
        str(args.markdown),
        "--report-dir",
        str(args.report_dir),
        "--advance-script",
        str(args.advance_script),
        "--intake",
        str(args.intake),
        "--registry",
        str(args.registry),
        "--coverage-report",
        str(args.coverage_report),
        "--workspace-root",
        str(args.workspace_root),
        "--property",
        property_name,
        "--timeout-seconds",
        str(args.timeout_seconds),
    ]
    target = str(target_month or args.target_month or "").strip()
    if target:
        command.extend(["--target-month", target])
    if apply:
        command.append("--apply")
    if process_existing_body_recaptures:
        command.append("--process-existing-body-recaptures")
    if args.enable_verified_registry_entry:
        command.append("--enable-verified-registry-entry")
    else:
        command.append("--no-enable-verified-registry-entry")
    return command


def tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def write_text_if_changed(path: Path, content: str) -> None:
    if not content.endswith("\n"):
        content += "\n"
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def fmt(value: Any) -> str:
    if value is None:
        return "null"
    return str(value)


def queued_entry_safe_to_run_automatically(entry: dict[str, Any]) -> bool:
    """Return whether the queued capture action itself is safe unattended."""
    if entry.get("safe_to_run_automatically") is not None:
        return entry.get("safe_to_run_automatically") is True
    if entry.get("capture_command_safe_to_run_automatically") is not None:
        return entry.get("capture_command_safe_to_run_automatically") is True
    return False


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Mortgage Capture Queue Processor",
        "",
        f"- status: `{fmt(report.get('status'))}`",
        f"- reason: `{fmt(report.get('reason'))}`",
        f"- apply: `{fmt(report.get('apply'))}`",
        f"- process_existing_body_recaptures: `{fmt(report.get('process_existing_body_recaptures'))}`",
        f"- enable_verified_registry_entry: `{fmt(report.get('enable_verified_registry_entry'))}`",
        f"- entry_count: `{fmt(report.get('entry_count'))}`",
        f"- processed_count: `{fmt(report.get('processed_count'))}`",
        f"- applied_count: `{fmt(report.get('applied_count'))}`",
        f"- dry_run_processed_count: `{fmt(report.get('dry_run_processed_count'))}`",
        f"- pending_apply_count: `{fmt(report.get('pending_apply_count'))}`",
        f"- durable_completion_count: `{fmt(report.get('durable_completion_count'))}`",
        f"- ok_count: `{fmt(report.get('ok_count'))}`",
        f"- review_count: `{fmt(report.get('review_count'))}`",
        f"- skipped_count: `{fmt(report.get('skipped_count'))}`",
        f"- missing_har_count: `{fmt(report.get('missing_har_count'))}`",
        f"- missing_har_properties: `{fmt(report.get('missing_har_properties'))}`",
        f"- existing_har_recapture_skipped_count: `{fmt(report.get('existing_har_recapture_skipped_count'))}`",
        f"- existing_har_recapture_skipped_properties: `{fmt(report.get('existing_har_recapture_skipped_properties'))}`",
        f"- existing_har_target_month_pdf_recapture_skipped_count: `{fmt(report.get('existing_har_target_month_pdf_recapture_skipped_count'))}`",
        f"- existing_har_target_month_pdf_recapture_skipped_properties: `{fmt(report.get('existing_har_target_month_pdf_recapture_skipped_properties'))}`",
        f"- existing_body_recapture_skipped_count: `{fmt(report.get('existing_body_recapture_skipped_count'))}`",
        f"- existing_body_recapture_skipped_properties: `{fmt(report.get('existing_body_recapture_skipped_properties'))}`",
        f"- tokenomics_workbook_write_blocker_count: `{fmt(report.get('tokenomics_workbook_write_blocker_count'))}`",
        f"- tokenomics_workbook_write_blocker_properties: `{fmt(report.get('tokenomics_workbook_write_blocker_properties'))}`",
        f"- tokenomics_workbook_write_blocker_reasons: `{fmt(report.get('tokenomics_workbook_write_blocker_reasons'))}`",
        f"- tokenomics_workbook_write_blocking_count: `{fmt(report.get('tokenomics_workbook_write_blocking_count'))}`",
        f"- tokenomics_workbook_write_blocking_properties: `{fmt(report.get('tokenomics_workbook_write_blocking_properties'))}`",
        f"- tokenomics_workbook_write_blocking_reasons: `{fmt(report.get('tokenomics_workbook_write_blocking_reasons'))}`",
        f"- tokenomics_queue_blocker_count: `{fmt(report.get('tokenomics_queue_blocker_count'))}`",
        f"- tokenomics_queue_blocker_properties: `{fmt(report.get('tokenomics_queue_blocker_properties'))}`",
        f"- queue_blocker_status: `{fmt(report.get('queue_blocker_status'))}`",
        f"- queue_blocker_count: `{fmt(report.get('queue_blocker_count'))}`",
        f"- queue_blocker_properties: `{fmt(report.get('queue_blocker_properties'))}`",
        f"- operator_next_action_count: `{fmt(report.get('operator_next_action_count'))}`",
        f"- operator_next_action_properties: `{fmt(report.get('operator_next_action_properties'))}`",
        f"- safe_to_run_automatically: `{fmt(report.get('safe_to_run_automatically'))}`",
        f"- idempotency_digest: `{fmt(report.get('idempotency_digest'))}`",
        "",
    ]
    operator_actions = report.get("operator_next_actions")
    if isinstance(operator_actions, list) and operator_actions:
        lines.extend(["## Operator Next Actions", ""])
        for index, item in enumerate(operator_actions, start=1):
            if not isinstance(item, dict):
                continue
            lines.extend(
                [
                    f"### {index}. {item.get('property') or 'unknown'}",
                    "",
                    f"- next_action: `{fmt(item.get('next_action'))}`",
                    f"- reason: `{fmt(item.get('reason'))}`",
                    f"- credential_login_hint_mismatch: `{fmt(item.get('credential_login_hint_mismatch'))}`",
                    f"- credential_login_hint_mismatch_overridden: `{fmt(item.get('credential_login_hint_mismatch_overridden'))}`",
                    f"- har_path: `{fmt(item.get('har_path'))}`",
                    f"- capture_prepare_command: `{fmt(item.get('capture_prepare_command'))}`",
                    f"- capture_command: `{fmt(item.get('capture_command'))}`",
                    f"- expected_document_ids: `{fmt(item.get('expected_document_ids'))}`",
                    f"- post_capture_check_command: `{fmt(item.get('post_capture_check_command'))}`",
                    f"- post_capture_apply_command: `{fmt(item.get('post_capture_apply_command'))}`",
                    f"- post_capture_apply_command_safe_after_har_capture: `{fmt(item.get('post_capture_apply_command_safe_after_har_capture'))}`",
                    f"- missing_response_body_paths: `{fmt(item.get('missing_response_body_paths'))}`",
                    f"- body_recapture_capture_method: `{fmt(item.get('body_recapture_capture_method'))}`",
                    f"- body_recapture_capture_instruction: `{fmt(item.get('body_recapture_capture_instruction'))}`",
                    f"- body_recapture_manual_har_export_warning: `{fmt(item.get('body_recapture_manual_har_export_warning'))}`",
                    f"- tokenomics_blocking: `{fmt(item.get('tokenomics_blocking'))}`",
                    f"- workbook_write_blocking: `{fmt(item.get('workbook_write_blocking'))}`",
                    f"- tokenomics_workbook_write_blocking: `{fmt(item.get('tokenomics_workbook_write_blocking'))}`",
                    "",
                ]
            )
    requirements = report.get("existing_body_recapture_requirements")
    if isinstance(requirements, list) and requirements:
        lines.extend(["## Existing Body Recapture Requirements", ""])
        for index, item in enumerate(requirements, start=1):
            if not isinstance(item, dict):
                continue
            lines.extend(
                [
                    f"### {index}. {item.get('property') or 'unknown'}",
                    "",
                    f"- har_path: `{fmt(item.get('har_path'))}`",
                    f"- required_capture_quality: `{fmt(item.get('required_capture_quality'))}`",
                    f"- missing_response_body_paths: `{fmt(item.get('missing_response_body_paths'))}`",
                    f"- body_recapture_capture_method: `{fmt(item.get('body_recapture_capture_method'))}`",
                    f"- body_recapture_capture_instruction: `{fmt(item.get('body_recapture_capture_instruction'))}`",
                    f"- body_recapture_manual_har_export_warning: `{fmt(item.get('body_recapture_manual_har_export_warning'))}`",
                    f"- response_body_requirement_progress: `{fmt(item.get('response_body_requirement_progress'))}`",
                    f"- validation_command: `{fmt(item.get('validation_command'))}`",
                    f"- capture_command: `{fmt(item.get('capture_command'))}`",
                    f"- expected_document_ids: `{fmt(item.get('expected_document_ids'))}`",
                    f"- post_capture_apply_command: `{fmt(item.get('post_capture_apply_command'))}`",
                    f"- tokenomics_blocking: `{fmt(item.get('tokenomics_blocking'))}`",
                    f"- workbook_write_blocking: `{fmt(item.get('workbook_write_blocking'))}`",
                    f"- tokenomics_workbook_write_blocking: `{fmt(item.get('tokenomics_workbook_write_blocking'))}`",
                    "",
                ]
            )
    target_pdf_requirements = report.get("existing_har_target_month_pdf_recapture_requirements")
    if isinstance(target_pdf_requirements, list) and target_pdf_requirements:
        lines.extend(["## Existing HAR Target-Month PDF Recapture Requirements", ""])
        for index, item in enumerate(target_pdf_requirements, start=1):
            if not isinstance(item, dict):
                continue
            lines.extend(
                [
                    f"### {index}. {item.get('property') or 'unknown'}",
                    "",
                    f"- har_path: `{fmt(item.get('har_path'))}`",
                    f"- required_capture_quality: `{fmt(item.get('required_capture_quality'))}`",
                    f"- body_recapture_capture_method: `{fmt(item.get('body_recapture_capture_method'))}`",
                    f"- body_recapture_capture_instruction: `{fmt(item.get('body_recapture_capture_instruction'))}`",
                    f"- body_recapture_manual_har_export_warning: `{fmt(item.get('body_recapture_manual_har_export_warning'))}`",
                    f"- validation_command: `{fmt(item.get('validation_command'))}`",
                    f"- capture_command: `{fmt(item.get('capture_command'))}`",
                    f"- expected_document_ids: `{fmt(item.get('expected_document_ids'))}`",
                    f"- post_capture_apply_command: `{fmt(item.get('post_capture_apply_command'))}`",
                    f"- tokenomics_blocking: `{fmt(item.get('tokenomics_blocking'))}`",
                    f"- workbook_write_blocking: `{fmt(item.get('workbook_write_blocking'))}`",
                    f"- tokenomics_workbook_write_blocking: `{fmt(item.get('tokenomics_workbook_write_blocking'))}`",
                    "",
                ]
            )
    results = report.get("results")
    if isinstance(results, list) and results:
        lines.extend(["## Results", ""])
        for index, item in enumerate(results, start=1):
            if not isinstance(item, dict):
                continue
            lines.extend(
                [
                    f"### {index}. {item.get('property') or 'unknown'}",
                    "",
                    f"- status: `{fmt(item.get('status'))}`",
                    f"- skip_reason: `{fmt(item.get('skip_reason'))}`",
                    f"- reason: `{fmt(item.get('reason'))}`",
                    f"- har_path: `{fmt(item.get('har_path'))}`",
                    f"- har_path_exists_now: `{fmt(item.get('har_path_exists_now'))}`",
                    f"- har_entry_count: `{fmt(item.get('har_entry_count'))}`",
                    f"- safe_to_run_automatically: `{fmt(item.get('safe_to_run_automatically'))}`",
                    f"- capture_command_safe_to_run_automatically: `{fmt(item.get('capture_command_safe_to_run_automatically'))}`",
                    f"- tokenomics_blocking: `{fmt(item.get('tokenomics_blocking'))}`",
                    f"- workbook_write_blocking: `{fmt(item.get('workbook_write_blocking'))}`",
                    f"- tokenomics_workbook_write_blocking: `{fmt(item.get('tokenomics_workbook_write_blocking'))}`",
                    f"- tokenomics_write_blocker_count: `{fmt(item.get('tokenomics_write_blocker_count'))}`",
                    f"- tokenomics_write_blocker_reasons: `{fmt(item.get('tokenomics_write_blocker_reasons'))}`",
                    f"- tokenomics_write_blocker_statement_month_statuses: `{fmt(item.get('tokenomics_write_blocker_statement_month_statuses'))}`",
                    f"- advance_report: `{fmt(item.get('advance_report'))}`",
                    f"- advance_command: `{fmt(item.get('advance_command'))}`",
                    f"- durable_completion: `{fmt(item.get('durable_completion'))}`",
                    f"- pending_apply: `{fmt(item.get('pending_apply'))}`",
                    f"- capture_prepare_command: `{fmt(item.get('capture_prepare_command'))}`",
                    f"- capture_command: `{fmt(item.get('capture_command'))}`",
                    f"- expected_document_ids: `{fmt(item.get('expected_document_ids'))}`",
                    f"- post_capture_check_command: `{fmt(item.get('post_capture_check_command'))}`",
                    f"- post_capture_apply_command: `{fmt(item.get('post_capture_apply_command'))}`",
                    f"- body_recapture_operator_next_action: `{fmt(item.get('body_recapture_operator_next_action'))}`",
                    f"- body_recapture_capture_method: `{fmt(item.get('body_recapture_capture_method'))}`",
                    f"- body_recapture_capture_instruction: `{fmt(item.get('body_recapture_capture_instruction'))}`",
                    f"- body_recapture_manual_har_export_warning: `{fmt(item.get('body_recapture_manual_har_export_warning'))}`",
                    f"- body_recapture_recheck_response_body_requirement_progress: `{fmt(item.get('body_recapture_recheck_response_body_requirement_progress'))}`",
                    "",
                ]
            )
            child = item.get("child_report")
            if isinstance(child, dict):
                lines.extend(
                    [
                        f"- child_status: `{fmt(child.get('status'))}`",
                        f"- child_reason: `{fmt(child.get('reason'))}`",
                        f"- child_ok_count: `{fmt(child.get('ok_count'))}`",
                        f"- child_review_count: `{fmt(child.get('review_count'))}`",
                        "",
                    ]
                )
    else:
        lines.append("No capture queue entries were available for processing.")
        lines.append("")
    return "\n".join(lines)


def compact_child_report(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "unreadable", "reason": str(exc), "report": str(path)}
    if not isinstance(data, dict):
        return {"status": "unreadable", "reason": "child_report_not_object", "report": str(path)}
    results = data.get("results")
    compact_results = []
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            generated = item.get("generated_dry_run_report")
            generated = generated if isinstance(generated, dict) else {}
            enable = item.get("enable_verified_registry_entry_report")
            enable = enable if isinstance(enable, dict) else {}
            compact_results.append(
                {
                    "property": item.get("property"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                    "analysis_status": item.get("analysis_status"),
                    "analysis_reason": item.get("analysis_reason"),
                    "scaffold_status": item.get("scaffold_status"),
                    "registry_install_status": item.get("registry_install_status"),
                    "registry_install_reason": item.get("registry_install_reason"),
                    "generated_dry_run_status": generated.get("status"),
                    "generated_dry_run_reason": generated.get("reason"),
                    "generated_dry_run_verified_target_month": generated.get("verified_target_month"),
                    "enable_verified_registry_entry_status": enable.get("status"),
                    "enable_verified_registry_entry_reason": enable.get("reason"),
                    "enable_verified_registry_entry_config_written": enable.get("config_written"),
                }
            )
    return {
        "report": str(path),
        "status": data.get("status"),
        "reason": data.get("reason"),
        "property_count": data.get("property_count"),
        "ok_count": data.get("ok_count"),
        "review_count": data.get("review_count"),
        "apply_evidence": data.get("apply_evidence"),
        "apply_registry": data.get("apply_registry"),
        "enable_verified_registry_entry": data.get("enable_verified_registry_entry"),
        "results": compact_results,
    }


def body_recapture_recheck_item(entry: dict[str, Any], property_name: str) -> dict[str, Any]:
    return {
        "property": property_name,
        "servicer_hint": entry.get("servicer_hint"),
        "portal_url": entry.get("portal_url"),
        "target_statement_dir": entry.get("target_statement_dir"),
        "target_statement_month": entry.get("target_statement_month"),
    }


def safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def ordered_unique(values: list[Any]) -> list[Any]:
    result = []
    for value in values:
        if value in result:
            continue
        result.append(value)
    return result


def collect_list_values(items: list[dict[str, Any]], key: str) -> list[Any]:
    values: list[Any] = []
    for item in items:
        raw = item.get(key)
        if isinstance(raw, list):
            values.extend(raw)
        elif raw is not None:
            values.append(raw)
    return ordered_unique(values)


def is_target_month_pdf_recapture(item: dict[str, Any]) -> bool:
    return str(item.get("body_recapture_recheck_required_capture_quality") or "") == "target_month_statement_pdf"


def body_recapture_capture_method(required_quality: str, missing_paths: list[Any]) -> dict[str, Any]:
    paths = [str(item) for item in missing_paths if str(item or "").strip()]
    if not paths:
        return {}
    if required_quality == "target_month_statement_pdf":
        return {
            "body_recapture_capture_method": "visible_cdp_capture_helper_required",
            "body_recapture_capture_instruction": (
                "Run capture_command before opening/downloading the target statement PDF in the visible browser; "
                "the helper records Network.getResponseBody so binary PDF payloads are retained."
            ),
            "body_recapture_manual_har_export_warning": (
                "A browser DevTools HAR export may show HTTP 200 document responses while omitting content.text "
                "for application/octet-stream PDF bodies."
            ),
        }
    return {
        "body_recapture_capture_method": "visible_cdp_capture_helper_required",
        "body_recapture_capture_instruction": (
            "Run capture_command while repeating the portal workflow in the visible browser so required response "
            "bodies are retained."
        ),
    }


BODY_RECAPTURE_RECHECK_DETAIL_FIELDS = [
    "body_recapture_recheck_target_expected_document_response_body_candidate_count",
    "body_recapture_recheck_target_expected_document_missing_response_body_count",
    "body_recapture_recheck_response_body_requirement_count",
    "body_recapture_recheck_raw_missing_response_body_path_counts",
    "body_recapture_recheck_missing_response_body_path_counts",
    "body_recapture_recheck_statement_document_payload_months",
    "body_recapture_recheck_target_month_missing",
]


def body_recapture_recheck_details(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in BODY_RECAPTURE_RECHECK_DETAIL_FIELDS if key in item}


def first_present(item: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return None


WORKFLOW_HANDOFF_FIELDS = [
    "suggested_next_action",
    "capture_auth_blocker",
    "capture_auth_blocker_reason",
    "capture_auth_next_action",
    "capture_command_ready_to_run_now",
    "capture_command_blocked_reason",
    "capture_command_safe_to_run_automatically",
    "live_cdp_downloader_command",
    "live_cdp_downloader_ready_after_visible_auth",
    "source_downloader_id",
    "source_downloader_status",
    "source_downloader_report_status",
    "source_downloader_runtime_status",
    "source_downloader_manual_auth_required",
    "source_downloader_operator_next_action",
    "source_downloader_suggested_next_action",
    "credentials_available",
    "credential_source",
    "credential_lookup_status",
    "credential_lookup_failure_reason",
    "credential_lookup_item_name",
    "credential_lookup_expected_folder_name",
    "credential_lookup_uri_host",
    "credential_lookup_uri_host_aliases",
    "credential_lookup_search_terms",
    "credential_repair_instruction",
    "manual_auth_reason",
    "manual_auth_portal_url",
    "auth_failure_reason",
    "auth_failure_visible_reason",
    "auth_mfa_reached",
    "auth_issue",
    "auth_issue_text",
    "credential_login_failure_suspected",
    "credential_login_failure_suspected_reason",
    "credential_login_hint_mismatch",
    "credential_login_hint_mismatch_overridden",
    "auth_state",
    "auth_stage",
    "auto_login_attempted",
    "auto_login_status",
    "auto_login_blocked_reason",
    "auto_login_force_enabled",
    "auto_login_input_method",
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
    "register_workflow_evidence_ready_to_run_now",
    "advance_workflow_evidence_ready_to_run_now",
    "command_precondition_status",
    "command_precondition_reason",
    "command_precondition_next_action",
    "safe_to_run_automatically",
]


AUTH_BLOCKING_CONTEXT_FIELDS = {
    "capture_auth_blocker",
    "capture_auth_blocker_reason",
    "capture_auth_next_action",
    "capture_command_ready_to_run_now",
    "capture_command_blocked_reason",
    "source_downloader_manual_auth_required",
    "source_downloader_operator_next_action",
    "source_downloader_suggested_next_action",
    "manual_auth_reason",
    "manual_auth_portal_url",
    "auth_failure_reason",
    "auth_failure_visible_reason",
    "auth_mfa_reached",
    "auth_issue",
    "auth_issue_text",
    "credential_login_failure_suspected",
    "credential_login_failure_suspected_reason",
    "auth_state",
    "auth_stage",
    "auto_login_attempted",
    "auto_login_status",
    "auto_login_blocked_reason",
    "command_precondition_status",
    "command_precondition_reason",
    "command_precondition_next_action",
}


def workflow_handoff_fields(item: dict[str, Any]) -> dict[str, Any]:
    fields = {key: item.get(key) for key in WORKFLOW_HANDOFF_FIELDS if key in item}
    if "live_cdp_downloader_command" in fields:
        fields["live_cdp_downloader_command"] = normalize_live_cdp_downloader_command(
            fields["live_cdp_downloader_command"]
        )
    if item.get("capture_auth_blocker") is True:
        reason = (
            item.get("capture_auth_blocker_reason")
            or item.get("capture_command_blocked_reason")
            or item.get("command_precondition_reason")
            or "auth_blocked"
        )
        fields.setdefault("command_precondition_status", "blocked_by_auth")
        fields.setdefault("command_precondition_reason", reason)
        fields.setdefault(
            "command_precondition_next_action",
            (
                "Authenticate loanDepot in the visible browser tab, then rerun the live-CDP downloader profile."
                if reason == "visible_loandepot_tab_not_authenticated"
                else
                "Recover or unlock the MortgageQuestions account in the visible portal "
                "before capturing the target-month statement PDF."
                if reason == "account_locked"
                else
                "Verify the MortgageQuestions credentials in the visible portal or update the Ops Bitwarden item "
                "before rerunning; an OTP cannot be submitted until the portal reaches MFA."
                if reason == "login_still_required_after_submit"
                else
                "Recover or update the MortgageQuestions credentials in the visible portal before rerunning; "
                "an OTP cannot be submitted until the portal reaches MFA."
                if reason == "login_rejected"
                else "Resolve portal authentication before capturing the target-month statement PDF."
            ),
        )
    elif (
        "command_precondition_status" not in fields
        and (
            item.get("reason") == "har_missing"
            or item.get("skip_reason") == "har_not_captured_yet"
        )
    ):
        fields["command_precondition_status"] = "missing_workflow_har"
        fields["command_precondition_next_action"] = "place_har_at_suggested_workflow_har_path"
    return fields


def tokenomics_blocking_fields(item: dict[str, Any]) -> dict[str, Any]:
    tokenomics_blocking = first_present(
        item,
        [
            "tokenomics_blocking",
            "tokenomics_statement_gap_blocking",
            "tokenomics_workbook_write_blocking",
        ],
    )
    workbook_write_blocking = first_present(
        item,
        [
            "workbook_write_blocking",
            "tokenomics_workbook_write_blocking",
        ],
    )
    tokenomics_workbook_write_blocking = first_present(
        item,
        [
            "tokenomics_workbook_write_blocking",
            "workbook_write_blocking",
        ],
    )
    return {
        "tokenomics_blocking": tokenomics_blocking,
        "workbook_write_blocking": workbook_write_blocking,
        "tokenomics_workbook_write_blocking": tokenomics_workbook_write_blocking,
    }


def auth_blocker_operator_next_action(item: dict[str, Any]) -> str | None:
    if (
        item.get("capture_auth_blocker") is not True
        and item.get("command_precondition_status") != "blocked_by_auth"
    ):
        return None
    for key in (
        "suggested_next_action",
        "source_downloader_operator_next_action",
        "source_downloader_suggested_next_action",
    ):
        value = str(item.get(key) or "").strip()
        if value and " " not in value and value != "capture_target_month_statement":
            return value
    reason = str(item.get("capture_auth_blocker_reason") or item.get("command_precondition_reason") or "").strip()
    if reason == "visible_loandepot_tab_not_authenticated":
        return "authenticate_visible_loandepot_tab_then_run_live_cdp"
    if reason:
        return "repair_portal_auth_before_capture"
    return None


def is_auth_blocked_before_capture(item: dict[str, Any]) -> bool:
    if item.get("capture_auth_blocker") is True:
        return True
    if item.get("command_precondition_status") == "blocked_by_auth":
        return True
    if item.get("capture_command_ready_to_run_now") is False and (
        item.get("capture_command_blocked_reason")
        or item.get("capture_auth_blocker_reason")
        or item.get("auth_issue")
    ):
        return True
    return False


def operator_next_action_item(item: dict[str, Any]) -> dict[str, Any] | None:
    property_name = str(item.get("property") or "").strip()
    if not property_name:
        return None
    skip_reason = str(item.get("skip_reason") or "").strip()
    if skip_reason == "auth_blocked_before_capture":
        return {
            "property": property_name,
            "next_action": auth_blocker_operator_next_action(item) or "repair_portal_auth_before_capture",
            "reason": item.get("reason"),
            "har_path": item.get("har_path"),
            "capture_prepare_command": item.get("capture_prepare_command"),
            "capture_command": item.get("capture_command"),
            "expected_document_ids": item.get("expected_document_ids"),
            "target_month_document_identifiers": item.get("target_month_document_identifiers"),
            "post_capture_check_command": item.get("post_capture_check_command"),
            "post_capture_apply_command": item.get("post_capture_apply_command"),
            "post_capture_apply_command_safe_after_har_capture": item.get(
                "post_capture_apply_command_safe_after_har_capture"
            ),
            **workflow_handoff_fields(item),
            **tokenomics_blocking_fields(item),
            "tokenomics_write_blocker_reasons": item.get("tokenomics_write_blocker_reasons"),
        }
    if skip_reason == "har_not_captured_yet":
        return {
            "property": property_name,
            "next_action": "capture_or_place_workflow_har",
            "reason": item.get("reason"),
            "har_path": item.get("har_path"),
            "capture_prepare_command": item.get("capture_prepare_command"),
            "capture_command": item.get("capture_command"),
            "expected_document_ids": item.get("expected_document_ids"),
            "target_month_document_identifiers": item.get("target_month_document_identifiers"),
            "post_capture_check_command": item.get("post_capture_check_command"),
            "post_capture_apply_command": item.get("post_capture_apply_command"),
            "post_capture_apply_command_safe_after_har_capture": item.get(
                "post_capture_apply_command_safe_after_har_capture"
            ),
            **workflow_handoff_fields(item),
            **tokenomics_blocking_fields(item),
            "tokenomics_write_blocker_reasons": item.get("tokenomics_write_blocker_reasons"),
        }
    if skip_reason == "existing_har_needs_body_recapture":
        required_quality = str(item.get("body_recapture_recheck_required_capture_quality") or "")
        missing_paths = safe_list(item.get("body_recapture_recheck_missing_response_body_paths"))
        default_next_action = (
            "recapture_har_with_target_month_statement_pdf"
            if required_quality == "target_month_statement_pdf"
            else "recapture_har_with_full_response_bodies"
        )
        auth_next_action = auth_blocker_operator_next_action(item)
        return {
            "property": property_name,
            "next_action": auth_next_action or item.get("body_recapture_operator_next_action") or default_next_action,
            "reason": item.get("reason"),
            "har_path": item.get("har_path"),
            "capture_prepare_command": item.get("capture_prepare_command"),
            "capture_command": item.get("capture_command"),
            "expected_document_ids": item.get("expected_document_ids"),
            "target_month_document_identifiers": item.get("target_month_document_identifiers"),
            "post_capture_check_command": item.get("post_capture_check_command"),
            "post_capture_apply_command": item.get("post_capture_apply_command"),
            "post_capture_apply_command_safe_after_har_capture": item.get(
                "post_capture_apply_command_safe_after_har_capture"
            ),
            "missing_response_body_paths": missing_paths,
            "response_body_requirement_progress": item.get("body_recapture_recheck_response_body_requirement_progress"),
            **body_recapture_recheck_details(item),
            **body_recapture_capture_method(required_quality, missing_paths),
            **workflow_handoff_fields(item),
            **tokenomics_blocking_fields(item),
        }
    if item.get("pending_apply") is True:
        return {
            "property": property_name,
            "next_action": "rerun_processor_with_apply",
            "reason": "ready_capture_processing_pending_apply",
            "har_path": item.get("har_path"),
            "advance_report": item.get("advance_report"),
            "post_capture_apply_command": item.get("post_capture_apply_command"),
            "post_capture_apply_command_safe_after_har_capture": True,
            "apply": True,
            **workflow_handoff_fields(item),
            **tokenomics_blocking_fields(item),
        }
    if item.get("status") == "review":
        return {
            "property": property_name,
            "next_action": "review_capture_processing_result",
            "reason": item.get("skip_reason") or item.get("reason"),
            "har_path": item.get("har_path"),
            "advance_report": item.get("advance_report"),
            "post_capture_apply_command": item.get("post_capture_apply_command"),
            **workflow_handoff_fields(item),
            **tokenomics_blocking_fields(item),
        }
    return None


def body_recapture_ready(entry: dict[str, Any], property_name: str, har_path: Path) -> dict[str, Any]:
    analysis = analyzer.analyze_har(
        har_path,
        property_name=property_name,
        item=body_recapture_recheck_item(entry, property_name),
        selection_reason="body_recapture_recheck",
        target_month=str(entry.get("target_statement_month") or "") or None,
        expected_document_identifiers=[
            str(item)
            for item in safe_list(entry.get("expected_document_ids") or entry.get("target_month_document_identifiers"))
            if str(item or "").strip()
        ],
    )
    target_requirements = safe_list(analysis.get("target_expected_document_response_body_requirements"))
    target_progress = safe_list(analysis.get("target_expected_document_response_body_requirement_progress"))
    target_missing_paths = safe_list(analysis.get("target_expected_document_missing_response_body_paths"))
    use_target_specific_requirements = bool(
        entry.get("expected_document_ids")
        and analysis.get("target_expected_document_response_body_candidate_count") not in (None, 0)
    )
    requirements = target_requirements if use_target_specific_requirements else safe_list(analysis.get("response_body_requirements"))
    progress = target_progress if use_target_specific_requirements else safe_list(analysis.get("response_body_requirement_progress"))
    missing_paths = target_missing_paths if use_target_specific_requirements else safe_list(analysis.get("missing_response_body_paths"))
    target_month = str(entry.get("target_statement_month") or "").strip()
    entry_required_quality = str(entry.get("required_capture_quality") or "")
    payload_months = {str(item) for item in safe_list(analysis.get("statement_document_payload_months"))}
    target_month_missing = (
        entry_required_quality == "target_month_statement_pdf"
        and bool(target_month)
        and target_month not in payload_months
    )
    promoted = analysis.get("status") == "ok" and not target_month_missing
    required_quality = str(analysis.get("required_capture_quality") or "")
    if target_month_missing:
        required_quality = "target_month_statement_pdf"
    recapture_action = (
        "recapture_har_with_target_month_statement_pdf"
        if required_quality == "target_month_statement_pdf"
        else "recapture_har_with_full_response_bodies"
    )
    return {
        "body_recapture_recheck_status": analysis.get("status"),
        "body_recapture_recheck_reason": "target_month_statement_unavailable"
        if target_month_missing
        else analysis.get("reason"),
        "body_recapture_recheck_candidate_endpoint_count": analysis.get("candidate_endpoint_count"),
        "body_recapture_recheck_actionable_missing_response_body_count": analysis.get(
            "actionable_missing_response_body_count"
        ),
        "body_recapture_recheck_missing_response_body_paths": missing_paths,
        "body_recapture_recheck_missing_response_body_path_counts": analysis.get(
            "target_expected_document_missing_response_body_path_counts"
            if use_target_specific_requirements
            else "missing_response_body_path_counts"
        ),
        "body_recapture_recheck_raw_missing_response_body_path_counts": analysis.get(
            "missing_response_body_path_counts"
        ),
        "body_recapture_recheck_target_expected_document_response_body_candidate_count": analysis.get(
            "target_expected_document_response_body_candidate_count"
        ),
        "body_recapture_recheck_target_expected_document_missing_response_body_count": analysis.get(
            "target_expected_document_missing_response_body_count"
        ),
        "body_recapture_recheck_response_body_requirement_count": len(requirements),
        "body_recapture_recheck_response_body_requirements": requirements,
        "body_recapture_recheck_response_body_requirement_progress": progress,
        "body_recapture_recheck_required_capture_quality": required_quality or analysis.get("required_capture_quality"),
        "body_recapture_recheck_target_statement_month": target_month or None,
        "body_recapture_recheck_expected_document_ids": entry.get("expected_document_ids"),
        "body_recapture_recheck_target_month_document_identifiers": entry.get("target_month_document_identifiers"),
        "body_recapture_recheck_statement_document_payload_months": sorted(payload_months),
        "body_recapture_recheck_target_month_missing": target_month_missing,
        "body_recapture_recheck_promoted": promoted,
        "body_recapture_validation_command": entry.get("post_capture_check_command"),
        "body_recapture_advance_command": entry.get("advance_workflow_evidence_command"),
        "body_recapture_advance_apply_command": entry.get("advance_workflow_evidence_apply_command"),
        "body_recapture_operator_next_action": (
            "run_post_capture_check_then_advance_workflow_evidence"
            if promoted
            else recapture_action
        ),
        **body_recapture_capture_method(required_quality, missing_paths),
    }


def process_entry(entry: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    property_name = str(entry.get("property") or "").strip()
    har_path_text = str(entry.get("suggested_workflow_har_path") or "").strip()
    reason = str(entry.get("reason") or "").strip()
    result: dict[str, Any] = {
        "property": property_name,
        "capture_identity": entry.get("capture_identity"),
        "reason": reason or None,
        "har_path": har_path_text or None,
        "suggested_workflow_har_path": har_path_text or None,
        "capture_command": entry.get("capture_command"),
        "capture_prepare_command": entry.get("capture_prepare_command"),
        "expected_document_ids": entry.get("expected_document_ids"),
        "target_month_document_identifiers": entry.get("target_month_document_identifiers"),
        "capture_command_ready_to_run_now": entry.get("capture_command_ready_to_run_now"),
        "capture_command_safe_to_run_automatically": entry.get("capture_command_safe_to_run_automatically"),
        "post_capture_check_command": entry.get("post_capture_check_command"),
        "post_capture_apply_command": None,
        "post_capture_apply_command_safe_after_har_capture": False,
        **workflow_handoff_fields(entry),
        **tokenomics_blocking_fields(entry),
        "tokenomics_write_blocker_count": entry.get("tokenomics_write_blocker_count"),
        "tokenomics_write_blocker_reasons": safe_list(entry.get("tokenomics_write_blocker_reasons")),
        "tokenomics_write_blocker_statement_month_statuses": safe_list(
            entry.get("tokenomics_write_blocker_statement_month_statuses")
        ),
        "status": "skipped",
        "skip_reason": None,
        "safe_to_run_automatically": queued_entry_safe_to_run_automatically(entry),
    }
    if not property_name:
        result.update(skip_reason="property_missing")
        return result
    result["post_capture_apply_command"] = command_text(
        processor_command(
            args,
            property_name=property_name,
            apply=True,
            process_existing_body_recaptures=reason in BODY_RECAPTURE_REASONS,
            target_month=str(entry.get("target_statement_month") or "").strip() or None,
        )
    )
    if is_auth_blocked_before_capture(result):
        result.update(
            skip_reason="auth_blocked_before_capture",
            post_capture_apply_command_safe_after_har_capture=False,
        )
        return result
    if not har_path_text:
        result.update(skip_reason="har_path_missing")
        return result
    har_path = Path(har_path_text)
    result.update(
        {
            "har_path_exists_now": har_path.exists(),
            "har_path_size_now": har_path.stat().st_size if har_path.exists() else None,
        }
    )
    if not har_path.exists():
        result.update(
            skip_reason="har_not_captured_yet",
            post_capture_apply_command_safe_after_har_capture=True,
        )
        return result
    if har_path.stat().st_size <= 0:
        result.update(status="review", skip_reason="har_empty")
        return result
    har_valid, har_invalid_reason, har_entry_count = validate_har_file(har_path)
    result["har_entry_count"] = har_entry_count
    if not har_valid:
        result.update(status="review", skip_reason=har_invalid_reason or "har_invalid")
        return result
    if reason in BODY_RECAPTURE_REASONS:
        recheck = body_recapture_ready(entry, property_name, har_path)
        result.update(recheck)
        if not recheck["body_recapture_recheck_promoted"]:
            result.update(
                skip_reason="existing_har_needs_body_recapture",
                post_capture_apply_command_safe_after_har_capture=True,
            )
            return result
        if not args.process_existing_body_recaptures:
            result.update(
                skip_reason="existing_har_needs_body_recapture",
                post_capture_apply_command_safe_after_har_capture=True,
            )
            return result

    report_path = item_report_path(args.report_dir, property_name)
    command = advance_command(
        advance_script=args.advance_script,
        property_name=property_name,
        har_path=har_path,
        intake=args.intake,
        registry=args.registry,
        coverage_report=args.coverage_report,
        report_dir=args.report_dir,
        workspace_root=args.workspace_root,
        report=report_path,
        target_month=str(entry.get("target_statement_month") or args.target_month or "").strip() or None,
        apply=args.apply,
        enable_verified_registry_entry=args.enable_verified_registry_entry,
    )
    result.update(
        {
            "status": "running",
            "advance_report": str(report_path),
            "advance_command": command_text(command),
            "apply": args.apply,
            "safe_to_run_automatically": True,
            "post_capture_apply_command_safe_after_har_capture": True,
        }
    )
    proc = subprocess.run(
        command,
        cwd=str(args.workspace_root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=args.timeout_seconds,
    )
    child = compact_child_report(report_path)
    child_status = child.get("status") if isinstance(child, dict) else None
    result.update(
        {
            "rc": proc.returncode,
            "status": "ok" if proc.returncode == 0 and child_status == "ok" else "review",
            "child_report": child,
        }
    )
    child_apply_evidence = child.get("apply_evidence") if isinstance(child, dict) else None
    child_apply_registry = child.get("apply_registry") if isinstance(child, dict) else None
    durable_completion = (
        result.get("status") == "ok"
        and args.apply
        and child_apply_evidence is True
        and child_apply_registry is True
    )
    result["durable_completion"] = durable_completion
    result["pending_apply"] = result.get("status") == "ok" and not durable_completion
    if proc.returncode != 0:
        result["stderr_tail"] = tail(proc.stderr)
        result["stdout_tail"] = tail(proc.stdout)
    return result


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    queue = load_json(args.queue)
    entries = entries_from_queue(queue, args.property)
    coverage_context = coverage_context_by_property(load_json(args.coverage_report))
    entries = [
        merge_coverage_context_into_entry(
            entry,
            coverage_context.get(coverage.normalize_property(entry.get("property")), {}),
        )
        for entry in entries
    ]
    processed: list[dict[str, Any]] = []
    for entry in entries:
        processed.append(process_entry(entry, args))

    ran = [item for item in processed if item.get("rc") is not None]
    skipped = [item for item in processed if item.get("status") == "skipped"]
    review = [item for item in processed if item.get("status") == "review"]
    ok = [item for item in processed if item.get("status") == "ok"]
    durable = [item for item in ok if item.get("durable_completion") is True]
    pending_apply = [item for item in ok if item.get("pending_apply") is True]
    applied = [item for item in ran if item.get("apply") is True]
    dry_run_processed = [item for item in ran if item.get("apply") is not True]
    missing = [item for item in skipped if item.get("skip_reason") == "har_not_captured_yet"]
    existing_har_recaptures = [
        item for item in skipped if item.get("skip_reason") == "existing_har_needs_body_recapture"
    ]
    auth_blocked_before_capture = [
        item for item in skipped if item.get("skip_reason") == "auth_blocked_before_capture"
    ]
    target_month_pdf_recaptures = [
        item for item in existing_har_recaptures if is_target_month_pdf_recapture(item)
    ]
    body_recaptures = [
        item for item in existing_har_recaptures if not is_target_month_pdf_recapture(item)
    ]
    existing_har_recapture_requirements = [
        {
            "property": item.get("property"),
            "har_path": item.get("har_path"),
            "required_capture_quality": item.get("body_recapture_recheck_required_capture_quality"),
            "missing_response_body_paths": item.get("body_recapture_recheck_missing_response_body_paths") or [],
            "response_body_requirements": item.get("body_recapture_recheck_response_body_requirements") or [],
            "response_body_requirement_progress": item.get(
                "body_recapture_recheck_response_body_requirement_progress"
            )
            or [],
            **body_recapture_recheck_details(item),
            "validation_command": item.get("body_recapture_validation_command"),
            "capture_command": item.get("capture_command"),
            "expected_document_ids": item.get("expected_document_ids"),
            "target_month_document_identifiers": item.get("target_month_document_identifiers"),
            "post_capture_apply_command": item.get("post_capture_apply_command"),
            "body_recapture_capture_method": item.get("body_recapture_capture_method"),
            "body_recapture_capture_instruction": item.get("body_recapture_capture_instruction"),
            "body_recapture_manual_har_export_warning": item.get("body_recapture_manual_har_export_warning"),
            **workflow_handoff_fields(item),
            **tokenomics_blocking_fields(item),
        }
        for item in existing_har_recaptures
    ]
    auth_blocked_capture_requirements = [
        {
            "property": item.get("property"),
            "har_path": item.get("har_path"),
            "reason": item.get("reason"),
            "capture_command": item.get("capture_command"),
            "capture_prepare_command": item.get("capture_prepare_command"),
            "expected_document_ids": item.get("expected_document_ids"),
            "target_month_document_identifiers": item.get("target_month_document_identifiers"),
            "post_capture_check_command": item.get("post_capture_check_command"),
            "post_capture_apply_command": item.get("post_capture_apply_command"),
            **workflow_handoff_fields(item),
            **tokenomics_blocking_fields(item),
        }
        for item in auth_blocked_before_capture
    ]
    queue_blockers = [
        item
        for item in skipped
        if item.get("skip_reason")
        in {"auth_blocked_before_capture", "har_not_captured_yet", "existing_har_needs_body_recapture"}
    ]
    queue_blocker_properties = [
        item.get("property")
        for item in queue_blockers
        if str(item.get("property") or "").strip()
    ]
    tokenomics_blockers = [
        item
        for item in processed
        if item.get("tokenomics_workbook_write_blocking") is True
    ]
    tokenomics_queue_blockers = [
        item
        for item in queue_blockers
        if item.get("tokenomics_workbook_write_blocking") is True
    ]
    tokenomics_blocker_properties = ordered_unique(
        [
            item.get("property")
            for item in tokenomics_blockers
            if str(item.get("property") or "").strip()
        ]
    )
    tokenomics_queue_blocker_properties = ordered_unique(
        [
            item.get("property")
            for item in tokenomics_queue_blockers
            if str(item.get("property") or "").strip()
        ]
    )
    invalid_hars = [
        item
        for item in review
        if str(item.get("skip_reason") or "").startswith("har_")
        or item.get("skip_reason") in {"har_empty"}
    ]
    operator_next_actions = [
        action
        for action in (operator_next_action_item(item) for item in processed)
        if action
    ]
    status = "review" if review or pending_apply or queue_blockers else "ok"
    reason = None
    if review:
        reason = "ready_capture_processing_review"
    elif pending_apply:
        reason = "ready_capture_processing_pending_apply"
    elif not ran and queue_blockers:
        reason = "waiting_for_capture_queue_blockers"
    elif not ran:
        reason = "no_ready_captured_hars"
    report = {
        "job": "process-mortgage-capture-queue",
        "generated_at": utc_now(),
        "queue": str(args.queue),
        "queue_exists": args.queue.exists(),
        "markdown": str(args.markdown),
        "apply": args.apply,
        "process_existing_body_recaptures": args.process_existing_body_recaptures,
        "enable_verified_registry_entry": args.enable_verified_registry_entry,
        "property_filter": args.property,
        "entry_count": len(entries),
        "processed_count": len(ran),
        "applied_count": len(applied),
        "dry_run_processed_count": len(dry_run_processed),
        "pending_apply_count": len(pending_apply),
        "pending_apply_properties": [item.get("property") for item in pending_apply],
        "durable_completion_count": len(durable),
        "durable_completion_properties": [item.get("property") for item in durable],
        "ok_count": len(ok),
        "review_count": len(review),
        "skipped_count": len(skipped),
        "missing_har_count": len(missing),
        "missing_har_properties": [item.get("property") for item in missing],
        "invalid_har_count": len(invalid_hars),
        "invalid_har_properties": [item.get("property") for item in invalid_hars],
        "auth_blocked_before_capture_count": len(auth_blocked_before_capture),
        "auth_blocked_before_capture_properties": [
            item.get("property") for item in auth_blocked_before_capture
        ],
        "auth_blocked_capture_requirements": auth_blocked_capture_requirements,
        "existing_har_recapture_skipped_count": len(existing_har_recaptures),
        "existing_har_recapture_skipped_properties": [
            item.get("property") for item in existing_har_recaptures
        ],
        "existing_har_recapture_requirements": existing_har_recapture_requirements,
        "existing_har_target_month_pdf_recapture_skipped_count": len(target_month_pdf_recaptures),
        "existing_har_target_month_pdf_recapture_skipped_properties": [
            item.get("property") for item in target_month_pdf_recaptures
        ],
        "existing_har_target_month_pdf_recapture_requirements": [
            item
            for item in existing_har_recapture_requirements
            if str(item.get("required_capture_quality") or "") == "target_month_statement_pdf"
        ],
        "existing_body_recapture_skipped_count": len(existing_har_recaptures),
        "existing_body_recapture_skipped_properties": [
            item.get("property") for item in existing_har_recaptures
        ],
        "existing_body_recapture_requirements": existing_har_recapture_requirements,
        "tokenomics_workbook_write_blocker_count": len(tokenomics_blocker_properties),
        "tokenomics_workbook_write_blocker_properties": tokenomics_blocker_properties,
        "tokenomics_workbook_write_blocker_reasons": collect_list_values(
            tokenomics_blockers,
            "tokenomics_write_blocker_reasons",
        ),
        "tokenomics_workbook_write_blocker_statement_month_statuses": collect_list_values(
            tokenomics_blockers,
            "tokenomics_write_blocker_statement_month_statuses",
        ),
        "tokenomics_workbook_write_blocking_count": len(tokenomics_blocker_properties),
        "tokenomics_workbook_write_blocking_properties": tokenomics_blocker_properties,
        "tokenomics_workbook_write_blocking_reasons": collect_list_values(
            tokenomics_blockers,
            "tokenomics_write_blocker_reasons",
        ),
        "tokenomics_workbook_write_blocking_statement_month_statuses": collect_list_values(
            tokenomics_blockers,
            "tokenomics_write_blocker_statement_month_statuses",
        ),
        "tokenomics_queue_blocker_count": len(tokenomics_queue_blocker_properties),
        "tokenomics_queue_blocker_properties": tokenomics_queue_blocker_properties,
        "queue_blocker_status": "review" if queue_blockers else "ok",
        "queue_blocker_reason": "capture_required_before_processing" if queue_blockers else None,
        "queue_blocker_count": len(queue_blockers),
        "queue_blocker_properties": queue_blocker_properties,
        "operator_next_action_count": len(operator_next_actions),
        "operator_next_action_properties": [
            item.get("property")
            for item in operator_next_actions
            if str(item.get("property") or "").strip()
        ],
        "operator_next_actions": operator_next_actions,
        "status": status,
        "reason": reason,
        "safe_to_run_automatically": not review and not queue_blockers and not pending_apply,
        "results": processed,
    }
    report["idempotency_digest"] = stable_report_digest(report)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--advance-script", type=Path, default=DEFAULT_ADVANCE_SCRIPT)
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--coverage-report", type=Path, default=DEFAULT_COVERAGE_REPORT)
    parser.add_argument("--workspace-root", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument("--target-month")
    parser.add_argument("--property", action="append", default=[], help="Limit to a queued property; repeatable")
    parser.add_argument("--apply", action="store_true", help="Apply evidence and registry updates")
    parser.add_argument(
        "--process-existing-body-recaptures",
        action="store_true",
        help="Also process queued HARs whose current blocker is missing response bodies.",
    )
    parser.add_argument(
        "--enable-verified-registry-entry",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When applying, enable generated registry entries only after target-month verification.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=180)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    report = build_report(args)
    report = write_json_report(args.report, report)
    write_text_if_changed(args.markdown, render_markdown(report))
    print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
    if report.get("status") == "ok" or report.get("reason") == "waiting_for_capture_queue_blockers":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
