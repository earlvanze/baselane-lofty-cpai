#!/usr/bin/env python3
"""Advance captured mortgage HAR evidence through offline downloader scaffolding."""

from __future__ import annotations

import argparse
import json
import os
import shlex
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
import mortgage_workflow_downloader_scaffold as scaffold
import mortgage_workflow_har_intake_analyzer as analyzer
import register_mortgage_downloader_registry_entry as registry
import register_mortgage_workflow_evidence as evidence
from stable_json_report import stable_report_digest, write_json_report

DEFAULT_INTAKE = WORKSPACE_ROOT / "config" / "mortgage_downloader_intake.json"
DEFAULT_REGISTRY = WORKSPACE_ROOT / "config" / "mortgage_statement_downloaders.json"
DEFAULT_COVERAGE_REPORT = WORKSPACE_ROOT / "reports" / "mortgage_downloader_coverage_report.json"
DEFAULT_REPORT_DIR = WORKSPACE_ROOT / "reports"
DEFAULT_REPORT = DEFAULT_REPORT_DIR / "mortgage_workflow_evidence_advance_report.json"
DEFAULT_SCAFFOLD_DIR = DEFAULT_REPORT_DIR / "mortgage_downloader_scaffolds"
DEFAULT_STUB_DIR = WORKSPACE_ROOT / "scripts" / "generated_mortgage_downloaders"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_intake(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    properties = data.get("properties") if isinstance(data, dict) else None
    if not isinstance(properties, list):
        raise ValueError("intake properties is not a list")
    return [item for item in properties if isinstance(item, dict)]


def selected_items(items: list[dict[str, Any]], property_filters: list[str]) -> list[dict[str, Any]]:
    if not property_filters:
        return items
    wanted = {coverage.normalize_property(value) for value in property_filters}
    return [item for item in items if coverage.normalize_property(item.get("property")) in wanted]


def parse_har_overrides(values: list[str]) -> dict[str, Path]:
    overrides: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--har must be formatted as PROPERTY=PATH")
        prop, path = value.split("=", 1)
        prop = prop.strip()
        path = path.strip()
        if not prop or not path:
            raise ValueError("--har must include both PROPERTY and PATH")
        overrides[coverage.normalize_property(prop)] = Path(path)
    return overrides


def coverage_ready_property_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    records = data.get("coverage_records") if isinstance(data, dict) else None
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
            ready.add(coverage.normalize_property(record.get("property")))
    return ready


def resolve_har(item: dict[str, Any], overrides: dict[str, Path]) -> tuple[Path | None, str]:
    prop = str(item.get("property") or "").strip()
    override = overrides.get(coverage.normalize_property(prop))
    if override is not None:
        return override, "har_override"
    selected, reason = analyzer.default_har_path(item, prop)
    return (Path(selected) if selected else None), reason


def scaffold_report_path(report_dir: Path, property_name: str) -> Path:
    return report_dir / f"mortgage_workflow_downloader_scaffold_{coverage.slugify(property_name)}.json"


def scaffold_command(
    analysis_report: object,
    *,
    scaffold_dir: Path,
    stub_dir: Path,
    report_path: Path,
    target_month: str | None,
) -> str:
    command = (
        "python3 scripts/mortgage_workflow_downloader_scaffold.py "
        f"--analysis-report {shlex.quote(str(analysis_report))} "
        f"--output-dir {shlex.quote(str(scaffold_dir))} "
        f"--stub-dir {shlex.quote(str(stub_dir))} "
        "--write-stub "
        f"--report {shlex.quote(str(report_path))}"
    )
    if target_month:
        command += f" --target-month {shlex.quote(target_month)}"
    return command


def registry_install_command(entry_path: object, registry_path: Path, *, apply: bool) -> str | None:
    text = str(entry_path or "").strip()
    if not text:
        return None
    command = (
        "python3 scripts/register_mortgage_downloader_registry_entry.py "
        f"--entry {shlex.quote(text)} "
        f"--config {shlex.quote(str(registry_path))}"
    )
    if apply:
        return f"{command} --apply"
    return command


def generated_dry_run_command(stub_path: object) -> str | None:
    text = str(stub_path or "").strip()
    if not text:
        return None
    return f"python3 {shlex.quote(text)}"


def tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def generated_dry_run_target_month_env(target_month: str | None) -> dict[str, str]:
    if not target_month:
        return {}
    return {
        "MORTGAGE_GENERATED_HAR_TARGET_MONTH": target_month,
        "MORTGAGE_STATEMENT_TARGET_MONTH": target_month,
        "BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH": target_month,
        "MORTGAGE_WORKFLOW_TARGET_MONTH": target_month,
    }


def generated_dry_run_verified(report: dict[str, Any], *, requested_target_month: str | None = None) -> bool:
    if requested_target_month and report.get("target_month") != requested_target_month:
        return False
    if report.get("target_month_statement_available") is True:
        return True
    return (
        report.get("reason") == "apply_required_to_write_target_month_statement"
        and int(report.get("target_month_downloadable_count") or 0) > 0
    )


def registry_conflict_is_usable_existing_downloader(report: dict[str, Any]) -> bool:
    return (
        report.get("reason") in {"property_already_configured", "duplicate_id_conflict"}
        and report.get("conflicting_entry_enabled") is True
        and bool(str(report.get("conflicting_entry_id") or "").strip())
        and bool(str(report.get("conflicting_entry_script") or "").strip())
    )


def enable_verified_registry_entry(
    *,
    registry_path: Path,
    entry_id: object,
    property_name: str,
    apply: bool,
) -> dict[str, Any]:
    entry_id_text = str(entry_id or "").strip()
    report: dict[str, Any] = {
        "status": "review",
        "reason": None,
        "registry": str(registry_path),
        "entry_id": entry_id_text,
        "property": property_name,
        "apply": apply,
        "config_written": False,
        "enabled_before": None,
        "enabled_after": None,
        "safe_to_run_automatically": True,
    }
    if not entry_id_text:
        report["reason"] = "entry_id_missing"
        return report
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as exc:
        report.update(reason="registry_unreadable", error=str(exc))
        return report
    entries = data.get("downloaders") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        report["reason"] = "registry_downloaders_not_list"
        return report
    target_index: int | None = None
    target_entry: dict[str, Any] | None = None
    for index, item in enumerate(entries):
        if isinstance(item, dict) and str(item.get("id") or "").strip() == entry_id_text:
            target_index = index
            target_entry = item
            break
    if target_index is None or target_entry is None:
        report["reason"] = "registry_entry_not_found"
        return report
    if coverage.normalize_property(target_entry.get("property")) != coverage.normalize_property(property_name):
        report.update(
            reason="registry_entry_property_mismatch",
            registry_property=target_entry.get("property"),
        )
        return report
    report["enabled_before"] = target_entry.get("enabled")
    if target_entry.get("enabled") is True:
        report.update(status="ok", reason="already_enabled", enabled_after=True)
        return report
    if not apply:
        report.update(status="ok", reason="ready_to_enable", enabled_after=True, would_write=True)
        return report
    updated_entries = [*entries]
    updated_entries[target_index] = {**target_entry, "enabled": True}
    updated_data = {**data, "downloaders": updated_entries}
    registry_path.write_text(json.dumps(updated_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report.update(
        status="ok",
        reason="enabled_verified_generated_downloader",
        enabled_after=True,
        config_written=True,
        would_write=False,
    )
    return report


def run_generated_dry_run(stub_path: Path, *, workspace_root: Path, target_month: str | None) -> dict[str, Any]:
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(workspace_root)
    dry_run_dir = workspace_root / "reports" / "generated_mortgage_dry_run" / stub_path.stem
    env["MORTGAGE_GENERATED_HAR_OUTPUT_DIR"] = str(dry_run_dir)
    env["MORTGAGE_GENERATED_HAR_REPORT"] = str(dry_run_dir / "report.json")
    env.pop("MORTGAGE_GENERATED_HAR_DOWNLOADER_APPLY", None)
    target_month_env = generated_dry_run_target_month_env(target_month)
    env.update(target_month_env)
    proc = subprocess.run(
        [sys.executable, str(stub_path)],
        cwd=str(workspace_root),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    report: dict[str, Any] = {
        "command": generated_dry_run_command(stub_path),
        "rc": proc.returncode,
        "stdout_tail": tail(proc.stdout),
        "stderr_tail": tail(proc.stderr),
        "status": "review",
        "verified_target_month": False,
        "requested_target_month": target_month,
        "target_month_env": target_month_env,
    }
    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        report.update(reason="generated_dry_run_stdout_unreadable", error=str(exc))
        return report
    if not isinstance(data, dict):
        report["reason"] = "generated_dry_run_stdout_not_object"
        return report
    child_target_month = data.get("target_month")
    target_month_mismatch = bool(target_month and child_target_month != target_month)
    verified = generated_dry_run_verified(data, requested_target_month=target_month)
    report.update(
        {
            "status": "ok" if verified else "review",
            "reason": None if verified else ("generated_dry_run_target_month_mismatch" if target_month_mismatch else data.get("reason")),
            "verified_target_month": verified,
            "target_month_mismatch": target_month_mismatch,
            "child_status": data.get("status"),
            "child_reason": data.get("reason"),
            "child_report_path": data.get("report_path"),
            "target_month": child_target_month,
            "target_month_statement_available": data.get("target_month_statement_available"),
            "target_month_existing_count": data.get("target_month_existing_count"),
            "target_month_downloaded_count": data.get("target_month_downloaded_count"),
            "target_month_downloadable_count": data.get("target_month_downloadable_count"),
            "target_month_skipped_count": data.get("target_month_skipped_count"),
            "candidate_count": data.get("candidate_count"),
            "candidate_source_counts": data.get("candidate_source_counts"),
            "target_month_candidate_count": data.get("target_month_candidate_count"),
            "target_month_candidate_source_counts": data.get("target_month_candidate_source_counts"),
            "target_month_downloadable_source_counts": data.get("target_month_downloadable_source_counts"),
            "skipped_target_month_files": data.get("skipped_target_month_files"),
        }
    )
    return report


def build_item_report(
    item: dict[str, Any],
    *,
    har_path: Path | None,
    har_selection_reason: str,
    intake_path: Path,
    registry_path: Path,
    report_dir: Path,
    scaffold_dir: Path,
    stub_dir: Path,
    workspace_root: Path,
    apply_evidence: bool,
    write_stub: bool,
    install_registry_entries: bool,
    apply_registry: bool,
    enable_verified_registry_entry_flag: bool,
    run_generated_dry_runs: bool,
    target_month: str | None,
) -> dict[str, Any]:
    prop = str(item.get("property") or "").strip()
    item_report: dict[str, Any] = {
        "property": prop,
        "servicer_hint": item.get("servicer_hint"),
        "har_path": str(har_path) if har_path else None,
        "har_selection_reason": har_selection_reason,
        "har_path_exists": bool(har_path and har_path.exists()),
        "apply_evidence": apply_evidence,
        "write_stub": write_stub,
        "install_registry_entries": install_registry_entries,
        "apply_registry": apply_registry,
        "enable_verified_registry_entry": enable_verified_registry_entry_flag,
        "run_generated_dry_runs": run_generated_dry_runs,
        "safe_to_run_automatically": False,
        "status": "review",
        "reason": None,
    }
    if not har_path:
        item_report["reason"] = "har_argument_required"
        return item_report

    evidence_report = evidence.build_report(
        property_name=prop,
        har_path=har_path,
        intake_path=intake_path,
        report_dir=report_dir,
        apply=apply_evidence,
    )
    item_report["evidence_report"] = evidence_report
    item_report["analysis_report"] = evidence_report.get("analysis_report")
    item_report["analysis_status"] = evidence_report.get("analysis_status")
    item_report["analysis_reason"] = evidence_report.get("analysis_reason")
    if evidence_report.get("status") != "ok":
        item_report.update(status="review", reason=evidence_report.get("reason") or "evidence_not_ready")
        return item_report

    analysis_report = Path(str(evidence_report.get("analysis_report") or ""))
    if not analysis_report.exists():
        item_report.update(status="review", reason="analysis_report_missing")
        return item_report
    if evidence_report.get("analysis_status") != "ok":
        item_report.update(status="review", reason=evidence_report.get("analysis_reason") or "analysis_not_ready")
        return item_report

    scaffold_output_path = scaffold_report_path(report_dir, prop)
    scaffold_report = scaffold.build_scaffold(
        analysis_report,
        output_dir=scaffold_dir,
        stub_dir=stub_dir,
        write_stub=write_stub,
        target_month=target_month,
    )
    scaffold_report = write_json_report(scaffold_output_path, scaffold_report)
    item_report.update(
        {
            "scaffold_report": str(scaffold_output_path),
            "scaffold_status": scaffold_report.get("status"),
            "scaffold_command": scaffold_command(
                analysis_report,
                scaffold_dir=scaffold_dir,
                stub_dir=stub_dir,
                report_path=scaffold_output_path,
                target_month=target_month,
            ),
            "manifest_path": scaffold_report.get("manifest_path"),
            "registry_entry_path": scaffold_report.get("registry_entry_path"),
            "registry_entry_written": scaffold_report.get("registry_entry_written"),
            "stub_path": scaffold_report.get("stub_path") if write_stub else None,
            "generated_dry_run_command": generated_dry_run_command(scaffold_report.get("stub_path"))
            if write_stub
            else None,
            "safe_to_register_automatically": scaffold_report.get("safe_to_register_automatically"),
        }
    )
    if scaffold_report.get("status") != "ok":
        item_report.update(status="review", reason=scaffold_report.get("reason") or "scaffold_not_ready")
        return item_report

    entry_path = Path(str(scaffold_report.get("registry_entry_path") or ""))
    item_report["registry_install_command"] = registry_install_command(entry_path, registry_path, apply=apply_registry)
    if install_registry_entries:
        registry_report = registry.build_report(entry_path, registry_path, apply=apply_registry)
        item_report["registry_install_report"] = registry_report
        if registry_report.get("status") != "ok":
            if registry_conflict_is_usable_existing_downloader(registry_report):
                item_report.update(
                    {
                        "registry_install_status": "ok",
                        "registry_install_reason": "existing_enabled_downloader_reused",
                        "existing_downloader_id": registry_report.get("conflicting_entry_id"),
                        "existing_downloader_script": registry_report.get("conflicting_entry_script"),
                        "existing_downloader_report": registry_report.get("conflicting_entry_report"),
                    }
                )
            else:
                item_report.update(status="review", reason=registry_report.get("reason") or "registry_install_not_ready")
                return item_report
        else:
            item_report["registry_install_status"] = registry_report.get("status")
            item_report["registry_install_reason"] = registry_report.get("reason")

    if run_generated_dry_runs and write_stub:
        stub_path = Path(str(scaffold_report.get("stub_path") or ""))
        generated_report = run_generated_dry_run(stub_path, workspace_root=workspace_root, target_month=target_month)
        item_report["generated_dry_run_report"] = generated_report
        if generated_report.get("status") != "ok":
            item_report.update(status="review", reason=generated_report.get("reason") or "generated_dry_run_not_verified")
            return item_report

    if enable_verified_registry_entry_flag:
        if not (install_registry_entries and apply_registry):
            item_report.update(status="review", reason="enable_verified_registry_entry_requires_apply_registry")
            return item_report
        if not (run_generated_dry_runs and item_report.get("generated_dry_run_report", {}).get("verified_target_month")):
            item_report.update(status="review", reason="enable_verified_registry_entry_requires_verified_dry_run")
            return item_report
        enable_report = enable_verified_registry_entry(
            registry_path=registry_path,
            entry_id=scaffold_report.get("proposed_config_entry", {}).get("id")
            if isinstance(scaffold_report.get("proposed_config_entry"), dict)
            else None,
            property_name=prop,
            apply=True,
        )
        item_report["enable_verified_registry_entry_report"] = enable_report
        if enable_report.get("status") != "ok":
            item_report.update(status="review", reason=enable_report.get("reason") or "enable_registry_entry_failed")
            return item_report

    item_report.update(status="ok", reason=None, safe_to_run_automatically=True)
    return item_report


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    items = load_intake(args.intake)
    selected = selected_items(items, args.property)
    coverage_ready = coverage_ready_property_keys(args.coverage_report) if args.skip_coverage_ready else set()
    skipped_ready = [
        item for item in selected if coverage.normalize_property(item.get("property")) in coverage_ready
    ]
    selected = [
        item for item in selected if coverage.normalize_property(item.get("property")) not in coverage_ready
    ]
    overrides = parse_har_overrides(args.har)
    results = []
    for item in selected:
        har_path, reason = resolve_har(item, overrides)
        results.append(
            build_item_report(
                item,
                har_path=har_path,
                har_selection_reason=reason,
                intake_path=args.intake,
                registry_path=args.registry,
                report_dir=args.report_dir,
                scaffold_dir=args.scaffold_dir,
                stub_dir=args.stub_dir,
                workspace_root=args.workspace_root,
                apply_evidence=args.apply_evidence,
                write_stub=args.write_stubs,
                install_registry_entries=args.install_registry_entries,
                apply_registry=args.apply_registry,
                enable_verified_registry_entry_flag=args.enable_verified_registry_entry,
                run_generated_dry_runs=args.run_generated_dry_runs,
                target_month=args.target_month,
            )
        )
    ok_count = sum(1 for item in results if item.get("status") == "ok")
    review_count = sum(1 for item in results if item.get("status") != "ok")
    complete = (bool(results) and review_count == 0) or (not results and bool(skipped_ready))
    safe_to_run_automatically = complete
    return {
        "job": "advance-mortgage-workflow-evidence",
        "generated_at": utc_now(),
        "intake": str(args.intake),
        "registry": str(args.registry),
        "report_dir": str(args.report_dir),
        "property_count": len(results),
        "skipped_coverage_ready_count": len(skipped_ready),
        "skipped_coverage_ready_properties": [item.get("property") for item in skipped_ready],
        "ok_count": ok_count,
        "review_count": review_count,
        "status": "ok" if complete else "review",
        "reason": None if complete else "workflow_evidence_advance_incomplete",
        "safe_to_run_automatically": safe_to_run_automatically,
        "apply_evidence": args.apply_evidence,
        "write_stubs": args.write_stubs,
        "install_registry_entries": args.install_registry_entries,
        "apply_registry": args.apply_registry,
        "enable_verified_registry_entry": args.enable_verified_registry_entry,
        "run_generated_dry_runs": args.run_generated_dry_runs,
        "target_month": args.target_month,
        "results": results,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--property", action="append", default=[], help="Limit to a property; repeatable")
    parser.add_argument("--har", action="append", default=[], help="Override HAR as PROPERTY=PATH; repeatable")
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--coverage-report", type=Path)
    parser.add_argument(
        "--skip-coverage-ready",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip properties already covered by an automation-ready downloader in the coverage report.",
    )
    parser.add_argument("--workspace-root", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--scaffold-dir", type=Path, default=DEFAULT_SCAFFOLD_DIR)
    parser.add_argument("--stub-dir", type=Path, default=DEFAULT_STUB_DIR)
    parser.add_argument("--apply-evidence", action="store_true", help="Write captured HAR path into intake config")
    parser.add_argument("--write-stubs", action="store_true", help="Write generated downloader stubs for ready analyses")
    parser.add_argument(
        "--install-registry-entries",
        action="store_true",
        help="Validate generated registry entries and optionally append them to the registry",
    )
    parser.add_argument("--apply-registry", action="store_true", help="Append generated disabled registry entries")
    parser.add_argument(
        "--enable-verified-registry-entry",
        action="store_true",
        help="After an apply-registry run and verified generated dry-run, enable the generated registry entry.",
    )
    parser.add_argument(
        "--run-generated-dry-runs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run generated stubs without apply after scaffold creation and verify target-month availability",
    )
    parser.add_argument("--target-month", help="Target statement month, YYYY-MM, for generated dry-runs")
    args = parser.parse_args(argv)
    if args.coverage_report is None:
        args.coverage_report = args.workspace_root / "reports" / "mortgage_downloader_coverage_report.json"
    return args


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    report = build_report(args)
    report = dict(report)
    report["idempotency_digest"] = stable_report_digest(report)
    report = write_json_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
