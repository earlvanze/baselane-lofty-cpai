#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coownership_mortgage_policy import P_AND_I_DAO_PROPERTIES, normalize_policy_key
from lofty_property_paths import resolve_index_property_path


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def load_index(index_csv: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    if not index_csv.is_file():
        return rows
    with index_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            property_path = str(resolve_index_property_path(row)[0])
            if property_path:
                rows[property_path] = row
    return rows


def first_candidate(candidates: list[str]) -> str | None:
    return candidates[0] if candidates else None


def key_present(required_key: str, keys: set[str]) -> bool:
    return any(required_key and key and (required_key == key or required_key in key or key in required_key) for key in keys)


def build_manifest(
    index_csv: Path,
    guarded_apply_report: Path,
    run_month: str,
    *,
    require_p_and_i_daos: bool = True,
) -> dict[str, Any]:
    index_rows = load_index(index_csv)
    guarded_apply = read_json(guarded_apply_report)
    guarded_records = guarded_apply.get("records") if isinstance(guarded_apply.get("records"), list) else []
    records: list[dict[str, Any]] = []
    pending_update_reviews = 0
    pending_financial_reviews = 0
    ready_update_count = 0
    ready_financial_count = 0
    issue_count = 0
    skipped_excluded_records: list[dict[str, Any]] = []
    approved_update_quality_issue_counts: dict[str, int] = {}
    approved_update_quality_issue_records: list[dict[str, Any]] = []

    for record in guarded_records:
        if not isinstance(record, dict):
            continue
        property_path = str(record.get("property_path") or "")
        index_row = index_rows.get(property_path, {})
        updates = record.get("updates") or {}
        financials = record.get("financials") or {}
        update_status = updates.get("status") or "unknown"
        financial_status = financials.get("status") or "unknown"
        update_candidates = [str(path) for path in updates.get("approved_candidates") or []]
        financial_candidates = [str(path) for path in financials.get("approved_candidates") or []]
        update_quality_issues = [str(issue) for issue in updates.get("approved_update_quality_issues") or [] if issue]
        for issue in update_quality_issues:
            approved_update_quality_issue_counts[issue] = approved_update_quality_issue_counts.get(issue, 0) + 1
        if update_quality_issues:
            approved_update_quality_issue_records.append(
                {
                    "property_name": Path(property_path).name,
                    "property_path": property_path,
                    "update_status": update_status,
                    "issues": update_quality_issues,
                    "approved_entry": updates.get("approved_entry"),
                }
            )
        update_review_target = first_candidate(update_candidates)
        financial_review_target = first_candidate(financial_candidates)
        update_skipped = update_status.startswith("skipped_") or update_status.startswith("excluded_")
        financial_skipped = financial_status.startswith("skipped_") or financial_status.startswith("excluded_")
        if update_skipped and financial_skipped:
            skipped_excluded_records.append(
                {
                    "property_name": Path(property_path).name,
                    "managed_name": index_row.get("managed_name") or index_row.get("name"),
                    "property_path": property_path,
                    "notes": index_row.get("notes"),
                    "update_status": update_status,
                    "financial_status": financial_status,
                }
            )
            continue
        update_ready = update_status in {"ready", "already_applied", "applied"} or update_skipped
        financial_ready = financial_status in {"ready", "already_applied", "applied"} or financial_skipped
        if update_skipped:
            ready_update_count += 1
        elif update_status == "needs_reviewed_entry":
            pending_update_reviews += 1
        elif update_ready:
            ready_update_count += 1
        else:
            issue_count += 1
        if financial_skipped:
            ready_financial_count += 1
        elif financial_status == "no_approved_financials_draft":
            pending_financial_reviews += 1
        elif financial_ready:
            ready_financial_count += 1
        else:
            issue_count += 1

        records.append(
            {
                "property_name": Path(property_path).name,
                "managed_name": index_row.get("managed_name") or index_row.get("name"),
                "property_path": property_path,
                "notes": index_row.get("notes"),
                "draft_path": record.get("draft_path") or index_row.get("draft_path"),
                "updates_md": record.get("updates_md"),
                "financials_md": record.get("financials_md"),
                "update_status": update_status,
                "financial_status": financial_status,
                "update_review_target": update_review_target,
                "financial_review_target": financial_review_target,
                "update_approved_candidates": update_candidates,
                "financial_approved_candidates": financial_candidates,
                "approved_update_quality_issues": update_quality_issues,
                "next_actions": [
                    action
                    for action in [
                        "Review owner update draft and save approved text to update_review_target." if update_status == "needs_reviewed_entry" else None,
                        "Review canonical FINANCIALS.md and save approved copy to financial_review_target." if financial_status == "no_approved_financials_draft" else None,
                        "Resolve update guard/apply issue before publish." if not update_skipped and update_status not in {"needs_reviewed_entry", "ready", "already_applied", "applied"} else None,
                        "Resolve financial guard/apply issue before publish." if not financial_skipped and financial_status not in {"no_approved_financials_draft", "ready", "already_applied", "applied"} else None,
                    ]
                    if action
                ],
            }
        )

    source_issues: list[str] = []
    guarded_status = str(guarded_apply.get("status") or "")
    if guarded_apply.get("status") in {"missing", "unreadable"}:
        source_issues.append(f"guarded_apply_report_{guarded_apply.get('status')}")
    if guarded_status.startswith("skipped_"):
        source_issues.append(f"guarded_apply_status={guarded_status}")
    if guarded_status == "in_progress" or guarded_apply.get("in_progress") is True:
        source_issues.append("guarded_apply_in_progress")
    if not guarded_records:
        source_issues.append("guarded_apply_records_empty")
    if not index_rows:
        source_issues.append("monthly_index_rows_empty")
    active_index_record_count = sum(
        1
        for row in index_rows.values()
        if str(row.get("status") or "").strip().lower() in {"existing", "new", "active"}
    )
    if guarded_records and len(guarded_records) < active_index_record_count:
        source_issues.append(f"guarded_apply_incomplete_records={len(guarded_records)}/{active_index_record_count}")
    manifest_property_keys = {
        normalize_policy_key(record.get("property_name") or record.get("property_path"))
        for record in records
        if isinstance(record, dict)
    }
    manifest_coverage_property_keys = {
        *manifest_property_keys,
        *(
            normalize_policy_key(record.get("property_name") or record.get("property_path"))
            for record in skipped_excluded_records
            if isinstance(record, dict)
        ),
    }
    active_index_property_keys = {
        normalize_policy_key(path)
        for path, row in index_rows.items()
        if str(row.get("status") or "").strip().lower() in {"existing", "new", "active", "would_create"}
    }
    missing_required_p_and_i_daos = (
        sorted(
            property_name
            for property_name in P_AND_I_DAO_PROPERTIES
            if not key_present(normalize_policy_key(property_name), active_index_property_keys)
            and not key_present(normalize_policy_key(property_name), manifest_coverage_property_keys)
        )
        if require_p_and_i_daos
        else []
    )
    stale_required_p_and_i_daos = (
        sorted(
            property_name
            for property_name in P_AND_I_DAO_PROPERTIES
            if key_present(normalize_policy_key(property_name), active_index_property_keys)
            and not key_present(normalize_policy_key(property_name), manifest_coverage_property_keys)
        )
        if require_p_and_i_daos
        else []
    )
    source_issues.extend(
        f"required_p_and_i_dao_missing_from_review_manifest={property_name}"
        for property_name in missing_required_p_and_i_daos
    )
    source_issues.extend(
        f"required_p_and_i_dao_missing_from_guarded_apply={property_name}"
        for property_name in stale_required_p_and_i_daos
    )
    status = "ok" if not pending_update_reviews and not pending_financial_reviews and not issue_count and not source_issues else "review"
    return {
        "generated_at": iso_z(),
        "run_month": run_month,
        "status": status,
        "guarded_apply_status": guarded_apply.get("status"),
        "guarded_apply_record_count": len(guarded_records),
        "monthly_index_record_count": len(index_rows),
        "monthly_index_active_record_count": active_index_record_count,
        "source_issue_count": len(source_issues),
        "source_issues": source_issues,
        "missing_required_p_and_i_daos": missing_required_p_and_i_daos,
        "stale_required_p_and_i_daos": stale_required_p_and_i_daos,
        "next_action": (
            "Run guarded monthly apply/review generation with a non-empty monthly index before building candidate packets."
            if source_issues
            else None
        ),
        "property_count": len(records),
        "pending_update_review_count": pending_update_reviews,
        "pending_financial_review_count": pending_financial_reviews,
        "ready_update_count": ready_update_count,
        "ready_financial_count": ready_financial_count,
        "skipped_excluded_record_count": len(skipped_excluded_records),
        "skipped_excluded_records": sorted(skipped_excluded_records, key=lambda item: item["property_name"]),
        "issue_count": issue_count,
        "approved_update_quality_issue_count": sum(approved_update_quality_issue_counts.values()),
        "approved_update_quality_issue_counts": dict(sorted(approved_update_quality_issue_counts.items())),
        "approved_update_quality_issue_records": sorted(approved_update_quality_issue_records, key=lambda item: item["property_name"])[:200],
        "index_csv": str(index_csv),
        "guarded_apply_report": str(guarded_apply_report),
        "records": sorted(records, key=lambda item: item["property_name"]),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Lofty Monthly Review Manifest",
        "",
        f"- Run month: `{report['run_month']}`",
        f"- Status: `{report['status']}`",
        f"- Properties: `{report['property_count']}`",
        f"- Pending owner update reviews: `{report['pending_update_review_count']}`",
        f"- Pending financial reviews: `{report['pending_financial_review_count']}`",
        f"- Guard/apply issues: `{report['issue_count']}`",
        f"- Approved update quality issues: `{report.get('approved_update_quality_issue_count', 0)}`",
        "",
        "## Required Review Actions",
        "",
    ]
    for record in report["records"]:
        if not record["next_actions"]:
            continue
        lines.append(f"### {record['property_name']}")
        if record.get("notes"):
            lines.append(f"- Notes: {record['notes']}")
        lines.append(f"- Owner update draft: `{record.get('draft_path')}`")
        if record.get("update_review_target"):
            lines.append(f"- Save approved update to: `{record['update_review_target']}`")
        lines.append(f"- Canonical financials: `{record.get('financials_md')}`")
        if record.get("financial_review_target"):
            lines.append(f"- Save approved financials to: `{record['financial_review_target']}`")
        for action in record["next_actions"]:
            lines.append(f"- Action: {action}")
        lines.append("")
    quality_records = report.get("approved_update_quality_issue_records") or []
    if quality_records:
        lines.append("## Approved Update Quality Issues")
        lines.append("")
        for record in quality_records[:50]:
            lines.append(f"- `{record.get('property_name')}`: `{', '.join(record.get('issues') or [])}`")
        lines.append("")
    if all(not record["next_actions"] for record in report["records"]):
        lines.append("No review actions are pending.")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a reviewer-facing manifest for monthly Lofty update and financial approval artifacts.")
    parser.add_argument("--index-csv", required=True, type=Path)
    parser.add_argument("--guarded-apply-report", required=True, type=Path)
    parser.add_argument("--run-month", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    args = parser.parse_args()

    report = build_manifest(args.index_csv, args.guarded_apply_report, args.run_month)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "property_count", "pending_update_review_count", "pending_financial_review_count", "issue_count")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
