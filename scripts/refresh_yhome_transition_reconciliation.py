#!/usr/bin/env python3
"""Refresh a complete, read-only snapshot of the Yhome reconciliation tabs."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SPREADSHEET_ID = "1HerPv9U7IB47ipCpJ-XshajQWouCUEwfDdkHSfVCwfc"
DEFAULT_SHEET_SPECS = (
    ("Cleveland", 1187056671),
    ("Chicago & non-Yhome", 433920866),
    ("Yhome Deeded & Sold", 1902489452),
)
METADATA_COLUMNS = (
    "__yhome_sheet_title",
    "__yhome_sheet_gid",
    "__yhome_sheet_row_number",
    "__yhome_sheet_lofty_operating_cash_column_index",
    "__yhome_sheet_eco_net_dao_funds_column_index",
)


def generated_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_sheet_spec(value: str) -> tuple[str, int]:
    title, separator, gid = value.partition("=")
    if not separator or not title.strip() or not gid.strip():
        raise argparse.ArgumentTypeError("sheet must be TITLE=GID")
    try:
        return title.strip(), int(gid)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sheet gid must be an integer") from exc


def command_result(command: list[str], timeout_seconds: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "return_code": None,
            "stdout_tail": str(exc.stdout or "")[-2000:],
            "stderr_tail": str(exc.stderr or "")[-2000:],
        }
    return {
        "status": "ok" if completed.returncode == 0 else "failed",
        "return_code": completed.returncode,
        "stdout": completed.stdout,
        "stdout_tail": completed.stdout[-2000:],
        "stderr_tail": completed.stderr[-2000:],
    }


def fetch_tabs(
    *, spreadsheet_id: str, sheet_specs: list[tuple[str, int]], gws_bin: str, timeout_seconds: float
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = {
        "spreadsheetId": spreadsheet_id,
        "ranges": [title for title, _gid in sheet_specs],
        "valueRenderOption": "UNFORMATTED_VALUE",
    }
    command = [
        gws_bin,
        "sheets",
        "spreadsheets",
        "values",
        "batchGet",
        "--params",
        json.dumps(params),
        "--format",
        "json",
    ]
    result = command_result(command, timeout_seconds)
    if result["status"] != "ok":
        raise RuntimeError(f"gws refresh failed: {result}")
    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        raise RuntimeError("gws refresh returned invalid JSON") from exc
    value_ranges = payload.get("valueRanges") if isinstance(payload, dict) else None
    if not isinstance(value_ranges, list) or len(value_ranges) != len(sheet_specs):
        raise RuntimeError("gws refresh did not return every configured sheet")
    tabs: list[dict[str, Any]] = []
    for (title, gid), value_range in zip(sheet_specs, value_ranges, strict=True):
        values = value_range.get("values") if isinstance(value_range, dict) else None
        if not isinstance(values, list) or not values:
            raise RuntimeError(f"gws refresh returned no rows for {title}")
        headers = [str(value or "").strip() for value in values[0]]
        if not {"Property", "New PM", "Lofty Operating Cash", "ECO Net DAO Funds"}.issubset(headers):
            raise RuntimeError(f"required Yhome columns missing from {title}")
        tabs.append({"title": title, "gid": gid, "headers": headers, "values": values})
    return tabs, {"status": "ok", "command": command, "sheet_count": len(tabs)}


def write_snapshot(path: Path, tabs: list[dict[str, Any]]) -> dict[str, Any]:
    headers: list[str] = []
    for tab in tabs:
        for header in tab["headers"]:
            if header and header not in headers:
                headers.append(header)
    headers.extend(METADATA_COLUMNS)
    rows: list[dict[str, Any]] = []
    for tab in tabs:
        for source_row_number, values in enumerate(tab["values"][1:], 2):
            row = {header: (values[index] if index < len(values) else "") for index, header in enumerate(tab["headers"])}
            if not str(row.get("Property") or "").strip():
                continue
            row["__yhome_sheet_title"] = tab["title"]
            row["__yhome_sheet_gid"] = tab["gid"]
            row["__yhome_sheet_row_number"] = source_row_number
            row["__yhome_sheet_lofty_operating_cash_column_index"] = tab["headers"].index("Lofty Operating Cash") + 1
            row["__yhome_sheet_eco_net_dao_funds_column_index"] = tab["headers"].index("ECO Net DAO Funds") + 1
            rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)
    return {
        "status": "ok",
        "generated_at": generated_at(),
        "path": str(path),
        "sheet_count": len(tabs),
        "row_count": len(rows),
        "sheet_titles": [tab["title"] for tab in tabs],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spreadsheet-id", default=os.environ.get("YHOME_GWS_SPREADSHEET_ID") or DEFAULT_SPREADSHEET_ID)
    parser.add_argument("--output", type=Path, default=Path("reports/yhome_transition_reconciliation.csv"))
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--gws-bin", default=os.environ.get("GWS_BIN") or "gws")
    parser.add_argument("--timeout-seconds", type=float, default=float(os.environ.get("YHOME_REFRESH_TIMEOUT_SECONDS") or 30))
    parser.add_argument("--sheet", dest="sheets", action="append", type=parse_sheet_spec)
    args = parser.parse_args()
    sheet_specs = args.sheets or list(DEFAULT_SHEET_SPECS)
    try:
        tabs, fetch_report = fetch_tabs(
            spreadsheet_id=args.spreadsheet_id,
            sheet_specs=sheet_specs,
            gws_bin=args.gws_bin,
            timeout_seconds=args.timeout_seconds,
        )
        report = write_snapshot(args.output, tabs)
        report["fetch"] = fetch_report
        status = "ok"
        rc = 0
    except Exception as exc:  # noqa: BLE001
        report = {
            "status": "failed",
            "generated_at": generated_at(),
            "path": str(args.output),
            "sheet_titles": [title for title, _gid in sheet_specs],
            "error": str(exc),
        }
        status = "failed"
        rc = 1
    report["status"] = status
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
