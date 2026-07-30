#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))

from scripts import baselane_export_ledger as core

WORKSPACE_ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE", Path(__file__).absolute().parents[1]))
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", str(WORKSPACE_ROOT / "reports")))


def first_existing_dir(candidates: list[Path], fallback: Path) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return fallback


DROPBOX_ROOT = Path(os.environ["DROPBOX_ROOT"]) if os.environ.get("DROPBOX_ROOT") else first_existing_dir(
    [
        Path("/mnt/c/Users/digit/Dropbox"),
        Path("/data/Dropbox"),
        Path.home() / "Dropbox",
        Path("/home/digit/Dropbox"),
    ],
    Path("/mnt/c/Users/digit/Dropbox"),
)
TRACKER_DIR = Path(os.environ.get("TRACKER_DIR", str(DROPBOX_ROOT / "Projects/assetrail")))
OUT_PATH = TRACKER_DIR / "ECO Systems General Ledger.csv"
GQL_BRIDGE = Path(os.environ.get("BASELANE_GQL_BRIDGE", str(WORKSPACE_ROOT / "scripts/baselane_graphql_via_cdp.js")))
EXPECTED_SELECTED = int(os.environ.get("BASELANE_EXPECTED_SELECTED", "0"))
MIN_ROWS = int(os.environ.get("BASELANE_MIN_ROWS", "6000"))
MAX_ROWS = int(os.environ.get("BASELANE_MAX_ROWS", "25000"))
ISSUE_CLASS = "baselane-export-ledger-cdp"
SCRIPT_PATH = Path(__file__).resolve()


def diagnostic_command() -> str:
    return f"python3 {SCRIPT_PATH} --json"


DIAGNOSTIC_COMMAND = diagnostic_command()


def remediation_fields(classification: str) -> dict[str, Any]:
    has_issues = classification != "ok"
    return {
        "remediation_class": "operator-reviewed-baselane-export-ledger-cdp" if has_issues else "no-remediation-needed",
        "requires_operator_approval": has_issues,
        "requires_interactive_sudo": False,
        "requires_interactive_oauth": False,
        "safe_to_run_automatically": not has_issues,
        "review_command": diagnostic_command(),
        "review_command_safe_to_run_automatically": True,
        "cleanup_command_after_review": None,
        "restart_command_after_review": None,
        "oauth_command_after_review": None,
        "helper_command_after_review": None,
    }


def review_command_validation(command: object | None = None) -> dict[str, Any]:
    command_text = str(command if command is not None else diagnostic_command())
    try:
        parts = shlex.split(command_text)
    except ValueError as exc:
        parts = []
        parse_issue = str(exc)
    else:
        parse_issue = None
    expected_path = str(SCRIPT_PATH)
    script_exists = SCRIPT_PATH.exists()
    script_is_file = SCRIPT_PATH.is_file()
    issues: list[str] = []
    if parse_issue:
        issues.append(f"command parse failed: {parse_issue}")
    if not parts or parts[0] != "python3":
        issues.append("review command must start with python3")
    if expected_path not in parts:
        issues.append(f"review command must target {expected_path}")
    if "--json" not in parts:
        issues.append("review command must include --json")
    if not script_exists:
        issues.append(f"review command script is missing: {expected_path}")
    elif not script_is_file:
        issues.append(f"review command path is not a file: {expected_path}")
    return {
        "command": command_text,
        "expected_script_path": expected_path,
        "script_exists": script_exists,
        "script_is_file": script_is_file,
        "python3_present": bool(parts and parts[0] == "python3"),
        "script_path_present": expected_path in parts,
        "json_flag_present": "--json" in parts,
        "path": expected_path,
        "path_exists": script_exists,
        "requires_executable": False,
        "valid": not issues,
        "issues": issues,
        "issue": issues[0] if issues else None,
    }


def classified_issue_records(issues: list[str], evidence: dict[str, Any], classification: str) -> list[dict[str, Any]]:
    fields = remediation_fields(classification)
    review_validation = review_command_validation(fields.get("review_command"))
    return [
        {
            "issue": issue,
            "issue_class": ISSUE_CLASS,
            "classification": classification,
            "area": "baselane-export-ledger-cdp",
            "node_available": evidence.get("node_available"),
            "gql_bridge_exists": evidence.get("gql_bridge_exists"),
            "gql_bridge_readable": evidence.get("gql_bridge_readable"),
            "tracker_parent_exists": evidence.get("tracker_parent_exists"),
            "tracker_parent_writable": evidence.get("tracker_parent_writable"),
            "reports_parent_exists": evidence.get("reports_parent_exists"),
            "reports_parent_writable": evidence.get("reports_parent_writable"),
            "min_rows": evidence.get("min_rows"),
            "max_rows": evidence.get("max_rows"),
            "review_command_valid": review_validation["valid"],
            "review_command_validation": review_validation,
            **fields,
        }
        for issue in issues
    ]


def classified_issue_summary(report: dict[str, Any]) -> dict[str, Any]:
    classified = report.get("classified_issues") or []
    class_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    for issue in classified:
        issue_class = issue.get("issue_class")
        route = issue.get("classification", report.get("classification"))
        if issue_class:
            class_counts[issue_class] = class_counts.get(issue_class, 0) + 1
        if route:
            route_counts[route] = route_counts.get(route, 0) + 1
    review_validation_issues = [
        issue.get("review_command_validation")
        for issue in classified
        if issue.get("review_command_safe_to_run_automatically")
        and not issue.get("review_command_valid")
    ]
    return {
        "total": len(classified),
        "total_count": len(classified),
        "ok_count": int(report.get("ok_count") or 0),
        "issue_count": int(report.get("issue_count") or 0),
        "visible_ok_count": len(report.get("visible_ok") or []),
        "class_counts": class_counts,
        "issue_class_counts": class_counts,
        "route_classification": report.get("classification"),
        "route_classification_counts": route_counts,
        "approval_required_count": sum(1 for issue in classified if issue.get("requires_operator_approval")),
        "review_required_count": int(report.get("review_required_count") or 0),
        "interactive_sudo_count": sum(1 for issue in classified if issue.get("requires_interactive_sudo")),
        "interactive_oauth_count": sum(1 for issue in classified if issue.get("requires_interactive_oauth")),
        "safe_review_command_count": sum(1 for issue in classified if issue.get("review_command_safe_to_run_automatically")),
        "valid_review_command_count": sum(1 for issue in classified if issue.get("review_command_valid")),
        "invalid_review_command_count": sum(
            1
            for issue in classified
            if issue.get("review_command_safe_to_run_automatically")
            and not issue.get("review_command_valid")
        ),
        "review_command_validation_issues": review_validation_issues,
        "safe_to_run_automatically": report.get("safe_to_run_automatically") is True,
        "node_available": report.get("node_available") is True,
        "gql_bridge_exists": report.get("gql_bridge_exists") is True,
        "gql_bridge_readable": report.get("gql_bridge_readable") is True,
        "tracker_dir_exists": report.get("tracker_dir_exists") is True,
        "tracker_parent_exists": report.get("tracker_parent_exists") is True,
        "tracker_parent_writable": report.get("tracker_parent_writable") is True,
        "reports_dir_exists": report.get("reports_dir_exists") is True,
        "reports_parent_exists": report.get("reports_parent_exists") is True,
        "reports_parent_writable": report.get("reports_parent_writable") is True,
        "canonical_exists": report.get("canonical_exists") is True,
        "node_bridge_attempted": report.get("node_bridge_attempted") is True,
        "temp_payload_write_attempted": report.get("temp_payload_write_attempted") is True,
        "csv_write_attempted": report.get("csv_write_attempted") is True,
        "guard_write_attempted": report.get("guard_write_attempted") is True,
        "alert_write_attempted": report.get("alert_write_attempted") is True,
        "remediation_class": report.get("remediation_class"),
        "cleanup_command_available_after_review": bool(report.get("cleanup_command_after_review")),
        "restart_command_available_after_review": bool(report.get("restart_command_after_review")),
        "oauth_command_available_after_review": bool(report.get("oauth_command_after_review")),
        "helper_command_available_after_review": bool(report.get("helper_command_after_review")),
    }


def build_report(
    tracker_dir: Path = TRACKER_DIR,
    reports_dir: Path = REPORTS_DIR,
    out_path: Path | None = None,
    gql_bridge: Path = GQL_BRIDGE,
    expected_selected: int = EXPECTED_SELECTED,
    min_rows: int = MIN_ROWS,
    max_rows: int = MAX_ROWS,
) -> dict[str, Any]:
    out_path = out_path or tracker_dir / "ECO Systems General Ledger.csv"
    issues: list[str] = []
    visible_ok: list[str] = []
    node_path = shutil.which("node")
    node_available = bool(node_path)
    gql_bridge_exists = gql_bridge.exists()
    gql_bridge_readable = os.access(gql_bridge, os.R_OK) if gql_bridge_exists else False
    tracker_parent = out_path.parent
    tracker_parent_exists = tracker_parent.exists()
    tracker_parent_writable = os.access(tracker_parent, os.W_OK) if tracker_parent_exists else False
    reports_parent = reports_dir.parent
    reports_parent_exists = reports_parent.exists()
    reports_parent_writable = os.access(reports_parent, os.W_OK) if reports_parent_exists else False

    if not node_available:
        issues.append("node executable is not available for Baselane CDP GraphQL bridge")
    if not gql_bridge_exists:
        issues.append(f"Baselane CDP GraphQL bridge missing: {gql_bridge}")
    elif not gql_bridge_readable:
        issues.append(f"Baselane CDP GraphQL bridge is not readable: {gql_bridge}")
    if not tracker_parent_exists:
        issues.append(f"ledger output parent does not exist: {tracker_parent}")
    elif not tracker_parent_writable:
        issues.append(f"ledger output parent is not writable: {tracker_parent}")
    if not reports_parent_exists:
        issues.append(f"reports parent does not exist: {reports_parent}")
    elif not reports_parent_writable:
        issues.append(f"reports parent is not writable: {reports_parent}")
    if min_rows < 0:
        issues.append(f"BASELANE_MIN_ROWS must be non-negative: {min_rows}")
    if max_rows < min_rows:
        issues.append(f"BASELANE_MAX_ROWS must be >= BASELANE_MIN_ROWS: {max_rows} < {min_rows}")
    if expected_selected < 0:
        issues.append(f"BASELANE_EXPECTED_SELECTED must be non-negative: {expected_selected}")

    if not issues:
        visible_ok.append(
            "OK Baselane CDP ledger export config: "
            f"bridge={gql_bridge} canonical={out_path} min_rows={min_rows} max_rows={max_rows}"
        )
        visible_ok.append(
            "OK Baselane CDP ledger export diagnostic: "
            "no Node bridge subprocess, temp payload write, CSV write, guard write, alert write, restart, sudo, OAuth, cleanup, or helper command"
        )

    classification = "baselane-export-ledger-cdp-review" if issues else "ok"
    evidence = {
        "node_available": node_available,
        "gql_bridge_exists": gql_bridge_exists,
        "gql_bridge_readable": gql_bridge_readable,
        "tracker_parent_exists": tracker_parent_exists,
        "tracker_parent_writable": tracker_parent_writable,
        "reports_parent_exists": reports_parent_exists,
        "reports_parent_writable": reports_parent_writable,
        "min_rows": min_rows,
        "max_rows": max_rows,
    }
    classified_issues = classified_issue_records(issues, evidence, classification)
    fields = remediation_fields(classification)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "BASELANE_EXPORT_LEDGER_CDP_REVIEW" if issues else "NO_REPLY",
        "classification": classification,
        "ok": visible_ok,
        "ok_state": not issues,
        "visible_ok": visible_ok,
        "ok_count": len(visible_ok),
        "issues": issues,
        "issue_count": len(issues),
        "issue_classes": [ISSUE_CLASS] if issues else [],
        "classified_issues": classified_issues,
        "advisory_count": 0,
        "review_required_count": len(classified_issues),
        "workspace_root": str(WORKSPACE_ROOT),
        "node_available": node_available,
        "node_path": node_path,
        "gql_bridge": str(gql_bridge),
        "gql_bridge_exists": gql_bridge_exists,
        "gql_bridge_readable": gql_bridge_readable,
        "tracker_dir": str(tracker_dir),
        "tracker_dir_exists": tracker_dir.exists(),
        "tracker_parent_exists": tracker_parent_exists,
        "tracker_parent_writable": tracker_parent_writable,
        "canonical_path": str(out_path),
        "canonical_exists": out_path.exists(),
        "reports_dir": str(reports_dir),
        "reports_dir_exists": reports_dir.exists(),
        "reports_parent_exists": reports_parent_exists,
        "reports_parent_writable": reports_parent_writable,
        "guard_report_path": str(reports_dir / "baselane_export_guard_last.json"),
        "expected_selected": expected_selected if expected_selected > 0 else None,
        "min_rows": min_rows,
        "max_rows": max_rows,
        "excluded_property_name_count": len(core.EXCLUDE_RAW),
        "node_bridge_attempted": False,
        "temp_payload_write_attempted": False,
        "csv_write_attempted": False,
        "guard_write_attempted": False,
        "alert_write_attempted": False,
        "remediation": {"classification": fields["remediation_class"], **fields},
        **fields,
    }
    report["classified_issue_summary"] = classified_issue_summary(report)
    summary = report["classified_issue_summary"]
    report["safe_review_command_count"] = summary["safe_review_command_count"]
    report["valid_review_command_count"] = summary["valid_review_command_count"]
    report["invalid_review_command_count"] = summary["invalid_review_command_count"]
    report["review_command_validation_issues"] = summary["review_command_validation_issues"]
    return report


def gql_via_cdp(
    operation_name: str,
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    gql_bridge: Path = GQL_BRIDGE,
) -> dict[str, Any]:
    payload = {"operationName": operation_name, "variables": variables or {}, "query": query}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as temp_file:
        json.dump(payload, temp_file)
        temp_path = temp_file.name
    try:
        last_detail = ""
        for attempt in range(1, 4):
            try:
                response = subprocess.run(
                    ["node", str(gql_bridge), temp_path],
                    text=True,
                    capture_output=True,
                    timeout=180,
                )
            except subprocess.TimeoutExpired as exc:
                last_detail = str(exc)
            else:
                if response.returncode == 0:
                    try:
                        body = json.loads(response.stdout)
                    except json.JSONDecodeError as exc:
                        last_detail = f"invalid JSON response: {exc}"
                    else:
                        if body.get("errors"):
                            raise RuntimeError(f"GraphQL {operation_name} errors: {body['errors']}")
                        return body.get("data", {})
                else:
                    last_detail = (response.stderr or response.stdout)[-800:]
            if attempt < 3:
                time.sleep(0.5 * attempt)
        raise RuntimeError(
            f"GraphQL bridge failed for {operation_name} after 3 attempts: {last_detail}"
        )
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def run_export(
    *,
    tracker_dir: Path = TRACKER_DIR,
    reports_dir: Path = REPORTS_DIR,
    out_path: Path | None = None,
    gql_bridge: Path = GQL_BRIDGE,
    expected_selected: int = EXPECTED_SELECTED,
    min_rows: int = MIN_ROWS,
    max_rows: int = MAX_ROWS,
    gql_func=None,
    stdout: TextIO | None = None,
) -> int:
    selected_gql = gql_func or (lambda operation, query, variables=None: gql_via_cdp(operation, query, variables, gql_bridge=gql_bridge))
    return core.run_export(
        app_check="cdp-bridge",
        bsession="cdp-bridge",
        tracker_dir=tracker_dir,
        reports_dir=reports_dir,
        out_path=out_path,
        expected_selected=expected_selected,
        min_rows=min_rows,
        max_rows=max_rows,
        gql_func=selected_gql,
        stdout=stdout,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run or inspect the Baselane CDP ledger export")
    parser.add_argument("--json", action="store_true", help="Emit a read-only diagnostic report and do not run the Node bridge or write files")
    parser.add_argument("--tracker-dir", default=str(TRACKER_DIR), help="Ledger output directory")
    parser.add_argument("--reports-dir", default=str(REPORTS_DIR), help="Report output directory")
    parser.add_argument("--out-path", default=None, help="Canonical ledger CSV output path")
    parser.add_argument("--gql-bridge", default=str(GQL_BRIDGE), help="Node CDP GraphQL bridge path")
    parser.add_argument("--expected-selected", type=int, default=EXPECTED_SELECTED)
    parser.add_argument("--min-rows", type=int, default=MIN_ROWS)
    parser.add_argument("--max-rows", type=int, default=MAX_ROWS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    tracker_dir = Path(args.tracker_dir)
    reports_dir = Path(args.reports_dir)
    out_path = Path(args.out_path) if args.out_path else tracker_dir / "ECO Systems General Ledger.csv"
    gql_bridge = Path(args.gql_bridge)
    if args.json:
        report = build_report(
            tracker_dir=tracker_dir,
            reports_dir=reports_dir,
            out_path=out_path,
            gql_bridge=gql_bridge,
            expected_selected=args.expected_selected,
            min_rows=args.min_rows,
            max_rows=args.max_rows,
        )
        print(json.dumps(report, indent=2, sort_keys=True), file=stdout or sys.stdout)
        return 0 if report["status"] == "NO_REPLY" else 1

    return run_export(
        tracker_dir=tracker_dir,
        reports_dir=reports_dir,
        out_path=out_path,
        gql_bridge=gql_bridge,
        expected_selected=args.expected_selected,
        min_rows=args.min_rows,
        max_rows=args.max_rows,
        stdout=stdout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
