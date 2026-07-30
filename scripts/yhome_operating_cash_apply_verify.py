#!/usr/bin/env python3
"""Refresh, audit, optionally apply, and verify Yhome operating cash updates."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).absolute().parent


def default_root() -> Path:
    for raw_candidate in (
        os.environ.get("ROOT"),
        os.environ.get("OPENCLAW_WORKSPACE"),
        str(Path.cwd()),
        str(SCRIPT_DIR.parent),
    ):
        if not raw_candidate:
            continue
        candidate = Path(raw_candidate).absolute()
        if (candidate / "scripts").is_dir() and (candidate / "reports").is_dir():
            return candidate
    return SCRIPT_DIR.parent


ROOT = default_root()
DEFAULT_YHOME_EXPORT_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1HerPv9U7IB47ipCpJ-XshajQWouCUEwfDdkHSfVCwfc/export?format=csv&gid=1187056671"
)
TARGET_COLUMNS = ("Lofty Operating Cash", "ECO Net DAO Funds")
YHOME_GSHEET_SPREADSHEET_ID = "1HerPv9U7IB47ipCpJ-XshajQWouCUEwfDdkHSfVCwfc"
YHOME_GSHEET_SHEET_TITLE = "Cleveland"
YHOME_GSHEET_SHEET_SPECS = (
    ("Cleveland", 1187056671),
    ("Chicago & non-Yhome", 433920866),
    ("Yhome Deeded & Sold", 1902489452),
)
YHOME_METADATA_COLUMNS = (
    "__yhome_sheet_title",
    "__yhome_sheet_gid",
    "__yhome_sheet_row_number",
    "__yhome_sheet_lofty_operating_cash_column_index",
    "__yhome_sheet_eco_net_dao_funds_column_index",
)


def generated_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_month() -> str:
    run_month = str(os.environ.get("RUN_MONTH") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", run_month):
        return run_month
    return datetime.now(timezone.utc).strftime("%Y-%m")


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "missing", "path": str(path)}
    except Exception as exc:
        return {"status": "unreadable", "path": str(path), "error": str(exc)}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def command_result(
    command: list[str],
    *,
    timeout_seconds: float,
    env: dict[str, str] | None = None,
    include_stdout: bool = False,
) -> dict[str, Any]:
    started_at = generated_at()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        return {
            "command": command,
            "started_at": started_at,
            "finished_at": generated_at(),
            "return_code": None,
            "timed_out": True,
            "timeout_seconds": timeout_seconds,
            "stdout_tail": (stdout or exc.stdout or "")[-4000:] if isinstance(stdout or exc.stdout, str) else "",
            "stderr_tail": (stderr or exc.stderr or "")[-4000:] if isinstance(stderr or exc.stderr, str) else "",
        }
    payload = {
        "command": command,
        "started_at": started_at,
        "finished_at": generated_at(),
        "return_code": process.returncode,
        "timed_out": False,
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
    }
    if include_stdout:
        payload["stdout"] = stdout
    return payload


def refresh_yhome_csv_from_gws(args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    spreadsheet_id = str(getattr(args, "yhome_gws_spreadsheet_id", "") or YHOME_GSHEET_SPREADSHEET_ID).strip()
    configured_specs = getattr(args, "yhome_gws_sheet_specs", None)
    strict_tab_schema = bool(configured_specs)
    if configured_specs:
        sheet_specs = []
        for raw_spec in configured_specs:
            title, separator, gid = str(raw_spec).partition("=")
            if not separator or not title.strip() or not gid.strip():
                return {"status": "failed", "reason": "invalid_gws_sheet_spec", "sheet_spec": str(raw_spec)}
            try:
                sheet_specs.append((title.strip(), int(gid)))
            except ValueError:
                return {"status": "failed", "reason": "invalid_gws_sheet_gid", "sheet_spec": str(raw_spec)}
    else:
        sheet_specs = list(YHOME_GSHEET_SHEET_SPECS)
    gws_bin = str(getattr(args, "gws_bin", "") or "gws").strip()
    if not spreadsheet_id or not sheet_specs or not gws_bin:
        return {
            "status": "skipped",
            "reason": "gws_refresh_not_configured",
            "path": str(args.yhome_csv),
        }
    command = [
        gws_bin,
        "sheets",
        "spreadsheets",
        "values",
        "batchGet",
        "--params",
        json.dumps(
            {
                "spreadsheetId": spreadsheet_id,
                "ranges": [title for title, _gid in sheet_specs],
                "valueRenderOption": "UNFORMATTED_VALUE",
            }
        ),
        "--format",
        "json",
    ]
    result = command_result(command, timeout_seconds=args.refresh_timeout_seconds, env=env, include_stdout=True)
    if result.get("return_code") != 0:
        return {
            "status": "failed",
            "reason": "gws_refresh_failed",
            "path": str(args.yhome_csv),
            "command": result,
        }
    raw_stdout = str(result.pop("stdout", "") or result.get("stdout_tail") or "{}")
    try:
        payload = json.loads(raw_stdout)
    except Exception as exc:
        return {
            "status": "failed",
            "reason": "gws_refresh_unparseable",
            "path": str(args.yhome_csv),
            "error": str(exc),
            "command": result,
        }
    value_ranges = payload.get("valueRanges") if isinstance(payload, dict) else None
    if not isinstance(value_ranges, list) or len(value_ranges) != len(sheet_specs):
        return {
            "status": "failed",
            "reason": "gws_refresh_missing_sheet_values",
            "path": str(args.yhome_csv),
            "command": result,
        }
    tabs = []
    for (sheet_title, sheet_gid), value_range in zip(sheet_specs, value_ranges, strict=True):
        rows = value_range.get("values") if isinstance(value_range, dict) else None
        if not isinstance(rows, list) or not rows:
            return {
                "status": "failed",
                "reason": "gws_refresh_empty_values",
                "sheet_title": sheet_title,
                "path": str(args.yhome_csv),
                "command": result,
            }
        headers = [str(value or "").strip() for value in rows[0]]
        required_headers = {"Property", "Lofty Operating Cash", "ECO Net DAO Funds"}
        if strict_tab_schema:
            required_headers.add("New PM")
        if not required_headers.issubset(headers):
            return {
                "status": "failed",
                "reason": "gws_refresh_required_columns_missing",
                "sheet_title": sheet_title,
                "missing_columns": sorted(required_headers.difference(headers)),
                "path": str(args.yhome_csv),
                "command": result,
            }
        tabs.append((sheet_title, sheet_gid, headers, rows))
    headers = []
    for _title, _gid, tab_headers, _rows in tabs:
        for header in tab_headers:
            if header and header not in headers:
                headers.append(header)
    headers.extend(YHOME_METADATA_COLUMNS)
    args.yhome_csv.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=args.yhome_csv.parent, delete=False) as handle:
        temporary_path = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for sheet_title, sheet_gid, tab_headers, rows in tabs:
            for source_row_number, values in enumerate(rows[1:], 2):
                row = {header: (values[index] if index < len(values) else "") for index, header in enumerate(tab_headers)}
                if not str(row.get("Property") or "").strip():
                    continue
                row["__yhome_sheet_title"] = sheet_title
                row["__yhome_sheet_gid"] = sheet_gid
                row["__yhome_sheet_row_number"] = source_row_number
                row["__yhome_sheet_lofty_operating_cash_column_index"] = tab_headers.index("Lofty Operating Cash") + 1
                row["__yhome_sheet_eco_net_dao_funds_column_index"] = tab_headers.index("ECO Net DAO Funds") + 1
                writer.writerow(row)
    temporary_path.replace(args.yhome_csv)
    return {
        "status": "ok",
        "reason": "gws_refreshed",
        "path": str(args.yhome_csv),
        "row_count": sum(max(len(rows) - 1, 0) for _title, _gid, _headers, rows in tabs),
        "sheet_count": len(tabs),
        "sheet_titles": [title for title, _gid, _headers, _rows in tabs],
        "spreadsheet_id": spreadsheet_id,
        "sheet_title": tabs[0][0] if len(tabs) == 1 else None,
        "command": result,
    }


def refresh_yhome_csv(args: argparse.Namespace, *, env: dict[str, str] | None = None, prefer_gws: bool = False) -> dict[str, Any]:
    if args.no_refresh:
        return {"status": "skipped", "reason": "no_refresh_requested", "path": str(args.yhome_csv)}
    if prefer_gws:
        gws_refresh = refresh_yhome_csv_from_gws(args, env or dict(os.environ))
        if gws_refresh.get("status") == "ok":
            return gws_refresh
        return {
            "status": "failed",
            "reason": "authenticated_gws_refresh_required",
            "path": str(args.yhome_csv),
            "gws_refresh": gws_refresh,
        }
    if not args.yhome_export_url:
        return {"status": "skipped", "reason": "yhome_export_url_empty", "path": str(args.yhome_csv)}
    args.yhome_csv.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(args.yhome_export_url, timeout=args.refresh_timeout_seconds) as response:
            payload = response.read()
        args.yhome_csv.write_bytes(payload)
    except Exception as exc:
        return {"status": "failed", "reason": "refresh_failed", "path": str(args.yhome_csv), "error": str(exc)}
    return {
        "status": "ok",
        "reason": "refreshed",
        "path": str(args.yhome_csv),
        "byte_count": len(payload),
    }


def audit_command(args: argparse.Namespace) -> list[str]:
    command = [
        args.python_bin,
        str(args.audit_script),
        "--month",
        args.month,
        "--candidate-packet",
        str(args.candidate_packet),
        "--yhome-csv",
        str(args.yhome_csv),
        "--report",
        str(args.audit_report),
        "--yhome-plan-csv",
        str(args.plan_csv),
    ]
    if getattr(args, "yhome_missing_candidates_csv", None):
        command.extend(["--yhome-missing-candidates-csv", str(args.yhome_missing_candidates_csv)])
    if args.audit_workbooks:
        command.extend(["--audit-workbooks", "--workbook-timeout-seconds", str(args.workbook_timeout_seconds)])
    if getattr(args, "require_all_yhome_rows", True):
        command.append("--require-all-yhome-rows")
    return command


def updater_command(args: argparse.Namespace) -> list[str]:
    command = [
        args.python_bin,
        str(args.updater_script),
        "--plan-csv",
        str(args.plan_csv),
        "--yhome-csv",
        str(args.yhome_csv),
        "--report",
        str(args.updater_report),
    ]
    if args.apply:
        command.append("--apply")
    return command


def normalized_target_columns(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def report_target_columns(payload: dict[str, Any]) -> list[str]:
    return normalized_target_columns(payload.get("target_columns") or payload.get("yhome_target_columns"))


def target_columns_match(payload: dict[str, Any]) -> bool:
    return report_target_columns(payload) == list(TARGET_COLUMNS)


def target_column_drift_issue(source: str, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "yhome_target_column_drift",
        "source": source,
        "path": str(path),
        "expected": list(TARGET_COLUMNS),
        "observed": report_target_columns(payload),
    }


def candidate_packet_record_count(path: Path) -> int:
    payload = read_json(path)
    records = payload.get("records")
    return len(records) if isinstance(records, list) else 0


def run_audit(args: argparse.Namespace, *, phase: str, env: dict[str, str]) -> dict[str, Any]:
    audit_started_ns = time.time_ns()
    result = command_result(audit_command(args), timeout_seconds=args.audit_timeout_seconds, env=env)
    report = read_json(args.audit_report)
    report_is_fresh = False
    try:
        report_is_fresh = args.audit_report.stat().st_mtime_ns >= audit_started_ns
    except FileNotFoundError:
        pass
    if not report_is_fresh:
        report = {
            "status": "missing",
            "reason": "audit_report_stale_or_missing",
            "path": str(args.audit_report),
        }
    overlay: dict[str, Any] = {
        "policy": "full_property_split_ecogl_column_e_all_rows_v1",
        "action": "preserve_full_gl_audit_plan",
    }
    merged_update_count = int(report.get("yhome_update_required_count") or 0)
    return {
        "phase": phase,
        "command": result,
        "report_status": report.get("status"),
        "issue_count": int(report.get("issue_count") or 0),
        "yhome_update_required_count": merged_update_count,
        "yhome_missing_candidate_count": int(
            report.get("yhome_missing_candidate_count") or report.get("yhome_unmatched_candidate_count") or 0
        ),
        "yhome_missing_candidates": (
            report.get("yhome_missing_candidates") or report.get("yhome_unmatched_candidates") or []
        )[:100],
        "yhome_required_states": report.get("yhome_required_states") or [],
        "yhome_excluded_candidate_count": int(report.get("yhome_excluded_candidate_count") or 0),
        "yhome_excluded_candidates": (
            report.get("yhome_excluded_candidates") if isinstance(report.get("yhome_excluded_candidates"), list) else []
        )[:100],
        "target_columns": report_target_columns(report),
        "report_is_fresh": report_is_fresh,
        "report_path": str(args.audit_report),
        "report": report,
        "eco_cash_policy_overlay": overlay,
    }


def run_updater(args: argparse.Namespace, *, env: dict[str, str]) -> dict[str, Any]:
    result = command_result(updater_command(args), timeout_seconds=args.updater_timeout_seconds, env=env)
    report = read_json(args.updater_report)
    return {
        "command": result,
        "report_status": report.get("status"),
        "reason": report.get("reason"),
        "update_count": int(report.get("update_count") or 0),
        "request_count": int(report.get("request_count") or 0),
        "applied_update_count": int(report.get("applied_update_count") or 0),
        "apply_requested": bool(report.get("apply_requested")),
        "write_enabled": bool(report.get("write_enabled")),
        "apply_allowed": bool(report.get("apply_allowed")),
        "dry_run": bool(report.get("dry_run")),
        "target_columns": report_target_columns(report),
        "report_path": str(args.updater_report),
        "report": report,
    }


def finish_report(report: dict[str, Any], *, status: str, reason: str) -> dict[str, Any]:
    report["status"] = status
    report["reason"] = reason
    if status == "review" and reason == "dry_run_updates_required":
        report["next_action"] = (
            "Review reports/yhome_operating_cash_update_plan.csv, then run "
            "YHOME_GSHEET_WRITE_ENABLED=1 python3 scripts/yhome_operating_cash_apply_verify.py --apply "
            "to update only Lofty Operating Cash and ECO Net DAO Funds, followed by post-apply verification."
        )
    elif status == "review" and reason == "post_apply_yhome_updates_still_required":
        report["next_action"] = (
            "Yhome operating cash values still differ after apply; inspect the post-audit report and rerun apply/verify only after confirming the sheet refreshed."
        )
    elif status == "review" and "target_column_drift" in reason:
        report["next_action"] = "Stop Yhome writes; target columns drifted from Lofty Operating Cash and ECO Net DAO Funds."
    elif status == "review":
        report["next_action"] = "Inspect the non-authoritative Yhome work-product report and resolve its review reason before the next spreadsheet refresh."
    elif status == "failed":
        report["next_action"] = "Fix the failed Yhome work-product apply/verify step and rerun it independently; other pipeline outputs remain eligible."
    else:
        report["next_action"] = None
    report["finished_at"] = generated_at()
    return report


def build_report(args: argparse.Namespace, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    run_env = dict(os.environ if env is None else env)
    write_enabled = run_env.get("YHOME_GSHEET_WRITE_ENABLED") == "1"
    apply_allowed = bool(args.apply and write_enabled)
    report: dict[str, Any] = {
        "job": "yhome-operating-cash-apply-verify",
        "generated_at": generated_at(),
        "status": "review",
        "month": args.month,
        "candidate_packet": str(args.candidate_packet),
        "yhome_csv": str(args.yhome_csv),
        "yhome_export_url_configured": bool(args.yhome_export_url),
        "audit_report": str(args.audit_report),
        "plan_csv": str(args.plan_csv),
        "yhome_missing_candidates_csv": str(args.yhome_missing_candidates_csv),
        "updater_report": str(args.updater_report),
        "target_columns": list(TARGET_COLUMNS),
        "target_column_policy": "Yhome Google Sheet writes are limited to Lofty Operating Cash and ECO Net DAO Funds.",
        "apply_requested": bool(args.apply),
        "write_enabled": write_enabled,
        "apply_allowed": apply_allowed,
        "dry_run": not apply_allowed,
        "write_gate": {
            "apply_requested": bool(args.apply),
            "write_enabled": write_enabled,
            "apply_allowed": apply_allowed,
            "required_env": "YHOME_GSHEET_WRITE_ENABLED=1",
            "target_columns": list(TARGET_COLUMNS),
        },
        "external_write_attempted": False,
        "post_apply_verification_required": True,
        "post_apply_verification_ok": False,
        "artifacts": {
            "audit_report": str(args.audit_report),
            "plan_csv": str(args.plan_csv),
            "yhome_missing_candidates_csv": str(args.yhome_missing_candidates_csv),
            "updater_report": str(args.updater_report),
        },
        "refreshes": [],
        "commands": [],
        "issues": [],
    }
    report["candidate_packet_record_count"] = candidate_packet_record_count(args.candidate_packet)
    if report["candidate_packet_record_count"] <= 0:
        report["issues"].append(
            {
                "type": "candidate_packet_empty",
                "path": str(args.candidate_packet),
                "reason": "Yhome operating cash audit requires non-empty monthly candidate packet coverage.",
            }
        )
        return finish_report(report, status="review", reason="candidate_packet_empty")

    pre_refresh = refresh_yhome_csv(
        args,
        env=run_env,
        prefer_gws=bool(getattr(args, "yhome_gws_sheet_specs", None)) or bool(args.apply),
    )
    report["refreshes"].append({"phase": "pre_audit", **pre_refresh})
    if pre_refresh["status"] == "failed":
        return finish_report(report, status="failed", reason="pre_refresh_failed")

    pre_audit = run_audit(args, phase="pre_update", env=run_env)
    report["commands"].append(pre_audit["command"])
    report.update(
        {
            "pre_audit_status": pre_audit["report_status"],
            "pre_audit_issue_count": pre_audit["issue_count"],
            "pre_yhome_update_required_count": pre_audit["yhome_update_required_count"],
            "pre_yhome_missing_candidate_count": pre_audit["yhome_missing_candidate_count"],
            "pre_yhome_missing_candidates": pre_audit["yhome_missing_candidates"],
            "pre_yhome_required_states": pre_audit["yhome_required_states"],
            "pre_yhome_excluded_candidate_count": pre_audit["yhome_excluded_candidate_count"],
            "pre_yhome_excluded_candidates": pre_audit["yhome_excluded_candidates"],
            "pre_audit_target_columns": pre_audit["target_columns"],
        }
    )
    if pre_audit["command"].get("timed_out"):
        return finish_report(report, status="failed", reason="pre_audit_timed_out")
    if pre_audit["report_status"] in {"missing", "unreadable"}:
        return finish_report(report, status="failed", reason="pre_audit_report_unavailable")
    if not target_columns_match(pre_audit["report"]):
        report["issues"].append(target_column_drift_issue("pre_audit", args.audit_report, pre_audit["report"]))
        return finish_report(report, status="review", reason="pre_audit_target_column_drift")
    if pre_audit["yhome_missing_candidate_count"] > 0 and getattr(args, "require_all_yhome_rows", True):
        report["issues"].append(
            {
                "type": "pre_audit_yhome_missing_candidate_rows",
                "count": pre_audit["yhome_missing_candidate_count"],
                "properties": pre_audit["yhome_missing_candidates"][:25],
                "reason": "Yhome transition reconciliation CSV does not cover every required OH/IL monthly candidate property.",
                "required_states": pre_audit["yhome_required_states"],
                "excluded_candidate_count": pre_audit["yhome_excluded_candidate_count"],
            }
        )
        return finish_report(report, status="review", reason="pre_audit_yhome_missing_candidate_rows")
    if pre_audit["yhome_update_required_count"] <= 0:
        if pre_audit["report_status"] == "ok":
            return finish_report(report, status="ok", reason="no_updates_required")
        return finish_report(report, status="review", reason="pre_audit_has_non_yhome_issues")

    update_result = run_updater(args, env=run_env)
    report["commands"].append(update_result["command"])
    report.update(
        {
            "update_report_status": update_result["report_status"],
            "update_report_reason": update_result["reason"],
            "update_count": update_result["update_count"],
            "request_count": update_result["request_count"],
            "applied_update_count": update_result["applied_update_count"],
            "update_report_target_columns": update_result["target_columns"],
            "update_report_idempotency_key": update_result["report"].get("idempotency_key"),
            "update_report_range_count": len(update_result["report"].get("ranges") or []),
            "update_report_ranges": update_result["report"].get("ranges") or [],
            "update_report_apply_requested": update_result["apply_requested"],
            "update_report_write_enabled": update_result["write_enabled"],
            "update_report_apply_allowed": update_result["apply_allowed"],
            "update_report_dry_run": update_result["dry_run"],
            "update_report_gws_available": update_result["report"].get("gws_available"),
            "update_report_gws_result_status": (update_result["report"].get("gws_result") or {}).get("status"),
        }
    )
    if update_result["command"].get("timed_out"):
        return finish_report(report, status="failed", reason="updater_timed_out")
    if update_result["report_status"] != "ok":
        return finish_report(report, status="review", reason=f"updater_{update_result['reason'] or 'review'}")
    if not target_columns_match(update_result["report"]):
        report["issues"].append(target_column_drift_issue("updater", args.updater_report, update_result["report"]))
        return finish_report(report, status="review", reason="updater_target_column_drift")
    if not apply_allowed:
        return finish_report(report, status="review", reason="dry_run_updates_required")

    report["external_write_attempted"] = bool(update_result["request_count"])
    if update_result["applied_update_count"] <= 0:
        return finish_report(report, status="review", reason="apply_allowed_but_no_updates_applied")

    post_refresh = refresh_yhome_csv(args, env=run_env, prefer_gws=True)
    report["refreshes"].append({"phase": "post_apply", **post_refresh})
    if post_refresh["status"] == "failed":
        return finish_report(report, status="failed", reason="post_refresh_failed")

    post_audit = run_audit(args, phase="post_apply", env=run_env)
    report["commands"].append(post_audit["command"])
    report.update(
        {
            "post_audit_status": post_audit["report_status"],
            "post_audit_issue_count": post_audit["issue_count"],
            "post_yhome_update_required_count": post_audit["yhome_update_required_count"],
            "post_yhome_missing_candidate_count": post_audit["yhome_missing_candidate_count"],
            "post_yhome_missing_candidates": post_audit["yhome_missing_candidates"],
            "post_audit_target_columns": post_audit["target_columns"],
        }
    )
    if post_audit["command"].get("timed_out"):
        return finish_report(report, status="failed", reason="post_audit_timed_out")
    if not target_columns_match(post_audit["report"]):
        report["issues"].append(target_column_drift_issue("post_audit", args.audit_report, post_audit["report"]))
        return finish_report(report, status="review", reason="post_audit_target_column_drift")
    if post_audit["yhome_update_required_count"] > 0:
        return finish_report(report, status="review", reason="post_apply_yhome_updates_still_required")
    informational_missing_only = (
        not getattr(args, "require_all_yhome_rows", True)
        and post_audit["yhome_missing_candidate_count"] > 0
        and post_audit["issue_count"] == post_audit["yhome_missing_candidate_count"]
    )
    if post_audit["report_status"] != "ok" and not informational_missing_only:
        return finish_report(report, status="review", reason="post_audit_has_non_yhome_issues")
    report["post_apply_verification_ok"] = True
    return finish_report(report, status="ok", reason="applied_and_verified")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default=default_month())
    parser.add_argument("--candidate-packet", type=Path, default=ROOT / "reports/baselane_financials_monthly_review_candidate_packet.json")
    parser.add_argument("--yhome-csv", type=Path, default=ROOT / "reports/yhome_transition_reconciliation.csv")
    parser.add_argument("--yhome-export-url", default=os.environ.get("YHOME_TRANSITION_RECONCILIATION_URL") or DEFAULT_YHOME_EXPORT_URL)
    parser.add_argument("--yhome-gws-spreadsheet-id", default=os.environ.get("YHOME_GSHEET_SPREADSHEET_ID") or YHOME_GSHEET_SPREADSHEET_ID)
    parser.add_argument("--yhome-gws-sheet-title", default=os.environ.get("YHOME_GSHEET_SHEET_TITLE") or YHOME_GSHEET_SHEET_TITLE)
    parser.add_argument(
        "--yhome-gws-sheet-spec",
        dest="yhome_gws_sheet_specs",
        action="append",
        default=None,
        help="Repeat TITLE=GID for each authoritative Yhome tab.",
    )
    parser.add_argument("--gws-bin", default=os.environ.get("GWS_BIN") or "gws")
    parser.add_argument("--audit-script", type=Path, default=ROOT / "scripts/baselane_cf_balance_sheet_consistency_audit.py")
    parser.add_argument("--audit-report", type=Path, default=ROOT / "reports/baselane_cf_balance_sheet_consistency_audit.json")
    parser.add_argument("--plan-csv", type=Path, default=ROOT / "reports/yhome_operating_cash_update_plan.csv")
    parser.add_argument("--yhome-missing-candidates-csv", type=Path, default=ROOT / "reports/yhome_missing_candidates.csv")
    parser.add_argument("--updater-script", type=Path, default=ROOT / "scripts/yhome_operating_cash_gsheet_update.py")
    parser.add_argument("--updater-report", type=Path, default=ROOT / "reports/yhome_operating_cash_gsheet_update_report.json")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/yhome_operating_cash_apply_verify_report.json")
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument("--refresh-timeout-seconds", type=float, default=float(os.environ.get("YHOME_REFRESH_TIMEOUT_SECONDS") or 20))
    parser.add_argument("--audit-timeout-seconds", type=float, default=float(os.environ.get("YHOME_AUDIT_TIMEOUT_SECONDS") or 60))
    parser.add_argument("--audit-workbooks", action="store_true")
    parser.add_argument("--allow-missing-yhome-rows", dest="require_all_yhome_rows", action="store_false")
    parser.set_defaults(require_all_yhome_rows=True)
    parser.add_argument("--workbook-timeout-seconds", type=float, default=float(os.environ.get("YHOME_WORKBOOK_TIMEOUT_SECONDS") or 10))
    parser.add_argument("--updater-timeout-seconds", type=float, default=float(os.environ.get("YHOME_UPDATER_TIMEOUT_SECONDS") or 30))
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.yhome_gws_sheet_specs is None:
        configured_specs = os.environ.get("YHOME_GWS_SHEET_SPECS")
        args.yhome_gws_sheet_specs = (
            configured_specs.split("|")
            if configured_specs
            else [f"{title}={gid}" for title, gid in YHOME_GSHEET_SHEET_SPECS]
        )
    return args


def main() -> int:
    args = parse_args()
    report = build_report(args)
    write_json(args.report, report)
    print(
        "wrote "
        f"{args.report} status={report['status']} reason={report.get('reason')} "
        f"updates={report.get('pre_yhome_update_required_count', 0)} "
        f"applied={report.get('applied_update_count', 0)}"
    )
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
