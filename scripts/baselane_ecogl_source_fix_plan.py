#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ACTION_FIELDS = [
    "id",
    "action_type",
    "property",
    "month",
    "date",
    "amount",
    "baselane_category",
    "label",
    "merchant",
    "description",
    "confidence",
    "source",
    "source_file",
    "source_row",
    "reason",
    "cf_value",
    "gl_total",
    "cf_formula",
    "automation_status",
]
CF_LABEL_TO_BASELANE_CATEGORY = {
    "Mortgage Interest-Only Payments": "Mortgage Interest Payments",
    "Property Management fee": "Property Management",
    "Property Management Fee": "Property Management",
}


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


def stable_digest(payload: Any) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def stable_action_id(action: dict[str, Any]) -> str:
    material = {
        "action_type": action.get("action_type"),
        "property": action.get("property"),
        "month": action.get("month"),
        "date": action.get("date"),
        "amount": action.get("amount"),
        "label": action.get("label"),
        "merchant": action.get("merchant"),
        "description": action.get("description"),
        "source_file": action.get("source_file"),
        "source_row": action.get("source_row"),
        "reason": action.get("reason"),
    }
    return stable_digest(material)[:16]


def text(value: Any) -> str:
    return "" if value is None else str(value)


def is_formula(value: Any) -> bool:
    return text(value).strip().startswith("=")


def conflict_action_type(row: dict[str, Any]) -> str:
    action = text(row.get("action")).strip()
    if action == "cf_has_value_gl_empty":
        return "book_or_tag_baselane_accrual"
    if action == "review_accrual_in_baselane" or is_formula(row.get("cf_value")):
        return "reconcile_formula_to_baselane_accrual_or_tagging"
    return "reconcile_baselane_gl_to_cash_flow_statement"


def conflict_automation_status(row: dict[str, Any]) -> str:
    return "blocked_source_write_missing"


def baselane_category_for_label(label: Any) -> str:
    text_label = text(label).strip()
    return CF_LABEL_TO_BASELANE_CATEGORY.get(text_label, text_label)


def is_management_fee_label(label: Any) -> bool:
    category = baselane_category_for_label(label)
    normalized = text(label).strip().lower()
    return category in {"Management Fees", "Property Management"} or "management fee" in normalized or "pm fee" in normalized


def is_source_fix_conflict(row: dict[str, Any]) -> bool:
    """Only source-relevant conflicts belong in the Baselane source-fix queue.

    Generic CF statement values with empty GL are downstream statement/template
    issues. They must stay in the CF conflict lane instead of becoming Baselane
    source-write actions.
    """
    return conflict_action_type(row) == "book_or_tag_baselane_accrual" and is_management_fee_label(row.get("label"))


def conflict_actions(conflict_plan: dict[str, Any]) -> list[dict[str, Any]]:
    month = text(conflict_plan.get("month"))
    actions: list[dict[str, Any]] = []
    for row in conflict_plan.get("results") or []:
        if not isinstance(row, dict):
            continue
        if row.get("status") not in {"blocked_action", "needs_approval"}:
            continue
        if not is_source_fix_conflict(row):
            continue
        action_type = conflict_action_type(row)
        automation_status = conflict_automation_status(row)
        reason = row.get("reason") or row.get("action")
        if action_type == "book_or_tag_baselane_accrual" and is_management_fee_label(row.get("label")):
            action_type = "reconcile_pm_fee_source_timing"
            automation_status = "blocked_pm_fee_source_timing_required"
            reason = (
                f"{reason}; PM-fee accruals must not be auto-booked from CF gaps. "
                "Use real Baselane/ECO GL PM-fee source rows and keep first-day accrual rows out of reporting."
            )
        action = {
            "action_type": action_type,
            "property": row.get("property"),
            "month": month,
            "date": "",
            "amount": row.get("cf_value") if not is_formula(row.get("cf_value")) else row.get("gl_total"),
            "baselane_category": baselane_category_for_label(row.get("label")),
            "label": row.get("label"),
            "merchant": "",
            "description": row.get("label"),
            "confidence": "source_required",
            "source": "cf_statement_conflict",
            "source_file": row.get("file"),
            "source_row": row.get("row"),
            "reason": reason,
            "cf_value": row.get("cf_value"),
            "gl_total": row.get("gl_total"),
            "cf_formula": row.get("cf_value") if is_formula(row.get("cf_value")) else "",
            "automation_status": automation_status,
        }
        action["id"] = text(row.get("id")) or stable_action_id(action)
        actions.append(action)
    return actions


def untagged_actions(untagged_packet: dict[str, Any]) -> list[dict[str, Any]]:
    month = text(untagged_packet.get("month"))
    source_file = text(untagged_packet.get("gl_csv"))
    actions: list[dict[str, Any]] = []
    for row in untagged_packet.get("rows") or []:
        if not isinstance(row, dict) or row.get("review_required") is not True:
            continue
        action = {
            "action_type": "tag_baselane_transaction_category",
            "property": row.get("Property"),
            "month": month,
            "date": row.get("Date"),
            "amount": row.get("Amount"),
            "baselane_category": row.get("suggested_baselane_category"),
            "label": row.get("suggested_cf_category") or row.get("suggested_baselane_category"),
            "merchant": row.get("Merchant"),
            "description": row.get("Description"),
            "confidence": "review",
            "source": "untagged_gl_transaction",
            "source_file": source_file,
            "source_row": row.get("source_row") or row.get("row") or "",
            "reason": row.get("review_reason") or "needs_specific_category",
            "cf_value": "",
            "gl_total": "",
            "cf_formula": "",
            "automation_status": "blocked_specific_category_required",
        }
        action["id"] = text(row.get("id")) or stable_action_id(action)
        actions.append(action)
    return actions


def autonomy_exception_actions(autonomy: dict[str, Any], conflict_plan: dict[str, Any]) -> list[dict[str, Any]]:
    exceptions = [row for row in autonomy.get("exceptions") or [] if isinstance(row, dict)]
    if not exceptions:
        return []
    conflict_actions_by_id = {
        text(action.get("id")): action
        for action in conflict_actions(conflict_plan)
        if action.get("id")
    }
    month = text(autonomy.get("source_month") or conflict_plan.get("month"))
    actions: list[dict[str, Any]] = []
    for exception in exceptions:
        exception_id = text(exception.get("id"))
        if exception_id in conflict_actions_by_id:
            actions.append(conflict_actions_by_id[exception_id])
            continue
        queue_type = text(exception.get("queue_type"))
        if queue_type == "conflict":
            continue
        merchant = text(exception.get("merchant") or exception.get("Merchant"))
        date = text(exception.get("date") or exception.get("Date"))
        amount = text(exception.get("amount") or exception.get("Amount"))
        reason = text(exception.get("review_reason") or exception.get("reason"))
        if queue_type == "future_dated_source_transaction":
            action = {
                "action_type": "reverse_or_delete_future_dated_source_journal",
                "property": exception.get("property") or exception.get("Property"),
                "month": month,
                "date": date,
                "amount": amount,
                "baselane_category": exception.get("baselane_category") or "",
                "label": exception.get("label") or "",
                "merchant": merchant,
                "description": exception.get("description") or exception.get("Description") or merchant,
                "confidence": "source_required",
                "source": "ecogl_data_quality_autonomy_exception",
                "source_file": exception.get("source_file") or exception.get("source_csv") or "",
                "source_row": exception.get("source_row") or exception.get("source_line") or "",
                "reason": reason or "Future-dated source journal must not affect the current live ledger.",
                "cf_value": "",
                "gl_total": "",
                "cf_formula": "",
                "automation_status": "blocked_live_source_mutation_required",
            }
        elif queue_type == "pending_unassigned_material_source_transaction":
            action = {
                "action_type": "split_pending_source_transaction_after_posting",
                "property": exception.get("property") or exception.get("Property"),
                "month": month,
                "date": date,
                "amount": amount,
                "baselane_category": exception.get("current_baselane_category") or "",
                "label": "",
                "merchant": merchant,
                "description": exception.get("description") or exception.get("Description") or merchant,
                "confidence": "source_required",
                "source": "ecogl_data_quality_autonomy_exception",
                "source_file": exception.get("source_file") or exception.get("source_csv") or "",
                "source_row": exception.get("source_row") or exception.get("source_line") or "",
                "reason": reason or "Pending source transaction must post before its native property split.",
                "cf_value": "",
                "gl_total": "",
                "cf_formula": "",
                "automation_status": "waiting_source_posting",
            }
        elif reason == "needs_specific_category" or merchant or date or amount:
            action = {
                "action_type": "tag_baselane_transaction_category",
                "property": exception.get("property") or exception.get("Property"),
                "month": month,
                "date": date,
                "amount": amount,
                "baselane_category": exception.get("suggested_baselane_category") or exception.get("baselane_category") or "",
                "label": exception.get("suggested_cf_category") or exception.get("suggested_baselane_category") or "",
                "merchant": merchant,
                "description": exception.get("description") or exception.get("Description") or merchant,
                "confidence": "review",
                "source": "ecogl_data_quality_autonomy_exception",
                "source_file": exception.get("source_file") or "",
                "source_row": exception.get("source_row") or exception.get("row") or "",
                "reason": reason or "needs_specific_category",
                "cf_value": "",
                "gl_total": "",
                "cf_formula": "",
                "automation_status": "blocked_specific_category_required",
            }
        else:
            action = {
                "action_type": "reconcile_baselane_gl_to_cash_flow_statement",
                "property": exception.get("property") or exception.get("Property"),
                "month": month,
                "date": "",
                "amount": amount,
                "baselane_category": exception.get("baselane_category") or "",
                "label": exception.get("label") or "",
                "merchant": "",
                "description": exception.get("label") or "",
                "confidence": "source_required",
                "source": "ecogl_data_quality_autonomy_exception",
                "source_file": exception.get("source_file") or "",
                "source_row": exception.get("source_row") or "",
                "reason": reason or "Autonomy report requires source reconciliation.",
                "cf_value": exception.get("cf_value") or "",
                "gl_total": exception.get("gl_total") or "",
                "cf_formula": exception.get("cf_formula") or "",
                "automation_status": "blocked_source_write_missing",
            }
        action["id"] = exception_id or stable_action_id(action)
        actions.append(action)
    return actions


def autonomy_exception_set_is_complete(autonomy: dict[str, Any]) -> bool:
    """Return whether autonomy supplied the complete source-fix exception set.

    An explicit empty list is meaningful: the autonomy classifier may have
    excluded direct-split Hemlane PM rows because their zero-cash void records
    prove a duplicate accrual must not be created.  Falling back to raw CF
    conflicts in that case reintroduces precisely the rows it cleared.
    """
    exceptions = autonomy.get("exceptions")
    if not isinstance(exceptions, list):
        return False
    try:
        expected_count = int(autonomy.get("exception_count") or 0)
    except (TypeError, ValueError):
        return False
    return expected_count == len([item for item in exceptions if isinstance(item, dict)])


def build_report(root: Path) -> dict[str, Any]:
    reports = root / "reports"
    conflict_plan = read_json(reports / "baselane_cf_conflict_resolution_plan.json")
    untagged_packet = read_json(reports / "baselane_cf_untagged_review_packet.json")
    autonomy = read_json(reports / "baselane_ecogl_data_quality_autonomy.json")

    autonomy_is_authoritative = autonomy_exception_set_is_complete(autonomy)
    actions = autonomy_exception_actions(autonomy, conflict_plan) if autonomy_is_authoritative else []
    source_mode = "autonomy_exceptions" if autonomy_is_authoritative else "raw_conflict_and_untagged_reports"
    if not autonomy_is_authoritative:
        actions = conflict_actions(conflict_plan) + untagged_actions(untagged_packet)
    action_type_counts = Counter(text(action.get("action_type")) for action in actions)
    status_counts = Counter(text(action.get("automation_status")) for action in actions)
    digest = stable_digest({"actions": actions})
    action_count = len(actions)
    return {
        "status": "review" if action_count else "ok",
        "generated_at": iso_z(),
        "source_month": text(conflict_plan.get("month") or untagged_packet.get("month") or autonomy.get("source_month")),
        "source_mode": source_mode,
        "policy": "Plan Baselane source corrections only; do not publish Lofty PM live updates or send owner email until this queue is empty.",
        "mutation_mode": "plan_only",
        "live_baselane_mutation_allowed": False,
        "baselane_source_write_allowed": False,
        "action_count": action_count,
        "action_type_counts": dict(sorted(action_type_counts.items())),
        "automation_status_counts": dict(sorted(status_counts.items())),
        "idempotency_digest": digest,
        "downstream_hold_targets": ["lofty_pm_live_updates", "owner_email"] if action_count else [],
        "source_reports": {
            "conflict_plan": str(reports / "baselane_cf_conflict_resolution_plan.json"),
            "untagged_packet": str(reports / "baselane_cf_untagged_review_packet.json"),
            "ecogl_autonomy": str(reports / "baselane_ecogl_data_quality_autonomy.json"),
        },
        "artifacts": {
            "actions_csv": str(reports / "baselane_ecogl_source_fix_actions.csv"),
            "markdown": str(reports / "baselane_ecogl_source_fix_plan.md"),
        },
        "next_actions": [
            "Execute Baselane source fixes from the CSV queue; keep downstream sends held.",
            "Rerun weekly file updates after source fixes; the queue must be empty before Lofty PM/email.",
        ]
        if action_count
        else ["ECO GL source-fix queue is empty; downstream gates may evaluate normally."],
        "actions": actions,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACTION_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in ACTION_FIELDS})


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        f"# ECO GL Source-Fix Plan — {report.get('source_month') or 'unknown month'}",
        "",
        f"- Status: `{report['status']}`",
        f"- Mutation mode: `{report['mutation_mode']}`",
        f"- Baselane source write allowed: `{report['baselane_source_write_allowed']}`",
        f"- Actions: `{report['action_count']}`",
        f"- Digest: `{report['idempotency_digest']}`",
        "",
        "## Action Counts",
        "",
    ]
    for action_type, count in (report.get("action_type_counts") or {}).items():
        lines.append(f"- `{action_type}`: `{count}`")
    if not report.get("action_type_counts"):
        lines.append("- None")
    lines.extend(["", "## Next", ""])
    for item in report.get("next_actions") or []:
        lines.append(f"- {item}")
    sample = (report.get("actions") or [])[:12]
    if sample:
        lines.extend(["", "## First Actions", ""])
        for action in sample:
            amount = text(action.get("amount")) or "n/a"
            category = text(action.get("baselane_category")) or text(action.get("label")) or "category needed"
            lines.append(
                f"- `{action.get('action_type')}` — {action.get('property') or 'unknown'} — "
                f"{category} — {amount} — `{action.get('automation_status')}`"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a non-mutating Baselane ECO GL source-fix action plan.")
    parser.add_argument("--root", type=Path, default=Path("/home/digit/.openclaw/workspace"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--actions-csv", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)

    root = args.root
    report = build_report(root)
    report_path = args.report or root / "reports" / "baselane_ecogl_source_fix_plan.json"
    actions_csv = args.actions_csv or root / "reports" / "baselane_ecogl_source_fix_actions.csv"
    markdown = args.markdown or root / "reports" / "baselane_ecogl_source_fix_plan.md"

    write_csv(actions_csv, report["actions"])
    write_markdown(report, markdown)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["status", "action_count", "idempotency_digest"]}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
