#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RECOMMENDATION_GROUPS = {
    "ready_for_deterministic_auto_approval": "needs_current_source_index",
    "blocked_email_invoice_required": "needs_source_evidence",
    "review_email_invoice_evidence": "needs_category_decision",
    "blocked_no_support": "needs_source_evidence",
    "review_weak_support": "needs_source_evidence",
    "blocked_conflicting_support": "needs_category_decision",
    "blocked_insufficient_evidence": "needs_source_evidence",
}


def evidence_status(record: dict[str, Any]) -> str:
    historical_status = str(record.get("historical_evidence_status") or "")
    context_status = str(record.get("context_candidate_status") or "")
    if context_status.startswith("automation_safe_") or context_status in {
        "context_only_exact_amount_notes",
        "context_only_notes",
        "context_only_same_merchant",
        "conflicting_context",
    }:
        return context_status
    return historical_status or context_status or "unknown"


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


def count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def index_apply_records(apply_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = {}
    for record in apply_plan.get("records") or []:
        if (
            isinstance(record, dict)
            and record.get("id")
            and record.get("match_status") == "ready_current_source_index"
            and str(record.get("baselane_id") or "").strip()
        ):
            records[str(record["id"])] = record
    return records


def index_apply_plan_records(apply_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(record["id"]): record
        for record in apply_plan.get("records") or []
        if isinstance(record, dict) and record.get("id")
    }


def index_resolved_apply_records(apply_report: dict[str, Any], *, current: bool) -> dict[str, dict[str, Any]]:
    if not current or apply_report.get("status") in {"missing", "unreadable"}:
        return {}
    resolved_statuses = {"already_applied", "applied"}
    return {
        str(record["id"]): record
        for record in apply_report.get("records") or []
        if (
            isinstance(record, dict)
            and record.get("id")
            and str(record.get("apply_status") or "").strip() in resolved_statuses
        )
    }


def index_validation_records(validation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(record["id"]): record
        for record in validation.get("records") or []
        if isinstance(record, dict) and record.get("id")
    }


def index_source_plan_actions(source_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(record["id"]): record
        for record in source_plan.get("actions") or []
        if isinstance(record, dict) and record.get("id")
    }


def next_action_for(
    record: dict[str, Any],
    group: str,
    ready_record: dict[str, Any] | None,
    apply_plan_record: dict[str, Any] | None = None,
    resolved_apply_record: dict[str, Any] | None = None,
    source_plan_action: dict[str, Any] | None = None,
) -> str:
    if group == "already_applied":
        reason = str((resolved_apply_record or {}).get("apply_reason") or "").strip()
        if reason:
            return f"Already reflected in the current Baselane export; no live source mutation needed: {reason}."
        return "Already reflected in the current Baselane export; no live source mutation needed."
    if group == "ready_native_split":
        return "Ready for guarded Baselane native split workflow; do not apply category-only mutation to the parent row."
    if ready_record:
        return "Ready for guarded Baselane source mutation; requires explicit BASELANE_SOURCE_FIX_APPLY=1."
    if group == "needs_current_source_index":
        reason = str((apply_plan_record or {}).get("match_reason") or "").strip()
        if reason:
            return f"Auto-approved category is blocked from live apply until current Baselane ID proof exists: {reason}."
        return "Auto-approved category is blocked from live apply until current Baselane ID proof exists; rerun deterministic sync/export."
    if (source_plan_action or {}).get("action_type") == "reconcile_pm_fee_source_timing":
        return (
            "Open the cited Cash Flow source row, locate the actual Baselane/ECO PM-fee transaction or "
            "accounting-approved accrual basis, then regenerate the source-fix plan. Do not use the Cash Flow "
            "value itself to create a synthetic Baselane transaction."
        )
    recommendation = str(record.get("autonomy_recommendation") or "")
    if recommendation == "blocked_no_support":
        checked = count(record.get("document_checked_file_count"))
        supported = count(record.get("document_support_count"))
        if checked and not supported:
            return f"Public-doc scan checked {checked} file(s) with no category proof; get source invoice/receipt or explicit category decision."
        return "Find source evidence or a deterministic recurring pattern; do not infer from merchant alone."
    if recommendation == "blocked_email_invoice_required":
        query = str(record.get("email_invoice_search_query") or "").strip()
        if query:
            return f"Find matching invoice/receipt in email with the generated query; do not infer from person-payment rail. Query: {query}"
        return "Find matching invoice/receipt in email; do not infer from person-payment rail."
    if recommendation == "review_email_invoice_evidence":
        return "Review matched email invoice/receipt artifact, then set the exact category supported by that evidence."
    if recommendation == "review_weak_support":
        return "Confirm the weak historical category with source evidence before approving."
    if recommendation == "blocked_conflicting_support":
        return "Resolve category conflict from source evidence; do not pick either historical category blindly."
    if group == "needs_category_decision":
        return "Set one allowed category only with supporting evidence."
    return "Resolve source evidence, set an allowed category, then rerun validation."


def queue_record(
    record: dict[str, Any],
    ready_record: dict[str, Any] | None,
    validation_record: dict[str, Any] | None = None,
    apply_plan_record: dict[str, Any] | None = None,
    resolved_apply_record: dict[str, Any] | None = None,
    source_plan_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recommendation = str(record.get("autonomy_recommendation") or "unknown")
    validation_status = str((validation_record or {}).get("validation_status") or "")
    if resolved_apply_record:
        group = "already_applied"
    elif validation_status == "ready_for_native_split":
        group = "ready_native_split"
    else:
        group = "ready_to_apply" if ready_record else RECOMMENDATION_GROUPS.get(recommendation, "needs_category_decision")
    category = ready_record.get("category_to_set") if ready_record else record.get("category_to_set") or record.get("candidate_category")
    if resolved_apply_record and resolved_apply_record.get("category_to_set"):
        category = resolved_apply_record.get("category_to_set")
    return {
        "id": record.get("id"),
        "group": group,
        "property": record.get("property"),
        "date": record.get("date"),
        "amount": record.get("amount"),
        "merchant": record.get("merchant"),
        "description": record.get("description"),
        "autonomy_recommendation": recommendation,
        "historical_evidence_status": record.get("historical_evidence_status"),
        "historical_category_counts": record.get("historical_category_counts"),
        "context_candidate_status": record.get("context_candidate_status"),
        "evidence_status": evidence_status(record),
        "document_support_count": count(record.get("document_support_count")),
        "document_checked_file_count": count(record.get("document_checked_file_count")),
        "document_limit_reached": record.get("document_limit_reached"),
        "document_category_counts": record.get("document_category_counts"),
        "email_invoice_evidence_required": str(record.get("email_invoice_evidence_required") or "").lower() == "true",
        "payment_rail": record.get("payment_rail") or "",
        "payee_tokens": record.get("payee_tokens") or "",
        "email_invoice_search_query": record.get("email_invoice_search_query") or "",
        "email_invoice_expected_window": record.get("email_invoice_expected_window") or "",
        "local_mail_invoice_status": record.get("local_mail_invoice_status") or "",
        "local_mail_invoice_match_count": count(record.get("local_mail_invoice_match_count")),
        "local_mail_invoice_checked_file_count": count(record.get("local_mail_invoice_checked_file_count")),
        "local_mail_invoice_matches": record.get("local_mail_invoice_matches") or "",
        "gws_mail_invoice_status": record.get("gws_mail_invoice_status") or "",
        "gws_mail_invoice_match_count": count(record.get("gws_mail_invoice_match_count")),
        "gws_mail_invoice_matches": record.get("gws_mail_invoice_matches") or "",
        "gws_mail_invoice_errors": record.get("gws_mail_invoice_errors") or "",
        "category_to_set": category or "",
        "baselane_id": (ready_record or resolved_apply_record or {}).get("baselane_id") or "",
        "match_status": (ready_record or resolved_apply_record or {}).get("match_status") or "",
        "apply_plan_match_status": (apply_plan_record or {}).get("match_status") or "",
        "apply_plan_match_reason": (apply_plan_record or {}).get("match_reason") or "",
        "apply_status": (resolved_apply_record or {}).get("apply_status") or "",
        "apply_reason": (resolved_apply_record or {}).get("apply_reason") or "",
        "validation_status": validation_status,
        "source_action_type": (source_plan_action or {}).get("action_type") or "",
        "source_month": (source_plan_action or {}).get("month") or "",
        "cash_flow_source_file": (source_plan_action or {}).get("source_file") or "",
        "cash_flow_source_row": (source_plan_action or {}).get("source_row") or "",
        "cash_flow_value": (source_plan_action or {}).get("cf_value") or "",
        "source_gl_total": (source_plan_action or {}).get("gl_total") or "",
        "source_fix_reason": (source_plan_action or {}).get("reason") or "",
        "evidence_needed": record.get("evidence_needed"),
        "next_action": next_action_for(
            record,
            group,
            ready_record,
            apply_plan_record,
            resolved_apply_record,
            source_plan_action,
        ),
    }


def operator_brief(queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    group_labels = {
        "ready_to_apply": "Apply guarded source fixes",
        "ready_native_split": "Apply guarded native splits",
        "needs_current_source_index": "Refresh current Baselane IDs",
        "needs_source_evidence": "Find source evidence",
        "needs_category_decision": "Resolve category conflicts",
        "already_applied": "Already applied",
    }
    brief = []
    for group in (
        "ready_to_apply",
        "ready_native_split",
        "needs_current_source_index",
        "needs_source_evidence",
        "needs_category_decision",
        "already_applied",
    ):
        rows = [record for record in queue if record.get("group") == group]
        if not rows:
            continue
        brief.append(
            {
                "group": group,
                "label": group_labels[group],
                "count": len(rows),
                "rows": rows,
            }
        )
    return brief


def build_report(root: Path) -> dict[str, Any]:
    reports = root / "reports"
    approval_path = reports / "baselane_ecogl_source_fix_approval.json"
    apply_plan_path = reports / "baselane_ecogl_source_fix_apply_plan.json"
    apply_report_path = reports / "baselane_ecogl_source_fix_apply.json"
    validation_path = reports / "baselane_ecogl_source_fix_correction_validation.json"
    source_plan_path = reports / "baselane_ecogl_source_fix_plan.json"
    approval = read_json(approval_path)
    apply_plan = read_json(apply_plan_path)
    apply_report = read_json(apply_report_path)
    validation = read_json(validation_path)
    source_plan = read_json(source_plan_path)
    apply_report_current = mtime(apply_report_path) >= mtime(apply_plan_path)
    ready_by_id = index_apply_records(apply_plan)
    apply_plan_by_id = index_apply_plan_records(apply_plan)
    resolved_apply_by_id = index_resolved_apply_records(apply_report, current=apply_report_current)
    validation_by_id = index_validation_records(validation)
    source_plan_by_id = index_source_plan_actions(source_plan)
    approvals = [record for record in approval.get("approvals") or [] if isinstance(record, dict)]
    queue = [
        queue_record(
            record,
            ready_by_id.get(str(record.get("id"))),
            validation_by_id.get(str(record.get("id"))),
            apply_plan_by_id.get(str(record.get("id"))),
            resolved_apply_by_id.get(str(record.get("id"))),
            source_plan_by_id.get(str(record.get("id"))),
        )
        for record in approvals
    ]
    group_counts = Counter(str(record.get("group") or "unknown") for record in queue)
    recommendation_counts = Counter(str(record.get("autonomy_recommendation") or "unknown") for record in queue)
    ready_to_apply_count = count(group_counts.get("ready_to_apply"))
    ready_native_split_count = count(group_counts.get("ready_native_split"))
    needs_current_source_index_count = count(group_counts.get("needs_current_source_index"))
    already_applied_count = count(group_counts.get("already_applied"))
    decision_required_count = (
        len(queue)
        - ready_to_apply_count
        - ready_native_split_count
        - needs_current_source_index_count
        - already_applied_count
    )
    email_invoice_required_count = sum(1 for record in queue if record.get("email_invoice_evidence_required") is True)
    local_mail_invoice_match_count = sum(1 for record in queue if count(record.get("local_mail_invoice_match_count")) > 0)
    gws_mail_invoice_match_count = sum(1 for record in queue if count(record.get("gws_mail_invoice_match_count")) > 0)
    status = "ok" if not queue else "review"
    if approval.get("status") in {"missing", "unreadable"}:
        status = "review"
    return {
        "status": status,
        "generated_at": iso_z(),
        "root": str(root),
        "policy": "No live Baselane source mutation is allowed unless a row has current-ID proof and BASELANE_SOURCE_FIX_APPLY=1 is explicitly set.",
        "approval_report": str(approval_path),
        "apply_plan": str(apply_plan_path),
        "apply_report": str(apply_report_path),
        "apply_report_current": apply_report_current,
        "validation_report": str(validation_path),
        "source_plan_report": str(source_plan_path),
        "row_count": len(queue),
        "ready_to_apply_count": ready_to_apply_count,
        "ready_native_split_count": ready_native_split_count,
        "needs_current_source_index_count": needs_current_source_index_count,
        "already_applied_count": already_applied_count,
        "decision_required_count": decision_required_count,
        "email_invoice_required_count": email_invoice_required_count,
        "local_mail_invoice_match_count": local_mail_invoice_match_count,
        "gws_mail_invoice_match_count": gws_mail_invoice_match_count,
        "pending_category_count": count(validation.get("pending_count")),
        "invalid_count": count(validation.get("invalid_count")),
        "group_counts": dict(sorted(group_counts.items())),
        "autonomy_recommendation_counts": dict(sorted(recommendation_counts.items())),
        "ready_command": "BASELANE_SOURCE_FIX_APPLY=1 bash scripts/baselane_apply_source_fix_then_refresh.sh" if ready_to_apply_count else "",
        "primary_next_action": (
            f"Apply {ready_to_apply_count} current-ID correction(s), apply {ready_native_split_count} native split(s), refresh current IDs for {needs_current_source_index_count}, then resolve {decision_required_count} evidence-backed category decision(s)."
            if ready_to_apply_count and ready_native_split_count and needs_current_source_index_count and decision_required_count
            else f"Apply {ready_to_apply_count} current-ID correction(s), refresh current IDs for {needs_current_source_index_count}, then resolve {decision_required_count} evidence-backed category decision(s)."
            if ready_to_apply_count and needs_current_source_index_count and decision_required_count
            else f"Refresh current IDs for {needs_current_source_index_count}, then resolve {decision_required_count} evidence-backed category decision(s)."
            if needs_current_source_index_count and decision_required_count
            else f"Refresh current IDs for {needs_current_source_index_count} auto-approved correction(s)."
            if needs_current_source_index_count
            else f"Apply {ready_to_apply_count} current-ID correction(s), apply {ready_native_split_count} native split(s), then resolve {decision_required_count} evidence-backed category decision(s)."
            if ready_to_apply_count and ready_native_split_count and decision_required_count
            else f"Apply {ready_to_apply_count} current-ID correction(s), then resolve {decision_required_count} evidence-backed category decision(s)."
            if ready_to_apply_count and decision_required_count
            else f"Apply {ready_native_split_count} native split(s), then resolve {decision_required_count} evidence-backed category decision(s)."
            if ready_native_split_count and decision_required_count
            else f"Apply {ready_to_apply_count} current-ID correction(s)."
            if ready_to_apply_count
            else f"Apply {ready_native_split_count} native split(s)."
            if ready_native_split_count
            else f"Resolve {decision_required_count} evidence-backed category decision(s)."
            if decision_required_count
            else "No ECO GL source-fix actions remain."
        ),
        "operator_brief": operator_brief(queue),
        "queue": queue,
    }


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "id",
        "group",
        "property",
        "date",
        "amount",
        "source_month",
        "source_action_type",
        "cash_flow_source_file",
        "cash_flow_source_row",
        "cash_flow_value",
        "source_gl_total",
        "source_fix_reason",
        "merchant",
        "category_to_set",
        "baselane_id",
        "match_status",
        "apply_plan_match_status",
        "apply_plan_match_reason",
        "validation_status",
        "autonomy_recommendation",
        "evidence_status",
        "historical_evidence_status",
        "context_candidate_status",
        "document_support_count",
        "document_checked_file_count",
        "email_invoice_evidence_required",
        "payment_rail",
        "email_invoice_search_query",
        "local_mail_invoice_status",
        "local_mail_invoice_match_count",
        "gws_mail_invoice_status",
        "gws_mail_invoice_match_count",
        "next_action",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in fields})


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Baselane ECO GL Source-Fix Action Queue",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Ready to apply: `{report.get('ready_to_apply_count')}`",
        f"- Ready native splits: `{report.get('ready_native_split_count')}`",
        f"- Auto-approved but missing current Baselane IDs: `{report.get('needs_current_source_index_count')}`",
        f"- Already applied in current export: `{report.get('already_applied_count')}`",
        f"- Evidence/category decisions required: `{report.get('decision_required_count')}`",
        f"- Email invoice lookups: `{report.get('email_invoice_required_count')}`",
        f"- Local mail invoice matches: `{report.get('local_mail_invoice_match_count')}`",
        f"- Gmail invoice matches: `{report.get('gws_mail_invoice_match_count')}`",
        f"- Invalid rows: `{report.get('invalid_count')}`",
        f"- Policy: {report.get('policy')}",
        f"- Next action: {report.get('primary_next_action')}",
    ]
    if report.get("ready_command"):
        lines.append(f"- Ready command: `{report.get('ready_command')}`")
    lines.extend(["", "## Operator Brief"])
    for section in report.get("operator_brief") or []:
        lines.append(f"- {section.get('label')}: `{section.get('count')}`")
        for record in section.get("rows") or []:
            lines.append(
                f"  - {record.get('property')} | {record.get('date')} | {record.get('amount')} | "
                f"{record.get('merchant')} | `{record.get('category_to_set') or 'unresolved'}` | `{record.get('evidence_status')}`"
            )
    if not report.get("operator_brief"):
        lines.append("- None.")
    lines.extend(["", "## Full Queue"])
    for record in report.get("queue") or []:
        lines.extend(
            [
                f"- `{record.get('group')}` — {record.get('property')} | {record.get('date')} | {record.get('amount')} | {record.get('merchant')}",
                f"  - Category: `{record.get('category_to_set') or 'unresolved'}`; recommendation: `{record.get('autonomy_recommendation')}`; evidence: `{record.get('evidence_status')}`",
                f"  - Current-ID match: `{record.get('match_status') or record.get('apply_plan_match_status') or 'not_ready'}`",
                f"  - Cash Flow source: `{record.get('cash_flow_source_file') or 'not_recorded'}`"
                + (f" row `{record.get('cash_flow_source_row')}`" if record.get('cash_flow_source_row') else ""),
                f"  - Source-plan action: `{record.get('source_action_type') or 'not_recorded'}`; CF value `{record.get('cash_flow_value') or 'not_recorded'}`; source GL total `{record.get('source_gl_total') or 'not_recorded'}`",
                f"  - Public-doc scan: support `{record.get('document_support_count')}`; checked `{record.get('document_checked_file_count')}`",
                f"  - Email invoice: required `{str(record.get('email_invoice_evidence_required')).lower()}`"
                + (f"; rail `{record.get('payment_rail')}`" if record.get("payment_rail") else ""),
                f"  - Local mail: `{record.get('local_mail_invoice_status') or 'unknown'}`; matches `{record.get('local_mail_invoice_match_count')}`",
                f"  - Gmail: `{record.get('gws_mail_invoice_status') or 'unknown'}`; matches `{record.get('gws_mail_invoice_match_count')}`",
                f"  - Action: {record.get('next_action')}",
            ]
        )
    if not report.get("queue"):
        lines.append("- None.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/home/digit/.openclaw/workspace"))
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    args = parser.parse_args(argv)

    report = build_report(args.root)
    report_path = args.report or args.root / "reports" / "baselane_ecogl_source_fix_action_queue.json"
    csv_path = args.csv or args.root / "reports" / "baselane_ecogl_source_fix_action_queue.csv"
    markdown_path = args.markdown or args.root / "reports" / "baselane_ecogl_source_fix_action_queue.md"
    report["csv"] = str(csv_path)
    report["markdown"] = str(markdown_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(csv_path, report["queue"])
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in ("status", "ready_to_apply_count", "ready_native_split_count", "decision_required_count", "row_count")
            },
            indent=2,
        )
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
