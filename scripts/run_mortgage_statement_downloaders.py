#!/usr/bin/env python3
"""Run configured co-owner-paid mortgage statement downloaders.

The registry keeps individual servicer downloaders out of the monthly shell
gate. New mortgage servicers should be added to
``config/mortgage_statement_downloaders.json`` with their own script/runtime.
"""

from __future__ import annotations

import json
import os
import argparse
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def default_workspace_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "config" / "mortgage_statement_downloaders.json").exists():
        return cwd
    return Path(__file__).absolute().parents[1]


WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", default_workspace_root()))
DEFAULT_CONFIG = WORKSPACE_ROOT / "config" / "mortgage_statement_downloaders.json"
DEFAULT_REPORT = WORKSPACE_ROOT / "reports" / "mortgage_statement_downloaders_report.json"
DEFAULT_HANDOFF_DIR = WORKSPACE_ROOT / "reports"
DEFAULT_SUPPLEMENTAL_DOWNLOADER_REPORTS = (
    WORKSPACE_ROOT / "reports" / "mortgage_statement_downloaders_live_cdp_summary_report.json",
)
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from stable_json_report import stable_report_digest


ACTIVE_CHILD_PROCESS_GROUPS: set[int] = set()


def _terminate_active_child_process_groups(signum: int, _frame: Any) -> None:
    for process_group_id in tuple(ACTIVE_CHILD_PROCESS_GROUPS):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    raise SystemExit(128 + signum)


def install_child_process_cleanup_handlers() -> None:
    signal.signal(signal.SIGTERM, _terminate_active_child_process_groups)
    signal.signal(signal.SIGINT, _terminate_active_child_process_groups)


RUNTIMES = {
    "node": lambda script: [os.environ.get("NODE_BIN", "node"), str(script)],
    "python": lambda script: [os.environ.get("PYTHON_BIN", "python3"), str(script)],
    "bash": lambda script: ["bash", str(script)],
}

REPORT_STATUSES_THAT_EXPLAIN_NONZERO_RC = {
    "otp_required",
    "review",
    "target_month_missing",
}

SUPPLEMENTAL_AUTH_CONTEXT_FIELDS = (
    "status",
    "reason",
    "manual_auth_required",
    "manual_auth_reason",
    "manual_auth_portal_url",
    "login_mode",
    "auth_state",
    "auth_stage",
    "auth_failure_reason",
    "auth_failure_visible_reason",
    "auth_mfa_reached",
    "auth_issue",
    "auth_issue_text",
    "credentials_available",
    "credential_source",
    "credential_lookup_status",
    "credential_lookup_failure_reason",
    "credential_lookup_exit_code",
    "credential_lookup_item_name",
    "credential_lookup_item_id_configured",
    "credential_lookup_uri_host",
    "credential_lookup_uri_host_aliases",
    "credential_lookup_login_hint_configured",
    "credential_lookup_search_term_count",
    "credential_lookup_search_terms",
    "credential_lookup_candidate_search_term_count",
    "credential_lookup_candidate_search_terms",
    "credential_lookup_expected_folder_name",
    "credential_lookup_expected_folder_id_configured",
    "credential_lookup_script",
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
    "credential_repair_instruction",
    "auto_login_attempted",
    "auto_login_status",
    "auto_login_blocked_reason",
    "auto_login_force_enabled",
    "auto_login_username_available",
    "auto_login_password_available",
    "auto_login_username_typed",
    "auto_login_password_typed",
    "auto_login_step",
    "credential_login_failure_suspected",
    "credential_login_failure_suspected_reason",
    "operator_next_action",
    "suggested_next_action",
    "required_capture_quality",
    "target_month_document_identifiers",
    "expected_document_ids",
    "target_month_statement_candidates",
    "target_month_candidate_count",
    "target_month_downloadable_count",
    "body_recapture_capture_method",
    "body_recapture_capture_instruction",
    "body_recapture_manual_har_export_warning",
)

SUPPLEMENTAL_TARGET_CONTEXT_REPLACE_FIELDS = {
    "target_month_document_identifiers",
    "expected_document_ids",
    "target_month_statement_candidates",
    "target_month_candidate_count",
    "target_month_downloadable_count",
}

EXPLICIT_ENV_OVERRIDE_KEYS = {
    "CITADEL_HAR_PATH",
    "CITADEL_LOGIN_MODE",
    "CITADEL_REPORT_PATH",
}

EXPLICIT_CITADEL_TARGET_MONTH_KEYS = (
    "BASELANE_MORTGAGE_CITADEL_STATEMENT_TARGET_MONTH",
    "MORTGAGE_DOWNLOADER_CITADEL_TARGET_MONTH",
    "CITADEL_STATEMENT_TARGET_MONTH",
    "CITADEL_TARGET_MONTH",
)

CITADEL_TARGET_MONTH_OFFSET_KEYS = (
    "MORTGAGE_DOWNLOADER_CITADEL_TARGET_MONTH_DEFAULT_OFFSET",
    "CITADEL_TARGET_MONTH_DEFAULT_OFFSET",
)

GENERATED_HAR_TARGET_MONTH_OFFSET_KEYS = (
    "MORTGAGE_GENERATED_HAR_TARGET_MONTH_DEFAULT_OFFSET",
)

BITWARDEN_WRITE_ENABLE_ENV_KEYS = (
    "CITADEL_BW_RECONCILE_UPDATE",
)

CITADEL_BW_WRITE_GUARD_ENV_KEYS = (
    "CITADEL_BW_EXPECTED_ITEM_ID",
    "CITADEL_BW_EXPECTED_ORGANIZATION_ID",
    "CITADEL_BW_EXPECTED_COLLECTION_ID",
    "CITADEL_BW_EXPECTED_FOLDER_ID",
    "CITADEL_BW_EXPECTED_FOLDER_NAME",
)

EXPLICIT_GENERATED_HAR_TARGET_MONTH_KEYS = (
    "MORTGAGE_DOWNLOADER_GENERATED_HAR_TARGET_MONTH",
)

EXPLICIT_MORTGAGEQUESTIONS_TARGET_MONTH_KEYS = (
    "BASELANE_MORTGAGE_MORTGAGEQUESTIONS_STATEMENT_TARGET_MONTH",
    "MORTGAGE_DOWNLOADER_MORTGAGEQUESTIONS_TARGET_MONTH",
    "MORTGAGEQUESTIONS_STATEMENT_TARGET_MONTH",
    "MORTGAGEQUESTIONS_TARGET_MONTH",
)

MORTGAGEQUESTIONS_TARGET_MONTH_OFFSET_KEYS = (
    "MORTGAGE_DOWNLOADER_MORTGAGEQUESTIONS_TARGET_MONTH_DEFAULT_OFFSET",
    "MORTGAGEQUESTIONS_TARGET_MONTH_DEFAULT_OFFSET",
)

CITADEL_DOWNSTREAM_ALIAS_MAP = {
    "mortgage_downloader_citadel_safe_to_run_automatically": "citadel_safe_to_run_automatically",
    "mortgage_downloader_citadel_idempotent_replay_safe": "citadel_idempotent_replay_safe",
    "mortgage_downloader_citadel_har_replay_ready_to_run_automatically": "citadel_har_replay_ready_to_run_automatically",
    "mortgage_downloader_citadel_automation_readiness_status": "citadel_automation_readiness_status",
    "mortgage_downloader_citadel_automation_blockers": "citadel_automation_blockers",
    "mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_report": "citadel_har_workflow_next_action_install_verified_capture_report",
    "mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_dry_run_command": "citadel_har_workflow_next_action_install_verified_capture_dry_run_command",
    "mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_apply_command": "citadel_har_workflow_next_action_install_verified_capture_apply_command",
    "mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_dry_run_command": "citadel_har_workflow_next_action_install_verified_capture_direct_dry_run_command",
    "mortgage_downloader_citadel_har_workflow_next_action_install_verified_capture_direct_apply_command": "citadel_har_workflow_next_action_install_verified_capture_direct_apply_command",
    "mortgage_downloader_citadel_install_verified_capture_report": "citadel_install_verified_capture_report",
    "mortgage_downloader_citadel_install_verified_capture_apply_command": "citadel_install_verified_capture_apply_command",
    "mortgage_downloader_citadel_install_verified_capture_direct_apply_command": "citadel_install_verified_capture_direct_apply_command",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


VOLATILE_REPORT_FIELDS = {"started_at", "ended_at", "idempotency_digest"}


def without_volatile_report_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: without_volatile_report_fields(item)
            for key, item in value.items()
            if key not in VOLATILE_REPORT_FIELDS
        }
    if isinstance(value, list):
        return [without_volatile_report_fields(item) for item in value]
    return value


def restore_volatile_report_fields(current: Any, previous: Any) -> Any:
    if isinstance(current, dict) and isinstance(previous, dict):
        restored = dict(current)
        for key, value in current.items():
            if key in VOLATILE_REPORT_FIELDS and key in previous:
                restored[key] = previous[key]
            elif key in previous:
                restored[key] = restore_volatile_report_fields(value, previous[key])
        return restored
    if isinstance(current, list) and isinstance(previous, list) and len(current) == len(previous):
        return [restore_volatile_report_fields(item, previous[index]) for index, item in enumerate(current)]
    return current


def preserve_volatile_fields_if_unchanged(report: dict[str, Any], path: Path) -> dict[str, Any]:
    if not path.exists():
        return report
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return report
    if not isinstance(previous, dict):
        return report
    if without_volatile_report_fields(previous) != without_volatile_report_fields(report):
        return report
    restored = restore_volatile_report_fields(report, previous)
    return restored if isinstance(restored, dict) else report


def write_json_report(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    report = dict(report)
    report["idempotency_digest"] = stable_report_digest(report, volatile_fields=VOLATILE_REPORT_FIELDS)
    stable_report = preserve_volatile_fields_if_unchanged(report, path)
    content = json.dumps(stable_report, indent=2, sort_keys=True)
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")
    return stable_report


def sync_citadel_downstream_aliases(report: dict[str, Any]) -> None:
    for downstream_key, source_key in CITADEL_DOWNSTREAM_ALIAS_MAP.items():
        report[downstream_key] = report.get(source_key)


def tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data.get("downloaders"), list):
        raise ValueError("mortgage downloader config must contain a downloaders list")
    return data


def resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return WORKSPACE_ROOT / path


def configured_downloader_report_paths(config_path: Path = DEFAULT_CONFIG) -> list[Path]:
    try:
        config = load_config(config_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    paths: list[Path] = []
    for entry in config.get("downloaders", []):
        if not isinstance(entry, dict):
            continue
        report = str(entry.get("report") or "").strip()
        if report:
            paths.append(resolve_path(report))
        profiles = entry.get("profiles")
        if not isinstance(profiles, dict):
            continue
        for profile in profiles.values():
            if not isinstance(profile, dict):
                continue
            profile_report = str(profile.get("report") or "").strip()
            if profile_report:
                paths.append(resolve_path(profile_report))
    return paths


def runtime_command(runtime: str, script_value: str) -> tuple[list[str] | None, str | None]:
    script = resolve_path(script_value) if script_value else None
    if runtime not in RUNTIMES:
        return None, f"unsupported runtime: {runtime}"
    if not script or not script.exists():
        return None, f"script missing: {script_value}"
    return RUNTIMES[runtime](script), None


def build_env(*env_maps: dict[str, Any] | None) -> dict[str, str]:
    env = os.environ.copy()
    for env_map in env_maps:
        for key, value in (env_map or {}).items():
            env[str(key)] = str(value)
    return env


def preserve_explicit_env_overrides(env: dict[str, str]) -> dict[str, str]:
    for key in EXPLICIT_ENV_OVERRIDE_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def target_month_env(target_month: str | None) -> dict[str, str]:
    value = str(target_month or "").strip()
    if not value:
        return {}
    env = {
        "MORTGAGE_STATEMENT_TARGET_MONTH": value,
        "BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH": value,
        "CITADEL_TARGET_MONTH": value,
    }
    return env


def first_nonempty_env(keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def exact_target_month_enabled() -> bool:
    return str(os.environ.get("MORTGAGE_DOWNLOADER_EXACT_TARGET_MONTH") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def default_target_month_offset_enabled(base_month: str) -> bool:
    if exact_target_month_enabled():
        return False
    if not re.fullmatch(r"20\d{2}-\d{2}", str(base_month or "")):
        return True
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    return str(base_month) >= current_month


def truthy_env_value(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def slugify(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "unknown"


def normalize_property(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def workflow_handoff_summary(property_name: object, handoff_dir: Path = DEFAULT_HANDOFF_DIR) -> dict[str, Any]:
    path = handoff_dir / f"mortgage_workflow_evidence_handoff_{slugify(property_name)}.json"
    data = load_json_file(path)
    if not isinstance(data, dict):
        return {}
    return {
        "target_statement_month": data.get("target_statement_month"),
        "target_month_document_identifiers": string_list(data.get("target_month_document_identifiers")),
        "required_capture_quality": data.get("required_capture_quality"),
        "suggested_next_action": data.get("suggested_next_action"),
    }


def supplemental_downloader_report_paths(report_path: Path, config_path: Path = DEFAULT_CONFIG) -> list[Path]:
    configured = str(os.environ.get("MORTGAGE_DOWNLOADER_SUPPLEMENTAL_REPORTS") or "").strip()
    if configured:
        paths = [Path(item.strip()) for item in configured.split(os.pathsep) if item.strip()]
    else:
        paths = configured_downloader_report_paths(config_path) + list(DEFAULT_SUPPLEMENTAL_DOWNLOADER_REPORTS)
    resolved_report = report_path.resolve()
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path if path.is_absolute() else WORKSPACE_ROOT / path
        try:
            resolved_key = resolved.resolve()
            if resolved_key == resolved_report or resolved_key in seen:
                continue
            seen.add(resolved_key)
        except OSError:
            if resolved in seen:
                continue
            seen.add(resolved)
        result.append(resolved)
    return result


def supplemental_context_is_auth_context(item: dict[str, Any]) -> bool:
    if item.get("manual_auth_required") is True:
        return True
    if str(item.get("manual_auth_reason") or "").strip():
        return True
    if str(item.get("auth_failure_reason") or "").strip():
        return True
    if str(item.get("auth_issue") or "").strip() in {"not_authenticated", "api_auth_failed", "credentials_unavailable"}:
        return True
    return str(item.get("suggested_next_action") or "").strip() == "authenticate_visible_loandepot_tab_then_run_live_cdp"


def supplemental_context_target_month(item: dict[str, Any]) -> str:
    return str(
        item.get("target_month")
        or item.get("expected_target_month")
        or item.get("target_statement_month")
        or ""
    ).strip()


def supplemental_context_items(report: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if supplemental_context_is_auth_context(report):
        item = dict(report)
        credential_diagnostics = item.get("credential_diagnostics")
        if isinstance(credential_diagnostics, dict):
            for key in SUPPLEMENTAL_AUTH_CONTEXT_FIELDS:
                if key.startswith("credential_") and item.get(key) in (None, "", []):
                    value = credential_diagnostics.get(key)
                    if value not in (None, "", []):
                        item[key] = value
        items.append(item)
    for key in ("target_month_statement_gaps", "results", "downloader_summaries"):
        values = report.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict) and supplemental_context_is_auth_context(value):
                items.append(value)
    return items


def load_supplemental_downloader_contexts(
    report_path: Path,
    config_path: Path = DEFAULT_CONFIG,
) -> dict[str, list[dict[str, Any]]]:
    contexts: dict[str, list[dict[str, Any]]] = {}
    for source_order, path in enumerate(supplemental_downloader_report_paths(report_path, config_path=config_path)):
        data = load_json_file(path)
        if not isinstance(data, dict):
            continue
        for item in supplemental_context_items(data):
            item_with_source = dict(item)
            item_with_source["supplemental_downloader_report"] = str(path)
            item_with_source["_supplemental_source_order"] = source_order
            for key in (
                f"id:{str(item.get('id') or '').strip()}",
                f"property:{normalize_property(item.get('property'))}",
            ):
                if key.split(":", 1)[1]:
                    contexts.setdefault(key, []).append(item_with_source)
    return contexts


def supplemental_context_for_result(
    result: dict[str, Any],
    contexts: dict[str, list[dict[str, Any]]],
    expected_target_month: str | None,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    downloader_id = str(result.get("id") or "").strip()
    if downloader_id:
        candidates.extend(contexts.get(f"id:{downloader_id}", []))
    prop_key = normalize_property(result.get("property"))
    if prop_key:
        candidates.extend(contexts.get(f"property:{prop_key}", []))
    seen: set[int] = set()
    unique_candidates: list[dict[str, Any]] = []
    for item in candidates:
        marker = id(item)
        if marker in seen:
            continue
        seen.add(marker)
        unique_candidates.append(item)
    unique_candidates.sort(key=lambda item: int(item.get("_supplemental_source_order") or 0))
    expected = str(expected_target_month or result.get("expected_target_month") or "").strip()
    for item in unique_candidates:
        item_month = supplemental_context_target_month(item)
        if expected and item_month and item_month != expected:
            continue
        return item
    return None


def summary_has_explicit_auth_context(summary: dict[str, Any]) -> bool:
    if summary.get("manual_auth_required") is True:
        return True
    for key in (
        "manual_auth_reason",
        "auth_failure_reason",
        "auth_issue",
    ):
        if str(summary.get(key) or "").strip():
            return True
    if summary.get("credentials_available") is False and str(summary.get("credential_lookup_status") or "").strip():
        return True
    return False


def overlay_supplemental_auth_context(
    result: dict[str, Any],
    contexts: dict[str, list[dict[str, Any]]],
    expected_target_month: str | None,
) -> None:
    summary = result.get("report_summary")
    if not isinstance(summary, dict):
        return
    if summary.get("target_month_statement_available") is True:
        return
    if summary_has_explicit_auth_context(summary):
        return
    context = supplemental_context_for_result(result, contexts, expected_target_month)
    if not context:
        return
    for field in SUPPLEMENTAL_AUTH_CONTEXT_FIELDS:
        value = context.get(field)
        if value in (None, "") or (value == [] and field != "credential_lookup_candidate_items"):
            continue
        if field == "status":
            summary["latest_supplemental_auth_status"] = value
            continue
        if field == "reason":
            summary["latest_supplemental_auth_reason"] = value
            if summary.get("manual_auth_reason") in (None, "", []) and str(value).strip():
                summary["manual_auth_reason"] = value
            continue
        if field in SUPPLEMENTAL_TARGET_CONTEXT_REPLACE_FIELDS:
            summary[field] = value
        elif field in {"manual_auth_required", "manual_auth_reason", "manual_auth_portal_url", "auth_state", "auth_stage", "auth_issue", "auth_issue_text", "operator_next_action", "suggested_next_action"}:
            summary[field] = value
        elif summary.get(field) in (None, "", []):
            summary[field] = value
    summary["latest_supplemental_auth_report"] = context.get("supplemental_downloader_report")
    summary["latest_supplemental_auth_applied"] = True
    apply_report_summary_to_result(result, summary)


def summary_target_month(summary: dict[str, Any], handoff: dict[str, Any], expected_target_month: str | None) -> object:
    handoff_target = handoff.get("target_statement_month")
    report_target = summary.get("target_month")
    if handoff_target and report_target and handoff_target != report_target:
        if expected_target_month and report_target == expected_target_month:
            return report_target
        return handoff_target
    return handoff_target or report_target


def summary_target_document_identifiers(
    summary: dict[str, Any],
    handoff: dict[str, Any],
    expected_target_month: str | None,
) -> list[str]:
    report_ids = string_list(summary.get("target_month_document_identifiers"))
    handoff_ids = string_list(handoff.get("target_month_document_identifiers"))
    handoff_target = handoff.get("target_statement_month")
    report_target = summary.get("target_month")
    if handoff_target and report_target and handoff_target != report_target:
        if expected_target_month and report_target == expected_target_month:
            return report_ids or handoff_ids
        return report_ids or handoff_ids
    return report_ids or handoff_ids


def bitwarden_write_preflight(env: dict[str, str]) -> dict[str, Any]:
    requested_by = [key for key in BITWARDEN_WRITE_ENABLE_ENV_KEYS if truthy_env_value(env.get(key))]
    if not requested_by:
        return {"status": "ok", "write_requested": False}
    configured_guards = [key for key in CITADEL_BW_WRITE_GUARD_ENV_KEYS if str(env.get(key) or "").strip()]
    if configured_guards:
        return {
            "status": "ok",
            "write_requested": True,
            "write_requested_by": requested_by,
            "configured_guards": configured_guards,
        }
    return {
        "status": "blocked",
        "reason": "bitwarden_write_guard_missing",
        "write_requested": True,
        "write_requested_by": requested_by,
        "required_any": list(CITADEL_BW_WRITE_GUARD_ENV_KEYS),
    }


def valid_year_month(value: str) -> bool:
    if re.fullmatch(r"20\d{2}-\d{2}", value) is None:
        return False
    month = int(value.split("-", 1)[1])
    return 1 <= month <= 12


def add_months(month: str, offset: int) -> str:
    year, mon = [int(part) for part in month.split("-")]
    absolute = year * 12 + (mon - 1) + offset
    target_year, target_month_zero = divmod(absolute, 12)
    return f"{target_year:04d}-{target_month_zero + 1:02d}"


def offset_target_month(base: str, offset_value: object) -> str:
    if not valid_year_month(base):
        return base
    try:
        offset = int(str(offset_value or "0").strip())
    except ValueError:
        offset = 0
    return add_months(base, offset)


def first_nonempty_mapping_value(mapping: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(mapping.get(key) or "").strip()
        if value:
            return value
    return ""


def entry_config_env(entry: dict[str, Any], profile_name: str) -> dict[str, str]:
    profile = active_profile(entry, profile_name)
    env: dict[str, str] = {}
    for env_map in (entry.get("env"), profile.get("env") if profile else None):
        for key, value in (env_map or {}).items():
            env[str(key)] = str(value)
    return env


def entry_marker(entry: dict[str, Any]) -> str:
    return " ".join(str(entry.get(key) or "") for key in ("id", "property", "servicer", "script")).casefold()


def is_citadel_entry(entry: dict[str, Any]) -> bool:
    marker = entry_marker(entry)
    return "citadel" in marker or "loansphere" in marker


def is_generated_har_entry(entry: dict[str, Any]) -> bool:
    marker = entry_marker(entry)
    return "generated_mortgage" in marker or "generated-" in marker


def is_mortgagequestions_entry(entry: dict[str, Any]) -> bool:
    marker = entry_marker(entry)
    return "mortgagequestions" in marker or "onity" in marker or "phh" in marker


def expected_target_month_for_entry(
    entry: dict[str, Any],
    *,
    profile_name: str,
    global_target_month: str | None,
) -> str | None:
    profile = active_profile(entry, profile_name)
    env = preserve_explicit_env_overrides(
        build_env(entry.get("env"), profile.get("env") if profile else None, target_month_env_for_entry(entry, profile_name, global_target_month))
    )
    if is_citadel_entry(entry):
        for key in ("CITADEL_STATEMENT_TARGET_MONTH", "CITADEL_TARGET_MONTH"):
            value = str(env.get(key) or "").strip()
            if value:
                return value
    if is_mortgagequestions_entry(entry):
        for key in ("MORTGAGEQUESTIONS_TARGET_MONTH", "MORTGAGEQUESTIONS_STATEMENT_TARGET_MONTH"):
            value = str(env.get(key) or "").strip()
            if value:
                return value
    if is_generated_har_entry(entry) and not profile.get("script"):
        value = str(env.get("MORTGAGE_GENERATED_HAR_TARGET_MONTH") or "").strip()
        if value:
            return value
        for key in (
            "MORTGAGE_STATEMENT_TARGET_MONTH",
            "BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH",
            "MORTGAGE_WORKFLOW_TARGET_MONTH",
            "BASELANE_MONTHLY_TARGET_STAMP",
        ):
            base = str(env.get(key) or "").strip()
            if base:
                return base
    for key in (
        "MORTGAGE_STATEMENT_TARGET_MONTH",
        "BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH",
        "MORTGAGE_WORKFLOW_TARGET_MONTH",
        "BASELANE_MONTHLY_TARGET_STAMP",
    ):
        value = str(env.get(key) or "").strip()
        if value:
            return value
    return None


def citadel_target_month_for_entry(
    entry: dict[str, Any],
    *,
    profile_name: str,
    global_target_month: str | None,
) -> str:
    explicit_target = first_nonempty_env(EXPLICIT_CITADEL_TARGET_MONTH_KEYS)
    if explicit_target:
        return explicit_target
    config_env = entry_config_env(entry, profile_name)
    for key in ("CITADEL_STATEMENT_TARGET_MONTH", "CITADEL_TARGET_MONTH"):
        value = str(config_env.get(key) or "").strip()
        if value:
            return value
    base = str(global_target_month or "").strip()
    if not base:
        return ""
    if not default_target_month_offset_enabled(base):
        return base
    offset = first_nonempty_mapping_value(config_env, CITADEL_TARGET_MONTH_OFFSET_KEYS)
    return offset_target_month(base, offset) if offset else base


def generated_har_target_month_for_entry(
    entry: dict[str, Any],
    *,
    profile_name: str,
    global_target_month: str | None,
) -> str:
    config_env = entry_config_env(entry, profile_name)
    explicit = first_nonempty_env(EXPLICIT_GENERATED_HAR_TARGET_MONTH_KEYS)
    if explicit:
        return explicit
    explicit = str(config_env.get("MORTGAGE_GENERATED_HAR_TARGET_MONTH") or "").strip()
    if explicit:
        return explicit
    base = str(global_target_month or "").strip()
    if not base:
        return ""
    if not default_target_month_offset_enabled(base):
        return base
    offset = first_nonempty_mapping_value(config_env, GENERATED_HAR_TARGET_MONTH_OFFSET_KEYS)
    return offset_target_month(base, offset) if offset else base


def mortgagequestions_target_month_for_entry(
    entry: dict[str, Any],
    *,
    profile_name: str,
    global_target_month: str | None,
) -> str:
    explicit_target = first_nonempty_env(EXPLICIT_MORTGAGEQUESTIONS_TARGET_MONTH_KEYS)
    if explicit_target:
        return explicit_target
    config_env = entry_config_env(entry, profile_name)
    for key in ("MORTGAGEQUESTIONS_STATEMENT_TARGET_MONTH", "MORTGAGEQUESTIONS_TARGET_MONTH"):
        value = str(config_env.get(key) or "").strip()
        if value:
            return value
    base = str(global_target_month or "").strip()
    if not base:
        return ""
    if not default_target_month_offset_enabled(base):
        return base
    offset = first_nonempty_mapping_value(config_env, MORTGAGEQUESTIONS_TARGET_MONTH_OFFSET_KEYS)
    return offset_target_month(base, offset) if offset else base


def target_month_env_for_entry(
    entry: dict[str, Any],
    profile_name: str,
    target_month: str | None,
) -> dict[str, str]:
    profile = active_profile(entry, profile_name)
    env = target_month_env(target_month)
    if is_citadel_entry(entry):
        citadel_target_month = citadel_target_month_for_entry(
            entry,
            profile_name=profile_name,
            global_target_month=target_month,
        )
        if citadel_target_month:
            env["CITADEL_TARGET_MONTH"] = citadel_target_month
    if is_mortgagequestions_entry(entry):
        mortgagequestions_target_month = mortgagequestions_target_month_for_entry(
            entry,
            profile_name=profile_name,
            global_target_month=target_month,
        )
        if mortgagequestions_target_month:
            env["MORTGAGEQUESTIONS_TARGET_MONTH"] = mortgagequestions_target_month
    if is_generated_har_entry(entry) and not profile.get("script"):
        generated_target_month = generated_har_target_month_for_entry(
            entry,
            profile_name=profile_name,
            global_target_month=target_month,
        )
        if generated_target_month:
            env["MORTGAGE_GENERATED_HAR_TARGET_MONTH"] = generated_target_month
    return env


def update_expected_target_month_summary(report: dict[str, Any]) -> None:
    months: list[str] = []
    for result in report.get("results") or []:
        if not result.get("enabled") or not result.get("co_owner_paid_mortgage"):
            continue
        month = str(result.get("expected_target_month") or "").strip()
        if month and month not in months:
            months.append(month)
    months.sort()
    report["downloader_expected_target_months"] = months
    report["downloader_expected_target_month_count"] = len(months)
    report["downloader_effective_statement_target_month"] = months[0] if len(months) == 1 else None
    report["downloader_statement_target_month"] = report["downloader_effective_statement_target_month"]
    if report["downloader_effective_statement_target_month"]:
        report["mortgage_statement_target_month"] = report["downloader_effective_statement_target_month"]
    global_month = str(report.get("target_month") or "").strip()
    if global_month and months:
        report["target_month_matches_all_downloader_expected_months"] = all(month == global_month for month in months)
        report["target_month_differs_from_downloader_expected_months"] = (
            not report["target_month_matches_all_downloader_expected_months"]
        )
    else:
        report["target_month_matches_all_downloader_expected_months"] = None
        report["target_month_differs_from_downloader_expected_months"] = None
    if not global_month:
        policy = "no_global_target_month"
    elif report["target_month_matches_all_downloader_expected_months"] is True:
        policy = "exact_workflow_month"
    elif report["target_month_differs_from_downloader_expected_months"] is True:
        policy = "downloader_statement_month_offset"
    else:
        policy = "unknown"
    report["downloader_statement_target_month_policy"] = policy
    report["workflow_target_month"] = global_month or None
    report["workflow_target_month_kind"] = "workflow_month" if global_month else None
    report["statement_target_month_kind"] = "statement_month" if months else None
    report["workflow_target_month_differs_from_statement_target_month"] = (
        bool(global_month and report["downloader_effective_statement_target_month"])
        and global_month != report["downloader_effective_statement_target_month"]
    )


def positive_int_env(key: str, default: int, env: dict[str, str] | None = None) -> int:
    source = env if env is not None and key in env else os.environ
    value = str(source.get(key) or "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def output_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def run_command(command: list[str], env: dict[str, str]) -> dict[str, Any]:
    started_at = utc_now()
    timeout_seconds = positive_int_env("MORTGAGE_DOWNLOADER_COMMAND_TIMEOUT_SECONDS", 360, env)
    proc = subprocess.Popen(
        command,
        cwd=str(WORKSPACE_ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    ACTIVE_CHILD_PROCESS_GROUPS.add(proc.pid)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        return {
            "status": "timeout",
            "rc": 124,
            "started_at": started_at,
            "ended_at": utc_now(),
            "timeout_seconds": timeout_seconds,
            "stdout_tail": tail(output_text(stdout or exc.stdout)),
            "stderr_tail": tail(output_text(stderr or exc.stderr)),
            "error": f"command timed out after {timeout_seconds}s",
        }
    finally:
        ACTIVE_CHILD_PROCESS_GROUPS.discard(proc.pid)
    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "rc": proc.returncode,
        "started_at": started_at,
        "ended_at": utc_now(),
        "timeout_seconds": timeout_seconds,
        "stdout_tail": tail(stdout),
        "stderr_tail": tail(stderr),
    }


def count_list(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, list):
        return len(value)
    return None


def count_value(data: dict[str, Any], list_key: str, count_key: str) -> int | None:
    counted = count_list(data, list_key)
    if counted is not None:
        return counted
    value = data.get(count_key)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def latest_citadel_live_auth_report(report_path: Path) -> tuple[Path, dict[str, Any]] | None:
    candidates = sorted(
        report_path.parent.glob("citadel_live_login_attempt_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            return candidate, data
    return None


def overlay_latest_citadel_live_auth_if_idempotent(
    entry: dict[str, Any], report_path: Path, data: dict[str, Any], summary: dict[str, Any]
) -> None:
    entry_id = str(entry.get("id") or "").lower()
    servicer = str(entry.get("servicer") or "").lower()
    if "citadel" not in entry_id and "citadel" not in servicer:
        return
    if data.get("idempotent_skip") is not True:
        return
    latest = latest_citadel_live_auth_report(report_path)
    if latest is None:
        return
    latest_path, latest_data = latest
    latest_status = str(latest_data.get("status") or "")
    if latest_status in {"", "ok"}:
        return
    summary.update(
        original_report_status=data.get("status"),
        idempotent_skip_latest_live_auth_attention=True,
        latest_live_auth_report=str(latest_path),
        latest_live_auth_status=latest_status,
        latest_live_auth_credentials_available=latest_data.get("credentials_available"),
        latest_live_auth_manual_auth_required=latest_data.get("manual_auth_required") or False,
        latest_live_auth_manual_auth_reason=latest_data.get("manual_auth_reason"),
        latest_live_auth_manual_auth_file=latest_data.get("manual_auth_file"),
        latest_live_auth_manual_auth_portal_url=latest_data.get("manual_auth_portal_url"),
        latest_live_auth_auth_failure_reason=latest_data.get("auth_failure_reason"),
        latest_live_auth_auth_failure_visible_reason=latest_data.get("auth_failure_visible_reason"),
        latest_live_auth_auth_visible_error=latest_data.get("auth_visible_error"),
        latest_live_auth_login_form_last_result=latest_data.get("login_form_last_result"),
        latest_live_auth_oauth_password_grant_failure_count=latest_data.get("oauth_password_grant_failure_count"),
        latest_live_auth_oauth_password_grant_error_codes=latest_data.get("oauth_password_grant_error_codes"),
        latest_live_auth_login_form_submitted=latest_data.get("login_form_submitted"),
    )


def read_report_summary(entry: dict[str, Any]) -> dict[str, Any] | None:
    report_value = str(entry.get("report") or "")
    if not report_value:
        return None
    report_path = resolve_path(report_value)
    summary: dict[str, Any] = {"path": str(report_path)}
    if not report_path.exists():
        summary["status"] = "missing"
        return summary
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        summary.update(status="unreadable", error=str(exc))
        return summary

    login_page_state = data.get("login_page_state_final") or data.get("login_page_state") or {}
    if not isinstance(login_page_state, dict):
        login_page_state = {}
    direct_auth = data.get("direct_auth") or {}
    if not isinstance(direct_auth, dict):
        direct_auth = {}
    credential_diagnostics = data.get("credential_diagnostics") or {}
    if not isinstance(credential_diagnostics, dict):
        credential_diagnostics = {}
    def credential_value(key: str) -> Any:
        value = credential_diagnostics.get(key)
        return value if value is not None else data.get(key)
    har_workflow_diagnostics = data.get("har_workflow_diagnostics") or {}
    if not isinstance(har_workflow_diagnostics, dict):
        har_workflow_diagnostics = {}
    har_workflow_next_action = har_workflow_diagnostics.get("next_action") or {}
    if not isinstance(har_workflow_next_action, dict):
        har_workflow_next_action = {}
    def har_workflow_value(flat_key: str, nested_key: str) -> Any:
        value = data.get(flat_key)
        return value if value is not None else har_workflow_diagnostics.get(nested_key)
    citadel_tab_scan = data.get("citadel_tab_scan") or {}
    if not isinstance(citadel_tab_scan, dict):
        citadel_tab_scan = {}
    oauth_events = data.get("oauth_network_events") or []
    if not isinstance(oauth_events, list):
        oauth_events = []
    api_headers_shape = data.get("api_headers_shape") or citadel_tab_scan.get("api_headers_shape") or {}
    if not isinstance(api_headers_shape, dict):
        api_headers_shape = {}
    oauth_error_codes: list[str] = []
    oauth_statuses: list[int] = []
    oauth_request_shape_statuses: list[int] = []
    oauth_request_structure_statuses: list[int] = []
    for event in oauth_events:
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("status"), int):
            oauth_statuses.append(event["status"])
        for status in event.get("request_shape_matched_har_statuses") or []:
            if isinstance(status, int) and status not in oauth_request_shape_statuses:
                oauth_request_shape_statuses.append(status)
        for status in event.get("request_structure_matched_har_statuses") or []:
            if isinstance(status, int) and status not in oauth_request_structure_statuses:
                oauth_request_structure_statuses.append(status)
        summary_error = event.get("error_summary") or {}
        if isinstance(summary_error, dict):
            for code in summary_error.get("error_message_codes") or []:
                if str(code) not in oauth_error_codes:
                    oauth_error_codes.append(str(code))
    summary.update(
        status=data.get("status"),
        reason=data.get("reason"),
        har_path=data.get("har_path"),
        har_path_exists=data.get("har_path_exists"),
        candidate_count=data.get("candidate_count"),
        candidate_source_counts=data.get("candidate_source_counts"),
        available_statement_months=data.get("available_statement_months"),
        downloadable_statement_months=data.get("downloadable_statement_months"),
        metadata_only_statement_months=data.get("metadata_only_statement_months"),
        latest_statement_month=data.get("latest_statement_month"),
        latest_downloadable_statement_month=data.get("latest_downloadable_statement_month"),
        target_month_candidate_count=data.get("target_month_candidate_count"),
        target_month_statement_candidates=data.get("target_month_statement_candidates"),
        target_month_candidate_source_counts=data.get("target_month_candidate_source_counts"),
        target_month_downloadable_count=data.get("target_month_downloadable_count"),
        target_month_downloadable_source_counts=data.get("target_month_downloadable_source_counts"),
        target_month_recapture_required=data.get("target_month_recapture_required"),
        target_month_recapture_reason=data.get("target_month_recapture_reason"),
        credentials_available=data.get("credentials_available"),
        credential_source=data.get("credential_source"),
        credential_lookup_status=credential_value("credential_lookup_status"),
        credential_lookup_failure_reason=credential_value("credential_lookup_failure_reason"),
        credential_lookup_exit_code=credential_value("credential_lookup_exit_code"),
        credential_lookup_item_name=credential_value("credential_lookup_item_name"),
        credential_lookup_item_id_configured=credential_value("credential_lookup_item_id_configured"),
        credential_lookup_uri_host=credential_value("credential_lookup_uri_host"),
        credential_lookup_uri_host_aliases=credential_value("credential_lookup_uri_host_aliases"),
        credential_lookup_login_hint_configured=credential_value("credential_lookup_login_hint_configured"),
        credential_lookup_search_term_count=credential_value("credential_lookup_search_term_count"),
        credential_lookup_search_terms=credential_value("credential_lookup_search_terms"),
        credential_lookup_candidate_search_term_count=credential_value("credential_lookup_candidate_search_term_count"),
        credential_lookup_candidate_search_terms=credential_value("credential_lookup_candidate_search_terms"),
        credential_lookup_expected_folder_name=credential_value("credential_lookup_expected_folder_name"),
        credential_lookup_expected_folder_id_configured=credential_value("credential_lookup_expected_folder_id_configured"),
        credential_lookup_script=credential_value("credential_lookup_script"),
        credential_lookup_candidate_count=credential_value("credential_lookup_candidate_count"),
        credential_lookup_candidate_items=credential_value("credential_lookup_candidate_items"),
        credential_lookup_unguarded_candidate_count=credential_value("credential_lookup_unguarded_candidate_count"),
        credential_lookup_unguarded_candidate_items=credential_value("credential_lookup_unguarded_candidate_items"),
        credential_lookup_misfiled_candidate_count=credential_value("credential_lookup_misfiled_candidate_count"),
        credential_lookup_absent=credential_value("credential_lookup_absent"),
        credential_lookup_scope=credential_value("credential_lookup_scope"),
        credential_lookup_repair_action=credential_value("credential_lookup_repair_action"),
        credential_login_hint_mismatch=credential_value("credential_login_hint_mismatch"),
        credential_login_hint_mismatch_overridden=credential_value("credential_login_hint_mismatch_overridden"),
        credential_repair_instruction=data.get("credential_repair_instruction"),
        downloaded_count=count_value(data, "downloaded_files", "target_month_downloaded_count"),
        skipped_count=count_value(data, "skipped_files", "target_month_skipped_count"),
        target_month=data.get("target_month"),
        target_month_statement_available=data.get("target_month_statement_available"),
        target_month_existing_count=data.get("target_month_existing_count"),
        target_month_downloaded_count=data.get("target_month_downloaded_count"),
        target_month_skipped_count=data.get("target_month_skipped_count"),
        target_month_document_identifiers=data.get("target_month_document_identifiers"),
        expected_document_ids=data.get("expected_document_ids"),
        body_recapture_capture_method=data.get("body_recapture_capture_method"),
        body_recapture_capture_instruction=data.get("body_recapture_capture_instruction"),
        body_recapture_manual_har_export_warning=data.get("body_recapture_manual_har_export_warning"),
        required_capture_quality=data.get("required_capture_quality"),
        suggested_next_action=data.get("suggested_next_action"),
        existing_target_month_files=data.get("existing_target_month_files"),
        downloaded_target_month_files=data.get("downloaded_target_month_files"),
        skipped_target_month_files=data.get("skipped_target_month_files"),
        safe_to_run_automatically=data.get("safe_to_run_automatically"),
        idempotent_replay_safe=data.get("idempotent_replay_safe"),
        copy_plan_safe_to_apply_automatically=data.get("copy_plan_safe_to_apply_automatically"),
        automation_readiness_status=data.get("automation_readiness_status"),
        automation_blockers=data.get("automation_blockers"),
        har_replay_ready_to_run_automatically=data.get("har_replay_ready_to_run_automatically"),
        idempotency_digest=data.get("idempotency_digest"),
        idempotent_skip=data.get("idempotent_skip"),
        idempotent_skip_reason=data.get("idempotent_skip_reason"),
        error_count=count_list(data, "errors"),
        warning_count=count_list(data, "warnings"),
        login_mode=data.get("login_mode"),
        otp_required=data.get("otp_required") or data.get("status") == "otp_required",
        otp_wait_ms=data.get("otp_wait_ms"),
        otp_file=data.get("otp_file"),
        otp_required_file=data.get("otp_required_file"),
        otp_next_command=data.get("otp_next_command"),
        auto_otp_attempted=data.get("auto_otp_attempted"),
        auto_otp_status=data.get("auto_otp_status"),
        auto_otp_code_available=data.get("auto_otp_code_available"),
        auto_otp_source=data.get("auto_otp_source"),
        auto_otp_fetch_enabled=data.get("auto_otp_fetch_enabled"),
        auto_otp_fetch_attempted=data.get("auto_otp_fetch_attempted"),
        auto_otp_fetch_attempt_count=data.get("auto_otp_fetch_attempt_count"),
        auto_otp_fetch_status=data.get("auto_otp_fetch_status"),
        auto_otp_fetch_exit_code=data.get("auto_otp_fetch_exit_code"),
        auto_otp_fetch_report=data.get("auto_otp_fetch_report"),
        manual_auth_required=data.get("manual_auth_required") or False,
        manual_auth_reason=data.get("manual_auth_reason"),
        manual_auth_file=data.get("manual_auth_file"),
        manual_auth_portal_url=data.get("manual_auth_portal_url"),
        auth_state=data.get("auth_state") or login_page_state.get("state"),
        auth_stage=data.get("auth_stage"),
        auth_issue=data.get("auth_issue"),
        auth_issue_text=data.get("auth_issue_text"),
        auto_login_status=data.get("auto_login_status"),
        auto_login_blocked_reason=data.get("auto_login_blocked_reason"),
        auto_login_force_enabled=data.get("auto_login_force_enabled"),
        auto_login_username_available=data.get("auto_login_username_available"),
        auto_login_password_available=data.get("auto_login_password_available"),
        operator_next_action=data.get("operator_next_action"),
        auth_failure_reason=data.get("auth_failure_reason"),
        auth_failure_visible_reason=data.get("auth_failure_visible_reason"),
        auth_mfa_reached=data.get("auth_mfa_reached"),
        auth_visible_error=data.get("auth_visible_error"),
        auto_login_attempted=data.get("auto_login_attempted"),
        auto_login_input_method=data.get("auto_login_input_method"),
        auto_login_step=data.get("auto_login_step"),
        credential_state_drift_suspected=data.get("credential_state_drift_suspected")
        or direct_auth.get("credential_state_drift_suspected")
        or False,
        credential_state_drift_checked=data.get("credential_state_drift_checked")
        if data.get("credential_state_drift_checked") is not None
        else direct_auth.get("credential_state_drift_checked"),
        credential_state_drift_basis=data.get("credential_state_drift_basis")
        or direct_auth.get("credential_state_drift_basis"),
        credential_login_failure_suspected=data.get("credential_login_failure_suspected"),
        credential_login_failure_suspected_reason=data.get("credential_login_failure_suspected_reason"),
        login_form_last_result=data.get("login_form_last_result"),
        oauth_password_grant_failure_count=data.get("oauth_password_grant_failure_count"),
        oauth_password_grant_error_codes=data.get("oauth_password_grant_error_codes"),
        browser_storage_bearer_token_available=data.get("browser_storage_bearer_token_available"),
        browser_storage_mobile_source_id_available=data.get("browser_storage_mobile_source_id_available"),
        browser_storage_token_candidate_count=data.get("browser_storage_token_candidate_count"),
        api_header_mobile_source_id_available=api_headers_shape.get("mobile_source_id"),
        api_header_authorization_enabled=api_headers_shape.get("authorization"),
        direct_auth_status=direct_auth.get("status"),
        direct_auth_transport=direct_auth.get("auth_transport"),
        direct_browser_fallback_attempted=direct_auth.get("browser_fallback_attempted"),
        direct_browser_fallback_authenticated_found=direct_auth.get("browser_fallback_authenticated_found"),
        direct_fresh_mfa_source_status=direct_auth.get("fresh_mfa_source_status"),
        direct_fresh_recaptcha_token_available=direct_auth.get("fresh_recaptcha_token_available"),
        direct_fresh_recaptcha_token_length=direct_auth.get("fresh_recaptcha_token_length"),
        direct_recaptcha_eval_stage=direct_auth.get("recaptcha_eval_stage"),
        direct_recaptcha_eval_error=direct_auth.get("recaptcha_eval_error"),
        direct_recaptcha_token_action=direct_auth.get("recaptcha_token_action"),
        direct_recaptcha_action_errors=direct_auth.get("recaptcha_action_errors"),
        direct_mfa_process_id_header_available=direct_auth.get("mfa_process_id_header_available"),
        direct_mfa_detail_available=direct_auth.get("mfa_detail_available"),
        direct_mfa_request_uuid_available=direct_auth.get("mfa_request_uuid_available"),
        direct_mfa_request_uuid_source=direct_auth.get("mfa_request_uuid_source"),
        direct_no_mfa_handoff_after_password=direct_auth.get("no_mfa_handoff_after_password"),
        direct_no_mfa_handoff_reason=direct_auth.get("no_mfa_handoff_reason"),
        direct_otp_send_via_type=direct_auth.get("otp_send_via_type"),
        direct_otp_request_status=direct_auth.get("otp_request_status"),
        direct_otp_request_shape_matches_har_success=direct_auth.get("otp_request_shape_matches_har_success"),
        direct_cdp_mfa_process_id_header_available=direct_auth.get("cdp_mfa_process_id_header_available"),
        direct_oauth_cdp_event_count=direct_auth.get("oauth_cdp_event_count"),
        direct_oauth_cdp_last_status=direct_auth.get("oauth_cdp_last_status"),
        direct_password_token_request_shape_matches_har_success=direct_auth.get("password_token_request_shape_matches_har_success"),
        direct_password_token_request_shape_matches_har_failure=direct_auth.get("password_token_request_shape_matches_har_failure"),
        direct_password_token_request_shape_matched_har_statuses=direct_auth.get("password_token_request_shape_matched_har_statuses"),
        direct_password_token_request_structure_matches_har_success=direct_auth.get("password_token_request_structure_matches_har_success"),
        direct_password_token_request_structure_matches_har_failure=direct_auth.get("password_token_request_structure_matches_har_failure"),
        direct_password_token_request_structure_matched_har_statuses=direct_auth.get("password_token_request_structure_matched_har_statuses"),
        direct_error_codes=(direct_auth.get("first_error") or {}).get("error_message_codes"),
        direct_error_categories=(direct_auth.get("first_error") or {}).get("error_message_categories"),
        credential_item_name=credential_diagnostics.get("credential_item_name"),
        credential_item_uri_hosts=credential_diagnostics.get("credential_item_uri_hosts"),
        credential_item_portal_host_match=credential_diagnostics.get("credential_item_portal_host_match"),
        credential_item_field_names=credential_diagnostics.get("credential_item_field_names"),
        credential_item_notes_len=credential_diagnostics.get("credential_item_notes_len"),
        credential_login_hint_configured=credential_diagnostics.get("credential_login_hint_configured"),
        credential_username_matches_login_hint=credential_diagnostics.get("credential_username_matches_login_hint"),
        credential_item_name_matches_login_hint=credential_diagnostics.get("credential_item_name_matches_login_hint"),
        credential_field_matches_login_hint=credential_diagnostics.get("credential_field_matches_login_hint"),
        credential_username_len=credential_diagnostics.get("username_len"),
        credential_password_len=credential_diagnostics.get("password_len"),
        credential_username_has_at=credential_diagnostics.get("username_has_at"),
        har_auth_diagnostics_enabled=credential_diagnostics.get("har_auth_diagnostics_enabled"),
        har_auth_diagnostics_skipped_reason=credential_diagnostics.get("har_auth_diagnostics_skipped_reason"),
        har_token_credential_match_count=credential_diagnostics.get("har_token_credential_match_count"),
        har_successful_password_token_match=credential_diagnostics.get("har_successful_password_token_match"),
        har_mfa_process_id_header_count=credential_diagnostics.get("har_mfa_process_id_header_count"),
        har_token_attempt_statuses=[
            event.get("status")
            for event in (credential_diagnostics.get("har_token_attempts") or [])
            if isinstance(event, dict)
        ],
        har_workflow_embedded_response_body_count=har_workflow_value("har_workflow_embedded_response_body_count", "embedded_response_body_count"),
        har_workflow_replayable_json_response_count=har_workflow_value("har_workflow_replayable_json_response_count", "replayable_json_response_count"),
        har_workflow_replayable_document_payload_count=har_workflow_value("har_workflow_replayable_document_payload_count", "replayable_document_payload_count"),
        har_workflow_target_month=har_workflow_value("har_workflow_target_month", "target_month"),
        har_workflow_target_month_replayable_document_available=har_workflow_value("har_workflow_target_month_replayable_document_available", "target_month_replayable_document_available"),
        har_workflow_target_month_replayable_document_payload_count=har_workflow_value("har_workflow_target_month_replayable_document_payload_count", "target_month_replayable_document_payload_count"),
        har_workflow_replayable_statement_months=har_workflow_value("har_workflow_replayable_statement_months", "replayable_statement_months"),
        har_workflow_statement_document_months=har_workflow_value("har_workflow_statement_document_months", "statement_document_months"),
        har_workflow_direct_pdf_response_count=har_workflow_value("har_workflow_direct_pdf_response_count", "direct_pdf_response_count"),
        har_workflow_source_direct_pdf_candidate_count=har_workflow_value("har_workflow_source_direct_pdf_candidate_count", "source_direct_pdf_candidate_count"),
        har_workflow_source_direct_pdf_path_counts=har_workflow_value("har_workflow_source_direct_pdf_path_counts", "source_direct_pdf_path_counts"),
        har_workflow_source_direct_pdf_filenames=har_workflow_value("har_workflow_source_direct_pdf_filenames", "source_direct_pdf_filenames"),
        har_workflow_source_direct_pdf_filename_candidates=har_workflow_value(
            "har_workflow_source_direct_pdf_filename_candidates",
            "source_direct_pdf_filename_candidates"
        ),
        har_workflow_target_month_direct_pdf_filenames=har_workflow_value(
            "har_workflow_target_month_direct_pdf_filenames",
            "target_month_direct_pdf_filenames"
        ),
        har_workflow_target_month_direct_pdf_filename_candidates=har_workflow_value(
            "har_workflow_target_month_direct_pdf_filename_candidates",
            "target_month_direct_pdf_filename_candidates"
        ),
        har_workflow_target_month_direct_pdf_body_missing_candidate_count=har_workflow_value(
            "har_workflow_target_month_direct_pdf_body_missing_candidate_count",
            "target_month_direct_pdf_body_missing_candidate_count"
        ),
        har_workflow_source_required_response_candidate_count=har_workflow_value("har_workflow_source_required_response_candidate_count", "source_required_response_candidate_count"),
        har_workflow_source_required_response_path_counts=har_workflow_value("har_workflow_source_required_response_path_counts", "source_required_response_path_counts"),
        har_workflow_direct_pdf_missing_response_count=har_workflow_value("har_workflow_direct_pdf_missing_response_count", "direct_pdf_missing_response_count"),
        har_workflow_direct_pdf_missing_response_paths=har_workflow_value("har_workflow_direct_pdf_missing_response_paths", "direct_pdf_missing_response_paths"),
        har_workflow_capture_quality_status=har_workflow_value("har_workflow_capture_quality_status", "capture_quality_status"),
        har_workflow_replay_blocker=har_workflow_value("har_workflow_replay_blocker", "replay_blocker"),
        har_workflow_missing_response_body_count=har_workflow_value("har_workflow_missing_response_body_count", "missing_response_body_count"),
        har_workflow_missing_response_body_paths=har_workflow_value("har_workflow_missing_response_body_paths", "missing_response_body_paths"),
        har_workflow_missing_response_body_path_counts=har_workflow_value("har_workflow_missing_response_body_path_counts", "missing_response_body_path_counts"),
        har_workflow_response_body_requirements=har_workflow_value("har_workflow_response_body_requirements", "response_body_requirements"),
        har_workflow_embedded_access_token_count=har_workflow_value("har_workflow_embedded_access_token_count", "embedded_access_token_count"),
        har_workflow_can_replay_documents=har_workflow_value("har_workflow_can_replay_documents", "can_replay_documents"),
        har_workflow_next_action_status=har_workflow_next_action.get("status"),
        har_workflow_next_action_reason=har_workflow_next_action.get("reason"),
        har_workflow_next_action_command=har_workflow_next_action.get("next_command") or har_workflow_diagnostics.get("next_command"),
        har_workflow_next_action_capture_command=har_workflow_next_action.get("capture_command"),
        har_workflow_next_action_capture_required=har_workflow_next_action.get("capture_required"),
        har_workflow_next_action_required_response_paths=har_workflow_next_action.get("required_response_paths"),
        har_workflow_next_action_response_body_requirements=har_workflow_next_action.get("response_body_requirements"),
        har_workflow_next_action_source_direct_pdf_candidate_count=har_workflow_next_action.get("source_direct_pdf_candidate_count"),
        har_workflow_next_action_source_direct_pdf_path_counts=har_workflow_next_action.get("source_direct_pdf_path_counts"),
        har_workflow_next_action_source_direct_pdf_filenames=har_workflow_next_action.get("source_direct_pdf_filenames"),
        har_workflow_next_action_target_month_direct_pdf_filenames=har_workflow_next_action.get(
            "target_month_direct_pdf_filenames"
        ),
        har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count=har_workflow_next_action.get(
            "target_month_direct_pdf_body_missing_candidate_count"
        ),
        har_workflow_next_action_source_required_response_candidate_count=har_workflow_next_action.get("source_required_response_candidate_count"),
        har_workflow_next_action_source_required_response_path_counts=har_workflow_next_action.get("source_required_response_path_counts"),
        har_workflow_next_action_install_verified_capture_report=har_workflow_next_action.get(
            "install_verified_capture_report"
        ),
        har_workflow_next_action_install_verified_capture_dry_run_command=har_workflow_next_action.get(
            "install_verified_capture_dry_run_command"
        ),
        har_workflow_next_action_install_verified_capture_apply_command=har_workflow_next_action.get(
            "install_verified_capture_apply_command"
        ),
        har_workflow_next_action_install_verified_capture_direct_dry_run_command=har_workflow_next_action.get(
            "install_verified_capture_direct_dry_run_command"
        ),
        har_workflow_next_action_install_verified_capture_direct_apply_command=har_workflow_next_action.get(
            "install_verified_capture_direct_apply_command"
        ),
        install_verified_capture_report=data.get("install_verified_capture_report")
        or har_workflow_next_action.get("install_verified_capture_report"),
        install_verified_capture_apply_command=data.get("install_verified_capture_apply_command")
        or har_workflow_next_action.get("install_verified_capture_apply_command"),
        install_verified_capture_direct_apply_command=data.get("install_verified_capture_direct_apply_command")
        or har_workflow_next_action.get("install_verified_capture_direct_apply_command"),
        tab_scan_candidate_count=citadel_tab_scan.get("candidate_count"),
        tab_scan_limit=citadel_tab_scan.get("scan_limit"),
        tab_scan_scanned_count=citadel_tab_scan.get("scanned_count"),
        tab_scan_skipped_count=citadel_tab_scan.get("scan_skipped_count"),
        tab_scan_fetch_timeout_ms=citadel_tab_scan.get("scan_fetch_timeout_ms"),
        tab_scan_target_id_requested=citadel_tab_scan.get("scan_target_id_requested"),
        tab_scan_target_id_found=citadel_tab_scan.get("scan_target_id_found"),
        tab_scan_authenticated_found=citadel_tab_scan.get("authenticated_found"),
        tab_scan_direct_fallback_target_selected=citadel_tab_scan.get("direct_fallback_target_selected"),
        tab_scan_direct_fallback_target_id_requested=citadel_tab_scan.get("direct_fallback_target_id_requested"),
        tab_scan_direct_fallback_target_id_found=citadel_tab_scan.get("direct_fallback_target_id_found"),
        oauth_network_event_count=len(oauth_events),
        oauth_network_statuses=oauth_statuses,
        oauth_network_error_codes=oauth_error_codes,
        oauth_network_request_shape_matched_har_statuses=sorted(oauth_request_shape_statuses),
        oauth_network_request_structure_matched_har_statuses=sorted(oauth_request_structure_statuses),
        login_form_submitted=data.get("login_form_submitted"),
    )
    if not string_list(summary.get("target_month_document_identifiers")):
        prop_key = normalize_property(entry.get("property"))
        target_month = str(summary.get("target_month") or "").strip()
        for gap in data.get("target_month_statement_gaps") or []:
            if not isinstance(gap, dict):
                continue
            if normalize_property(gap.get("property")) != prop_key:
                continue
            gap_month = str(gap.get("target_month") or gap.get("expected_target_month") or "").strip()
            if target_month and gap_month and gap_month != target_month:
                continue
            ids = string_list(gap.get("target_month_document_identifiers"))
            if not ids:
                continue
            summary["target_month_document_identifiers"] = ids
            for key in (
                "expected_document_ids",
                "target_month_statement_candidates",
                "target_month_candidate_count",
                "target_month_downloadable_count",
                "target_month_recapture_required",
                "target_month_recapture_reason",
                "required_capture_quality",
                "suggested_next_action",
                "operator_next_action",
                "manual_auth_required",
                "auth_state",
                "auth_stage",
                "auth_issue",
                "auth_issue_text",
                "body_recapture_capture_method",
                "body_recapture_capture_instruction",
                "body_recapture_manual_har_export_warning",
            ):
                if summary.get(key) in (None, [], "") and gap.get(key) not in (None, [], ""):
                    summary[key] = gap.get(key)
            break
    overlay_latest_citadel_live_auth_if_idempotent(entry, report_path, data, summary)
    return summary


def read_prepare_report_summary(prepare: dict[str, Any]) -> dict[str, Any] | None:
    report_value = str(prepare.get("report") or "")
    if not report_value:
        return None
    report_path = resolve_path(report_value)
    summary: dict[str, Any] = {"path": str(report_path)}
    if not report_path.exists():
        summary["status"] = "missing"
        return summary
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        summary.update(status="unreadable", error=str(exc))
        return summary
    errors = data.get("errors")
    next_action = data.get("next_action") or {}
    if not isinstance(next_action, dict):
        next_action = {}
    summary.update(
        status=data.get("status"),
        reason=data.get("reason"),
        successful_password_request_found=data.get("successful_password_request_found"),
        bw_session_status=data.get("bw_session_status"),
        bw_item_found=data.get("bw_item_found"),
        bw_item_name=data.get("bw_item_name"),
        bw_item_uri_host_match=data.get("bw_item_uri_host_match"),
        username_matches_har=data.get("username_matches_har"),
        password_matched_before_update=data.get("password_matched_before_update"),
        password_updated=data.get("password_updated"),
        bw_sync_attempted=data.get("bw_sync_attempted"),
        har_token_available=data.get("token_available"),
        har_token_entry_count=data.get("token_entry_count"),
        har_token_mobile_source_id_available=data.get("mobile_source_id_available"),
        har_token_source_endpoint_paths=data.get("source_endpoint_paths"),
        har_token_source_document_detail_id_count=data.get("source_document_detail_id_count"),
        har_token_endpoint_statuses=data.get("endpoint_statuses"),
        next_action_status=next_action.get("status"),
        next_action_reason=next_action.get("reason"),
        next_action_command=next_action.get("next_command") or data.get("next_command"),
        next_action_capture_command=next_action.get("capture_command"),
        next_action_capture_required=next_action.get("capture_required"),
        next_action_target_month=next_action.get("target_month"),
        next_action_target_month_replayable_document_available=next_action.get("target_month_replayable_document_available"),
        next_action_target_month_replayable_document_payload_count=next_action.get("target_month_replayable_document_payload_count"),
        next_action_replayable_statement_months=next_action.get("replayable_statement_months"),
        next_action_statement_document_months=next_action.get("statement_document_months"),
        next_action_required_response_paths=next_action.get("required_response_paths"),
        next_action_required_response_path_counts=next_action.get("required_response_path_counts"),
        next_action_required_response_path_progress=next_action.get("required_response_path_progress"),
        next_action_response_body_requirements=next_action.get("response_body_requirements"),
        manual_auth_required=data.get("manual_auth_required"),
        manual_auth_file=data.get("manual_auth_file"),
        manual_auth_portal_url=data.get("manual_auth_portal_url"),
        manual_auth_target_id=data.get("manual_auth_target_id"),
        manual_auth_next_command=data.get("manual_auth_next_command"),
        manual_auth_install_verified_har_dry_run_command=data.get(
            "manual_auth_install_verified_har_dry_run_command"
        ),
        manual_auth_install_verified_har_apply_command=data.get(
            "manual_auth_install_verified_har_apply_command"
        ),
        authenticated_found=data.get("authenticated_found"),
        candidate_count=data.get("candidate_count"),
        route_counts=data.get("route_counts"),
        login_tab_count=data.get("login_tab_count"),
        non_login_tab_count=data.get("non_login_tab_count"),
        scanned_count=data.get("scanned_count"),
        captured_endpoint_count=data.get("captured_endpoint_count", data.get("fetched_endpoint_count")),
        captured_response_body_count=data.get("captured_response_body_count"),
        source_har_path_exists=data.get("source_har_path_exists"),
        source_direct_pdf_limit=data.get("source_direct_pdf_limit"),
        source_direct_pdf_candidate_count=data.get("source_direct_pdf_candidate_count"),
        source_direct_pdf_path_counts=data.get("source_direct_pdf_path_counts"),
        source_direct_pdf_fetched_count=data.get("source_direct_pdf_fetched_count"),
        source_direct_pdf_replayable_count=data.get("source_direct_pdf_replayable_count"),
        source_required_response_candidate_count=data.get("source_required_response_candidate_count"),
        source_required_response_path_counts=data.get("source_required_response_path_counts"),
        source_required_response_fetched_count=data.get("source_required_response_fetched_count"),
        source_required_response_replayable_count=data.get("source_required_response_replayable_count"),
        required_response_paths=data.get("required_response_paths"),
        required_response_path_counts=data.get("required_response_path_counts"),
        required_response_path_progress=data.get("required_response_path_progress"),
        response_body_requirements=data.get("response_body_requirements"),
        response_body_requirement_role_counts=data.get("response_body_requirement_role_counts"),
        captured_response_body_requirement_counts=data.get("captured_response_body_requirement_counts"),
        response_body_requirement_role_capture_counts=data.get("response_body_requirement_role_capture_counts"),
        missing_response_body_requirements=data.get("missing_response_body_requirements"),
        missing_response_body_requirement_count=data.get("missing_response_body_requirement_count"),
        captured_required_response_paths=data.get("captured_required_response_paths"),
        captured_required_response_path_counts=data.get("captured_required_response_path_counts"),
        missing_required_response_paths=data.get("missing_required_response_paths"),
        missing_required_response_path_counts=data.get("missing_required_response_path_counts"),
        statement_candidate_count=data.get("statement_candidate_count"),
        replayable_document_payload_count=data.get("replayable_document_payload_count"),
        target_month=data.get("target_month"),
        target_month_replayable_document_available=data.get("target_month_replayable_document_available"),
        target_month_replayable_document_payload_count=data.get("target_month_replayable_document_payload_count"),
        replayable_statement_months=data.get("replayable_statement_months"),
        statement_document_months=data.get("statement_document_months"),
        direct_pdf_response_count=data.get("direct_pdf_response_count"),
        capture_har_path=data.get("capture_har_path"),
        error_count=len(errors) if isinstance(errors, list) else None,
    )
    return summary


def apply_report_summary_to_result(result: dict[str, Any], summary: dict[str, Any] | None) -> None:
    if not isinstance(summary, dict):
        return
    report_reason = summary.get("reason")
    result.update(
        report_status=summary.get("status"),
        report_reason=report_reason,
        har_path=summary.get("har_path"),
        har_path_exists=summary.get("har_path_exists"),
        candidate_count=summary.get("candidate_count"),
        candidate_source_counts=summary.get("candidate_source_counts"),
        available_statement_months=summary.get("available_statement_months"),
        downloadable_statement_months=summary.get("downloadable_statement_months"),
        metadata_only_statement_months=summary.get("metadata_only_statement_months"),
        latest_statement_month=summary.get("latest_statement_month"),
        latest_downloadable_statement_month=summary.get("latest_downloadable_statement_month"),
        target_month_candidate_count=summary.get("target_month_candidate_count"),
        target_month_statement_candidates=summary.get("target_month_statement_candidates"),
        target_month_candidate_source_counts=summary.get("target_month_candidate_source_counts"),
        target_month_downloadable_count=summary.get("target_month_downloadable_count"),
        target_month_downloadable_source_counts=summary.get("target_month_downloadable_source_counts"),
        target_month_recapture_required=summary.get("target_month_recapture_required"),
        target_month_recapture_reason=summary.get("target_month_recapture_reason"),
        credentials_available=summary.get("credentials_available"),
        credential_source=summary.get("credential_source"),
        credential_lookup_status=summary.get("credential_lookup_status"),
        credential_lookup_failure_reason=summary.get("credential_lookup_failure_reason"),
        credential_lookup_exit_code=summary.get("credential_lookup_exit_code"),
        credential_lookup_item_name=summary.get("credential_lookup_item_name"),
        credential_lookup_uri_host=summary.get("credential_lookup_uri_host"),
        credential_lookup_uri_host_aliases=summary.get("credential_lookup_uri_host_aliases"),
        credential_lookup_search_terms=summary.get("credential_lookup_search_terms"),
        credential_lookup_candidate_search_term_count=summary.get("credential_lookup_candidate_search_term_count"),
        credential_lookup_candidate_search_terms=summary.get("credential_lookup_candidate_search_terms"),
        credential_lookup_expected_folder_name=summary.get("credential_lookup_expected_folder_name"),
        credential_lookup_expected_folder_id_configured=summary.get("credential_lookup_expected_folder_id_configured"),
        credential_lookup_candidate_count=summary.get("credential_lookup_candidate_count"),
        credential_lookup_candidate_items=summary.get("credential_lookup_candidate_items"),
        credential_lookup_unguarded_candidate_count=summary.get("credential_lookup_unguarded_candidate_count"),
        credential_lookup_unguarded_candidate_items=summary.get("credential_lookup_unguarded_candidate_items"),
        credential_lookup_misfiled_candidate_count=summary.get("credential_lookup_misfiled_candidate_count"),
        credential_lookup_absent=summary.get("credential_lookup_absent"),
        credential_lookup_scope=summary.get("credential_lookup_scope"),
        credential_lookup_repair_action=summary.get("credential_lookup_repair_action"),
        credential_repair_instruction=summary.get("credential_repair_instruction"),
        target_month=summary.get("target_month"),
        target_month_statement_available=summary.get("target_month_statement_available"),
        target_month_existing_count=summary.get("target_month_existing_count"),
        target_month_downloaded_count=summary.get("target_month_downloaded_count"),
        target_month_skipped_count=summary.get("target_month_skipped_count"),
        downloaded_count=summary.get("downloaded_count"),
        skipped_count=summary.get("skipped_count"),
        error_count=summary.get("error_count"),
        warning_count=summary.get("warning_count"),
        manual_auth_required=summary.get("manual_auth_required"),
        manual_auth_reason=summary.get("manual_auth_reason"),
        manual_auth_portal_url=summary.get("manual_auth_portal_url"),
        auth_state=summary.get("auth_state"),
        auth_stage=summary.get("auth_stage"),
        auth_failure_reason=summary.get("auth_failure_reason"),
        auth_failure_visible_reason=summary.get("auth_failure_visible_reason"),
        auth_mfa_reached=summary.get("auth_mfa_reached"),
        auth_issue=summary.get("auth_issue"),
        auth_issue_text=summary.get("auth_issue_text"),
        login_mode=summary.get("login_mode"),
        auto_login_attempted=summary.get("auto_login_attempted"),
        auto_login_status=summary.get("auto_login_status"),
        auto_login_blocked_reason=summary.get("auto_login_blocked_reason"),
        auto_login_force_enabled=summary.get("auto_login_force_enabled"),
        auto_login_username_available=summary.get("auto_login_username_available"),
        auto_login_password_available=summary.get("auto_login_password_available"),
        auto_login_username_typed=summary.get("auto_login_username_typed"),
        auto_login_password_typed=summary.get("auto_login_password_typed"),
        auto_login_step=summary.get("auto_login_step"),
        auto_otp_attempted=summary.get("auto_otp_attempted"),
        auto_otp_status=summary.get("auto_otp_status"),
        auto_otp_code_available=summary.get("auto_otp_code_available"),
        auto_otp_source=summary.get("auto_otp_source"),
        auto_otp_fetch_enabled=summary.get("auto_otp_fetch_enabled"),
        auto_otp_fetch_attempted=summary.get("auto_otp_fetch_attempted"),
        auto_otp_fetch_attempt_count=summary.get("auto_otp_fetch_attempt_count"),
        auto_otp_fetch_status=summary.get("auto_otp_fetch_status"),
        auto_otp_fetch_exit_code=summary.get("auto_otp_fetch_exit_code"),
        auto_otp_fetch_report=summary.get("auto_otp_fetch_report"),
        credential_login_failure_suspected=summary.get("credential_login_failure_suspected"),
        credential_login_failure_suspected_reason=summary.get("credential_login_failure_suspected_reason"),
        operator_next_action=summary.get("operator_next_action"),
        required_capture_quality=summary.get("required_capture_quality"),
        suggested_next_action=summary.get("suggested_next_action"),
        target_month_document_identifiers=summary.get("target_month_document_identifiers"),
        expected_document_ids=summary.get("expected_document_ids"),
        body_recapture_capture_method=summary.get("body_recapture_capture_method"),
        body_recapture_capture_instruction=summary.get("body_recapture_capture_instruction"),
        body_recapture_manual_har_export_warning=summary.get("body_recapture_manual_har_export_warning"),
        safe_to_run_automatically=summary.get("safe_to_run_automatically") is True,
        idempotent_replay_safe=summary.get("idempotent_replay_safe"),
        copy_plan_safe_to_apply_automatically=summary.get("copy_plan_safe_to_apply_automatically"),
        report_idempotency_digest=summary.get("idempotency_digest"),
        idempotent_skip=summary.get("idempotent_skip"),
        idempotent_skip_reason=summary.get("idempotent_skip_reason"),
    )
    if result.get("reason") in (None, "", []):
        result["reason"] = report_reason


def apply_expected_target_month_validation(result: dict[str, Any], expected_target_month: str | None) -> None:
    summary = result.get("report_summary")
    expected = str(expected_target_month or "").strip()
    if not expected or not isinstance(summary, dict):
        return
    actual = str(summary.get("target_month") or "").strip()
    if not actual:
        return
    matches = actual == expected
    result["report_target_month"] = actual
    result["report_target_month_matches_expected"] = matches
    if matches:
        return
    result["stale_report_for_expected_target_month"] = True
    result["stale_report_reason"] = "report_target_month_mismatch"
    result["target_month_statement_available"] = False
    summary["target_month_statement_available_for_expected_target_month"] = False
    summary["expected_target_month"] = expected
    summary["report_target_month_matches_expected"] = False
    summary["stale_for_expected_target_month"] = True
    summary["stale_report_reason"] = "report_target_month_mismatch"


def apply_effective_status(result: dict[str, Any]) -> str:
    runtime_status = str(result.get("status") or "unknown")
    report_status = str(result.get("report_status") or "").strip()
    effective_status = runtime_status
    if report_status in REPORT_STATUSES_THAT_EXPLAIN_NONZERO_RC and runtime_status in {"failed", "ok", "unknown"}:
        effective_status = report_status
    result["runtime_status"] = runtime_status
    result["effective_status"] = effective_status
    return effective_status


def active_profile(entry: dict[str, Any], profile_name: str) -> dict[str, Any]:
    profiles = entry.get("profiles") or {}
    if not profile_name or not isinstance(profiles, dict):
        return {}
    profile = profiles.get(profile_name) or {}
    return profile if isinstance(profile, dict) else {}


def effective_runtime_and_script(entry: dict[str, Any], profile: dict[str, Any] | None = None) -> tuple[str, str]:
    active = profile or {}
    return (
        str(active.get("runtime") or entry.get("runtime") or ""),
        str(active.get("script") or entry.get("script") or ""),
    )


def har_mode_blocked(env: dict[str, str]) -> bool:
    return (
        str(env.get("CITADEL_LOGIN_MODE") or "").strip().lower() == "har"
        and os.environ.get("MORTGAGE_DOWNLOADER_ALLOW_HAR_MODE") != "1"
    )


def run_entry(
    entry: dict[str, Any],
    dry_run: bool,
    profile_name: str,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    profile = active_profile(entry, profile_name)
    fallback_on_prepare_fail = os.environ.get("MORTGAGE_DOWNLOADER_FALLBACK_ON_PREPARE_FAIL") == "1"
    active_report = str(profile.get("report") or entry.get("report") or "")
    runtime, script_value = effective_runtime_and_script(entry, profile)
    result: dict[str, Any] = {
        "id": entry.get("id"),
        "property": entry.get("property"),
        "servicer": entry.get("servicer"),
        "co_owner_paid_mortgage": bool(entry.get("co_owner_paid_mortgage")),
        "enabled": bool(entry.get("enabled", True)),
        "runtime": runtime,
        "script": script_value,
        "report": active_report,
        "profile": profile_name if profile else None,
        "status": "unknown",
        "rc": 0,
    }
    if not result["enabled"]:
        result["status"] = "disabled"
        return result
    if not result["co_owner_paid_mortgage"]:
        result.update(
            status="skipped_not_co_owner_paid_mortgage",
            skipped_reason="co_owner_paid_mortgage=false",
        )
        return result

    command, command_error = runtime_command(runtime, script_value)
    if command_error:
        result.update(status="invalid" if "unsupported runtime" in command_error else "missing", rc=2, error=command_error)
        return result
    env = preserve_explicit_env_overrides(
        build_env(entry.get("env"), profile.get("env") if profile else None, env_overrides)
    )
    if profile and "prepare" in profile:
        prepare = profile.get("prepare") if isinstance(profile.get("prepare"), dict) else None
    else:
        prepare = entry.get("prepare")
    if har_mode_blocked(env):
        result.update(
            status="invalid",
            rc=2,
            error="CITADEL_LOGIN_MODE=har is diagnostic-only and blocked in the mortgage downloader registry",
        )
        return result
    if dry_run:
        result.update(status="dry_run", command=command)
        if isinstance(prepare, dict):
            prepare_command, prepare_error = runtime_command(
                str(prepare.get("runtime") or ""),
                str(prepare.get("script") or ""),
            )
            result["prepare"] = {
                "status": "dry_run" if prepare_command else "invalid",
                "command": prepare_command,
                "error": prepare_error,
                "report": str(resolve_path(str(prepare.get("report") or ""))) if prepare.get("report") else None,
            }
        return result

    if isinstance(prepare, dict):
        prepare_command, prepare_error = runtime_command(
            str(prepare.get("runtime") or ""),
            str(prepare.get("script") or ""),
        )
        if prepare_error or not prepare_command:
            result.update(status="invalid", rc=2, error=f"prepare {prepare_error}")
            return result
        prepare_env = preserve_explicit_env_overrides(
            build_env(entry.get("env"), profile.get("env") if profile else None, prepare.get("env"), env_overrides)
        )
        prepare_preflight = bitwarden_write_preflight(prepare_env)
        if prepare_preflight.get("status") == "blocked":
            result["prepare"] = prepare_preflight
            result.update(
                status="invalid",
                rc=2,
                error="prepare Bitwarden write requested without CITADEL_BW_EXPECTED_* guard",
            )
            return result
        prepare_result = run_command(prepare_command, prepare_env)
        result["prepare"] = prepare_result
        prepare_report_summary = read_prepare_report_summary(prepare)
        if prepare_report_summary is not None:
            result["prepare_report_summary"] = prepare_report_summary
        if int(prepare_result.get("rc") or 0) != 0:
            result["prepare_failed_but_fallback_attempted"] = fallback_on_prepare_fail
            if not fallback_on_prepare_fail:
                prepare_status = str(prepare_result.get("status") or "failed")
                result.update(status=prepare_status, runtime_status=prepare_status, rc=prepare_result["rc"])
                return result
            env = preserve_explicit_env_overrides(build_env(entry.get("env"), env_overrides))

    result.update(run_command(command or [], env))
    report_summary = read_report_summary({**entry, "report": active_report})
    if report_summary is not None:
        result["report_summary"] = report_summary
        apply_report_summary_to_result(result, report_summary)
    apply_effective_status(result)
    return result


def summarize_existing_entry(entry: dict[str, Any], profile_name: str) -> dict[str, Any]:
    profile = active_profile(entry, profile_name)
    active_report = str(profile.get("report") or entry.get("report") or "")
    runtime, script_value = effective_runtime_and_script(entry, profile)
    result: dict[str, Any] = {
        "id": entry.get("id"),
        "property": entry.get("property"),
        "servicer": entry.get("servicer"),
        "co_owner_paid_mortgage": bool(entry.get("co_owner_paid_mortgage")),
        "enabled": bool(entry.get("enabled", True)),
        "runtime": runtime,
        "script": script_value,
        "report": active_report,
        "profile": profile_name if profile else None,
        "status": "unknown",
        "rc": 0,
        "summarized_existing_report": True,
    }
    if not result["enabled"]:
        result["status"] = "disabled"
        return result
    if not result["co_owner_paid_mortgage"]:
        result.update(
            status="skipped_not_co_owner_paid_mortgage",
            skipped_reason="co_owner_paid_mortgage=false",
        )
        return result

    _, command_error = runtime_command(runtime, script_value)
    if command_error:
        result.update(status="invalid" if "unsupported runtime" in command_error else "missing", rc=2, error=command_error)
        return result

    if profile and "prepare" in profile:
        prepare = profile.get("prepare") if isinstance(profile.get("prepare"), dict) else None
    else:
        prepare = entry.get("prepare")
    if isinstance(prepare, dict):
        prepare_summary = read_prepare_report_summary(prepare)
        if prepare_summary is not None:
            result["prepare_report_summary"] = prepare_summary

    report_summary = read_report_summary({**entry, "report": active_report})
    if report_summary is not None:
        result["report_summary"] = report_summary
        apply_report_summary_to_result(result, report_summary)
        summary_status = str(report_summary.get("status") or "")
        if summary_status == "ok":
            result.update(status="ok", rc=0)
        elif summary_status == "review":
            result.update(status="review", rc=0)
        elif summary_status in {"missing", "unreadable"}:
            result.update(status="failed", rc=2)
        else:
            result.update(status="failed", rc=1)
        apply_effective_status(result)
    else:
        result.update(status="failed", rc=2, error="report not configured")
        apply_effective_status(result)
    return result


def target_month_statement_gap_reason(result: dict[str, Any], expected_target_month: str | None = None) -> str | None:
    summary = result.get("report_summary")
    if not isinstance(summary, dict):
        return f"downloader_{result.get('status') or 'unknown'}"
    summary_status = summary.get("status")
    if summary_status in {"missing", "unreadable"}:
        return f"report_summary_{summary_status}"
    actual_target_month = str(summary.get("target_month") or "").strip()
    expected = str(expected_target_month or result.get("expected_target_month") or "").strip()
    if expected and actual_target_month and actual_target_month != expected:
        return "report_target_month_mismatch"
    available = summary.get("target_month_statement_available")
    if available is True:
        return None
    if available is False:
        manual_auth_reason = str(summary.get("manual_auth_reason") or "").strip()
        if manual_auth_reason in {
            "visible_loandepot_tab_not_authenticated",
            "loandepot_credentials_unavailable",
            "loandepot_api_auth_failed",
            "login_rejected",
            "login_still_required_after_submit",
            "account_locked",
            "previous_account_locked",
        }:
            return manual_auth_reason
        concrete_reason = str(summary.get("reason") or "").strip()
        if concrete_reason in {
            "target_month_statement_pdf_payload_missing",
            "visible_loandepot_tab_not_authenticated",
            "loandepot_credentials_unavailable",
            "loandepot_api_auth_failed",
        }:
            return concrete_reason
        auth_issue = str(summary.get("auth_issue") or "").strip()
        if auth_issue in {"not_authenticated", "api_auth_failed", "credentials_unavailable"}:
            return manual_auth_reason or auth_issue
        return "target_month_statement_unavailable"
    if summary_status and summary_status != "ok":
        return f"report_summary_{summary_status}"
    return "target_month_statement_unknown"


def target_month_statement_available_for_result(
    result: dict[str, Any],
    expected_target_month: str | None = None,
) -> bool:
    summary = result.get("report_summary")
    if not isinstance(summary, dict):
        return False
    expected = str(expected_target_month or result.get("expected_target_month") or "").strip()
    actual_target_month = str(summary.get("target_month") or "").strip()
    target_month_matches = not expected or not actual_target_month or actual_target_month == expected
    return summary.get("target_month_statement_available") is True and target_month_matches


def target_month_gap_next_action(
    *,
    reason: str | None,
    required_capture_quality: object,
    summary: dict[str, Any],
    handoff: dict[str, Any],
    expected_target_month: str | None,
) -> object:
    summary_action = summary.get("suggested_next_action")
    if summary.get("manual_auth_required") is True and summary.get("operator_next_action"):
        return summary.get("operator_next_action")
    required_quality = str(required_capture_quality or "").strip()
    target_candidate_count = summary.get("target_month_candidate_count")
    no_target_candidate = target_candidate_count in (0, "0")
    if (
        str(reason or "").strip() == "target_month_statement_unavailable"
        and required_quality == "full_response_bodies"
        and no_target_candidate
    ):
        return "capture_workflow_har_with_full_response_bodies"
    handoff_target = str(handoff.get("target_statement_month") or "").strip()
    expected = str(expected_target_month or "").strip()
    handoff_action = handoff.get("suggested_next_action")
    if handoff_action and (not expected or not handoff_target or handoff_target == expected):
        return handoff_action
    return summary_action


def append_target_month_statement_status(
    report: dict[str, Any],
    result: dict[str, Any],
    dry_run: bool,
    expected_target_month: str | None = None,
) -> None:
    if dry_run or not result.get("enabled") or not result.get("co_owner_paid_mortgage") or not result.get("report"):
        return
    summary = result.get("report_summary")
    if target_month_statement_available_for_result(result, expected_target_month):
        report["target_month_statement_available_count"] += 1
        return

    reason = target_month_statement_gap_reason(result, expected_target_month)
    if not reason:
        return
    prop = result.get("property")
    handoff = workflow_handoff_summary(prop)
    report["target_month_statement_gap_count"] += 1
    if prop and prop not in report["target_month_statement_gap_properties"]:
        report["target_month_statement_gap_properties"].append(prop)
    gap: dict[str, Any] = {
        "id": result.get("id"),
        "property": prop,
        "servicer": result.get("servicer"),
        "status": result.get("effective_status") or result.get("status"),
        "effective_status": result.get("effective_status") or result.get("status"),
        "runtime_status": result.get("runtime_status") or result.get("status"),
        "rc": result.get("rc"),
        "reason": reason,
    }
    if isinstance(summary, dict):
        required_capture_quality = handoff.get("required_capture_quality") or summary.get("required_capture_quality")
        summary_suggested_next_action = target_month_gap_next_action(
            reason=reason,
            required_capture_quality=required_capture_quality,
            summary=summary,
            handoff=handoff,
            expected_target_month=expected_target_month,
        )
        gap.update(
            report_status=summary.get("status"),
            target_month=summary_target_month(summary, handoff, expected_target_month),
            expected_target_month=expected_target_month or None,
            target_month_existing_count=summary.get("target_month_existing_count"),
            target_month_downloaded_count=summary.get("target_month_downloaded_count"),
            target_month_skipped_count=summary.get("target_month_skipped_count"),
            target_month_candidate_count=summary.get("target_month_candidate_count"),
            target_month_statement_candidates=summary.get("target_month_statement_candidates"),
            credentials_available=summary.get("credentials_available"),
            credential_source=summary.get("credential_source"),
            credential_lookup_status=summary.get("credential_lookup_status"),
            credential_lookup_failure_reason=summary.get("credential_lookup_failure_reason"),
            credential_lookup_exit_code=summary.get("credential_lookup_exit_code"),
            credential_lookup_item_name=summary.get("credential_lookup_item_name"),
            credential_lookup_uri_host=summary.get("credential_lookup_uri_host"),
            credential_lookup_search_term_count=summary.get("credential_lookup_search_term_count"),
            credential_lookup_search_terms=summary.get("credential_lookup_search_terms"),
            credential_lookup_candidate_search_term_count=summary.get(
                "credential_lookup_candidate_search_term_count"
            ),
            credential_lookup_candidate_search_terms=summary.get("credential_lookup_candidate_search_terms"),
            credential_lookup_expected_folder_name=summary.get("credential_lookup_expected_folder_name"),
            credential_lookup_expected_folder_id_configured=summary.get("credential_lookup_expected_folder_id_configured"),
            credential_lookup_candidate_count=summary.get("credential_lookup_candidate_count"),
            credential_lookup_candidate_items=summary.get("credential_lookup_candidate_items"),
            credential_lookup_unguarded_candidate_count=summary.get("credential_lookup_unguarded_candidate_count"),
            credential_lookup_unguarded_candidate_items=summary.get("credential_lookup_unguarded_candidate_items"),
            credential_lookup_misfiled_candidate_count=summary.get("credential_lookup_misfiled_candidate_count"),
            credential_lookup_absent=summary.get("credential_lookup_absent"),
            credential_lookup_scope=summary.get("credential_lookup_scope"),
            credential_lookup_repair_action=summary.get("credential_lookup_repair_action"),
            credential_login_hint_mismatch=summary.get("credential_login_hint_mismatch"),
            credential_login_hint_mismatch_overridden=summary.get("credential_login_hint_mismatch_overridden"),
            manual_auth_required=summary.get("manual_auth_required"),
            manual_auth_reason=summary.get("manual_auth_reason"),
            manual_auth_portal_url=summary.get("manual_auth_portal_url"),
            auth_state=summary.get("auth_state"),
            auth_stage=summary.get("auth_stage"),
            auth_failure_reason=summary.get("auth_failure_reason"),
            auth_failure_visible_reason=summary.get("auth_failure_visible_reason"),
            auth_mfa_reached=summary.get("auth_mfa_reached"),
            auth_issue=summary.get("auth_issue"),
            auth_issue_text=summary.get("auth_issue_text"),
            login_mode=summary.get("login_mode"),
            auto_login_attempted=summary.get("auto_login_attempted"),
            auto_login_status=summary.get("auto_login_status"),
            auto_login_blocked_reason=summary.get("auto_login_blocked_reason"),
            auto_login_force_enabled=summary.get("auto_login_force_enabled"),
            auto_login_username_available=summary.get("auto_login_username_available"),
            auto_login_password_available=summary.get("auto_login_password_available"),
            auto_login_username_typed=summary.get("auto_login_username_typed"),
            auto_login_password_typed=summary.get("auto_login_password_typed"),
            auto_login_step=summary.get("auto_login_step"),
            auto_otp_attempted=summary.get("auto_otp_attempted"),
            auto_otp_status=summary.get("auto_otp_status"),
            auto_otp_code_available=summary.get("auto_otp_code_available"),
            auto_otp_source=summary.get("auto_otp_source"),
            auto_otp_fetch_enabled=summary.get("auto_otp_fetch_enabled"),
            auto_otp_fetch_attempted=summary.get("auto_otp_fetch_attempted"),
            auto_otp_fetch_attempt_count=summary.get("auto_otp_fetch_attempt_count"),
            auto_otp_fetch_status=summary.get("auto_otp_fetch_status"),
            auto_otp_fetch_exit_code=summary.get("auto_otp_fetch_exit_code"),
            auto_otp_fetch_report=summary.get("auto_otp_fetch_report"),
            credential_login_failure_suspected=summary.get("credential_login_failure_suspected"),
            credential_login_failure_suspected_reason=summary.get(
                "credential_login_failure_suspected_reason"
            ),
            operator_next_action=summary.get("operator_next_action"),
            required_capture_quality=required_capture_quality,
            suggested_next_action=summary_suggested_next_action,
            target_month_document_identifiers=summary_target_document_identifiers(
                summary,
                handoff,
                expected_target_month,
            ),
            expected_document_ids=summary.get("expected_document_ids"),
            body_recapture_capture_method=summary.get("body_recapture_capture_method"),
            body_recapture_capture_instruction=summary.get("body_recapture_capture_instruction"),
            body_recapture_manual_har_export_warning=summary.get("body_recapture_manual_har_export_warning"),
            target_month_recapture_required=summary.get("target_month_recapture_required"),
            target_month_recapture_reason=summary.get("target_month_recapture_reason"),
        )
    report["target_month_statement_gaps"].append(gap)


def append_failed_downloader_statement_coverage(
    report: dict[str, Any],
    result: dict[str, Any],
    expected_target_month: str | None = None,
) -> None:
    if not result.get("enabled") or not result.get("co_owner_paid_mortgage"):
        return
    if int(result.get("rc") or 0) == 0:
        return
    if not target_month_statement_available_for_result(result, expected_target_month):
        return
    summary = result.get("report_summary")
    if not isinstance(summary, dict):
        summary = {}
    prop = result.get("property")
    report["failed_downloader_target_month_statement_available_count"] += 1
    if prop and prop not in report["failed_downloader_target_month_statement_available_properties"]:
        report["failed_downloader_target_month_statement_available_properties"].append(prop)
    report["failed_downloader_target_month_statement_available_details"].append(
        {
            "id": result.get("id"),
            "property": prop,
            "servicer": result.get("servicer"),
            "status": result.get("status"),
            "rc": result.get("rc"),
            "report_status": summary.get("status"),
            "target_month": summary.get("target_month"),
            "expected_target_month": expected_target_month or result.get("expected_target_month"),
            "target_month_existing_count": summary.get("target_month_existing_count"),
            "target_month_downloaded_count": summary.get("target_month_downloaded_count"),
            "target_month_skipped_count": summary.get("target_month_skipped_count"),
            "auth_failure_reason": summary.get("auth_failure_reason"),
            "manual_auth_required": summary.get("manual_auth_required"),
        }
    )


def automation_attention_for_result(
    result: dict[str, Any],
    expected_target_month: str | None = None,
) -> dict[str, Any] | None:
    if not result.get("enabled") or not result.get("co_owner_paid_mortgage"):
        return None
    summary = result.get("report_summary")
    if not isinstance(summary, dict):
        summary = {}

    reasons: list[str] = []
    if summary.get("idempotent_skip_latest_live_auth_attention") is True:
        reasons.append("latest_live_auth_failed_after_idempotent_skip")
    if int(result.get("rc") or 0) != 0 and target_month_statement_available_for_result(
        result,
        expected_target_month,
    ):
        reasons.append("downloader_failed_but_target_month_statement_available")
    replay_blocker = str(summary.get("har_workflow_replay_blocker") or "").strip()
    if replay_blocker:
        reasons.append("har_replay_blocked")
    capture_quality_status = str(summary.get("har_workflow_capture_quality_status") or "").strip()
    if capture_quality_status == "needs_full_response_bodies":
        reasons.append("har_capture_needs_full_response_bodies")

    if not reasons:
        return None
    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "id": result.get("id"),
        "property": result.get("property"),
        "servicer": result.get("servicer"),
        "status": result.get("status"),
        "rc": result.get("rc"),
        "reasons": unique_reasons,
        "report": result.get("report"),
        "report_status": summary.get("status"),
        "target_month": summary.get("target_month"),
        "expected_target_month": expected_target_month or result.get("expected_target_month"),
        "target_month_statement_available": summary.get("target_month_statement_available"),
        "target_month_existing_count": summary.get("target_month_existing_count"),
        "latest_live_auth_report": summary.get("latest_live_auth_report"),
        "latest_live_auth_status": summary.get("latest_live_auth_status"),
        "latest_live_auth_auth_failure_reason": summary.get("latest_live_auth_auth_failure_reason"),
        "latest_live_auth_auth_failure_visible_reason": summary.get(
            "latest_live_auth_auth_failure_visible_reason"
        ),
        "latest_live_auth_manual_auth_required": summary.get("latest_live_auth_manual_auth_required"),
        "auth_failure_reason": summary.get("auth_failure_reason"),
        "auth_failure_visible_reason": summary.get("auth_failure_visible_reason"),
        "auth_state": summary.get("auth_state"),
        "auth_stage": summary.get("auth_stage"),
        "auth_issue": summary.get("auth_issue"),
        "auth_issue_text": summary.get("auth_issue_text"),
        "auto_login_status": summary.get("auto_login_status"),
        "auto_login_blocked_reason": summary.get("auto_login_blocked_reason"),
        "auto_login_force_enabled": summary.get("auto_login_force_enabled"),
        "auto_otp_attempted": summary.get("auto_otp_attempted"),
        "auto_otp_status": summary.get("auto_otp_status"),
        "auto_otp_code_available": summary.get("auto_otp_code_available"),
        "auto_otp_source": summary.get("auto_otp_source"),
        "auto_otp_fetch_enabled": summary.get("auto_otp_fetch_enabled"),
        "auto_otp_fetch_attempted": summary.get("auto_otp_fetch_attempted"),
        "auto_otp_fetch_attempt_count": summary.get("auto_otp_fetch_attempt_count"),
        "auto_otp_fetch_status": summary.get("auto_otp_fetch_status"),
        "auto_otp_fetch_exit_code": summary.get("auto_otp_fetch_exit_code"),
        "auto_otp_fetch_report": summary.get("auto_otp_fetch_report"),
        "operator_next_action": summary.get("operator_next_action"),
        "manual_auth_required": summary.get("manual_auth_required"),
        "manual_auth_reason": summary.get("manual_auth_reason"),
        "har_workflow_replay_blocker": replay_blocker or None,
        "har_workflow_capture_quality_status": capture_quality_status or None,
    }


def append_automation_attention(
    report: dict[str, Any],
    result: dict[str, Any],
    expected_target_month: str | None = None,
) -> dict[str, Any] | None:
    attention = automation_attention_for_result(result, expected_target_month)
    if not attention:
        return None
    report["automation_attention_count"] += 1
    prop = attention.get("property")
    if prop and prop not in report["automation_attention_properties"]:
        report["automation_attention_properties"].append(prop)
    for reason in attention.get("reasons") or []:
        if reason not in report["automation_attention_reasons"]:
            report["automation_attention_reasons"].append(reason)
    report["automation_attention_details"].append(attention)
    return attention


def downloader_summary(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("report_summary")
    if not isinstance(summary, dict):
        summary = {}
    attention = automation_attention_for_result(
        result,
        str(result.get("expected_target_month") or "").strip() or None,
    )
    return {
        "id": result.get("id"),
        "property": result.get("property"),
        "servicer": result.get("servicer"),
        "enabled": bool(result.get("enabled")),
        "co_owner_paid_mortgage": bool(result.get("co_owner_paid_mortgage")),
        "status": result.get("effective_status") or result.get("status"),
        "runtime_status": result.get("runtime_status") or result.get("status"),
        "effective_status": result.get("effective_status") or result.get("status"),
        "rc": result.get("rc"),
        "report": result.get("report"),
        "expected_target_month": result.get("expected_target_month"),
        "report_status": summary.get("status"),
        "report_reason": summary.get("reason"),
        "latest_supplemental_auth_report": summary.get("latest_supplemental_auth_report"),
        "latest_supplemental_auth_status": summary.get("latest_supplemental_auth_status"),
        "latest_supplemental_auth_reason": summary.get("latest_supplemental_auth_reason"),
        "latest_supplemental_auth_applied": summary.get("latest_supplemental_auth_applied"),
        "har_path": summary.get("har_path"),
        "har_path_exists": summary.get("har_path_exists"),
        "candidate_count": summary.get("candidate_count"),
        "candidate_source_counts": summary.get("candidate_source_counts"),
        "available_statement_months": summary.get("available_statement_months"),
        "downloadable_statement_months": summary.get("downloadable_statement_months"),
        "metadata_only_statement_months": summary.get("metadata_only_statement_months"),
        "latest_statement_month": summary.get("latest_statement_month"),
        "latest_downloadable_statement_month": summary.get("latest_downloadable_statement_month"),
        "target_month_candidate_count": summary.get("target_month_candidate_count"),
        "target_month_statement_candidates": summary.get("target_month_statement_candidates"),
        "target_month_candidate_source_counts": summary.get("target_month_candidate_source_counts"),
        "target_month_downloadable_count": summary.get("target_month_downloadable_count"),
        "target_month_downloadable_source_counts": summary.get("target_month_downloadable_source_counts"),
        "target_month_recapture_required": summary.get("target_month_recapture_required"),
        "target_month_recapture_reason": summary.get("target_month_recapture_reason"),
        "expected_document_ids": summary.get("expected_document_ids"),
        "body_recapture_capture_method": summary.get("body_recapture_capture_method"),
        "body_recapture_capture_instruction": summary.get("body_recapture_capture_instruction"),
        "body_recapture_manual_har_export_warning": summary.get("body_recapture_manual_har_export_warning"),
        "credentials_available": summary.get("credentials_available"),
        "credential_source": summary.get("credential_source"),
        "credential_lookup_status": summary.get("credential_lookup_status"),
        "credential_lookup_failure_reason": summary.get("credential_lookup_failure_reason"),
        "credential_lookup_exit_code": summary.get("credential_lookup_exit_code"),
        "credential_lookup_item_name": summary.get("credential_lookup_item_name"),
        "credential_lookup_item_id_configured": summary.get("credential_lookup_item_id_configured"),
        "credential_lookup_uri_host": summary.get("credential_lookup_uri_host"),
        "credential_lookup_login_hint_configured": summary.get("credential_lookup_login_hint_configured"),
        "credential_lookup_search_term_count": summary.get("credential_lookup_search_term_count"),
        "credential_lookup_search_terms": summary.get("credential_lookup_search_terms"),
        "credential_lookup_candidate_search_term_count": summary.get(
            "credential_lookup_candidate_search_term_count"
        ),
        "credential_lookup_candidate_search_terms": summary.get("credential_lookup_candidate_search_terms"),
        "credential_lookup_expected_folder_name": summary.get("credential_lookup_expected_folder_name"),
        "credential_lookup_expected_folder_id_configured": summary.get("credential_lookup_expected_folder_id_configured"),
        "credential_lookup_script": summary.get("credential_lookup_script"),
        "credential_lookup_candidate_count": summary.get("credential_lookup_candidate_count"),
        "credential_lookup_candidate_items": summary.get("credential_lookup_candidate_items"),
        "credential_lookup_unguarded_candidate_count": summary.get("credential_lookup_unguarded_candidate_count"),
        "credential_lookup_unguarded_candidate_items": summary.get("credential_lookup_unguarded_candidate_items"),
        "credential_lookup_misfiled_candidate_count": summary.get("credential_lookup_misfiled_candidate_count"),
        "credential_lookup_absent": summary.get("credential_lookup_absent"),
        "credential_lookup_scope": summary.get("credential_lookup_scope"),
        "credential_lookup_repair_action": summary.get("credential_lookup_repair_action"),
        "credential_login_hint_mismatch": summary.get("credential_login_hint_mismatch"),
        "credential_login_hint_mismatch_overridden": summary.get("credential_login_hint_mismatch_overridden"),
        "target_month": summary.get("target_month"),
        "target_month_statement_available": summary.get("target_month_statement_available"),
        "target_month_existing_count": summary.get("target_month_existing_count"),
        "target_month_downloaded_count": summary.get("target_month_downloaded_count"),
        "target_month_skipped_count": summary.get("target_month_skipped_count"),
        "existing_target_month_files": summary.get("existing_target_month_files"),
        "downloaded_target_month_files": summary.get("downloaded_target_month_files"),
        "skipped_target_month_files": summary.get("skipped_target_month_files"),
        "safe_to_run_automatically": summary.get("safe_to_run_automatically") is True,
        "idempotent_replay_safe": summary.get("idempotent_replay_safe"),
        "copy_plan_safe_to_apply_automatically": summary.get("copy_plan_safe_to_apply_automatically"),
        "automation_readiness_status": summary.get("automation_readiness_status"),
        "automation_blockers": summary.get("automation_blockers"),
        "har_replay_ready_to_run_automatically": summary.get("har_replay_ready_to_run_automatically"),
        "har_workflow_capture_quality_status": summary.get("har_workflow_capture_quality_status"),
        "har_workflow_replay_blocker": summary.get("har_workflow_replay_blocker"),
        "har_workflow_target_month": summary.get("har_workflow_target_month"),
        "har_workflow_target_month_replayable_document_available": summary.get(
            "har_workflow_target_month_replayable_document_available"
        ),
        "har_workflow_target_month_replayable_document_payload_count": summary.get(
            "har_workflow_target_month_replayable_document_payload_count"
        ),
        "har_workflow_can_replay_documents": summary.get("har_workflow_can_replay_documents"),
        "report_idempotency_digest": summary.get("idempotency_digest"),
        "idempotent_skip": summary.get("idempotent_skip"),
        "idempotent_skip_reason": summary.get("idempotent_skip_reason"),
        "idempotent_skip_latest_live_auth_attention": summary.get("idempotent_skip_latest_live_auth_attention"),
        "automation_attention_required": bool(attention),
        "automation_attention_reasons": attention.get("reasons") if attention else [],
        "latest_live_auth_status": summary.get("latest_live_auth_status"),
        "latest_live_auth_report": summary.get("latest_live_auth_report"),
        "latest_live_auth_auth_failure_reason": summary.get("latest_live_auth_auth_failure_reason"),
        "latest_live_auth_auth_failure_visible_reason": summary.get("latest_live_auth_auth_failure_visible_reason"),
        "latest_live_auth_manual_auth_required": summary.get("latest_live_auth_manual_auth_required"),
        "latest_live_auth_manual_auth_reason": summary.get("latest_live_auth_manual_auth_reason"),
        "downloaded_count": summary.get("downloaded_count"),
        "skipped_count": summary.get("skipped_count"),
        "error_count": summary.get("error_count"),
        "warning_count": summary.get("warning_count"),
        "manual_auth_required": summary.get("manual_auth_required"),
        "manual_auth_reason": summary.get("manual_auth_reason"),
        "auth_state": summary.get("auth_state"),
        "auth_stage": summary.get("auth_stage"),
        "auth_issue": summary.get("auth_issue"),
        "auth_issue_text": summary.get("auth_issue_text"),
        "login_mode": summary.get("login_mode"),
        "auto_login_attempted": summary.get("auto_login_attempted"),
        "auto_login_status": summary.get("auto_login_status"),
        "auto_login_blocked_reason": summary.get("auto_login_blocked_reason"),
        "auto_login_force_enabled": summary.get("auto_login_force_enabled"),
        "auto_login_username_available": summary.get("auto_login_username_available"),
        "auto_login_password_available": summary.get("auto_login_password_available"),
        "auto_login_username_typed": summary.get("auto_login_username_typed"),
        "auto_login_password_typed": summary.get("auto_login_password_typed"),
        "auto_login_step": summary.get("auto_login_step"),
        "operator_next_action": summary.get("operator_next_action"),
        "required_capture_quality": summary.get("required_capture_quality"),
        "suggested_next_action": summary.get("suggested_next_action"),
        "target_month_document_identifiers": summary.get("target_month_document_identifiers"),
        "auth_failure_reason": summary.get("auth_failure_reason"),
        "auth_failure_visible_reason": summary.get("auth_failure_visible_reason"),
        "auth_mfa_reached": summary.get("auth_mfa_reached"),
        "credential_login_failure_suspected": summary.get("credential_login_failure_suspected"),
        "credential_login_failure_suspected_reason": summary.get(
            "credential_login_failure_suspected_reason"
        ),
    }


def parse_selected_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def default_scoped_report_path(report_path: Path, selected_ids: list[str], profile_name: str) -> Path:
    scope_parts = []
    if profile_name:
        scope_parts.append(profile_name)
    scope_parts.extend(selected_ids)
    if not scope_parts:
        return report_path
    scope = slugify("_".join(scope_parts))
    return report_path.with_name(f"{report_path.stem}_{scope}{report_path.suffix}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("MORTGAGE_DOWNLOADER_CONFIG", DEFAULT_CONFIG)))
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("MORTGAGE_DOWNLOADER_DRY_RUN") == "1")
    parser.add_argument("--profile", default=os.environ.get("MORTGAGE_DOWNLOADER_PROFILE", "").strip())
    parser.add_argument(
        "--target-month",
        default=os.environ.get("MORTGAGE_STATEMENT_TARGET_MONTH", "").strip(),
        help="YYYY-MM statement month to pass to every downloader",
    )
    parser.add_argument(
        "--summarize-existing",
        action="store_true",
        default=(
            os.environ.get("MORTGAGE_DOWNLOADER_SUMMARIZE_EXISTING") == "1"
            or os.environ.get("MORTGAGE_DOWNLOADER_REPORT_ONLY") == "1"
        ),
        help="Do not execute downloaders; rebuild the aggregate from configured per-downloader reports",
    )
    parser.add_argument(
        "--id",
        dest="ids",
        action="append",
        default=parse_selected_ids(os.environ.get("MORTGAGE_DOWNLOADER_IDS")),
        help="Run only the downloader with this id; may be repeated",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    install_child_process_cleanup_handlers()
    args = parse_args(argv)
    config_path = args.config
    dry_run = bool(args.dry_run)
    summarize_existing = bool(args.summarize_existing)
    profile_name = str(args.profile or "").strip()
    selected_ids = [str(item).strip() for item in (args.ids or []) if str(item).strip()]
    selected_id_set = set(selected_ids)
    explicit_report_value = os.environ.get("MORTGAGE_DOWNLOADER_REPORT")
    explicit_report_requested = args.report is not None or bool(explicit_report_value)
    configured_report_path = Path(args.report or explicit_report_value or DEFAULT_REPORT)
    report_path = configured_report_path
    if selected_ids and not explicit_report_requested:
        report_path = default_scoped_report_path(configured_report_path, selected_ids, profile_name)
    report: dict[str, Any] = {
        "job": "mortgage-statement-downloaders",
        "started_at": utc_now(),
        "config": str(config_path),
        "report": str(report_path),
        "canonical_report": str(configured_report_path),
        "report_scope": "selected_ids" if selected_ids and not explicit_report_requested else "canonical",
        "report_explicitly_requested": explicit_report_requested,
        "dry_run": dry_run,
        "summarize_existing": summarize_existing,
        "profile": profile_name or None,
        "selected_ids": selected_ids,
        "target_month": str(args.target_month or "").strip() or None,
        "mortgage_statement_target_month": str(args.target_month or "").strip() or None,
        "target_month_override": str(args.target_month or "").strip() or None,
        "downloader_expected_target_months": [],
        "downloader_expected_target_month_count": 0,
        "downloader_effective_statement_target_month": None,
        "target_month_matches_all_downloader_expected_months": None,
        "target_month_differs_from_downloader_expected_months": None,
        "entries": [],
        "results": [],
        "downloader_summaries": [],
        "downloader_status_counts": {},
        "status": "unknown",
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "enabled_count": 0,
        "eligible_count": 0,
        "skipped_non_co_owner_paid_count": 0,
        "safe_to_run_automatically": False,
        "safe_downloader_count": 0,
        "unsafe_downloader_count": 0,
        "unsafe_downloader_properties": [],
        "unsafe_downloader_details": [],
        "target_month_statement_available_count": 0,
        "target_month_statement_gap_count": 0,
        "target_month_statement_gap_properties": [],
        "target_month_statement_gaps": [],
        "current_cycle_statement_ready": False,
        "current_cycle_statement_ready_count": 0,
        "current_cycle_statement_blocker_count": 0,
        "current_cycle_statement_blocker_properties": [],
        "current_cycle_future_automation_attention_required": False,
        "current_cycle_future_automation_attention_properties": [],
        "current_cycle_future_automation_attention_reasons": [],
        "failed_downloader_target_month_statement_available_count": 0,
        "failed_downloader_target_month_statement_available_properties": [],
        "failed_downloader_target_month_statement_available_details": [],
        "automation_attention_count": 0,
        "automation_attention_properties": [],
        "automation_attention_reasons": [],
        "automation_attention_details": [],
        "citadel_download_rc": None,
        "citadel_prepare_rc": None,
        "citadel_prepare_status": None,
        "citadel_prepare_reason": None,
        "citadel_prepare_successful_password_request_found": None,
        "citadel_prepare_bw_session_status": None,
        "citadel_prepare_bw_item_found": None,
        "citadel_prepare_bw_item_name": None,
        "citadel_prepare_bw_item_uri_host_match": None,
        "citadel_prepare_username_matches_har": None,
        "citadel_prepare_password_matched_before_update": None,
        "citadel_prepare_password_updated": None,
        "citadel_prepare_bw_sync_attempted": None,
        "citadel_auth_retryable_portal_failure": False,
        "citadel_auth_retryable_reason": None,
        "citadel_capture_report": None,
        "citadel_capture_status": None,
        "citadel_capture_reason": None,
        "citadel_capture_har_token_available": None,
        "citadel_capture_har_token_entry_count": None,
        "citadel_capture_har_token_mobile_source_id_available": None,
        "citadel_capture_har_token_source_endpoint_paths": None,
        "citadel_capture_har_token_source_document_detail_id_count": None,
        "citadel_capture_har_token_endpoint_statuses": None,
        "citadel_capture_next_action_status": None,
        "citadel_capture_next_action_reason": None,
        "citadel_capture_next_action_command": None,
        "citadel_capture_next_action_capture_command": None,
        "citadel_capture_next_action_capture_required": None,
        "citadel_capture_next_action_target_month": None,
        "citadel_capture_next_action_target_month_replayable_document_available": None,
        "citadel_capture_next_action_target_month_replayable_document_payload_count": None,
        "citadel_capture_next_action_replayable_statement_months": None,
        "citadel_capture_next_action_statement_document_months": None,
        "citadel_capture_next_action_required_response_paths": None,
        "citadel_capture_next_action_required_response_path_counts": None,
        "citadel_capture_next_action_required_response_path_progress": None,
        "citadel_capture_next_action_response_body_requirements": None,
        "citadel_capture_required_response_paths": None,
        "citadel_capture_required_response_path_counts": None,
        "citadel_capture_required_response_path_progress": None,
        "citadel_capture_response_body_requirements": None,
        "citadel_capture_response_body_requirement_role_counts": None,
        "citadel_capture_captured_response_body_requirement_counts": None,
        "citadel_capture_response_body_requirement_role_capture_counts": None,
        "citadel_capture_missing_response_body_requirements": None,
        "citadel_capture_missing_response_body_requirement_count": None,
        "citadel_capture_captured_required_response_paths": None,
        "citadel_capture_captured_required_response_path_counts": None,
        "citadel_capture_missing_required_response_paths": None,
        "citadel_capture_missing_required_response_path_counts": None,
        "citadel_capture_manual_auth_required": None,
        "citadel_capture_manual_auth_file": None,
        "citadel_capture_manual_auth_portal_url": None,
        "citadel_capture_manual_auth_target_id": None,
        "citadel_capture_manual_auth_next_command": None,
        "citadel_capture_manual_auth_install_verified_har_dry_run_command": None,
        "citadel_capture_manual_auth_install_verified_har_apply_command": None,
        "citadel_capture_authenticated_found": None,
        "citadel_capture_candidate_count": None,
        "citadel_capture_route_counts": None,
        "citadel_capture_login_tab_count": None,
        "citadel_capture_non_login_tab_count": None,
        "citadel_capture_scanned_count": None,
        "citadel_capture_captured_endpoint_count": None,
        "citadel_capture_captured_response_body_count": None,
        "citadel_capture_source_har_path_exists": None,
        "citadel_capture_source_direct_pdf_limit": None,
        "citadel_capture_source_direct_pdf_candidate_count": None,
        "citadel_capture_source_direct_pdf_path_counts": None,
        "citadel_capture_source_direct_pdf_fetched_count": None,
        "citadel_capture_source_direct_pdf_replayable_count": None,
        "citadel_capture_source_required_response_candidate_count": None,
        "citadel_capture_source_required_response_path_counts": None,
        "citadel_capture_source_required_response_fetched_count": None,
        "citadel_capture_source_required_response_replayable_count": None,
        "citadel_capture_statement_candidate_count": None,
        "citadel_capture_replayable_document_payload_count": None,
        "citadel_capture_target_month": None,
        "citadel_capture_target_month_replayable_document_available": None,
        "citadel_capture_target_month_replayable_document_payload_count": None,
        "citadel_capture_replayable_statement_months": None,
        "citadel_capture_statement_document_months": None,
        "citadel_capture_direct_pdf_response_count": None,
        "citadel_capture_har_path": None,
        "citadel_capture_error_count": None,
        "citadel_report_status": None,
        "citadel_downloaded_count": None,
        "citadel_skipped_count": None,
        "citadel_target_month": None,
        "citadel_target_month_statement_available": None,
        "citadel_target_month_existing_count": None,
        "citadel_target_month_downloaded_count": None,
        "citadel_target_month_skipped_count": None,
        "citadel_existing_target_month_files": None,
        "citadel_downloaded_target_month_files": None,
        "citadel_skipped_target_month_files": None,
        "citadel_safe_to_run_automatically": None,
        "citadel_idempotent_replay_safe": None,
        "citadel_copy_plan_safe_to_apply_automatically": None,
        "citadel_har_replay_ready_to_run_automatically": None,
        "citadel_automation_readiness_status": None,
        "citadel_automation_blockers": None,
        "citadel_idempotent_skip": None,
        "citadel_idempotent_skip_reason": None,
        "citadel_idempotent_skip_latest_live_auth_attention": None,
        "citadel_latest_live_auth_report": None,
        "citadel_latest_live_auth_status": None,
        "citadel_latest_live_auth_credentials_available": None,
        "citadel_latest_live_auth_manual_auth_required": None,
        "citadel_latest_live_auth_manual_auth_reason": None,
        "citadel_latest_live_auth_manual_auth_file": None,
        "citadel_latest_live_auth_manual_auth_portal_url": None,
        "citadel_latest_live_auth_auth_failure_reason": None,
        "citadel_latest_live_auth_auth_failure_visible_reason": None,
        "citadel_latest_live_auth_auth_visible_error": None,
        "citadel_latest_live_auth_login_form_last_result": None,
        "citadel_latest_live_auth_oauth_password_grant_failure_count": None,
        "citadel_latest_live_auth_oauth_password_grant_error_codes": None,
        "citadel_latest_live_auth_login_form_submitted": None,
        "citadel_error_count": None,
        "citadel_warning_count": None,
        "citadel_auth_state": None,
        "citadel_credentials_available": None,
        "citadel_login_mode": None,
        "citadel_otp_required": None,
        "citadel_otp_wait_ms": None,
        "citadel_otp_file": None,
        "citadel_otp_required_file": None,
        "citadel_otp_next_command": None,
        "citadel_manual_auth_required": None,
        "citadel_manual_auth_reason": None,
        "citadel_manual_auth_file": None,
        "citadel_manual_auth_portal_url": None,
        "citadel_auth_failure_reason": None,
        "citadel_auth_failure_visible_reason": None,
        "citadel_auth_visible_error": None,
        "citadel_credential_state_drift_suspected": None,
        "citadel_login_form_last_result": None,
        "citadel_oauth_password_grant_failure_count": None,
        "citadel_oauth_password_grant_error_codes": None,
        "citadel_browser_storage_bearer_token_available": None,
        "citadel_browser_storage_mobile_source_id_available": None,
        "citadel_browser_storage_token_candidate_count": None,
        "citadel_api_header_mobile_source_id_available": None,
        "citadel_api_header_authorization_enabled": None,
        "citadel_direct_auth_status": None,
        "citadel_direct_auth_transport": None,
        "citadel_direct_browser_fallback_attempted": None,
        "citadel_direct_browser_fallback_authenticated_found": None,
        "citadel_direct_fresh_mfa_source_status": None,
        "citadel_direct_fresh_recaptcha_token_available": None,
        "citadel_direct_fresh_recaptcha_token_length": None,
        "citadel_direct_recaptcha_eval_stage": None,
        "citadel_direct_recaptcha_eval_error": None,
        "citadel_direct_recaptcha_token_action": None,
        "citadel_direct_recaptcha_action_errors": None,
        "citadel_direct_mfa_process_id_header_available": None,
        "citadel_direct_mfa_detail_available": None,
        "citadel_direct_mfa_request_uuid_available": None,
        "citadel_direct_mfa_request_uuid_source": None,
        "citadel_direct_no_mfa_handoff_after_password": None,
        "citadel_direct_no_mfa_handoff_reason": None,
        "citadel_direct_otp_send_via_type": None,
        "citadel_direct_cdp_mfa_process_id_header_available": None,
        "citadel_direct_oauth_cdp_event_count": None,
        "citadel_direct_oauth_cdp_last_status": None,
        "citadel_direct_password_token_request_shape_matches_har_success": None,
        "citadel_direct_password_token_request_shape_matches_har_failure": None,
        "citadel_direct_password_token_request_shape_matched_har_statuses": None,
        "citadel_direct_password_token_request_structure_matches_har_success": None,
        "citadel_direct_password_token_request_structure_matches_har_failure": None,
        "citadel_direct_password_token_request_structure_matched_har_statuses": None,
        "citadel_direct_error_codes": None,
        "citadel_direct_error_categories": None,
        "citadel_credential_item_name": None,
        "citadel_credential_item_uri_hosts": None,
        "citadel_credential_item_portal_host_match": None,
        "citadel_credential_item_field_names": None,
        "citadel_credential_item_notes_len": None,
        "citadel_credential_login_hint_configured": None,
        "citadel_credential_username_matches_login_hint": None,
        "citadel_credential_item_name_matches_login_hint": None,
        "citadel_credential_field_matches_login_hint": None,
        "citadel_credential_username_len": None,
        "citadel_credential_password_len": None,
        "citadel_credential_username_has_at": None,
        "citadel_har_auth_diagnostics_enabled": None,
        "citadel_har_auth_diagnostics_skipped_reason": None,
        "citadel_har_token_credential_match_count": None,
        "citadel_har_successful_password_token_match": None,
        "citadel_har_mfa_process_id_header_count": None,
        "citadel_har_token_attempt_statuses": None,
        "citadel_har_workflow_embedded_response_body_count": None,
        "citadel_har_workflow_replayable_json_response_count": None,
        "citadel_har_workflow_replayable_document_payload_count": None,
        "citadel_har_workflow_target_month": None,
        "citadel_har_workflow_target_month_replayable_document_available": None,
        "citadel_har_workflow_target_month_replayable_document_payload_count": None,
        "citadel_har_workflow_replayable_statement_months": None,
        "citadel_har_workflow_statement_document_months": None,
        "citadel_har_workflow_direct_pdf_response_count": None,
        "citadel_har_workflow_source_direct_pdf_candidate_count": None,
        "citadel_har_workflow_source_direct_pdf_path_counts": None,
        "citadel_har_workflow_source_direct_pdf_filenames": None,
        "citadel_har_workflow_source_direct_pdf_filename_candidates": None,
        "citadel_har_workflow_target_month_direct_pdf_filenames": None,
        "citadel_har_workflow_target_month_direct_pdf_filename_candidates": None,
        "citadel_har_workflow_target_month_direct_pdf_body_missing_candidate_count": None,
        "citadel_har_workflow_source_required_response_candidate_count": None,
        "citadel_har_workflow_source_required_response_path_counts": None,
        "citadel_har_workflow_direct_pdf_missing_response_count": None,
        "citadel_har_workflow_direct_pdf_missing_response_paths": None,
        "citadel_har_workflow_capture_quality_status": None,
        "citadel_har_workflow_replay_blocker": None,
        "citadel_har_workflow_missing_response_body_count": None,
        "citadel_har_workflow_missing_response_body_paths": None,
        "citadel_har_workflow_missing_response_body_path_counts": None,
        "citadel_har_workflow_response_body_requirements": None,
        "citadel_har_workflow_embedded_access_token_count": None,
        "citadel_har_workflow_can_replay_documents": None,
        "citadel_har_workflow_next_action_status": None,
        "citadel_har_workflow_next_action_reason": None,
        "citadel_har_workflow_next_action_command": None,
        "citadel_har_workflow_next_action_capture_command": None,
        "citadel_har_workflow_next_action_capture_required": None,
        "citadel_har_workflow_next_action_required_response_paths": None,
        "citadel_har_workflow_next_action_response_body_requirements": None,
        "citadel_har_workflow_next_action_source_direct_pdf_filenames": None,
        "citadel_har_workflow_next_action_target_month_direct_pdf_filenames": None,
        "citadel_har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count": None,
        "citadel_har_workflow_next_action_source_direct_pdf_candidate_count": None,
        "citadel_har_workflow_next_action_source_direct_pdf_path_counts": None,
        "citadel_har_workflow_next_action_source_required_response_candidate_count": None,
        "citadel_har_workflow_next_action_source_required_response_path_counts": None,
        "citadel_har_workflow_next_action_install_verified_capture_report": None,
        "citadel_har_workflow_next_action_install_verified_capture_dry_run_command": None,
        "citadel_har_workflow_next_action_install_verified_capture_apply_command": None,
        "citadel_har_workflow_next_action_install_verified_capture_direct_dry_run_command": None,
        "citadel_har_workflow_next_action_install_verified_capture_direct_apply_command": None,
        "citadel_install_verified_capture_report": None,
        "citadel_install_verified_capture_apply_command": None,
        "citadel_install_verified_capture_direct_apply_command": None,
        "citadel_tab_scan_candidate_count": None,
        "citadel_tab_scan_limit": None,
        "citadel_tab_scan_scanned_count": None,
        "citadel_tab_scan_skipped_count": None,
        "citadel_tab_scan_fetch_timeout_ms": None,
        "citadel_tab_scan_target_id_requested": None,
        "citadel_tab_scan_target_id_found": None,
        "citadel_tab_scan_authenticated_found": None,
        "citadel_tab_scan_direct_fallback_target_selected": None,
        "citadel_tab_scan_direct_fallback_target_id_requested": None,
        "citadel_tab_scan_direct_fallback_target_id_found": None,
        "citadel_oauth_network_event_count": None,
        "citadel_oauth_network_statuses": None,
        "citadel_oauth_network_error_codes": None,
        "citadel_oauth_network_request_shape_matched_har_statuses": None,
        "citadel_oauth_network_request_structure_matched_har_statuses": None,
    }
    try:
        config = load_config(config_path)
        entries = config["downloaders"]
        if selected_id_set:
            entries = [entry for entry in entries if str(entry.get("id") or "") in selected_id_set]
            missing_ids = sorted(selected_id_set - {str(entry.get("id") or "") for entry in entries})
            if missing_ids:
                report["status"] = "error"
                report["failed_count"] = 1
                report["error"] = f"unknown downloader id(s): {', '.join(missing_ids)}"
                return 2
        supplemental_contexts = load_supplemental_downloader_contexts(report_path, config_path=config_path)
        for entry in entries:
            entry_env_overrides = target_month_env_for_entry(entry, profile_name, args.target_month)
            if summarize_existing and not dry_run:
                result = summarize_existing_entry(entry, profile_name=profile_name)
            else:
                result = run_entry(entry, dry_run=dry_run, profile_name=profile_name, env_overrides=entry_env_overrides)
            expected_target_month = expected_target_month_for_entry(
                entry,
                profile_name=profile_name,
                global_target_month=args.target_month,
            )
            result["expected_target_month"] = expected_target_month
            apply_expected_target_month_validation(result, expected_target_month)
            overlay_supplemental_auth_context(result, supplemental_contexts, expected_target_month)
            report["entries"].append(result)
            report["results"].append(result)
            report["downloader_summaries"].append(downloader_summary(result))
            result_status = str(result.get("effective_status") or result.get("status") or "unknown")
            report["downloader_status_counts"][result_status] = (
                int(report["downloader_status_counts"].get(result_status) or 0) + 1
            )
            if result_status == "ok":
                report["success_count"] += 1
            if result_status == "disabled" or result_status.startswith("skipped"):
                report["skipped_count"] += 1
            if result.get("enabled"):
                report["enabled_count"] += 1
            if result.get("enabled") and result.get("co_owner_paid_mortgage"):
                report["eligible_count"] += 1
                if result.get("safe_to_run_automatically") is True:
                    report["safe_downloader_count"] += 1
                else:
                    report["unsafe_downloader_count"] += 1
                    prop = result.get("property")
                    if prop and prop not in report["unsafe_downloader_properties"]:
                        report["unsafe_downloader_properties"].append(prop)
                    result_summary = result.get("report_summary") or {}
                    if not isinstance(result_summary, dict):
                        result_summary = {}
                    report["unsafe_downloader_details"].append(
                        {
                            "id": result.get("id"),
                            "property": prop,
                            "servicer": result.get("servicer"),
                            "status": result.get("effective_status") or result.get("status"),
                            "runtime_status": result.get("runtime_status") or result.get("status"),
                            "effective_status": result.get("effective_status") or result.get("status"),
                            "rc": result.get("rc"),
                            "report": result.get("report"),
                            "report_status": result.get("report_status"),
                            "report_reason": result.get("report_reason"),
                            "safe_to_run_automatically": result.get("safe_to_run_automatically") is True,
                            "idempotent_replay_safe": result.get("idempotent_replay_safe"),
                            "expected_target_month": result.get("expected_target_month"),
                            "target_month": result.get("target_month"),
                            "report_target_month": result.get("report_target_month"),
                            "report_target_month_matches_expected": result.get("report_target_month_matches_expected"),
                            "stale_report_for_expected_target_month": result.get("stale_report_for_expected_target_month"),
                            "stale_report_reason": result.get("stale_report_reason"),
                            "target_month_statement_available": result.get("target_month_statement_available"),
                            "idempotent_skip": result.get("idempotent_skip"),
                            "credential_lookup_status": result.get("credential_lookup_status")
                            or result_summary.get("credential_lookup_status"),
                            "credential_lookup_failure_reason": result.get("credential_lookup_failure_reason")
                            or result_summary.get("credential_lookup_failure_reason"),
                            "credential_lookup_item_name": result.get("credential_lookup_item_name")
                            or result_summary.get("credential_lookup_item_name"),
                            "credential_lookup_uri_host": result.get("credential_lookup_uri_host")
                            or result_summary.get("credential_lookup_uri_host"),
                            "credential_lookup_search_term_count": result.get("credential_lookup_search_term_count")
                            if result.get("credential_lookup_search_term_count") is not None
                            else result_summary.get("credential_lookup_search_term_count"),
                            "credential_lookup_search_terms": result.get("credential_lookup_search_terms")
                            if result.get("credential_lookup_search_terms") is not None
                            else result_summary.get("credential_lookup_search_terms"),
                            "credential_lookup_candidate_search_term_count": result.get(
                                "credential_lookup_candidate_search_term_count"
                            )
                            if result.get("credential_lookup_candidate_search_term_count") is not None
                            else result_summary.get("credential_lookup_candidate_search_term_count"),
                            "credential_lookup_candidate_search_terms": result.get(
                                "credential_lookup_candidate_search_terms"
                            )
                            if result.get("credential_lookup_candidate_search_terms") is not None
                            else result_summary.get("credential_lookup_candidate_search_terms"),
                            "credential_lookup_expected_folder_name": result.get("credential_lookup_expected_folder_name")
                            or result_summary.get("credential_lookup_expected_folder_name"),
                            "credential_lookup_expected_folder_id_configured": result.get("credential_lookup_expected_folder_id_configured")
                            if result.get("credential_lookup_expected_folder_id_configured") is not None
                            else result_summary.get("credential_lookup_expected_folder_id_configured"),
                            "credential_lookup_login_hint_configured": result.get("credential_lookup_login_hint_configured")
                            if result.get("credential_lookup_login_hint_configured") is not None
                            else result_summary.get("credential_lookup_login_hint_configured"),
                            "credential_lookup_candidate_count": result.get("credential_lookup_candidate_count")
                            if result.get("credential_lookup_candidate_count") is not None
                            else result_summary.get("credential_lookup_candidate_count"),
                            "credential_lookup_candidate_items": result.get("credential_lookup_candidate_items")
                            if result.get("credential_lookup_candidate_items") is not None
                            else result_summary.get("credential_lookup_candidate_items"),
                            "credential_lookup_unguarded_candidate_count": result.get(
                                "credential_lookup_unguarded_candidate_count"
                            )
                            if result.get("credential_lookup_unguarded_candidate_count") is not None
                            else result_summary.get("credential_lookup_unguarded_candidate_count"),
                            "credential_lookup_unguarded_candidate_items": result.get(
                                "credential_lookup_unguarded_candidate_items"
                            )
                            if result.get("credential_lookup_unguarded_candidate_items") is not None
                            else result_summary.get("credential_lookup_unguarded_candidate_items"),
                            "credential_lookup_misfiled_candidate_count": result.get(
                                "credential_lookup_misfiled_candidate_count"
                            )
                            if result.get("credential_lookup_misfiled_candidate_count") is not None
                            else result_summary.get("credential_lookup_misfiled_candidate_count"),
                            "credential_lookup_absent": result.get("credential_lookup_absent")
                            if result.get("credential_lookup_absent") is not None
                            else result_summary.get("credential_lookup_absent"),
                            "credential_lookup_scope": result.get("credential_lookup_scope")
                            or result_summary.get("credential_lookup_scope"),
                            "credential_lookup_repair_action": result.get("credential_lookup_repair_action")
                            or result_summary.get("credential_lookup_repair_action"),
                            "credential_login_hint_mismatch": result.get("credential_login_hint_mismatch")
                            if result.get("credential_login_hint_mismatch") is not None
                            else result_summary.get("credential_login_hint_mismatch"),
                            "credential_login_hint_mismatch_overridden": result.get(
                                "credential_login_hint_mismatch_overridden"
                            )
                            if result.get("credential_login_hint_mismatch_overridden") is not None
                            else result_summary.get("credential_login_hint_mismatch_overridden"),
		                        }
	                    )
            if result.get("status") == "skipped_not_co_owner_paid_mortgage":
                report["skipped_non_co_owner_paid_count"] += 1
            append_target_month_statement_status(
                report,
                result,
                dry_run=dry_run,
                expected_target_month=expected_target_month,
            )
            append_failed_downloader_statement_coverage(
                report,
                result,
                expected_target_month=expected_target_month,
            )
            append_automation_attention(
                report,
                result,
                expected_target_month=expected_target_month,
            )
            if result.get("id") == "citadel-90-madison":
                report["citadel_download_rc"] = int(result.get("rc") or 0)
                prepare_result = result.get("prepare") or {}
                if isinstance(prepare_result, dict):
                    report["citadel_prepare_rc"] = prepare_result.get("rc")
                    report["citadel_prepare_status"] = prepare_result.get("status")
                prepare_summary = result.get("prepare_report_summary") or {}
                if isinstance(prepare_summary, dict):
                    report["citadel_prepare_reason"] = prepare_summary.get("reason")
                    report["citadel_prepare_successful_password_request_found"] = prepare_summary.get("successful_password_request_found")
                    report["citadel_prepare_bw_session_status"] = prepare_summary.get("bw_session_status")
                    report["citadel_prepare_bw_item_found"] = prepare_summary.get("bw_item_found")
                    report["citadel_prepare_bw_item_name"] = prepare_summary.get("bw_item_name")
                    report["citadel_prepare_bw_item_uri_host_match"] = prepare_summary.get("bw_item_uri_host_match")
                    report["citadel_prepare_username_matches_har"] = prepare_summary.get("username_matches_har")
                    report["citadel_prepare_password_matched_before_update"] = prepare_summary.get("password_matched_before_update")
                    report["citadel_prepare_password_updated"] = prepare_summary.get("password_updated")
                    report["citadel_prepare_bw_sync_attempted"] = prepare_summary.get("bw_sync_attempted")
                    report["citadel_capture_report"] = prepare_summary.get("path")
                    report["citadel_capture_status"] = prepare_summary.get("status")
                    report["citadel_capture_reason"] = prepare_summary.get("reason")
                    report["citadel_capture_har_token_available"] = prepare_summary.get("har_token_available")
                    report["citadel_capture_har_token_entry_count"] = prepare_summary.get("har_token_entry_count")
                    report["citadel_capture_har_token_mobile_source_id_available"] = prepare_summary.get("har_token_mobile_source_id_available")
                    report["citadel_capture_har_token_source_endpoint_paths"] = prepare_summary.get("har_token_source_endpoint_paths")
                    report["citadel_capture_har_token_source_document_detail_id_count"] = prepare_summary.get("har_token_source_document_detail_id_count")
                    report["citadel_capture_har_token_endpoint_statuses"] = prepare_summary.get("har_token_endpoint_statuses")
                    report["citadel_capture_next_action_status"] = prepare_summary.get("next_action_status")
                    report["citadel_capture_next_action_reason"] = prepare_summary.get("next_action_reason")
                    report["citadel_capture_next_action_command"] = prepare_summary.get("next_action_command")
                    report["citadel_capture_next_action_capture_command"] = prepare_summary.get("next_action_capture_command")
                    report["citadel_capture_next_action_capture_required"] = prepare_summary.get("next_action_capture_required")
                    report["citadel_capture_next_action_target_month"] = prepare_summary.get("next_action_target_month")
                    report["citadel_capture_next_action_target_month_replayable_document_available"] = prepare_summary.get("next_action_target_month_replayable_document_available")
                    report["citadel_capture_next_action_target_month_replayable_document_payload_count"] = prepare_summary.get("next_action_target_month_replayable_document_payload_count")
                    report["citadel_capture_next_action_replayable_statement_months"] = prepare_summary.get("next_action_replayable_statement_months")
                    report["citadel_capture_next_action_statement_document_months"] = prepare_summary.get("next_action_statement_document_months")
                    report["citadel_capture_next_action_required_response_paths"] = prepare_summary.get("next_action_required_response_paths")
                    report["citadel_capture_next_action_required_response_path_counts"] = prepare_summary.get("next_action_required_response_path_counts")
                    report["citadel_capture_next_action_required_response_path_progress"] = prepare_summary.get("next_action_required_response_path_progress")
                    report["citadel_capture_next_action_response_body_requirements"] = prepare_summary.get("next_action_response_body_requirements")
                    report["citadel_capture_manual_auth_required"] = prepare_summary.get("manual_auth_required")
                    report["citadel_capture_manual_auth_file"] = prepare_summary.get("manual_auth_file")
                    report["citadel_capture_manual_auth_portal_url"] = prepare_summary.get("manual_auth_portal_url")
                    report["citadel_capture_manual_auth_target_id"] = prepare_summary.get("manual_auth_target_id")
                    report["citadel_capture_manual_auth_next_command"] = prepare_summary.get("manual_auth_next_command")
                    report["citadel_capture_manual_auth_install_verified_har_dry_run_command"] = prepare_summary.get(
                        "manual_auth_install_verified_har_dry_run_command"
                    )
                    report["citadel_capture_manual_auth_install_verified_har_apply_command"] = prepare_summary.get(
                        "manual_auth_install_verified_har_apply_command"
                    )
                    report["citadel_capture_authenticated_found"] = prepare_summary.get("authenticated_found")
                    report["citadel_capture_candidate_count"] = prepare_summary.get("candidate_count")
                    report["citadel_capture_route_counts"] = prepare_summary.get("route_counts")
                    report["citadel_capture_login_tab_count"] = prepare_summary.get("login_tab_count")
                    report["citadel_capture_non_login_tab_count"] = prepare_summary.get("non_login_tab_count")
                    report["citadel_capture_scanned_count"] = prepare_summary.get("scanned_count")
                    report["citadel_capture_captured_endpoint_count"] = prepare_summary.get("captured_endpoint_count")
                    report["citadel_capture_captured_response_body_count"] = prepare_summary.get("captured_response_body_count")
                    report["citadel_capture_source_har_path_exists"] = prepare_summary.get("source_har_path_exists")
                    report["citadel_capture_source_direct_pdf_limit"] = prepare_summary.get("source_direct_pdf_limit")
                    report["citadel_capture_source_direct_pdf_candidate_count"] = prepare_summary.get("source_direct_pdf_candidate_count")
                    report["citadel_capture_source_direct_pdf_path_counts"] = prepare_summary.get("source_direct_pdf_path_counts")
                    report["citadel_capture_source_direct_pdf_fetched_count"] = prepare_summary.get("source_direct_pdf_fetched_count")
                    report["citadel_capture_source_direct_pdf_replayable_count"] = prepare_summary.get("source_direct_pdf_replayable_count")
                    report["citadel_capture_source_required_response_candidate_count"] = prepare_summary.get("source_required_response_candidate_count")
                    report["citadel_capture_source_required_response_path_counts"] = prepare_summary.get("source_required_response_path_counts")
                    report["citadel_capture_source_required_response_fetched_count"] = prepare_summary.get("source_required_response_fetched_count")
                    report["citadel_capture_source_required_response_replayable_count"] = prepare_summary.get("source_required_response_replayable_count")
                    report["citadel_capture_required_response_paths"] = prepare_summary.get("required_response_paths")
                    report["citadel_capture_required_response_path_counts"] = prepare_summary.get("required_response_path_counts")
                    report["citadel_capture_required_response_path_progress"] = prepare_summary.get("required_response_path_progress")
                    report["citadel_capture_response_body_requirements"] = prepare_summary.get("response_body_requirements")
                    report["citadel_capture_response_body_requirement_role_counts"] = prepare_summary.get("response_body_requirement_role_counts")
                    report["citadel_capture_captured_response_body_requirement_counts"] = prepare_summary.get("captured_response_body_requirement_counts")
                    report["citadel_capture_response_body_requirement_role_capture_counts"] = prepare_summary.get("response_body_requirement_role_capture_counts")
                    report["citadel_capture_missing_response_body_requirements"] = prepare_summary.get("missing_response_body_requirements")
                    report["citadel_capture_missing_response_body_requirement_count"] = prepare_summary.get("missing_response_body_requirement_count")
                    report["citadel_capture_captured_required_response_paths"] = prepare_summary.get("captured_required_response_paths")
                    report["citadel_capture_captured_required_response_path_counts"] = prepare_summary.get("captured_required_response_path_counts")
                    report["citadel_capture_missing_required_response_paths"] = prepare_summary.get("missing_required_response_paths")
                    report["citadel_capture_missing_required_response_path_counts"] = prepare_summary.get("missing_required_response_path_counts")
                    report["citadel_capture_statement_candidate_count"] = prepare_summary.get("statement_candidate_count")
                    report["citadel_capture_replayable_document_payload_count"] = prepare_summary.get("replayable_document_payload_count")
                    report["citadel_capture_target_month"] = prepare_summary.get("target_month")
                    report["citadel_capture_target_month_replayable_document_available"] = prepare_summary.get("target_month_replayable_document_available")
                    report["citadel_capture_target_month_replayable_document_payload_count"] = prepare_summary.get("target_month_replayable_document_payload_count")
                    report["citadel_capture_replayable_statement_months"] = prepare_summary.get("replayable_statement_months")
                    report["citadel_capture_statement_document_months"] = prepare_summary.get("statement_document_months")
                    report["citadel_capture_direct_pdf_response_count"] = prepare_summary.get("direct_pdf_response_count")
                    report["citadel_capture_har_path"] = prepare_summary.get("capture_har_path")
                    report["citadel_capture_error_count"] = prepare_summary.get("error_count")
                summary = result.get("report_summary") or {}
                if isinstance(summary, dict):
                    report["citadel_report_status"] = summary.get("status")
                    report["citadel_downloaded_count"] = summary.get("downloaded_count")
                    report["citadel_skipped_count"] = summary.get("skipped_count")
                    report["citadel_target_month"] = summary.get("target_month")
                    report["citadel_target_month_statement_available"] = summary.get("target_month_statement_available")
                    report["citadel_target_month_existing_count"] = summary.get("target_month_existing_count")
                    report["citadel_target_month_downloaded_count"] = summary.get("target_month_downloaded_count")
                    report["citadel_target_month_skipped_count"] = summary.get("target_month_skipped_count")
                    report["citadel_existing_target_month_files"] = summary.get("existing_target_month_files")
                    report["citadel_downloaded_target_month_files"] = summary.get("downloaded_target_month_files")
                    report["citadel_skipped_target_month_files"] = summary.get("skipped_target_month_files")
                    report["citadel_safe_to_run_automatically"] = summary.get("safe_to_run_automatically")
                    report["citadel_idempotent_replay_safe"] = summary.get("idempotent_replay_safe")
                    report["citadel_copy_plan_safe_to_apply_automatically"] = summary.get(
                        "copy_plan_safe_to_apply_automatically"
                    )
                    report["citadel_har_replay_ready_to_run_automatically"] = summary.get(
                        "har_replay_ready_to_run_automatically"
                    )
                    report["citadel_automation_readiness_status"] = summary.get("automation_readiness_status")
                    report["citadel_automation_blockers"] = summary.get("automation_blockers")
                    report["citadel_idempotent_skip"] = summary.get("idempotent_skip")
                    report["citadel_idempotent_skip_reason"] = summary.get("idempotent_skip_reason")
                    report["citadel_idempotent_skip_latest_live_auth_attention"] = summary.get(
                        "idempotent_skip_latest_live_auth_attention"
                    )
                    report["citadel_latest_live_auth_report"] = summary.get("latest_live_auth_report")
                    report["citadel_latest_live_auth_status"] = summary.get("latest_live_auth_status")
                    report["citadel_latest_live_auth_credentials_available"] = summary.get(
                        "latest_live_auth_credentials_available"
                    )
                    report["citadel_latest_live_auth_manual_auth_required"] = summary.get(
                        "latest_live_auth_manual_auth_required"
                    )
                    report["citadel_latest_live_auth_manual_auth_reason"] = summary.get(
                        "latest_live_auth_manual_auth_reason"
                    )
                    report["citadel_latest_live_auth_manual_auth_file"] = summary.get(
                        "latest_live_auth_manual_auth_file"
                    )
                    report["citadel_latest_live_auth_manual_auth_portal_url"] = summary.get(
                        "latest_live_auth_manual_auth_portal_url"
                    )
                    report["citadel_latest_live_auth_auth_failure_reason"] = summary.get(
                        "latest_live_auth_auth_failure_reason"
                    )
                    report["citadel_latest_live_auth_auth_failure_visible_reason"] = summary.get(
                        "latest_live_auth_auth_failure_visible_reason"
                    )
                    report["citadel_latest_live_auth_auth_visible_error"] = summary.get(
                        "latest_live_auth_auth_visible_error"
                    )
                    report["citadel_latest_live_auth_login_form_last_result"] = summary.get(
                        "latest_live_auth_login_form_last_result"
                    )
                    report["citadel_latest_live_auth_oauth_password_grant_failure_count"] = summary.get(
                        "latest_live_auth_oauth_password_grant_failure_count"
                    )
                    report["citadel_latest_live_auth_oauth_password_grant_error_codes"] = summary.get(
                        "latest_live_auth_oauth_password_grant_error_codes"
                    )
                    report["citadel_latest_live_auth_login_form_submitted"] = summary.get(
                        "latest_live_auth_login_form_submitted"
                    )
                    report["citadel_error_count"] = summary.get("error_count")
                    report["citadel_warning_count"] = summary.get("warning_count")
                    report["citadel_auth_state"] = summary.get("auth_state")
                    report["citadel_credentials_available"] = summary.get("credentials_available")
                    report["citadel_login_mode"] = summary.get("login_mode")
                    report["citadel_otp_required"] = summary.get("otp_required")
                    report["citadel_otp_wait_ms"] = summary.get("otp_wait_ms")
                    report["citadel_otp_file"] = summary.get("otp_file")
                    report["citadel_otp_required_file"] = summary.get("otp_required_file")
                    report["citadel_otp_next_command"] = summary.get("otp_next_command")
                    report["citadel_manual_auth_required"] = summary.get("manual_auth_required")
                    report["citadel_manual_auth_reason"] = summary.get("manual_auth_reason")
                    report["citadel_manual_auth_file"] = summary.get("manual_auth_file")
                    report["citadel_manual_auth_portal_url"] = summary.get("manual_auth_portal_url")
                    report["citadel_auth_failure_reason"] = summary.get("auth_failure_reason")
                    report["citadel_auth_failure_visible_reason"] = summary.get("auth_failure_visible_reason")
                    report["citadel_auth_visible_error"] = summary.get("auth_visible_error")
                    report["citadel_credential_state_drift_suspected"] = summary.get("credential_state_drift_suspected")
                    report["citadel_credential_state_drift_checked"] = summary.get("credential_state_drift_checked")
                    report["citadel_credential_state_drift_basis"] = summary.get("credential_state_drift_basis")
                    report["citadel_login_form_last_result"] = summary.get("login_form_last_result")
                    report["citadel_oauth_password_grant_failure_count"] = summary.get("oauth_password_grant_failure_count")
                    report["citadel_oauth_password_grant_error_codes"] = summary.get("oauth_password_grant_error_codes")
                    report["citadel_browser_storage_bearer_token_available"] = summary.get("browser_storage_bearer_token_available")
                    report["citadel_browser_storage_mobile_source_id_available"] = summary.get("browser_storage_mobile_source_id_available")
                    report["citadel_browser_storage_token_candidate_count"] = summary.get("browser_storage_token_candidate_count")
                    report["citadel_api_header_mobile_source_id_available"] = summary.get("api_header_mobile_source_id_available")
                    report["citadel_api_header_authorization_enabled"] = summary.get("api_header_authorization_enabled")
                    report["citadel_direct_auth_status"] = summary.get("direct_auth_status")
                    report["citadel_direct_auth_transport"] = summary.get("direct_auth_transport")
                    report["citadel_direct_browser_fallback_attempted"] = summary.get("direct_browser_fallback_attempted")
                    report["citadel_direct_browser_fallback_authenticated_found"] = summary.get("direct_browser_fallback_authenticated_found")
                    report["citadel_direct_fresh_mfa_source_status"] = summary.get("direct_fresh_mfa_source_status")
                    report["citadel_direct_fresh_recaptcha_token_available"] = summary.get("direct_fresh_recaptcha_token_available")
                    report["citadel_direct_fresh_recaptcha_token_length"] = summary.get("direct_fresh_recaptcha_token_length")
                    report["citadel_direct_recaptcha_eval_stage"] = summary.get("direct_recaptcha_eval_stage")
                    report["citadel_direct_recaptcha_eval_error"] = summary.get("direct_recaptcha_eval_error")
                    report["citadel_direct_recaptcha_token_action"] = summary.get("direct_recaptcha_token_action")
                    report["citadel_direct_recaptcha_action_errors"] = summary.get("direct_recaptcha_action_errors")
                    report["citadel_direct_mfa_process_id_header_available"] = summary.get("direct_mfa_process_id_header_available")
                    report["citadel_direct_mfa_detail_available"] = summary.get("direct_mfa_detail_available")
                    report["citadel_direct_mfa_request_uuid_available"] = summary.get("direct_mfa_request_uuid_available")
                    report["citadel_direct_mfa_request_uuid_source"] = summary.get("direct_mfa_request_uuid_source")
                    report["citadel_direct_no_mfa_handoff_after_password"] = summary.get("direct_no_mfa_handoff_after_password")
                    report["citadel_direct_no_mfa_handoff_reason"] = summary.get("direct_no_mfa_handoff_reason")
                    report["citadel_direct_otp_send_via_type"] = summary.get("direct_otp_send_via_type")
                    report["citadel_direct_otp_request_status"] = summary.get("direct_otp_request_status")
                    report["citadel_direct_otp_request_shape_matches_har_success"] = summary.get(
                        "direct_otp_request_shape_matches_har_success"
                    )
                    report["citadel_direct_cdp_mfa_process_id_header_available"] = summary.get("direct_cdp_mfa_process_id_header_available")
                    report["citadel_direct_oauth_cdp_event_count"] = summary.get("direct_oauth_cdp_event_count")
                    report["citadel_direct_oauth_cdp_last_status"] = summary.get("direct_oauth_cdp_last_status")
                    report["citadel_direct_password_token_request_shape_matches_har_success"] = summary.get("direct_password_token_request_shape_matches_har_success")
                    report["citadel_direct_password_token_request_shape_matches_har_failure"] = summary.get("direct_password_token_request_shape_matches_har_failure")
                    report["citadel_direct_password_token_request_shape_matched_har_statuses"] = summary.get("direct_password_token_request_shape_matched_har_statuses")
                    report["citadel_direct_password_token_request_structure_matches_har_success"] = summary.get("direct_password_token_request_structure_matches_har_success")
                    report["citadel_direct_password_token_request_structure_matches_har_failure"] = summary.get("direct_password_token_request_structure_matches_har_failure")
                    report["citadel_direct_password_token_request_structure_matched_har_statuses"] = summary.get("direct_password_token_request_structure_matched_har_statuses")
                    report["citadel_direct_error_codes"] = summary.get("direct_error_codes")
                    report["citadel_direct_error_categories"] = summary.get("direct_error_categories")
                    report["citadel_credential_item_name"] = summary.get("credential_item_name")
                    report["citadel_credential_item_uri_hosts"] = summary.get("credential_item_uri_hosts")
                    report["citadel_credential_item_portal_host_match"] = summary.get("credential_item_portal_host_match")
                    report["citadel_credential_item_field_names"] = summary.get("credential_item_field_names")
                    report["citadel_credential_item_notes_len"] = summary.get("credential_item_notes_len")
                    report["citadel_credential_login_hint_configured"] = summary.get("credential_login_hint_configured")
                    report["citadel_credential_username_matches_login_hint"] = summary.get("credential_username_matches_login_hint")
                    report["citadel_credential_item_name_matches_login_hint"] = summary.get("credential_item_name_matches_login_hint")
                    report["citadel_credential_field_matches_login_hint"] = summary.get("credential_field_matches_login_hint")
                    report["citadel_credential_username_len"] = summary.get("credential_username_len")
                    report["citadel_credential_password_len"] = summary.get("credential_password_len")
                    report["citadel_credential_username_has_at"] = summary.get("credential_username_has_at")
                    report["citadel_har_auth_diagnostics_enabled"] = summary.get("har_auth_diagnostics_enabled")
                    report["citadel_har_auth_diagnostics_skipped_reason"] = summary.get("har_auth_diagnostics_skipped_reason")
                    report["citadel_har_token_credential_match_count"] = summary.get("har_token_credential_match_count")
                    report["citadel_har_successful_password_token_match"] = summary.get("har_successful_password_token_match")
                    report["citadel_har_mfa_process_id_header_count"] = summary.get("har_mfa_process_id_header_count")
                    report["citadel_har_token_attempt_statuses"] = summary.get("har_token_attempt_statuses")
                    report["citadel_har_workflow_embedded_response_body_count"] = summary.get("har_workflow_embedded_response_body_count")
                    report["citadel_har_workflow_replayable_json_response_count"] = summary.get("har_workflow_replayable_json_response_count")
                    report["citadel_har_workflow_replayable_document_payload_count"] = summary.get("har_workflow_replayable_document_payload_count")
                    report["citadel_har_workflow_target_month"] = summary.get("har_workflow_target_month")
                    report["citadel_har_workflow_target_month_replayable_document_available"] = summary.get("har_workflow_target_month_replayable_document_available")
                    report["citadel_har_workflow_target_month_replayable_document_payload_count"] = summary.get("har_workflow_target_month_replayable_document_payload_count")
                    report["citadel_har_workflow_replayable_statement_months"] = summary.get("har_workflow_replayable_statement_months")
                    report["citadel_har_workflow_statement_document_months"] = summary.get("har_workflow_statement_document_months")
                    report["citadel_har_workflow_direct_pdf_response_count"] = summary.get("har_workflow_direct_pdf_response_count")
                    report["citadel_har_workflow_source_direct_pdf_candidate_count"] = summary.get("har_workflow_source_direct_pdf_candidate_count")
                    report["citadel_har_workflow_source_direct_pdf_path_counts"] = summary.get("har_workflow_source_direct_pdf_path_counts")
                    report["citadel_har_workflow_source_direct_pdf_filenames"] = summary.get("har_workflow_source_direct_pdf_filenames")
                    report["citadel_har_workflow_source_direct_pdf_filename_candidates"] = summary.get("har_workflow_source_direct_pdf_filename_candidates")
                    report["citadel_har_workflow_target_month_direct_pdf_filenames"] = summary.get("har_workflow_target_month_direct_pdf_filenames")
                    report["citadel_har_workflow_target_month_direct_pdf_filename_candidates"] = summary.get("har_workflow_target_month_direct_pdf_filename_candidates")
                    report["citadel_har_workflow_target_month_direct_pdf_body_missing_candidate_count"] = summary.get("har_workflow_target_month_direct_pdf_body_missing_candidate_count")
                    report["citadel_har_workflow_source_required_response_candidate_count"] = summary.get("har_workflow_source_required_response_candidate_count")
                    report["citadel_har_workflow_source_required_response_path_counts"] = summary.get("har_workflow_source_required_response_path_counts")
                    report["citadel_har_workflow_direct_pdf_missing_response_count"] = summary.get("har_workflow_direct_pdf_missing_response_count")
                    report["citadel_har_workflow_direct_pdf_missing_response_paths"] = summary.get("har_workflow_direct_pdf_missing_response_paths")
                    report["citadel_har_workflow_capture_quality_status"] = summary.get("har_workflow_capture_quality_status")
                    report["citadel_har_workflow_replay_blocker"] = summary.get("har_workflow_replay_blocker")
                    report["citadel_har_workflow_missing_response_body_count"] = summary.get("har_workflow_missing_response_body_count")
                    report["citadel_har_workflow_missing_response_body_paths"] = summary.get("har_workflow_missing_response_body_paths")
                    report["citadel_har_workflow_missing_response_body_path_counts"] = summary.get("har_workflow_missing_response_body_path_counts")
                    report["citadel_har_workflow_response_body_requirements"] = summary.get("har_workflow_response_body_requirements")
                    report["citadel_har_workflow_embedded_access_token_count"] = summary.get("har_workflow_embedded_access_token_count")
                    report["citadel_har_workflow_can_replay_documents"] = summary.get("har_workflow_can_replay_documents")
                    report["citadel_har_workflow_next_action_status"] = summary.get("har_workflow_next_action_status")
                    report["citadel_har_workflow_next_action_reason"] = summary.get("har_workflow_next_action_reason")
                    report["citadel_har_workflow_next_action_command"] = summary.get("har_workflow_next_action_command")
                    report["citadel_har_workflow_next_action_capture_command"] = summary.get("har_workflow_next_action_capture_command")
                    report["citadel_har_workflow_next_action_capture_required"] = summary.get("har_workflow_next_action_capture_required")
                    report["citadel_har_workflow_next_action_required_response_paths"] = summary.get("har_workflow_next_action_required_response_paths")
                    report["citadel_har_workflow_next_action_response_body_requirements"] = summary.get("har_workflow_next_action_response_body_requirements")
                    report["citadel_har_workflow_next_action_source_direct_pdf_candidate_count"] = summary.get("har_workflow_next_action_source_direct_pdf_candidate_count")
                    report["citadel_har_workflow_next_action_source_direct_pdf_path_counts"] = summary.get("har_workflow_next_action_source_direct_pdf_path_counts")
                    report["citadel_har_workflow_next_action_source_direct_pdf_filenames"] = summary.get("har_workflow_next_action_source_direct_pdf_filenames")
                    report["citadel_har_workflow_next_action_target_month_direct_pdf_filenames"] = summary.get("har_workflow_next_action_target_month_direct_pdf_filenames")
                    report["citadel_har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count"] = summary.get("har_workflow_next_action_target_month_direct_pdf_body_missing_candidate_count")
                    report["citadel_har_workflow_next_action_source_required_response_candidate_count"] = summary.get("har_workflow_next_action_source_required_response_candidate_count")
                    report["citadel_har_workflow_next_action_source_required_response_path_counts"] = summary.get("har_workflow_next_action_source_required_response_path_counts")
                    report["citadel_har_workflow_next_action_install_verified_capture_report"] = summary.get("har_workflow_next_action_install_verified_capture_report")
                    report["citadel_har_workflow_next_action_install_verified_capture_dry_run_command"] = summary.get("har_workflow_next_action_install_verified_capture_dry_run_command")
                    report["citadel_har_workflow_next_action_install_verified_capture_apply_command"] = summary.get("har_workflow_next_action_install_verified_capture_apply_command")
                    report["citadel_har_workflow_next_action_install_verified_capture_direct_dry_run_command"] = summary.get("har_workflow_next_action_install_verified_capture_direct_dry_run_command")
                    report["citadel_har_workflow_next_action_install_verified_capture_direct_apply_command"] = summary.get("har_workflow_next_action_install_verified_capture_direct_apply_command")
                    report["citadel_install_verified_capture_report"] = summary.get("install_verified_capture_report")
                    report["citadel_install_verified_capture_apply_command"] = summary.get("install_verified_capture_apply_command")
                    report["citadel_install_verified_capture_direct_apply_command"] = summary.get("install_verified_capture_direct_apply_command")
                    report["citadel_tab_scan_candidate_count"] = summary.get("tab_scan_candidate_count")
                    report["citadel_tab_scan_limit"] = summary.get("tab_scan_limit")
                    report["citadel_tab_scan_scanned_count"] = summary.get("tab_scan_scanned_count")
                    report["citadel_tab_scan_skipped_count"] = summary.get("tab_scan_skipped_count")
                    report["citadel_tab_scan_fetch_timeout_ms"] = summary.get("tab_scan_fetch_timeout_ms")
                    report["citadel_tab_scan_target_id_requested"] = summary.get("tab_scan_target_id_requested")
                    report["citadel_tab_scan_target_id_found"] = summary.get("tab_scan_target_id_found")
                    report["citadel_tab_scan_authenticated_found"] = summary.get("tab_scan_authenticated_found")
                    report["citadel_tab_scan_direct_fallback_target_selected"] = summary.get("tab_scan_direct_fallback_target_selected")
                    report["citadel_tab_scan_direct_fallback_target_id_requested"] = summary.get("tab_scan_direct_fallback_target_id_requested")
                    report["citadel_tab_scan_direct_fallback_target_id_found"] = summary.get("tab_scan_direct_fallback_target_id_found")
                    report["citadel_oauth_network_event_count"] = summary.get("oauth_network_event_count")
                    report["citadel_oauth_network_statuses"] = summary.get("oauth_network_statuses")
                    report["citadel_oauth_network_error_codes"] = summary.get("oauth_network_error_codes")
                    report["citadel_oauth_network_request_shape_matched_har_statuses"] = summary.get("oauth_network_request_shape_matched_har_statuses")
                    report["citadel_oauth_network_request_structure_matched_har_statuses"] = summary.get("oauth_network_request_structure_matched_har_statuses")
                credential_verified = (
                    report.get("citadel_prepare_successful_password_request_found") is True
                    and report.get("citadel_prepare_username_matches_har") is True
                    and (
                        report.get("citadel_prepare_password_matched_before_update") is True
                        or report.get("citadel_prepare_password_updated") is True
                    )
                )
                fresh_mfa_ready = (
                    report.get("citadel_direct_fresh_mfa_source_status") == "ready"
                    or report.get("citadel_direct_fresh_recaptcha_token_available") is True
                )
                effective_auth_status = report.get("citadel_report_status")
                effective_auth_reason = report.get("citadel_auth_failure_reason")
                effective_auth_visible_reason = report.get("citadel_auth_failure_visible_reason")
                if report.get("citadel_latest_live_auth_status"):
                    effective_auth_status = report.get("citadel_latest_live_auth_status")
                    effective_auth_reason = (
                        report.get("citadel_latest_live_auth_auth_failure_reason") or effective_auth_reason
                    )
                    effective_auth_visible_reason = (
                        report.get("citadel_latest_live_auth_auth_failure_visible_reason")
                        or effective_auth_visible_reason
                    )
                otp_request_matches_har_but_rejected = (
                    effective_auth_reason == "otp_request_failed"
                    and report.get("citadel_direct_auth_status") == "otp_request_failed"
                    and report.get("citadel_direct_otp_request_shape_matches_har_success") is True
                    and int(report.get("citadel_direct_otp_request_status") or 0) >= 400
                )
                if (
                    effective_auth_status == "auth_failed"
                    and effective_auth_reason == "credential_rejected_before_mfa"
                    and credential_verified
                    and (
                        fresh_mfa_ready
                        or effective_auth_visible_reason == "login_not_recognized"
                    )
                ):
                    report["citadel_auth_retryable_portal_failure"] = True
                    report["citadel_auth_retryable_reason"] = (
                        "credential_verified_against_successful_har_but_portal_rejected_before_mfa"
                    )
                elif (
                    effective_auth_status == "auth_failed"
                    and otp_request_matches_har_but_rejected
                ):
                    report["citadel_auth_retryable_portal_failure"] = True
                    report["citadel_auth_retryable_reason"] = (
                        "otp_request_matches_successful_har_but_portal_rejected"
                    )
            if result.get("enabled") and result.get("co_owner_paid_mortgage") and int(result.get("rc") or 0) != 0:
                report["failed_count"] += 1
        update_expected_target_month_summary(report)
        report["status"] = (
            "ok" if report["failed_count"] == 0 and report["target_month_statement_gap_count"] == 0 else "review"
        )
        report["current_cycle_statement_ready_count"] = report["target_month_statement_available_count"]
        report["current_cycle_statement_blocker_count"] = report["target_month_statement_gap_count"]
        report["current_cycle_statement_blocker_properties"] = report["target_month_statement_gap_properties"]
        report["current_cycle_statement_ready"] = (
            report["eligible_count"] > 0
            and report["target_month_statement_gap_count"] == 0
            and report["target_month_statement_available_count"] == report["eligible_count"]
        )
        report["current_cycle_future_automation_attention_required"] = report["automation_attention_count"] > 0
        report["current_cycle_future_automation_attention_properties"] = report["automation_attention_properties"]
        report["current_cycle_future_automation_attention_reasons"] = report["automation_attention_reasons"]
        report["safe_to_run_automatically"] = (
            report["status"] == "ok"
            and report["eligible_count"] > 0
            and report["unsafe_downloader_count"] == 0
            and report["automation_attention_count"] == 0
        )
    except Exception as exc:
        report["status"] = "error"
        report["failed_count"] = 1
        report["error"] = str(exc)
    finally:
        sync_citadel_downstream_aliases(report)
        report["ended_at"] = utc_now()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = write_json_report(report_path, report)

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
