#!/usr/bin/env python3
"""Safely register a captured mortgage workflow HAR in downloader intake config."""

from __future__ import annotations

import argparse
import json
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
from stable_json_report import write_json_report

DEFAULT_INTAKE = WORKSPACE_ROOT / "config" / "mortgage_downloader_intake.json"
DEFAULT_REPORT_DIR = WORKSPACE_ROOT / "reports"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "JSON root is not an object"
    return data, None


def find_property(items: list[Any], property_name: str) -> tuple[int | None, dict[str, Any] | None]:
    target = coverage.normalize_property(property_name)
    for index, item in enumerate(items):
        if isinstance(item, dict) and coverage.normalize_property(item.get("property")) == target:
            return index, item
    return None, None


def same_path(left: object, right: Path) -> bool:
    left_text = str(left or "").strip()
    if not left_text:
        return False
    if left_text == str(right):
        return True
    try:
        return Path(left_text).resolve() == right.resolve()
    except Exception:
        return False


def build_report(
    *,
    property_name: str,
    har_path: Path,
    intake_path: Path,
    report_dir: Path,
    apply: bool,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "job": "register-mortgage-workflow-evidence",
        "generated_at": utc_now(),
        "property": property_name,
        "har_path": str(har_path),
        "har_path_exists": har_path.exists(),
        "har_path_size": har_path.stat().st_size if har_path.exists() else None,
        "intake_path": str(intake_path),
        "intake_exists": intake_path.exists(),
        "apply": apply,
        "status": "review",
        "reason": None,
        "config_written": False,
        "safe_to_run_automatically": True,
    }
    if not har_path.exists():
        report["reason"] = "har_missing"
        return report
    if har_path.stat().st_size <= 0:
        report["reason"] = "har_empty"
        return report

    intake, intake_error = load_json(intake_path)
    if intake_error or intake is None:
        report.update(reason="intake_unreadable", error=intake_error)
        return report
    properties = intake.get("properties")
    if not isinstance(properties, list):
        report["reason"] = "intake_properties_not_list"
        return report
    index, item = find_property(properties, property_name)
    if index is None or item is None:
        report["reason"] = "property_not_found"
        return report

    evidence = item.get("workflow_evidence")
    existing_evidence = evidence if isinstance(evidence, list) else []
    already_present = any(same_path(value, har_path) for value in existing_evidence)
    analysis_report_path = analyzer.default_report_path(report_dir, str(item.get("property") or property_name), har_path)
    analysis_selection_reason = "first_existing_workflow_evidence" if already_present else "registered_workflow_evidence"
    analysis = analyzer.analyze_har(
        har_path,
        property_name=str(item.get("property") or property_name),
        item=item,
        selection_reason=analysis_selection_reason,
    )
    analysis["report_path"] = str(analysis_report_path)
    analysis = write_json_report(analysis_report_path, analysis)

    next_status = item.get("status")
    if str(item.get("status") or "") == "needs_workflow_evidence":
        next_status = "workflow_evidence_captured"
    report.update(
        {
            "status": "ok",
            "reason": "already_present" if already_present else ("registered" if apply else "ready_to_register"),
            "property_index": index,
            "servicer_hint": item.get("servicer_hint"),
            "portal_url": item.get("portal_url"),
            "analysis_report": str(analysis_report_path),
            "analysis_status": analysis.get("status"),
            "analysis_reason": analysis.get("reason"),
            "analysis_suggested_next_action": analysis.get("suggested_next_action"),
            "required_capture_quality": analysis.get("required_capture_quality"),
            "actionable_missing_response_body_count": analysis.get("actionable_missing_response_body_count"),
            "missing_response_body_paths": analysis.get("missing_response_body_paths"),
            "existing_workflow_evidence_count": len(existing_evidence),
            "already_present": already_present,
            "would_append": not already_present and not apply,
            "previous_status": item.get("status"),
            "next_status": next_status,
        }
    )
    if already_present or not apply:
        return report

    updated_item = {
        **item,
        "status": next_status,
        "workflow_evidence": [*existing_evidence, str(har_path)],
    }
    updated_properties = [*properties]
    updated_properties[index] = updated_item
    updated_intake = {**intake, "properties": updated_properties}
    intake_path.write_text(json.dumps(updated_intake, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report.update(
        {
            "config_written": True,
            "would_append": False,
            "workflow_evidence_count": len(updated_item["workflow_evidence"]),
        }
    )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--property", required=True, help="Property name from config/mortgage_downloader_intake.json")
    parser.add_argument("--har", required=True, type=Path, help="Captured HAR path to register")
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    report = build_report(
        property_name=args.property,
        har_path=args.har,
        intake_path=args.intake,
        report_dir=args.report_dir,
        apply=args.apply,
    )
    if args.report:
        report = write_json_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
