#!/usr/bin/env python3
"""Roll up mortgage downloader replayability evidence into one offline gate."""

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

from stable_json_report import write_json_report

SCRIPT_PATH = Path(__file__).absolute()
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", SCRIPT_PATH.parents[1]))
DEFAULT_REPORT_DIR = WORKSPACE_ROOT / "reports"
DEFAULT_COVERAGE_REPORT = DEFAULT_REPORT_DIR / "mortgage_downloader_coverage_report.json"
DEFAULT_CITADEL_DIAGNOSTICS_REPORT = DEFAULT_REPORT_DIR / "citadel_har_workflow_diagnostics_report.json"
DEFAULT_EVIDENCE_REFRESH_REPORT = DEFAULT_REPORT_DIR / "mortgage_workflow_evidence_refresh_report.json"
DEFAULT_CAPTURE_QUEUE_PROCESSOR_REPORT = DEFAULT_REPORT_DIR / "mortgage_workflow_capture_queue_processor.json"
DEFAULT_REPORT = DEFAULT_REPORT_DIR / "mortgage_workflow_replayability_gate_report.json"
DEFAULT_SCAFFOLD_DIR_NAME = "mortgage_downloader_scaffolds"
DEFAULT_STUB_DIR = WORKSPACE_ROOT / "scripts" / "generated_mortgage_downloaders"


def slugify(value: object) -> str:
    text = " ".join(str(value or "").strip().casefold().split())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "unknown"


def scaffold_paths(path: Path, property_name: object) -> dict[str, str]:
    prop_slug = slugify(property_name) if str(property_name or "").strip() else slugify(path.stem)
    report_dir = path.parent
    scaffold_dir = report_dir / DEFAULT_SCAFFOLD_DIR_NAME
    return {
        "scaffold_report": str(report_dir / f"mortgage_workflow_downloader_scaffold_{prop_slug}.json"),
        "scaffold_output_dir": str(scaffold_dir),
        "stub_dir": str(DEFAULT_STUB_DIR),
        "manifest_path": str(scaffold_dir / f"{prop_slug}_downloader_scaffold_manifest.json"),
        "registry_entry_path": str(scaffold_dir / f"{prop_slug}_mortgage_statement_downloader_registry_entry.json"),
        "stub_path": str(DEFAULT_STUB_DIR / f"download_{prop_slug}_statements.py"),
    }


def scaffold_command(path: Path, paths: dict[str, str]) -> str:
    return (
        "python3 scripts/mortgage_workflow_downloader_scaffold.py "
        f"--analysis-report {shlex.quote(str(path))} "
        f"--output-dir {shlex.quote(paths['scaffold_output_dir'])} "
        f"--stub-dir {shlex.quote(paths['stub_dir'])} "
        f"--write-stub "
        f"--report {shlex.quote(paths['scaffold_report'])}"
    )


def registry_install_command(paths: dict[str, str]) -> str:
    return (
        "python3 scripts/register_mortgage_downloader_registry_entry.py "
        f"--entry {shlex.quote(paths['registry_entry_path'])} "
        f"--config {shlex.quote(str(WORKSPACE_ROOT / 'config' / 'mortgage_statement_downloaders.json'))} "
        "--apply"
    )


def register_workflow_evidence_command(property_name: object, har_path: object, *, apply: bool = False) -> str | None:
    prop = str(property_name or "").strip()
    har = str(har_path or "").strip()
    if not prop or not har:
        return None
    command = (
        "python3 scripts/register_mortgage_workflow_evidence.py "
        f"--property {shlex.quote(prop)} "
        f"--har {shlex.quote(har)} "
        f"--intake {shlex.quote(str(WORKSPACE_ROOT / 'config' / 'mortgage_downloader_intake.json'))} "
        f"--report-dir {shlex.quote(str(DEFAULT_REPORT_DIR))}"
    )
    if apply:
        return f"{command} --apply"
    return command


def generated_downloader_command(stub_path: object, *, apply: bool = False) -> str | None:
    text = str(stub_path or "").strip()
    if not text:
        return None
    command = f"python3 {shlex.quote(text)}"
    if apply:
        return f"MORTGAGE_GENERATED_HAR_DOWNLOADER_APPLY=1 {command}"
    return command


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "JSON root is not an object"
    return data, None


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def append_action(actions: list[dict[str, Any]], item: dict[str, Any]) -> None:
    actions.append({key: value for key, value in item.items() if value is not None})


def unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


AUTH_BLOCKER_REASONS = {
    "account_locked",
    "password_reset_in_progress",
    "authenticated_browser_required",
    "credential_rejected_before_mfa",
    "credentials_unavailable",
    "login_rejected",
    "login_still_required_after_submit",
    "visible_loandepot_tab_not_authenticated",
}


CREDENTIAL_CONTEXT_FIELDS = (
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
    "credential_login_hint_mismatch",
    "credential_login_hint_mismatch_overridden",
    "manual_auth_reason",
    "auth_issue",
    "operator_next_action",
    "portal_auth_repair_action",
    "portal_auth_repair_validation_command",
    "source_downloader_id",
    "source_downloader_report_status",
    "source_downloader_manual_auth_required",
    "source_downloader_operator_next_action",
    "source_downloader_suggested_next_action",
)


def loandepot_context(item: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(item.get(key) or "")
        for key in (
            "property",
            "servicer_hint",
            "reason",
            "manual_auth_reason",
            "credential_lookup_failure_reason",
            "credential_lookup_item_name",
            "credential_lookup_uri_host",
            "source_downloader_id",
            "source_downloader_operator_next_action",
            "source_downloader_suggested_next_action",
            "capture_command",
            "next_command",
            "suggested_workflow_har_path",
            "har_path",
        )
    ).casefold()
    return "loandepot" in haystack


def credential_context(item: dict[str, Any]) -> dict[str, Any]:
    context = {key: item.get(key) for key in CREDENTIAL_CONTEXT_FIELDS if key in item}
    if item.get("credential_lookup_status") == "failed":
        context.setdefault("credentials_available", False)
        context.setdefault("credential_source", "unavailable")
        context.setdefault(
            "manual_auth_reason",
            "loandepot_credentials_unavailable" if loandepot_context(item) else "credentials_unavailable",
        )
    return context


def auth_blocker_reason(item: dict[str, Any]) -> str | None:
    for key in (
        "capture_auth_blocker_reason",
        "capture_command_blocked_reason",
        "capture_precondition_blocker",
        "command_precondition_reason",
        "live_auth_blocker",
        "reason",
    ):
        if key == "capture_auth_blocker_reason" and item.get("capture_auth_blocker") is False:
            continue
        value = str(item.get(key) or "").strip()
        if value in AUTH_BLOCKER_REASONS:
            return value
    for value in item.get("reasons") or []:
        text = str(value or "").strip()
        if text in AUTH_BLOCKER_REASONS:
            return text
    if item.get("capture_auth_blocker") is True:
        return str(item.get("reason") or "authenticated_browser_required")
    return None


GENERIC_CAPTURE_ACTIONS = {
    "capture_full_response_bodies",
    "capture_target_month_statement",
    "capture_target_month_statement_pdf",
    "recapture_target_month_statement_pdf",
    "capture_workflow_har_with_target_month_statement_pdf",
    "capture_workflow_har_with_full_response_bodies",
}


def auth_blocker_suggested_next_action(item: dict[str, Any], reason: str) -> str:
    precondition_next_action = str(item.get("capture_precondition_next_action") or "").strip()
    if (
        precondition_next_action
        and precondition_next_action not in GENERIC_CAPTURE_ACTIONS
        and re.fullmatch(r"[a-z0-9_]+", precondition_next_action)
    ):
        return precondition_next_action
    command_next_action = str(item.get("command_precondition_next_action") or "").strip()
    if (
        command_next_action
        and command_next_action not in GENERIC_CAPTURE_ACTIONS
        and re.fullmatch(r"[a-z0-9_]+", command_next_action)
    ):
        return command_next_action
    for key in ("operator_next_action", "source_downloader_operator_next_action", "suggested_next_action"):
        value = str(item.get(key) or "").strip()
        if value and value not in GENERIC_CAPTURE_ACTIONS and re.fullmatch(r"[a-z0-9_]+", value):
            return value
    haystack = " ".join(
        str(item.get(key) or "")
        for key in (
            "property",
            "servicer_hint",
            "capture_command",
            "next_command",
            "suggested_workflow_har_path",
            "har_path",
        )
    ).casefold()
    if reason == "visible_loandepot_tab_not_authenticated" or "loandepot" in haystack:
        return "authenticate_visible_loandepot_tab_then_run_live_cdp"
    return "repair_portal_auth_before_capture"


def has_target_month_statement_context(item: dict[str, Any]) -> bool:
    for key in (
        "target_month_document_identifiers",
        "expected_document_ids",
        "target_month_statement_candidates",
    ):
        if string_list(item.get(key)):
            return True
    if str(item.get("required_capture_quality") or "").strip() == "target_month_statement_pdf":
        return True
    return False


def propagate_auth_blockers(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    auth_context_by_property: dict[str, dict[str, Any]] = {}
    for item in actions:
        prop_key = slugify(item.get("property"))
        reason = auth_blocker_reason(item)
        if not prop_key or not reason:
            continue
        context = auth_context_by_property.setdefault(
            prop_key,
            {
                "reason": reason,
                "sources": [],
            },
        )
        context["sources"] = unique_strings([*(context.get("sources") or []), item.get("source")])

    if not auth_context_by_property:
        return actions

    for item in actions:
        prop_key = slugify(item.get("property"))
        context = auth_context_by_property.get(prop_key)
        if not context:
            continue
        reason = str(context.get("reason") or "authenticated_browser_required")
        next_action = auth_blocker_suggested_next_action(item, reason)
        item["capture_auth_blocker"] = True
        item["capture_auth_blocker_reason"] = reason
        item["capture_auth_blocker_sources"] = context.get("sources") or []
        item["capture_command_ready_to_run_now"] = False
        item["register_workflow_evidence_ready_to_run_now"] = False
        item["advance_workflow_evidence_ready_to_run_now"] = False
        item["safe_to_run_automatically"] = False
        if not item.get("capture_command_blocked_reason"):
            item["capture_command_blocked_reason"] = reason
        if not item.get("command_precondition_reason"):
            item["command_precondition_reason"] = reason
        if item.get("command_precondition_status") in (None, "", "ready"):
            item["command_precondition_status"] = "blocked_by_auth"
        item["suggested_next_action"] = next_action
        item["next_action"] = next_action
        reasons = item.get("reasons") if isinstance(item.get("reasons"), list) else []
        item["reasons"] = unique_strings([*reasons, item.get("reason"), reason])
    return actions


def action_merge_key(item: dict[str, Any]) -> tuple[str, str] | None:
    prop = str(item.get("property") or "").strip()
    if not prop:
        return None
    action_type = str(item.get("type") or "").strip()
    status = str(item.get("status") or "").strip()
    reason = str(item.get("reason") or "").strip()
    if auth_blocker_reason(item) and has_target_month_statement_context(item):
        return (prop, "capture_target_month_statement")
    if (
        action_type == "capture_target_month_statement"
        or status == "capture_target_month_statement"
        or reason in {"target_month_statement_unavailable", "target_month_statement_pdf_payload_missing"}
    ):
        return (prop, "capture_target_month_statement")
    if (
        action_type in {"collect_workflow_evidence", "place_har_at_suggested_path"}
        or status in {"collect_workflow_evidence", "place_har_at_suggested_path"}
        or reason in {"workflow_evidence_missing", "har_missing"}
    ):
        return (prop, "collect_workflow_evidence")
    if (
        action_type in {"capture_full_response_bodies", "resolve_configured_downloader_blocker"}
        or status == "capture_full_response_bodies"
        or reason in {"missing_response_bodies", "candidate_endpoints_missing_response_bodies"}
    ):
        return (prop, "capture_full_response_bodies")
    return None


def auth_blocked_statement_merge_key(item: dict[str, Any]) -> tuple[str, str, str] | None:
    prop = str(item.get("property") or "").strip()
    reason = auth_blocker_reason(item)
    har_path = str(item.get("har_path") or item.get("suggested_workflow_har_path") or "").strip()
    if not prop or not reason or not har_path:
        return None
    return (prop, reason, har_path)


def merge_action(primary: dict[str, Any], item: dict[str, Any]) -> None:
    source_values = primary.get("evidence_sources")
    sources = source_values if isinstance(source_values, list) else []
    sources = [*sources, primary.get("source"), item.get("source")]
    primary["evidence_sources"] = unique_strings(sources)
    primary["reasons"] = unique_strings([*(primary.get("reasons") or []), primary.get("reason"), item.get("reason")])
    for key in [
        "report_path",
        "har_path",
        "analysis_command",
        "handoff_command",
        "register_workflow_evidence_command",
        "register_workflow_evidence_apply_command",
        "post_capture_diagnostics_command",
        "validate_existing_capture_command",
        "advance_capture_workflow_evidence_dry_run_command",
        "advance_capture_workflow_evidence_apply_command",
        "advance_workflow_evidence_command",
        "advance_workflow_evidence_apply_command",
        "install_verified_capture_report",
        "install_verified_capture_dry_run_command",
        "install_verified_capture_apply_command",
        "install_verified_capture_direct_dry_run_command",
        "install_verified_capture_direct_apply_command",
    ]:
        values_key = f"{key}s"
        values = primary.get(values_key) if isinstance(primary.get(values_key), list) else []
        primary[values_key] = unique_strings([*values, primary.get(key), item.get(key)])
    primary["required_response_paths"] = unique_strings(
        [
            *(primary.get("required_response_paths") or []),
            *(item.get("required_response_paths") or []),
        ]
    )
    primary["missing_response_body_paths"] = unique_strings(
        [
            *(primary.get("missing_response_body_paths") or []),
            *(item.get("missing_response_body_paths") or []),
        ]
    )
    for key in [
        "next_command",
        "next_action",
        "suggested_next_action",
        "capture_command",
        "status",
        "servicer_hint",
        "required_capture_quality",
        "actionable_missing_response_body_count",
        "missing_response_body_path_counts",
        "response_body_requirements",
        "handoff_path",
        "handoff_command",
        "register_workflow_evidence_command",
        "register_workflow_evidence_apply_command",
        "capture_plan_report",
        "capture_plan_markdown",
        "capture_har_path",
        "capture_report",
        "post_capture_diagnostics_command",
        "validate_existing_capture_command",
        "advance_capture_workflow_evidence_dry_run_command",
        "advance_capture_workflow_evidence_apply_command",
        "install_verified_capture_report",
        "install_verified_capture_dry_run_command",
        "install_verified_capture_apply_command",
        "install_verified_capture_direct_dry_run_command",
        "install_verified_capture_direct_apply_command",
        "workflow_evidence_advance_report",
        "advance_workflow_evidence_command",
        "advance_workflow_evidence_apply_command",
        "post_capture_check_script_path",
        "post_capture_check_command",
        "post_capture_check_safe_to_run_after_har_capture",
        "scaffold_command",
        "scaffold_report",
        "manifest_path",
        "registry_entry_path",
        "registry_install_command",
        "stub_path",
        "generated_downloader_dry_run_command",
        "generated_downloader_apply_command",
        "live_auth_report",
        "live_auth_status",
        "live_auth_blocker",
        "live_auth_failure_visible_reason",
        "live_auth_manual_auth_required",
        "live_auth_manual_auth_file",
        "live_auth_oauth_error_codes",
        "capture_precondition_status",
        "capture_precondition_blocker",
        "capture_precondition_reason",
        "capture_precondition_manual_auth_required",
        "capture_precondition_next_action",
        "capture_precondition_safe_to_capture_now",
        "capture_precondition_prepare_command",
        "capture_precondition_safe_to_prepare_now",
        "capture_auth_blocker",
        "capture_auth_blocker_reason",
        "capture_command_ready_to_run_now",
        "capture_command_blocked_reason",
        "suggested_workflow_har_path",
        "suggested_workflow_har_path_exists",
        "register_workflow_evidence_ready_to_run_now",
        "advance_workflow_evidence_ready_to_run_now",
        "capture_required_before_offline_next_step",
        "command_precondition_status",
        "command_precondition_reason",
        "command_precondition_next_action",
        "har_path_exists",
        "discovered_har_candidate_count",
        "safe_discovered_har_candidate_count",
        "safe_discovered_har_candidate_path",
        "selected_discovered_har_candidate",
        "rejected_discovered_har_candidate_count",
        "rejected_discovered_har_candidate_paths",
        *CREDENTIAL_CONTEXT_FIELDS,
    ]:
        if (key not in primary or primary.get(key) is None) and key in item:
            primary[key] = item.get(key)
    for key in [
        "target_month",
        "target_month_statement_available",
        "target_month_existing_count",
        "target_month_downloaded_count",
        "target_month_skipped_count",
        "target_month_replayable_document_available",
        "target_month_replayable_document_payload_count",
    ]:
        if primary.get(key) is None and key in item:
            primary[key] = item.get(key)
    incoming_suggested_action = item.get("suggested_next_action")
    if incoming_suggested_action and primary.get("suggested_next_action") in (
        None,
        "",
        "collect_workflow_evidence",
        "place_har_at_suggested_path",
        "resolve_configured_downloader_blocker",
    ):
        primary["suggested_next_action"] = incoming_suggested_action
    for key in [
        "existing_target_month_files",
        "downloaded_target_month_files",
        "skipped_target_month_files",
        "replayable_statement_months",
        "statement_document_months",
        "source_direct_pdf_filenames",
        "target_month_direct_pdf_filenames",
        "target_month_document_identifiers",
        "expected_document_ids",
    ]:
        values = primary.get(key) if isinstance(primary.get(key), list) else []
        item_values = item.get(key) if isinstance(item.get(key), list) else []
        primary[key] = unique_strings([*values, *item_values])
    for key in [
        "source_direct_pdf_filename_candidates",
        "target_month_direct_pdf_filename_candidates",
        "target_month_direct_pdf_body_missing_candidate_count",
    ]:
        if primary.get(key) is None and key in item:
            primary[key] = item.get(key)
    for key in ["discovered_har_candidates", "rejected_discovered_har_candidates"]:
        values = primary.get(key) if isinstance(primary.get(key), list) else []
        item_values = item.get(key) if isinstance(item.get(key), list) else []
        by_path: dict[str, Any] = {}
        ordered: list[Any] = []
        for candidate in [*values, *item_values]:
            if not isinstance(candidate, dict):
                continue
            candidate_key = str(candidate.get("path") or candidate.get("name") or json.dumps(candidate, sort_keys=True))
            if candidate_key in by_path:
                continue
            by_path[candidate_key] = candidate
            ordered.append(candidate)
        primary[key] = ordered
    if item.get("source") == "citadel_har_workflow_diagnostics":
        for key in [
            "capture_precondition_status",
            "capture_precondition_blocker",
            "capture_precondition_reason",
            "capture_precondition_manual_auth_required",
            "capture_precondition_next_action",
            "capture_precondition_safe_to_capture_now",
            "capture_precondition_prepare_command",
            "capture_precondition_safe_to_prepare_now",
        ]:
            if item.get(key) is not None:
                primary[key] = item.get(key)
    if item.get("safe_to_run_automatically") is not True:
        primary["safe_to_run_automatically"] = False
    if item.get("capture_required") is True:
        primary["capture_required"] = True


def consolidate_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    consolidated: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in actions:
        key = action_merge_key(item)
        if key is None:
            consolidated.append(item)
            continue
        existing = by_key.get(key)
        if existing is None:
            item["evidence_sources"] = unique_strings([item.get("source")])
            if item.get("reason"):
                item["reasons"] = unique_strings([item.get("reason")])
            by_key[key] = item
            consolidated.append(item)
            continue
        merge_action(existing, item)
    return consolidated


def consolidate_auth_blocked_statement_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    target_keys = {
        key
        for item in actions
        if (key := auth_blocked_statement_merge_key(item)) is not None
        and has_target_month_statement_context(item)
    }
    if not target_keys:
        return actions
    primary_by_key = {
        key: item
        for item in actions
        if (key := auth_blocked_statement_merge_key(item)) in target_keys
        and has_target_month_statement_context(item)
    }
    consolidated: list[dict[str, Any]] = []
    emitted: set[tuple[str, str, str]] = set()
    for item in actions:
        key = auth_blocked_statement_merge_key(item)
        if key not in target_keys:
            consolidated.append(item)
            continue
        primary = primary_by_key[key]
        if key not in emitted:
            consolidated.append(primary)
            emitted.add(key)
        if item is not primary:
            merge_action(primary, item)
    return consolidated


def coverage_actions(coverage: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not coverage:
        return []
    actions: list[dict[str, Any]] = []
    for item in coverage.get("next_action_plan") or []:
        if not isinstance(item, dict):
            continue
        append_action(
            actions,
            {
                "source": "coverage",
                "type": item.get("type"),
                "property": item.get("property"),
                "servicer_hint": item.get("servicer_hint"),
                "reason": item.get("reason") or item.get("blocker"),
                "live_auth_report": item.get("live_auth_report"),
                "live_auth_status": item.get("live_auth_status"),
                "live_auth_blocker": item.get("live_auth_blocker"),
                "live_auth_failure_visible_reason": item.get("live_auth_failure_visible_reason"),
                "live_auth_manual_auth_required": item.get("live_auth_manual_auth_required"),
                "live_auth_manual_auth_file": item.get("live_auth_manual_auth_file"),
                "live_auth_oauth_error_codes": item.get("live_auth_oauth_error_codes"),
                "capture_precondition_status": item.get("capture_precondition_status"),
                "capture_precondition_blocker": item.get("capture_precondition_blocker"),
                "capture_precondition_reason": item.get("capture_precondition_reason"),
                "capture_precondition_manual_auth_required": item.get("capture_precondition_manual_auth_required"),
                "capture_precondition_next_action": item.get("capture_precondition_next_action"),
                "capture_precondition_safe_to_capture_now": item.get("capture_precondition_safe_to_capture_now"),
                "capture_precondition_prepare_command": item.get("capture_precondition_prepare_command"),
                "capture_precondition_safe_to_prepare_now": item.get("capture_precondition_safe_to_prepare_now"),
                "capture_auth_blocker": item.get("capture_auth_blocker"),
                "capture_auth_blocker_reason": item.get("capture_auth_blocker_reason"),
                "capture_command_ready_to_run_now": item.get("capture_command_ready_to_run_now"),
                "capture_command_blocked_reason": item.get("capture_command_blocked_reason"),
                "status": item.get("next_action_status"),
                "suggested_next_action": item.get("suggested_next_action") or item.get("type"),
                "next_action": item.get("next_action"),
                "next_command": item.get("next_command"),
                "har_path": item.get("har_path") or item.get("suggested_workflow_har_path"),
                "har_path_exists": item.get("har_path_exists"),
                "handoff_path": item.get("handoff_path"),
                "handoff_command": item.get("handoff_command"),
                "suggested_workflow_har_path": item.get("suggested_workflow_har_path"),
                "suggested_workflow_har_path_exists": item.get("suggested_workflow_har_path_exists"),
                "register_workflow_evidence_ready_to_run_now": item.get(
                    "register_workflow_evidence_ready_to_run_now"
                ),
                "advance_workflow_evidence_ready_to_run_now": item.get(
                    "advance_workflow_evidence_ready_to_run_now"
                ),
                "capture_required_before_offline_next_step": item.get(
                    "capture_required_before_offline_next_step"
                ),
                "command_precondition_status": item.get("command_precondition_status"),
                "command_precondition_reason": item.get("command_precondition_reason"),
                "command_precondition_next_action": item.get("command_precondition_next_action"),
                "post_capture_check_script_path": item.get("post_capture_check_script_path"),
                "post_capture_check_command": item.get("post_capture_check_command"),
                "post_capture_check_safe_to_run_after_har_capture": item.get(
                    "post_capture_check_safe_to_run_after_har_capture"
                ),
                "analysis_command": item.get("analysis_command"),
                "capture_command": item.get("capture_command"),
                "capture_plan_report": item.get("capture_plan_report"),
                "capture_plan_markdown": item.get("capture_plan_markdown"),
                "capture_har_path": item.get("capture_har_path"),
                "capture_report": item.get("capture_report"),
                "post_capture_diagnostics_command": item.get("post_capture_diagnostics_command"),
                "validate_existing_capture_command": item.get("validate_existing_capture_command"),
                "advance_capture_workflow_evidence_dry_run_command": item.get(
                    "advance_capture_workflow_evidence_dry_run_command"
                ),
                "advance_capture_workflow_evidence_apply_command": item.get(
                    "advance_capture_workflow_evidence_apply_command"
                ),
                "workflow_evidence_advance_report": item.get("workflow_evidence_advance_report"),
                "advance_workflow_evidence_command": item.get("advance_workflow_evidence_command"),
                "advance_workflow_evidence_apply_command": item.get("advance_workflow_evidence_apply_command"),
                "capture_required": item.get("capture_required"),
                "required_response_paths": string_list(item.get("required_response_paths")),
                "target_month": item.get("target_month"),
                "target_month_statement_available": item.get("target_month_statement_available"),
                "target_month_existing_count": item.get("target_month_existing_count"),
                "target_month_downloaded_count": item.get("target_month_downloaded_count"),
                "target_month_skipped_count": item.get("target_month_skipped_count"),
                "target_month_document_identifiers": string_list(item.get("target_month_document_identifiers")),
                "expected_document_ids": string_list(item.get("expected_document_ids")),
                "target_month_replayable_document_available": item.get(
                    "target_month_replayable_document_available"
                ),
                "target_month_replayable_document_payload_count": item.get(
                    "target_month_replayable_document_payload_count"
                ),
                "replayable_statement_months": string_list(item.get("replayable_statement_months")),
                "statement_document_months": string_list(item.get("statement_document_months")),
                "safe_to_run_automatically": item.get("safe_to_run_automatically") is True,
                **credential_context(item),
            },
        )
    return actions


def coverage_ready_property_keys(coverage: dict[str, Any] | None) -> set[str]:
    if not coverage:
        return set()
    records = coverage.get("coverage_records")
    if not isinstance(records, list):
        return set()
    ready: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        if (
            record.get("coverage_status") == "covered"
            and record.get("operational_status") == "ready"
            and int(record.get("automation_ready_downloader_count") or 0) > 0
        ):
            ready.add(slugify(record.get("property")))
    return ready


def rejected_discovered_candidates(candidates: object) -> list[dict[str, Any]]:
    if not isinstance(candidates, list):
        return []
    return [
        item
        for item in candidates
        if isinstance(item, dict) and item.get("safe_workflow_candidate") is not True
    ]


def evidence_refresh_actions(evidence_refresh: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not evidence_refresh:
        return []
    actions: list[dict[str, Any]] = []
    for item in evidence_refresh.get("next_action_plan") or evidence_refresh.get("results") or []:
        if not isinstance(item, dict):
            continue
        candidates = item.get("discovered_har_candidates")
        rejected = rejected_discovered_candidates(candidates)
        rejected_paths = [candidate.get("path") for candidate in rejected if candidate.get("path")]
        has_operator_action = any(
            item.get(key)
            for key in (
                "type",
                "next_action",
                "suggested_next_action",
                "capture_command",
                "post_capture_check_command",
                "register_workflow_evidence_command",
                "advance_workflow_evidence_command",
                "advance_workflow_evidence_apply_command",
                "command_precondition_status",
            )
        )
        if not candidates and not rejected and not has_operator_action:
            continue
        append_action(
            actions,
            {
                "source": "mortgage_workflow_evidence_refresh",
                "type": item.get("type") or item.get("next_action") or "collect_workflow_evidence",
                "property": item.get("property"),
                "servicer_hint": item.get("servicer_hint"),
                "reason": item.get("reason") or item.get("next_action_reason") or item.get("analysis_reason"),
                "suggested_next_action": item.get("suggested_next_action") or item.get("next_action") or item.get("type"),
                "har_path": item.get("har_path") or item.get("suggested_workflow_har_path"),
                "har_path_exists": item.get("har_path_exists"),
                "suggested_workflow_har_path": item.get("suggested_workflow_har_path") or item.get("har_path"),
                "suggested_workflow_har_path_exists": item.get("suggested_workflow_har_path_exists")
                if "suggested_workflow_har_path_exists" in item
                else item.get("har_path_exists"),
                "analysis_command": item.get("analysis_command"),
                "handoff_path": item.get("handoff_path"),
                "handoff_command": item.get("handoff_command"),
                "post_capture_check_script_path": item.get("post_capture_check_script_path"),
                "post_capture_check_command": item.get("post_capture_check_command"),
                "post_capture_check_safe_to_run_after_har_capture": item.get(
                    "post_capture_check_safe_to_run_after_har_capture"
                ),
                "register_workflow_evidence_command": item.get("register_workflow_evidence_command"),
                "register_workflow_evidence_apply_command": item.get("register_workflow_evidence_apply_command"),
                "capture_command": item.get("capture_command"),
                "capture_auth_blocker": item.get("capture_auth_blocker"),
                "capture_auth_blocker_reason": item.get("capture_auth_blocker_reason"),
                "capture_command_ready_to_run_now": item.get("capture_command_ready_to_run_now"),
                "capture_command_blocked_reason": item.get("capture_command_blocked_reason"),
                "register_workflow_evidence_ready_to_run_now": item.get(
                    "register_workflow_evidence_ready_to_run_now"
                ),
                "advance_workflow_evidence_command": item.get("advance_workflow_evidence_command"),
                "advance_workflow_evidence_apply_command": item.get("advance_workflow_evidence_apply_command"),
                "advance_workflow_evidence_ready_to_run_now": item.get(
                    "advance_workflow_evidence_ready_to_run_now"
                ),
                "capture_required_before_offline_next_step": item.get(
                    "capture_required_before_offline_next_step"
                ),
                "command_precondition_status": item.get("command_precondition_status"),
                "command_precondition_reason": item.get("command_precondition_reason"),
                "command_precondition_next_action": item.get("command_precondition_next_action"),
                "required_capture_quality": item.get("required_capture_quality"),
                "actionable_missing_response_body_count": item.get("actionable_missing_response_body_count"),
                "required_response_paths": string_list(item.get("missing_response_body_paths")),
                "missing_response_body_paths": string_list(item.get("missing_response_body_paths")),
                "missing_response_body_path_counts": item.get("missing_response_body_path_counts"),
                "response_body_requirements": item.get("response_body_requirements"),
                "target_month_document_identifiers": string_list(item.get("target_month_document_identifiers")),
                "expected_document_ids": string_list(item.get("expected_document_ids")),
                "discovered_har_candidate_count": item.get("discovered_har_candidate_count"),
                "discovered_har_candidates": candidates if isinstance(candidates, list) else [],
                "safe_discovered_har_candidate_count": item.get("safe_discovered_har_candidate_count"),
                "safe_discovered_har_candidate_path": item.get("safe_discovered_har_candidate_path"),
                "selected_discovered_har_candidate": item.get("selected_discovered_har_candidate"),
                "rejected_discovered_har_candidate_count": len(rejected),
                "rejected_discovered_har_candidate_paths": rejected_paths,
                "rejected_discovered_har_candidates": rejected,
                "safe_to_run_automatically": item.get("safe_to_run_automatically") is True,
                **credential_context(item),
            },
        )
    return actions


def capture_queue_operator_contexts(capture_queue_processor: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not capture_queue_processor:
        return {}
    contexts: dict[str, dict[str, Any]] = {}
    for item in capture_queue_processor.get("operator_next_actions") or []:
        if not isinstance(item, dict):
            continue
        prop_key = slugify(item.get("property"))
        if not prop_key:
            continue
        context = credential_context(item)
        for key in [
            "capture_auth_blocker",
            "capture_auth_blocker_reason",
            "capture_auth_next_action",
            "capture_command_ready_to_run_now",
            "capture_command_blocked_reason",
            "command_precondition_status",
            "command_precondition_reason",
            "command_precondition_next_action",
            "expected_document_ids",
            "target_month_document_identifiers",
        ]:
            if key in item:
                context[key] = item.get(key)
        if not context:
            continue
        existing = contexts.setdefault(prop_key, {})
        for key, value in context.items():
            if value is not None and (key not in existing or existing.get(key) is None):
                existing[key] = value
    return contexts


def enrich_actions_from_capture_queue(
    actions: list[dict[str, Any]],
    capture_queue_processor: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    contexts = capture_queue_operator_contexts(capture_queue_processor)
    if not contexts:
        return actions
    for item in actions:
        context = contexts.get(slugify(item.get("property")))
        if not context:
            continue
        for key, value in context.items():
            if value is not None and (key not in item or item.get(key) is None):
                item[key] = value
    return actions


def citadel_action_type(summary: dict[str, Any]) -> str:
    status = str(summary.get("next_action_status") or "").strip()
    if status in {"capture_full_response_bodies", "capture_target_month_statement"}:
        return status
    if (
        summary.get("capture_quality_status") == "missing_target_month_statement"
        or summary.get("blocker") in {"target_month_statement_unavailable", "target_month_statement_pdf_payload_missing"}
    ):
        return "capture_target_month_statement"
    return "capture_full_response_bodies"


def citadel_summary(path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "source": "citadel_har_workflow_diagnostics",
        "property": "90 Madison Ave",
        "report_path": str(path),
        "report_exists": path.exists(),
        "status": None,
        "ready": False,
        "blocker": None,
    }
    if not path.exists():
        summary["status"] = "missing"
        summary["blocker"] = "diagnostics_report_missing"
        return summary
    data, error = load_json(path)
    if error or data is None:
        summary["status"] = "unreadable"
        summary["blocker"] = "diagnostics_report_unreadable"
        summary["error"] = error
        return summary
    next_action = data.get("next_action") if isinstance(data.get("next_action"), dict) else {}
    ready = data.get("status") == "ok" and data.get("can_replay_documents") is True
    summary.update(
        {
            "status": data.get("status"),
            "capture_quality_status": data.get("capture_quality_status"),
            "can_replay_documents": data.get("can_replay_documents"),
            "replay_blocker": data.get("replay_blocker"),
            "missing_response_body_count": data.get("missing_response_body_count"),
            "missing_response_body_paths": string_list(data.get("missing_response_body_paths")),
            "missing_response_body_path_counts": data.get("missing_response_body_path_counts"),
            "replayable_document_payload_count": data.get("replayable_document_payload_count"),
            "target_month": data.get("target_month"),
            "target_month_replayable_document_available": data.get(
                "target_month_replayable_document_available"
            ),
            "target_month_replayable_document_payload_count": data.get(
                "target_month_replayable_document_payload_count"
            ),
            "replayable_statement_months": string_list(data.get("replayable_statement_months")),
            "statement_document_months": string_list(data.get("statement_document_months")),
            "direct_pdf_response_count": data.get("direct_pdf_response_count"),
            "source_direct_pdf_candidate_count": data.get("source_direct_pdf_candidate_count"),
            "source_direct_pdf_path_counts": data.get("source_direct_pdf_path_counts"),
            "source_direct_pdf_filenames": string_list(data.get("source_direct_pdf_filenames")),
            "source_direct_pdf_filename_candidates": data.get("source_direct_pdf_filename_candidates"),
            "target_month_direct_pdf_filenames": string_list(data.get("target_month_direct_pdf_filenames")),
            "target_month_direct_pdf_filename_candidates": data.get("target_month_direct_pdf_filename_candidates"),
            "target_month_direct_pdf_body_missing_candidate_count": data.get(
                "target_month_direct_pdf_body_missing_candidate_count"
            ),
            "direct_pdf_missing_response_count": data.get("direct_pdf_missing_response_count"),
            "direct_pdf_missing_response_paths": string_list(data.get("direct_pdf_missing_response_paths")),
            "replayable_json_response_count": data.get("replayable_json_response_count"),
            "embedded_response_body_count": data.get("embedded_response_body_count"),
            "ready": ready,
            "blocker": None if ready else (data.get("replay_blocker") or data.get("reason") or "not_replayable"),
            "next_action_status": next_action.get("status"),
            "next_command": next_action.get("next_command") or data.get("next_command"),
            "capture_command": next_action.get("capture_command"),
            "capture_plan_report": next_action.get("capture_plan_report"),
            "capture_plan_markdown": next_action.get("capture_plan_markdown"),
            "capture_har_path": next_action.get("capture_har_path"),
            "capture_report": next_action.get("capture_report"),
            "post_capture_diagnostics_command": next_action.get("post_capture_diagnostics_command"),
            "validate_existing_capture_command": next_action.get("validate_existing_capture_command"),
            "advance_capture_workflow_evidence_dry_run_command": next_action.get(
                "advance_capture_workflow_evidence_dry_run_command"
            ),
            "advance_capture_workflow_evidence_apply_command": next_action.get(
                "advance_capture_workflow_evidence_apply_command"
            ),
            "workflow_evidence_advance_report": next_action.get("workflow_evidence_advance_report"),
            "advance_workflow_evidence_command": next_action.get("advance_workflow_evidence_command"),
            "advance_workflow_evidence_apply_command": next_action.get("advance_workflow_evidence_apply_command"),
            "install_verified_capture_report": next_action.get("install_verified_capture_report"),
            "install_verified_capture_dry_run_command": next_action.get("install_verified_capture_dry_run_command"),
            "install_verified_capture_apply_command": next_action.get("install_verified_capture_apply_command"),
            "install_verified_capture_direct_dry_run_command": next_action.get(
                "install_verified_capture_direct_dry_run_command"
            ),
            "install_verified_capture_direct_apply_command": next_action.get(
                "install_verified_capture_direct_apply_command"
            ),
            "capture_required": next_action.get("capture_required"),
            "required_response_paths": string_list(next_action.get("required_response_paths")),
            "capture_precondition_status": next_action.get("capture_precondition_status"),
            "capture_precondition_blocker": next_action.get("capture_precondition_blocker"),
            "capture_precondition_reason": next_action.get("capture_precondition_reason"),
            "capture_precondition_manual_auth_required": next_action.get("capture_precondition_manual_auth_required"),
            "capture_precondition_next_action": next_action.get("capture_precondition_next_action"),
            "capture_precondition_safe_to_capture_now": next_action.get("capture_precondition_safe_to_capture_now"),
            "capture_precondition_prepare_command": next_action.get("capture_precondition_prepare_command"),
            "capture_precondition_safe_to_prepare_now": next_action.get("capture_precondition_safe_to_prepare_now"),
        }
    )
    return summary


def har_analysis_summary(path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "source": "mortgage_workflow_har_intake_analysis",
        "report_path": str(path),
        "report_exists": path.exists(),
        "status": None,
        "ready": False,
        "blocker": None,
    }
    data, error = load_json(path)
    if error or data is None:
        summary["status"] = "unreadable"
        summary["blocker"] = "analysis_report_unreadable"
        summary["error"] = error
        return summary
    ready = data.get("status") == "ok" and bool(data.get("safe_to_build_downloader_automatically"))
    actionable_with_body = [
        item
        for item in data.get("candidate_endpoints") or []
        if isinstance(item, dict)
        and item.get("kind") != "auth"
        and item.get("has_embedded_response_body") is True
    ]
    if data.get("status") == "ok" and actionable_with_body:
        ready = True
    paths = scaffold_paths(path, data.get("property"))
    registry_path = Path(paths["registry_entry_path"])
    register_evidence_dry_run_command = register_workflow_evidence_command(data.get("property"), data.get("har_path"))
    register_evidence_apply_command = register_workflow_evidence_command(
        data.get("property"), data.get("har_path"), apply=True
    )
    summary.update(
        {
            "property": data.get("property"),
            "servicer_hint": data.get("servicer_hint"),
            "portal_url": data.get("portal_url"),
            "har_path": data.get("har_path"),
            "har_path_exists": data.get("har_path_exists"),
            "status": data.get("status"),
            "reason": data.get("reason"),
            "suggested_next_action": data.get("suggested_next_action"),
            "entry_count": data.get("entry_count"),
            "candidate_endpoint_count": data.get("candidate_endpoint_count"),
            "candidate_statement_endpoint_count": data.get("candidate_statement_endpoint_count"),
            "candidate_document_endpoint_count": data.get("candidate_document_endpoint_count"),
            "candidate_pdf_response_count": data.get("candidate_pdf_response_count"),
            "candidate_json_response_count": data.get("candidate_json_response_count"),
            "embedded_response_body_count": data.get("embedded_response_body_count"),
            "missing_response_body_count": data.get("missing_response_body_count"),
            "actionable_missing_response_body_count": data.get("actionable_missing_response_body_count"),
            "missing_response_body_paths": string_list(data.get("missing_response_body_paths")),
            "missing_response_body_path_counts": data.get("missing_response_body_path_counts"),
            "response_body_requirements": data.get("response_body_requirements"),
            "required_capture_quality": data.get("required_capture_quality"),
            "register_workflow_evidence_command": register_evidence_dry_run_command,
            "register_workflow_evidence_apply_command": register_evidence_apply_command,
            "ready": ready,
            "blocker": None if ready else (data.get("reason") or data.get("suggested_next_action") or "not_ready"),
            "scaffold_command": scaffold_command(path, paths) if ready else None,
            "scaffold_report": paths["scaffold_report"] if ready else None,
            "manifest_path": paths["manifest_path"] if ready else None,
            "registry_entry_path": paths["registry_entry_path"] if ready else None,
            "registry_entry_exists": registry_path.exists() if ready else None,
            "registry_entry_written": registry_path.exists() if ready else None,
            "registry_install_command": registry_install_command(paths) if ready else None,
            "stub_path": paths["stub_path"] if ready else None,
            "generated_downloader_dry_run_command": generated_downloader_command(paths["stub_path"]) if ready else None,
            "generated_downloader_apply_command": generated_downloader_command(paths["stub_path"], apply=True)
            if ready
            else None,
            "safe_to_register_automatically": False if ready else None,
        }
    )
    return summary


def discover_analysis_reports(
    report_dir: Path,
    explicit_paths: list[Path],
    evidence_refresh_report: Path | None = None,
) -> list[Path]:
    paths = [path for path in explicit_paths]
    if not explicit_paths and evidence_refresh_report and evidence_refresh_report.exists():
        data, error = load_json(evidence_refresh_report)
        if not error and data:
            for item in data.get("results") or []:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("analysis_report") or "").strip()
                if text:
                    paths.append(Path(text))
    if not paths and report_dir.exists():
        paths.extend(sorted(report_dir.glob("mortgage_workflow_har_intake_analysis_*.json")))
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = str(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def build_report(
    *,
    coverage_report: Path,
    citadel_diagnostics_report: Path,
    evidence_refresh_report: Path,
    capture_queue_processor_report: Path,
    analysis_reports: list[Path],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "job": "mortgage-workflow-replayability-gate",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "safe_to_run_automatically": True,
        "coverage_report": str(coverage_report),
        "citadel_diagnostics_report": str(citadel_diagnostics_report),
        "evidence_refresh_report": str(evidence_refresh_report),
        "capture_queue_processor_report": str(capture_queue_processor_report),
        "analysis_report_paths": [str(path) for path in analysis_reports],
    }

    coverage_data: dict[str, Any] | None = None
    coverage_error: str | None = None
    if coverage_report.exists():
        coverage_data, coverage_error = load_json(coverage_report)
    else:
        coverage_error = "missing"
    report.update(
        {
            "coverage_report_exists": coverage_report.exists(),
            "coverage_status": coverage_data.get("status") if coverage_data else None,
            "coverage_error": coverage_error,
            "co_owner_paid_property_count": coverage_data.get("co_owner_paid_property_count") if coverage_data else None,
            "covered_co_owner_paid_property_count": coverage_data.get("covered_co_owner_paid_property_count") if coverage_data else None,
            "automation_ready_co_owner_paid_property_count": (
                coverage_data.get("automation_ready_co_owner_paid_property_count") if coverage_data else None
            ),
            "configured_but_not_ready_co_owner_paid_property_count": (
                coverage_data.get("configured_but_not_ready_co_owner_paid_property_count") if coverage_data else None
            ),
            "blocked_configured_downloader_count": (
                coverage_data.get("blocked_configured_downloader_count") if coverage_data else None
            ),
            "missing_downloader_count": coverage_data.get("missing_downloader_count") if coverage_data else None,
            "missing_downloader_properties": coverage_data.get("missing_downloader_properties") if coverage_data else None,
        }
    )

    evidence_refresh_data: dict[str, Any] | None = None
    evidence_refresh_error: str | None = None
    if evidence_refresh_report.exists():
        evidence_refresh_data, evidence_refresh_error = load_json(evidence_refresh_report)
    else:
        evidence_refresh_error = "missing"
    report.update(
        {
            "evidence_refresh_report_exists": evidence_refresh_report.exists(),
            "evidence_refresh_status": evidence_refresh_data.get("status") if evidence_refresh_data else None,
            "evidence_refresh_error": evidence_refresh_error,
            "evidence_refresh_reason": evidence_refresh_data.get("reason") if evidence_refresh_data else None,
            "raw_discovered_har_candidate_property_count": (
                evidence_refresh_data.get("raw_discovered_har_candidate_property_count")
                if evidence_refresh_data
                else None
            ),
            "raw_discovered_har_candidate_properties": (
                evidence_refresh_data.get("raw_discovered_har_candidate_properties") if evidence_refresh_data else None
            ),
            "discovered_har_candidate_property_count": (
                evidence_refresh_data.get("discovered_har_candidate_property_count")
                if evidence_refresh_data
                else None
            ),
            "discovered_har_candidate_properties": (
                evidence_refresh_data.get("discovered_har_candidate_properties") if evidence_refresh_data else None
            ),
            "safe_discovered_har_candidate_property_count": (
                evidence_refresh_data.get("safe_discovered_har_candidate_property_count")
                if evidence_refresh_data
                else None
            ),
            "safe_discovered_har_candidate_properties": (
                evidence_refresh_data.get("safe_discovered_har_candidate_properties")
                if evidence_refresh_data
                else None
            ),
            "selected_discovered_har_candidate_count": (
                evidence_refresh_data.get("selected_discovered_har_candidate_count")
                if evidence_refresh_data
                else None
            ),
            "selected_discovered_har_candidate_properties": (
                evidence_refresh_data.get("selected_discovered_har_candidate_properties")
                if evidence_refresh_data
                else None
            ),
            "rejected_discovered_har_candidate_count": (
                evidence_refresh_data.get("rejected_discovered_har_candidate_count")
                if evidence_refresh_data
                else None
            ),
            "rejected_discovered_har_candidate_paths": (
                evidence_refresh_data.get("rejected_discovered_har_candidate_paths")
                if evidence_refresh_data
                else None
            ),
        }
    )
    capture_queue_processor_data: dict[str, Any] | None = None
    capture_queue_processor_error: str | None = None
    if capture_queue_processor_report.exists():
        capture_queue_processor_data, capture_queue_processor_error = load_json(capture_queue_processor_report)
    else:
        capture_queue_processor_error = "missing"
    report.update(
        {
            "capture_queue_processor_report_exists": capture_queue_processor_report.exists(),
            "capture_queue_processor_status": (
                capture_queue_processor_data.get("status") if capture_queue_processor_data else None
            ),
            "capture_queue_processor_error": capture_queue_processor_error,
            "capture_queue_processor_reason": (
                capture_queue_processor_data.get("reason") if capture_queue_processor_data else None
            ),
            "capture_queue_processor_operator_next_action_count": (
                capture_queue_processor_data.get("operator_next_action_count")
                if capture_queue_processor_data
                else None
            ),
            "capture_queue_processor_operator_next_action_properties": (
                capture_queue_processor_data.get("operator_next_action_properties")
                if capture_queue_processor_data
                else None
            ),
        }
    )

    citadel = citadel_summary(citadel_diagnostics_report)
    analyses = [har_analysis_summary(path) for path in analysis_reports]
    ready_coverage_keys = coverage_ready_property_keys(coverage_data)
    actions = coverage_actions(coverage_data)
    actions.extend(evidence_refresh_actions(evidence_refresh_data))
    actions = enrich_actions_from_capture_queue(actions, capture_queue_processor_data)
    if not citadel.get("ready"):
        append_action(
            actions,
            {
                "source": "citadel_har_workflow_diagnostics",
                "type": citadel_action_type(citadel),
                "property": citadel.get("property"),
                "reason": citadel.get("blocker"),
                "status": citadel.get("next_action_status"),
                "next_command": citadel.get("next_command"),
                "capture_command": citadel.get("capture_command"),
                "capture_plan_report": citadel.get("capture_plan_report"),
                "capture_plan_markdown": citadel.get("capture_plan_markdown"),
                "capture_har_path": citadel.get("capture_har_path"),
                "capture_report": citadel.get("capture_report"),
                "post_capture_diagnostics_command": citadel.get("post_capture_diagnostics_command"),
                "validate_existing_capture_command": citadel.get("validate_existing_capture_command"),
                "advance_capture_workflow_evidence_dry_run_command": citadel.get(
                    "advance_capture_workflow_evidence_dry_run_command"
                ),
                "advance_capture_workflow_evidence_apply_command": citadel.get(
                    "advance_capture_workflow_evidence_apply_command"
                ),
                "workflow_evidence_advance_report": citadel.get("workflow_evidence_advance_report"),
                "advance_workflow_evidence_command": citadel.get("advance_workflow_evidence_command"),
                "advance_workflow_evidence_apply_command": citadel.get("advance_workflow_evidence_apply_command"),
                "install_verified_capture_report": citadel.get("install_verified_capture_report"),
                "install_verified_capture_dry_run_command": citadel.get("install_verified_capture_dry_run_command"),
                "install_verified_capture_apply_command": citadel.get("install_verified_capture_apply_command"),
                "install_verified_capture_direct_dry_run_command": citadel.get(
                    "install_verified_capture_direct_dry_run_command"
                ),
                "install_verified_capture_direct_apply_command": citadel.get(
                    "install_verified_capture_direct_apply_command"
                ),
                "capture_required": citadel.get("capture_required"),
                "required_response_paths": citadel.get("required_response_paths"),
                "capture_precondition_status": citadel.get("capture_precondition_status"),
                "capture_precondition_blocker": citadel.get("capture_precondition_blocker"),
                "capture_precondition_reason": citadel.get("capture_precondition_reason"),
                "capture_precondition_manual_auth_required": citadel.get("capture_precondition_manual_auth_required"),
                "capture_precondition_next_action": citadel.get("capture_precondition_next_action"),
                "capture_precondition_safe_to_capture_now": citadel.get("capture_precondition_safe_to_capture_now"),
                "capture_precondition_prepare_command": citadel.get("capture_precondition_prepare_command"),
                "capture_precondition_safe_to_prepare_now": citadel.get("capture_precondition_safe_to_prepare_now"),
                "target_month": citadel.get("target_month"),
                "target_month_replayable_document_available": citadel.get(
                    "target_month_replayable_document_available"
                ),
                "target_month_replayable_document_payload_count": citadel.get(
                    "target_month_replayable_document_payload_count"
                ),
                "replayable_statement_months": citadel.get("replayable_statement_months"),
                "statement_document_months": citadel.get("statement_document_months"),
                "source_direct_pdf_filenames": citadel.get("source_direct_pdf_filenames"),
                "source_direct_pdf_filename_candidates": citadel.get("source_direct_pdf_filename_candidates"),
                "target_month_direct_pdf_filenames": citadel.get("target_month_direct_pdf_filenames"),
                "target_month_direct_pdf_filename_candidates": citadel.get(
                    "target_month_direct_pdf_filename_candidates"
                ),
                "target_month_direct_pdf_body_missing_candidate_count": citadel.get(
                    "target_month_direct_pdf_body_missing_candidate_count"
                ),
                "safe_to_run_automatically": False,
            },
        )
    for analysis in analyses:
        if analysis.get("ready"):
            if slugify(analysis.get("property")) in ready_coverage_keys:
                continue
            append_action(
                actions,
                {
                    "source": "mortgage_workflow_har_intake_analysis",
                    "type": "generate_downloader_scaffold",
                    "property": analysis.get("property"),
                    "servicer_hint": analysis.get("servicer_hint"),
                    "report_path": analysis.get("report_path"),
                    "har_path": analysis.get("har_path"),
                    "register_workflow_evidence_command": analysis.get("register_workflow_evidence_command"),
                    "register_workflow_evidence_apply_command": analysis.get("register_workflow_evidence_apply_command"),
                    "scaffold_command": analysis.get("scaffold_command"),
                    "scaffold_report": analysis.get("scaffold_report"),
                    "manifest_path": analysis.get("manifest_path"),
                    "registry_entry_path": analysis.get("registry_entry_path"),
                    "registry_entry_exists": analysis.get("registry_entry_exists"),
                    "registry_entry_written": analysis.get("registry_entry_written"),
                    "registry_install_command": analysis.get("registry_install_command"),
                    "stub_path": analysis.get("stub_path"),
                    "generated_downloader_dry_run_command": analysis.get("generated_downloader_dry_run_command"),
                    "generated_downloader_apply_command": analysis.get("generated_downloader_apply_command"),
                    "safe_to_register_automatically": analysis.get("safe_to_register_automatically"),
                    "safe_to_run_automatically": True,
                },
            )
            continue
        append_action(
            actions,
            {
                "source": "mortgage_workflow_har_intake_analysis",
                "type": analysis.get("suggested_next_action"),
                "suggested_next_action": analysis.get("suggested_next_action"),
                "property": analysis.get("property"),
                "servicer_hint": analysis.get("servicer_hint"),
                "reason": analysis.get("blocker"),
                "report_path": analysis.get("report_path"),
                "har_path": analysis.get("har_path"),
                "har_path_exists": analysis.get("har_path_exists"),
                "register_workflow_evidence_command": analysis.get("register_workflow_evidence_command"),
                "register_workflow_evidence_apply_command": analysis.get("register_workflow_evidence_apply_command"),
                "required_capture_quality": analysis.get("required_capture_quality"),
                "actionable_missing_response_body_count": analysis.get("actionable_missing_response_body_count"),
                "required_response_paths": analysis.get("missing_response_body_paths"),
                "missing_response_body_paths": analysis.get("missing_response_body_paths"),
                "missing_response_body_path_counts": analysis.get("missing_response_body_path_counts"),
                "response_body_requirements": analysis.get("response_body_requirements"),
                "safe_to_run_automatically": False,
            },
        )

    actions = enrich_actions_from_capture_queue(actions, capture_queue_processor_data)
    actions = consolidate_auth_blocked_statement_actions(propagate_auth_blockers(consolidate_actions(actions)))
    ready_analysis_count = sum(1 for item in analyses if item.get("ready"))
    blocked_analysis_count = sum(1 for item in analyses if not item.get("ready"))
    manual_action_count = sum(1 for item in actions if item.get("safe_to_run_automatically") is not True)
    safe_action_count = sum(1 for item in actions if item.get("safe_to_run_automatically") is True)
    status = "ok"
    reasons: list[str] = []
    if coverage_error or not coverage_data:
        status = "review"
        reasons.append("coverage_report_unavailable")
    elif coverage_data.get("status") != "ok":
        status = "review"
        reasons.append("coverage_not_ready")
    if not citadel.get("ready"):
        status = "review"
        reasons.append("citadel_not_replayable")
    if blocked_analysis_count:
        status = "review"
        reasons.append("har_analysis_not_ready")
    if manual_action_count:
        status = "review"
        reasons.append("manual_actions_required")
    safe_to_run_automatically = manual_action_count == 0 and (status == "ok" or safe_action_count > 0)

    report.update(
        {
            "status": status,
            "reason": ";".join(dict.fromkeys(reasons)) or None,
            "safe_to_run_automatically": safe_to_run_automatically,
            "citadel": citadel,
            "citadel_status": citadel.get("status"),
            "citadel_ready": citadel.get("ready"),
            "citadel_blocker": citadel.get("blocker"),
            "citadel_target_month": citadel.get("target_month"),
            "citadel_target_month_replayable_document_available": citadel.get(
                "target_month_replayable_document_available"
            ),
            "citadel_target_month_replayable_document_payload_count": citadel.get(
                "target_month_replayable_document_payload_count"
            ),
            "har_analysis_count": len(analyses),
            "har_analysis_ready_count": ready_analysis_count,
            "har_analysis_blocked_count": blocked_analysis_count,
            "har_analyses": analyses,
            "next_action_plan_count": len(actions),
            "next_action_manual_count": manual_action_count,
            "next_action_safe_to_run_automatically_count": safe_action_count,
            "next_action_plan": actions,
        }
    )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-report", type=Path, default=DEFAULT_COVERAGE_REPORT)
    parser.add_argument("--citadel-diagnostics-report", type=Path, default=DEFAULT_CITADEL_DIAGNOSTICS_REPORT)
    parser.add_argument("--evidence-refresh-report", type=Path, default=None)
    parser.add_argument("--capture-queue-processor-report", type=Path, default=None)
    parser.add_argument("--analysis-report", type=Path, action="append", default=[])
    parser.add_argument("--analysis-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    evidence_refresh_report = args.evidence_refresh_report or (
        args.report.parent / DEFAULT_EVIDENCE_REFRESH_REPORT.name
    )
    capture_queue_processor_report = args.capture_queue_processor_report or (
        args.report.parent / DEFAULT_CAPTURE_QUEUE_PROCESSOR_REPORT.name
    )
    analysis_reports = discover_analysis_reports(
        args.analysis_dir,
        args.analysis_report,
        evidence_refresh_report=evidence_refresh_report,
    )
    report = build_report(
        coverage_report=args.coverage_report,
        citadel_diagnostics_report=args.citadel_diagnostics_report,
        evidence_refresh_report=evidence_refresh_report,
        capture_queue_processor_report=capture_queue_processor_report,
        analysis_reports=analysis_reports,
    )
    report = write_json_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
