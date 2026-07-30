#!/usr/bin/env python3
"""Repair and harden row-local formulas on the Yhome Cleveland tab.

Google Sheets sorts can move a formula with a property while preserving the
formula's old explicit row references.  This workflow replaces only the five
generic, row-local derived columns with formulas based on ROW() and INDEX().
Those formulas continue to evaluate the property on their current row after a
future sort.

The property-specific accrual columns, including "PM Fees Due TO New PM", are
never targeted.

Default mode is preview-only.  A write requires:

* --apply
* --confirm-digest matching the current preview
* YHOME_FORMULA_REPAIR_WRITE_ENABLED=1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SPREADSHEET_ID = "1HerPv9U7IB47ipCpJ-XshajQWouCUEwfDdkHSfVCwfc"
SHEET_TITLE = "Cleveland"
SHEET_GID = 1187056671
READ_RANGE = f"{SHEET_TITLE}!A1:AH"
PROPERTY_HEADER = "Property"
PRESERVED_CUSTOM_COLUMNS = (
    "Taxes Due FROM DAO",
    "Insurance Due FROM DAO",
    "Capital Call Due TO Lending DAO",
    "PM Fees Due TO New PM",
)
TARGET_HEADERS = (
    "Aligned Net DAO Funds",
    "DAO Net Cash (Capital Call)",
    "Distribute?",
    "DAO Estimated Net Asset Value",
    "Status",
)


def generated_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalized(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def column_letters(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def stable_formulas() -> dict[str, str]:
    """Return sort-stable formulas whose current row is resolved at runtime."""
    aligned = "=SUM(INDEX(P:P,ROW()):INDEX(R:R,ROW()))"
    net_cash = (
        "=SUM(INDEX(N:N,ROW()),INDEX(O:O,ROW()),INDEX(S:S,ROW()),"
        "INDEX(T:T,ROW()),INDEX(U:U,ROW()),INDEX(V:V,ROW()),"
        "INDEX(W:W,ROW()))-SUM(INDEX(X:X,ROW()),INDEX(Y:Y,ROW()),"
        "INDEX(Z:Z,ROW()),INDEX(AA:AA,ROW()))"
    )
    distribute = (
        '=IF(AND(INDEX(AB:AB,ROW())>0,'
        "(INDEX(W:W,ROW())+INDEX(U:U,ROW()))>"
        "(INDEX(X:X,ROW())+INDEX(Z:Z,ROW())),"
        'INDEX(S:S,ROW())>=0,NOT(INDEX(AC:AC,ROW()))),"Maybe","No")'
    )
    nav = (
        '=IFERROR((INDEX(AF:AF,ROW())+INDEX(AB:AB,ROW())+'
        'INDEX(V:V,ROW()))/INDEX(AE:AE,ROW()),"")'
    )
    status = (
        "=IF(AND(INDEX(N:N,ROW())>0,INDEX(U:U,ROW())<0),"
        'IF(INDEX(N:N,ROW())<ABS(INDEX(U:U,ROW())),'
        'IF(INDEX(S:S,ROW())<0,"🟦","🟣"),'
        'IF(INDEX(S:S,ROW())<0,"🟨","🟡")),'
        "IF(INDEX(N:N,ROW())>0,"
        'IF(INDEX(S:S,ROW())<0,"🟧","🟢"),'
        "IF(AND(INDEX(N:N,ROW())<0,INDEX(U:U,ROW())<0,"
        "INDEX(AB:AB,ROW())<0),"
        'IF(INDEX(S:S,ROW())<0,"⚫","🔴"),'
        "IF(INDEX(U:U,ROW())<0,"
        'IF(INDEX(S:S,ROW())<0,"🧿","🟠"),'
        "IF(INDEX(N:N,ROW())<0,"
        'IF(INDEX(S:S,ROW())<0,"🟪","🟤"),'
        "IF(INDEX(U:U,ROW())<0,"
        'IF(INDEX(S:S,ROW())<0,"🟫","🔵"),'
        'IF(INDEX(S:S,ROW())<0,"🟥","⚪")))))))'
    )
    return {
        "Aligned Net DAO Funds": aligned,
        "DAO Net Cash (Capital Call)": net_cash,
        "Distribute?": distribute,
        "DAO Estimated Net Asset Value": nav,
        "Status": status,
    }


def run_command(command: list[str], timeout_seconds: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "return_code": None, "stdout": "", "stderr": ""}
    return {
        "status": "ok" if completed.returncode == 0 else "failed",
        "return_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr[-3000:],
    }


def fetch_formula_grid(args: argparse.Namespace) -> list[list[Any]]:
    params = {
        "spreadsheetId": args.spreadsheet_id,
        "ranges": [READ_RANGE],
        "valueRenderOption": "FORMULA",
    }
    result = run_command(
        [
            args.gws_bin,
            "sheets",
            "spreadsheets",
            "values",
            "batchGet",
            "--params",
            json.dumps(params, separators=(",", ":")),
            "--format",
            "json",
        ],
        args.timeout_seconds,
    )
    if result["status"] != "ok":
        raise RuntimeError(f"formula read failed: {result['status']}: {result['stderr']}")
    try:
        payload = json.loads(result["stdout"])
        grids = payload["valueRanges"]
        values = grids[0]["values"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("formula read returned an invalid payload") from exc
    if not isinstance(values, list) or not values:
        raise RuntimeError("formula read returned no rows")
    return values


def header_map(headers: list[Any]) -> dict[str, int]:
    return {normalized(value): index for index, value in enumerate(headers)}


def cell(row: list[Any], index: int) -> Any:
    return row[index] if index < len(row) else ""


def build_plan(values: list[list[Any]]) -> dict[str, Any]:
    headers = values[0]
    indexes = header_map(headers)
    required = (PROPERTY_HEADER, *TARGET_HEADERS, *PRESERVED_CUSTOM_COLUMNS)
    missing = [header for header in required if normalized(header) not in indexes]
    if missing:
        raise RuntimeError(f"required columns missing: {missing}")
    property_index = indexes[normalized(PROPERTY_HEADER)]
    formulas = stable_formulas()
    changes: list[dict[str, Any]] = []
    missing_formula_cells: list[dict[str, Any]] = []
    property_rows: list[dict[str, Any]] = []
    property_table_started = False
    for row_number, row in enumerate(values[1:], 2):
        property_name = str(cell(row, property_index) or "").strip()
        if not property_name:
            # The Cleveland property table is contiguous and is followed by a
            # blank separator plus a summary block that also uses column A.
            if property_table_started:
                break
            continue
        property_table_started = True
        property_rows.append({"row_number": row_number, "property": property_name})
        for header in TARGET_HEADERS:
            column_index = indexes[normalized(header)]
            before = str(cell(row, column_index) or "")
            after = formulas[header]
            a1 = f"{column_letters(column_index + 1)}{row_number}"
            if not before.startswith("="):
                missing_formula_cells.append(
                    {
                        "property": property_name,
                        "row_number": row_number,
                        "column": header,
                        "a1": a1,
                        "observed": before,
                    }
                )
                continue
            if before != after:
                changes.append(
                    {
                        "property": property_name,
                        "row_number": row_number,
                        "column": header,
                        "column_index": column_index,
                        "a1": a1,
                        "before": before,
                        "after": after,
                    }
                )
    digest_payload = [
        {
            "property": item["property"],
            "row_number": item["row_number"],
            "a1": item["a1"],
            "before": item["before"],
            "after": item["after"],
        }
        for item in changes
    ]
    digest = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "headers": headers,
        "property_rows": property_rows,
        "changes": changes,
        "missing_formula_cells": missing_formula_cells,
        "digest": digest,
    }


def build_requests(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requests = []
    for change in changes:
        row_index = change["row_number"] - 1
        column_index = change["column_index"]
        requests.append(
            {
                "updateCells": {
                    "range": {
                        "sheetId": SHEET_GID,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + 1,
                        "startColumnIndex": column_index,
                        "endColumnIndex": column_index + 1,
                    },
                    "rows": [
                        {
                            "values": [
                                {
                                    "userEnteredValue": {
                                        "formulaValue": change["after"],
                                    }
                                }
                            ]
                        }
                    ],
                    "fields": "userEnteredValue",
                }
            }
        )
    return requests


def apply_requests(args: argparse.Namespace, requests: list[dict[str, Any]]) -> dict[str, Any]:
    result = run_command(
        [
            args.gws_bin,
            "sheets",
            "spreadsheets",
            "batchUpdate",
            "--params",
            json.dumps({"spreadsheetId": args.spreadsheet_id}, separators=(",", ":")),
            "--json",
            json.dumps({"requests": requests}, ensure_ascii=False, separators=(",", ":")),
            "--format",
            "json",
        ],
        args.timeout_seconds,
    )
    if result["status"] != "ok":
        raise RuntimeError(f"formula write failed: {result['status']}: {result['stderr']}")
    return {
        "status": "ok",
        "request_count": len(requests),
        "response_sha256": hashlib.sha256(result["stdout"].encode("utf-8")).hexdigest(),
    }


def verify_expected(
    before_plan: dict[str, Any],
    current_plan: dict[str, Any],
    expected_formulas: dict[str, str],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    before_properties = {
        item["row_number"]: item["property"] for item in before_plan["property_rows"]
    }
    current_properties = {
        item["row_number"]: item["property"] for item in current_plan["property_rows"]
    }
    if before_properties != current_properties:
        issues.append(
            {
                "type": "property_row_drift",
                "expected": before_properties,
                "observed": current_properties,
            }
        )
    for item in current_plan["changes"]:
        if item["column"] in expected_formulas:
            issues.append(
                {
                    "type": "formula_not_hardened",
                    "property": item["property"],
                    "a1": item["a1"],
                    "column": item["column"],
                    "observed": item["before"],
                    "expected": expected_formulas[item["column"]],
                }
            )
    issues.extend(
        {"type": "missing_formula", **item}
        for item in current_plan["missing_formula_cells"]
    )
    return issues


def write_report(path: Path | None, report: dict[str, Any]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spreadsheet-id", default=SPREADSHEET_ID)
    parser.add_argument("--gws-bin", default=os.environ.get("GWS_BIN") or "gws")
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-digest")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    report: dict[str, Any] = {
        "job": "yhome-repair-sorted-formula-rows",
        "generated_at": generated_at(),
        "spreadsheet_id": args.spreadsheet_id,
        "sheet_title": SHEET_TITLE,
        "sheet_gid": SHEET_GID,
        "target_columns": list(TARGET_HEADERS),
        "preserved_custom_columns": list(PRESERVED_CUSTOM_COLUMNS),
        "formula_strategy": "nonvolatile INDEX(column,ROW()) formulas survive future row sorts",
        "apply_requested": args.apply,
        "write_enabled": os.environ.get("YHOME_FORMULA_REPAIR_WRITE_ENABLED") == "1",
        "status": "review",
    }
    try:
        initial_values = fetch_formula_grid(args)
        plan = build_plan(initial_values)
        report.update(
            {
                "status": "ok",
                "property_row_count": len(plan["property_rows"]),
                "change_count": len(plan["changes"]),
                "missing_formula_count": len(plan["missing_formula_cells"]),
                "missing_formula_cells": plan["missing_formula_cells"],
                "digest": plan["digest"],
                "changes": plan["changes"],
            }
        )
        if plan["missing_formula_cells"]:
            report["status"] = "review"
            report["reason"] = "target_formula_cells_missing"
        elif not args.apply:
            report["reason"] = "preview_only"
        elif os.environ.get("YHOME_FORMULA_REPAIR_WRITE_ENABLED") != "1":
            report["status"] = "review"
            report["reason"] = "write_not_enabled"
        elif args.confirm_digest != plan["digest"]:
            report["status"] = "review"
            report["reason"] = "confirmation_digest_mismatch"
        else:
            # Re-read immediately before mutation. A sort or edit invalidates
            # the digest and property-row map, so the workflow fails closed.
            prewrite_values = fetch_formula_grid(args)
            prewrite_plan = build_plan(prewrite_values)
            if (
                prewrite_plan["digest"] != plan["digest"]
                or prewrite_plan["property_rows"] != plan["property_rows"]
            ):
                report["status"] = "review"
                report["reason"] = "live_sheet_drift_before_write"
                report["prewrite_digest"] = prewrite_plan["digest"]
            else:
                report["write"] = apply_requests(args, build_requests(plan["changes"]))
                verified_values = fetch_formula_grid(args)
                verified_plan = build_plan(verified_values)
                issues = verify_expected(plan, verified_plan, stable_formulas())
                report["verification"] = {
                    "status": "ok" if not issues else "failed",
                    "remaining_change_count": len(verified_plan["changes"]),
                    "issue_count": len(issues),
                    "issues": issues,
                }
                report["status"] = "ok" if not issues else "failed"
                report["reason"] = "applied_and_verified" if not issues else "post_write_verification_failed"
    except Exception as exc:  # noqa: BLE001
        report["status"] = "failed"
        report["reason"] = "exception"
        report["error"] = str(exc)

    write_report(args.report, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
