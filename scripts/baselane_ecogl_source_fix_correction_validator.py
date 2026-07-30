#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = {
    "id",
    "fix_status",
    "property",
    "date",
    "amount",
    "merchant",
    "operator_category_to_set_in_baselane",
    "context_candidate_status",
}
INVALID_EMPTY_CATEGORIES = {"", "uncategorized", "uncategorized expense"}
BASELANE_CATEGORY_ALIASES = {
    "Cleaning & Janitorial": "Cleaning & Maintenance",
    "Gardening & Landscaping": "Cleaning & Maintenance",
    "Landscaping": "Cleaning & Maintenance",
    "Water & Sewer": "Utilities",
}
DEFAULT_ALLOWED_CATEGORIES = {
    "Advertising",
    "Cleaning & Maintenance",
    "Fees & Other Revenue",
    "Insurance",
    "Legal & Other Professional Fees",
    "Management Fees",
    "Mortgage Interest Payments",
    "Mortgage Principal Payments",
    "Other Operating Expenses",
    "Repairs",
    "Repairs Reimbursement",
    "Rents",
    "Supplies",
    "Taxes",
    "Tax Licenses & Registrations",
    "Utilities",
}


def baselane_category(category: str) -> str:
    category = str(category or "").strip()
    return BASELANE_CATEGORY_ALIASES.get(category, category)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def count_like(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def category_values(path: Path, fieldnames: tuple[str, ...] = ("Category", "new_category", "baselane_category")) -> set[str]:
    if not path.is_file():
        return set()
    values: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                for field in fieldnames:
                    value = str(row.get(field) or "").strip()
                    if value and value.lower() not in INVALID_EMPTY_CATEGORIES:
                        values.add(value)
    except Exception:
        return set()
    return values


def allowed_categories(root: Path) -> set[str]:
    reports = root / "reports"
    categories = set(DEFAULT_ALLOWED_CATEGORIES)
    for relative in [
        "baselane_weekly_clean_reporting_ledger.csv",
        "baselane_weekly_accrual_overlay_reporting_ledger.csv",
        "baselane_ecogl_safe_category_apply_actions.csv",
        "baselane_ecogl_accrual_overlay_actions.csv",
        "baselane_ecogl_source_fix_actions.csv",
    ]:
        categories.update(category_values(reports / relative))
    return {category for category in categories if category and category.lower() not in INVALID_EMPTY_CATEGORIES}


def read_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.is_file():
        return [], [f"missing_csv:{path}"]
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = sorted(REQUIRED_FIELDS - set(reader.fieldnames or []))
            if missing:
                return [], [f"missing_columns:{','.join(missing)}"]
            return [{key: str(value or "") for key, value in row.items()} for row in reader], []
    except Exception as exc:  # noqa: BLE001
        return [], [f"unreadable_csv:{exc}"]


def expected_ids_from_report(report: dict[str, Any]) -> set[str]:
    approvals = report.get("approvals")
    if isinstance(approvals, list):
        return {
            str(row.get("id"))
            for row in approvals
            if isinstance(row, dict) and row.get("id") and row.get("approved") is True
        }
    rows = report.get("rows")
    if isinstance(rows, list):
        return {
            str(row.get("id"))
            for row in rows
            if isinstance(row, dict) and row.get("id") and row.get("fix_status") != "verified_fixed"
        }
    return set()


def default_corrections_csv(root: Path) -> Path:
    approved = root / "reports" / "baselane_ecogl_source_fix_approved_corrections.csv"
    return approved if approved.is_file() else root / "reports" / "baselane_ecogl_source_fix_corrections.csv"


def default_corrections_report(root: Path, corrections_csv: Path) -> Path:
    approval = root / "reports" / "baselane_ecogl_source_fix_approval.json"
    if corrections_csv.name == "baselane_ecogl_source_fix_approved_corrections.csv" and approval.is_file():
        return approval
    return root / "reports" / "baselane_ecogl_source_fix_corrections.json"


def build_report(root: Path, corrections_csv: Path, corrections_report_path: Path) -> dict[str, Any]:
    corrections_report = read_json(corrections_report_path)
    rows, input_errors = read_rows(corrections_csv)
    allowed = allowed_categories(root)
    expected_ids = expected_ids_from_report(corrections_report)
    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    for row in rows:
        row_id = row.get("id", "")
        if row_id in seen_ids:
            duplicate_ids.add(row_id)
        seen_ids.add(row_id)
        requested_category = row.get("operator_category_to_set_in_baselane", "").strip()
        category = baselane_category(requested_category)
        context_status = row.get("context_candidate_status", "").strip()
        if context_status == "automation_safe_native_split" and category in allowed:
            status = "ready_for_native_split"
            reason = "vendor is governed by a Baselane-native split rule; use native split plan instead of category-only source update"
        elif not category:
            status = "pending_category"
            reason = "operator_category_to_set_in_baselane is blank"
        elif category not in allowed:
            status = "invalid_category"
            reason = f"category is not in allowed Baselane category set: {category}"
        else:
            status = "ready_for_baselane_entry"
            reason = "category is allowed; enter it in Baselane source row, export again, then rerun weekly cron"
        records.append(
            {
                "id": row_id,
                "property": row.get("property"),
                "date": row.get("date"),
                "amount": row.get("amount"),
                "merchant": row.get("merchant"),
                "requested_operator_category_to_set_in_baselane": requested_category,
                "operator_category_to_set_in_baselane": category,
                "category_alias_applied": category != requested_category,
                "context_candidate_status": context_status,
                "validation_status": status,
                "validation_reason": reason,
            }
        )
    missing_expected_ids = sorted(expected_ids - seen_ids)
    unexpected_ids = sorted(seen_ids - expected_ids) if expected_ids else []
    invalid_count = sum(1 for record in records if record["validation_status"] == "invalid_category")
    pending_count = sum(1 for record in records if record["validation_status"] == "pending_category")
    ready_count = sum(1 for record in records if record["validation_status"] == "ready_for_baselane_entry")
    native_split_ready_count = sum(1 for record in records if record["validation_status"] == "ready_for_native_split")
    structural_issue_count = len(input_errors) + len(duplicate_ids) + len(missing_expected_ids) + len(unexpected_ids)
    no_pending_source_fix_rows = (
        not records
        and not expected_ids
        and corrections_report.get("status") == "ok"
        and count_like(corrections_report.get("remaining_count")) == 0
    )
    status = (
        "ok"
        if (
            (records and pending_count == 0 and invalid_count == 0 and structural_issue_count == 0)
            or (no_pending_source_fix_rows and structural_issue_count == 0)
        )
        else "review"
    )
    return {
        "generated_at": iso_z(),
        "status": status,
        "policy": "Validation only; does not mutate Baselane, public docs, Lofty PM, Telegram, or email.",
        "corrections_csv": str(corrections_csv),
        "corrections_report": str(corrections_report_path),
        "allowed_category_count": len(allowed),
        "allowed_categories": sorted(allowed),
        "row_count": len(records),
        "ready_count": ready_count,
        "native_split_ready_count": native_split_ready_count,
        "pending_count": pending_count,
        "invalid_count": invalid_count,
        "structural_issue_count": structural_issue_count,
        "input_errors": input_errors,
        "duplicate_ids": sorted(duplicate_ids),
        "missing_expected_ids": missing_expected_ids,
        "unexpected_ids": unexpected_ids,
        "next_action": (
            "Enter validated categories in Baselane, export again, then rerun weekly cron."
            if status == "ok" and records
            else "No pending source-fix category entries; rerun weekly cron."
            if status == "ok"
            else "Fill valid categories for pending rows and fix any invalid categories before changing Baselane source data."
        ),
        "records": records,
    }


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "id",
        "property",
        "date",
        "amount",
        "merchant",
        "operator_category_to_set_in_baselane",
        "context_candidate_status",
        "validation_status",
        "validation_reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in fieldnames})


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ECO GL Source-Fix Correction Validation",
        "",
        f"- Status: `{report['status']}`",
        f"- Rows: `{report['row_count']}`",
        f"- Ready: `{report['ready_count']}`",
        f"- Pending: `{report['pending_count']}`",
        f"- Invalid: `{report['invalid_count']}`",
        f"- Structural issues: `{report['structural_issue_count']}`",
        f"- Policy: {report['policy']}",
        f"- Next action: {report['next_action']}",
        "",
        "## Pending / Invalid Rows",
        "",
    ]
    flagged = [record for record in report.get("records") or [] if record.get("validation_status") != "ready_for_baselane_entry"]
    for record in flagged:
        lines.append(
            f"- `{record.get('id')}` — {record.get('property')} — {record.get('date')} — {record.get('amount')} — "
            f"{record.get('merchant')} — `{record.get('validation_status')}`"
        )
    if not flagged:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate manually filled Baselane source-fix correction worksheet.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--corrections-csv", type=Path)
    parser.add_argument("--corrections-report", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()

    root = args.root
    corrections_csv = args.corrections_csv or default_corrections_csv(root)
    report = build_report(
        root,
        corrections_csv,
        args.corrections_report or default_corrections_report(root, corrections_csv),
    )
    report_path = args.report or root / "reports" / "baselane_ecogl_source_fix_correction_validation.json"
    csv_path = args.csv or root / "reports" / "baselane_ecogl_source_fix_correction_validation.csv"
    markdown_path = args.markdown or root / "reports" / "baselane_ecogl_source_fix_correction_validation.md"
    write_json(report_path, report)
    write_csv(csv_path, report["records"])
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "row_count": report["row_count"],
                "ready_count": report["ready_count"],
                "pending_count": report["pending_count"],
                "invalid_count": report["invalid_count"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
