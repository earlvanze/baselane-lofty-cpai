#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CSV_FIELDS = [
    "queue_type",
    "priority",
    "source_artifact",
    "id",
    "status",
    "approved",
    "next_action",
    "next_action_file",
    "next_action_command",
    "next_action_detail",
    "property",
    "date",
    "amount",
    "merchant",
    "description",
    "workbook_file",
    "workbook_row",
    "label",
    "action",
    "reason",
    "suggested_baselane_category",
    "suggested_cf_category",
    "confidence",
    "match_type",
    "match_value",
    "row_count",
    "property_count",
    "sample_properties",
    "sample_merchants",
]


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    if isinstance(data, dict):
        return data
    return {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def norm_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def norm_amount(value: object) -> float:
    try:
        return round(float(str(value or "").replace("$", "").replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return 0.0


def stable_digest(payload: dict[str, Any]) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def rel_path(path: object, root: Path) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    candidate = Path(raw)
    try:
        if candidate.is_absolute():
            return str(candidate.relative_to(root))
    except ValueError:
        pass
    return raw


def csv_value(value: object) -> str:
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)


def stable_row_id(prefix: str, payload: dict[str, Any]) -> str:
    existing = str(payload.get("id") or "").strip()
    if existing:
        return existing
    digest = stable_digest(payload)
    return f"{prefix}-{digest[:12]}"


def action_fields(queue_type: str, status: str, source_artifact: str, item: dict[str, Any]) -> dict[str, str]:
    if queue_type == "conflict" and status == "needs_approval":
        return {
            "next_action": "approve_exact_match_applicable_row",
            "next_action_file": "reports/baselane_cf_conflict_resolution_approval_template.json",
            "next_action_command": "",
            "next_action_detail": "Set approved=true only for exact-match applicable rows; leave unsafe rows false.",
        }
    if queue_type == "conflict":
        workbook_file = csv_value(item.get("file"))
        workbook_row = csv_value(item.get("row"))
        row_detail = f" row {workbook_row}" if workbook_row else ""
        return {
            "next_action": "manual_baselane_or_workbook_review",
            "next_action_file": workbook_file or source_artifact,
            "next_action_command": "",
            "next_action_detail": f"Manual-only conflict: inspect Baselane source and workbook{row_detail}; do not auto-apply.",
        }
    if queue_type == "untagged":
        return {
            "next_action": "classify_gl_row_or_add_reviewed_rule",
            "next_action_file": source_artifact,
            "next_action_command": "",
            "next_action_detail": "Classify this GL row or add a reviewed rule before treating weekly CF sync as clean.",
        }
    if queue_type == "rule_candidate":
        return {
            "next_action": "review_rule_candidate_then_flip_approved_if_safe",
            "next_action_file": source_artifact,
            "next_action_command": "",
            "next_action_detail": "Review the candidate rule; approved must stay false until human approval.",
        }
    return {
        "next_action": "",
        "next_action_file": source_artifact,
        "next_action_command": "",
        "next_action_detail": "",
    }


def source_fix_clear_context(source_fix_verifier: dict[str, Any], source_fix_action_queue: dict[str, Any]) -> dict[str, Any]:
    verified_results = [
        item
        for item in source_fix_verifier.get("results") or []
        if isinstance(item, dict) and item.get("status") == "verified_fixed" and item.get("id")
    ]
    verified_ids = {str(item["id"]) for item in verified_results}
    effectively_clear = (
        source_fix_verifier.get("status") == "ok"
        and count(source_fix_verifier.get("verified_fixed_count")) > 0
        and count(source_fix_verifier.get("remaining_count")) == 0
        and count(source_fix_action_queue.get("row_count")) == 0
        and count(source_fix_action_queue.get("ready_to_apply_count")) == 0
        and count(source_fix_action_queue.get("decision_required_count")) == 0
    )
    verified_rule_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in verified_results:
        group_key = (norm_text(item.get("property")), norm_text(item.get("merchant")))
        group = verified_rule_groups.setdefault(group_key, {"row_count": 0, "amount_total": 0.0})
        group["row_count"] += 1
        group["amount_total"] += norm_amount(item.get("amount"))
    return {
        "effectively_clear": effectively_clear,
        "verified_ids": verified_ids if effectively_clear else set(),
        "verified_rule_groups": verified_rule_groups if effectively_clear else {},
        "verified_fixed_count": len(verified_results),
    }


def rule_candidate_covered_by_verified_source_fix(item: dict[str, Any], source_fix_context: dict[str, Any]) -> bool:
    if not source_fix_context.get("effectively_clear"):
        return False
    groups = source_fix_context.get("verified_rule_groups") or {}
    if not isinstance(groups, dict):
        return False
    properties = [part for part in str(item.get("sample_properties") or "").split(";") if part.strip()]
    if len(properties) != 1:
        return False
    group = groups.get((norm_text(properties[0]), norm_text(item.get("match_value"))))
    if not group:
        return False
    return count(item.get("row_count")) == count(group.get("row_count")) and norm_amount(item.get("amount_total")) == norm_amount(group.get("amount_total"))


def is_blocking_rule_candidate(item: dict[str, Any]) -> bool:
    return str(item.get("confidence") or "").strip().lower() == "high"


def build_action_queue(
    conflict_plan: dict[str, Any],
    untagged_packet: dict[str, Any],
    rule_candidates: dict[str, Any],
    autonomy: dict[str, Any] | None = None,
    source_fix_context: dict[str, Any] | None = None,
    auto_approved_conflict_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    conflict_artifact = "reports/baselane_cf_conflict_resolution_plan.md"
    untagged_artifact = "reports/baselane_cf_untagged_review_packet.md"
    rule_artifact = "reports/baselane_cf_untagged_rule_candidates.md"
    autonomy = autonomy or {}
    source_fix_context = source_fix_context or {}
    auto_approved_conflict_ids = auto_approved_conflict_ids or set()
    safe_untagged_ids = {
        str(item)
        for item in autonomy.get("safe_untagged_weekly_queue_ids") or []
        if str(item)
    }
    safe_untagged_ids.update(str(item) for item in source_fix_context.get("verified_ids") or [] if str(item))
    safe_rule_ids = {
        str(item)
        for item in autonomy.get("safe_rule_candidate_weekly_queue_ids") or []
        if str(item)
    }
    for item in conflict_plan.get("results") or []:
        if not isinstance(item, dict) or item.get("status") not in {"needs_approval", "blocked_action"}:
            continue
        if item.get("status") == "needs_approval" and str(item.get("id") or "") in auto_approved_conflict_ids:
            continue
        status = str(item.get("status") or "")
        next_action = action_fields("conflict", status, conflict_artifact, item)
        rows.append(
            {
                "queue_type": "conflict",
                "priority": "1" if status == "needs_approval" else "2",
                "source_artifact": conflict_artifact,
                "id": stable_row_id("conflict", item),
                "status": status,
                "approved": csv_value(item.get("approved")),
                **next_action,
                "property": csv_value(item.get("property")),
                "workbook_file": csv_value(item.get("file")),
                "workbook_row": csv_value(item.get("row")),
                "label": csv_value(item.get("label")),
                "action": csv_value(item.get("action")),
                "reason": csv_value(item.get("reason")),
            }
        )
    for item in untagged_packet.get("rows") or []:
        if (
            not isinstance(item, dict)
            or item.get("review_required") is not True
            or item.get("source_index_resolved") is True
        ):
            continue
        row_id = stable_row_id("untagged", item)
        if row_id in safe_untagged_ids:
            continue
        next_action = action_fields("untagged", "review_required", untagged_artifact, item)
        rows.append(
            {
                "queue_type": "untagged",
                "priority": "3",
                "source_artifact": untagged_artifact,
                "id": row_id,
                "status": "review_required",
                "approved": "",
                **next_action,
                "property": csv_value(item.get("Property")),
                "date": csv_value(item.get("Date")),
                "amount": csv_value(item.get("Amount")),
                "merchant": csv_value(item.get("Merchant")),
                "description": csv_value(item.get("Description")),
                "reason": csv_value(item.get("review_reason")),
                "suggested_baselane_category": csv_value(item.get("suggested_baselane_category")),
                "suggested_cf_category": csv_value(item.get("suggested_cf_category")),
            }
        )
    for item in rule_candidates.get("records") or []:
        if not isinstance(item, dict) or item.get("approved") is True:
            continue
        row_id = stable_row_id("rule", item)
        if row_id in safe_rule_ids:
            continue
        if rule_candidate_covered_by_verified_source_fix(item, source_fix_context):
            continue
        if not is_blocking_rule_candidate(item):
            continue
        next_action = action_fields("rule_candidate", "review_required", rule_artifact, item)
        rows.append(
            {
                "queue_type": "rule_candidate",
                "priority": "4",
                "source_artifact": rule_artifact,
                "id": row_id,
                "status": "review_required",
                "approved": csv_value(item.get("approved")),
                **next_action,
                "amount": csv_value(item.get("amount_total")),
                "reason": csv_value(item.get("review_note")),
                "suggested_baselane_category": csv_value(item.get("suggested_baselane_category")),
                "suggested_cf_category": csv_value(item.get("suggested_cf_category")),
                "confidence": csv_value(item.get("confidence")),
                "match_type": csv_value(item.get("match_type")),
                "match_value": csv_value(item.get("match_value")),
                "row_count": csv_value(item.get("row_count")),
                "property_count": csv_value(item.get("property_count")),
                "sample_properties": csv_value(item.get("sample_properties")),
                "sample_merchants": csv_value(item.get("sample_merchants")),
            }
        )
    rows.sort(key=lambda row: (row.get("priority", ""), row.get("property", ""), row.get("id", "")))
    return rows


def action_queue_digest(action_queue: list[dict[str, str]]) -> str:
    normalized_rows = []
    for row in action_queue:
        normalized_rows.append({field: csv_value(row.get(field)) for field in CSV_FIELDS})
    return stable_digest({"rows": normalized_rows})


def approval_template_coverage(
    approval_template: dict[str, Any],
    needs_approval: list[dict[str, Any]],
    blocked_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    approved_rows = [item for item in approval_template.get("approved") or [] if isinstance(item, dict)]
    blocked_rows = [item for item in approval_template.get("blocked") or [] if isinstance(item, dict)]
    required_approved_ids = sorted(str(item.get("id") or "") for item in needs_approval if str(item.get("id") or ""))
    required_blocked_ids = sorted(str(item.get("id") or "") for item in blocked_actions if str(item.get("id") or ""))
    template_approved_ids = sorted(str(item.get("id") or "") for item in approved_rows if str(item.get("id") or ""))
    template_blocked_ids = sorted(str(item.get("id") or "") for item in blocked_rows if str(item.get("id") or ""))
    missing_approved_ids = sorted(set(required_approved_ids) - set(template_approved_ids))
    extra_approved_ids = sorted(set(template_approved_ids) - set(required_approved_ids))
    missing_blocked_ids = sorted(set(required_blocked_ids) - set(template_blocked_ids))
    extra_blocked_ids = sorted(set(template_blocked_ids) - set(required_blocked_ids))
    digest = stable_digest(
        {
            "approval_scope": approval_template.get("approval_scope"),
            "month": approval_template.get("month"),
            "approved": [
                {
                    "id": item.get("id"),
                    "approved": item.get("approved"),
                    "property": item.get("property"),
                    "file": item.get("file"),
                    "row": item.get("row"),
                    "label": item.get("label"),
                    "action": item.get("action"),
                    "current_value": item.get("current_value"),
                    "new_value": item.get("new_value"),
                }
                for item in approved_rows
            ],
            "blocked": [
                {
                    "id": item.get("id"),
                    "property": item.get("property"),
                    "file": item.get("file"),
                    "row": item.get("row"),
                    "label": item.get("label"),
                    "action": item.get("action"),
                    "reason": item.get("reason"),
                }
                for item in blocked_rows
            ],
        }
    )
    return {
        "status": "ok" if not (missing_approved_ids or extra_approved_ids or missing_blocked_ids or extra_blocked_ids) else "review",
        "approved_row_count": len(approved_rows),
        "blocked_row_count": len(blocked_rows),
        "required_approved_count": len(required_approved_ids),
        "required_blocked_count": len(required_blocked_ids),
        "missing_approved_ids": missing_approved_ids,
        "extra_approved_ids": extra_approved_ids,
        "missing_blocked_ids": missing_blocked_ids,
        "extra_blocked_ids": extra_blocked_ids,
        "digest": digest,
    }


def auto_approval_coverage(
    approval_template: dict[str, Any],
    auto_approval: dict[str, Any],
) -> dict[str, Any]:
    """Validate generated safe approvals against the exact current template rows."""
    if auto_approval.get("approval_scope") != "baselane_cf_conflict_resolution":
        return {"status": "missing", "approved_ids": [], "reason": "approval_scope_mismatch"}
    if auto_approval.get("month") != approval_template.get("month"):
        return {"status": "review", "approved_ids": [], "reason": "month_mismatch"}
    template_rows = {
        str(item.get("id") or ""): item
        for item in approval_template.get("approved") or []
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    auto_rows = [item for item in auto_approval.get("approved") or [] if isinstance(item, dict)]
    fields = (
        "id", "property", "file", "row", "label", "action", "current_value", "new_value",
        "verified_void_baselane_id", "verified_voided_amount",
    )
    approved_ids: list[str] = []
    invalid_ids: list[str] = []
    for item in auto_rows:
        row_id = str(item.get("id") or "")
        template_row = template_rows.get(row_id)
        if item.get("approved") is not True or not template_row:
            invalid_ids.append(row_id or "missing")
            continue
        if any(csv_value(item.get(field)) != csv_value(template_row.get(field)) for field in fields):
            invalid_ids.append(row_id)
            continue
        approved_ids.append(row_id)
    return {
        "status": "ok" if not invalid_ids else "review",
        "approved_ids": sorted(set(approved_ids)),
        "approved_row_count": len(set(approved_ids)),
        "invalid_ids": sorted(set(invalid_ids)),
        "reason": "" if not invalid_ids else "approval_rows_do_not_match_current_template",
        "digest": stable_digest({"approved_ids": sorted(set(approved_ids)), "invalid_ids": sorted(set(invalid_ids))}),
    }


def build_report(root: Path) -> dict[str, Any]:
    reports = root / "reports"
    weekly_cf = read_json(reports / "baselane_weekly_cf_statement_sync_report.json")
    conflict_plan = read_json(reports / "baselane_cf_conflict_resolution_plan.json")
    approval_template = read_json(reports / "baselane_cf_conflict_resolution_approval_template.json")
    auto_approval = read_json(reports / "baselane_cf_conflict_auto_zero_fill_approval.json")
    untagged_packet = read_json(reports / "baselane_cf_untagged_review_packet.json")
    rule_candidates = read_json(reports / "baselane_cf_untagged_rule_candidates.json")
    autonomy = read_json(reports / "baselane_ecogl_data_quality_autonomy.json")
    safe_apply = read_json(reports / "baselane_ecogl_safe_category_apply_report.json")
    source_fix_verifier = read_json(reports / "baselane_ecogl_source_fix_verifier.json")
    source_fix_action_queue = read_json(reports / "baselane_ecogl_source_fix_action_queue.json")
    source_fix_context = source_fix_clear_context(source_fix_verifier, source_fix_action_queue)

    conflict_results = [item for item in conflict_plan.get("results") or [] if isinstance(item, dict)]
    needs_approval = [item for item in conflict_results if item.get("status") == "needs_approval"]
    blocked_actions = [item for item in conflict_results if item.get("status") == "blocked_action"]
    untagged_rows = [item for item in untagged_packet.get("rows") or [] if isinstance(item, dict)]
    review_required_rows = [
        item
        for item in untagged_rows
        if item.get("review_required") is True and item.get("source_index_resolved") is not True
    ]
    template_coverage = approval_template_coverage(approval_template, needs_approval, blocked_actions)
    auto_coverage = auto_approval_coverage(approval_template, auto_approval)
    auto_approved_conflict_ids = set(auto_coverage["approved_ids"]) if auto_coverage["status"] == "ok" else set()
    action_queue = build_action_queue(
        conflict_plan,
        untagged_packet,
        rule_candidates,
        autonomy,
        source_fix_context,
        auto_approved_conflict_ids,
    )
    manual_untagged_action_count = sum(1 for row in action_queue if row.get("queue_type") == "untagged")
    manual_rule_candidate_action_count = sum(1 for row in action_queue if row.get("queue_type") == "rule_candidate")
    manual_conflict_action_count = sum(1 for row in action_queue if row.get("queue_type") == "conflict")
    conflict_needs_explicit_approval_count = len(
        [item for item in needs_approval if str(item.get("id") or "") not in auto_approved_conflict_ids]
    )
    advisory_rule_candidates = [
        item
        for item in rule_candidates.get("records") or []
        if isinstance(item, dict)
        and item.get("approved") is not True
        and not is_blocking_rule_candidate(item)
    ]

    summary = {
        "weekly_cf_status": weekly_cf.get("status"),
        "weekly_cf_reason": weekly_cf.get("reason"),
        "conflict_count": count(weekly_cf.get("conflict_count") or conflict_plan.get("conflict_count")),
        "cf_statement_update_count": count(weekly_cf.get("cf_statement_update_count")),
        "cf_statement_zero_fill_count": count(weekly_cf.get("cf_statement_zero_fill_count")),
        "cf_statement_overwrite_formula_update_count": count(weekly_cf.get("cf_statement_overwrite_formula_update_count")),
        "cf_statement_update_property_count": count(weekly_cf.get("cf_statement_update_property_count")),
        "conflict_needs_approval_count": len(needs_approval),
        "conflict_needs_explicit_approval_count": conflict_needs_explicit_approval_count,
        "conflict_blocked_count": len(blocked_actions),
        "conflict_auto_approval_status": auto_coverage["status"],
        "conflict_auto_approval_accepted_count": auto_coverage.get("approved_row_count", 0),
        "conflict_auto_approval_digest": auto_coverage.get("digest"),
        "conflict_approved_applicable_count": count(weekly_cf.get("conflict_resolution_approved_applicable_count") or conflict_plan.get("approved_applicable_count")),
        "conflict_auto_approval_count": count(weekly_cf.get("conflict_auto_approval_count")),
        "conflict_auto_apply_updated_count": count(((weekly_cf.get("conflict_auto_apply_status_counts") or {}).get("updated"))),
        "conflict_approval_template_status": template_coverage["status"],
        "conflict_approval_template_approved_count": template_coverage["approved_row_count"],
        "conflict_approval_template_blocked_count": template_coverage["blocked_row_count"],
        "conflict_approval_template_digest": template_coverage["digest"],
        "untagged_raw_review_required_count": count(
            weekly_cf.get("untagged_review_required_count")
            or untagged_packet.get("raw_review_required_count")
            or len([item for item in untagged_rows if item.get("review_required") is True])
        ),
        "untagged_review_required_count": len(review_required_rows),
        "untagged_rule_candidate_count": count(rule_candidates.get("candidate_count")),
        "untagged_rule_high_confidence_count": count(rule_candidates.get("high_confidence_count")),
        "untagged_rule_medium_confidence_count": count(rule_candidates.get("medium_confidence_count")),
        "untagged_rule_covered_row_count": count(rule_candidates.get("covered_row_count")),
        "untagged_rule_candidate_digest": rule_candidates.get("candidate_digest"),
        "ecogl_autonomy_status": autonomy.get("status"),
        "ecogl_safe_apply_status": safe_apply.get("status"),
        "ecogl_safe_apply_action_count": count(safe_apply.get("safe_action_count")),
        "ecogl_safe_apply_output_written": bool(safe_apply.get("output_written")),
        "ecogl_auto_safe_untagged_row_count": count(autonomy.get("safe_auto_untagged_row_count")),
        "ecogl_auto_safe_rule_count": count(autonomy.get("safe_auto_rule_count")),
        "ecogl_untagged_exception_row_count": count(autonomy.get("untagged_exception_row_count")),
        "ecogl_exception_count": count(autonomy.get("exception_count")),
        "ecogl_downstream_hold": bool(autonomy.get("downstream_hold")),
        "action_queue_count": len(action_queue),
        "manual_conflict_action_count": manual_conflict_action_count,
        "manual_untagged_action_count": manual_untagged_action_count,
        "manual_rule_candidate_action_count": manual_rule_candidate_action_count,
        "advisory_rule_candidate_count": len(advisory_rule_candidates),
        "ecogl_source_fix_effectively_clear": bool(source_fix_context.get("effectively_clear")),
        "ecogl_source_fix_verified_fixed_count": count(source_fix_verifier.get("verified_fixed_count")),
        "ecogl_source_fix_remaining_count": count(source_fix_verifier.get("remaining_count")),
    }
    blockers: list[str] = []
    weekly_cf_untagged_only_review = (
        weekly_cf.get("status") == "review"
        and summary["conflict_count"] == 0
        and summary["conflict_needs_approval_count"] == 0
        and summary["conflict_blocked_count"] == 0
        and (
            weekly_cf.get("reason") == "cf_audit_untagged_gl_rows"
            or summary["untagged_review_required_count"] > 0
        )
    )
    if weekly_cf.get("status") not in {"ok", None} and not weekly_cf_untagged_only_review:
        blockers.append(f"weekly_cf_status={weekly_cf.get('status')}:{weekly_cf.get('reason')}")
    if summary["conflict_needs_explicit_approval_count"]:
        blockers.append(f"conflict_rows_need_explicit_approval={summary['conflict_needs_explicit_approval_count']}")
    if summary["conflict_blocked_count"]:
        blockers.append(f"conflict_rows_manual_only={summary['conflict_blocked_count']}")
    if template_coverage["status"] != "ok":
        blockers.append(
            "conflict_approval_template_coverage="
            f"approved {template_coverage['approved_row_count']}/{template_coverage['required_approved_count']},"
            f"blocked {template_coverage['blocked_row_count']}/{template_coverage['required_blocked_count']}"
        )
    effective_untagged_exceptions = manual_untagged_action_count
    if not autonomy or autonomy.get("status") == "missing":
        effective_untagged_exceptions = summary["untagged_review_required_count"]
    if effective_untagged_exceptions:
        blockers.append(f"untagged_rows_need_classification={effective_untagged_exceptions}")
    if manual_rule_candidate_action_count:
        blockers.append(f"untagged_rule_candidates_need_review={manual_rule_candidate_action_count}")

    review_inputs = {
        "weekly_cf": {
            "status": weekly_cf.get("status"),
            "reason": weekly_cf.get("reason"),
            "conflict_count": weekly_cf.get("conflict_count"),
            "cf_statement_update_count": weekly_cf.get("cf_statement_update_count"),
            "cf_statement_zero_fill_count": weekly_cf.get("cf_statement_zero_fill_count"),
            "cf_statement_overwrite_formula_update_count": weekly_cf.get("cf_statement_overwrite_formula_update_count"),
            "cf_statement_update_property_count": weekly_cf.get("cf_statement_update_property_count"),
            "untagged_review_required_count": weekly_cf.get("untagged_review_required_count"),
            "conflict_resolution_approved_applicable_count": weekly_cf.get("conflict_resolution_approved_applicable_count"),
            "conflict_auto_approval_count": weekly_cf.get("conflict_auto_approval_count"),
            "conflict_auto_apply_status_counts": weekly_cf.get("conflict_auto_apply_status_counts"),
        },
        "conflict_plan": {
            "status": conflict_plan.get("status"),
            "conflict_count": conflict_plan.get("conflict_count"),
            "status_counts": conflict_plan.get("status_counts"),
        },
        "conflict_approval_template": template_coverage,
        "conflict_auto_approval": auto_coverage,
        "untagged_packet": {
            "status": untagged_packet.get("status"),
            "row_count": len(untagged_rows),
            "review_required_count": len(review_required_rows),
        },
        "rule_candidates": {
            "status": rule_candidates.get("status"),
            "candidate_count": rule_candidates.get("candidate_count"),
            "covered_row_count": rule_candidates.get("covered_row_count"),
            "candidate_digest": rule_candidates.get("candidate_digest"),
            "advisory_count": len(advisory_rule_candidates),
            "advisory_ids": [stable_row_id("rule", item) for item in advisory_rule_candidates],
        },
        "ecogl_autonomy": {
            "status": autonomy.get("status"),
            "safe_auto_untagged_row_count": autonomy.get("safe_auto_untagged_row_count"),
            "safe_auto_rule_count": autonomy.get("safe_auto_rule_count"),
            "untagged_exception_row_count": autonomy.get("untagged_exception_row_count"),
            "exception_count": autonomy.get("exception_count"),
            "safe_auto_action_digest": autonomy.get("safe_auto_action_digest"),
            "exception_digest": autonomy.get("exception_digest"),
        },
        "ecogl_safe_apply": {
            "status": safe_apply.get("status"),
            "mode": safe_apply.get("mode"),
            "safe_action_count": safe_apply.get("safe_action_count"),
            "output_written": safe_apply.get("output_written"),
            "actions_digest": safe_apply.get("actions_digest"),
            "output_digest": safe_apply.get("output_digest"),
        },
        "ecogl_source_fix": {
            "verifier_status": source_fix_verifier.get("status"),
            "effectively_clear": source_fix_context.get("effectively_clear"),
            "verified_fixed_count": source_fix_verifier.get("verified_fixed_count"),
            "remaining_count": source_fix_verifier.get("remaining_count"),
            "action_queue_row_count": source_fix_action_queue.get("row_count"),
        },
        "action_queue": {
            "row_count": len(action_queue),
            "digest": action_queue_digest(action_queue),
            "ids": [row.get("id") for row in action_queue],
        },
    }
    actions = []
    if summary["conflict_needs_explicit_approval_count"] or summary["conflict_blocked_count"]:
        actions.append("Review reports/baselane_cf_conflict_resolution_plan.md and approve only exact-match applicable rows in reports/baselane_cf_conflict_resolution_approval_template.json.")
    if summary["conflict_auto_approval_accepted_count"]:
        actions.append(
            "Source-verified conflict approvals are recorded locally; they remain dry-run candidates until the guarded workbook apply is authorized."
        )
    if summary["conflict_auto_apply_updated_count"]:
        actions.append(
            f"Deterministic zero-current CF conflicts already auto-filled from GL: {summary['conflict_auto_apply_updated_count']} row(s)."
        )
    if summary["ecogl_safe_apply_action_count"]:
        actions.append(
            "Deterministic ECO GL category fixes were applied to the cleaned reporting ledger before CF sync."
        )
    elif summary["ecogl_auto_safe_untagged_row_count"]:
        actions.append(
            "Auto-safe high-confidence accrual rows are excluded from manual review; apply those Baselane category fixes through the deterministic category workflow."
        )
    if manual_rule_candidate_action_count:
        actions.append("Handle only non-auto-safe rule candidates from reports/baselane_weekly_cf_review_gate.csv.")
    elif advisory_rule_candidates:
        actions.append("Non-high-confidence rule candidates are advisory only; do not approve broad Baselane rules without narrower source evidence.")
    if effective_untagged_exceptions:
        actions.append("Classify remaining ECO GL exception rows from reports/baselane_ecogl_data_quality_exceptions.csv before treating weekly sync as clean.")
    if not actions:
        actions.append("No weekly CF review action needed.")

    status = "ok" if not blockers else "review"
    digest = stable_digest(review_inputs)
    return {
        "status": status,
        "generated_at": iso_z(),
        "idempotency_key": digest[:16],
        "input_digest": digest,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "summary": summary,
        "conflict_approval_template": template_coverage,
        "actions": actions,
        "action_queue_count": len(action_queue),
        "action_queue_digest": review_inputs["action_queue"]["digest"],
        "action_queue": action_queue,
        "artifacts": {
            "weekly_cf_report": rel_path(reports / "baselane_weekly_cf_statement_sync_report.json", root),
            "conflict_plan": rel_path(reports / "baselane_cf_conflict_resolution_plan.md", root),
            "conflict_approval_template": rel_path(reports / "baselane_cf_conflict_resolution_approval_template.json", root),
            "conflict_auto_approval": rel_path(reports / "baselane_cf_conflict_auto_zero_fill_approval.json", root),
            "untagged_review_packet": rel_path(reports / "baselane_cf_untagged_review_packet.md", root),
            "untagged_rule_candidates": rel_path(reports / "baselane_cf_untagged_rule_candidates.md", root),
            "ecogl_autonomy": rel_path(reports / "baselane_ecogl_data_quality_autonomy.md", root),
            "ecogl_auto_safe_actions": rel_path(reports / "baselane_ecogl_auto_safe_actions.csv", root),
            "ecogl_safe_apply": rel_path(reports / "baselane_ecogl_safe_category_apply_report.md", root),
            "ecogl_safe_apply_actions": rel_path(reports / "baselane_ecogl_safe_category_apply_actions.csv", root),
            "ecogl_exceptions": rel_path(reports / "baselane_ecogl_data_quality_exceptions.csv", root),
            "action_queue_csv": rel_path(reports / "baselane_weekly_cf_review_gate.csv", root),
        },
        "review_inputs": review_inputs,
    }


def write_csv(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for item in report.get("action_queue") or []:
            if isinstance(item, dict):
                writer.writerow({field: csv_value(item.get(field)) for field in CSV_FIELDS})


def write_markdown(report: dict[str, Any], path: Path) -> None:
    summary = report["summary"]
    lines = [
        "# Baselane Weekly CF Review Gate",
        "",
        f"- Status: `{report['status']}`",
        f"- Blockers: `{report['blocker_count']}`",
        f"- Idempotency key: `{report['idempotency_key']}`",
        "",
        "## Act Now",
        "",
    ]
    lines.extend(f"- {action}" for action in report["actions"])
    lines.extend(
        [
            "",
            "## Counts",
            "",
            f"- Conflicts: `{summary['conflict_count']}`",
            f"- CF workbook cells updated from GL: `{summary['cf_statement_update_count']}` across `{summary['cf_statement_update_property_count']}` properties; zero-fill `{summary['cf_statement_zero_fill_count']}`; formulas overwritten `{summary['cf_statement_overwrite_formula_update_count']}`",
            f"- Conflict zero-fill auto-applied: `{summary['conflict_auto_apply_updated_count']}`; auto-approved `{summary['conflict_auto_approval_count']}`",
            f"- Conflict rows needing approval: `{summary['conflict_needs_approval_count']}`",
            f"- Conflict rows manual-only: `{summary['conflict_blocked_count']}`",
            f"- Conflict approval template: `{summary['conflict_approval_template_status']}`; approved `{summary['conflict_approval_template_approved_count']}`; blocked `{summary['conflict_approval_template_blocked_count']}`; digest `{summary['conflict_approval_template_digest']}`",
            f"- Raw untagged review rows: `{summary['untagged_review_required_count']}`",
            f"- Manual untagged actions: `{summary['manual_untagged_action_count']}`; manual rule candidates `{summary['manual_rule_candidate_action_count']}`; manual conflicts `{summary['manual_conflict_action_count']}`",
            f"- Advisory rule candidates: `{summary['advisory_rule_candidate_count']}`",
            f"- Rule candidates: `{summary['untagged_rule_candidate_count']}`; high `{summary['untagged_rule_high_confidence_count']}`; medium `{summary['untagged_rule_medium_confidence_count']}`; covered rows `{summary['untagged_rule_covered_row_count']}`; digest `{summary['untagged_rule_candidate_digest']}`",
            f"- ECO GL safe apply: `{summary['ecogl_safe_apply_status']}`; applied rows `{summary['ecogl_safe_apply_action_count']}`; output written `{summary['ecogl_safe_apply_output_written']}`",
            f"- ECO GL autonomy: `{summary['ecogl_autonomy_status']}`; auto-safe rows `{summary['ecogl_auto_safe_untagged_row_count']}`; auto-safe rules `{summary['ecogl_auto_safe_rule_count']}`; untagged exceptions `{summary['ecogl_untagged_exception_row_count']}`; total exceptions `{summary['ecogl_exception_count']}`; downstream hold `{summary['ecogl_downstream_hold']}`",
            f"- Action queue rows: `{summary['action_queue_count']}`",
            "",
            "## Queue Samples",
            "",
        ]
    )
    for item in (report.get("action_queue") or [])[:10]:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"- `{item.get('queue_type')}` `{item.get('status')}` `{item.get('id')}`: {item.get('next_action_detail')}"
        )
        if item.get("next_action_file"):
            lines.append(f"  - File: `{item.get('next_action_file')}`")
        if item.get("next_action_command"):
            lines.append(f"  - Command: `{item.get('next_action_command')}`")
    lines.extend(
        [
            "",
            "## Artifacts",
            "",
        ]
    )
    for label, artifact in report["artifacts"].items():
        lines.append(f"- {label}: `{artifact}`")
    if report["blockers"]:
        lines.extend(["", "## Blockers", ""])
        lines.extend(f"- `{blocker}`" for blocker in report["blockers"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one deterministic weekly CF review gate from conflict and untagged packets.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--csv", type=Path)
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    report_path = args.report or root / "reports" / "baselane_weekly_cf_review_gate.json"
    markdown_path = args.markdown or root / "reports" / "baselane_weekly_cf_review_gate.md"
    csv_path = args.csv or root / "reports" / "baselane_weekly_cf_review_gate.csv"
    report = build_report(root)
    report["artifacts"]["action_queue_csv"] = rel_path(csv_path, root)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(report, csv_path)
    write_markdown(report, markdown_path)
    print(json.dumps({"status": report["status"], "blocker_count": report["blocker_count"], "idempotency_key": report["idempotency_key"]}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
