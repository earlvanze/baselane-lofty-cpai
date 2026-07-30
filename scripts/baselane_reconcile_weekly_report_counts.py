#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def update_if_changed(target: dict[str, Any], key: str, value: Any, changed: list[str]) -> None:
    if value is None:
        return
    if target.get(key) != value:
        target[key] = value
        changed.append(key)


EFFECTIVE_CLEAR_REASON_TOKENS = {
    "cf_statement_sync_review",
    "cf_review_gate_review",
    "ecogl_data_quality_hold",
    "ecogl_source_fix_queue",
    "ecogl_source_fix_evidence",
}


def reconcile_reason_tokens(
    raw_reason: Any,
    *,
    cf_effectively_clear: bool,
    source_fix_effectively_clear: bool,
) -> str:
    parts = [part.strip() for part in str(raw_reason or "").split(";") if part.strip()]
    filtered: list[str] = []
    for part in parts:
        if cf_effectively_clear and source_fix_effectively_clear and part in EFFECTIVE_CLEAR_REASON_TOKENS:
            continue
        if part not in filtered:
            filtered.append(part)
    return ";".join(filtered)



def compact_list(values: object, limit: int = 8) -> list[Any]:
    if not isinstance(values, list):
        return []
    return values[:limit]


def weekly_primary_blocker(weekly: dict[str, Any], root: Path) -> dict[str, Any]:
    reports = root / "reports"
    reasons = [part for part in str(weekly.get("reason") or "").split(";") if part]
    reason_set = set(reasons)
    if weekly.get("status") == "ok":
        return {
            "id": "weekly_file_updates_clean",
            "blocker": None,
            "artifact": str(reports / "baselane_weekly_file_updates_run_report.json"),
            "next_action": None,
            "hold": None,
            "evidence": {"review_reasons": reasons},
        }
    if weekly.get("cf_statement_sync_effective_status") not in {None, "ok"}:
        return {
            "id": "cf_statement_sync_review",
            "blocker": "Cash Flow Statement sync is not effectively clean",
            "artifact": str(reports / "baselane_weekly_cf_statement_sync_report.json"),
            "next_action": "Fix the CF statement sync blockers, rerun bash scripts/baselane_weekly_file_updates_cron.sh, and require effective_status=ok before publish/email.",
            "hold": "Lofty PM publish and investor email",
            "evidence": {
                "review_reasons": reasons,
                "effective_status": weekly.get("cf_statement_sync_effective_status"),
                "effective_reason": weekly.get("cf_statement_sync_effective_reason"),
                "effective_blockers": weekly.get("cf_statement_sync_effective_blockers") or [],
            },
        }
    if (
        weekly.get("mortgage_workflow_tokenomics_workbook_write_guard_status") == "blocked"
        or "mortgage_workflow_tokenomics_workbook_write_guard_blocked" in reason_set
        or "coownership_tokenomics_workbook_write_review" in reason_set
        or "coownership_tokenomics_workbook_write_not_ready" in reason_set
    ):
        blocker_properties = (
            weekly.get("mortgage_workflow_tokenomics_stale_statement_properties")
            or weekly.get("mortgage_workflow_downloader_coverage_current_month_statement_gap_properties")
            or weekly.get("mortgage_workflow_tokenomics_missing_current_month_statement_properties")
            or []
        )
        return {
            "id": "mortgage_coownership_tokenomics_guard",
            "blocker": "Mortgage/coownership tokenomics workbook guard is not ready",
            "artifact": str(reports / "baselane_monthly_mortgage_workflow_review_packet.md"),
            "next_action": "Resolve stale co-owner mortgage statement evidence for the listed properties, then rerun bash scripts/baselane_weekly_file_updates_cron.sh.",
            "hold": "Lofty PM publish and investor email",
            "evidence": {
                "review_reasons": reasons,
                "guard_status": weekly.get("mortgage_workflow_tokenomics_workbook_write_guard_status"),
                "guard_reason": weekly.get("mortgage_workflow_tokenomics_workbook_write_guard_reason"),
                "stale_statement_count": count(weekly.get("mortgage_workflow_tokenomics_stale_statement_count")),
                "stale_statement_properties": compact_list(blocker_properties),
                "safe_to_run_automatically": weekly.get("mortgage_workflow_tokenomics_safe_to_run_automatically"),
            },
        }
    return {
        "id": "weekly_review",
        "blocker": "Weekly file update run is in review",
        "artifact": str(reports / "baselane_weekly_file_updates_run_report.json"),
        "next_action": "Open reports/baselane_weekly_file_updates_run_report.json, resolve the listed review_reasons, then rerun bash scripts/baselane_weekly_file_updates_cron.sh.",
        "hold": "weekly/monthly document updates",
        "evidence": {"review_reasons": reasons},
    }

def effective_cf_status(
    cf: dict[str, Any],
    cf_gate: dict[str, Any],
    source_fix_effectively_clear: bool,
) -> dict[str, Any]:
    source_cash_violations = count(cf.get("source_cash_balance_violation_count"))
    no_mortgage_violations = count(cf.get("no_mortgage_debt_violation_count"))
    conflict_count = count(cf.get("conflict_count"))
    missing_canonical_count = count(cf.get("missing_canonical_cf_count"))
    audit_error_count = count(cf.get("audit_error_count"))
    audit_error_class_counts = cf.get("audit_error_class_counts") if isinstance(cf.get("audit_error_class_counts"), dict) else {}
    gate_action_queue_count = count(cf_gate.get("action_queue_count") or (cf_gate.get("summary") or {}).get("action_queue_count"))
    gate_blocker_count = count(cf_gate.get("blocker_count"))
    gate_ok = cf_gate.get("status") == "ok" and gate_action_queue_count == 0 and gate_blocker_count == 0

    hard_blockers = [
        name
        for name, value in (
            ("source_cash_balance_violation_count", source_cash_violations),
            ("no_mortgage_debt_violation_count", no_mortgage_violations),
            ("conflict_count", conflict_count),
            ("missing_canonical_cf_count", missing_canonical_count),
            ("audit_error_count", audit_error_count),
            ("cf_review_gate_action_queue_count", gate_action_queue_count),
            ("cf_review_gate_blocker_count", gate_blocker_count),
        )
        if value
    ]
    hard_blockers.extend(
        f"audit_error_{error_class}_count"
        for error_class, value in sorted(audit_error_class_counts.items())
        if count(value)
    )
    if hard_blockers:
        status = "review"
        reason = cf.get("reason") or "cf_sync_hard_blockers"
    elif cf.get("status") == "ok":
        status = "ok"
        reason = "raw_cf_sync_ok"
    elif cf.get("status") == "review" and gate_ok and source_fix_effectively_clear and not hard_blockers:
        status = "ok"
        reason = "review_gate_clear_after_verified_source_fixes"
    else:
        status = cf.get("status") or "unknown"
        reason = cf.get("reason") or "cf_sync_not_effectively_clear"

    return {
        "effective_status": status,
        "effective_ok": status == "ok",
        "effective_reason": reason,
        "effective_blockers": hard_blockers,
        "effective_gate_status": cf_gate.get("status"),
        "effective_gate_action_queue_count": gate_action_queue_count,
        "effective_gate_blocker_count": gate_blocker_count,
        "effective_source_fix_clear": source_fix_effectively_clear,
    }


def reconcile(root: Path) -> dict[str, Any]:
    reports = root / "reports"
    weekly_path = reports / "baselane_weekly_file_updates_run_report.json"
    cf_path = reports / "baselane_weekly_cf_statement_sync_report.json"
    weekly = read_json(weekly_path)
    cf = read_json(cf_path)
    safe_apply = read_json(reports / "baselane_ecogl_safe_category_apply_report.json")
    autonomy = read_json(reports / "baselane_ecogl_data_quality_autonomy.json")
    source_fix = read_json(reports / "baselane_ecogl_source_fix_plan.json")
    source_fix_verifier = read_json(reports / "baselane_ecogl_source_fix_verifier.json")
    source_fix_action_queue = read_json(reports / "baselane_ecogl_source_fix_action_queue.json")
    untagged = read_json(reports / "baselane_cf_untagged_review_packet.json")
    cf_gate = read_json(reports / "baselane_weekly_cf_review_gate.json")
    weekly_unprocessed = read_json(reports / "baselane_weekly_unprocessed_report.json")

    verified_fixed_count = count(source_fix_verifier.get("verified_fixed_count"))
    remaining_count = count(source_fix_verifier.get("remaining_count"))
    verifier_total_count = verified_fixed_count + remaining_count
    source_fix_queue_group_counts = (
        source_fix_action_queue.get("group_counts")
        if isinstance(source_fix_action_queue.get("group_counts"), dict)
        else {}
    )
    queue_ready_to_apply_count = count(source_fix_action_queue.get("ready_to_apply_count"))
    queue_ready_native_split_count = count(source_fix_action_queue.get("ready_native_split_count"))
    queue_needs_current_source_index_count = count(source_fix_action_queue.get("needs_current_source_index_count"))
    queue_decision_required_count = count(source_fix_action_queue.get("decision_required_count"))
    queue_already_applied_count = count(
        source_fix_action_queue.get("already_applied_count")
        or source_fix_queue_group_counts.get("already_applied")
    )
    source_fix_action_queue_current = source_fix_action_queue.get("status") not in {"missing", "unreadable"}
    source_fix_queue_actionable_count = (
        queue_ready_to_apply_count
        + queue_ready_native_split_count
        + queue_needs_current_source_index_count
        + queue_decision_required_count
    )
    if source_fix_action_queue_current:
        verified_fixed_count += queue_already_applied_count
        remaining_count = source_fix_queue_actionable_count
        verifier_total_count = verified_fixed_count + remaining_count
    source_fix_effectively_clear = (
        source_fix_action_queue_current
        and count(source_fix_action_queue.get("ready_to_apply_count")) == 0
        and count(source_fix_action_queue.get("ready_native_split_count")) == 0
        and count(source_fix_action_queue.get("needs_current_source_index_count")) == 0
        and count(source_fix_action_queue.get("decision_required_count")) == 0
        and (
            count(source_fix_action_queue.get("row_count")) == queue_already_applied_count
            or (
                source_fix_verifier.get("status") == "ok"
                and verifier_total_count > 0
                and remaining_count == 0
            )
        )
    ) or (
        source_fix_verifier.get("status") == "ok"
        and verifier_total_count > 0
        and remaining_count == 0
        and count(source_fix_action_queue.get("row_count")) == 0
        and count(source_fix_action_queue.get("ready_to_apply_count")) == 0
        and count(source_fix_action_queue.get("decision_required_count")) == 0
    )
    if source_fix_effectively_clear:
        effective_exception_count = 0
        effective_exception_reason_counts = {}
        effective_source_fix_action_count = 0
        effective_source_fix_action_type_counts = {}
    elif source_fix_action_queue_current:
        effective_exception_count = source_fix_queue_actionable_count
        effective_exception_reason_counts = source_fix_queue_group_counts
        effective_source_fix_action_count = source_fix_queue_actionable_count
        effective_source_fix_action_type_counts = {
            key: value
            for key, value in source_fix_queue_group_counts.items()
            if key != "already_applied" and count(value)
        }
    else:
        effective_exception_count = autonomy.get("exception_count")
        effective_exception_reason_counts = autonomy.get("exception_reason_counts")
        effective_source_fix_action_count = source_fix.get("action_count")
        effective_source_fix_action_type_counts = source_fix.get("action_type_counts")
    cf_for_effective = dict(cf)
    if count(untagged.get("review_required_count")) and cf_for_effective.get("status") == "ok":
        cf_for_effective["status"] = "review"
        cf_for_effective["reason"] = "cf_audit_untagged_gl_rows"
    cf_effective = effective_cf_status(cf_for_effective, cf_gate, source_fix_effectively_clear)

    changed_weekly: list[str] = []
    changed_cf: list[str] = []
    if weekly.get("status") not in {"missing", "unreadable"}:
        weekly_unprocessed_idempotent = (weekly_unprocessed.get("idempotency") or {}).get("idempotent")
        update_if_changed(weekly, "ecogl_safe_apply_action_count", safe_apply.get("safe_action_count"), changed_weekly)
        update_if_changed(weekly, "ecogl_safe_apply_actions_digest", safe_apply.get("actions_digest"), changed_weekly)
        update_if_changed(weekly, "ecogl_exception_count", effective_exception_count, changed_weekly)
        update_if_changed(weekly, "ecogl_exception_reason_counts", effective_exception_reason_counts, changed_weekly)
        update_if_changed(weekly, "ecogl_source_fix_action_count", effective_source_fix_action_count, changed_weekly)
        update_if_changed(weekly, "ecogl_source_fix_action_type_counts", effective_source_fix_action_type_counts, changed_weekly)
        update_if_changed(weekly, "ecogl_source_fix_digest", source_fix.get("idempotency_digest"), changed_weekly)
        update_if_changed(weekly, "ecogl_source_fix_effectively_clear", source_fix_effectively_clear, changed_weekly)
        update_if_changed(weekly, "ecogl_source_fix_verified_fixed_count", verified_fixed_count, changed_weekly)
        update_if_changed(weekly, "ecogl_source_fix_remaining_count", remaining_count, changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_status", cf_for_effective.get("status"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_reason", cf_for_effective.get("reason"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_audit_report", cf_for_effective.get("audit_report"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_discovery_report", cf_for_effective.get("discovery_report"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_audited_property_count", cf_for_effective.get("audited_property_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_audit_error_count", cf_for_effective.get("audit_error_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_audit_error_class_counts", cf_for_effective.get("audit_error_class_counts"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_canonical_cf_property_count", cf_for_effective.get("canonical_cf_property_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_excluded_cf_property_count", cf_for_effective.get("excluded_cf_property_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_conflict_count", cf_for_effective.get("conflict_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_conflict_property_count", cf_for_effective.get("conflict_property_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_untagged_gl_rows", cf_for_effective.get("untagged_gl_rows"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_source_cash_balance_checked_property_count", cf_for_effective.get("source_cash_balance_checked_property_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_source_cash_balance_update_count", cf_for_effective.get("source_cash_balance_update_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_source_cash_balance_violation_count", cf_for_effective.get("source_cash_balance_violation_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_no_mortgage_debt_checked_property_count", cf_for_effective.get("no_mortgage_debt_checked_property_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_no_mortgage_debt_violation_count", cf_for_effective.get("no_mortgage_debt_violation_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_missing_canonical_cf_count", cf_for_effective.get("missing_canonical_cf_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_review_reasons", cf_for_effective.get("review_reasons"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_untagged_review_required_count", untagged.get("review_required_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_untagged_review_row_count", untagged.get("untagged_row_count"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_untagged_review_auto_suggested_count", untagged.get("auto_suggested_count"), changed_weekly)
        update_if_changed(weekly, "cf_review_gate_status", cf_gate.get("status"), changed_weekly)
        update_if_changed(weekly, "cf_review_gate_action_queue_count", cf_gate.get("action_queue_count"), changed_weekly)
        update_if_changed(weekly, "cf_review_gate_blocker_count", cf_gate.get("blocker_count"), changed_weekly)
        update_if_changed(weekly, "cf_review_gate_action_queue_digest", cf_gate.get("action_queue_digest"), changed_weekly)
        update_if_changed(weekly, "cf_review_gate_idempotency_key", cf_gate.get("idempotency_key"), changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_effective_status", cf_effective["effective_status"], changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_effective_ok", cf_effective["effective_ok"], changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_effective_reason", cf_effective["effective_reason"], changed_weekly)
        update_if_changed(weekly, "cf_statement_sync_effective_blockers", cf_effective["effective_blockers"], changed_weekly)
        update_if_changed(
            weekly,
            "reason",
            reconcile_reason_tokens(
                weekly.get("reason"),
                cf_effectively_clear=cf_effective["effective_ok"],
                source_fix_effectively_clear=source_fix_effectively_clear,
            ),
            changed_weekly,
        )
        update_if_changed(weekly, "weekly_unprocessed_idempotent", weekly_unprocessed_idempotent, changed_weekly)
        update_if_changed(weekly, "weekly_unprocessed_state_idempotent", weekly_unprocessed_idempotent, changed_weekly)
        if weekly.get("status") == "review" and not weekly.get("state_file_marked_complete"):
            update_if_changed(weekly, "state_file_unmarked", True, changed_weekly)
            update_if_changed(
                weekly,
                "state_file_unmarked_reason",
                "latest weekly run is review; do not skip retry before review is clean",
                changed_weekly,
            )
        review_safe = weekly.get("review_safe_idempotency") if isinstance(weekly.get("review_safe_idempotency"), dict) else {}
        nested_before = dict(review_safe)
        review_safe.update(
            {
                "state_file_marked_complete": weekly.get("state_file_marked_complete"),
                "state_file_unmarked": weekly.get("state_file_unmarked"),
                "state_file_unmarked_reason": weekly.get("state_file_unmarked_reason"),
                "weekly_unprocessed_idempotent": weekly_unprocessed_idempotent,
                "weekly_unprocessed_state_idempotent": weekly_unprocessed_idempotent,
                "ecogl_safe_apply_action_count": safe_apply.get("safe_action_count"),
                "ecogl_exception_count": effective_exception_count,
                "ecogl_exception_reason_counts": effective_exception_reason_counts,
                "ecogl_source_fix_action_count": effective_source_fix_action_count,
                "ecogl_source_fix_action_type_counts": effective_source_fix_action_type_counts,
                "ecogl_source_fix_digest": source_fix.get("idempotency_digest"),
                "ecogl_source_fix_effectively_clear": source_fix_effectively_clear,
                "ecogl_source_fix_verified_fixed_count": verified_fixed_count,
                "ecogl_source_fix_remaining_count": remaining_count,
                "cf_statement_sync_canonical_cf_property_count": cf_for_effective.get("canonical_cf_property_count"),
                "cf_statement_sync_conflict_count": cf_for_effective.get("conflict_count"),
                "cf_statement_sync_source_cash_balance_violation_count": cf_for_effective.get("source_cash_balance_violation_count"),
                "cf_statement_sync_no_mortgage_debt_violation_count": cf_for_effective.get("no_mortgage_debt_violation_count"),
                "cf_statement_sync_missing_canonical_cf_count": cf_for_effective.get("missing_canonical_cf_count"),
                "cf_review_gate_status": cf_gate.get("status"),
                "cf_review_gate_action_queue_count": cf_gate.get("action_queue_count"),
                "cf_review_gate_blocker_count": cf_gate.get("blocker_count"),
                "cf_review_gate_action_queue_digest": cf_gate.get("action_queue_digest"),
                "cf_review_gate_idempotency_key": cf_gate.get("idempotency_key"),
                "cf_statement_sync_effective_status": cf_effective["effective_status"],
                "cf_statement_sync_effective_ok": cf_effective["effective_ok"],
                "cf_statement_sync_effective_reason": cf_effective["effective_reason"],
                "cf_statement_sync_effective_blockers": cf_effective["effective_blockers"],
            }
        )
        if review_safe != nested_before:
            weekly["review_safe_idempotency"] = review_safe
            changed_weekly.append("review_safe_idempotency")
        if count(effective_source_fix_action_count) or count(effective_exception_count):
            update_if_changed(weekly, "status", "review", changed_weekly)
        primary_blocker = weekly_primary_blocker(weekly, root)
        if weekly.get("primary_blocker") != primary_blocker:
            weekly["primary_blocker"] = primary_blocker
            weekly["next_action"] = primary_blocker.get("next_action")
            weekly["hold"] = primary_blocker.get("hold")
            weekly["actionable_summary"] = {
                "primary_blocker": primary_blocker,
                "review_reason_count": len([part for part in str(weekly.get("reason") or "").split(";") if part]),
                "review_reasons": [part for part in str(weekly.get("reason") or "").split(";") if part],
                "noise_policy": "Use primary_blocker for action; alerts remain diagnostic evidence.",
            }
            changed_weekly.append("primary_blocker")
        if changed_weekly:
            weekly["reconciled_at"] = iso_z()
            weekly["reconcile_source"] = "authoritative_subreports"
            write_json(weekly_path, weekly)

    if cf.get("status") not in {"missing", "unreadable"}:
        update_if_changed(cf, "untagged_review_required_count", untagged.get("review_required_count"), changed_cf)
        update_if_changed(cf, "untagged_review_row_count", untagged.get("untagged_row_count"), changed_cf)
        update_if_changed(cf, "untagged_review_auto_suggested_count", untagged.get("auto_suggested_count"), changed_cf)
        update_if_changed(cf, "ecogl_source_fix_action_count", effective_source_fix_action_count, changed_cf)
        update_if_changed(cf, "ecogl_source_fix_action_type_counts", effective_source_fix_action_type_counts, changed_cf)
        update_if_changed(cf, "ecogl_source_fix_digest", source_fix.get("idempotency_digest"), changed_cf)
        update_if_changed(cf, "ecogl_source_fix_effectively_clear", source_fix_effectively_clear, changed_cf)
        update_if_changed(cf, "ecogl_source_fix_verified_fixed_count", verified_fixed_count, changed_cf)
        update_if_changed(cf, "ecogl_source_fix_remaining_count", remaining_count, changed_cf)
        for key, value in cf_effective.items():
            update_if_changed(cf, key, value, changed_cf)
        if cf.get("status") != cf_for_effective.get("status"):
            update_if_changed(cf, "status", cf_for_effective.get("status"), changed_cf)
        if cf.get("reason") != cf_for_effective.get("reason"):
            update_if_changed(cf, "reason", cf_for_effective.get("reason"), changed_cf)
        if count(untagged.get("review_required_count")) and cf.get("status") == "ok":
            update_if_changed(cf, "status", "review", changed_cf)
            update_if_changed(cf, "reason", "cf_audit_untagged_gl_rows", changed_cf)
        if changed_cf:
            cf["reconciled_at"] = iso_z()
            cf["reconcile_source"] = "authoritative_subreports"
            write_json(cf_path, cf)

    return {
        "status": "ok",
        "weekly_changed": bool(changed_weekly),
        "weekly_changed_fields": sorted(set(changed_weekly)),
        "cf_changed": bool(changed_cf),
        "cf_changed_fields": sorted(set(changed_cf)),
        "source_fix_action_count": effective_source_fix_action_count,
        "ecogl_exception_count": effective_exception_count,
        "ecogl_source_fix_effectively_clear": source_fix_effectively_clear,
        "ecogl_source_fix_verified_fixed_count": verified_fixed_count,
        "ecogl_source_fix_remaining_count": remaining_count,
        "untagged_review_required_count": untagged.get("review_required_count"),
        "cf_effective_status": cf_effective["effective_status"],
        "cf_effective_reason": cf_effective["effective_reason"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile aggregate weekly Baselane report counts from current authoritative subreports.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    args = parser.parse_args()
    print(json.dumps(reconcile(args.root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
