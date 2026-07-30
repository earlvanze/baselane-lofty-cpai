#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from baselane_first_day_pm_fee_audit import is_first_day_pm_fee_row, iso_z, parse_date


def stable_digest(payload: object) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def default_ledger() -> Path:
    env_path = os.environ.get("BASELANE_CLEAN_REPORTING_LEDGER_PATH") or os.environ.get("BASELANE_LEDGER_PATH")
    if env_path:
        return Path(env_path)
    return Path(__file__).absolute().parents[1] / "reports" / "baselane_weekly_clean_reporting_ledger.csv"


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames:
            raise ValueError(f"Ledger has no CSV header: {path}")
        return fieldnames, list(reader)


def write_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def bounded_row(row: dict[str, str], line_number: int) -> dict[str, Any]:
    return {
        "line_number": line_number,
        "date": row.get("Date") or "",
        "merchant": row.get("Merchant") or "",
        "amount": row.get("Amount") or "",
        "property": row.get("Property") or "",
        "category": row.get("Category") or "",
        "notes": row.get("Notes") or "",
    }


def build_report(
    ledger: Path,
    out_ledger: Path,
    target_month: str | None,
    apply: bool,
    quarantine_csv: Path | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    fieldnames, rows = read_rows(ledger)
    kept_rows: list[dict[str, str]] = []
    quarantined_rows: list[dict[str, str]] = []
    quarantined_details: list[dict[str, Any]] = []
    month_counts: dict[str, int] = {}
    parse_error_count = 0

    for offset, row in enumerate(rows, start=2):
        parsed = parse_date(row.get("Date") or "")
        if row.get("Date") and parsed is None:
            parse_error_count += 1
        if not is_first_day_pm_fee_row(row, target_month):
            kept_rows.append(dict(row))
            continue
        quarantined_rows.append(dict(row))
        month_key = parsed.strftime("%Y-%m") if parsed is not None else "unknown"
        month_counts[month_key] = month_counts.get(month_key, 0) + 1
        quarantined_details.append(bounded_row(row, offset))

    remaining_matches = [row for row in kept_rows if is_first_day_pm_fee_row(row, target_month)]
    output_clean = len(remaining_matches) == 0
    input_digest = stable_digest({"fieldnames": fieldnames, "rows": rows})
    output_digest = stable_digest({"fieldnames": fieldnames, "rows": kept_rows})
    quarantine_digest = stable_digest({"fieldnames": fieldnames, "rows": quarantined_rows})

    if apply:
        write_rows(out_ledger, fieldnames, kept_rows)
        if quarantine_csv is not None:
            write_rows(quarantine_csv, fieldnames, quarantined_rows)

    return {
        "generated_at": iso_z(),
        "status": "ok" if output_clean else "failed",
        "mode": "apply" if apply else "dry_run",
        "month": target_month or "all",
        "scope": "all_months" if target_month is None else "single_month",
        "ledger_csv": str(ledger),
        "out_ledger": str(out_ledger),
        "quarantine_csv": str(quarantine_csv) if quarantine_csv is not None else None,
        "input_row_count": len(rows),
        "output_row_count": len(kept_rows),
        "quarantined_row_count": len(quarantined_rows),
        "first_day_pm_fee_count": len(quarantined_rows),
        "remaining_first_day_pm_fee_count": len(remaining_matches),
        "reporting_output_clean": output_clean,
        "raw_source_mutated": False,
        "month_counts": dict(sorted(month_counts.items())),
        "parse_error_count": parse_error_count,
        "rows_bounded": quarantined_details[:limit],
        "bounded": len(quarantined_details) > limit,
        "input_digest": input_digest,
        "output_digest": output_digest,
        "quarantine_digest": quarantine_digest,
        "output_written": bool(apply),
        "policy": (
            "Derived reporting quarantine only: remove 1st-day AOPS-PM-FEE accrual rows from downstream "
            "weekly/monthly reporting ledgers while preserving the raw Baselane/ECO GL export for source cleanup evidence."
        ),
    }


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Baselane 1st-Day PM Fee Quarantine",
        "",
        f"- Status: `{report['status']}`",
        f"- Mode: `{report['mode']}`",
        f"- Scope: `{report['scope']}`",
        f"- Quarantined rows: `{report['quarantined_row_count']}`",
        f"- Remaining 1st-day PM-fee rows: `{report['remaining_first_day_pm_fee_count']}`",
        f"- Reporting output clean: `{str(report['reporting_output_clean']).lower()}`",
        f"- Raw source mutated: `{str(report['raw_source_mutated']).lower()}`",
        f"- Input: `{report['ledger_csv']}`",
        f"- Output: `{report['out_ledger']}`",
        f"- Quarantine CSV: `{report.get('quarantine_csv') or 'none'}`",
        f"- Quarantine digest: `{report['quarantine_digest']}`",
        "",
        "## Policy",
        "",
        report["policy"],
        "",
        "## Month Counts",
    ]
    for month, count in report.get("month_counts", {}).items():
        lines.append(f"- `{month}`: {count}")
    if not report.get("month_counts"):
        lines.append("- none")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Quarantine forbidden 1st-day AOPS PM fee rows from derived reporting ledgers.")
    parser.add_argument("--ledger", type=Path, default=default_ledger())
    parser.add_argument("--out-ledger", type=Path, default=Path(__file__).absolute().parents[1] / "reports" / "baselane_weekly_clean_reporting_ledger.csv")
    parser.add_argument("--month", default=datetime.now().strftime("%Y-%m"))
    parser.add_argument("--all-months", action="store_true", help="Quarantine all ledger months instead of only --month.")
    parser.add_argument("--report", type=Path, default=Path(__file__).absolute().parents[1] / "reports" / "baselane_first_day_pm_fee_quarantine_report.json")
    parser.add_argument("--quarantine-csv", type=Path, default=Path(__file__).absolute().parents[1] / "reports" / "baselane_first_day_pm_fee_quarantine_rows.csv")
    parser.add_argument("--markdown", type=Path, default=Path(__file__).absolute().parents[1] / "reports" / "baselane_first_day_pm_fee_quarantine_report.md")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    report = build_report(
        ledger=args.ledger,
        out_ledger=args.out_ledger,
        target_month=None if args.all_months else args.month,
        apply=not args.dry_run,
        quarantine_csv=args.quarantine_csv,
    )
    write_json(args.report, report)
    write_markdown(args.markdown, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "month": report["month"],
                "quarantined_row_count": report["quarantined_row_count"],
                "reporting_output_clean": report["reporting_output_clean"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
