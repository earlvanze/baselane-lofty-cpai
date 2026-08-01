#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def count_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def summarize_transfer_source_blockers(blockers: list[Any]) -> dict[str, Any]:
    categories: dict[str, dict[str, Any]] = {
        "monthly_accruals": {
            "count": 0,
            "sample": [],
            "next_action": "Resolve accrual append audit and gap approvals before transfer totals can be final.",
        },
        "baselane_auth": {
            "count": 0,
            "sample": [],
            "next_action": (
                "Solve Baselane reCAPTCHA/login in the visible CDP tab, then rerun monthly finance-truth refresh "
                "before final transfer totals."
            ),
        },
        "missing_lofty_reserve": {
            "count": 0,
            "sample": [],
            "next_action": "Fill reviewed current maintenance reserve values from live Lofty evidence.",
        },
        "cf_balance_sheet": {
            "count": 0,
            "sample": [],
            "next_action": "Clear authoritative CF balance-sheet consistency issues; Yhome spreadsheet rows are a separate nonblocking work product.",
        },
        "property_cash_review": {
            "count": 0,
            "sample": [],
            "next_action": "Complete property cash review decisions before moving money.",
        },
        "required_source": {
            "count": 0,
            "sample": [],
            "next_action": "Regenerate or restore required source artifacts before transfer reconciliation.",
        },
        "other": {
            "count": 0,
            "sample": [],
            "next_action": "Review remaining transfer source blockers.",
        },
    }

    def category_for(blocker: str) -> str:
        lowered = blocker.lower()
        if (
            lowered.startswith("monthly_accruals_live_plan_")
            and ("auth_blocked" in lowered or "cdp_blocked" in lowered or "recaptcha" in lowered)
        ):
            return "baselane_auth"
        if "monthly_accruals_" in lowered:
            return "monthly_accruals"
        if lowered.startswith("missing_lofty_reserve_decision_"):
            return "missing_lofty_reserve"
        if lowered.startswith("cf_balance_sheet_"):
            return "cf_balance_sheet"
        if lowered.startswith("property_cash_review:"):
            return "property_cash_review"
        if lowered.startswith("required_source_"):
            return "required_source"
        return "other"

    for raw_blocker in blockers:
        blocker = str(raw_blocker or "").strip()
        if not blocker:
            continue
        category = categories[category_for(blocker)]
        category["count"] += 1
        if len(category["sample"]) < 5:
            category["sample"].append(blocker)

    return {name: details for name, details in categories.items() if details["count"]}


def money_text(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value or "").strip()


def live_accrual_update_summary(status: dict[str, Any]) -> str:
    updates = status.get("monthly_accruals_live_plan_update_details")
    updates = updates if isinstance(updates, list) else []
    mismatches = status.get("monthly_accruals_amount_mismatch_details")
    mismatches = mismatches if isinstance(mismatches, list) else []
    if not updates and not mismatches:
        return ""
    update = updates[0] if updates and isinstance(updates[0], dict) else {}
    mismatch = mismatches[0] if mismatches and isinstance(mismatches[0], dict) else {}
    property_name = update.get("property") or mismatch.get("property") or "unknown property"
    kind = update.get("kind") or mismatch.get("kind") or "accrual"
    month = update.get("month") or mismatch.get("month") or status.get("run_month") or "unknown month"
    row_id = update.get("id") or update.get("transaction_id") or "unknown"
    current = money_text(mismatch.get("current_marker_amount") or mismatch.get("current_row_amount"))
    expected = money_text(mismatch.get("expected_amount") or update.get("absolute_amount"))
    report = status.get("monthly_accruals_live_plan_report") or "reports/baselane_monthly_accruals_202606.live-plan.json"
    digest = status.get("monthly_accruals_live_plan_target_digest")
    digest_text = f" digest `{digest}`" if digest else ""
    return (
        f"Apply/verify guarded Baselane accrual update for {property_name} {month} {kind}: "
        f"row `{row_id}`, current `{current}`, expected `{expected}` from `{report}`{digest_text}."
    )


def build_must_review_next(status: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def add(name: str, gate: str, why: str, next_action: str, artifacts: list[str] | None = None) -> None:
        items.append(
            {
                "order": len(items) + 1,
                "name": name,
                "gate": gate,
                "why": why,
                "next_action": next_action,
                "artifacts": [artifact for artifact in (artifacts or []) if artifact],
            }
        )

    live_accrual_summary = live_accrual_update_summary(status)
    if live_accrual_summary:
        add(
            "Guarded Baselane accrual update",
            "ECO GL accuracy / transfer reconciliation",
            "A live Baselane accrual row differs from the policy-backed expected amount, so downstream ECO Operating Cash is not final.",
            live_accrual_summary,
            [
                str(status.get("monthly_accruals_live_plan_report") or ""),
            ],
        )

    if count_value(status.get("baselane_85104_preclosing_retag_protected_closing_row_review_required_count")):
        add(
            "85-104 protected closing-row review",
            "monthly close / coownership GL policy",
            "Protected pre-closing funding rows still require human disposition before the coownership GL policy can clear.",
            "Review the protected CSV, choose keep/untag/exclude disposition for each row, then import only with explicit approval.",
            [
                str(status.get("baselane_85104_preclosing_protected_row_review_csv") or ""),
                str(status.get("baselane_85104_preclosing_protected_row_review_import_commands") or ""),
            ],
        )

    if status.get("monthly_accruals_append_audit_status") == "restore_ready":
        add(
            "Accrual append audit restore/confirm",
            "ECO GL accuracy / transfer reconciliation",
            "The ledger contains 71 appended AOPS rows versus the verified baseline; transfer totals are blocked until this is restored or explicitly accepted.",
            "Run the guarded restore only with explicit approval, or record an explicit decision accepting the current ledger.",
            [
                str(status.get("monthly_accruals_append_audit_restore_commands_file") or ""),
                str(status.get("monthly_accruals_append_audit_decision_report") or ""),
            ],
        )

    if count_value(status.get("monthly_accruals_blocking_gap_action_count")):
        add(
            "Monthly accrual gap approvals",
            "ECO GL accuracy / transfer reconciliation",
            "PM fee basis gaps and missing fixed coverage keep monthly accrual completeness in review.",
            "Review gap rows, approve true zero/missing-rent cases or fix accrual templates, then import approvals with explicit approval.",
            [
                str(status.get("monthly_accruals_gap_approval_review_csv") or ""),
                str(status.get("monthly_accruals_gap_approval_import_commands") or ""),
            ],
        )

    if count_value(status.get("transfer_reconciliation_missing_lofty_reserve_count")):
        add(
            "Missing Lofty reserve evidence",
            "transfer reconciliation / Lofty financial patch",
            "At least one active transfer row lacks Lofty curr_maintenance_reserve evidence, so the send-to-Lofty formula cannot be final.",
            "Fill and review the missing-reserve decision scaffold from live Lofty evidence, regenerate review candidates, then rerun transfer reconciliation.",
            [
                str(status.get("lofty_financial_patch_missing_lofty_reserve_markdown") or ""),
                str(status.get("lofty_financial_patch_missing_lofty_reserve_decision_scaffold") or ""),
            ],
        )

    if status.get("quitman_804_cash_alignment_source_clean_status") == "review":
        add(
            "804 S Quitman cash-alignment review",
            "transfer reconciliation",
            "804 has unresolved transfer-between-account and owner contribution/distribution groups that can change transferable cash.",
            "Classify the next 804 review groups and import reviewed decisions with explicit approval before moving cash.",
            [],
        )

    if status.get("discord_all_send_plan_validation_discord_review_ready_but_financial_blocked") is True:
        add(
            "Discord review only after financial blockers clear",
            "Discord review / owner email final gate",
            "Discord routing is review-ready, but financial blockers remain; email and live publish must stay gated.",
            "After financial blockers clear, regenerate the Discord all-send plan for human review before any email send.",
            [],
        )

    return items


def build_monthly_close_status(report: dict[str, Any]) -> dict[str, Any]:
    artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
    blockers = report.get("monthly_blocker_command_index")
    blockers = blockers if isinstance(blockers, list) else []
    next_actions = report.get("monthly_completion_next_actions")
    next_actions = next_actions if isinstance(next_actions, list) else []
    transfer_source_blockers = report.get("transfer_reconciliation_source_blockers") or []
    transfer_source_blockers = transfer_source_blockers if isinstance(transfer_source_blockers, list) else []
    top_blockers = [
        {
            "order": item.get("order"),
            "name": item.get("name"),
            "action": item.get("action"),
            "command": item.get("command"),
            "preflight_status": item.get("preflight_status"),
            "preflight_blockers": item.get("preflight_blockers") if isinstance(item.get("preflight_blockers"), list) else [],
            "partial_command_artifact": item.get("partial_command_artifact"),
            "partial_apply_ready": item.get("partial_apply_ready"),
            "partial_apply_ack_env": item.get("partial_apply_ack_env"),
            "partial_apply_warning": item.get("partial_apply_warning"),
            "artifacts": item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {},
        }
        for item in blockers[:5]
        if isinstance(item, dict)
    ]
    ready_manual_blockers = [
        {
            "order": item.get("order"),
            "name": item.get("name"),
            "command": item.get("command"),
            "preflight_status": item.get("preflight_status"),
        }
        for item in blockers
        if (
            isinstance(item, dict)
            and item.get("ready_to_run") is True
            and item.get("safe_to_run_automatically") is not True
        )
    ]
    safe_auto_blockers = [
        item for item in blockers if isinstance(item, dict) and item.get("safe_to_run_automatically") is True
    ]
    status = {
        "status": report.get("status"),
        "failed_step": report.get("failed_step"),
        "run_month": report.get("run_month"),
        "generated_at": iso_z(),
        "close_status_generated_at": None,
        "source_report_generated_at": report.get("generated_at"),
        "monthly_completion_gap_count": report.get("monthly_completion_gap_count"),
        "next_action": report.get("next_action"),
        "top_next_actions": next_actions[:5],
        "monthly_blocker_command_index_count": len(blockers),
        "monthly_blocker_ready_manual_count": len(ready_manual_blockers),
        "monthly_blocker_ready_manual_top": ready_manual_blockers[:5],
        "monthly_blocker_safe_auto_count": len(safe_auto_blockers),
        "monthly_blocker_command_index_markdown": artifacts.get("monthly_blocker_command_index_markdown"),
        "top_blockers": top_blockers,
        "transfer_reconciliation_status": report.get("transfer_reconciliation_status"),
        "transfer_reconciliation_recommended_total": report.get("transfer_reconciliation_recommended_total"),
        "transfer_reconciliation_approved_send_to_lofty_now_total": report.get(
            "transfer_reconciliation_approved_send_to_lofty_now_total"
        ),
        "transfer_reconciliation_held_surplus_pending_review_total": report.get(
            "transfer_reconciliation_held_surplus_pending_review_total"
        ),
        "transfer_reconciliation_provisional_send_to_lofty_total": report.get(
            "transfer_reconciliation_provisional_send_to_lofty_total"
        ),
        "transfer_reconciliation_eco_cash_shortfall_total": report.get(
            "transfer_reconciliation_eco_cash_shortfall_total"
        ),
        "transfer_reconciliation_recommended_total_is_final": report.get("transfer_reconciliation_recommended_total_is_final"),
        "transfer_reconciliation_source_blockers": transfer_source_blockers,
        "transfer_reconciliation_source_blocker_count": len(transfer_source_blockers),
        "transfer_reconciliation_source_blocker_summary": summarize_transfer_source_blockers(
            transfer_source_blockers
        ),
        "transfer_reconciliation_property_cash_review_blockers": report.get("transfer_reconciliation_property_cash_review_blockers") or [],
        "transfer_reconciliation_property_cash_review_details": (
            report.get("transfer_reconciliation_property_cash_review_details") or []
        )[:3],
        "transfer_reconciliation_bank_action_counts": report.get("transfer_reconciliation_bank_action_counts") or {},
        "transfer_reconciliation_bank_action_amount_totals": (
            report.get("transfer_reconciliation_bank_action_amount_totals") or {}
        ),
        "transfer_reconciliation_missing_lofty_reserve_count": report.get(
            "transfer_reconciliation_missing_lofty_reserve_count"
        ),
        "transfer_reconciliation_missing_lofty_reserve_rows": (
            report.get("transfer_reconciliation_missing_lofty_reserve_rows") or []
        )[:10],
        "transfer_reconciliation_missing_lofty_reserve_decision_scaffold": report.get(
            "transfer_reconciliation_missing_lofty_reserve_decision_scaffold"
        ),
        "transfer_reconciliation_missing_lofty_reserve_decision_validation": (
            report.get("transfer_reconciliation_missing_lofty_reserve_decision_validation") or {}
        ),
        "transfer_reconciliation_missing_lofty_reserve_decision_blockers": (
            report.get("transfer_reconciliation_missing_lofty_reserve_decision_blockers") or []
        ),
        "cf_balance_sheet_consistency_status": report.get("cf_balance_sheet_consistency_status"),
        "cf_balance_sheet_consistency_issue_count": report.get("cf_balance_sheet_consistency_issue_count"),
        "cf_balance_sheet_consistency_yhome_update_required_count": report.get(
            "cf_balance_sheet_consistency_yhome_update_required_count"
        ),
        "cf_balance_sheet_consistency_yhome_missing_candidate_count": report.get(
            "cf_balance_sheet_consistency_yhome_missing_candidate_count"
        ),
        "cf_balance_sheet_consistency_yhome_missing_candidates": (
            report.get("cf_balance_sheet_consistency_yhome_missing_candidates") or []
        )[:10],
        "cf_balance_sheet_consistency_yhome_required_states": (
            report.get("cf_balance_sheet_consistency_yhome_required_states") or []
        ),
        "cf_balance_sheet_consistency_yhome_excluded_candidate_count": report.get(
            "cf_balance_sheet_consistency_yhome_excluded_candidate_count"
        ),
        "cf_balance_sheet_consistency_yhome_excluded_candidates": (
            report.get("cf_balance_sheet_consistency_yhome_excluded_candidates") or []
        )[:10],
        "yhome_operating_cash_apply_verify_status": report.get("yhome_operating_cash_apply_verify_status"),
        "yhome_operating_cash_apply_verify_reason": report.get("yhome_operating_cash_apply_verify_reason"),
        "yhome_operating_cash_apply_verify_pre_update_required_count": report.get(
            "yhome_operating_cash_apply_verify_pre_update_required_count"
        ),
        "yhome_operating_cash_apply_verify_pre_missing_candidate_count": report.get(
            "yhome_operating_cash_apply_verify_pre_missing_candidate_count"
        ),
        "yhome_operating_cash_apply_verify_pre_missing_candidates": (
            report.get("yhome_operating_cash_apply_verify_pre_missing_candidates") or []
        )[:10],
        "yhome_operating_cash_apply_verify_pre_required_states": (
            report.get("yhome_operating_cash_apply_verify_pre_required_states") or []
        ),
        "yhome_operating_cash_apply_verify_pre_excluded_candidate_count": report.get(
            "yhome_operating_cash_apply_verify_pre_excluded_candidate_count"
        ),
        "yhome_operating_cash_apply_verify_post_update_required_count": report.get(
            "yhome_operating_cash_apply_verify_post_update_required_count"
        ),
        "yhome_operating_cash_apply_verify_applied_update_count": report.get(
            "yhome_operating_cash_apply_verify_applied_update_count"
        ),
        "yhome_operating_cash_apply_verify_external_write_attempted": report.get(
            "yhome_operating_cash_apply_verify_external_write_attempted"
        ),
        "yhome_operating_cash_apply_verify_report": report.get("yhome_operating_cash_apply_verify_report"),
        "yhome_operating_cash_apply_verify_report_modified_at": report.get(
            "yhome_operating_cash_apply_verify_report_modified_at"
        ),
        "yhome_operating_cash_apply_verify_update_report_range_count": report.get(
            "yhome_operating_cash_apply_verify_update_report_range_count"
        ),
        "yhome_operating_cash_apply_verify_post_apply_verification_ok": report.get(
            "yhome_operating_cash_apply_verify_post_apply_verification_ok"
        ),
        "yhome_operating_cash_apply_verify_next_action": report.get(
            "yhome_operating_cash_apply_verify_next_action"
        ),
        "coownership_gl_policy_validation_status": report.get("coownership_gl_policy_validation_status"),
        "coownership_gl_policy_validation_blocked_count": report.get(
            "coownership_gl_policy_validation_blocked_count"
        ),
        "coownership_gl_policy_validation_blocked_properties": (
            report.get("coownership_gl_policy_validation_blocked_properties") or []
        )[:10],
        "baselane_85104_preclosing_retag_status": report.get("baselane_85104_preclosing_retag_status"),
        "baselane_85104_preclosing_retag_ready_count": report.get(
            "baselane_85104_preclosing_retag_ready_count"
        ),
        "baselane_85104_preclosing_retag_blocked_count": report.get(
            "baselane_85104_preclosing_retag_blocked_count"
        ),
        "baselane_85104_preclosing_retag_payload_digest_reported": report.get(
            "baselane_85104_preclosing_retag_payload_digest_reported"
        ),
        "baselane_85104_preclosing_retag_apply_ready": report.get(
            "baselane_85104_preclosing_retag_apply_ready"
        ),
        "baselane_85104_preclosing_retag_apply_readiness_status": report.get(
            "baselane_85104_preclosing_retag_apply_readiness_status"
        ),
        "baselane_85104_preclosing_retag_apply_readiness_blockers": (
            report.get("baselane_85104_preclosing_retag_apply_readiness_blockers") or []
        )[:10],
        "baselane_85104_preclosing_retag_partial_apply_ready": report.get(
            "baselane_85104_preclosing_retag_partial_apply_ready"
        ),
        "baselane_85104_preclosing_retag_partial_ready_count": report.get(
            "baselane_85104_preclosing_retag_partial_ready_count"
        ),
        "baselane_85104_preclosing_retag_partial_unprotected_blocked_count": report.get(
            "baselane_85104_preclosing_retag_partial_unprotected_blocked_count"
        ),
        "baselane_85104_preclosing_retag_protected_closing_row_count": report.get(
            "baselane_85104_preclosing_retag_protected_closing_row_count"
        ),
        "baselane_85104_preclosing_retag_protected_closing_row_review_status": report.get(
            "baselane_85104_preclosing_retag_protected_closing_row_review_status"
        ),
        "baselane_85104_preclosing_retag_protected_closing_row_reviewed_count": report.get(
            "baselane_85104_preclosing_retag_protected_closing_row_reviewed_count"
        ),
        "baselane_85104_preclosing_retag_protected_closing_row_review_required_count": report.get(
            "baselane_85104_preclosing_retag_protected_closing_row_review_required_count"
        ),
        "baselane_85104_preclosing_retag_protected_closing_row_review_blockers": (
            report.get("baselane_85104_preclosing_retag_protected_closing_row_review_blockers") or []
        )[:10],
        "baselane_85104_preclosing_retag_commands": (
            (report.get("artifacts") or {}).get("baselane_85104_preclosing_retag_commands")
            if isinstance(report.get("artifacts"), dict)
            else None
        ),
        "baselane_85104_preclosing_retag_partial_commands": (
            (report.get("artifacts") or {}).get("baselane_85104_preclosing_retag_partial_commands")
            if isinstance(report.get("artifacts"), dict)
            else None
        ),
        "baselane_85104_preclosing_protected_row_review_csv": (
            (report.get("artifacts") or {}).get("baselane_85104_preclosing_protected_row_review_csv")
            if isinstance(report.get("artifacts"), dict)
            else None
        ),
        "baselane_85104_preclosing_protected_row_review_import_commands": (
            (report.get("artifacts") or {}).get("baselane_85104_preclosing_protected_row_review_import_commands")
            if isinstance(report.get("artifacts"), dict)
            else None
        ),
        "transfer_reconciliation_telegram_status": report.get("transfer_reconciliation_telegram_status"),
        "transfer_reconciliation_telegram_send_ok": report.get("transfer_reconciliation_telegram_send_ok"),
        "transfer_reconciliation_telegram_current_for_transfer": report.get(
            "transfer_reconciliation_telegram_current_for_transfer"
        ),
        "transfer_reconciliation_telegram_transfer_digest": report.get(
            "transfer_reconciliation_telegram_transfer_digest"
        ),
        "transfer_reconciliation_telegram_recorded_transfer_digest": report.get(
            "transfer_reconciliation_telegram_recorded_transfer_digest"
        ),
        "pipeline_candidate_coverage_status": report.get("pipeline_candidate_coverage_status"),
        "pipeline_candidate_coverage_generated_at": report.get("pipeline_candidate_coverage_generated_at"),
        "pipeline_candidate_coverage_mismatch_count": report.get("pipeline_candidate_coverage_mismatch_count"),
        "pipeline_candidate_coverage_mismatches": report.get("pipeline_candidate_coverage_mismatches") or [],
        "pipeline_candidate_coverage_input_digests": report.get("pipeline_candidate_coverage_input_digests") or {},
        "pipeline_candidate_coverage_transfer_reconciliation": report.get("pipeline_candidate_coverage_transfer_reconciliation") or {},
        "pipeline_candidate_coverage_telegram_reconciliation": report.get("pipeline_candidate_coverage_telegram_reconciliation") or {},
        "monthly_accruals_status": report.get("monthly_accruals_status"),
        "monthly_accruals_live_plan_status": report.get("monthly_accruals_live_plan_status"),
        "monthly_accruals_live_plan_report": report.get("monthly_accruals_live_plan_report"),
        "monthly_accruals_live_plan_target_digest": report.get("monthly_accruals_live_plan_target_digest"),
        "monthly_accruals_live_plan_update_count": report.get("monthly_accruals_live_plan_update_count"),
        "monthly_accruals_live_plan_update_details": (
            report.get("monthly_accruals_live_plan_update_details")[:10]
            if isinstance(report.get("monthly_accruals_live_plan_update_details"), list)
            else []
        ),
        "monthly_accruals_amount_mismatch_detail_count": report.get(
            "monthly_accruals_amount_mismatch_detail_count"
        ),
        "monthly_accruals_amount_mismatch_details": (
            report.get("monthly_accruals_amount_mismatch_details")[:10]
            if isinstance(report.get("monthly_accruals_amount_mismatch_details"), list)
            else []
        ),
        "monthly_accruals_missing_fixed_coverage_count": report.get("monthly_accruals_missing_fixed_coverage_count"),
        "monthly_accruals_missing_fixed_coverage_by_kind": report.get(
            "monthly_accruals_missing_fixed_coverage_by_kind"
        )
        or {},
        "monthly_accruals_pm_fee_basis_gap_count": report.get("monthly_accruals_pm_fee_basis_gap_count"),
        "monthly_accruals_unapproved_pm_fee_basis_gap_count": report.get(
            "monthly_accruals_unapproved_pm_fee_basis_gap_count"
        ),
        "monthly_accruals_blocking_gap_action_count": report.get("monthly_accruals_blocking_gap_action_count"),
        "monthly_accruals_gap_action_queue": (report.get("monthly_accruals_gap_action_queue") or [])[:5],
        "monthly_accruals_gap_approval_status": report.get("monthly_accruals_gap_approval_status"),
        "monthly_accruals_gap_approval_issue_count": report.get("monthly_accruals_gap_approval_issue_count"),
        "monthly_accruals_gap_approval_review_csv_counts": (
            report.get("monthly_accruals_gap_approval_review_csv_counts") or {}
        ),
        "monthly_accruals_gap_approval_scaffold": artifacts.get("monthly_accruals_gap_approval_scaffold"),
        "monthly_accruals_gap_approval_review_csv": artifacts.get("monthly_accruals_gap_approval_review_csv"),
        "monthly_accruals_gap_approval_import_commands": artifacts.get(
            "monthly_accruals_gap_approval_import_commands"
        ),
        "monthly_accruals_append_audit_status": report.get("monthly_accruals_append_audit_status"),
        "monthly_accruals_append_audit_decision_report": report.get("monthly_accruals_append_audit_decision_report"),
        "monthly_accruals_append_audit_acceptance": report.get("monthly_accruals_append_audit_acceptance") or {},
        "monthly_accruals_append_audit_blockers": report.get("monthly_accruals_append_audit_blockers") or [],
        "monthly_accruals_append_audit_safe_to_restore_baseline": report.get(
            "monthly_accruals_append_audit_safe_to_restore_baseline"
        ),
        "monthly_accruals_append_audit_added_aops_count": report.get(
            "monthly_accruals_append_audit_added_aops_count"
        ),
        "monthly_accruals_append_audit_added_non_aops_count": report.get(
            "monthly_accruals_append_audit_added_non_aops_count"
        ),
        "monthly_accruals_append_audit_removed_count": report.get("monthly_accruals_append_audit_removed_count"),
        "monthly_accruals_append_audit_added_aops_amount_sum": report.get(
            "monthly_accruals_append_audit_added_aops_amount_sum"
        ),
        "monthly_accruals_append_audit_current": report.get("monthly_accruals_append_audit_current"),
        "monthly_accruals_append_audit_baseline": report.get("monthly_accruals_append_audit_baseline"),
        "monthly_accruals_append_audit_current_sha256": report.get("monthly_accruals_append_audit_current_sha256"),
        "monthly_accruals_append_audit_baseline_sha256": report.get("monthly_accruals_append_audit_baseline_sha256"),
        "monthly_accruals_append_audit_current_row_count": report.get("monthly_accruals_append_audit_current_row_count"),
        "monthly_accruals_append_audit_baseline_row_count": report.get("monthly_accruals_append_audit_baseline_row_count"),
        "monthly_accruals_append_audit_restore_command": report.get("monthly_accruals_append_audit_restore_command"),
        "monthly_accruals_append_audit_restore_command_safe_to_write": report.get(
            "monthly_accruals_append_audit_restore_command_safe_to_write"
        ),
        "monthly_accruals_append_audit_restore_command_safety_blockers": (
            report.get("monthly_accruals_append_audit_restore_command_safety_blockers") or []
        ),
        "monthly_accruals_append_audit_restore_commands_file": report.get(
            "monthly_accruals_append_audit_restore_commands_file"
        ),
        "quitman_804_cash_alignment_status": report.get("quitman_804_cash_alignment_status"),
        "quitman_804_cash_alignment_review_count": report.get("quitman_804_cash_alignment_review_count"),
        "quitman_804_cash_alignment_source_clean_status": report.get("quitman_804_cash_alignment_source_clean_status"),
        "quitman_804_cash_alignment_decision_validation_effective_status": report.get(
            "quitman_804_cash_alignment_decision_validation_effective_status"
        ),
        "quitman_804_cash_alignment_reviewed_template_reviewed_group_count": report.get(
            "quitman_804_cash_alignment_reviewed_template_reviewed_group_count"
        ),
        "quitman_804_cash_alignment_reviewed_template_source_group_count": report.get(
            "quitman_804_cash_alignment_reviewed_template_source_group_count"
        ),
        "quitman_804_cash_alignment_instruction": report.get("quitman_804_cash_alignment_instruction"),
        "quitman_804_cash_alignment_high_priority_unreviewed_group_count": report.get(
            "quitman_804_cash_alignment_high_priority_unreviewed_group_count"
        ),
        "quitman_804_cash_alignment_unreviewed_absolute_amount_sum": report.get(
            "quitman_804_cash_alignment_unreviewed_absolute_amount_sum"
        ),
        "quitman_804_cash_alignment_remaining_signed_amount_by_transfer_basis_effect": (
            report.get("quitman_804_cash_alignment_remaining_signed_amount_by_transfer_basis_effect") or {}
        ),
        "quitman_804_cash_alignment_remaining_row_count_by_transfer_basis_effect": (
            report.get("quitman_804_cash_alignment_remaining_row_count_by_transfer_basis_effect") or {}
        ),
        "quitman_804_cash_alignment_next_review_groups": (
            report.get("quitman_804_cash_alignment_next_review_groups") or []
        )[:5],
        "lofty_listing_financial_update_chain_status": (
            report.get("lofty_listing_financial_update_chain")
            if isinstance(report.get("lofty_listing_financial_update_chain"), dict)
            else {}
        ).get("status"),
        "lofty_pm_publish_generated_at": report.get("lofty_pm_publish_generated_at"),
        "lofty_pm_publish_current_for_run": report.get("lofty_pm_publish_current_for_run"),
        "lofty_financial_patch_readiness_status": report.get("lofty_financial_patch_readiness_status"),
        "lofty_financial_patch_missing_lofty_reserve_count": report.get(
            "lofty_financial_patch_missing_lofty_reserve_count"
        ),
        "lofty_financial_patch_missing_lofty_reserve_csv": report.get(
            "lofty_financial_patch_missing_lofty_reserve_csv"
        ),
        "lofty_financial_patch_missing_lofty_reserve_markdown": report.get(
            "lofty_financial_patch_missing_lofty_reserve_markdown"
        ),
        "lofty_financial_patch_missing_lofty_reserve_decision_scaffold": report.get(
            "lofty_financial_patch_missing_lofty_reserve_decision_scaffold"
        ),
        "lofty_financial_patch_missing_lofty_reserve_decision_scaffold_record_count": report.get(
            "lofty_financial_patch_missing_lofty_reserve_decision_scaffold_record_count"
        ),
        "lofty_financial_patch_blocker_csv_count": report.get("lofty_financial_patch_blocker_csv_count"),
        "lofty_financial_patch_blocker_csv": report.get("lofty_financial_patch_blocker_csv"),
        "lofty_financial_patch_blocker_markdown": report.get("lofty_financial_patch_blocker_markdown"),
        "lofty_financial_patch_candidate_packet_monthly_summary_issue_count": report.get(
            "lofty_financial_patch_candidate_packet_monthly_summary_issue_count"
        ),
        "lofty_financial_patch_candidate_packet_monthly_summary_issue_records": (
            report.get("lofty_financial_patch_candidate_packet_monthly_summary_issue_records") or []
        )[:5],
        "lofty_financial_patch_runtime_monthly_summary_issue_count": report.get(
            "lofty_financial_patch_runtime_monthly_summary_issue_count"
        ),
        "lofty_financial_patch_runtime_monthly_summary_issue_records": (
            report.get("lofty_financial_patch_runtime_monthly_summary_issue_records") or []
        )[:5],
        "discord_all_send_plan_validation_status": report.get("discord_all_send_plan_validation_status"),
        "discord_all_send_plan_validation_unmapped_count": report.get("discord_all_send_plan_validation_unmapped_count"),
        "discord_all_send_plan_validation_stale_route_count": report.get("discord_all_send_plan_validation_stale_route_count"),
        "discord_all_send_plan_validation_missing_financial_summary_count": report.get(
            "discord_all_send_plan_validation_missing_financial_summary_count"
        ),
        "discord_all_send_plan_validation_financial_review_issue_count": report.get(
            "discord_all_send_plan_validation_financial_review_issue_count"
        ),
        "discord_all_send_plan_validation_financial_review_artifact_area_count": report.get(
            "discord_all_send_plan_validation_financial_review_artifact_area_count"
        ),
        "discord_all_send_plan_validation_financial_review_missing_artifact_count": report.get(
            "discord_all_send_plan_validation_financial_review_missing_artifact_count"
        ),
        "discord_all_send_plan_validation_discord_review_ready": report.get(
            "discord_all_send_plan_validation_discord_review_ready"
        ),
        "discord_all_send_plan_validation_discord_review_ready_but_financial_blocked": report.get(
            "discord_all_send_plan_validation_discord_review_ready_but_financial_blocked"
        ),
        "discord_property_update_generated_at": report.get("discord_property_update_generated_at"),
        "discord_property_update_current_for_run": report.get("discord_property_update_current_for_run"),
        "discord_property_update_send_status": report.get("discord_property_update_send_status"),
        "owner_email_send_allowed": report.get("owner_email_send_guard_send_allowed"),
        "owner_email_packet_status": report.get("owner_email_packet_status"),
        "owner_email_packet_property_count": report.get("owner_email_packet_property_count"),
        "owner_email_packet_available_property_count": report.get("owner_email_packet_available_property_count"),
        "owner_email_packet_property_unavailable_count": report.get("owner_email_packet_property_unavailable_count"),
        "owner_email_packet_property_unavailable_reason_counts": report.get(
            "owner_email_packet_property_unavailable_reason_counts"
        )
        or {},
        "owner_email_packet_property_gap_csv": report.get("owner_email_packet_property_gap_csv"),
        "owner_email_packet_native_property_coverage_ok": report.get("owner_email_packet_native_property_coverage_ok"),
        "owner_email_packet_native_property_count": report.get("owner_email_packet_native_property_count"),
        "owner_email_packet_native_eligible_property_count": report.get(
            "owner_email_packet_native_eligible_property_count"
        ),
        "owner_email_packet_native_financially_held_property_count": report.get(
            "owner_email_packet_native_financially_held_property_count"
        ),
        "owner_email_final_financial_blocked": report.get("owner_email_send_guard_email_final_gate_financial_blocked"),
        "owner_email_final_discord_review_blocked": report.get(
            "owner_email_send_guard_email_final_gate_discord_review_blocked"
        ),
        "owner_email_final_transfer_telegram_blocked": report.get(
            "owner_email_send_guard_email_final_gate_transfer_telegram_blocked"
        ),
        "final_publish_email_gate_clear": (
            report.get("lofty_listing_financial_update_chain", {}).get("status") == "ok"
            if isinstance(report.get("lofty_listing_financial_update_chain"), dict)
            else False
        )
        and report.get("transfer_reconciliation_recommended_total_is_final") is True
        and report.get("transfer_reconciliation_telegram_send_ok") is True
        and report.get("transfer_reconciliation_telegram_current_for_transfer") is True
        and report.get("owner_email_send_guard_send_allowed") is True
        and report.get("owner_email_send_guard_email_final_gate_financial_blocked") is not True
        and report.get("owner_email_send_guard_email_final_gate_discord_review_blocked") is not True
        and report.get("owner_email_send_guard_email_final_gate_transfer_telegram_blocked") is not True,
        "owner_email_transfer_telegram_digest_matches_current": report.get(
            "owner_email_send_guard_transfer_telegram_transfer_report_digest_matches_current"
        ),
    }
    status["must_review_next"] = build_must_review_next(status)
    return status


def build_monthly_close_status_markdown(status: dict[str, Any]) -> str:
    must_review_lines: list[str] = []
    must_review_next = status.get("must_review_next") if isinstance(status.get("must_review_next"), list) else []
    if must_review_next:
        for item in must_review_next:
            if not isinstance(item, dict):
                continue
            must_review_lines.extend(
                [
                    f"### {item.get('order')}. {item.get('name')}",
                    f"- Gate: {item.get('gate')}",
                    f"- Why: {item.get('why')}",
                    f"- Next action: {item.get('next_action')}",
                ]
            )
            artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), list) else []
            if artifacts:
                must_review_lines.append("- Artifacts:")
                for artifact in artifacts:
                    must_review_lines.append(f"  - `{artifact}`")
            must_review_lines.append("")
    else:
        must_review_lines.append("- No must-review blockers recorded.")
    transfer_blocker_summary_lines: list[str] = []
    transfer_blocker_summary = status.get("transfer_reconciliation_source_blocker_summary")
    transfer_blocker_summary = transfer_blocker_summary if isinstance(transfer_blocker_summary, dict) else {}
    if transfer_blocker_summary:
        for name, details in transfer_blocker_summary.items():
            if isinstance(details, dict):
                transfer_blocker_summary_lines.append(
                    f"- {name}: count `{details.get('count')}`, next `{details.get('next_action')}`, samples `{details.get('sample')}`"
                )
    else:
        transfer_blocker_summary_lines.append("- No transfer source blocker categories recorded.")
    lines = [
        "# Monthly Close Status",
        "",
        f"- Status: `{status.get('status')}`",
        f"- Failed step: `{status.get('failed_step')}`",
        f"- Run month: `{status.get('run_month')}`",
        f"- Close status generated: `{status.get('close_status_generated_at') or status.get('generated_at')}`",
        f"- Source report generated: `{status.get('source_report_generated_at')}`",
        f"- Completion gaps: `{status.get('monthly_completion_gap_count')}`",
        f"- Next action: {status.get('next_action')}",
        "",
        "## Must Review Next",
        "",
        "__MUST_REVIEW_NEXT_PLACEHOLDER__",
        "",
        "## Transfer / Send Gates",
        "",
        f"- Transfer reconciliation: `{status.get('transfer_reconciliation_status')}`",
        f"- Transfer total final: `{status.get('transfer_reconciliation_recommended_total_is_final')}`",
        f"- Approved to send to Lofty now: `{status.get('transfer_reconciliation_approved_send_to_lofty_now_total')}`",
        f"- Held surplus, do not send yet: `{status.get('transfer_reconciliation_held_surplus_pending_review_total')}`",
        f"- Combined ECO + Lofty OR shortfall before distributions: `{status.get('transfer_reconciliation_eco_cash_shortfall_total')}`",
        f"- Telegram transfer DM: `{status.get('transfer_reconciliation_telegram_status')}`, send ok `{status.get('transfer_reconciliation_telegram_send_ok')}`",
        f"- Telegram transfer current: `{status.get('transfer_reconciliation_telegram_current_for_transfer')}`, digest match `{status.get('owner_email_transfer_telegram_digest_matches_current')}`",
        f"- Lofty listing financial chain: `{status.get('lofty_listing_financial_update_chain_status')}`",
        f"- Lofty publish proof current for this run: `{status.get('lofty_pm_publish_current_for_run')}`, generated `{status.get('lofty_pm_publish_generated_at')}`",
        f"- Lofty financial patch readiness: `{status.get('lofty_financial_patch_readiness_status')}`, blockers `{status.get('lofty_financial_patch_blocker_csv_count')}`",
        f"- FINANCIALS.md candidate summary issues: `{status.get('lofty_financial_patch_candidate_packet_monthly_summary_issue_count')}`",
        f"- FINANCIALS.md runtime summary issues: `{status.get('lofty_financial_patch_runtime_monthly_summary_issue_count')}`",
        f"- Missing Lofty reserve review rows: `{status.get('lofty_financial_patch_missing_lofty_reserve_count')}`",
        f"- Missing Lofty reserve review packet: `{status.get('lofty_financial_patch_missing_lofty_reserve_markdown')}`",
        f"- Missing Lofty reserve decision scaffold: `{status.get('lofty_financial_patch_missing_lofty_reserve_decision_scaffold')}`, rows `{status.get('lofty_financial_patch_missing_lofty_reserve_decision_scaffold_record_count')}`",
        f"- Lofty financial blocker packet: `{status.get('lofty_financial_patch_blocker_markdown')}`",
        f"- Discord all-send validation: `{status.get('discord_all_send_plan_validation_status')}`, unmapped `{status.get('discord_all_send_plan_validation_unmapped_count')}`, stale routes `{status.get('discord_all_send_plan_validation_stale_route_count')}`, missing summaries `{status.get('discord_all_send_plan_validation_missing_financial_summary_count')}`",
        f"- Discord send proof current for this run: `{status.get('discord_property_update_current_for_run')}`, status `{status.get('discord_property_update_send_status')}`, generated `{status.get('discord_property_update_generated_at')}`",
        f"- Discord review ready: `{status.get('discord_all_send_plan_validation_discord_review_ready')}`, ready but financial-blocked `{status.get('discord_all_send_plan_validation_discord_review_ready_but_financial_blocked')}`",
        f"- Discord financial review blockers: `{status.get('discord_all_send_plan_validation_financial_review_issue_count')}`, artifact areas `{status.get('discord_all_send_plan_validation_financial_review_artifact_area_count')}`, missing artifacts `{status.get('discord_all_send_plan_validation_financial_review_missing_artifact_count')}`",
        f"- Monthly candidate coverage: `{status.get('pipeline_candidate_coverage_status')}`, mismatches `{status.get('pipeline_candidate_coverage_mismatch_count')}`, generated `{status.get('pipeline_candidate_coverage_generated_at')}`",
        f"- Owner email send allowed: `{status.get('owner_email_send_allowed')}`",
        f"- Owner email packet: `{status.get('owner_email_packet_status')}`, available `{status.get('owner_email_packet_available_property_count')}/{status.get('owner_email_packet_property_count')}`",
        f"- Owner email native packet coverage: `{status.get('owner_email_packet_native_property_coverage_ok')}`, prepared `{status.get('owner_email_packet_native_property_count')}`, eligible `{status.get('owner_email_packet_native_eligible_property_count')}`, financially held `{status.get('owner_email_packet_native_financially_held_property_count')}`",
        f"- Owner email unavailable reasons: `{status.get('owner_email_packet_property_unavailable_reason_counts')}`",
        f"- Owner email property gap CSV: `{status.get('owner_email_packet_property_gap_csv')}`",
        f"- Owner email financial blocked: `{status.get('owner_email_final_financial_blocked')}`",
        f"- Owner email Discord review blocked: `{status.get('owner_email_final_discord_review_blocked')}`",
        f"- Owner email transfer Telegram blocked: `{status.get('owner_email_final_transfer_telegram_blocked')}`",
        f"- Final Lofty publish/email gate clear: `{status.get('final_publish_email_gate_clear')}`",
        "",
        "## Transfer Reconciliation Blockers",
        "",
        f"- Source blocker count: `{status.get('transfer_reconciliation_source_blocker_count')}`",
        "- Source blocker categories:",
        "__TRANSFER_BLOCKER_SUMMARY_PLACEHOLDER__",
        f"- Source blockers: `{status.get('transfer_reconciliation_source_blockers')}`",
        f"- Property cash blockers: `{status.get('transfer_reconciliation_property_cash_review_blockers')}`",
        f"- Bank action counts: `{status.get('transfer_reconciliation_bank_action_counts')}`",
        f"- Bank action amount totals: `{status.get('transfer_reconciliation_bank_action_amount_totals')}`",
        f"- Missing Lofty reserve transfer rows: `{status.get('transfer_reconciliation_missing_lofty_reserve_count')}`, samples `{status.get('transfer_reconciliation_missing_lofty_reserve_rows')}`",
        f"- Missing Lofty reserve transfer decision validation: `{status.get('transfer_reconciliation_missing_lofty_reserve_decision_validation')}`",
        f"- Missing Lofty reserve transfer decision blockers: `{status.get('transfer_reconciliation_missing_lofty_reserve_decision_blockers')}`",
        f"- CF balance-sheet consistency: `{status.get('cf_balance_sheet_consistency_status')}`, authoritative issues `{status.get('cf_balance_sheet_consistency_issue_count')}`",
        f"- Non-authoritative Yhome work product: updates `{status.get('cf_balance_sheet_consistency_yhome_update_required_count')}`, missing rows `{status.get('cf_balance_sheet_consistency_yhome_missing_candidate_count')}`; does not block transfer or investor outputs",
        f"- Yhome required states: `{status.get('cf_balance_sheet_consistency_yhome_required_states')}`, excluded non-Yhome candidates `{status.get('cf_balance_sheet_consistency_yhome_excluded_candidate_count')}`",
        f"- Missing Yhome rows: `{status.get('cf_balance_sheet_consistency_yhome_missing_candidates')}`",
        f"- Excluded non-Yhome candidates: `{status.get('cf_balance_sheet_consistency_yhome_excluded_candidates')}`",
        f"- Yhome apply/verify: `{status.get('yhome_operating_cash_apply_verify_status')}`, reason `{status.get('yhome_operating_cash_apply_verify_reason')}`",
        f"- Yhome pre/post/applied updates: `{status.get('yhome_operating_cash_apply_verify_pre_update_required_count')}/{status.get('yhome_operating_cash_apply_verify_post_update_required_count')}/{status.get('yhome_operating_cash_apply_verify_applied_update_count')}`",
        f"- Yhome apply/verify required states: `{status.get('yhome_operating_cash_apply_verify_pre_required_states')}`, excluded non-Yhome candidates `{status.get('yhome_operating_cash_apply_verify_pre_excluded_candidate_count')}`",
        f"- Yhome apply/verify missing required rows: `{status.get('yhome_operating_cash_apply_verify_pre_missing_candidate_count')}`, samples `{status.get('yhome_operating_cash_apply_verify_pre_missing_candidates')}`",
        f"- Yhome external write attempted: `{status.get('yhome_operating_cash_apply_verify_external_write_attempted')}`",
        f"- Yhome report: `{status.get('yhome_operating_cash_apply_verify_report')}`, modified `{status.get('yhome_operating_cash_apply_verify_report_modified_at')}`",
        f"- Yhome guarded range count: `{status.get('yhome_operating_cash_apply_verify_update_report_range_count')}`",
        f"- Yhome post-apply verification ok: `{status.get('yhome_operating_cash_apply_verify_post_apply_verification_ok')}`",
        f"- Yhome next action: {status.get('yhome_operating_cash_apply_verify_next_action')}",
        "",
        "## FINANCIALS.md Summary Field Issues",
        "",
    ]
    for label, records in (
        (
            "candidate",
            status.get("lofty_financial_patch_candidate_packet_monthly_summary_issue_records")
            if isinstance(status.get("lofty_financial_patch_candidate_packet_monthly_summary_issue_records"), list)
            else [],
        ),
        (
            "runtime",
            status.get("lofty_financial_patch_runtime_monthly_summary_issue_records")
            if isinstance(status.get("lofty_financial_patch_runtime_monthly_summary_issue_records"), list)
            else [],
        ),
    ):
        if records:
            for item in records[:5]:
                if isinstance(item, dict):
                    lines.append(
                        "- `{}` `{}`: missing `{}`; FINANCIALS.md `{}`".format(
                            label,
                            item.get("property_name"),
                            item.get("missing_required_fields"),
                            item.get("financials_md"),
                        )
                    )
    if not any(
        isinstance(status.get(key), list) and status.get(key)
        for key in (
            "lofty_financial_patch_candidate_packet_monthly_summary_issue_records",
            "lofty_financial_patch_runtime_monthly_summary_issue_records",
        )
    ):
        lines.append("- No FINANCIALS.md summary field issues recorded.")
    lines.extend([
        "",
        "## Coownership / 85-104 Retag",
        "",
        f"- Coownership GL policy: `{status.get('coownership_gl_policy_validation_status')}`, blocked `{status.get('coownership_gl_policy_validation_blocked_count')}`",
        f"- Blocked coownership properties: `{status.get('coownership_gl_policy_validation_blocked_properties')}`",
        f"- 85-104 pre-closing retag: `{status.get('baselane_85104_preclosing_retag_status')}`, ready `{status.get('baselane_85104_preclosing_retag_ready_count')}`, blocked `{status.get('baselane_85104_preclosing_retag_blocked_count')}`",
        f"- 85-104 retag payload digest: `{status.get('baselane_85104_preclosing_retag_payload_digest_reported')}`",
        f"- 85-104 full apply ready: `{status.get('baselane_85104_preclosing_retag_apply_ready')}`, readiness `{status.get('baselane_85104_preclosing_retag_apply_readiness_status')}`, blockers `{status.get('baselane_85104_preclosing_retag_apply_readiness_blockers')}`",
        f"- 85-104 partial apply ready: `{status.get('baselane_85104_preclosing_retag_partial_apply_ready')}`, ready rows `{status.get('baselane_85104_preclosing_retag_partial_ready_count')}`, unprotected blocked `{status.get('baselane_85104_preclosing_retag_partial_unprotected_blocked_count')}`",
        f"- 85-104 protected closing rows: `{status.get('baselane_85104_preclosing_retag_protected_closing_row_count')}`, review `{status.get('baselane_85104_preclosing_retag_protected_closing_row_review_status')}`, reviewed `{status.get('baselane_85104_preclosing_retag_protected_closing_row_reviewed_count')}/{status.get('baselane_85104_preclosing_retag_protected_closing_row_review_required_count')}`",
        f"- 85-104 protected review blockers: `{status.get('baselane_85104_preclosing_retag_protected_closing_row_review_blockers')}`",
        f"- 85-104 prepared full apply script: `{status.get('baselane_85104_preclosing_retag_commands')}`",
        f"- 85-104 prepared partial apply script: `{status.get('baselane_85104_preclosing_retag_partial_commands')}`",
        f"- 85-104 protected review CSV: `{status.get('baselane_85104_preclosing_protected_row_review_csv')}`",
        f"- 85-104 protected review import: `{status.get('baselane_85104_preclosing_protected_row_review_import_commands')}`",
        "- 85-104 mutation rule: do not run prepared apply/import scripts until human review, CDP auth, and digest gates pass.",
        "",
        "## Accrual / 804 Review Queues",
        "",
        f"- Monthly accruals: `{status.get('monthly_accruals_status')}`",
        f"- Live accrual plan: `{status.get('monthly_accruals_live_plan_status')}`, updates `{status.get('monthly_accruals_live_plan_update_count')}`, report `{status.get('monthly_accruals_live_plan_report')}`",
        f"- Live accrual target digest: `{status.get('monthly_accruals_live_plan_target_digest')}`",
        f"- Amount mismatch details: `{status.get('monthly_accruals_amount_mismatch_details')}`",
        f"- Live update details: `{status.get('monthly_accruals_live_plan_update_details')}`",
        f"- Missing fixed accrual coverage: `{status.get('monthly_accruals_missing_fixed_coverage_count')}`",
        f"- Missing fixed accrual coverage by kind: `{status.get('monthly_accruals_missing_fixed_coverage_by_kind')}`",
        f"- PM fee basis gaps: `{status.get('monthly_accruals_pm_fee_basis_gap_count')}`",
        f"- Unapproved PM fee basis gaps: `{status.get('monthly_accruals_unapproved_pm_fee_basis_gap_count')}`",
        f"- Blocking accrual actions: `{status.get('monthly_accruals_blocking_gap_action_count')}`",
        f"- Gap approval status: `{status.get('monthly_accruals_gap_approval_status')}`, issues `{status.get('monthly_accruals_gap_approval_issue_count')}`",
        f"- Gap approval review CSV counts: `{status.get('monthly_accruals_gap_approval_review_csv_counts')}`",
        f"- Gap approval artifacts: scaffold `{status.get('monthly_accruals_gap_approval_scaffold')}`, review CSV `{status.get('monthly_accruals_gap_approval_review_csv')}`, import `{status.get('monthly_accruals_gap_approval_import_commands')}`",
        f"- Accrual append audit: `{status.get('monthly_accruals_append_audit_status')}`, added AOPS `{status.get('monthly_accruals_append_audit_added_aops_count')}`, safe restore `{status.get('monthly_accruals_append_audit_safe_to_restore_baseline')}`",
        f"- 804 review rows: `{status.get('quitman_804_cash_alignment_review_count')}`",
        f"- 804 source clean: `{status.get('quitman_804_cash_alignment_source_clean_status')}`",
        f"- 804 reviewed groups: `{status.get('quitman_804_cash_alignment_reviewed_template_reviewed_group_count')}/{status.get('quitman_804_cash_alignment_reviewed_template_source_group_count')}`",
        f"- 804 high-priority unreviewed groups: `{status.get('quitman_804_cash_alignment_high_priority_unreviewed_group_count')}`",
        f"- 804 unreviewed absolute exposure: `{status.get('quitman_804_cash_alignment_unreviewed_absolute_amount_sum')}`",
        f"- 804 remaining signed amount by transfer-basis effect: `{status.get('quitman_804_cash_alignment_remaining_signed_amount_by_transfer_basis_effect')}`",
        f"- 804 remaining row count by transfer-basis effect: `{status.get('quitman_804_cash_alignment_remaining_row_count_by_transfer_basis_effect')}`",
        f"- 804 instruction: {status.get('quitman_804_cash_alignment_instruction')}",
        "",
        "## Top Blockers",
        "",
    ])
    gap_queue = status.get("monthly_accruals_gap_action_queue") if isinstance(status.get("monthly_accruals_gap_action_queue"), list) else []
    if gap_queue:
        lines.extend(["", "### Accrual Gap Action Queue", ""])
        for item in gap_queue:
            if isinstance(item, dict):
                lines.append(f"- `{item.get('property')}` `{item.get('kind')}`: {item.get('action')}")
    if status.get("monthly_accruals_append_audit_status"):
        lines.extend(["", "### Accrual Append Audit", ""])
        lines.append(
            "- Status `{}`; added AOPS `{}`; non-AOPS `{}`; removed `{}`; amount `{}`".format(
                status.get("monthly_accruals_append_audit_status"),
                status.get("monthly_accruals_append_audit_added_aops_count"),
                status.get("monthly_accruals_append_audit_added_non_aops_count"),
                status.get("monthly_accruals_append_audit_removed_count"),
                status.get("monthly_accruals_append_audit_added_aops_amount_sum"),
            )
        )
        lines.append(
            "- Current `{}` rows `{}` sha `{}`".format(
                status.get("monthly_accruals_append_audit_current"),
                status.get("monthly_accruals_append_audit_current_row_count"),
                status.get("monthly_accruals_append_audit_current_sha256"),
            )
        )
        lines.append(
            "- Baseline `{}` rows `{}` sha `{}`".format(
                status.get("monthly_accruals_append_audit_baseline"),
                status.get("monthly_accruals_append_audit_baseline_row_count"),
                status.get("monthly_accruals_append_audit_baseline_sha256"),
            )
        )
        if status.get("monthly_accruals_append_audit_restore_command"):
            lines.append(
                "- Restore command requires explicit operator execution: `{}`; safe to write `{}`; blockers `{}`".format(
                    status.get("monthly_accruals_append_audit_restore_command"),
                    status.get("monthly_accruals_append_audit_restore_command_safe_to_write"),
                    status.get("monthly_accruals_append_audit_restore_command_safety_blockers"),
                )
            )
        if status.get("monthly_accruals_append_audit_restore_commands_file"):
            lines.append(
                "- Restore command artifact: `{}`".format(
                    status.get("monthly_accruals_append_audit_restore_commands_file")
                )
            )
        if status.get("monthly_accruals_append_audit_decision_report"):
            lines.append(
                "- Accept-current decision artifact: `{}`; acceptance `{}`; blockers `{}`".format(
                    status.get("monthly_accruals_append_audit_decision_report"),
                    status.get("monthly_accruals_append_audit_acceptance"),
                    status.get("monthly_accruals_append_audit_blockers"),
                )
            )
    property_cash_details = (
        status.get("transfer_reconciliation_property_cash_review_details")
        if isinstance(status.get("transfer_reconciliation_property_cash_review_details"), list)
        else []
    )
    if property_cash_details:
        lines.extend(["", "### Property Cash Review Details", ""])
        for item in property_cash_details:
            if isinstance(item, dict):
                lines.append(
                    "- `{}`: review rows `{}`, high-priority unresolved `{}`".format(
                        item.get("property") or item.get("property_name"),
                        item.get("classification_review_count")
                        or item.get("property_cash_review_classification_review_count"),
                        (
                            item.get("property_cash_review_high_priority_unresolved_sum")
                            or (item.get("net_cash_exposure_review") or {}).get("high_priority_unresolved_sum")
                        ),
                    )
                )
    next_804_groups = (
        status.get("quitman_804_cash_alignment_next_review_groups")
        if isinstance(status.get("quitman_804_cash_alignment_next_review_groups"), list)
        else []
    )
    if next_804_groups:
        lines.extend(["", "### 804 Next Review Groups", ""])
        for item in next_804_groups:
            if not isinstance(item, dict):
                continue
            lines.append(
                "- `{}` `{}`: rows `{}`, abs `{}`, signed `{}`, suggested `{}`".format(
                    item.get("group_id"),
                    item.get("category"),
                    item.get("row_count"),
                    item.get("absolute_amount_sum"),
                    item.get("signed_amount_sum"),
                    item.get("suggested_decision"),
                )
            )
            account = str(item.get("account") or "").strip()
            buckets = item.get("cash_alignment_review_bucket_names")
            if account or buckets:
                bucket_text = ", ".join(str(bucket) for bucket in buckets) if isinstance(buckets, list) else ""
                lines.append(f"  - account: `{account or 'unknown'}`; buckets: `{bucket_text}`")
    blockers = status.get("top_blockers") if isinstance(status.get("top_blockers"), list) else []
    if not blockers:
        lines.append("- No top blockers recorded.")
    for item in blockers:
        if not isinstance(item, dict):
            continue
        lines.extend(
            [
                f"### {item.get('order')}. {item.get('name')}",
                f"- Action: {item.get('action')}",
                f"- Preflight: `{item.get('preflight_status')}`",
            ]
        )
        if item.get("command"):
            lines.append(f"- Command: `{item.get('command')}`")
        if item.get("partial_apply_warning"):
            lines.append(f"- Partial apply warning: {item.get('partial_apply_warning')}")
        if item.get("partial_apply_ack_env"):
            lines.append(f"- Partial acknowledgement env: `{item.get('partial_apply_ack_env')}=1`")
        if item.get("partial_command_artifact"):
            lines.append(f"- Partial command artifact: `{item.get('partial_command_artifact')}`")
        blockers_list = item.get("preflight_blockers") if isinstance(item.get("preflight_blockers"), list) else []
        if blockers_list:
            lines.append("- Blockers:")
            for blocker in blockers_list[:8]:
                lines.append(f"  - `{blocker}`")
        artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
        if artifacts:
            lines.append("- Key artifacts:")
            for key, value in list(artifacts.items())[:10]:
                lines.append(f"  - `{key}`: `{value}`")
        lines.append("")
    rendered_lines: list[str] = []
    for line in lines:
        if line == "__MUST_REVIEW_NEXT_PLACEHOLDER__":
            rendered_lines.extend(must_review_lines)
        elif line == "__TRANSFER_BLOCKER_SUMMARY_PLACEHOLDER__":
            rendered_lines.extend(transfer_blocker_summary_lines)
        else:
            rendered_lines.append(line)
    return "\n".join(rendered_lines).rstrip() + "\n"


def write_monthly_close_status(json_path: Path, markdown_path: Path, report: dict[str, Any]) -> dict[str, Any]:
    status = build_monthly_close_status(report)
    status["close_status_generated_at"] = status.get("generated_at")
    status["close_status_report"] = str(json_path)
    status["close_status_markdown"] = str(markdown_path)
    status["close_status_write_status"] = "writing"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(build_monthly_close_status_markdown(status), encoding="utf-8")
    status["close_status_write_status"] = "written"
    json_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return status


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "missing", "path": str(path)}
    except Exception as exc:
        return {"status": "unreadable", "path": str(path), "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Write monthly close-status JSON and Markdown from the monthly run report.")
    parser.add_argument("--monthly-run-report", type=Path, default=Path("reports/baselane_financials_monthly_run_report.json"))
    parser.add_argument("--report", type=Path, default=Path("reports/baselane_financials_monthly_close_status.json"))
    parser.add_argument("--markdown", type=Path, default=Path("reports/baselane_financials_monthly_close_status.md"))
    args = parser.parse_args()

    monthly_run = read_json(args.monthly_run_report)
    status = write_monthly_close_status(args.report, args.markdown, monthly_run)
    print(
        json.dumps(
            {
                "status": status.get("status"),
                "failed_step": status.get("failed_step"),
                "close_status_write_status": status.get("close_status_write_status"),
                "report": str(args.report),
                "markdown": str(args.markdown),
            },
            sort_keys=True,
        )
    )
    return 0 if status.get("close_status_write_status") == "written" else 1


if __name__ == "__main__":
    raise SystemExit(main())
