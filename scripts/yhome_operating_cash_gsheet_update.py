#!/usr/bin/env python3
"""Apply the approved Yhome operating-cash update plan to Google Sheets.

Default mode is report-only. A real Google Sheets write requires both:
- --apply
- YHOME_GSHEET_WRITE_ENABLED=1
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SPREADSHEET_ID = "1HerPv9U7IB47ipCpJ-XshajQWouCUEwfDdkHSfVCwfc"
SHEET_GID = 1187056671
TARGET_COLUMNS = ("Lofty Operating Cash", "ECO Net DAO Funds")
ECO_CASH_POLICY = "eco_held_unrestricted_cash_v1"
SHEET_COLUMN_METADATA = {
    "Lofty Operating Cash": (
        "yhome_lofty_operating_cash_column_index",
        "__yhome_sheet_lofty_operating_cash_column_index",
    ),
    "ECO Net DAO Funds": (
        "yhome_eco_net_dao_funds_column_index",
        "__yhome_sheet_eco_net_dao_funds_column_index",
    ),
}
SCRIPT_DIR = Path(__file__).absolute().parent


def generated_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_header(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def normalize_property_name(value: Any) -> str:
    text = str(value or "").strip().lower().replace("&", " and ")
    replacements = {
        "avenue": "ave",
        "street": "st",
        "road": "rd",
        "lane": "ln",
        "drive": "dr",
        "place": "pl",
        "north": "n",
        "south": "s",
        "east": "e",
        "west": "w",
    }
    for source, target in replacements.items():
        text = re.sub(rf"\b{source}\b", target, text)
    street_match = re.match(
        r"^\s*(.*?\b(?:st|ave|rd|ln|dr|blvd|pl|ct|pkwy|ter)\b)",
        text,
        flags=re.IGNORECASE,
    )
    if street_match:
        text = street_match.group(1)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def column_number_to_letters(column_number: int) -> str:
    letters = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def parse_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def load_csv_rows(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.exists():
        return [], []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def header_index(headers: list[str], wanted: str) -> int | None:
    normalized = normalize_header(wanted)
    for index, header in enumerate(headers, start=1):
        if normalize_header(header) == normalized:
            return index
    return None


def load_updates(plan_csv: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    rows, _headers = load_csv_rows(plan_csv)
    updates = []
    rejected_updates = []
    non_update_count = 0
    for row in rows:
        if str(row.get("action") or "").strip() != "update":
            non_update_count += 1
            continue
        column = str(row.get("column") or "").strip()
        if column not in TARGET_COLUMNS:
            rejected_updates.append(
                {
                    "type": "unsupported_update_column",
                    "property": row.get("property"),
                    "column": column,
                    "allowed_columns": list(TARGET_COLUMNS),
                    "yhome_row_number": row.get("yhome_row_number"),
                }
            )
            continue
        if (
            column == "ECO Net DAO Funds"
            and str(row.get("eco_cash_policy") or "").strip() != ECO_CASH_POLICY
        ):
            rejected_updates.append(
                {
                    "type": "eco_cash_policy_missing_or_invalid",
                    "property": row.get("property"),
                    "column": column,
                    "required_policy": ECO_CASH_POLICY,
                    "observed_policy": row.get("eco_cash_policy"),
                    "yhome_row_number": row.get("yhome_row_number"),
                }
            )
            continue
        row_number_raw = str(row.get("yhome_row_number") or "").strip()
        try:
            row_number = int(row_number_raw)
        except ValueError:
            continue
        target_value = parse_number(row.get("target_value"))
        if target_value is None:
            continue
        updates.append(
            {
                "property": row.get("property"),
                "row_number": row_number,
                "sheet_gid": parse_number(row.get("yhome_sheet_gid")),
                "sheet_title": row.get("yhome_sheet_title"),
                "column": column,
                "target_value": target_value,
                "current_value": parse_number(row.get("current_value")),
                "diff": parse_number(row.get("diff")),
                "source_paths": str(row.get("source_paths") or "").strip(),
                "source_fingerprint": str(row.get("source_fingerprint") or "").strip(),
                "sheet_column_index": next(
                    (
                        parsed
                        for key in SHEET_COLUMN_METADATA.get(column, ())
                        for parsed in [parse_number(row.get(key))]
                        if parsed is not None
                    ),
                    None,
                ),
            }
        )
    return updates, rejected_updates, non_update_count


def source_fingerprint(paths: list[Path]) -> str:
    entries = []
    for path in sorted(paths, key=lambda item: str(item)):
        entries.append(
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def source_freshness_issues(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reject source-backed ECO plans if their source files no longer match planning time."""
    issues = []
    for update in updates:
        source_paths = str(update.get("source_paths") or "").strip()
        if not source_paths:
            continue
        expected = str(update.get("source_fingerprint") or "").strip()
        paths = [Path(value.strip()) for value in source_paths.split(" | ") if value.strip()]
        if not expected:
            issues.append({"type": "source_fingerprint_missing", "property": update.get("property"), "column": update.get("column")})
            continue
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            issues.append({"type": "source_path_missing", "property": update.get("property"), "column": update.get("column"), "paths": missing})
            continue
        observed = source_fingerprint(paths)
        if observed != expected:
            issues.append(
                {
                    "type": "source_fingerprint_mismatch",
                    "property": update.get("property"),
                    "column": update.get("column"),
                    "expected": expected,
                    "observed": observed,
                }
            )
    return issues


def idempotency_key(updates: list[dict[str, Any]]) -> str:
    payload = json.dumps(sorted(updates, key=lambda item: (item.get("sheet_gid") or 0, item["row_number"], item["column"])), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_gws_config_dir() -> str | None:
    configured = os.environ.get("GOOGLE_WORKSPACE_CLI_CONFIG_DIR")
    if configured:
        return configured
    base = os.environ.get("GWS_CONFIG_BASE")
    candidates = []
    if base:
        candidates.append(Path(base))
    candidates.extend(
        [
            SCRIPT_DIR.parent.parent / "gws",
            Path("/home/digit/.openclaw/gws"),
            Path("/data/.openclaw/gws"),
            Path("/home/umbrel/.openclaw/gws"),
        ]
    )
    for candidate in candidates:
        if candidate.is_dir():
            return str(candidate)
    return None


def gws_environment(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    config_dir = getattr(args, "gws_config_dir", None) or default_gws_config_dir()
    keyring_backend = getattr(args, "gws_keyring_backend", None) or env.get("GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND")
    if config_dir:
        env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(config_dir)
    if keyring_backend:
        env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = str(keyring_backend)
    elif config_dir:
        env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"
    return env


def run_gws(command: list[str], timeout_seconds: float, env: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout_seconds, env=env)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "return_code": None, "stderr": "", "stdout": "", "timeout_seconds": timeout_seconds}
    return {
        "status": "ok" if completed.returncode == 0 else "failed",
        "return_code": completed.returncode,
        "stdout": completed.stdout.strip()[:2000],
        "stderr": completed.stderr.strip()[:2000],
    }


def quote_sheet_title(value: Any) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def live_row_validation_ranges(
    updates: list[dict[str, Any]],
    headers: list[str],
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build live property/value readback ranges for every pending write."""
    ranges: list[str] = []
    expected: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for update in updates:
        sheet_title = str(update.get("sheet_title") or "").strip()
        column_index = int(update.get("sheet_column_index") or 0) or header_index(
            headers, str(update["column"])
        )
        if not sheet_title:
            issues.append(
                {
                    "type": "live_row_sheet_title_missing",
                    "property": update.get("property"),
                    "row_number": update.get("row_number"),
                }
            )
            continue
        if column_index is None:
            issues.append(
                {
                    "type": "live_row_target_column_missing",
                    "property": update.get("property"),
                    "column": update.get("column"),
                }
            )
            continue
        row_number = int(update["row_number"])
        target_cell = f"{column_number_to_letters(column_index)}{row_number}"
        quoted_title = quote_sheet_title(sheet_title)
        ranges.extend(
            [
                f"{quoted_title}!A{row_number}",
                f"{quoted_title}!{target_cell}",
            ]
        )
        expected.append(
            {
                "property": str(update.get("property") or "").strip(),
                "current_value": update.get("current_value"),
                "sheet_title": sheet_title,
                "row_number": row_number,
                "target_cell": target_cell,
            }
        )
    return ranges, expected, issues


def validate_live_row_payload(
    expected: list[dict[str, Any]], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Fail closed when live row identity or current value drifted after planning."""
    value_ranges = payload.get("valueRanges")
    if not isinstance(value_ranges, list) or len(value_ranges) != len(expected) * 2:
        return [
            {
                "type": "live_row_readback_incomplete",
                "expected_range_count": len(expected) * 2,
                "observed_range_count": len(value_ranges or []),
            }
        ]
    issues: list[dict[str, Any]] = []
    for index, item in enumerate(expected):
        property_values = value_ranges[index * 2].get("values") or []
        target_values = value_ranges[index * 2 + 1].get("values") or []
        live_property = (
            str(property_values[0][0]).strip()
            if property_values and property_values[0]
            else ""
        )
        if normalize_property_name(live_property) != normalize_property_name(item["property"]):
            issues.append(
                {
                    "type": "live_row_property_mismatch",
                    "expected_property": item["property"],
                    "observed_property": live_property,
                    "sheet_title": item["sheet_title"],
                    "row_number": item["row_number"],
                }
            )
            continue
        expected_current = parse_number(item.get("current_value"))
        observed_current = parse_number(
            target_values[0][0] if target_values and target_values[0] else None
        )
        if expected_current is not None and observed_current != expected_current:
            issues.append(
                {
                    "type": "live_row_current_value_mismatch",
                    "property": item["property"],
                    "sheet_title": item["sheet_title"],
                    "row_number": item["row_number"],
                    "target_cell": item["target_cell"],
                    "expected_current_value": expected_current,
                    "observed_current_value": observed_current,
                }
            )
    return issues


def fetch_live_row_validation(
    args: argparse.Namespace,
    updates: list[dict[str, Any]],
    headers: list[str],
    env: dict[str, str],
) -> dict[str, Any]:
    ranges, expected, issues = live_row_validation_ranges(updates, headers)
    if issues:
        return {"status": "review", "issues": issues, "range_count": len(ranges)}
    command = [
        args.gws_bin,
        "sheets",
        "spreadsheets",
        "values",
        "batchGet",
        "--params",
        json.dumps(
            {
                "spreadsheetId": args.spreadsheet_id,
                "ranges": ranges,
                "valueRenderOption": "UNFORMATTED_VALUE",
            },
            separators=(",", ":"),
        ),
        "--format",
        "json",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=args.gws_timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "review",
            "issues": [{"type": "live_row_readback_timeout"}],
            "range_count": len(ranges),
        }
    if completed.returncode != 0:
        return {
            "status": "review",
            "issues": [
                {
                    "type": "live_row_readback_failed",
                    "return_code": completed.returncode,
                    "stderr": completed.stderr.strip()[:1000],
                }
            ],
            "range_count": len(ranges),
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "status": "review",
            "issues": [{"type": "live_row_readback_invalid_json"}],
            "range_count": len(ranges),
        }
    validation_issues = validate_live_row_payload(expected, payload)
    return {
        "status": "ok" if not validation_issues else "review",
        "issues": validation_issues,
        "range_count": len(ranges),
        "row_count": len(expected),
    }


def build_update_requests(updates: list[dict[str, Any]], headers: list[str], sheet_gid: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    requests = []
    summaries = []
    issues = []
    for update in updates:
        column_index = int(update.get("sheet_column_index") or 0) or header_index(headers, str(update["column"]))
        if column_index is None:
            issues.append({"type": "target_column_missing", "column": update["column"], "property": update.get("property")})
            continue
        row_index = int(update["row_number"]) - 1
        column_zero_index = column_index - 1
        grid_range = {
            "sheetId": int(update.get("sheet_gid") or sheet_gid),
            "startRowIndex": row_index,
            "endRowIndex": row_index + 1,
            "startColumnIndex": column_zero_index,
            "endColumnIndex": column_zero_index + 1,
        }
        requests.append(
            {
                "updateCells": {
                    "range": grid_range,
                    "rows": [{"values": [{"userEnteredValue": {"numberValue": update["target_value"]}}]}],
                    "fields": "userEnteredValue",
                }
            }
        )
        summaries.append(
            {
                "a1_cell_hint": f"{column_number_to_letters(column_index)}{update['row_number']}",
                "grid_range": grid_range,
                "property": update.get("property"),
                "column": update["column"],
                "row_number": update["row_number"],
                "sheet_gid": int(update.get("sheet_gid") or sheet_gid),
                "sheet_title": update.get("sheet_title"),
                "target_value": update["target_value"],
            }
        )
    return requests, summaries, issues


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    updates, rejected_updates, non_update_count = load_updates(args.plan_csv)
    _rows, headers = load_csv_rows(args.yhome_csv)
    gws_available = shutil.which(args.gws_bin) is not None
    run_env = gws_environment(args)
    write_enabled = os.environ.get("YHOME_GSHEET_WRITE_ENABLED") == "1"
    apply_allowed = bool(args.apply and write_enabled)
    report: dict[str, Any] = {
        "job": "yhome-operating-cash-gsheet-update",
        "generated_at": generated_at(),
        "status": "ok",
        "spreadsheet_id": args.spreadsheet_id,
        "sheet_gid": args.sheet_gid,
        "range_mode": "grid_range_sheet_id",
        "target_column_policy": "Yhome Google Sheet writes are limited to Lofty Operating Cash and ECO Net DAO Funds.",
        "plan_csv": str(args.plan_csv),
        "yhome_csv": str(args.yhome_csv),
        "target_columns": list(TARGET_COLUMNS),
        "target_column_count": len(TARGET_COLUMNS),
        "allowed_target_columns_only": not rejected_updates,
        "update_count": len(updates),
        "rejected_update_count": len(rejected_updates),
        "skipped_non_update_count": non_update_count,
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
        "gws_bin": args.gws_bin,
        "gws_available": gws_available,
        "gws_config_dir": run_env.get("GOOGLE_WORKSPACE_CLI_CONFIG_DIR"),
        "gws_keyring_backend": run_env.get("GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"),
        "idempotency_key": idempotency_key(updates),
        "issues": [],
    }
    if rejected_updates:
        report["status"] = "review"
        report["reason"] = "unsupported_update_column_in_plan"
        report["issues"].extend(rejected_updates)
        return report
    if not updates:
        report["reason"] = "no_updates_required"
        return report
    if not headers:
        report["status"] = "review"
        report["reason"] = "yhome_csv_missing_or_empty"
        report["issues"].append({"type": "yhome_csv_missing_or_empty", "path": str(args.yhome_csv)})
        return report
    if args.apply and not write_enabled:
        report["status"] = "review"
        report["reason"] = "write_not_enabled"
        report["issues"].append({"type": "write_not_enabled", "required_env": "YHOME_GSHEET_WRITE_ENABLED=1"})
        return report
    freshness_issues = source_freshness_issues(updates)
    report["source_freshness_checked_count"] = sum(bool(item.get("source_paths")) for item in updates)
    report["source_freshness_issue_count"] = len(freshness_issues)
    if freshness_issues:
        report["status"] = "review"
        report["reason"] = "source_plan_stale_or_unverifiable"
        report["issues"].extend(freshness_issues)
        return report
    requests, request_summaries, range_issues = build_update_requests(updates, headers, args.sheet_gid)
    report["request_count"] = len(requests)
    report["request_summary_count"] = len(request_summaries)
    report["ranges"] = request_summaries
    report["all_grid_ranges_use_sheet_gid"] = all(
        int(summary.get("grid_range", {}).get("sheetId", -1)) == int(summary.get("sheet_gid", -1))
        for summary in request_summaries
    )
    if range_issues:
        report["status"] = "review"
        report["reason"] = "range_build_issues"
        report["issues"].extend(range_issues)
        return report
    if not gws_available:
        report["status"] = "review"
        report["reason"] = "gws_missing"
        report["issues"].append({"type": "gws_missing", "gws_bin": args.gws_bin})
        return report
    if apply_allowed:
        live_validation = fetch_live_row_validation(args, updates, headers, run_env)
        report["live_row_validation"] = live_validation
        if live_validation["status"] != "ok":
            report["status"] = "review"
            report["reason"] = "live_sheet_row_or_value_drift"
            report["issues"].extend(live_validation["issues"])
            return report
    body = {"requests": requests}
    command = [
        args.gws_bin,
        "sheets",
        "spreadsheets",
        "batchUpdate",
        "--params",
        json.dumps({"spreadsheetId": args.spreadsheet_id}),
        "--json",
        json.dumps(body),
        "--format",
        "json",
    ]
    if not apply_allowed:
        command.append("--dry-run")
    result = run_gws(command, args.gws_timeout_seconds, env=run_env)
    report["gws_result"] = result
    if result.get("return_code") != 0:
        report["status"] = "review"
        report["reason"] = "gws_dry_run_failed" if not apply_allowed else "gws_apply_failed"
        report["issues"].append({"type": report["reason"], "return_code": result.get("return_code")})
        return report
    report["reason"] = "dry_run_ok" if not apply_allowed else "applied"
    report["applied_update_count"] = len(requests) if apply_allowed else 0
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-csv", type=Path, default=Path("reports/yhome_operating_cash_update_plan.csv"))
    parser.add_argument("--yhome-csv", type=Path, default=Path("reports/yhome_transition_reconciliation.csv"))
    parser.add_argument("--report", type=Path, default=Path("reports/yhome_operating_cash_gsheet_update_report.json"))
    parser.add_argument("--spreadsheet-id", default=os.environ.get("YHOME_GSHEET_SPREADSHEET_ID") or SPREADSHEET_ID)
    parser.add_argument("--sheet-gid", type=int, default=int(os.environ.get("YHOME_GSHEET_SHEET_GID") or SHEET_GID))
    parser.add_argument("--gws-bin", default=os.environ.get("GWS_BIN") or "gws")
    parser.add_argument("--gws-config-dir", default=os.environ.get("YHOME_GSHEET_GWS_CONFIG_DIR"))
    parser.add_argument("--gws-keyring-backend", default=os.environ.get("YHOME_GSHEET_GWS_KEYRING_BACKEND") or os.environ.get("GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"))
    parser.add_argument("--gws-timeout-seconds", type=float, default=float(os.environ.get("YHOME_GSHEET_GWS_TIMEOUT_SECONDS") or 20))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    report = build_report(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.report} status={report['status']} updates={report['update_count']} reason={report.get('reason')}")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
