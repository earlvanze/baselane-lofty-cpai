#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any

from baselane_ecogl_data_quality_autonomy import (
    iso_z,
    raw_no_dao_mortgage_violation_reason,
    stable_digest,
)


def default_ledger() -> Path:
    env_path = os.environ.get("BASELANE_NO_DAO_MORTGAGE_INPUT_LEDGER") or os.environ.get("BASELANE_CLEAN_REPORTING_LEDGER_PATH")
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


def bounded_row(row: dict[str, str], line_number: int, reason: str) -> dict[str, Any]:
    return {
        "line_number": line_number,
        "date": row.get("Date") or "",
        "merchant": row.get("Merchant") or "",
        "description": row.get("Description") or "",
        "amount": row.get("Amount") or "",
        "property": row.get("Property") or "",
        "type": row.get("Type") or "",
        "category": row.get("Category") or "",
        "sub_category": row.get("Sub-category") or "",
        "notes": row.get("Notes") or "",
        "reason": reason,
    }


def build_report(
    ledger: Path,
    out_ledger: Path,
    apply: bool,
    quarantine_csv: Path | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    fieldnames, rows = read_rows(ledger)
    kept_rows: list[dict[str, str]] = []
    quarantined_rows: list[dict[str, str]] = []
    quarantined_details: list[dict[str, Any]] = []
    property_counts: dict[str, int] = {}

    for line_number, row in enumerate(rows, start=2):
        reason = raw_no_dao_mortgage_violation_reason(row)
        if not reason:
            kept_rows.append(dict(row))
            continue
        quarantined_rows.append(dict(row))
        property_name = row.get("Property") or "unknown"
        property_counts[property_name] = property_counts.get(property_name, 0) + 1
        quarantined_details.append(bounded_row(row, line_number, reason))

    remaining_matches = [
        row
        for row in kept_rows
        if raw_no_dao_mortgage_violation_reason(row)
    ]
    output_clean = not remaining_matches
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
        "ledger_csv": str(ledger),
        "out_ledger": str(out_ledger),
        "quarantine_csv": str(quarantine_csv) if quarantine_csv is not None else None,
        "input_row_count": len(rows),
        "output_row_count": len(kept_rows),
        "quarantined_row_count": len(quarantined_rows),
        "remaining_no_dao_mortgage_row_count": len(remaining_matches),
        "reporting_output_clean": output_clean,
        "raw_source_mutated": False,
        "property_counts": dict(sorted(property_counts.items())),
        "rows_bounded": quarantined_details[:limit],
        "bounded": len(quarantined_details) > limit,
        "input_digest": input_digest,
        "output_digest": output_digest,
        "quarantine_digest": quarantine_digest,
        "output_written": bool(apply),
        "policy": (
            "Derived reporting quarantine only: no-DAO-mortgage properties must not feed mortgage "
            "principal, interest, or debt-servicer rows into downstream CF statements or ECO operating cash."
        ),
    }


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Baselane No-DAO Mortgage Reporting Quarantine",
        "",
        f"- Status: `{report['status']}`",
        f"- Mode: `{report['mode']}`",
        f"- Quarantined rows: `{report['quarantined_row_count']}`",
        f"- Remaining no-DAO mortgage rows: `{report['remaining_no_dao_mortgage_row_count']}`",
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
        "## Property Counts",
    ]
    for property_name, count in report.get("property_counts", {}).items():
        lines.append(f"- `{property_name}`: {count}")
    if not report.get("property_counts"):
        lines.append("- none")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Quarantine no-DAO mortgage rows from derived reporting ledgers.")
    parser.add_argument("--ledger", type=Path, default=default_ledger())
    parser.add_argument(
        "--out-ledger",
        type=Path,
        default=Path(__file__).absolute().parents[1] / "reports" / "baselane_weekly_no_dao_mortgage_clean_reporting_ledger.csv",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(__file__).absolute().parents[1] / "reports" / "baselane_no_dao_mortgage_reporting_quarantine.json",
    )
    parser.add_argument(
        "--quarantine-csv",
        type=Path,
        default=Path(__file__).absolute().parents[1] / "reports" / "baselane_no_dao_mortgage_reporting_quarantine_rows.csv",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=Path(__file__).absolute().parents[1] / "reports" / "baselane_no_dao_mortgage_reporting_quarantine.md",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    report = build_report(
        ledger=args.ledger,
        out_ledger=args.out_ledger,
        apply=not args.dry_run,
        quarantine_csv=args.quarantine_csv,
    )
    write_json(args.report, report)
    write_markdown(args.markdown, report)
    print(
        json.dumps(
            {
                "status": report["status"],
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
