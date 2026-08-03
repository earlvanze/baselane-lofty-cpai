#!/usr/bin/env python3
"""Promote a replay-verified Citadel capture HAR to the canonical HAR path."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

SCRIPT_PATH = Path(__file__).absolute()
WORKSPACE_ROOT = SCRIPT_PATH.parents[1]
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from stable_json_report import stable_report_digest, write_json_report
import mortgage_workflow_har_intake_analyzer as har_intake

DEFAULT_REPORTS = WORKSPACE_ROOT / "reports"
DEFAULT_CAPTURE_HAR = DEFAULT_REPORTS / "citadel_replay_capture.har"
DEFAULT_CAPTURE_REPORT = DEFAULT_REPORTS / "citadel_replay_capture_report.json"
DEFAULT_CANONICAL_HAR = Path("/mnt/f/har/citadel_loansphereservicingdigital.bkiconnect.com.har")
DEFAULT_REPORT = DEFAULT_REPORTS / "citadel_verified_capture_install_report.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def nonempty(value: Any) -> bool:
    return bool(value) if isinstance(value, (list, dict, str)) else False


def countish(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def paths_match(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)
    except Exception:
        return str(left) == str(right)


def summarized_har_analysis(analysis: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(analysis, dict):
        return None
    keys = [
        "status",
        "reason",
        "har_path",
        "har_path_exists",
        "entry_count",
        "embedded_response_body_count",
        "missing_response_body_count",
        "actionable_missing_response_body_count",
        "missing_response_body_paths",
        "missing_response_body_path_counts",
        "response_body_requirements",
        "candidate_endpoint_count",
        "candidate_statement_endpoint_count",
        "candidate_document_endpoint_count",
        "candidate_pdf_response_count",
        "candidate_json_response_count",
        "required_capture_quality",
        "suggested_next_action",
        "safe_to_build_downloader_automatically",
    ]
    return {key: analysis.get(key) for key in keys if key in analysis}


def summarized_capture_report(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None
    keys = [
        "status",
        "reason",
        "target_month",
        "target_month_replayable_document_available",
        "target_month_replayable_document_payload_count",
        "replayable_document_payload_count",
        "replayable_statement_months",
        "statement_document_months",
        "required_response_paths",
        "required_response_path_counts",
        "captured_required_response_paths",
        "captured_required_response_path_counts",
        "missing_required_response_paths",
        "missing_required_response_path_counts",
        "missing_response_body_requirement_count",
        "missing_response_body_requirements",
        "source_required_response_candidate_count",
        "source_required_response_path_counts",
        "source_required_response_fetched_count",
        "source_required_response_replayable_count",
        "source_direct_pdf_candidate_count",
        "source_direct_pdf_path_counts",
        "source_direct_pdf_fetched_count",
        "source_direct_pdf_replayable_count",
        "direct_pdf_response_count",
        "target_month_direct_pdf_filenames",
        "target_month_direct_pdf_body_missing_candidate_count",
    ]
    summary = {key: report.get(key) for key in keys if key in report}
    next_action = report.get("next_action")
    if isinstance(next_action, dict):
        summary["next_action_status"] = next_action.get("status")
        summary["next_action_reason"] = next_action.get("reason")
        summary["next_action_capture_required"] = next_action.get("capture_required")
    return summary


def run_har_analysis(capture_har: Path) -> dict[str, Any] | None:
    if not capture_har.exists() or capture_har.stat().st_size <= 0:
        return None
    return har_intake.analyze_har(
        capture_har,
        property_name="90 Madison Ave",
        selection_reason="install_verified_capture_candidate",
    )


def verification_blockers(
    capture_report: dict[str, Any] | None,
    *,
    capture_report_path: Path,
    capture_har: Path,
    har_analysis: dict[str, Any] | None,
) -> list[str]:
    blockers: list[str] = []
    if not isinstance(capture_report, dict):
        blockers.append("capture_report_missing_or_unreadable")
    else:
        if capture_report.get("status") != "ok":
            blockers.append(f"capture_status={capture_report.get('status')}")
        if capture_report.get("reason"):
            blockers.append(f"capture_reason={capture_report.get('reason')}")
        if countish(capture_report.get("replayable_document_payload_count")) <= 0:
            blockers.append("replayable_document_payload_count=0")
        if (
            "target_month_replayable_document_available" in capture_report
            and capture_report.get("target_month_replayable_document_available") is not True
        ):
            blockers.append("target_month_replayable_document_available=false")
        if (
            "target_month_replayable_document_payload_count" in capture_report
            and countish(capture_report.get("target_month_replayable_document_payload_count")) <= 0
        ):
            blockers.append("target_month_replayable_document_payload_count=0")
        if nonempty(capture_report.get("missing_required_response_paths")):
            blockers.append("missing_required_response_paths")
        if nonempty(capture_report.get("missing_required_response_path_counts")):
            blockers.append("missing_required_response_path_counts")
        if countish(capture_report.get("missing_response_body_requirement_count")) > 0:
            blockers.append("missing_response_body_requirement_count")
        if nonempty(capture_report.get("missing_response_body_requirements")):
            blockers.append("missing_response_body_requirements")
        next_action = capture_report.get("next_action")
        if isinstance(next_action, dict) and next_action.get("capture_required") is not False:
            blockers.append(f"next_action_status={next_action.get('status')}")

        reported_capture_har = capture_report.get("capture_har_path")
        if isinstance(reported_capture_har, str) and reported_capture_har.strip():
            reported_path = Path(reported_capture_har.strip())
            if not paths_match(reported_path, capture_har):
                blockers.append("capture_report_har_path_mismatch")

        reported_report_path = capture_report.get("report_path")
        if isinstance(reported_report_path, str) and reported_report_path.strip():
            reported_path = Path(reported_report_path.strip())
            if not paths_match(reported_path, capture_report_path):
                blockers.append("capture_report_path_mismatch")

    if not isinstance(har_analysis, dict):
        blockers.append("har_intake_analysis_not_run")
        return blockers
    if har_analysis.get("status") != "ok":
        blockers.append(f"har_intake_status={har_analysis.get('status')}")
    if har_analysis.get("reason"):
        blockers.append(f"har_intake_reason={har_analysis.get('reason')}")
    if countish(har_analysis.get("actionable_missing_response_body_count")) > 0:
        blockers.append("har_intake_actionable_missing_response_body_count")
    if nonempty(har_analysis.get("missing_response_body_paths")):
        blockers.append("har_intake_missing_response_body_paths")
    if nonempty(har_analysis.get("missing_response_body_path_counts")):
        blockers.append("har_intake_missing_response_body_path_counts")
    if nonempty(har_analysis.get("response_body_requirements")):
        blockers.append("har_intake_response_body_requirements")
    return blockers


def backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.name}.bak-{stamp}")


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    capture_har = args.capture_har
    capture_report_path = args.capture_report
    canonical_har = args.canonical_har
    capture_report = load_json(capture_report_path)
    capture_hash = file_sha256(capture_har) if capture_har.exists() else None
    canonical_hash = file_sha256(canonical_har) if canonical_har.exists() else None
    har_analysis = run_har_analysis(capture_har)
    blockers = verification_blockers(
        capture_report,
        capture_report_path=capture_report_path,
        capture_har=capture_har,
        har_analysis=har_analysis,
    )
    if not capture_har.exists():
        blockers.append("capture_har_missing")
    elif capture_har.stat().st_size <= 0:
        blockers.append("capture_har_empty")

    already_installed = bool(capture_hash and canonical_hash and capture_hash == canonical_hash)
    report: dict[str, Any] = {
        "job": "install-verified-citadel-capture-har",
        "generated_at": utc_now(),
        "capture_har": str(capture_har),
        "capture_har_exists": capture_har.exists(),
        "capture_har_size": capture_har.stat().st_size if capture_har.exists() else None,
        "capture_har_sha256": capture_hash,
        "capture_report": str(capture_report_path),
        "capture_report_readable": isinstance(capture_report, dict),
        "capture_report_capture_har_path": capture_report.get("capture_har_path") if isinstance(capture_report, dict) else None,
        "capture_report_summary": summarized_capture_report(capture_report),
        "har_intake_analysis": summarized_har_analysis(har_analysis),
        "canonical_har": str(canonical_har),
        "canonical_har_exists_before": canonical_har.exists(),
        "canonical_har_size_before": canonical_har.stat().st_size if canonical_har.exists() else None,
        "canonical_har_sha256_before": canonical_hash,
        "verification_blockers": blockers,
        "verification_blocker_count": len(blockers),
        "apply": args.apply,
        "already_installed": already_installed,
        "installed": False,
        "backup_path": None,
        "review_command_safe_to_run_automatically": True,
        "safe_to_run_automatically": False,
    }
    if blockers:
        report.update(status="review", reason="capture_not_verified_for_install")
        return report
    if already_installed:
        report.update(status="ok", reason="already_installed", safe_to_run_automatically=True)
        return report
    if not args.apply:
        report.update(status="ok", reason="ready_to_install", would_install=True)
        return report

    canonical_har.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if canonical_har.exists():
        backup = backup_path(canonical_har)
        shutil.copy2(canonical_har, backup)
    tmp_path = canonical_har.with_name(f".{canonical_har.name}.tmp")
    shutil.copy2(capture_har, tmp_path)
    tmp_path.replace(canonical_har)
    report.update(
        status="ok",
        reason="installed_verified_capture",
        installed=True,
        backup_path=str(backup) if backup else None,
        canonical_har_exists_after=canonical_har.exists(),
        canonical_har_size_after=canonical_har.stat().st_size if canonical_har.exists() else None,
        canonical_har_sha256_after=file_sha256(canonical_har),
        safe_to_run_automatically=True,
    )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-har", type=Path, default=DEFAULT_CAPTURE_HAR)
    parser.add_argument("--capture-report", type=Path, default=DEFAULT_CAPTURE_REPORT)
    parser.add_argument("--canonical-har", type=Path, default=DEFAULT_CANONICAL_HAR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    report = build_report(args)
    report["idempotency_digest"] = stable_report_digest(report)
    report = write_json_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
