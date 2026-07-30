#!/usr/bin/env python3
"""Audit PM fees that exist in both first-day and month-end accrual lanes."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GL = Path("/home/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv")
DEFAULT_REPORT = ROOT / "reports" / "baselane_pm_fee_duplicate_lane_audit.json"
DEFAULT_CSV = ROOT / "reports" / "baselane_pm_fee_duplicate_lane_audit.csv"

FIRST_DAY_PM_RE = re.compile(r"AOPS-PM-FEE\|([^|]+)\|(\d{4}-\d{2})\|([\d.]+)")
MONTH_END_PM_RE = re.compile(r"AOPS-[A-Z]+-ACCRUAL\|pm\|([^|]+)\|(\d{4}-\d{2})\|([\d.]+)")


def iso_z() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_date(value: str) -> dt.date | None:
    value = (value or "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def read_rows(path: Path) -> list[tuple[int, dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(enumerate(reader, start=2))


def row_amount(row: dict[str, str]) -> str:
    return str(row.get("Amount") or row.get("Amount (USD)") or "")


def row_text(row: dict[str, str]) -> str:
    return " ".join(str(row.get(key) or "") for key in ("Merchant", "Description", "Category", "Property", "Notes"))


def collect_pm_lanes(rows: list[tuple[int, dict[str, str]]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    lanes: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for line, row in rows:
        text = row_text(row)
        row_date = parse_date(str(row.get("Date") or ""))
        for lane, pattern in (("first_day", FIRST_DAY_PM_RE), ("month_end", MONTH_END_PM_RE)):
            match = pattern.search(text)
            if not match:
                continue
            property_name, month, marker_amount = match.groups()
            if lane == "first_day" and row_date and row_date.day != 1:
                continue
            if lane == "month_end" and row_date and row_date.day < 28:
                continue
            lanes[(property_name, month)].append({
                "lane": lane,
                "line": line,
                "property": property_name,
                "month": month,
                "date": row.get("Date", ""),
                "amount": row_amount(row),
                "marker_amount": marker_amount,
                "category": row.get("Category", ""),
                "description": row.get("Description", ""),
                "notes": row.get("Notes", ""),
            })
    return lanes


def build_report(gl_csv: Path, month: str | None = None) -> dict[str, Any]:
    lanes = collect_pm_lanes(read_rows(gl_csv))
    issues: list[dict[str, Any]] = []
    for (property_name, service_month), rows in sorted(lanes.items()):
        if month and service_month != month:
            continue
        lane_names = {row["lane"] for row in rows}
        if {"first_day", "month_end"}.issubset(lane_names):
            issues.append({
                "property": property_name,
                "month": service_month,
                "row_count": len(rows),
                "rows": rows,
                "reason": "pm_fee_exists_in_first_day_and_month_end_lanes",
            })
    return {
        "generated_at": iso_z(),
        "gl_csv": str(gl_csv),
        "month": month or "all",
        "status": "review" if issues else "ok",
        "issue_count": len(issues),
        "issues": issues,
    }


def write_csv(path: Path, issues: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["property", "month", "lane", "line", "date", "amount", "marker_amount", "category", "description", "reason"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for issue in issues:
            for row in issue["rows"]:
                writer.writerow({
                    "property": issue["property"],
                    "month": issue["month"],
                    "lane": row["lane"],
                    "line": row["line"],
                    "date": row["date"],
                    "amount": row["amount"],
                    "marker_amount": row["marker_amount"],
                    "category": row["category"],
                    "description": row["description"],
                    "reason": issue["reason"],
                })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gl-csv", type=Path, default=DEFAULT_GL)
    parser.add_argument("--month", help="Optional YYYY-MM month filter")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--fail-on-review", action="store_true")
    args = parser.parse_args(argv)

    report = build_report(args.gl_csv, args.month)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(args.csv, report["issues"])
    print(json.dumps({key: report[key] for key in ("status", "issue_count", "month")}, indent=2))
    return 1 if args.fail_on_review and report["status"] != "ok" else 0


if __name__ == "__main__":
    raise SystemExit(main())
