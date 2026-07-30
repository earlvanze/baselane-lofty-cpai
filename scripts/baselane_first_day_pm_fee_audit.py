#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATE_FORMATS = ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y")


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_date(value: str) -> datetime | None:
    raw = (value or "").strip().strip('"')
    if not raw:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    return None


def default_gl_csv() -> Path:
    env_path = os.environ.get("BASELANE_LEDGER_PATH")
    if env_path:
        return Path(env_path)
    workspace_root = Path(os.environ.get("WORKSPACE_ROOT", Path(__file__).absolute().parents[1]))
    for candidate in (
        workspace_root / "Dropbox/Projects/assetrail/ECO Systems General Ledger.csv",
        Path("/home/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"),
        Path("/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"),
    ):
        if candidate.is_file():
            return candidate
    return Path("/home/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv")


def is_first_day_pm_fee_row(row: dict[str, str], target_month: str | None) -> bool:
    parsed = parse_date(row.get("Date") or "")
    if parsed is None or parsed.day != 1:
        return False
    if target_month is not None and parsed.strftime("%Y-%m") != target_month:
        return False
    notes = row.get("Notes") or ""
    merchant = row.get("Merchant") or ""
    category = row.get("Category") or ""
    return "AOPS-PM-FEE" in notes


def build_report(gl_csv: Path, target_month: str | None, limit: int = 25) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    month_counts: dict[str, int] = {}
    parse_error_count = 0
    scope = "all_months" if target_month is None else "single_month"
    if not gl_csv.is_file():
        return {
            "generated_at": iso_z(),
            "status": "missing",
            "month": target_month or "all",
            "scope": scope,
            "ledger_csv": str(gl_csv),
            "first_day_pm_fee_count": 0,
            "month_counts": {},
            "rows_bounded": [],
            "error": "ledger CSV missing",
        }
    with gl_csv.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader, start=2):
            if row.get("Date") and parse_date(row.get("Date") or "") is None:
                parse_error_count += 1
            if not is_first_day_pm_fee_row(row, target_month):
                continue
            parsed = parse_date(row.get("Date") or "")
            month_key = parsed.strftime("%Y-%m") if parsed is not None else "unknown"
            month_counts[month_key] = month_counts.get(month_key, 0) + 1
            rows.append(
                {
                    "line_number": index,
                    "date": row.get("Date") or "",
                    "merchant": row.get("Merchant") or "",
                    "amount": row.get("Amount") or "",
                    "property": row.get("Property") or "",
                    "notes": row.get("Notes") or "",
                }
            )
    bounded = rows[:limit]
    return {
        "generated_at": iso_z(),
        "status": "review" if rows else "ok",
        "month": target_month or "all",
        "scope": scope,
        "ledger_csv": str(gl_csv),
        "first_day_pm_fee_count": len(rows),
        "month_counts": dict(sorted(month_counts.items())),
        "parse_error_count": parse_error_count,
        "rows_bounded": bounded,
        "bounded": len(rows) > len(bounded),
        "policy": "No AOPS-PM-FEE property-management rows should be generated on the 1st of the month.",
    }


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit raw ECO GL for forbidden 1st-day AOPS PM fee rows.")
    parser.add_argument("--gl-csv", type=Path, default=default_gl_csv())
    parser.add_argument("--month", default=datetime.now().strftime("%Y-%m"))
    parser.add_argument("--all-months", action="store_true", help="Scan all ledger months instead of only --month.")
    parser.add_argument("--report", type=Path, default=Path(__file__).absolute().parents[1] / "reports" / "baselane_first_day_pm_fee_audit.json")
    args = parser.parse_args()
    report = build_report(args.gl_csv, None if args.all_months else args.month)
    write_json(args.report, report)
    print(json.dumps({key: report[key] for key in ("status", "month", "first_day_pm_fee_count")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
