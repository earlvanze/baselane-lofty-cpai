#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


INVALID_EMPTY_CATEGORIES = {"", "uncategorized", "uncategorized expense"}
APPLY_ENV = "BASELANE_SOURCE_FIX_APPLY"
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


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.is_file():
        return [], [f"missing_csv:{path}"]
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            return [{key: str(value or "") for key, value in row.items()} for row in reader], []
    except Exception as exc:  # noqa: BLE001
        return [], [f"unreadable_csv:{path}:{exc}"]


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def apply_preflight(root: Path) -> dict[str, Any]:
    reports = root / "reports"
    daily = read_json(reports / "baselane_daily_sync_report.json")
    sync = read_json(reports / "baselane_sync_cdp_report.json")
    split = read_json(reports / "split_ledger_public_financials_last.json")
    weekly_cf = read_json(reports / "baselane_weekly_cf_statement_sync_report.json")
    split_mismatch_count = max(count(split.get("output_mismatch_count")), count(daily.get("split_output_mismatch_count")))
    split_unresolved_count = max(count(split.get("unresolved_property_count")), count(daily.get("split_unresolved_property_count")))
    source_cash_violations = count(weekly_cf.get("source_cash_balance_violation_count"))
    daily_status = str(daily.get("status") or "")
    source_fixable_export_guard_review = (
        daily_status == "review"
        and sync.get("status") == "review"
        and sync.get("reason") == "export_guard_review"
        and sync.get("export_failure_class") == "baselane_export_guard_review"
        and sync.get("canonical_overwrite_blocked") is True
    )
    source_fixable_hemlane_self_review = (
        daily_status == "review"
        and sync.get("status") == "ok"
        and str(daily.get("failed_step") or "") == "baselane_hemlane_auto_tag_source_fix"
        and split_mismatch_count == 0
        and split_unresolved_count == 0
        and source_cash_violations == 0
    )
    daily_sync_review_allowed_for_source_fix = source_fixable_export_guard_review or source_fixable_hemlane_self_review
    issues = [
        blocker
        for blocker in [
            None if daily_status == "ok" or daily_sync_review_allowed_for_source_fix else f"daily_sync={daily.get('status')}",
            None if split.get("status") in {"NO_REPLY", "ok"} else f"split={split.get('status')}",
            None if split_mismatch_count == 0 else f"split_output_mismatch_count={split_mismatch_count}",
            None if split_unresolved_count == 0 else f"split_unresolved_property_count={split_unresolved_count}",
            None if source_cash_violations == 0 else f"source_cash_balance_violation_count={source_cash_violations}",
        ]
        if blocker
    ]
    return {
        "status": "ok" if not issues else "review",
        "issue_count": len(issues),
        "issues": issues,
        "evidence": {
            "daily_sync_status": daily_status,
            "daily_sync_review_allowed_for_source_fix": daily_sync_review_allowed_for_source_fix,
            "daily_sync_review_allowed_reason": (
                "export_guard_review"
                if source_fixable_export_guard_review
                else ("hemlane_auto_tag_self_review" if source_fixable_hemlane_self_review else None)
            ),
            "daily_failed_step": daily.get("failed_step"),
            "sync_report_status": sync.get("status"),
            "sync_report_reason": sync.get("reason"),
            "sync_report_failure_class": sync.get("export_failure_class"),
            "split_status": split.get("status"),
            "split_output_mismatch_count": split_mismatch_count,
            "split_unresolved_property_count": split_unresolved_count,
            "source_cash_balance_violation_count": source_cash_violations,
            "weekly_cf_status": weekly_cf.get("status"),
        },
    }


def row_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        norm_text(row.get("Property") or row.get("property")),
        norm_date(row.get("ISODate") or row.get("Date") or row.get("date")),
        norm_amount(row.get("Amount") or row.get("amount")),
        norm_text(row.get("Merchant") or row.get("merchant") or row.get("merchantName")),
    )


def category_is_empty(value: object) -> bool:
    return str(value or "").strip().lower() in INVALID_EMPTY_CATEGORIES


def baselane_category(value: object) -> str:
    category = str(value or "").strip()
    return BASELANE_CATEGORY_ALIASES.get(category, category)


def build_category_tag_map(source_rows: list[dict[str, str]]) -> dict[str, set[str]]:
    tag_map: dict[str, set[str]] = {}
    for row in source_rows:
        category = baselane_category(row.get("Category"))
        tag_id = str(row.get("TagId") or "").strip()
        if not category or not tag_id:
            continue
        tag_map.setdefault(category, set()).add(tag_id)
    return tag_map


def source_rows_by_id(source_rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    rows_by_id: dict[str, dict[str, str]] = {}
    for row in source_rows:
        row_id = str(row.get("BaselaneId") or row.get("id") or "").strip()
        if row_id and row_id not in rows_by_id:
            rows_by_id[row_id] = row
    return rows_by_id


def graphql_payload(mutation_rows: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "operationName": "UpdateTransactions",
        "query": (
            "mutation UpdateTransactions($input: [UpdateTransaction!]) { "
            "updateTransactions(input: $input) { id tagId propertyId note } "
            "}"
        ),
        "variables": {
            "input": [
                {
                    "id": row["baselane_id"],
                    "tagId": row["target_tag_id"],
                    "propertyId": row["property_id"],
                }
                for row in mutation_rows
            ]
        },
    }


def classify_plan_row(
    plan_row: dict[str, str],
    source_by_id: dict[str, dict[str, str]],
    category_tag_map: dict[str, set[str]],
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": plan_row.get("id", ""),
        "property": plan_row.get("property", ""),
        "date": plan_row.get("date", ""),
        "amount": plan_row.get("amount", ""),
        "merchant": plan_row.get("merchant", ""),
        "category_to_set": plan_row.get("category_to_set", ""),
        "baselane_category_to_set": "",
        "category_alias_applied": False,
        "baselane_id": plan_row.get("baselane_id", ""),
        "match_status": plan_row.get("match_status", ""),
        "apply_status": "",
        "apply_reason": "",
        "target_tag_id": "",
        "property_id": "",
    }
    if record["match_status"] != "ready_current_source_index":
        if record["match_status"] == "already_applied_current_source_index":
            category = baselane_category(record["category_to_set"])
            record["baselane_category_to_set"] = category
            record["category_alias_applied"] = category != str(record["category_to_set"] or "").strip()
            record["apply_status"] = "already_applied"
            record["apply_reason"] = "current source index already has the requested category on all matching rows"
            return record
        record["apply_status"] = "blocked_not_ready_current_source_index"
        record["apply_reason"] = "apply plan row is not ready against the current source index"
        return record
    source_row = source_by_id.get(record["baselane_id"])
    if not source_row:
        record["apply_status"] = "blocked_missing_current_source_row"
        record["apply_reason"] = "Baselane ID is no longer present in current source transaction index"
        return record
    expected_key = (
        norm_text(record["property"]),
        norm_date(record["date"]),
        norm_amount(record["amount"]),
        norm_text(record["merchant"]),
    )
    if row_key(source_row) != expected_key:
        record["apply_status"] = "blocked_current_source_row_changed"
        record["apply_reason"] = "current source row no longer matches property/date/amount/merchant from apply plan"
        return record
    category = baselane_category(record["category_to_set"])
    record["baselane_category_to_set"] = category
    record["category_alias_applied"] = category != str(record["category_to_set"] or "").strip()
    current_category = str(source_row.get("Category") or "").strip()
    current_baselane_category = baselane_category(current_category)
    if current_baselane_category == category:
        tag_ids = category_tag_map.get(category) or set()
        if len(tag_ids) == 1:
            record["target_tag_id"] = next(iter(tag_ids))
        record["property_id"] = str(source_row.get("PropertyId") or "").strip()
        record["apply_status"] = "already_applied"
        record["apply_reason"] = "current source index already has the requested category"
        return record
    if not category_is_empty(current_category):
        record["apply_status"] = "blocked_current_category_conflict"
        record["apply_reason"] = f"current source index category is already set to {current_category!r}"
        return record
    tag_ids = category_tag_map.get(category) or set()
    if len(tag_ids) != 1:
        record["apply_status"] = "blocked_category_tag_mapping"
        record["apply_reason"] = f"category maps to {len(tag_ids)} current TagId values; expected exactly one"
        return record
    property_id = str(source_row.get("PropertyId") or "").strip()
    if not property_id:
        record["apply_status"] = "blocked_missing_property_id"
        record["apply_reason"] = "current source index row lacks PropertyId"
        return record
    record["target_tag_id"] = next(iter(tag_ids))
    record["property_id"] = property_id
    record["apply_status"] = "ready_to_apply"
    record["apply_reason"] = "exact current source row and unique category TagId verified"
    return record


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "id",
        "property",
        "date",
        "amount",
        "merchant",
        "category_to_set",
        "baselane_category_to_set",
        "category_alias_applied",
        "baselane_id",
        "target_tag_id",
        "property_id",
        "match_status",
        "apply_status",
        "apply_reason",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in fields})


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Baselane ECO GL Source-Fix Apply",
        "",
        f"- Status: `{report['status']}`",
        f"- Mode: `{report['mode']}`",
        f"- Ready to apply: `{report['ready_to_apply_count']}`",
        f"- Already applied: `{report['already_applied_count']}`",
        f"- Blocked: `{report['blocked_count']}`",
        f"- Applied: `{report['applied_count']}`",
        f"- Failed: `{report['failed_count']}`",
        f"- Policy: {report['policy']}",
        "",
        "## Records",
        "",
    ]
    for record in report.get("records") or []:
        lines.append(
            f"- `{record.get('id')}` — Baselane `{record.get('baselane_id')}` — "
            f"{record.get('property')} — {record.get('merchant')} → `{record.get('category_to_set')}`"
            f" / Baselane `{record.get('baselane_category_to_set') or record.get('category_to_set')}` "
            f"(TagId `{record.get('target_tag_id') or 'missing'}`): `{record.get('apply_status')}`"
        )
        lines.append(f"  - {record.get('apply_reason')}")
    if not report.get("records"):
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def execute_graphql(root: Path, payload_path: Path, timeout_seconds: int) -> tuple[int, str, str]:
    helper = root / "scripts" / "baselane_graphql_via_cdp.js"
    proc = subprocess.run(
        ["node", str(helper), str(payload_path)],
        cwd=str(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
    )
    return proc.returncode, proc.stdout, proc.stderr


def build_report(
    root: Path,
    apply_plan_csv: Path,
    source_index_csv: Path,
    payload_path: Path,
    apply: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    plan_rows, plan_errors = read_csv(apply_plan_csv)
    source_rows, source_errors = read_csv(source_index_csv)
    category_tag_map = build_category_tag_map(source_rows)
    source_by_id = source_rows_by_id(source_rows)
    records = [classify_plan_row(row, source_by_id, category_tag_map) for row in plan_rows]
    mutation_rows = [record for record in records if record["apply_status"] == "ready_to_apply"]
    payload = graphql_payload(mutation_rows)
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    ready_count = len(mutation_rows)
    already_count = sum(1 for record in records if record["apply_status"] == "already_applied")
    blocked_count = len(records) - ready_count - already_count
    structural_issue_count = len(plan_errors) + len(source_errors)
    mode = "apply" if apply else "dry_run"
    applied_count = 0
    failed_count = 0
    graphql_result: dict[str, Any] | None = None
    apply_allowed = apply and os.environ.get(APPLY_ENV) == "1"
    preflight = apply_preflight(root)
    if apply and preflight["issue_count"]:
        structural_issue_count += preflight["issue_count"]
        failed_count = ready_count
        graphql_result = {
            "status": "blocked",
            "error": "preflight blocked live Baselane source mutation",
            "preflight_issues": preflight["issues"],
        }
    if apply and not apply_allowed:
        structural_issue_count += 1
        failed_count = ready_count
        graphql_result = {
            "status": "blocked",
            "error": f"--apply requires {APPLY_ENV}=1",
        }
    elif apply and preflight["issue_count"]:
        pass
    elif apply and ready_count:
        try:
            return_code, stdout, stderr = execute_graphql(root, payload_path, timeout_seconds)
            parsed = json.loads(stdout) if stdout.strip() else {}
            errors = parsed.get("errors") or []
            updated = ((parsed.get("data") or {}).get("updateTransactions") or [])
            failed_count = len(errors)
            applied_count = len(updated) if not errors and return_code == 0 else 0
            graphql_result = {
                "return_code": return_code,
                "error_count": len(errors),
                "updated_count": len(updated),
                "errors": [{"message": error.get("message"), "path": error.get("path")} for error in errors],
                "stderr_tail": stderr.strip().splitlines()[-3:],
            }
        except subprocess.TimeoutExpired as exc:
            failed_count = ready_count
            graphql_result = {"status": "timeout", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            failed_count = ready_count
            graphql_result = {"status": "error", "error": str(exc)}
    elif apply:
        graphql_result = {"status": "skipped", "reason": "no ready_to_apply rows"}

    if structural_issue_count or blocked_count or failed_count:
        status = "review"
    else:
        status = "ok"
    return {
        "generated_at": iso_z(),
        "status": status,
        "mode": mode,
        "policy": "Dry-run by default; live Baselane mutation requires --apply, BASELANE_SOURCE_FIX_APPLY=1, and clean daily/split/source-cash preflight.",
        "baselane_category_aliases": BASELANE_CATEGORY_ALIASES,
        "apply_plan_csv": str(apply_plan_csv),
        "source_index_csv": str(source_index_csv),
        "payload_path": str(payload_path),
        "row_count": len(records),
        "ready_to_apply_count": ready_count,
        "already_applied_count": already_count,
        "blocked_count": blocked_count,
        "applied_count": applied_count,
        "failed_count": failed_count,
        "structural_issue_count": structural_issue_count,
        "plan_errors": plan_errors,
        "source_errors": source_errors,
        "apply_allowed": apply_allowed,
        "apply_preflight": preflight,
        "graphql_result": graphql_result,
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Guarded Baselane ECO GL source-fix apply workflow.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--apply-plan-csv", type=Path)
    parser.add_argument("--source-index-csv", type=Path)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    root = args.root
    report_path = args.report or root / "reports" / "baselane_ecogl_source_fix_apply.json"
    csv_path = args.csv or root / "reports" / "baselane_ecogl_source_fix_apply.csv"
    markdown_path = args.markdown or root / "reports" / "baselane_ecogl_source_fix_apply.md"
    payload_path = args.payload or root / "reports" / "baselane_ecogl_source_fix_apply_payload.json"
    report = build_report(
        root=root,
        apply_plan_csv=args.apply_plan_csv or root / "reports" / "baselane_ecogl_source_fix_apply_plan.csv",
        source_index_csv=args.source_index_csv or root / "reports" / "baselane_source_transaction_index.csv",
        payload_path=payload_path,
        apply=args.apply,
        timeout_seconds=args.timeout_seconds,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(csv_path, report.get("records") or [])
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in [
                    "status",
                    "mode",
                    "row_count",
                    "ready_to_apply_count",
                    "already_applied_count",
                    "blocked_count",
                    "applied_count",
                    "failed_count",
                ]
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
