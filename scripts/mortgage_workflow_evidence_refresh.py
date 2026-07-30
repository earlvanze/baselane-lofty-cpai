#!/usr/bin/env python3
"""Refresh mortgage HAR evidence analyses and downloader scaffolds offline."""

from __future__ import annotations

import argparse
import json
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
import mortgage_workflow_downloader_scaffold as scaffold
import mortgage_workflow_evidence_handoff as handoff
import mortgage_workflow_har_intake_analyzer as analyzer
from stable_json_report import stable_report_digest, write_json_report

DEFAULT_INTAKE = WORKSPACE_ROOT / "config" / "mortgage_downloader_intake.json"
DEFAULT_REPORT_DIR = WORKSPACE_ROOT / "reports"
DEFAULT_REPORT = DEFAULT_REPORT_DIR / "mortgage_workflow_evidence_refresh_report.json"
DEFAULT_COVERAGE_REPORT_NAME = "mortgage_downloader_coverage_report.json"
DEFAULT_SCAFFOLD_DIR = DEFAULT_REPORT_DIR / "mortgage_downloader_scaffolds"
DEFAULT_STUB_DIR = WORKSPACE_ROOT / "scripts" / "generated_mortgage_downloaders"


def load_intake(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    properties = data.get("properties") if isinstance(data, dict) else None
    if not isinstance(properties, list):
        raise ValueError("intake properties is not a list")
    return [item for item in properties if isinstance(item, dict)]


def same_path(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return left.resolve() == right.resolve()
    except Exception:
        return False


def evidence_paths(item: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for value in item.get("workflow_evidence") or []:
        text = str(value or "").strip()
        if text:
            paths.append(Path(text))
    return paths


def first_existing_evidence_path(item: dict[str, Any]) -> Path | None:
    for path in evidence_paths(item):
        if path.exists():
            return path
    return None


def selected_existing_evidence_analysis(
    item: dict[str, Any],
    prop: str,
    *,
    analyses: list[dict[str, Any]] | None = None,
) -> tuple[Path | None, str, list[dict[str, Any]]]:
    if analyses is None:
        analyses = analyzer.analyze_workflow_evidence(item, property_name=prop)
    selected = analyzer.select_workflow_evidence_analysis(analyses)
    if not selected:
        return None, "", analyses
    path_text = str(selected.get("har_path") or "").strip()
    if not path_text:
        return None, "", analyses
    existing_count = sum(1 for item in analyses if item.get("har_path_exists") is True)
    reason = str(selected.get("har_selection_reason") or "workflow_evidence")
    if existing_count > 1:
        reason = "best_existing_workflow_evidence"
    else:
        reason = "first_existing_workflow_evidence"
    return Path(path_text), reason, analyses


def suggested_har_path(item: dict[str, Any], har_root: Path) -> Path | None:
    prop = str(item.get("property") or "").strip()
    if not prop:
        return None
    return Path(coverage.suggested_workflow_har_path(prop, item.get("portal_url"), har_root))


def compact_slug(value: str) -> str:
    return value.replace("-", "")


def property_core_slug(property_name: str) -> str:
    tokens = [token for token in coverage.slugify(property_name).split("-") if token]
    suffixes = {"ave", "avenue", "st", "street", "rd", "road", "ln", "lane", "pl", "place", "dr", "drive"}
    while tokens and tokens[-1] in suffixes:
        tokens.pop()
    return "-".join(tokens)


def stale_scaffold_artifact_paths(
    *,
    slug: str,
    report_dir: Path,
    scaffold_dir: Path,
    stub_dir: Path,
) -> list[Path]:
    _ = stub_dir
    return [
        report_dir / f"mortgage_workflow_downloader_scaffold_{slug}.json",
        scaffold_dir / f"{slug}_downloader_scaffold_manifest.json",
        scaffold_dir / f"{slug}_mortgage_statement_downloader_registry_entry.json",
    ]


def remove_stale_scaffold_artifacts(
    *,
    slug: str,
    report_dir: Path,
    scaffold_dir: Path,
    stub_dir: Path,
) -> list[str]:
    removed: list[str] = []
    for path in stale_scaffold_artifact_paths(
        slug=slug,
        report_dir=report_dir,
        scaffold_dir=scaffold_dir,
        stub_dir=stub_dir,
    ):
        try:
            if path.exists() and path.is_file():
                path.unlink()
                removed.append(str(path))
        except FileNotFoundError:
            continue
    return removed


def match_terms(item: dict[str, Any]) -> dict[str, str]:
    prop = str(item.get("property") or "").strip()
    portal_host = coverage.portal_host(item.get("portal_url"))
    portal_site = portal_host.split(".")[0]
    servicer = str(item.get("servicer_hint") or "").strip()
    terms = {
        "property_slug": coverage.slugify(prop),
        "portal_host_slug": coverage.slugify(portal_host),
        "portal_site_slug": coverage.slugify(portal_site),
        "servicer_hint_slug": coverage.slugify(servicer),
        "property_core_slug": property_core_slug(prop),
    }
    return {key: value for key, value in terms.items() if value and value != "unknown"}


def har_candidate_match(
    item: dict[str, Any],
    path: Path,
    suggested: Path | None,
    *,
    intake_path: Path,
) -> dict[str, Any] | None:
    stem_slug = coverage.slugify(path.stem)
    stem_compact = compact_slug(stem_slug)
    terms = match_terms(item)
    reasons: list[str] = []
    score = 0

    if suggested and (same_path(path, suggested) or path.name == suggested.name):
        reasons.append("suggested_filename")
        score += 100

    prop_slug = terms.get("property_slug")
    if prop_slug and (prop_slug in stem_slug or compact_slug(prop_slug) in stem_compact):
        reasons.append("property_slug")
        score += 70

    prop_core_slug = terms.get("property_core_slug")
    if (
        prop_core_slug
        and prop_core_slug != prop_slug
        and len(compact_slug(prop_core_slug)) >= 4
        and (prop_core_slug in stem_slug or compact_slug(prop_core_slug) in stem_compact)
    ):
        reasons.append("property_core_slug")
        score += 50

    for reason, term, points in (
        ("portal_host_slug", terms.get("portal_host_slug"), 60),
        ("portal_site_slug", terms.get("portal_site_slug"), 55),
        ("servicer_hint_slug", terms.get("servicer_hint_slug"), 35),
    ):
        if not term or len(term) < 5:
            continue
        if term in stem_slug or compact_slug(term) in stem_compact:
            reasons.append(reason)
            score += points

    if not reasons:
        return None
    return {
        "path": str(path),
        "name": path.name,
        "size": path.stat().st_size if path.exists() else None,
        "match_score": score,
        "match_reasons": sorted(set(reasons)),
        "register_workflow_evidence_command": register_workflow_evidence_command(
            str(item.get("property") or ""),
            path,
            intake_path=intake_path,
            apply=False,
        ),
        "register_workflow_evidence_apply_command": register_workflow_evidence_command(
            str(item.get("property") or ""),
            path,
            intake_path=intake_path,
            apply=True,
        ),
        "advance_workflow_evidence_command": advance_workflow_evidence_command(
            str(item.get("property") or ""),
            path,
            intake_path=intake_path,
            apply=False,
        ),
        "advance_workflow_evidence_apply_command": advance_workflow_evidence_command(
            str(item.get("property") or ""),
            path,
            intake_path=intake_path,
            apply=True,
        ),
    }


def discover_har_candidates(item: dict[str, Any], har_root: Path, *, intake_path: Path) -> list[dict[str, Any]]:
    if not har_root.exists() or not har_root.is_dir():
        return []
    suggested = suggested_har_path(item, har_root)
    registered_evidence_paths = evidence_paths(item)
    candidates: list[dict[str, Any]] = []
    for path in sorted(har_root.glob("*.har")):
        if path.name.startswith("."):
            continue
        if not path.is_file():
            continue
        if any(same_path(path, registered_path) for registered_path in registered_evidence_paths):
            continue
        match = har_candidate_match(item, path, suggested, intake_path=intake_path)
        if match:
            analysis = analyzer.analyze_har(
                path,
                property_name=str(item.get("property") or ""),
                item=item,
                selection_reason="discovered_har_candidate",
            )
            actionable_missing_count = int(analysis.get("actionable_missing_response_body_count") or 0)
            candidate_endpoint_count = int(analysis.get("candidate_endpoint_count") or 0)
            has_workflow_signal = analysis.get("status") == "ok" or actionable_missing_count > 0
            match.update(
                {
                    "analysis_status": analysis.get("status"),
                    "analysis_reason": analysis.get("reason"),
                    "analysis_suggested_next_action": analysis.get("suggested_next_action"),
                    "candidate_endpoint_count": candidate_endpoint_count,
                    "actionable_missing_response_body_count": actionable_missing_count,
                    "missing_response_body_paths": analysis.get("missing_response_body_paths"),
                    "safe_workflow_candidate": bool(has_workflow_signal),
                    "rejected_reason": None if has_workflow_signal else (
                        analysis.get("reason") or "statement_workflow_endpoints_not_identified"
                    ),
                }
            )
            if not has_workflow_signal:
                for command_key in [
                    "register_workflow_evidence_command",
                    "register_workflow_evidence_apply_command",
                    "advance_workflow_evidence_command",
                    "advance_workflow_evidence_apply_command",
                ]:
                    match.pop(command_key, None)
                match["command_status"] = "rejected_candidate_not_runnable"
            candidates.append(match)
    candidates.sort(key=lambda item: (-int(item.get("match_score") or 0), str(item.get("name") or "")))
    workflow_candidates = [item for item in candidates if item.get("safe_workflow_candidate")]
    safe = (
        len(workflow_candidates) == 1
        and len(candidates) == 1
        and int(workflow_candidates[0].get("match_score") or 0) >= 55
    )
    for candidate in candidates:
        candidate["safe_unambiguous_candidate"] = safe and candidate is workflow_candidates[0]
    return candidates


def register_workflow_evidence_command(
    property_name: str,
    har_path: Path,
    *,
    intake_path: Path,
    apply: bool,
) -> str:
    command = (
        "python3 scripts/register_mortgage_workflow_evidence.py "
        f"--property {shlex.quote(property_name)} "
        f"--har {shlex.quote(str(har_path))} "
        f"--intake {shlex.quote(str(intake_path))}"
    )
    if apply:
        return f"{command} --apply"
    return command


def advance_workflow_evidence_command(
    property_name: str,
    har_path: Path,
    *,
    intake_path: Path,
    target_month: str | None = None,
    apply: bool,
) -> str:
    command = (
        "python3 scripts/advance_mortgage_workflow_evidence.py "
        f"--property {shlex.quote(property_name)} "
        f"--har {shlex.quote(f'{property_name}={har_path}')} "
        f"--intake {shlex.quote(str(intake_path))} "
        "--write-stubs "
        "--install-registry-entries"
    )
    if target_month:
        command += f" --target-month {shlex.quote(str(target_month))}"
    if apply:
        return f"{command} --apply-evidence --apply-registry"
    return command


def selected_har(
    item: dict[str, Any],
    *,
    har_root: Path,
    discovered_candidates: list[dict[str, Any]],
    use_discovered_hars: bool,
    workflow_evidence_analyses: list[dict[str, Any]] | None = None,
) -> tuple[Path | None, str]:
    selected_existing, existing_reason, _analyses = selected_existing_evidence_analysis(
        item,
        str(item.get("property") or ""),
        analyses=workflow_evidence_analyses,
    )
    if selected_existing:
        return selected_existing, existing_reason
    suggested = suggested_har_path(item, har_root)
    if suggested and suggested.exists():
        return suggested, "suggested_workflow_har_path"
    if use_discovered_hars:
        safe_candidates = [item for item in discovered_candidates if item.get("safe_unambiguous_candidate")]
        if len(safe_candidates) == 1:
            return Path(str(safe_candidates[0]["path"])), "unambiguous_discovered_workflow_har_candidate"
    return suggested, "suggested_workflow_har_path"


def scaffold_command(
    *,
    analysis_report_path: Path,
    scaffold_dir: Path,
    stub_dir: Path,
    scaffold_report_path: Path,
) -> str:
    return (
        "python3 scripts/mortgage_workflow_downloader_scaffold.py "
        f"--analysis-report {shlex.quote(str(analysis_report_path))} "
        f"--output-dir {shlex.quote(str(scaffold_dir))} "
        f"--stub-dir {shlex.quote(str(stub_dir))} "
        "--write-stub "
        f"--report {shlex.quote(str(scaffold_report_path))}"
    )


def registry_install_command(registry_entry_path: object) -> str | None:
    text = str(registry_entry_path or "").strip()
    if not text:
        return None
    return (
        "python3 scripts/register_mortgage_downloader_registry_entry.py "
        f"--entry {shlex.quote(text)} "
        "--config config/mortgage_statement_downloaders.json "
        "--apply"
    )


def generated_downloader_command(stub_path: object, *, apply: bool = False) -> str | None:
    text = str(stub_path or "").strip()
    if not text:
        return None
    command = f"python3 {shlex.quote(text)}"
    if apply:
        return f"MORTGAGE_GENERATED_HAR_DOWNLOADER_APPLY=1 {command}"
    return command


def action_type_for_item(item: dict[str, Any]) -> str | None:
    if not item.get("har_path_exists"):
        return "provide_workflow_har"
    return item.get("next_action")


def action_reason_for_item(item: dict[str, Any]) -> str | None:
    action_type = action_type_for_item(item)
    if action_type == "provide_workflow_har":
        return item.get("analysis_reason") or "har_missing"
    if action_type == "implement_generated_scaffold":
        return "analysis_ready"
    return item.get("analysis_reason") or item.get("required_capture_quality")


def suggested_next_action_for_item(item: dict[str, Any]) -> str | None:
    action_type = action_type_for_item(item)
    if action_type == "provide_workflow_har":
        if item.get("capture_command"):
            return "capture_workflow_har_with_full_response_bodies"
        return item.get("command_precondition_next_action") or "place_har_at_har_path"
    if action_type == "implement_generated_scaffold":
        return "review_and_install_generated_downloader"
    return item.get("next_action") or item.get("analysis_suggested_next_action") or action_type


def load_handoff_index(report_dir: Path) -> dict[str, dict[str, Any]]:
    index_path = report_dir / "mortgage_workflow_evidence_handoff_index.json"
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    handoffs = data.get("handoffs") if isinstance(data, dict) else None
    if not isinstance(handoffs, list):
        return {}
    by_property: dict[str, dict[str, Any]] = {}
    for handoff in handoffs:
        if not isinstance(handoff, dict):
            continue
        prop = str(handoff.get("property") or "").strip()
        if prop:
            by_property[coverage.normalize_property(prop)] = handoff
    return by_property


def load_coverage_action_index(coverage_report: Path) -> dict[str, dict[str, Any]]:
    if not coverage_report.exists():
        return {}
    try:
        data = json.loads(coverage_report.read_text(encoding="utf-8"))
    except Exception:
        return {}
    actions = data.get("next_action_plan") if isinstance(data, dict) else None
    if not isinstance(actions, list):
        return {}
    by_property: dict[str, dict[str, Any]] = {}
    for action in actions:
        if not isinstance(action, dict):
            continue
        prop = str(action.get("property") or "").strip()
        if prop:
            by_property[coverage.normalize_property(prop)] = action
    return by_property


def load_coverage_record_index(coverage_report: Path) -> dict[str, dict[str, Any]]:
    if not coverage_report.exists():
        return {}
    try:
        data = json.loads(coverage_report.read_text(encoding="utf-8"))
    except Exception:
        return {}
    records = data.get("coverage_records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return {}
    by_property: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        prop = str(record.get("property") or "").strip()
        if prop:
            by_property[coverage.normalize_property(prop)] = record
    return by_property


def coverage_record_ready(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    return (
        record.get("coverage_status") == "covered"
        and record.get("operational_status") == "ready"
        and int(record.get("automation_ready_downloader_count") or 0) > 0
    )


def coverage_record_fresh_har_required_path(
    record: dict[str, Any] | None,
    item: dict[str, Any],
    har_root: Path,
) -> Path | None:
    if not record:
        return None
    if record.get("coverage_status") != "missing_downloader":
        return None
    if str(record.get("missing_reason") or "").strip() != "no_enabled_co_owner_paid_downloader_with_existing_script":
        return None
    suggested = suggested_har_path(item, har_root)
    if not suggested:
        return None
    existing_paths = [Path(str(path)) for path in evidence_paths(item)]
    if any(same_path(path, suggested) for path in existing_paths):
        return None
    return suggested


def coverage_capture_precondition_fields(coverage_action: dict[str, Any]) -> dict[str, Any]:
    if not coverage_action:
        return {}
    capture_auth_blocker = coverage_action.get("capture_auth_blocker") is True
    command_status = str(coverage_action.get("command_precondition_status") or "").strip()
    command_reason = coverage_action.get("command_precondition_reason")
    command_next_action = coverage_action.get("command_precondition_next_action")
    if capture_auth_blocker or command_status == "blocked_by_auth":
        return {
            "capture_auth_blocker": True,
            "capture_auth_blocker_reason": coverage_action.get("capture_auth_blocker_reason") or command_reason,
            "capture_auth_next_action": coverage_action.get("capture_auth_next_action") or command_next_action,
            "capture_command_blocked_reason": coverage_action.get("capture_command_blocked_reason")
            or coverage_action.get("capture_auth_blocker_reason")
            or command_reason,
            "capture_command_ready_to_run_now": False,
            "command_precondition_status": "blocked_by_auth",
            "command_precondition_reason": command_reason
            or coverage_action.get("capture_auth_blocker_reason"),
            "command_precondition_next_action": command_next_action
            or coverage_action.get("capture_auth_next_action"),
        }
    fields = {
        "capture_auth_blocker": coverage_action.get("capture_auth_blocker"),
        "capture_auth_blocker_reason": coverage_action.get("capture_auth_blocker_reason"),
        "capture_auth_next_action": coverage_action.get("capture_auth_next_action"),
        "capture_command_blocked_reason": coverage_action.get("capture_command_blocked_reason"),
        "command_precondition_status": coverage_action.get("command_precondition_status"),
        "command_precondition_reason": command_reason,
        "command_precondition_next_action": command_next_action,
    }
    return {key: value for key, value in fields.items() if value not in (None, "", [], {})}


def post_capture_check_fields(
    prop: str,
    *,
    item: dict[str, Any] | None = None,
    analysis_sources: list[dict[str, Any]] | None = None,
    report_dir: Path,
    handoff_index: dict[str, dict[str, Any]],
    coverage_action_index: dict[str, dict[str, Any]],
    target_month_override: str | None = None,
) -> dict[str, Any]:
    handoff_record = handoff_index.get(coverage.normalize_property(prop), {})
    coverage_action = coverage_action_index.get(coverage.normalize_property(prop), {})
    handoff_path = str(handoff_record.get("handoff_path") or coverage.workflow_evidence_handoff_path(prop, report_dir))
    handoff_markdown_path = str(
        handoff_record.get("handoff_markdown_path") or coverage.workflow_evidence_handoff_markdown_path(prop, report_dir)
    )
    check_script_path = str(
        handoff_record.get("post_capture_check_script_path") or coverage.workflow_evidence_check_path(prop, report_dir)
    )
    check_command = str(
        handoff_record.get("post_capture_check_command") or coverage.workflow_evidence_check_command(prop, report_dir)
    )
    post_capture_har_path = str(coverage_action.get("capture_har_path") or "").strip() or None
    post_capture_advance_command = (
        str(coverage_action.get("advance_workflow_evidence_command") or "").strip() or None
    )
    post_capture_advance_apply_command = (
        str(coverage_action.get("advance_workflow_evidence_apply_command") or "").strip() or None
    )
    target_month = target_month_override or handoff_record.get("target_statement_month") or handoff.target_statement_month(item or {})
    post_capture_advance_command = coverage.command_with_target_month(post_capture_advance_command, target_month)
    post_capture_advance_apply_command = coverage.command_with_target_month(post_capture_advance_apply_command, target_month)
    fresh_target_statement_document_metadata_only_details = handoff.aggregate_statement_metadata_details(
        analysis_sources or [],
        target_month=str(target_month or ""),
    )
    target_statement_document_metadata_only_details = (
        fresh_target_statement_document_metadata_only_details
        or handoff_record.get("target_statement_document_metadata_only_details")
    )
    capture_requirements = handoff_record.get("capture_requirements")
    if target_month_override:
        capture_requirements = handoff.capture_requirements_for_handoff(
            target_month=str(target_month),
            analysis=(analysis_sources or [{}])[0],
            target_statement_document_metadata_only_details=target_statement_document_metadata_only_details,
        )
    target_statement_document_capture_instruction = None
    if isinstance(capture_requirements, list):
        target_statement_document_capture_instruction = next(
            (
                str(requirement)
                for requirement in capture_requirements
                if isinstance(requirement, str) and "document_identifier" in requirement
            ),
            None,
        )
    if not target_statement_document_capture_instruction and target_statement_document_metadata_only_details:
        detail = target_statement_document_metadata_only_details[0]
        date = str(detail.get("date") or "").strip()
        identifier = str(detail.get("document_identifier") or "").strip()
        descriptor = str(target_month or "").strip()
        if date:
            descriptor = f"{descriptor} statement dated {date}" if descriptor else f"statement dated {date}"
        if identifier:
            descriptor = f"{descriptor} (document_identifier {identifier})" if descriptor else f"document_identifier {identifier}"
        if descriptor:
            target_statement_document_capture_instruction = (
                f"Authenticate normally and open/download the {descriptor} so the HAR captures the embedded PDF payload."
            )
    capture_command = handoff_record.get("capture_command") or coverage_action.get("capture_command")
    if not capture_command:
        capture_portal_url = handoff_record.get("portal_url") or coverage_action.get("portal_url")
        capture_har_path = handoff_record.get("suggested_workflow_har_path") or coverage_action.get(
            "suggested_workflow_har_path"
        )
        if capture_portal_url and capture_har_path:
            capture_command = coverage.workflow_evidence_capture_command(prop, capture_portal_url, capture_har_path)
    target_document_ids = coverage.string_list(
        coverage_action.get("target_month_document_identifiers")
    ) or coverage.string_list(handoff_record.get("target_month_document_identifiers"))
    if not target_document_ids and isinstance(target_statement_document_metadata_only_details, list):
        for detail in target_statement_document_metadata_only_details:
            if not isinstance(detail, dict):
                continue
            identifier = str(detail.get("document_identifier") or "").strip()
            if identifier and identifier not in target_document_ids:
                target_document_ids.append(identifier)
    capture_command = coverage.capture_command_with_expected_document_ids(capture_command, target_document_ids)
    advance_command = coverage.command_with_target_month(
        handoff_record.get("advance_workflow_evidence_command"),
        target_month,
    )
    advance_apply_command = coverage.command_with_target_month(
        handoff_record.get("advance_workflow_evidence_apply_command"),
        target_month,
    )
    precondition_fields = coverage_capture_precondition_fields(coverage_action)
    capture_command_ready_to_run_now = (
        handoff_record.get("capture_command_ready_to_run_now")
        if "capture_command_ready_to_run_now" in handoff_record
        else bool(capture_command)
    )
    if precondition_fields.get("capture_command_ready_to_run_now") is False:
        capture_command_ready_to_run_now = False
    return {
        "handoff_path": handoff_path,
        "handoff_exists": Path(handoff_path).exists(),
        "handoff_markdown_path": handoff_markdown_path,
        "handoff_markdown_exists": Path(handoff_markdown_path).exists(),
        "suggested_workflow_har_path": handoff_record.get("suggested_workflow_har_path"),
        "suggested_workflow_har_path_exists": handoff_record.get("suggested_workflow_har_path_exists"),
        "suggested_workflow_har_path_size": handoff_record.get("suggested_workflow_har_path_size"),
        "target_statement_month": target_month,
        "target_month_document_identifiers": target_document_ids,
        "expected_document_ids": target_document_ids,
        "target_statement_document_metadata_only_details": target_statement_document_metadata_only_details,
        "target_statement_document_capture_instruction": target_statement_document_capture_instruction,
        "capture_requirements": capture_requirements,
        "register_workflow_evidence_command": handoff_record.get("register_workflow_evidence_command"),
        "register_workflow_evidence_apply_command": handoff_record.get("register_workflow_evidence_apply_command"),
        "register_workflow_evidence_ready_to_run_now": handoff_record.get("register_workflow_evidence_ready_to_run_now"),
        "advance_workflow_evidence_command": advance_command,
        "advance_workflow_evidence_apply_command": advance_apply_command,
        "advance_workflow_evidence_ready_to_run_now": handoff_record.get("advance_workflow_evidence_ready_to_run_now"),
        "post_capture_check_script_path": check_script_path,
        "post_capture_check_script_exists": Path(check_script_path).exists(),
        "post_capture_check_command": check_command,
        "post_capture_check_safe_to_run_after_har_capture": True,
        "post_capture_check_ready_to_run_now": handoff_record.get("post_capture_check_ready_to_run_now"),
        "capture_command": capture_command,
        "capture_command_ready_to_run_now": capture_command_ready_to_run_now,
        "capture_command_safe_to_run_automatically": False,
        "capture_required_before_offline_next_step": handoff_record.get("capture_required_before_offline_next_step"),
        **precondition_fields,
        "post_capture_har_path": post_capture_har_path,
        "post_capture_har_path_exists": Path(post_capture_har_path).exists() if post_capture_har_path else False,
        "post_capture_advance_workflow_evidence_command": post_capture_advance_command,
        "post_capture_advance_workflow_evidence_apply_command": post_capture_advance_apply_command,
        "post_capture_advance_workflow_evidence_ready_after_capture": bool(
            post_capture_har_path and post_capture_advance_command and post_capture_advance_apply_command
        ),
        "post_capture_source": (
            "mortgage_downloader_coverage_next_action" if post_capture_har_path else None
        ),
    }


def refresh_item(
    item: dict[str, Any],
    *,
    intake_path: Path,
    report_dir: Path,
    scaffold_dir: Path,
    stub_dir: Path,
    har_root: Path,
    write_stub: bool,
    handoff_index: dict[str, dict[str, Any]],
    coverage_action_index: dict[str, dict[str, Any]],
    coverage_record_index: dict[str, dict[str, Any]],
    discover_hars: bool,
    use_discovered_hars: bool,
    target_month_override: str | None = None,
) -> dict[str, Any]:
    prop = str(item.get("property") or "").strip()
    slug = coverage.slugify(prop)
    discovered_candidates = (
        discover_har_candidates(item, har_root, intake_path=intake_path)
        if discover_hars
        else []
    )
    safe_discovered_candidates = [
        item for item in discovered_candidates if item.get("safe_unambiguous_candidate")
    ]
    workflow_evidence_analyses = analyzer.analyze_workflow_evidence(item, property_name=prop)
    coverage_record = coverage_record_index.get(coverage.normalize_property(prop))
    fresh_required_har_path = coverage_record_fresh_har_required_path(coverage_record, item, har_root)
    if fresh_required_har_path:
        har_path, selection_reason = fresh_required_har_path, "coverage_suggested_target_month_workflow_har_path"
    else:
        har_path, selection_reason = selected_har(
            item,
            har_root=har_root,
            discovered_candidates=discovered_candidates,
            use_discovered_hars=use_discovered_hars,
            workflow_evidence_analyses=workflow_evidence_analyses,
        )
    analysis_report_path = analyzer.default_report_path(report_dir, prop, har_path)
    analysis = next(
        (
            dict(item)
            for item in workflow_evidence_analyses
            if har_path
            and item.get("har_path")
            and same_path(Path(str(item.get("har_path"))), har_path)
        ),
        None,
    )
    if analysis is None:
        analysis = analyzer.analyze_har(
            har_path,
            property_name=prop,
            item=item,
            selection_reason=selection_reason,
        )
    else:
        analysis["har_selection_reason"] = selection_reason
    analysis["report_path"] = str(analysis_report_path)
    analysis = write_json_report(analysis_report_path, analysis)
    analysis_sources = workflow_evidence_analyses or [analysis]
    statement_document_months = handoff.aggregate_analysis_months(
        analysis_sources,
        "statement_document_months",
    )
    statement_document_metadata_only_months = handoff.aggregate_analysis_months(
        analysis_sources,
        "statement_document_metadata_only_months",
    )
    statement_document_payload_months = handoff.aggregate_analysis_months(
        analysis_sources,
        "statement_document_payload_months",
    )
    target_month = target_month_override or handoff.target_statement_month(item)
    latest_metadata_only_month = handoff.latest_year_month(statement_document_metadata_only_months)
    target_statement_document_metadata_only_details = handoff.aggregate_statement_metadata_details(
        analysis_sources,
        target_month=target_month,
    )
    analysis_reason = analysis.get("reason")
    analysis_suggested_next_action = analysis.get("suggested_next_action")
    required_capture_quality = analysis.get("required_capture_quality")
    if analysis_reason == "candidate_endpoints_missing_response_bodies" and (
        target_statement_document_metadata_only_details or latest_metadata_only_month
    ):
        analysis_reason = "embedded_statement_pdf_payload_missing"
        analysis_suggested_next_action = "capture_target_month_statement"
        required_capture_quality = "target_month_statement_pdf"

    item_report: dict[str, Any] = {
        "property": prop,
        "servicer_hint": item.get("servicer_hint"),
        "portal_url": item.get("portal_url"),
        "har_path": str(har_path) if har_path else None,
        "har_path_exists": bool(har_path and har_path.exists()),
        "har_selection_reason": selection_reason,
        "workflow_evidence_paths": [str(path) for path in evidence_paths(item)],
        "workflow_evidence_path_count": len(evidence_paths(item)),
        "workflow_evidence_analysis_count": len(workflow_evidence_analyses),
        "workflow_evidence_analyses": [
            analyzer.workflow_evidence_analysis_summary(item) for item in workflow_evidence_analyses
        ],
        "har_root": str(har_root),
        "use_discovered_hars": use_discovered_hars,
        "discovered_har_candidate_count": len(discovered_candidates),
        "discovered_har_candidates": discovered_candidates,
        "safe_discovered_har_candidate_count": len(safe_discovered_candidates),
        "safe_discovered_har_candidate_path": (
            safe_discovered_candidates[0].get("path") if len(safe_discovered_candidates) == 1 else None
        ),
        "selected_discovered_har_candidate": selection_reason == "unambiguous_discovered_workflow_har_candidate",
        "analysis_report": str(analysis_report_path),
        "analysis_status": analysis.get("status"),
        "analysis_reason": analysis_reason,
        "analysis_suggested_next_action": analysis_suggested_next_action,
        "candidate_endpoint_count": analysis.get("candidate_endpoint_count"),
        "required_capture_quality": required_capture_quality,
        "actionable_missing_response_body_count": analysis.get("actionable_missing_response_body_count"),
        "missing_response_body_paths": analysis.get("missing_response_body_paths"),
        "missing_response_body_path_counts": analysis.get("missing_response_body_path_counts"),
        "response_body_requirements": analysis.get("response_body_requirements"),
        "statement_document_candidate_count": handoff.aggregate_analysis_counts(
            analysis_sources,
            "statement_document_candidate_count",
        ),
        "statement_document_metadata_only_count": handoff.aggregate_analysis_counts(
            analysis_sources,
            "statement_document_metadata_only_count",
        ),
        "statement_document_payload_count": handoff.aggregate_analysis_counts(
            analysis_sources,
            "statement_document_payload_count",
        ),
        "statement_document_months": statement_document_months,
        "statement_document_metadata_only_months": statement_document_metadata_only_months,
        "statement_document_payload_months": statement_document_payload_months,
        "latest_statement_document_metadata_only_month": latest_metadata_only_month,
        "safe_to_build_downloader_automatically": analysis.get("safe_to_build_downloader_automatically"),
        "scaffold_status": None,
        "scaffold_report": None,
        "manifest_path": None,
        "registry_entry_path": None,
        "registry_entry_written": None,
        "registry_install_command": None,
        "proposed_config_entry": None,
        "safe_to_register_automatically": None,
        "stub_path": None,
        "scaffold_command": None,
        "generated_downloader_dry_run_command": None,
        "generated_downloader_apply_command": None,
        "next_action": analysis_suggested_next_action,
        "next_action_reason": analysis_reason,
        "safe_to_run_automatically": analysis.get("status") == "ok",
        "stale_scaffold_artifacts_removed": [],
    }
    item_report["command_precondition_status"] = (
        "ready" if item_report["har_path_exists"] else "missing_workflow_har"
    )
    item_report["command_precondition_next_action"] = (
        None if item_report["har_path_exists"] else "place_har_at_har_path"
    )
    item_report.update(
        post_capture_check_fields(
            prop,
            item=item,
            analysis_sources=analysis_sources,
            report_dir=report_dir,
            handoff_index=handoff_index,
            coverage_action_index=coverage_action_index,
            target_month_override=target_month_override,
        )
    )
    if har_path:
        item_report.update(
            {
                "register_workflow_evidence_command": register_workflow_evidence_command(
                    prop,
                    har_path,
                    intake_path=intake_path,
                    apply=False,
                ),
                "register_workflow_evidence_apply_command": register_workflow_evidence_command(
                    prop,
                    har_path,
                    intake_path=intake_path,
                    apply=True,
                ),
                "register_workflow_evidence_ready_to_run_now": item_report["har_path_exists"],
                "advance_workflow_evidence_command": advance_workflow_evidence_command(
                    prop,
                    har_path,
                    intake_path=intake_path,
                    target_month=str(item_report.get("target_statement_month") or target_month or "").strip() or None,
                    apply=False,
                ),
                "advance_workflow_evidence_apply_command": advance_workflow_evidence_command(
                    prop,
                    har_path,
                    intake_path=intake_path,
                    target_month=str(item_report.get("target_statement_month") or target_month or "").strip() or None,
                    apply=True,
                ),
                "advance_workflow_evidence_ready_to_run_now": item_report["har_path_exists"],
            }
        )

    if analysis.get("status") != "ok":
        item_report["stale_scaffold_artifacts_removed"] = remove_stale_scaffold_artifacts(
            slug=slug,
            report_dir=report_dir,
            scaffold_dir=scaffold_dir,
            stub_dir=stub_dir,
        )

    if analysis.get("status") == "ok":
        coverage_record = coverage_record_index.get(coverage.normalize_property(prop))
        if coverage_record_ready(coverage_record):
            item_report.update(
                {
                    "next_action": None,
                    "next_action_reason": "existing_ready_downloader_reused",
                    "existing_downloader_count": coverage_record.get("active_downloader_count"),
                    "existing_automation_ready_downloader_count": coverage_record.get(
                        "automation_ready_downloader_count"
                    ),
                    "existing_current_month_statement_available": coverage_record.get(
                        "current_month_statement_available"
                    ),
                    "existing_statement_month_status": coverage_record.get("statement_month_status"),
                    "suggested_next_action": None,
                }
            )
            return item_report
        scaffold_report_path = report_dir / f"mortgage_workflow_downloader_scaffold_{slug}.json"
        scaffold_report = scaffold.build_scaffold(
            analysis_report_path,
            output_dir=scaffold_dir,
            stub_dir=stub_dir,
            write_stub=write_stub,
        )
        scaffold_report_path.write_text(
            json.dumps(scaffold_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        item_report.update(
            {
                "scaffold_status": scaffold_report.get("status"),
                "scaffold_report": str(scaffold_report_path),
                "scaffold_command": scaffold_command(
                    analysis_report_path=analysis_report_path,
                    scaffold_dir=scaffold_dir,
                    stub_dir=stub_dir,
                    scaffold_report_path=scaffold_report_path,
                ),
                "manifest_path": scaffold_report.get("manifest_path"),
                "registry_entry_path": scaffold_report.get("registry_entry_path"),
                "registry_entry_written": scaffold_report.get("registry_entry_written"),
                "registry_install_command": registry_install_command(scaffold_report.get("registry_entry_path")),
                "proposed_config_entry": scaffold_report.get("proposed_config_entry"),
                "safe_to_register_automatically": scaffold_report.get("safe_to_register_automatically"),
                "stub_path": scaffold_report.get("stub_path") if write_stub else None,
                "generated_downloader_dry_run_command": generated_downloader_command(
                    scaffold_report.get("stub_path") if write_stub else None,
                    apply=False,
                ),
                "generated_downloader_apply_command": generated_downloader_command(
                    scaffold_report.get("stub_path") if write_stub else None,
                    apply=True,
                ),
                "next_action": "implement_generated_scaffold",
                "next_action_reason": "analysis_ready",
            }
        )
    item_report["suggested_next_action"] = suggested_next_action_for_item(item_report)
    return item_report


def build_refresh_report(
    intake_path: Path,
    *,
    report_dir: Path,
    scaffold_dir: Path,
    stub_dir: Path,
    har_root: Path,
    write_stub: bool,
    ensure_handoffs: bool,
    discover_hars: bool,
    use_discovered_hars: bool,
    coverage_report: Path | None = None,
    target_month_override: str | None = None,
) -> dict[str, Any]:
    items = load_intake(intake_path)
    handoff_report: dict[str, Any] | None = None
    if ensure_handoffs:
        report_dir.mkdir(parents=True, exist_ok=True)
        handoff_report = handoff.build_batch_handoff(items, report_dir, intake_path, target_month_override)
    handoff_index = load_handoff_index(report_dir)
    resolved_coverage_report = coverage_report or report_dir / DEFAULT_COVERAGE_REPORT_NAME
    coverage_action_index = load_coverage_action_index(resolved_coverage_report)
    coverage_record_index = load_coverage_record_index(resolved_coverage_report)
    results = [
        refresh_item(
            item,
            intake_path=intake_path,
            report_dir=report_dir,
            scaffold_dir=scaffold_dir,
            stub_dir=stub_dir,
            har_root=har_root,
            write_stub=write_stub,
            handoff_index=handoff_index,
            coverage_action_index=coverage_action_index,
            coverage_record_index=coverage_record_index,
            discover_hars=discover_hars,
            use_discovered_hars=use_discovered_hars,
            target_month_override=target_month_override,
        )
        for item in items
    ]
    ready = [item for item in results if item.get("analysis_status") == "ok"]
    blocked = [item for item in results if item.get("analysis_status") != "ok"]
    scaffolds = [item for item in results if item.get("scaffold_status") == "ok"]
    har_missing = [item for item in blocked if not item.get("har_path_exists")]
    har_present_blocked = [item for item in blocked if item.get("har_path_exists")]
    handoff_file_gaps = [
        item
        for item in blocked
        if not (
            item.get("handoff_exists")
            and item.get("handoff_markdown_exists")
            and item.get("post_capture_check_script_exists")
        )
    ]
    post_capture_check_ready = [
        item
        for item in blocked
        if item.get("har_path_exists") and item.get("post_capture_check_script_exists")
    ]
    capture_required_before_offline_next_step = [
        item
        for item in blocked
        if not item.get("har_path_exists")
    ]
    offline_next_step_gaps = [
        item
        for item in blocked
        if not item.get("har_path_exists") or not item.get("post_capture_check_script_exists")
    ]
    raw_discovered_candidates = [item for item in results if item.get("discovered_har_candidate_count")]
    safe_discovered_candidates = [item for item in results if item.get("safe_discovered_har_candidate_count")]
    selected_discovered_candidates = [item for item in results if item.get("selected_discovered_har_candidate")]
    rejected_discovered_candidates = [
        candidate
        for item in results
        for candidate in item.get("discovered_har_candidates") or []
        if not candidate.get("safe_workflow_candidate")
    ]
    status = "ok" if results and not blocked else "review"
    reason = None if status == "ok" else "workflow_evidence_not_ready"
    next_action_plan = [
        {
            "type": action_type_for_item(item),
            "suggested_next_action": suggested_next_action_for_item(item),
            "reason": action_reason_for_item(item),
            "property": item.get("property"),
            "servicer_hint": item.get("servicer_hint"),
            "har_path": item.get("har_path"),
            "har_path_exists": item.get("har_path_exists"),
            "analysis_report": item.get("analysis_report"),
            "command_precondition_status": item.get("command_precondition_status"),
            "command_precondition_reason": item.get("command_precondition_reason"),
            "command_precondition_next_action": item.get("command_precondition_next_action"),
            "required_capture_quality": item.get("required_capture_quality"),
            "actionable_missing_response_body_count": item.get("actionable_missing_response_body_count"),
            "missing_response_body_paths": item.get("missing_response_body_paths"),
            "missing_response_body_path_counts": item.get("missing_response_body_path_counts"),
            "response_body_requirements": item.get("response_body_requirements"),
            "scaffold_report": item.get("scaffold_report"),
            "scaffold_command": item.get("scaffold_command"),
            "manifest_path": item.get("manifest_path"),
            "registry_entry_path": item.get("registry_entry_path"),
            "registry_entry_written": item.get("registry_entry_written"),
            "registry_install_command": item.get("registry_install_command"),
            "proposed_config_entry": item.get("proposed_config_entry"),
            "safe_to_register_automatically": item.get("safe_to_register_automatically"),
            "stub_path": item.get("stub_path"),
            "generated_downloader_dry_run_command": item.get("generated_downloader_dry_run_command"),
            "generated_downloader_apply_command": item.get("generated_downloader_apply_command"),
            "handoff_path": item.get("handoff_path"),
            "handoff_exists": item.get("handoff_exists"),
            "handoff_markdown_path": item.get("handoff_markdown_path"),
            "handoff_markdown_exists": item.get("handoff_markdown_exists"),
            "suggested_workflow_har_path": item.get("suggested_workflow_har_path"),
            "suggested_workflow_har_path_exists": item.get("suggested_workflow_har_path_exists"),
            "target_statement_month": item.get("target_statement_month"),
            "target_month_document_identifiers": item.get("target_month_document_identifiers"),
            "expected_document_ids": item.get("expected_document_ids"),
            "target_statement_document_metadata_only_details": item.get(
                "target_statement_document_metadata_only_details"
            ),
            "target_statement_document_capture_instruction": item.get(
                "target_statement_document_capture_instruction"
            ),
            "capture_requirements": item.get("capture_requirements"),
            "discovered_har_candidate_count": item.get("discovered_har_candidate_count"),
            "discovered_har_candidates": item.get("discovered_har_candidates"),
            "safe_discovered_har_candidate_count": item.get("safe_discovered_har_candidate_count"),
            "safe_discovered_har_candidate_path": item.get("safe_discovered_har_candidate_path"),
            "selected_discovered_har_candidate": item.get("selected_discovered_har_candidate"),
            "register_workflow_evidence_command": item.get("register_workflow_evidence_command"),
            "register_workflow_evidence_apply_command": item.get("register_workflow_evidence_apply_command"),
            "register_workflow_evidence_ready_to_run_now": item.get(
                "register_workflow_evidence_ready_to_run_now"
            ),
            "advance_workflow_evidence_command": item.get("advance_workflow_evidence_command"),
            "advance_workflow_evidence_apply_command": item.get("advance_workflow_evidence_apply_command"),
            "advance_workflow_evidence_ready_to_run_now": item.get(
                "advance_workflow_evidence_ready_to_run_now"
            ),
            "post_capture_check_script_path": item.get("post_capture_check_script_path"),
            "post_capture_check_script_exists": item.get("post_capture_check_script_exists"),
            "post_capture_check_command": item.get("post_capture_check_command"),
            "post_capture_check_safe_to_run_after_har_capture": item.get(
                "post_capture_check_safe_to_run_after_har_capture"
            ),
            "post_capture_check_ready_to_run_now": item.get("post_capture_check_ready_to_run_now"),
            "capture_command": item.get("capture_command"),
            "capture_command_ready_to_run_now": item.get("capture_command_ready_to_run_now"),
            "capture_command_blocked_reason": item.get("capture_command_blocked_reason"),
            "capture_auth_blocker": item.get("capture_auth_blocker"),
            "capture_auth_blocker_reason": item.get("capture_auth_blocker_reason"),
            "capture_auth_next_action": item.get("capture_auth_next_action"),
            "capture_command_safe_to_run_automatically": item.get(
                "capture_command_safe_to_run_automatically"
            ),
            "capture_required_before_offline_next_step": item.get(
                "capture_required_before_offline_next_step"
            ),
            "post_capture_har_path": item.get("post_capture_har_path"),
            "post_capture_har_path_exists": item.get("post_capture_har_path_exists"),
            "post_capture_advance_workflow_evidence_command": item.get(
                "post_capture_advance_workflow_evidence_command"
            ),
            "post_capture_advance_workflow_evidence_apply_command": item.get(
                "post_capture_advance_workflow_evidence_apply_command"
            ),
            "post_capture_advance_workflow_evidence_ready_after_capture": item.get(
                "post_capture_advance_workflow_evidence_ready_after_capture"
            ),
            "post_capture_source": item.get("post_capture_source"),
            "safe_to_run_automatically": item.get("analysis_status") == "ok",
        }
        for item in results
        if item.get("analysis_status") != "ok" or item.get("scaffold_status") == "ok"
    ]
    next_action_safe_count = sum(
        1 for item in next_action_plan if item.get("safe_to_run_automatically") is True
    )
    next_action_manual_count = len(next_action_plan) - next_action_safe_count
    safe_to_run_automatically = status == "ok" and next_action_manual_count == 0
    return {
        "job": "mortgage-workflow-evidence-refresh",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reason": reason,
        "intake_path": str(intake_path),
        "har_root": str(har_root),
        "property_count": len(results),
        "analysis_ready_count": len(ready),
        "analysis_blocked_count": len(blocked),
        "scaffold_ready_count": len(scaffolds),
        "write_stub": write_stub,
        "target_month": target_month_override,
        "ensure_handoffs": ensure_handoffs,
        "discover_hars": discover_hars,
        "use_discovered_hars": use_discovered_hars,
        "coverage_report": str(resolved_coverage_report),
        "coverage_next_action_property_count": len(coverage_action_index),
        "raw_discovered_har_candidate_property_count": len(raw_discovered_candidates),
        "raw_discovered_har_candidate_properties": [item.get("property") for item in raw_discovered_candidates],
        "discovered_har_candidate_property_count": len(safe_discovered_candidates),
        "discovered_har_candidate_properties": [item.get("property") for item in safe_discovered_candidates],
        "rejected_discovered_har_candidate_count": len(rejected_discovered_candidates),
        "rejected_discovered_har_candidate_paths": [
            item.get("path") for item in rejected_discovered_candidates
        ],
        "safe_discovered_har_candidate_property_count": len(safe_discovered_candidates),
        "safe_discovered_har_candidate_properties": [item.get("property") for item in safe_discovered_candidates],
        "selected_discovered_har_candidate_count": len(selected_discovered_candidates),
        "selected_discovered_har_candidate_properties": [
            item.get("property") for item in selected_discovered_candidates
        ],
        "handoff_pending_property_count": (
            handoff_report.get("pending_property_count") if isinstance(handoff_report, dict) else None
        ),
        "handoff_pending_properties": (
            handoff_report.get("pending_properties") if isinstance(handoff_report, dict) else None
        ),
        "handoff_index_path": handoff_report.get("index_path") if isinstance(handoff_report, dict) else None,
        "handoff_file_gap_count": len(handoff_file_gaps),
        "handoff_file_gap_properties": [item.get("property") for item in handoff_file_gaps],
        "har_path_missing_count": len(har_missing),
        "har_path_missing_properties": [item.get("property") for item in har_missing],
        "har_path_present_blocked_count": len(har_present_blocked),
        "har_path_present_blocked_properties": [item.get("property") for item in har_present_blocked],
        "post_capture_check_ready_count": len(post_capture_check_ready),
        "post_capture_check_ready_properties": [item.get("property") for item in post_capture_check_ready],
        "capture_required_before_offline_next_step_count": len(capture_required_before_offline_next_step),
        "capture_required_before_offline_next_step_properties": [
            item.get("property") for item in capture_required_before_offline_next_step
        ],
        "offline_next_step_ready_count": len(scaffolds) + len(post_capture_check_ready),
        "offline_next_step_gap_count": len(offline_next_step_gaps),
        "offline_next_step_gap_properties": [item.get("property") for item in offline_next_step_gaps],
        "safe_to_run_automatically": safe_to_run_automatically,
        "results": results,
        "next_action_plan_count": len(next_action_plan),
        "next_action_manual_count": next_action_manual_count,
        "next_action_safe_to_run_automatically_count": next_action_safe_count,
        "next_action_plan": next_action_plan,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--coverage-report",
        type=Path,
        default=None,
        help="Coverage report to mine for post-capture HAR and advance-evidence commands.",
    )
    parser.add_argument("--scaffold-dir", type=Path, default=DEFAULT_SCAFFOLD_DIR)
    parser.add_argument("--stub-dir", type=Path, default=DEFAULT_STUB_DIR)
    parser.add_argument("--har-root", type=Path)
    parser.add_argument("--write-stubs", action="store_true")
    parser.add_argument("--target-month", help="Override target statement month as YYYY-MM.")
    parser.add_argument(
        "--ensure-handoffs",
        action="store_true",
        help="Regenerate pending workflow-evidence handoffs and post-capture check scripts before refreshing.",
    )
    parser.add_argument(
        "--use-discovered-hars",
        action="store_true",
        help="Analyze an unregistered HAR from --har-root only when it is the single safe candidate for a property.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    har_root = args.har_root or coverage.DEFAULT_HAR_ROOT
    discover_hars = bool(args.har_root or args.use_discovered_hars)
    report = build_refresh_report(
        args.intake,
        report_dir=args.report_dir,
        scaffold_dir=args.scaffold_dir,
        stub_dir=args.stub_dir,
        har_root=har_root,
        write_stub=args.write_stubs,
        ensure_handoffs=args.ensure_handoffs,
        discover_hars=discover_hars,
        use_discovered_hars=args.use_discovered_hars,
        coverage_report=args.coverage_report,
        target_month_override=args.target_month,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = dict(report)
    report["idempotency_digest"] = stable_report_digest(report)
    report = write_json_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
