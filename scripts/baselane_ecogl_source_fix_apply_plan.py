#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


REQUIRED_APPROVED_FIELDS = {
    "id",
    "property",
    "date",
    "amount",
    "merchant",
    "operator_category_to_set_in_baselane",
}
INDEX_ID_FIELDS = ("BaselaneId", "baselane_id", "id")
INVALID_EMPTY_CATEGORIES = {"", "uncategorized", "uncategorized expense"}
BASELANE_CATEGORY_ALIASES = {
    "Cleaning & Janitorial": "Cleaning & Maintenance",
    "Gardening & Landscaping": "Cleaning & Maintenance",
    "Landscaping": "Cleaning & Maintenance",
    "Remodeling": "Repairs",
    "Water & Sewer": "Utilities",
}


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def norm_text(value: object) -> str:
    return " ".join(str(value or "").strip().lower().replace("&", " and ").split())


def norm_amount(value: object) -> str:
    raw = str(value or "").strip().replace("$", "").replace(",", "")
    if not raw:
        return ""
    try:
        return str(Decimal(raw).quantize(Decimal("0.01")).normalize())
    except InvalidOperation:
        return raw


def norm_date(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%-m/%-d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


def transaction_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        norm_text(row.get("Property") or row.get("property")),
        norm_date(row.get("ISODate") or row.get("Date") or row.get("date")),
        norm_amount(row.get("Amount") or row.get("amount")),
        norm_text(row.get("Merchant") or row.get("merchant") or row.get("merchantName")),
    )


def correction_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        norm_text(row.get("property")),
        norm_date(row.get("date")),
        norm_amount(row.get("amount")),
        norm_text(row.get("merchant")),
    )


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str], list[str]]:
    if not path.is_file():
        return [], [], [f"missing_csv:{path}"]
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            return [{key: str(value or "") for key, value in row.items()} for row in reader], list(reader.fieldnames or []), []
    except Exception as exc:  # noqa: BLE001
        return [], [], [f"unreadable_csv:{path}:{exc}"]


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def baselane_id(row: dict[str, str]) -> str:
    for field in INDEX_ID_FIELDS:
        value = str(row.get(field) or "").strip()
        if value:
            return value
    return ""


def category_is_set(value: object) -> bool:
    return str(value or "").strip().lower() not in INVALID_EMPTY_CATEGORIES


def baselane_category(value: object) -> str:
    category = str(value or "").strip()
    return BASELANE_CATEGORY_ALIASES.get(category, category)


def current_match_categories_are_target(matches: list[dict[str, str]], category: str) -> bool:
    target = baselane_category(category)
    return bool(matches) and all(baselane_category(match.get("Category")) == target for match in matches)


def source_paths(root: Path, explicit: list[Path]) -> list[Path]:
    paths = [path for path in explicit if path]
    paths.extend(
        [
            root / "reports" / "baselane_source_transaction_index.csv",
            root / "reports" / "baselane_export_filtered_preview.csv",
            Path("/home/digit/Dropbox/Real Estate/Lofty PM/reports"),
            Path("/mnt/c/Users/digit/Dropbox/Real Estate/Lofty PM/reports"),
        ]
    )
    expanded: list[Path] = []
    for path in paths:
        if path.is_dir():
            expanded.extend(sorted(path.glob("baselane_export_fresh_*.csv")))
            expanded.extend(sorted(path.glob("baselane_source_transaction_index*.csv")))
        else:
            expanded.append(path)
    seen: set[str] = set()
    deduped: list[Path] = []
    for path in expanded:
        key = str(path.resolve(strict=False))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def index_source_rows(paths: list[Path]) -> tuple[dict[tuple[str, str, str, str], list[dict[str, str]]], list[str], list[str]]:
    indexed: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    used: list[str] = []
    errors: list[str] = []
    for path in paths:
        rows, _fields, row_errors = read_csv(path)
        if row_errors:
            if path.name == "baselane_source_transaction_index.csv":
                errors.extend(row_errors)
            continue
        usable_count = 0
        for row in rows:
            row_id = baselane_id(row)
            if not row_id:
                continue
            key = transaction_key(row)
            if not all(key):
                continue
            indexed[key].append({**row, "_source_path": str(path), "_baselane_id": row_id})
            usable_count += 1
        if usable_count:
            used.append(str(path))
    return indexed, used, errors


def build_report(root: Path, approved_csv: Path, validation_report_path: Path, source_index_paths: list[Path]) -> dict[str, Any]:
    approved_rows, approved_fields, approved_errors = read_csv(approved_csv)
    missing_approved_fields = sorted(REQUIRED_APPROVED_FIELDS - set(approved_fields))
    validation_report = read_json(validation_report_path)
    export_guard = read_json(root / "reports" / "baselane_export_guard_last.json")
    current_write_status = str(export_guard.get("source_transaction_index_current_write_status") or "").strip()
    current_source_index_fresh = current_write_status in {"", "written_current"}
    ready_ids = {
        str(record.get("id"))
        for record in validation_report.get("records") or []
        if isinstance(record, dict) and record.get("validation_status") == "ready_for_baselane_entry"
    }
    native_split_ids = {
        str(record.get("id"))
        for record in validation_report.get("records") or []
        if isinstance(record, dict) and record.get("validation_status") == "ready_for_native_split"
    }
    indexed_rows, used_sources, index_errors = (
        index_source_rows(source_paths(root, source_index_paths))
        if approved_rows
        else ({}, [], [])
    )
    records: list[dict[str, Any]] = []
    for row in approved_rows:
        row_id = str(row.get("id") or "")
        if row_id in native_split_ids:
            continue
        category = baselane_category(row.get("operator_category_to_set_in_baselane"))
        if not category_is_set(category):
            continue
        key = correction_key(row)
        matches = indexed_rows.get(key, [])
        unique_ids = sorted({match["_baselane_id"] for match in matches})
        current_matches = [
            match
            for match in matches
            if current_source_index_fresh and Path(match["_source_path"]).name == "baselane_source_transaction_index.csv"
        ]
        current_ids = sorted({match["_baselane_id"] for match in current_matches})
        if row_id not in ready_ids:
            status = "blocked_not_validated_ready"
            reason = "approved correction is not marked ready_for_baselane_entry in the correction validation report"
        elif len(current_ids) == 1:
            status = "ready_current_source_index"
            reason = "exactly one current source-index transaction ID matches property/date/amount/merchant"
        elif len(current_ids) > 1 and current_match_categories_are_target(current_matches, category):
            status = "already_applied_current_source_index"
            reason = "multiple current source-index transaction IDs match, but every matching row already has the requested category"
        elif len(current_ids) > 1:
            status = "blocked_ambiguous_current_match"
            reason = "multiple current source-index transaction IDs match; do not mutate Baselane"
        elif len(unique_ids) == 1:
            status = "needs_current_source_index_refresh"
            reason = "historical source export resolves one Baselane ID, but current source index is missing; rerun daily sync before mutation"
        elif len(unique_ids) > 1:
            status = "blocked_ambiguous_historical_match"
            reason = "multiple historical transaction IDs match; do not mutate Baselane"
        else:
            status = "blocked_missing_baselane_id"
            reason = "no Baselane transaction ID found for exact property/date/amount/merchant key"
        chosen_id = current_ids[0] if len(current_ids) == 1 else unique_ids[0] if len(unique_ids) == 1 else ""
        records.append(
            {
                "id": row_id,
                "property": row.get("property"),
                "date": row.get("date"),
                "amount": row.get("amount"),
                "merchant": row.get("merchant"),
                "category_to_set": category,
                "baselane_id": chosen_id,
                "match_status": status,
                "match_reason": reason,
                "current_match_count": len(current_ids),
                "historical_or_current_match_count": len(unique_ids),
                "matched_source_paths": sorted({match["_source_path"] for match in matches}),
                "policy": "Plan only; does not mutate Baselane.",
            }
        )
    ready_count = sum(1 for record in records if record["match_status"] == "ready_current_source_index")
    already_count = sum(1 for record in records if record["match_status"] == "already_applied_current_source_index")
    refresh_count = sum(1 for record in records if record["match_status"] == "needs_current_source_index_refresh")
    blocked_count = len(records) - ready_count - already_count - refresh_count
    structural_issue_count = len(approved_errors) + len(index_errors) + len(missing_approved_fields)
    validation_status = str(validation_report.get("status") or "")
    status = (
        "ok"
        if (
            (records and ready_count + already_count == len(records) and structural_issue_count == 0)
            # This plan can be intentionally filtered to a narrow automation
            # lane. With no rows in that lane, a portfolio-wide validation
            # review has no mutation to block here.
            or (not records and structural_issue_count == 0)
        )
        else "review"
    )
    return {
        "generated_at": iso_z(),
        "status": status,
        "policy": "No-mutation apply plan; Baselane writes require a current source-index ID and an explicit apply workflow.",
        "approved_corrections_csv": str(approved_csv),
        "validation_report": str(validation_report_path),
        "validation_status": validation_status,
        "source_transaction_index_current_write_status": current_write_status or None,
        "current_source_index_fresh": current_source_index_fresh,
        "source_paths_used": used_sources,
        "row_count": len(records),
        "ready_current_source_index_count": ready_count,
        "already_applied_current_source_index_count": already_count,
        "needs_current_source_index_refresh_count": refresh_count,
        "blocked_count": blocked_count,
        "structural_issue_count": structural_issue_count,
        "approved_csv_errors": approved_errors,
        "source_index_errors": index_errors,
        "missing_approved_fields": missing_approved_fields,
        "next_action": (
            "Approved corrections have current Baselane IDs; run the guarded Baselane source update workflow."
            if status == "ok" and records
            else "No approved source-fix category mutations are pending."
            if status == "ok"
            else "Rerun deterministic daily sync to generate reports/baselane_source_transaction_index.csv, then regenerate this apply plan."
        ),
        "records": records,
    }


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id",
        "property",
        "date",
        "amount",
        "merchant",
        "category_to_set",
        "baselane_id",
        "match_status",
        "match_reason",
        "current_match_count",
        "historical_or_current_match_count",
        "matched_source_paths",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = dict(record)
            row["matched_source_paths"] = json.dumps(record.get("matched_source_paths") or [], sort_keys=True)
            writer.writerow({field: row.get(field, "") for field in fields})


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ECO GL Source-Fix Apply Plan",
        "",
        f"- Status: `{report['status']}`",
        f"- Rows: `{report['row_count']}`",
        f"- Ready with current source index: `{report['ready_current_source_index_count']}`",
        f"- Needs current source index refresh: `{report['needs_current_source_index_refresh_count']}`",
        f"- Blocked: `{report['blocked_count']}`",
        f"- Structural issues: `{report['structural_issue_count']}`",
        f"- Policy: {report['policy']}",
        f"- Next action: {report['next_action']}",
        "",
        "## Records",
        "",
    ]
    for record in report.get("records") or []:
        lines.append(
            f"- `{record.get('id')}` — {record.get('property')} — {record.get('date')} — "
            f"{record.get('amount')} — {record.get('merchant')} → `{record.get('category_to_set')}` — "
            f"`{record.get('match_status')}` — Baselane ID `{record.get('baselane_id') or 'missing'}`"
        )
        lines.append(f"  - {record.get('match_reason')}")
    if not report.get("records"):
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve approved ECO GL source-fix corrections to exact Baselane transaction IDs without mutating Baselane.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--approved-csv", type=Path)
    parser.add_argument("--validation-report", type=Path)
    parser.add_argument("--source-index", action="append", type=Path, default=[])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()

    root = args.root
    report_path = args.report or root / "reports" / "baselane_ecogl_source_fix_apply_plan.json"
    csv_path = args.csv or root / "reports" / "baselane_ecogl_source_fix_apply_plan.csv"
    markdown_path = args.markdown or root / "reports" / "baselane_ecogl_source_fix_apply_plan.md"
    report = build_report(
        root,
        args.approved_csv or root / "reports" / "baselane_ecogl_source_fix_approved_corrections.csv",
        args.validation_report or root / "reports" / "baselane_ecogl_source_fix_correction_validation.json",
        args.source_index,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(csv_path, report.get("records") or [])
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["status", "row_count", "ready_current_source_index_count", "needs_current_source_index_refresh_count", "blocked_count"]}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
