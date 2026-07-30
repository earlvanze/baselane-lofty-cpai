#!/usr/bin/env python3
"""Run no-apply Baselane duplicate preflight for queued Aligned months.

This wrapper is intentionally read-only. It invokes the existing per-month
Aligned owner-statement importer without ``--apply`` for every month in the
reviewed queue, then writes an aggregate report that can be used as a live
import gate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def workspace_root() -> Path:
    for candidate in (
        os.environ.get("WORKSPACE_ROOT"),
        "/home/digit/.openclaw/workspace",
        "/home/umbrel/.openclaw/workspace",
        str(Path(__file__).resolve().parents[1]),
    ):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return Path(__file__).resolve().parents[1]


ROOT = workspace_root()
DEFAULT_QUEUE = ROOT / "config" / "aligned_owner_statement_backfill_queue.json"
DEFAULT_CONFIG = ROOT / "config" / "aligned_owner_statement_imports.json"
DEFAULT_IMPORTER = ROOT / "scripts" / "baselane_aligned_owner_statement_import.py"
DEFAULT_REPORT_DIR = ROOT / "reports" / "aligned-owner-statement-live-preflight"
DEFAULT_MANIFEST_DIR = ROOT / "reports" / "aligned-owner-statement-import-manifests"
RUN_DIR_RE = re.compile(r"^aligned-monthly-import-(\d{4}-\d{2})-\d{8}T\d{6}Z$")


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def month_report_path(report_dir: Path, month: str) -> Path:
    return report_dir / f"baselane_aligned_owner_statement_import_{month}.json"


def queue_months(queue: dict[str, Any]) -> list[str]:
    months = [str(month).strip() for month in queue.get("months") or [] if str(month).strip()]
    expected_plan = queue.get("expected_plan") if isinstance(queue.get("expected_plan"), dict) else {}
    plan_months = expected_plan.get("months") if isinstance(expected_plan.get("months"), dict) else {}
    for month in sorted(plan_months):
        if month not in months:
            months.append(str(month))
    return months


def run_command_with_timeout(command: list[str], timeout_seconds: int) -> tuple[int, str, str, bool]:
    timeout = timeout_seconds if timeout_seconds > 0 else None
    proc = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout or "", stderr or "", False
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
        combined_stdout = stdout or exc.stdout or ""
        combined_stderr = stderr or exc.stderr or ""
        return 124, combined_stdout, combined_stderr, True


def looks_like_query_error(error: Any) -> bool:
    text = str(error or "")
    lowered = text.lower()
    return bool(
        text
        and (
            "unauthorized_access" in lowered
            or "missing cookie" in lowered
            or "auth_required" in lowered
            or "timed out waiting for x-firebase-appcheck" in lowered
            or "login" in lowered
        )
    )


def report_needs_staging_fallback(report_path: Path, report: dict[str, Any]) -> bool:
    if not report_path.is_file():
        return True
    if not report:
        return True
    if str(report.get("status") or "").strip().lower() != "ok":
        return True
    if looks_like_query_error(report.get("query_error")):
        return True
    return False


def fresh_successful_live_report(report_path: Path, report: dict[str, Any], started_at: float) -> bool:
    if report_needs_staging_fallback(report_path, report):
        return False
    try:
        if report_path.stat().st_mtime < started_at - 1:
            return False
    except FileNotFoundError:
        return False
    if report.get("apply") is True:
        return False
    if int_field(report, "created_count"):
        return False
    return True


def run_month(args: argparse.Namespace, queue: dict[str, Any], month: str) -> dict[str, Any]:
    report_path = month_report_path(args.report_dir, month)
    expected = queue.get("expected") if isinstance(queue.get("expected"), dict) else {}
    property_id = str(args.property_id or expected.get("baselane_property_id") or "").strip()
    command = [
        str(args.python_bin),
        str(args.importer),
        "--config",
        str(args.config),
        "--month",
        month,
        "--report",
        str(report_path),
        "--manifest-dir",
        str(args.manifest_dir),
        "--expected-plan-queue",
        str(args.queue),
        "--convert",
        "--skip-settlement-relabels",
    ]
    if property_id:
        command.extend(["--property-id", property_id])

    command_started_at = time.time()
    return_code, stdout, stderr, timed_out = run_command_with_timeout(command, args.per_month_timeout_seconds)
    command_timed_out = timed_out
    fallback: dict[str, Any] | None = None
    live_report = read_json(report_path)
    fresh_live_success = fresh_successful_live_report(report_path, live_report, command_started_at)
    needs_fallback = report_needs_staging_fallback(report_path, live_report)
    if fresh_live_success:
        timed_out = False
    if (command_timed_out and not fresh_live_success) or (return_code != 0 and needs_fallback):
        if timed_out:
            stderr = (stderr or "") + f"\npreflight month timed out after {args.per_month_timeout_seconds}s"
        else:
            stderr = (stderr or "") + "\npreflight live duplicate query failed; running no-query staging fallback"
        fallback_command = command + ["--skip-baselane-query"]
        fallback_return_code, fallback_stdout, fallback_stderr, fallback_timed_out = run_command_with_timeout(
            fallback_command,
            args.staging_fallback_timeout_seconds,
        )
        if fallback_timed_out:
            fallback_stderr = (
                (fallback_stderr or "")
                + f"\npreflight staging fallback timed out after {args.staging_fallback_timeout_seconds}s"
            )
        fallback = {
            "command": fallback_command,
            "return_code": fallback_return_code,
            "timed_out": fallback_timed_out,
            "stdout_tail": (fallback_stdout or "")[-4000:],
            "stderr_tail": (fallback_stderr or "")[-4000:],
            "report_exists": report_path.is_file(),
            "live_report_before_fallback": {
                "status": live_report.get("status"),
                "query_error": live_report.get("query_error"),
                "planned_count": live_report.get("planned_count"),
                "to_create_count": live_report.get("to_create_count"),
                "created_count": live_report.get("created_count"),
            },
        }
    report = read_json(report_path)
    effective_return_code = return_code
    if fresh_live_success:
        effective_return_code = 0
    if fallback and fallback.get("report_exists"):
        effective_return_code = int(fallback.get("return_code") or 0)
    return {
        "month": month,
        "command": command,
        "return_code": return_code,
        "effective_return_code": effective_return_code,
        "timed_out": timed_out,
        "command_timed_out": command_timed_out,
        "fresh_successful_live_report": fresh_live_success,
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
        "used_staging_fallback": bool(fallback and fallback.get("report_exists")),
        "staging_fallback": fallback,
        "report_path": str(report_path),
        "report_exists": report_path.is_file(),
        "report": report,
    }


def directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() or item.is_symlink():
                total += item.lstat().st_size
        except FileNotFoundError:
            continue
    return total


def prune_run_artifacts(report_dir: Path, keep_per_month: int) -> dict[str, Any]:
    root = report_dir / "aligned-owner-statement-import"
    record: dict[str, Any] = {
        "enabled": keep_per_month >= 0,
        "root": str(root),
        "keep_per_month": keep_per_month,
        "exists": root.is_dir(),
        "pruned_count": 0,
        "pruned_bytes": 0,
        "kept_count": 0,
        "errors": [],
    }
    if keep_per_month < 0 or not root.is_dir():
        return record

    grouped: dict[str, list[Path]] = {}
    for child in root.iterdir():
        if not child.is_dir():
            continue
        match = RUN_DIR_RE.match(child.name)
        if not match:
            continue
        grouped.setdefault(match.group(1), []).append(child)

    for month, paths in sorted(grouped.items()):
        paths = sorted(paths, key=lambda path: path.name, reverse=True)
        keep = set(paths[:keep_per_month])
        record["kept_count"] += len(keep)
        for path in paths[keep_per_month:]:
            size = directory_size(path)
            try:
                shutil.rmtree(path)
            except Exception as exc:
                record["errors"].append({"month": month, "path": str(path), "error": str(exc)})
                continue
            record["pruned_count"] += 1
            record["pruned_bytes"] += size
    return record


def int_field(report: dict[str, Any], key: str) -> int:
    try:
        return int(report.get(key) or 0)
    except Exception:
        return 0


def query_error_class(error: Any) -> str | None:
    text = str(error or "")
    if not text:
        return None
    if looks_like_query_error(error):
        return "auth_required"
    return "query_error"


def append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def evaluate(queue: dict[str, Any], months: list[str], results: list[dict[str, Any]]) -> dict[str, Any]:
    reasons: list[str] = []
    query_error_months: list[str] = []
    auth_error_months: list[str] = []
    pre_fallback_query_error_months: list[str] = []
    pre_fallback_auth_error_months: list[str] = []
    non_ok_months: list[str] = []
    missing_report_months: list[str] = []
    timed_out_months: list[str] = []
    used_staging_fallback_months: list[str] = []
    live_duplicate_query_months: list[str] = []
    expected_plan_non_ok_months: list[str] = []
    apply_true_months: list[str] = []
    created_months: list[str] = []
    duplicate_key_months: list[str] = []

    planned_count_total = 0
    to_create_count_total = 0
    created_count_total = 0
    existing_key_count_total = 0
    skipped_existing_count_total = 0
    apply_all_false = True

    for result in results:
        month = str(result.get("month") or "")
        report = result.get("report") if isinstance(result.get("report"), dict) else {}
        if result.get("timed_out"):
            timed_out_months.append(month)
        if result.get("used_staging_fallback"):
            used_staging_fallback_months.append(month)
        fallback = result.get("staging_fallback") if isinstance(result.get("staging_fallback"), dict) else {}
        live_before_fallback = (
            fallback.get("live_report_before_fallback")
            if isinstance(fallback.get("live_report_before_fallback"), dict)
            else {}
        )
        pre_fallback_error_class = query_error_class(live_before_fallback.get("query_error"))
        if pre_fallback_error_class:
            append_unique(pre_fallback_query_error_months, month)
            append_unique(query_error_months, month)
        if pre_fallback_error_class == "auth_required":
            append_unique(pre_fallback_auth_error_months, month)
            append_unique(auth_error_months, month)
        if not result.get("report_exists"):
            missing_report_months.append(month)
            continue
        status = str(report.get("status") or "unknown")
        effective_return_code = result.get("effective_return_code")
        if effective_return_code is None:
            effective_return_code = result.get("return_code")
        if int(effective_return_code or 0) != 0 or status != "ok":
            non_ok_months.append(month)
        error_class = query_error_class(report.get("query_error"))
        if error_class:
            append_unique(query_error_months, month)
        if error_class == "auth_required":
            append_unique(auth_error_months, month)
        if report.get("apply"):
            apply_all_false = False
            apply_true_months.append(month)
        if int_field(report, "created_count"):
            created_months.append(month)
        plan_check = report.get("expected_plan_check") if isinstance(report.get("expected_plan_check"), dict) else {}
        if plan_check.get("status") != "ok":
            expected_plan_non_ok_months.append(month)

        planned_count_total += int_field(report, "planned_count")
        to_create_count_total += int_field(report, "to_create_count")
        created_count_total += int_field(report, "created_count")
        existing_key_count_total += int_field(report, "existing_key_count")
        skipped_existing_count_total += int_field(report, "skipped_existing_count")
        if not result.get("used_staging_fallback") and not result.get("timed_out") and not error_class and status == "ok":
            live_duplicate_query_months.append(month)

    expected = queue.get("expected") if isinstance(queue.get("expected"), dict) else {}
    expected_to_create = expected.get("to_create_count")
    expected_remaining_or_existing_total = to_create_count_total + skipped_existing_count_total
    to_create_matches_expected = (
        expected_to_create is None
        or expected_remaining_or_existing_total == int(expected_to_create or 0)
    )

    if missing_report_months:
        reasons.append("missing_month_reports")
    if timed_out_months:
        reasons.append("month_timed_out")
    if used_staging_fallback_months:
        reasons.append("used_staging_fallback")
    if non_ok_months:
        reasons.append("month_status_not_ok")
    if query_error_months:
        reasons.append("query_error")
    if auth_error_months:
        reasons.append("auth_required")
    if expected_plan_non_ok_months:
        reasons.append("expected_plan_not_ok")
    if apply_true_months:
        reasons.append("unexpected_apply_true")
    if created_months or created_count_total:
        reasons.append("unexpected_created_rows")
    if duplicate_key_months:
        reasons.append("duplicate_keys_present")
    if not to_create_matches_expected:
        reasons.append("to_create_count_mismatch")
    if not months:
        reasons.append("queue_has_no_months")

    return {
        "status": "ok" if not reasons else "review",
        "review_reasons": reasons,
        "months_requested": months,
        "month_count": len(months),
        "apply_all_false": apply_all_false,
        "planned_count_total": planned_count_total,
        "to_create_count_total": to_create_count_total,
        "expected_to_create_count": expected_to_create,
        "expected_remaining_or_existing_total": expected_remaining_or_existing_total,
        "to_create_matches_expected": to_create_matches_expected,
        "created_count_total": created_count_total,
        "existing_key_count_total": existing_key_count_total,
        "skipped_existing_count_total": skipped_existing_count_total,
        "query_error_months": query_error_months,
        "auth_error_months": auth_error_months,
        "auth_error_month_count": len(auth_error_months),
        "pre_fallback_query_error_months": pre_fallback_query_error_months,
        "pre_fallback_query_error_month_count": len(pre_fallback_query_error_months),
        "pre_fallback_auth_error_months": pre_fallback_auth_error_months,
        "pre_fallback_auth_error_month_count": len(pre_fallback_auth_error_months),
        "non_ok_months": non_ok_months,
        "missing_report_months": missing_report_months,
        "timed_out_months": timed_out_months,
        "used_staging_fallback_months": used_staging_fallback_months,
        "used_staging_fallback_month_count": len(used_staging_fallback_months),
        "live_duplicate_query_months": live_duplicate_query_months,
        "live_duplicate_query_month_count": len(live_duplicate_query_months),
        "duplicate_check_complete": len(live_duplicate_query_months) == len(months) and bool(months),
        "duplicate_check_trusted_zero": (
            len(live_duplicate_query_months) == len(months)
            and bool(months)
            and not duplicate_key_months
        ),
        "staging_plan_complete": (
            not missing_report_months
            and not expected_plan_non_ok_months
            and not apply_true_months
            and not created_months
            and created_count_total == 0
            and to_create_matches_expected
        ),
        "expected_plan_non_ok_months": expected_plan_non_ok_months,
        "apply_true_months": apply_true_months,
        "created_months": created_months,
        "duplicate_key_months": duplicate_key_months,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--importer", type=Path, default=DEFAULT_IMPORTER)
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--property-id", default=None)
    parser.add_argument("--python-bin", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--per-month-timeout-seconds",
        type=int,
        default=int(os.environ.get("BASELANE_ALIGNED_OWNER_PREFLIGHT_MONTH_TIMEOUT_SECONDS") or 120),
        help="Maximum seconds to allow one read-only importer duplicate query; use 0 to disable",
    )
    parser.add_argument(
        "--staging-fallback-timeout-seconds",
        type=int,
        default=int(os.environ.get("BASELANE_ALIGNED_OWNER_PREFLIGHT_STAGING_FALLBACK_TIMEOUT_SECONDS") or 120),
        help="Maximum seconds for the no-query staging fallback after a duplicate-query timeout",
    )
    parser.add_argument(
        "--keep-run-artifacts-per-month",
        type=int,
        default=1,
        help="Keep this many generated converter run directories per month under report-dir; use -1 to disable pruning",
    )
    parser.add_argument("--prune-only", action="store_true", help="Only prune generated run artifacts and write the summary")
    args = parser.parse_args()

    args.report_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report or (args.report_dir / "summary.json")
    queue = read_json(args.queue)
    months = queue_months(queue)
    results = [] if args.prune_only else [run_month(args, queue, month) for month in months]
    summary = evaluate(queue, months, results)
    prune_record = prune_run_artifacts(args.report_dir, args.keep_run_artifacts_per_month)
    if args.prune_only:
        expected = queue.get("expected") if isinstance(queue.get("expected"), dict) else {}
        summary = {
            "status": "ok" if not prune_record.get("errors") else "review",
            "review_reasons": [] if not prune_record.get("errors") else ["prune_errors"],
            "months_requested": months,
            "month_count": len(months),
            "apply_all_false": True,
            "planned_count_total": 0,
            "to_create_count_total": 0,
            "expected_to_create_count": expected.get("to_create_count"),
            "to_create_matches_expected": None,
            "created_count_total": 0,
            "existing_key_count_total": 0,
            "skipped_existing_count_total": 0,
            "query_error_months": [],
            "auth_error_months": [],
            "auth_error_month_count": 0,
            "pre_fallback_query_error_months": [],
            "pre_fallback_query_error_month_count": 0,
            "pre_fallback_auth_error_months": [],
            "pre_fallback_auth_error_month_count": 0,
            "non_ok_months": [],
            "missing_report_months": [],
            "timed_out_months": [],
            "expected_plan_non_ok_months": [],
            "apply_true_months": [],
            "created_months": [],
            "duplicate_key_months": [],
        }
    report = {
        "job": "baselane-aligned-owner-statement-queue-preflight",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "queue": str(args.queue),
        "queue_id": queue.get("queue_id"),
        "config": str(args.config),
        "importer": str(args.importer),
        "manifest_dir": str(args.manifest_dir),
        "report_dir": str(args.report_dir),
        "reports": [
            {
                "month": result.get("month"),
                "report_path": result.get("report_path"),
                "report_exists": result.get("report_exists"),
                "return_code": result.get("return_code"),
                "effective_return_code": result.get("effective_return_code"),
                "timed_out": result.get("timed_out") is True,
                "command_timed_out": result.get("command_timed_out") is True,
                "fresh_successful_live_report": result.get("fresh_successful_live_report") is True,
                "used_staging_fallback": result.get("used_staging_fallback") is True,
                "status": (result.get("report") or {}).get("status") if isinstance(result.get("report"), dict) else None,
                "query_error": (result.get("report") or {}).get("query_error") if isinstance(result.get("report"), dict) else None,
                "existing_key_count": int_field(result.get("report") or {}, "existing_key_count")
                if isinstance(result.get("report"), dict)
                else 0,
                "skipped_existing_count": int_field(result.get("report") or {}, "skipped_existing_count")
                if isinstance(result.get("report"), dict)
                else 0,
                "to_create_count": int_field(result.get("report") or {}, "to_create_count")
                if isinstance(result.get("report"), dict)
                else 0,
                "created_count": int_field(result.get("report") or {}, "created_count")
                if isinstance(result.get("report"), dict)
                else 0,
            }
            for result in results
        ],
        "run_artifact_prune": prune_record,
        "commands": [
            {
                "month": result.get("month"),
                "command": result.get("command"),
                "return_code": result.get("return_code"),
                "effective_return_code": result.get("effective_return_code"),
                "timed_out": result.get("timed_out") is True,
                "command_timed_out": result.get("command_timed_out") is True,
                "fresh_successful_live_report": result.get("fresh_successful_live_report") is True,
                "used_staging_fallback": result.get("used_staging_fallback") is True,
                "stderr_tail": result.get("stderr_tail"),
                "staging_fallback": result.get("staging_fallback"),
            }
            for result in results
        ],
        **summary,
    }
    write_json(report_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
