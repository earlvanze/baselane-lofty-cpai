#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS = 30.0
EXPECTED_LOCAL_MODEL = "ollama-cyber/qwen3.5:35b-a3b"
EXPECTED_LOCAL_PROVIDER = "ollama-cyber"
EXPECTED_LOCAL_MODEL_ID = "qwen3.5:35b-a3b"
EXPECTED_FINANCE_CONTRACT_RESPONSE = '{"category":"Rents","column_e_sum":177679.32,"ok":true}'
YHOME_OPERATING_CASH_APPLY_COMMAND = "YHOME_GSHEET_WRITE_ENABLED=1 python3 scripts/yhome_operating_cash_apply_verify.py --apply"
YHOME_OPERATING_CASH_TARGET_COLUMNS = ["Lofty Operating Cash", "ECO Net DAO Funds"]
POST_AUTH_RESUME_COMMAND = "bash scripts/baselane_financials_post_auth_resume.sh"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    if isinstance(data, dict):
        return data
    return {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def status_is_ok(status: object) -> bool:
    return str(status or "").lower() in {"ok", "ok_dry_run", "no_reply"}


def iso_age_hours(value: object, now: datetime | None = None) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        checked_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return round(max(0.0, (now - checked_at.astimezone(timezone.utc)).total_seconds() / 3600), 2)


def fresh_generated_at(report: dict[str, Any], max_age_hours: float = LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS) -> bool:
    if not str(report.get("generated_at") or "").strip():
        return True
    age_hours = iso_age_hours(report.get("generated_at"))
    return age_hours is not None and age_hours <= max_age_hours


def hex_digest_64(value: object) -> bool:
    raw = str(value or "").strip()
    return len(raw) == 64 and all(char in "0123456789abcdef" for char in raw.lower())


def nested_dict(report: dict[str, Any], key: str) -> dict[str, Any]:
    value = report.get(key)
    return value if isinstance(value, dict) else {}


def local_model_ready(report: dict[str, Any]) -> bool:
    direct = nested_dict(report, "direct_smoke")
    finance = nested_dict(report, "finance_contract_smoke")
    contract = nested_dict(report, "validation_contract")
    scope = nested_dict(report, "model_execution_scope")
    contract_scope = nested_dict(contract, "model_execution_scope")
    direct_attempted = first_present(report.get("direct_smoke_attempted"), direct.get("attempted"))
    direct_ok = first_present(report.get("direct_smoke_ok"), direct.get("ok"))
    direct_response = first_present(report.get("direct_smoke_response"), direct.get("response"))
    finance_attempted = first_present(report.get("finance_contract_smoke_attempted"), finance.get("attempted"))
    finance_ok = first_present(report.get("finance_contract_smoke_ok"), finance.get("ok"))
    finance_response = first_present(report.get("finance_contract_smoke_response"), finance.get("response"))
    return (
        report.get("status") == "ok"
        and int(report.get("issue_count") or 0) == 0
        and report.get("model") == EXPECTED_LOCAL_MODEL
        and report.get("provider") == EXPECTED_LOCAL_PROVIDER
        and report.get("model_id") == EXPECTED_LOCAL_MODEL_ID
        and report.get("configured_model_present") is True
        and report.get("selected_endpoint_from_config") is True
        and report.get("model_available") is True
        and report.get("small_model_execution_allowed") is False
        and report.get("small_model_pipeline_execution_allowed") is False
        and report.get("small_model_task_scoped_execution_allowed") is True
        and scope.get("deterministic_only") is True
        and scope.get("pipeline_execution_allowed") is False
        and "calculating ledger balances" in set(scope.get("forbidden_uses") or [])
        and direct_attempted is True
        and direct_ok is True
        and direct_response == "BASELANE_MODEL_OK"
        and finance_attempted is True
        and finance_ok is True
        and finance_response == EXPECTED_FINANCE_CONTRACT_RESPONSE
        and report.get("finance_contract_expected_response") == EXPECTED_FINANCE_CONTRACT_RESPONSE
        and contract.get("expected_model") == EXPECTED_LOCAL_MODEL
        and contract.get("selected_endpoint_from_config") is True
        and contract.get("direct_smoke_ok") is True
        and contract.get("direct_smoke_response") == "BASELANE_MODEL_OK"
        and contract.get("finance_contract_smoke_ok") is True
        and contract.get("finance_contract_response") == EXPECTED_FINANCE_CONTRACT_RESPONSE
        and contract.get("model_scope_deterministic") is True
        and contract.get("model_pipeline_execution_denied") is True
        and contract_scope.get("deterministic_only") is True
        and contract_scope.get("pipeline_execution_allowed") is False
        and hex_digest_64(report.get("validation_digest"))
        and fresh_generated_at(report)
    )


def monthly_readiness_snapshot_current(snapshot: dict[str, Any], readiness: dict[str, Any]) -> bool:
    return (
        snapshot.get("status") == readiness.get("status")
        and snapshot.get("owner_email_allowed") == readiness.get("owner_email_allowed")
        and int(snapshot.get("blocker_count") or 0) == int(readiness.get("blocker_count") or 0)
    )


def refreshed_monthly_readiness_snapshot(snapshot: dict[str, Any], readiness: dict[str, Any]) -> dict[str, Any]:
    if not monthly_readiness_snapshot_current(snapshot, readiness):
        return snapshot
    refreshed = json.loads(json.dumps(snapshot))
    for key in ("actionable_summary", "monthly_comms_gates", "primary_blocker"):
        if key in readiness:
            refreshed[key] = readiness.get(key)
    refreshed["actionable_fields_refreshed_from_current_readiness"] = True
    return refreshed


def sample(values: list[str], limit: int) -> list[str]:
    return values[: max(0, limit)]


def rebase_workspace_artifact_path(root: Path, value: object) -> object:
    if not isinstance(value, str):
        return value
    marker = "/home/umbrel/.openclaw/workspace/"
    raw = value.strip()
    if marker not in raw:
        return value
    relative = raw.split(marker, 1)[1].lstrip("/")
    if not relative:
        return value
    return str(root / relative)


def rebase_workspace_artifacts(root: Path, value: Any) -> Any:
    if isinstance(value, dict):
        return {key: rebase_workspace_artifacts(root, nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [rebase_workspace_artifacts(root, nested) for nested in value]
    return rebase_workspace_artifact_path(root, value)


def summarize_candidate_records(candidate_packet: dict[str, Any], limit: int) -> dict[str, Any]:
    records = [record for record in candidate_packet.get("records") or [] if isinstance(record, dict)]
    update_targets = [str(record.get("update_approval_target") or "") for record in records if record.get("update_approval_target")]
    financial_targets = [str(record.get("financial_approval_target") or "") for record in records if record.get("financial_approval_target")]
    property_names = [str(record.get("property_name") or "") for record in records if record.get("property_name")]
    return {
        "property_count": len(records),
        "property_samples": sample(property_names, limit),
        "update_approval_target_count": len(update_targets),
        "financial_approval_target_count": len(financial_targets),
        "update_approval_target_samples": sample(update_targets, limit),
        "financial_approval_target_samples": sample(financial_targets, limit),
    }


def summarize_cf_plan(cf_plan: dict[str, Any], limit: int) -> dict[str, Any]:
    results = [record for record in cf_plan.get("results") or [] if isinstance(record, dict)]
    status_counts = Counter(str(record.get("status") or "unknown") for record in results)
    action_counts = Counter(str(record.get("action") or "unknown") for record in results)
    needs_approval = [record for record in results if record.get("status") == "needs_approval"]
    blocked = [record for record in results if record.get("status") == "blocked_action"]
    approval_samples = [
        {
            "id": record.get("id"),
            "property": record.get("property"),
            "label": record.get("label"),
            "action": record.get("action"),
            "current_value": record.get("current_value"),
            "new_value": record.get("new_value"),
        }
        for record in needs_approval[:limit]
    ]
    blocked_samples = [
        {
            "property": record.get("property"),
            "label": record.get("label"),
            "action": record.get("action"),
            "reason": record.get("reason"),
        }
        for record in blocked[:limit]
    ]
    return {
        "status_counts": dict(status_counts),
        "action_counts": dict(action_counts),
        "needs_approval_count": len(needs_approval),
        "blocked_count": len(blocked),
        "needs_approval_samples": approval_samples,
        "blocked_samples": blocked_samples,
    }


def summarize_untagged(untagged_packet: dict[str, Any], limit: int) -> dict[str, Any]:
    rows = [record for record in untagged_packet.get("rows") or [] if isinstance(record, dict)]
    reason_counts = Counter(str(record.get("review_reason") or "unknown") for record in rows)
    merchant_counts = Counter(str(record.get("Merchant") or record.get("Description") or "unknown") for record in rows)
    review_required = [record for record in rows if record.get("review_required") is True]
    return {
        "row_count": len(rows),
        "review_required_count": len(review_required),
        "auto_suggested_count": int(untagged_packet.get("auto_suggested_count") or 0),
        "reason_counts": dict(reason_counts),
        "top_merchants": dict(merchant_counts.most_common(limit)),
    }


def count_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def hemlane_cdp_command(comms_root: Path, run_month: str | None) -> str:
    month = str(run_month or "YYYY-MM").strip() or "YYYY-MM"
    return f"cd {shlex.quote(str(comms_root))} && bash scripts/monthly_hemlane_cdp.sh --month {shlex.quote(month)} --dry-run"


def hemlane_post_auth_action(auth_action: str | None = None) -> str:
    prefix = f"{auth_action.strip().rstrip(';')}; " if auth_action and auth_action.strip() else ""
    return (
        f"{prefix}run {POST_AUTH_RESUME_COMMAND}; "
        "this refreshes Hemlane rent-roll evidence, monthly dry-run readiness, and EOD reporting while keeping owner email, Lofty PM publish, and guarded live writes disabled."
    )


def hemlane_login_screen_recovery_action(attempt_count: int | None = None, *, captcha_conditional: bool = False) -> str:
    attempt_note = f" ({attempt_count} tries)" if attempt_count else ""
    captcha_note = "; solve reCAPTCHA only if still shown" if captcha_conditional else ""
    return (
        f"Hard refresh or close/open the Hemlane rent-roll tab{attempt_note}{captcha_note}; "
        f"authenticate only if still redirected, then run {POST_AUTH_RESUME_COMMAND}; "
        "this refreshes Hemlane rent-roll evidence, monthly dry-run readiness, and EOD reporting while keeping owner email, Lofty PM publish, and guarded live writes disabled."
    )


def hemlane_visible_login_after_recovery_action(attempt_count: int | None = None) -> str:
    attempt_note = f"; auto recovery tried {attempt_count}x" if attempt_count else "; auto recovery done"
    return hemlane_post_auth_action(f"Finish Hemlane login/CAPTCHA{attempt_note}")


def hemlane_recaptcha_after_bitwarden_action() -> str:
    return hemlane_post_auth_action("Solve Hemlane reCAPTCHA / finish login in the visible tab (Bitwarden credentials already submitted)")


def normalized_hemlane_preflight_action(preflight: dict[str, Any], attempt_count: int | None = None) -> object:
    action = str(preflight.get("next_action") or "").strip()
    if "Auth Hemlane visible tab" in action:
        attempts = attempt_count or count_value(
            preflight.get("login_recovery_try_count")
            if "login_recovery_try_count" in preflight
            else preflight.get("login_recovery_attempt_count")
        )
        return hemlane_visible_login_after_recovery_action(attempts)
    return preflight.get("next_action")


def first_present(*values: object) -> object:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def hemlane_rent_roll_next_action(monthly_comms: dict[str, Any], comms_root: Path) -> str:
    run_month = str(monthly_comms.get("run_month") or "YYYY-MM")
    rent_roll_source = monthly_comms.get("rent_roll_source") if isinstance(monthly_comms.get("rent_roll_source"), dict) else {}
    attempt_count = count_value(
        monthly_comms.get("hemlane_login_recovery_try_count")
        or rent_roll_source.get("hemlane_login_recovery_try_count")
        or monthly_comms.get("hemlane_login_recovery_attempt_count")
        or rent_roll_source.get("hemlane_login_recovery_attempt_count")
    )
    bitwarden_submitted = (
        monthly_comms.get("hemlane_capture_bitwarden_login_submit_ok") is True
        or rent_roll_source.get("hemlane_capture_bitwarden_login_submit_ok") is True
    )
    recaptcha_required = (
        monthly_comms.get("hemlane_capture_issue") == "recaptcha_required"
        or monthly_comms.get("hemlane_manual_auth_reason") == "recaptcha_required"
        or monthly_comms.get("hemlane_bitwarden_login_recaptcha_error") is True
        or rent_roll_source.get("hemlane_capture_issue") == "recaptcha_required"
        or rent_roll_source.get("hemlane_manual_auth_reason") == "recaptcha_required"
        or rent_roll_source.get("hemlane_bitwarden_login_recaptcha_error") is True
    )
    source_action = str(
        rent_roll_source.get("hemlane_capture_next_action")
        or rent_roll_source.get("next_action")
        or monthly_comms.get("hemlane_capture_next_action")
        or monthly_comms.get("hemlane_next_action")
        or ""
    ).strip()
    if "Auth Hemlane visible tab" in source_action:
        return hemlane_visible_login_after_recovery_action(attempt_count)
    login_recovery_opened_rent_roll = (
        monthly_comms.get("hemlane_login_recovery_opened_rent_roll") is True
        or rent_roll_source.get("hemlane_login_recovery_opened_rent_roll") is True
    )
    recovery_exhausted = (
        monthly_comms.get("hemlane_login_recovery_exhausted") is True
        or rent_roll_source.get("hemlane_login_recovery_exhausted") is True
        or monthly_comms.get("hemlane_automated_browser_recovery_complete") is True
        or rent_roll_source.get("hemlane_automated_browser_recovery_complete") is True
        or monthly_comms.get("hemlane_manual_auth_phase") == "after_browser_recovery"
        or rent_roll_source.get("hemlane_manual_auth_phase") == "after_browser_recovery"
    )
    hemlane_at_login = (
        monthly_comms.get("hemlane_cdp_preflight_status") == "review"
        and monthly_comms.get("hemlane_cdp_available") is True
        and count_value(monthly_comms.get("hemlane_login_tab_count")) > 0
        and count_value(monthly_comms.get("hemlane_logged_in_tab_count")) == 0
    )
    if recaptcha_required:
        if hemlane_at_login and login_recovery_opened_rent_roll:
            return hemlane_visible_login_after_recovery_action(attempt_count)
        if bitwarden_submitted:
            return hemlane_recaptcha_after_bitwarden_action()
        return hemlane_login_screen_recovery_action(attempt_count, captcha_conditional=True)
    if hemlane_at_login and login_recovery_opened_rent_roll and attempt_count:
        return hemlane_visible_login_after_recovery_action(attempt_count)
    return hemlane_login_screen_recovery_action()


def lofty_pm_next_action(cdp_preflight: dict[str, Any]) -> str:
    direct = str(cdp_preflight.get("next_action") or "").strip()
    if direct:
        if "rerun monthly readiness" in direct.lower():
            return direct
        return f"{direct} Then rerun monthly readiness before live capture or publish."
    return (
        "Hard-refresh or close/open Lofty property-owners tab; authenticate only if still redirected, "
        "then rerun monthly readiness before live capture or publish."
    )


def monthly_readiness_blocked_reason(readiness: dict[str, Any]) -> str:
    actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
    primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
    primary_text = str(primary.get("blocker") or primary.get("class") or "").strip()
    actionable_count = count_value(actionable.get("actionable_blocker_count"))
    if primary_text:
        return f"monthly readiness owner_email_allowed=false; primary={primary_text}; actionable={actionable_count}"
    return f"monthly readiness owner_email_allowed=false; actionable={actionable_count}"


def monthly_run_failed(monthly_run: dict[str, Any]) -> bool:
    return str(monthly_run.get("status") or "").strip().lower() == "failed" or bool(
        str(monthly_run.get("failed_step") or "").strip()
    )


def monthly_run_blocker_summary(monthly_run: dict[str, Any]) -> str:
    failed_step = str(monthly_run.get("failed_step") or "").strip()
    if failed_step:
        return f"monthly close failed ({failed_step})"
    status = str(monthly_run.get("status") or "failed").strip()
    return f"monthly close failed ({status})"


def weekly_review_effectively_safe(weekly_cf: dict[str, Any]) -> bool:
    review_safe = weekly_cf.get("weekly_review_safe_idempotency") if isinstance(weekly_cf.get("weekly_review_safe_idempotency"), dict) else {}
    if weekly_cf.get("weekly_file_update_effective_ok") is True:
        return True
    return (
        review_safe.get("cf_review_gate_snapshot_current") is True
        and count_value(review_safe.get("cf_review_gate_action_queue_count")) == 0
        and review_safe.get("ecogl_source_fix_effectively_clear") is True
        and count_value(review_safe.get("ecogl_exception_count")) == 0
        and count_value(review_safe.get("ecogl_source_fix_action_count")) == 0
        and count_value(weekly_cf.get("conflict_count")) == 0
        and count_value((weekly_cf.get("review_gate") or {}).get("blocker_count")) == 0
        and count_value((weekly_cf.get("review_gate") or {}).get("action_queue_count")) == 0
    )


def summarize_blocked_publish_records(runtime_map: dict[str, Any], limit: int) -> dict[str, Any]:
    records = [
        record
        for record in runtime_map.get("records") or []
        if isinstance(record, dict) and str(record.get("status") or "").startswith("blocked_")
    ]
    status_counts = Counter(str(record.get("status") or "unknown") for record in records)
    samples = []
    for record in records[: max(0, limit)]:
        status = str(record.get("status") or "")
        next_action = "Resolve blocked Lofty PM publish record before live publish/email."
        if status == "blocked_missing_financials_md":
            next_action = (
                "Create or approve canonical Public/00 - README & Property Snapshot/FINANCIALS.md "
                "from current Dropbox/Baselane data; do not use template values."
            )
        samples.append(
            {
                "status": status,
                "property_name": record.get("property_name"),
                "updates_md": record.get("updates_md"),
                "financials_md": record.get("financials_md"),
                "next_action": next_action,
            }
        )
    return {
        "publish_blocked_record_count": len(records),
        "publish_blocked_status_counts": dict(sorted(status_counts.items())),
        "publish_blocked_record_samples": samples,
    }


def compact_reason_parts(values: dict[str, Any], aliases: list[tuple[str, str]]) -> list[str]:
    parts: list[str] = []
    for key, label in aliases:
        count = count_value(values.get(key))
        if count:
            parts.append(f"{label}={count}")
    return parts


def summarize_yhome_operating_cash(
    cf_balance_sheet_consistency: dict[str, Any],
    yhome_apply_verify: dict[str, Any],
    weekly_cf: dict[str, Any],
    report_dir: Path,
) -> dict[str, Any]:
    target_columns = (
        cf_balance_sheet_consistency.get("yhome_target_columns")
        or yhome_apply_verify.get("target_columns")
        or YHOME_OPERATING_CASH_TARGET_COLUMNS
    )
    apply_verify_status = yhome_apply_verify.get("status")
    apply_verify_reason = yhome_apply_verify.get("reason")
    post_update_available = "post_yhome_update_required_count" in yhome_apply_verify
    if apply_verify_status == "ok" and (
        post_update_available
        or apply_verify_reason in {"applied_and_verified", "no_updates_required"}
    ):
        update_required_count = count_value(
            yhome_apply_verify.get("post_yhome_update_required_count")
            if post_update_available
            else yhome_apply_verify.get("pre_yhome_update_required_count")
        )
    else:
        update_required_count = max(
            count_value(weekly_cf.get("cf_balance_sheet_consistency_yhome_update_required_count")),
            count_value(cf_balance_sheet_consistency.get("yhome_update_required_count")),
            count_value(yhome_apply_verify.get("pre_yhome_update_required_count")),
            count_value(yhome_apply_verify.get("update_count")),
        )
    needs_attention = update_required_count > 0 or yhome_apply_verify.get("status") == "review"
    return {
        "status": "review" if needs_attention else "ok",
        "authoritative": False,
        "blocks_downstream": False,
        "blocked": False,
        "work_product_needs_attention": needs_attention,
        "update_required_count": update_required_count,
        "target_columns": target_columns,
        "target_column_policy": "Weekly Yhome Transition Reconciliation updates must only write Lofty Operating Cash and ECO Net DAO Funds.",
        "next_action": (
            f"Run gated Yhome operating cash apply/verify after confirming external sheet write is intended: {YHOME_OPERATING_CASH_APPLY_COMMAND}"
            if needs_attention
            else "No Yhome operating cash update required."
        ),
        "hold": "none",
        "apply_verify_status": apply_verify_status,
        "apply_verify_reason": apply_verify_reason,
        "pre_update_required_count": yhome_apply_verify.get("pre_yhome_update_required_count"),
        "post_update_required_count": yhome_apply_verify.get("post_yhome_update_required_count"),
        "applied_update_count": yhome_apply_verify.get("applied_update_count"),
        "external_write_attempted": yhome_apply_verify.get("external_write_attempted", False),
        "cf_balance_sheet_status": cf_balance_sheet_consistency.get("status"),
        "yhome_csv": cf_balance_sheet_consistency.get("yhome_csv") or yhome_apply_verify.get("yhome_csv"),
        "update_plan_csv": cf_balance_sheet_consistency.get("yhome_update_plan_csv"),
        "consistency_report": str(report_dir / "baselane_cf_balance_sheet_consistency_audit.json"),
        "apply_verify_report": str(report_dir / "yhome_operating_cash_apply_verify_report.json"),
    }


def build_decision(packet: dict[str, Any]) -> dict[str, Any]:
    weekly_cf = packet.get("weekly_cf") or {}
    monthly = packet.get("monthly_review") or {}
    monthly_run = packet.get("monthly_run") if isinstance(packet.get("monthly_run"), dict) else {}
    monthly_comms = packet.get("monthly_comms") or {}
    readiness = packet.get("monthly_readiness") or {}
    send_guard = packet.get("external_send_guard") or {}
    local_model = packet.get("local_model") or {}
    path_guard = packet.get("path_guard") or {}
    discord_context = packet.get("discord_context") or {}
    discord_review = packet.get("discord_review") if isinstance(packet.get("discord_review"), dict) else {}
    ecogl_source_fix = weekly_cf.get("ecogl_source_fix") or {}
    ecogl_safe_apply = weekly_cf.get("ecogl_safe_apply") or {}
    ecogl_autonomy = weekly_cf.get("ecogl_autonomy") or {}
    weekly_cf_gate = weekly_cf.get("review_gate") or {}
    yhome_operating_cash = weekly_cf.get("yhome_operating_cash") if isinstance(weekly_cf.get("yhome_operating_cash"), dict) else {}
    skip_policy = monthly.get("skip_policy") or {}

    source_fix_effectively_clear = ecogl_source_fix.get("source_fix_effectively_clear") is True
    source_fix_queue_count = count_value(ecogl_source_fix.get("source_fix_action_queue_ready_to_apply_count")) + count_value(
        ecogl_source_fix.get("source_fix_action_queue_decision_required_count")
    )
    source_fix_remaining_count = count_value(ecogl_source_fix.get("source_fix_corrections_remaining_count"))
    source_fix_count = 0 if source_fix_effectively_clear else count_value(source_fix_remaining_count or source_fix_queue_count or ecogl_source_fix.get("action_count"))
    source_fix_counts = ecogl_source_fix.get("action_type_counts") if isinstance(ecogl_source_fix.get("action_type_counts"), dict) else {}
    exception_count = 0 if source_fix_effectively_clear else count_value(ecogl_autonomy.get("exception_count"))
    auto_rows = count_value(ecogl_safe_apply.get("safe_action_count"))
    cf_synced_rows = count_value(weekly_cf.get("conflict_auto_apply_approved_applicable_count"))
    weekly_operator_queue_count = (
        source_fix_count
        + count_value(weekly_cf.get("conflict_count"))
        + count_value(weekly_cf_gate.get("blocker_count"))
        + count_value(weekly_cf_gate.get("action_queue_count"))
    )
    skipped_closed_count = count_value(skip_policy.get("owner_review_gate_property_skipped_count"))
    external_excluded_count = count_value(skip_policy.get("owner_review_gate_property_external_excluded_count"))
    total_excluded_count = count_value(skip_policy.get("owner_review_gate_property_excluded_total_count"))
    if not total_excluded_count:
        total_excluded_count = skipped_closed_count + external_excluded_count
    weekly_file_update_status = str(weekly_cf.get("weekly_file_update_status") or "")
    weekly_review_safe = weekly_review_effectively_safe(weekly_cf)
    weekly_cf_needs_operator = (weekly_file_update_status == "review" and not weekly_review_safe) or weekly_operator_queue_count > 0
    source_fix_parts = compact_reason_parts(
        source_fix_counts,
        [
            ("book_or_tag_baselane_accrual", "GL-empty accrual"),
            ("reconcile_formula_to_baselane_accrual_or_tagging", "formula accrual"),
            ("tag_baselane_transaction_category", "untagged category"),
        ],
    )

    gate = "ok"
    blocker = "none"
    primary_artifact = "none"
    next_action = "No manual report review needed; keep scheduled automation running."
    hold = "none"
    if not local_model_ready(local_model):
        gate = "blocked"
        blocker = "local model preflight"
        primary_artifact = "reports/baselane_local_model_preflight_report.json"
        next_action = "Restore ollama-cyber/qwen3.5:35b-a3b deterministic finance contract before autonomous report generation."
        hold = "AI-assisted document generation"
    elif path_guard.get("public_path_guard_status") not in {"ok", None} or count_value(path_guard.get("public_path_guard_issue_count")):
        gate = "blocked"
        blocker = "public path guard"
        primary_artifact = "reports/lofty_public_path_guard_report.json"
        next_action = "Remove legacy owner-statement targets; use Public/07 - P&L & Owner Statements only."
        hold = "monthly public document updates"
    elif path_guard.get("discord_public_financial_source_guard_status") not in {"ok", None} or count_value(path_guard.get("discord_public_financial_source_guard_issue_count")):
        gate = "blocked"
        blocker = "discord-public financial source guard"
        primary_artifact = "reports/discord_public_financial_source_guard_report.json"
        next_action = (
            "Remove legacy ad-hoc ledger CSV regressions and keep discord-public financial reads limited "
            "to Dropbox-sourced FINANCIALS.md/UPDATES.md."
        )
        hold = "investor-facing public financial answers"
    elif monthly_run_failed(monthly_run):
        gate = "blocked"
        blocker = monthly_run_blocker_summary(monthly_run)
        primary_artifact = "reports/baselane_financials_monthly_run_report.json"
        next_action = str(
            monthly_run.get("next_action")
            or "Rerun monthly finance-truth refresh before downstream CF/FINANCIALS/Lofty/Discord/email outputs."
        )
        hold = "Lofty PM publish and investor email"
    elif weekly_cf_needs_operator:
        gate = "blocked"
        blocker = f"ECO GL source quality ({exception_count or source_fix_count} exceptions)"
        primary_artifact = "reports/baselane_ecogl_source_fix_action_queue.md" if source_fix_count else "reports/baselane_ecogl_data_quality_exceptions.csv"
        fix_summary = ", ".join(source_fix_parts) or f"{count_value(weekly_cf.get('untagged_review_required_count'))} untagged rows"
        next_action = f"Use the minimal ECO GL action queue for {fix_summary}; then update Baselane source rows, export, and rerun scripts/baselane_weekly_file_updates_cron.sh."
        hold = "Lofty PM publish and investor email"
    elif readiness.get("status") != "ok":
        actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
        primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
        gate = "blocked"
        blocker = f"monthly readiness ({count_value(readiness.get('blocker_count'))} blockers)"
        primary_artifact = str(primary.get("artifact") or "reports/baselane_financials_monthly_readiness.json")
        primary_class = str(primary.get("class") or primary.get("blocker") or "")
        next_action = (
            hemlane_rent_roll_next_action(monthly_comms, Path(str(monthly_comms.get("comms_root") or ".")))
            if primary_class.startswith("monthly_comms.rent_roll")
            else str(primary.get("next_action") or "Resolve the monthly readiness primary blocker, then rerun the guarded monthly dry-run before publish/email.")
        )
        hold = "Lofty PM publish and investor email"
    elif monthly.get("owner_review_gate_status") == "review":
        gate = "blocked"
        blocker = f"monthly owner guards ({count_value(monthly.get('owner_review_gate_blocker_count'))} blockers)"
        primary_artifact = "reports/baselane_monthly_owner_review_gate.csv"
        next_action = "Rerun guarded UPDATES.md/FINANCIALS.md generation and live-capture checks for active properties only."
        hold = "Lofty PM publish and investor email"
    elif count_value(monthly_comms.get("rent_roll_pending_gap_count")) or count_value(monthly_comms.get("rent_roll_pending_stale_export_date_count")):
        gate = "blocked"
        blocker = "rent-roll freshness"
        primary_artifact = str(monthly_comms.get("rent_roll_gap_queue_csv") or "workspace-lofty-vp/updates/rent-roll-gap-review.csv")
        next_action = hemlane_rent_roll_next_action(monthly_comms, Path(str(monthly_comms.get("comms_root") or ".")))
        hold = "investor email"
    elif send_guard.get("owner_email_allowed") is not True:
        gate = "blocked"
        blocker = "owner email guard"
        primary_artifact = "reports/baselane_financials_monthly_lofty_pm_publish.json"
        next_action = "Keep owner email disabled until readiness, guarded apply, and send evidence are all clean."
        hold = "investor email"

    return {
        "gate": gate,
        "blocker": blocker,
        "primary_artifact": primary_artifact,
        "next_action": next_action,
        "hold": hold,
        "autonomous_ok": gate == "ok",
        "safe_auto_actions": {
            "ecogl_category_rows_applied": auto_rows,
            "cf_workbook_rows_synced_from_gl": cf_synced_rows,
            "sold_delisted_closed_excluded": skipped_closed_count,
            "external_manual_excluded": external_excluded_count,
            "total_excluded_from_publish_email": total_excluded_count,
        },
        "publish_allowed": gate == "ok" and send_guard.get("owner_email_allowed") is True,
        "owner_email_allowed": gate == "ok" and send_guard.get("owner_email_allowed") is True,
        "owner_email_max_once_monthly": True,
        "active_property_only_policy": "Sold, delisted, closed, skipped_* index rows, and manual/external exclusions are excluded from Lofty PM live publish and owner email.",
        "discord_context_checked": discord_context.get("fresh") is True or discord_review.get("plan_validation_ok") is True,
        "discord_review_plan_validated": discord_review.get("plan_validation_ok") is True,
        "discord_review_dry_run_current": discord_review.get("dry_run_current_for_run") is True,
        "discord_review_send_proof_current": discord_review.get("owner_email_live_post_ok") is True,
        "local_model_validated": local_model_ready(local_model),
        "weekly_review_effectively_safe": weekly_review_safe,
        "canonical_paths": packet.get("canonical_paths") or {},
    }


def build_packet(root: Path, sample_limit: int) -> dict[str, Any]:
    report_dir = root / "reports"
    readiness = read_json(report_dir / "baselane_financials_monthly_readiness.json")
    readiness_skip_policy = readiness.get("monthly_skip_policy") if isinstance(readiness.get("monthly_skip_policy"), dict) else {}
    review_manifest = read_json(report_dir / "baselane_financials_monthly_review_manifest.json")
    candidate_packet = read_json(report_dir / "baselane_financials_monthly_review_candidate_packet.json")
    owner_review_gate = read_json(report_dir / "baselane_monthly_owner_review_gate.json")
    safety_scan = read_json(report_dir / "baselane_financials_monthly_review_safety_scan.json")
    safe_approval = read_json(report_dir / "baselane_financials_monthly_safe_candidate_approval.json")
    live_update_capture = read_json(report_dir / "baselane_financials_monthly_live_update_capture.json")
    live_financial_capture = read_json(report_dir / "baselane_financials_monthly_live_financial_capture.json")
    weekly_cf = read_json(report_dir / "baselane_weekly_cf_statement_sync_report.json")
    weekly_files = read_json(report_dir / "baselane_weekly_file_updates_run_report.json")
    weekly_review_safe_idempotency = weekly_files.get("review_safe_idempotency") if isinstance(weekly_files.get("review_safe_idempotency"), dict) else {}
    weekly_review_safe_idempotency = rebase_workspace_artifacts(root, weekly_review_safe_idempotency)
    cf_balance_sheet_consistency = read_json(report_dir / "baselane_cf_balance_sheet_consistency_audit.json")
    yhome_apply_verify = read_json(report_dir / "yhome_operating_cash_apply_verify_report.json")
    yhome_operating_cash = summarize_yhome_operating_cash(cf_balance_sheet_consistency, yhome_apply_verify, weekly_cf, report_dir)
    cf_plan = read_json(report_dir / "baselane_cf_conflict_resolution_plan.json")
    untagged_packet = read_json(report_dir / "baselane_cf_untagged_review_packet.json")
    untagged_rule_candidates = read_json(report_dir / "baselane_cf_untagged_rule_candidates.json")
    ecogl_autonomy = read_json(report_dir / "baselane_ecogl_data_quality_autonomy.json")
    ecogl_source_fix = read_json(report_dir / "baselane_ecogl_source_fix_plan.json")
    ecogl_source_fix_evidence = read_json(report_dir / "baselane_ecogl_source_fix_evidence.json")
    ecogl_source_fix_verifier = read_json(report_dir / "baselane_ecogl_source_fix_verifier.json")
    ecogl_source_fix_corrections = read_json(report_dir / "baselane_ecogl_source_fix_corrections.json")
    ecogl_source_fix_approval = read_json(report_dir / "baselane_ecogl_source_fix_approval.json")
    ecogl_source_fix_correction_validation = read_json(report_dir / "baselane_ecogl_source_fix_correction_validation.json")
    ecogl_source_fix_action_queue = read_json(report_dir / "baselane_ecogl_source_fix_action_queue.json")
    ecogl_safe_apply = read_json(report_dir / "baselane_ecogl_safe_category_apply_report.json")
    weekly_cf_gate = read_json(report_dir / "baselane_weekly_cf_review_gate.json")
    cdp_preflight = read_json(report_dir / "lofty_cdp_preflight_report.json")
    public_path_guard = read_json(report_dir / "lofty_public_path_guard_report.json")
    discord_public_guard = read_json(report_dir / "discord_public_financial_source_guard_report.json")
    local_model = read_json(report_dir / "baselane_local_model_preflight_report.json")
    local_model_direct_smoke = local_model.get("direct_smoke") if isinstance(local_model.get("direct_smoke"), dict) else {}
    local_model_finance_smoke = local_model.get("finance_contract_smoke") if isinstance(local_model.get("finance_contract_smoke"), dict) else {}
    local_model_generated_at = local_model.get("generated_at")
    local_model_report_age_hours = iso_age_hours(local_model_generated_at)
    publish = read_json(report_dir / "baselane_financials_monthly_lofty_pm_publish.json")
    owner_email_send_guard = read_json(report_dir / "baselane_monthly_owner_email_send_guard.json")
    publish_runtime_map = read_json(report_dir / "baselane_financials_monthly_lofty_pm_runtime_map.json")
    publish_blocked_summary = summarize_blocked_publish_records(publish_runtime_map, sample_limit)
    owner_email_send_decision = publish.get("owner_email_send_decision") if isinstance(publish.get("owner_email_send_decision"), dict) else {}
    owner_email_idempotency = publish.get("owner_email_idempotency") if isinstance(publish.get("owner_email_idempotency"), dict) else {}
    monthly_readiness_snapshot = publish.get("monthly_readiness_snapshot") if isinstance(publish.get("monthly_readiness_snapshot"), dict) else {}
    readiness_snapshot_current = monthly_readiness_snapshot_current(monthly_readiness_snapshot, readiness)
    readiness_snapshot_for_packet = refreshed_monthly_readiness_snapshot(monthly_readiness_snapshot, readiness)
    current_readiness_blocked_reason = (
        monthly_readiness_blocked_reason(readiness)
        if readiness.get("owner_email_allowed") is not True
        else ""
    )
    publish_blocked_reason = owner_email_send_decision.get("blocked_reason") or publish.get("send_blocked_reason") or ""
    send_decision_digest = publish.get("send_decision_digest")
    owner_email_idempotency_send_decision_digest = owner_email_idempotency.get("send_decision_digest")
    owner_email_send_decision_digest = owner_email_send_decision.get("send_decision_digest")
    send_decision_digest_consistent = (
        bool(send_decision_digest)
        and send_decision_digest == owner_email_idempotency_send_decision_digest
        and send_decision_digest == owner_email_send_decision_digest
    )
    existing_send_lock_decision_digest = (
        publish.get("existing_send_lock_decision_digest")
        if "existing_send_lock_decision_digest" in publish
        else owner_email_idempotency.get("existing_send_lock_decision_digest")
    )
    existing_send_lock_matches_send_decision = (
        publish.get("existing_send_lock_matches_send_decision")
        if "existing_send_lock_matches_send_decision" in publish
        else owner_email_idempotency.get("existing_send_lock_matches_send_decision")
    )
    owner_email_discord_review_chain = (
        owner_email_send_guard.get("owner_email_discord_review_chain")
        if isinstance(owner_email_send_guard.get("owner_email_discord_review_chain"), dict)
        else {}
    )
    discord_context = read_json(report_dir / "discord_project_baselane_financials_context.latest.json")
    discord_checked_age_hours = iso_age_hours(discord_context.get("checked_at"))
    discord_context_hits = discord_context.get("context_hits") if isinstance(discord_context.get("context_hits"), list) else []
    discord_all_send_plan_validation = read_json(report_dir / "baselane_financials_monthly_discord_all_send_plan_validation.json")
    discord_property_update_send = read_json(report_dir / "baselane_financials_monthly_discord_property_update_send.json")
    discord_all_send_report = read_json(report_dir / "baselane_financials_monthly_discord_all_send_report.json")
    monthly_run = read_json(report_dir / "baselane_financials_monthly_run_report.json")
    monthly_close_status = read_json(report_dir / "baselane_financials_monthly_close_status.json")
    run_month = str(monthly_run.get("run_month") or datetime.now(timezone.utc).strftime("%Y-%m"))
    comms_root = Path(os.environ.get("COMMS_WORKSPACE") or root.parent / "workspace-lofty-vp")
    if not comms_root.is_dir():
        comms_root = root.parent / "workspace-lofty-vp-comms"
    comms_updates = comms_root / "updates"
    rent_roll_gap_review = read_json(comms_updates / f"{run_month}-rent-roll-gap-review.json")
    rent_roll_approval_coverage = rent_roll_gap_review.get("approval_template_coverage") if isinstance(rent_roll_gap_review.get("approval_template_coverage"), dict) else {}
    rent_roll_source = rent_roll_gap_review.get("rent_roll_source") if isinstance(rent_roll_gap_review.get("rent_roll_source"), dict) else {}
    if not rent_roll_source and isinstance(rent_roll_gap_review.get("source"), dict):
        rent_roll_source = rent_roll_gap_review.get("source") or {}
    hemlane_cdp_preflight = read_json(report_dir / "hemlane_cdp_preflight_report.json")
    hemlane_cdp_capture_path = comms_updates / f"{run_month}-hemlane-cdp-capture-report.json"
    hemlane_cdp_capture = read_json(hemlane_cdp_capture_path)
    hemlane_capture_bitwarden_login = (
        hemlane_cdp_capture.get("bitwarden_login")
        if isinstance(hemlane_cdp_capture.get("bitwarden_login"), dict)
        else rent_roll_source.get("hemlane_capture_bitwarden_login")
        if isinstance(rent_roll_source.get("hemlane_capture_bitwarden_login"), dict)
        else {}
    )
    hemlane_capture_attempts = (
        hemlane_cdp_capture.get("login_recovery_attempts")
        if isinstance(hemlane_cdp_capture.get("login_recovery_attempts"), list)
        else []
    )
    hemlane_preflight_attempts = (
        hemlane_cdp_preflight.get("login_recovery_attempts")
        if isinstance(hemlane_cdp_preflight.get("login_recovery_attempts"), list)
        else []
    )
    hemlane_recovery_attempt_count = count_value(
        first_present(
            hemlane_cdp_capture.get("login_recovery_try_count"),
            hemlane_cdp_capture.get("login_recovery_attempt_count"),
            rent_roll_source.get("hemlane_login_recovery_try_count"),
            rent_roll_source.get("hemlane_login_recovery_attempt_count"),
            len(hemlane_preflight_attempts),
        )
    )
    monthly_comms_context = {
        "run_month": run_month,
        "comms_root": str(comms_root),
        "rent_roll_gap_review_status": rent_roll_gap_review.get("status"),
        "rent_roll_gap_count": rent_roll_gap_review.get("gap_count"),
        "rent_roll_deferred_gap_count": rent_roll_gap_review.get("deferred_gap_count"),
        "rent_roll_source_blocker_count": rent_roll_gap_review.get("source_blocker_count"),
        "rent_roll_pending_gap_count": rent_roll_gap_review.get("pending_gap_count"),
        "rent_roll_stale_export_dates": rent_roll_gap_review.get("stale_export_dates"),
        "rent_roll_pending_stale_export_date_count": rent_roll_gap_review.get("pending_stale_export_date_count"),
        "rent_roll_gap_queue_digest": rent_roll_gap_review.get("action_queue_digest"),
        "rent_roll_gap_approval_template_coverage_status": rent_roll_approval_coverage.get("status"),
        "rent_roll_gap_approval_template_digest": rent_roll_gap_review.get("approval_template_digest") or rent_roll_approval_coverage.get("digest"),
        "rent_roll_source": rent_roll_source,
        "rent_roll_freshness_status": rent_roll_source.get("freshness_status"),
        "rent_roll_latest_exported_on": rent_roll_source.get("latest_exported_on"),
        "rent_roll_latest_local_file": rent_roll_source.get("latest_local_rent_roll_file"),
        "rent_roll_monthly_dry_run_command": rent_roll_gap_review.get("monthly_dry_run_command"),
        "hemlane_cdp_preflight_status": hemlane_cdp_preflight.get("status"),
        "hemlane_cdp_preflight_issue_summary": hemlane_cdp_preflight.get("issue_summary"),
        "hemlane_cdp_available": hemlane_cdp_preflight.get("cdp_available"),
        "hemlane_logged_in_tab_count": hemlane_cdp_preflight.get("logged_in_tab_count"),
        "hemlane_login_tab_count": hemlane_cdp_preflight.get("login_tab_count"),
        "hemlane_rent_roll_tab_count": hemlane_cdp_preflight.get("rent_roll_tab_count"),
        "hemlane_login_recovery_opened_rent_roll": hemlane_cdp_preflight.get("login_recovery_opened_rent_roll"),
        "hemlane_login_recovery_attempt_count": first_present(
            hemlane_cdp_capture.get("login_recovery_attempt_count"),
            len(hemlane_capture_attempts) if hemlane_capture_attempts else None,
            rent_roll_source.get("hemlane_login_recovery_attempt_count"),
            len(hemlane_preflight_attempts) if hemlane_preflight_attempts else None,
        ),
        "hemlane_login_recovery_try_count": hemlane_recovery_attempt_count,
        "hemlane_login_recovery_exhausted": first_present(hemlane_cdp_capture.get("login_recovery_exhausted"), rent_roll_source.get("hemlane_login_recovery_exhausted")),
        "hemlane_automated_browser_recovery_complete": first_present(
            hemlane_cdp_capture.get("automated_browser_recovery_complete"),
            rent_roll_source.get("hemlane_automated_browser_recovery_complete"),
        ),
        "hemlane_next_action": normalized_hemlane_preflight_action(hemlane_cdp_preflight, hemlane_recovery_attempt_count),
        "hemlane_cdp_capture_report": str(hemlane_cdp_capture_path),
        "hemlane_capture_status": first_present(hemlane_cdp_capture.get("status"), rent_roll_source.get("hemlane_capture_status")),
        "hemlane_capture_generated_at": first_present(hemlane_cdp_capture.get("generated_at"), rent_roll_source.get("hemlane_capture_generated_at")),
        "hemlane_capture_issue": first_present(hemlane_cdp_capture.get("issue"), rent_roll_source.get("hemlane_capture_issue")),
        "hemlane_capture_next_action": first_present(hemlane_cdp_capture.get("next_action"), rent_roll_source.get("hemlane_capture_next_action")),
        "hemlane_manual_auth_required": first_present(hemlane_cdp_capture.get("manual_auth_required"), rent_roll_source.get("hemlane_manual_auth_required")),
        "hemlane_manual_auth_reason": first_present(hemlane_cdp_capture.get("manual_auth_reason"), rent_roll_source.get("hemlane_manual_auth_reason")),
        "hemlane_manual_auth_phase": first_present(hemlane_cdp_capture.get("manual_auth_phase"), rent_roll_source.get("hemlane_manual_auth_phase")),
        "hemlane_manual_auth_blocker": first_present(hemlane_cdp_capture.get("manual_auth_blocker"), rent_roll_source.get("hemlane_manual_auth_blocker")),
        "hemlane_capture_bitwarden_login": hemlane_capture_bitwarden_login,
        "hemlane_capture_bitwarden_login_attempted": first_present(
            hemlane_cdp_capture.get("bitwarden_login_attempted"),
            rent_roll_source.get("hemlane_capture_bitwarden_login_attempted"),
        ),
        "hemlane_capture_bitwarden_login_status": first_present(
            hemlane_cdp_capture.get("bitwarden_login_status"),
            rent_roll_source.get("hemlane_capture_bitwarden_login_status"),
        ),
        "hemlane_capture_bitwarden_login_submit_ok": first_present(
            hemlane_cdp_capture.get("bitwarden_login_submit_ok"),
            rent_roll_source.get("hemlane_capture_bitwarden_login_submit_ok"),
        ),
        "hemlane_bitwarden_login_recaptcha_error": first_present(
            hemlane_cdp_capture.get("bitwarden_login_recaptcha_error"),
            rent_roll_source.get("hemlane_bitwarden_login_recaptcha_error"),
        ),
        "rent_roll_gap_review": str(comms_updates / f"{run_month}-rent-roll-gap-review.md"),
        "rent_roll_gap_queue_csv": rent_roll_gap_review.get("queue_csv") or str(comms_updates / f"{run_month}-rent-roll-gap-review.csv"),
        "rent_roll_gap_approval": rent_roll_gap_review.get("approval_path") or str(comms_updates / f"{run_month}-rent-roll-gap-approval.json"),
    }
    weekly_cf_gate_action_queue_count = weekly_cf_gate.get("action_queue_count") or (weekly_cf_gate.get("summary") or {}).get("action_queue_count")
    weekly_report_cf_gate_action_queue_count = weekly_review_safe_idempotency.get("cf_review_gate_action_queue_count")
    weekly_cf_gate_snapshot_current = (
        weekly_review_safe_idempotency.get("cf_review_gate_action_queue_digest") == weekly_cf_gate.get("action_queue_digest")
        and weekly_review_safe_idempotency.get("cf_review_gate_idempotency_key") == weekly_cf_gate.get("idempotency_key")
        and int(weekly_report_cf_gate_action_queue_count or 0) == int(weekly_cf_gate_action_queue_count or 0)
    )
    source_fix_remaining_raw = ecogl_source_fix_corrections.get("remaining_count")
    source_fix_remaining_count = int(
        source_fix_remaining_raw
        if source_fix_remaining_raw is not None
        else ecogl_source_fix.get("action_count") or 0
    )
    source_fix_validation_pending_count = int(ecogl_source_fix_correction_validation.get("pending_count") or 0)
    source_fix_validation_invalid_count = int(ecogl_source_fix_correction_validation.get("invalid_count") or 0)
    source_fix_validation_ready_count = int(ecogl_source_fix_correction_validation.get("ready_count") or 0)
    source_fix_action_queue_ready_count = int(ecogl_source_fix_action_queue.get("ready_to_apply_count") or 0)
    source_fix_action_queue_decision_required_count = int(ecogl_source_fix_action_queue.get("decision_required_count") or 0)
    source_fix_effectively_clear = bool(weekly_review_safe_idempotency.get("ecogl_source_fix_effectively_clear")) or (
        source_fix_remaining_count == 0
        and source_fix_action_queue_ready_count == 0
        and source_fix_action_queue_decision_required_count == 0
        and ecogl_source_fix_verifier.get("status") == "ok"
        and int(ecogl_source_fix_verifier.get("remaining_count") or 0) == 0
    )
    if source_fix_effectively_clear:
        source_fix_remaining_count = 0
    weekly_files_review_effectively_safe = weekly_review_effectively_safe(
        {
            "weekly_file_update_effective_ok": weekly_files.get("effective_ok"),
            "weekly_review_safe_idempotency": weekly_review_safe_idempotency,
            "conflict_count": weekly_cf.get("conflict_count"),
            "review_gate": {
                "blocker_count": weekly_cf_gate.get("blocker_count"),
                "action_queue_count": weekly_cf_gate_action_queue_count,
            },
            "yhome_operating_cash": yhome_operating_cash,
        }
    )

    owner_review_summary = owner_review_gate.get("summary") if isinstance(owner_review_gate.get("summary"), dict) else {}
    effective_pending_update_review_count = count_value(
        owner_review_summary.get("pending_update_review_count")
        if "pending_update_review_count" in owner_review_summary
        else review_manifest.get("pending_update_review_count")
    )
    effective_pending_financial_review_count = count_value(
        owner_review_summary.get("pending_financial_review_count")
        if "pending_financial_review_count" in owner_review_summary
        else review_manifest.get("pending_financial_review_count")
    )
    owner_review_needs_approval_work = any(
        int(owner_review_summary.get(key) or 0)
        for key in (
            "pending_update_review_count",
            "pending_financial_review_count",
            "candidate_issue_count",
            "candidate_marker_count",
            "safety_high_count",
            "safety_medium_count",
            "safety_missing_count",
        )
    )

    action_items: list[str] = []
    readiness_actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
    readiness_primary = readiness_actionable.get("primary_blocker") if isinstance(readiness_actionable.get("primary_blocker"), dict) else {}
    readiness_primary_class = str(readiness_primary.get("class") or readiness_primary.get("blocker") or "")
    readiness_primary_holds_downstream = readiness_primary_class.startswith("monthly_comms.rent_roll")

    def add_action(item: str) -> None:
        if item not in action_items:
            action_items.append(item)

    monthly_run_is_failed = monthly_run_failed(monthly_run)
    monthly_run_next_action = str(
        monthly_run.get("next_action")
        or "Rerun monthly finance-truth refresh before downstream CF/FINANCIALS/Lofty/Discord/email outputs."
    )
    if monthly_run_is_failed:
        add_action(monthly_run_next_action)
        add_action("Do not work downstream Lofty live publish, Discord posting, or owner email until the current monthly run is clean.")
    elif readiness_primary_holds_downstream:
        add_action(hemlane_rent_roll_next_action(monthly_comms_context, comms_root))
        if not status_is_ok(cdp_preflight.get("status")):
            add_action(lofty_pm_next_action(cdp_preflight))
    elif readiness_primary_class.startswith("lofty_cdp_preflight"):
        add_action(lofty_pm_next_action(cdp_preflight))
    elif readiness_primary_class.startswith(("live_update_capture", "live_financial_capture", "guard.updates", "guard.financials")):
        add_action("Capture and verify Lofty PM live UPDATES.md/FINANCIALS.md guards for every target before publish/email.")
    if weekly_files.get("status") == "review" and not weekly_files_review_effectively_safe and not monthly_run_is_failed:
        add_action("Resolve weekly CF/file review before treating owner financials as final.")
    if effective_pending_update_review_count:
        add_action("Work reports/baselane_monthly_owner_review_gate.csv for owner update approvals, then write approved artifacts to listed targets.")
    if effective_pending_financial_review_count:
        add_action("Work reports/baselane_monthly_owner_review_gate.csv for FINANCIALS approvals, then write approved artifacts to listed targets.")
    if owner_review_gate.get("status") == "review" and owner_review_needs_approval_work:
        add_action("Use reports/baselane_monthly_owner_review_gate.csv as the ranked monthly approval/guard queue; use the .md for detail.")
    if (
        not monthly_run_is_failed
        and
        not readiness_primary_holds_downstream
        and (
            live_update_capture.get("status") != "ok"
            or int(live_update_capture.get("check_ok_count") or 0) < int(live_update_capture.get("target_count") or 0)
            or live_financial_capture.get("status") != "ok"
            or int(live_financial_capture.get("check_ok_count") or 0) < int(live_financial_capture.get("target_count") or 0)
        )
    ):
        add_action("Capture and verify Lofty PM live UPDATES.md/FINANCIALS.md guards for every target before publish/email.")
    if not monthly_run_is_failed and not readiness_primary_holds_downstream and not status_is_ok(cdp_preflight.get("status")):
        add_action(lofty_pm_next_action(cdp_preflight))
    if not monthly_run_is_failed and not readiness_primary_holds_downstream and readiness.get("owner_email_allowed") is not True:
        add_action("Keep owner email disabled until monthly readiness is ok and send evidence is clean.")
    if readiness_primary_holds_downstream:
        add_action("Downstream Lofty live guards, publish, and owner email remain held until rent-roll evidence is current or approved.")
    if publish_blocked_summary["publish_blocked_record_count"]:
        add_action(
            "Resolve blocked Lofty PM publish records from reports/baselane_financials_monthly_lofty_pm_runtime_map.json before live publish/email; missing FINANCIALS.md requires current canonical owner-statement data, not templates."
        )
    if int(weekly_cf.get("conflict_resolution_applicable_count") or 0):
        add_action("Work reports/baselane_weekly_cf_review_gate.csv conflict rows before applying workbook mutations.")
    if int(ecogl_safe_apply.get("safe_action_count") or 0):
        add_action("Deterministic ECO GL category fixes are applied to reports/baselane_weekly_clean_reporting_ledger.csv before CF sync.")
    elif not source_fix_effectively_clear and int(ecogl_autonomy.get("safe_auto_untagged_row_count") or 0):
        add_action("Apply deterministic ECO GL accrual category fixes from reports/baselane_ecogl_auto_safe_actions.csv; do not route those rows to human review.")
    if not source_fix_effectively_clear and int(ecogl_source_fix.get("action_count") or 0):
        add_action("Use reports/baselane_ecogl_source_fix_action_queue.md as the minimal ECO GL queue, export again, then rerun weekly cron.")
    elif not source_fix_effectively_clear and int(ecogl_autonomy.get("untagged_exception_row_count") or weekly_cf.get("untagged_review_required_count") or 0):
        add_action("Handle ECO GL exception rows from reports/baselane_ecogl_data_quality_exceptions.csv before relying on CF statement sync as complete.")
    if weekly_cf_gate.get("status") == "review":
        add_action("Use reports/baselane_weekly_cf_review_gate.csv as the single ranked CF queue; use the .md for detail.")
    if (
        not monthly_run_is_failed
        and
        not readiness_primary_holds_downstream
        and (
            int(rent_roll_gap_review.get("pending_gap_count") or 0)
            or int(rent_roll_gap_review.get("pending_stale_export_date_count") or 0)
        )
    ):
        add_action("Work the rent-roll queue CSV: download current rent roll, fix matches, or approve each gap/stale export before owner email.")
    if rent_roll_approval_coverage and rent_roll_approval_coverage.get("status") != "ok":
        add_action("Regenerate the rent-roll gap review so the approval template covers every current gap/stale-export row.")
    if source_fix_remaining_count and not monthly_run_is_failed:
        correction_action = (
            "Use reports/baselane_ecogl_source_fix_action_queue.md: apply current-ID ready rows only with explicit source-mutation env, resolve remaining evidence-backed category decisions, export again, then rerun weekly cron."
            if source_fix_action_queue_ready_count
            or source_fix_action_queue_decision_required_count
            or source_fix_validation_pending_count
            or source_fix_validation_invalid_count
            else "Use reports/baselane_ecogl_source_fix_approved_corrections.csv to set exact Baselane source categories, export again, then rerun weekly cron."
        )
        action_items = [
            correction_action,
            "Do not work monthly review, Lofty PM live publish, or owner-email queues until the weekly source-quality gate is clean.",
        ]
    if monthly_run_is_failed:
        action_items = [
            monthly_run_next_action,
            "Do not work downstream Lofty live publish, Discord posting, or owner email until the current monthly run is clean.",
        ]

    packet = {
        "generated_at": iso_z(),
        "status": "ok",
        "root": str(root),
        "canonical_paths": {
            "updates": "Public/00 - README & Property Snapshot",
            "financials": "Public/07 - P&L & Owner Statements",
        },
        "discord_context": {
            "status": "ok" if discord_context.get("ok") is True else discord_context.get("status"),
            "channel_id": discord_context.get("channel_id"),
            "checked_at": discord_context.get("checked_at"),
            "checked_age_hours": discord_checked_age_hours,
            "fresh": discord_checked_age_hours is not None and discord_checked_age_hours <= 24,
            "message_count": discord_context.get("message_count"),
            "context_hit_count": len(discord_context_hits),
            "context_hit_keywords": sorted({str(hit.get("keyword") or hit.get("term") or "").strip() for hit in discord_context_hits if isinstance(hit, dict) and str(hit.get("keyword") or hit.get("term") or "").strip()}),
            "artifact": str(report_dir / "discord_project_baselane_financials_context.latest.json"),
        },
        "discord_review": {
            "plan_validation_status": discord_all_send_plan_validation.get("status"),
            "plan_validation_ok": (
                discord_all_send_plan_validation.get("status") == "ok"
                and count_value(discord_all_send_plan_validation.get("issue_count")) == 0
                and count_value(discord_all_send_plan_validation.get("unmapped_count")) == 0
                and count_value(discord_all_send_plan_validation.get("stale_route_count")) == 0
                and count_value(discord_all_send_plan_validation.get("missing_financial_summary_count")) == 0
            ),
            "discord_review_ready": discord_all_send_plan_validation.get("discord_review_ready"),
            "discord_review_ready_but_financial_blocked": discord_all_send_plan_validation.get("discord_review_ready_but_financial_blocked"),
            "eligible_discord_send_ready": discord_all_send_plan_validation.get("eligible_discord_send_ready"),
            "record_count": discord_all_send_plan_validation.get("record_count"),
            "unmapped_count": discord_all_send_plan_validation.get("unmapped_count"),
            "stale_route_count": discord_all_send_plan_validation.get("stale_route_count"),
            "missing_financial_summary_count": discord_all_send_plan_validation.get("missing_financial_summary_count"),
            "financial_review_issue_count": discord_all_send_plan_validation.get("financial_review_issue_count"),
            "plan_validation_generated_at": discord_all_send_plan_validation.get("generated_at"),
            "property_update_send_status": discord_property_update_send.get("status"),
            "property_update_send_mode": discord_property_update_send.get("send_mode"),
            "property_update_generated_at": discord_property_update_send.get("generated_at"),
            "dry_run_current_for_run": (
                monthly_close_status.get("discord_property_update_current_for_run") is True
                and str(discord_property_update_send.get("status") or "") == "ok_dry_run"
            ),
            "send_report_status": discord_all_send_report.get("status"),
            "send_report_run_month": discord_all_send_report.get("run_month"),
            "send_report_record_count": discord_all_send_report.get("record_count"),
            "send_report_sent_count": discord_all_send_report.get("sent_count"),
            "send_report_generated_at": discord_all_send_report.get("generated_at"),
            "send_proof_current_for_run": monthly_close_status.get("discord_property_update_current_for_run") is True,
            "owner_email_chain_status": owner_email_discord_review_chain.get("status"),
            "owner_email_dry_run_verified": owner_email_discord_review_chain.get("discord_all_property_dry_run_verified"),
            "owner_email_live_post_ok": owner_email_discord_review_chain.get("discord_all_property_live_post_ok"),
            "owner_email_discord_review_policy": owner_email_discord_review_chain.get("policy"),
            "artifact_plan_validation": str(report_dir / "baselane_financials_monthly_discord_all_send_plan_validation.json"),
            "artifact_send": str(report_dir / "baselane_financials_monthly_discord_property_update_send.json"),
            "artifact_all_send": str(report_dir / "baselane_financials_monthly_discord_all_send_report.json"),
        },
        "local_model": {
            "status": local_model.get("status"),
            "model": local_model.get("model"),
            "provider": local_model.get("provider"),
            "model_id": local_model.get("model_id"),
            "base_url": local_model.get("base_url"),
            "issue_count": local_model.get("issue_count"),
            "generated_at": local_model_generated_at,
            "report_age_hours": local_model_report_age_hours,
            "max_age_hours": LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS,
            "configured_model_present": local_model.get("configured_model_present"),
            "selected_endpoint_from_config": local_model.get("selected_endpoint_from_config"),
            "model_available": local_model.get("model_available"),
            "small_model_execution_allowed": local_model.get("small_model_execution_allowed"),
            "small_model_pipeline_execution_allowed": local_model.get("small_model_pipeline_execution_allowed"),
            "small_model_task_scoped_execution_allowed": local_model.get("small_model_task_scoped_execution_allowed"),
            "small_model_execution_decision": local_model.get("small_model_execution_decision"),
            "small_model_execution_policy": local_model.get("small_model_execution_policy"),
            "direct_smoke_attempted": local_model_direct_smoke.get("attempted"),
            "direct_smoke_ok": local_model_direct_smoke.get("ok"),
            "direct_smoke_response": local_model_direct_smoke.get("response"),
            "finance_contract_expected_response": local_model.get("finance_contract_expected_response"),
            "finance_contract_smoke_attempted": local_model_finance_smoke.get("attempted"),
            "finance_contract_smoke_ok": local_model_finance_smoke.get("ok"),
            "finance_contract_smoke_response": local_model_finance_smoke.get("response"),
            "model_execution_scope": local_model.get("model_execution_scope"),
            "validation_contract": local_model.get("validation_contract"),
            "validation_digest": local_model.get("validation_digest"),
        },
        "path_guard": {
            "public_path_guard_status": public_path_guard.get("status"),
            "public_path_guard_issue_count": public_path_guard.get("issue_count"),
            "discord_public_financial_source_guard_status": discord_public_guard.get("status"),
            "discord_public_financial_source_guard_issue_count": discord_public_guard.get("issue_count"),
            "discord_public_financial_source_guard_deleted_gl_rows_count": discord_public_guard.get("deleted_gl_rows_count"),
            "discord_public_financial_source_guard_financial_doc_count": discord_public_guard.get("financial_doc_count"),
            "discord_public_financial_source_guard_update_doc_count": discord_public_guard.get("update_doc_count"),
            "canonical_financials_folder": public_path_guard.get("canonical_financials_folder"),
            "canonical_updates_folder": public_path_guard.get("canonical_updates_folder"),
        },
        "monthly_run": {
            "status": monthly_run.get("status"),
            "run_month": monthly_run.get("run_month"),
            "failed_step": monthly_run.get("failed_step"),
            "next_action": monthly_run.get("next_action"),
            "primary_blocker": monthly_run.get("primary_blocker"),
            "report": str(report_dir / "baselane_financials_monthly_run_report.json"),
        },
        "monthly_close_status": {
            "status": monthly_close_status.get("status"),
            "failed_step": monthly_close_status.get("failed_step"),
            "monthly_completion_gap_count": monthly_close_status.get("monthly_completion_gap_count"),
            "monthly_blocker_command_index_count": monthly_close_status.get("monthly_blocker_command_index_count"),
            "monthly_blocker_ready_manual_count": monthly_close_status.get("monthly_blocker_ready_manual_count"),
            "monthly_blocker_safe_auto_count": monthly_close_status.get("monthly_blocker_safe_auto_count"),
            "monthly_blocker_ready_manual_top": monthly_close_status.get("monthly_blocker_ready_manual_top") or [],
            "monthly_blocker_command_index_markdown": monthly_close_status.get("monthly_blocker_command_index_markdown"),
            "report": str(report_dir / "baselane_financials_monthly_close_status.json"),
            "markdown": str(report_dir / "baselane_financials_monthly_close_status.md"),
        },
        "monthly_candidate_coverage": {
            "status": monthly_close_status.get("pipeline_candidate_coverage_status"),
            "generated_at": monthly_close_status.get("pipeline_candidate_coverage_generated_at"),
            "mismatch_count": monthly_close_status.get("pipeline_candidate_coverage_mismatch_count"),
            "mismatches": monthly_close_status.get("pipeline_candidate_coverage_mismatches") or [],
            "input_digests": monthly_close_status.get("pipeline_candidate_coverage_input_digests") or {},
            "transfer_reconciliation": monthly_close_status.get("pipeline_candidate_coverage_transfer_reconciliation") or {},
            "telegram_reconciliation": monthly_close_status.get("pipeline_candidate_coverage_telegram_reconciliation") or {},
            "report": str(report_dir / "baselane_monthly_pipeline_candidate_coverage_audit.json"),
        },
        "monthly_readiness": {
            "status": readiness.get("status"),
            "blocker_count": readiness.get("blocker_count"),
            "actionable_summary": readiness.get("actionable_summary") or {},
            "data_quality_gate": readiness.get("data_quality_gate") or {},
            "operational_blocker_count": readiness.get("operational_blocker_count"),
            "blocked_property_count": readiness.get("blocked_property_count"),
            "owner_email_allowed": readiness.get("owner_email_allowed"),
            "counts": readiness.get("counts") or {},
        },
        "monthly_review": {
            "manifest_status": review_manifest.get("status"),
            "pending_update_review_count": effective_pending_update_review_count,
            "pending_financial_review_count": effective_pending_financial_review_count,
            "raw_pending_update_review_count": review_manifest.get("pending_update_review_count"),
            "raw_pending_financial_review_count": review_manifest.get("pending_financial_review_count"),
            "candidate_packet_status": candidate_packet.get("status"),
            "safety_scan_status": safety_scan.get("status"),
            "safe_approval_status": safe_approval.get("status"),
            "safe_approval_reason": safe_approval.get("reason"),
            "owner_review_gate_status": owner_review_gate.get("status"),
            "owner_review_gate_blocker_count": owner_review_gate.get("blocker_count"),
            "owner_review_gate_idempotency_key": owner_review_gate.get("idempotency_key"),
            "owner_review_gate_property_checklist_digest": owner_review_gate.get("property_checklist_digest"),
            "owner_review_gate_guard_workflow_coverage_status": (owner_review_gate.get("guard_workflow_coverage") or {}).get("status") if isinstance(owner_review_gate.get("guard_workflow_coverage"), dict) else None,
            "owner_review_gate_guard_workflow_digest": (owner_review_gate.get("guard_workflow_coverage") or {}).get("digest") if isinstance(owner_review_gate.get("guard_workflow_coverage"), dict) else None,
            "guarded_apply": readiness.get("monthly_guarded_apply") or {},
            "skip_policy": readiness_skip_policy,
            "live_update_capture_status": live_update_capture.get("status"),
            "live_update_target_count": live_update_capture.get("target_count"),
            "live_update_skipped_index_count": live_update_capture.get("skipped_index_count"),
            "live_update_skipped_index_status_counts": live_update_capture.get("skipped_index_status_counts"),
            "live_update_skipped_index_digest": live_update_capture.get("skipped_index_digest"),
            "live_update_skipped_index_records": live_update_capture.get("skipped_index_records"),
            "live_update_check_ok_count": live_update_capture.get("check_ok_count"),
            "live_update_unverified_count": live_update_capture.get("unverified_count"),
            "live_update_target_digest": live_update_capture.get("target_digest"),
            "live_financial_capture_status": live_financial_capture.get("status"),
            "live_financial_target_count": live_financial_capture.get("target_count"),
            "live_financial_skipped_index_count": live_financial_capture.get("skipped_index_count"),
            "live_financial_skipped_index_status_counts": live_financial_capture.get("skipped_index_status_counts"),
            "live_financial_skipped_index_digest": live_financial_capture.get("skipped_index_digest"),
            "live_financial_skipped_index_records": live_financial_capture.get("skipped_index_records"),
            "live_financial_check_ok_count": live_financial_capture.get("check_ok_count"),
            "live_financial_unverified_count": live_financial_capture.get("unverified_count"),
            "live_financial_target_digest": live_financial_capture.get("target_digest"),
            "owner_review_gate": str(report_dir / "baselane_monthly_owner_review_gate.md"),
            "owner_review_gate_csv": str(report_dir / "baselane_monthly_owner_review_gate.csv"),
            **summarize_candidate_records(candidate_packet, sample_limit),
        },
        "monthly_comms": monthly_comms_context,
        "weekly_cf": {
            "weekly_file_update_status": weekly_files.get("status"),
            "weekly_file_update_reason": weekly_files.get("reason"),
            "weekly_file_update_effective_ok": weekly_files.get("effective_ok"),
            "weekly_file_update_review_retry_safe": weekly_files.get("review_retry_safe"),
            "weekly_file_update_cf_gate_snapshot_current": weekly_files.get("cf_gate_snapshot_current"),
            "weekly_file_update_review_effectively_safe": weekly_files_review_effectively_safe,
            "weekly_review_safe_idempotency": weekly_review_safe_idempotency,
            "weekly_cf_gate_snapshot_current": weekly_cf_gate_snapshot_current,
            "weekly_report_cf_gate_idempotency_key": weekly_review_safe_idempotency.get("cf_review_gate_idempotency_key"),
            "weekly_report_cf_gate_action_queue_digest": weekly_review_safe_idempotency.get("cf_review_gate_action_queue_digest"),
            "weekly_report_cf_gate_action_queue_count": weekly_report_cf_gate_action_queue_count,
            "status": weekly_cf.get("status"),
            "reason": weekly_cf.get("reason"),
            "conflict_count": weekly_cf.get("conflict_count"),
            "conflict_review_high_count": weekly_cf.get("conflict_review_high_count"),
            "conflict_review_medium_count": weekly_cf.get("conflict_review_medium_count"),
            "conflict_auto_approval_count": weekly_cf.get("conflict_auto_approval_count"),
            "conflict_auto_approval_digest": weekly_cf.get("conflict_auto_approval_digest"),
            "conflict_auto_apply_status": weekly_cf.get("conflict_auto_apply_status"),
            "conflict_auto_apply_status_counts": weekly_cf.get("conflict_auto_apply_status_counts"),
            "conflict_auto_apply_approved_applicable_count": weekly_cf.get("conflict_auto_apply_approved_applicable_count"),
            "untagged_gl_rows": weekly_cf.get("untagged_gl_rows"),
            "untagged_review_required_count": weekly_cf.get("untagged_review_required_count"),
            "approval_template": weekly_cf.get("conflict_resolution_approval_template"),
            "yhome_operating_cash": yhome_operating_cash,
            "conflict_plan": summarize_cf_plan(cf_plan, sample_limit),
            "untagged": summarize_untagged(untagged_packet, sample_limit),
            "untagged_rule_candidates": {
                "status": untagged_rule_candidates.get("status"),
                "candidate_count": untagged_rule_candidates.get("candidate_count"),
                "high_confidence_count": untagged_rule_candidates.get("high_confidence_count"),
                "medium_confidence_count": untagged_rule_candidates.get("medium_confidence_count"),
                "covered_row_count": untagged_rule_candidates.get("covered_row_count"),
                "candidate_digest": untagged_rule_candidates.get("candidate_digest"),
                "packet": str(report_dir / "baselane_cf_untagged_rule_candidates.md"),
            },
            "ecogl_autonomy": {
                "status": ecogl_autonomy.get("status"),
                "downstream_hold": ecogl_autonomy.get("downstream_hold"),
                "safe_auto_untagged_row_count": ecogl_autonomy.get("safe_auto_untagged_row_count"),
                "safe_auto_rule_count": ecogl_autonomy.get("safe_auto_rule_count"),
                "untagged_exception_row_count": ecogl_autonomy.get("untagged_exception_row_count"),
                "exception_count": ecogl_autonomy.get("exception_count"),
                "safe_auto_action_digest": ecogl_autonomy.get("safe_auto_action_digest"),
                "exception_digest": ecogl_autonomy.get("exception_digest"),
                "safe_actions_csv": str(report_dir / "baselane_ecogl_auto_safe_actions.csv"),
                "exceptions_csv": str(report_dir / "baselane_ecogl_data_quality_exceptions.csv"),
                "packet": str(report_dir / "baselane_ecogl_data_quality_autonomy.md"),
            },
            "ecogl_source_fix": {
                "status": ecogl_source_fix.get("status"),
                "action_count": ecogl_source_fix.get("action_count"),
                "action_type_counts": ecogl_source_fix.get("action_type_counts"),
                "automation_status_counts": ecogl_source_fix.get("automation_status_counts"),
                "idempotency_digest": ecogl_source_fix.get("idempotency_digest"),
                "mutation_mode": ecogl_source_fix.get("mutation_mode"),
                "baselane_source_write_allowed": ecogl_source_fix.get("baselane_source_write_allowed"),
                "historical_evidence_status_counts": ecogl_source_fix_evidence.get("historical_evidence_status_counts"),
                "historical_evidence_automation_safe_count": ecogl_source_fix_evidence.get("historical_evidence_automation_safe_count"),
                "source_fix_verifier_status": ecogl_source_fix_verifier.get("status"),
                "source_fix_verifier_verified_fixed_count": ecogl_source_fix_verifier.get("verified_fixed_count"),
                "source_fix_verifier_remaining_count": ecogl_source_fix_verifier.get("remaining_count"),
                "source_fix_verifier_status_counts": ecogl_source_fix_verifier.get("status_counts"),
                "source_fix_corrections_status": ecogl_source_fix_corrections.get("status"),
                "source_fix_corrections_row_count": ecogl_source_fix_corrections.get("row_count"),
                "source_fix_corrections_remaining_count": ecogl_source_fix_corrections.get("remaining_count"),
                "source_fix_approval_status": ecogl_source_fix_approval.get("status"),
                "source_fix_approval_approved_count": ecogl_source_fix_approval.get("approved_count"),
                "source_fix_approval_pending_count": ecogl_source_fix_approval.get("pending_count"),
                "source_fix_approval_invalid_count": ecogl_source_fix_approval.get("invalid_count"),
                "source_fix_correction_validation_status": ecogl_source_fix_correction_validation.get("status"),
                "source_fix_correction_validation_ready_count": source_fix_validation_ready_count,
                "source_fix_correction_validation_pending_count": source_fix_validation_pending_count,
                "source_fix_correction_validation_invalid_count": source_fix_validation_invalid_count,
                "source_fix_action_queue_status": ecogl_source_fix_action_queue.get("status"),
                "source_fix_action_queue_ready_to_apply_count": source_fix_action_queue_ready_count,
                "source_fix_action_queue_decision_required_count": source_fix_action_queue_decision_required_count,
                "source_fix_action_queue_group_counts": ecogl_source_fix_action_queue.get("group_counts"),
                "source_fix_effectively_clear": source_fix_effectively_clear,
                "evidence_packet": str(report_dir / "baselane_ecogl_source_fix_evidence.md"),
                "verifier_packet": str(report_dir / "baselane_ecogl_source_fix_verifier.md"),
                "action_queue": str(report_dir / "baselane_ecogl_source_fix_action_queue.md"),
                "approval_json": str(report_dir / "baselane_ecogl_source_fix_approval.json"),
                "approved_corrections_csv": str(report_dir / "baselane_ecogl_source_fix_approved_corrections.csv"),
                "corrections_csv": str(report_dir / "baselane_ecogl_source_fix_corrections.csv"),
                "corrections_packet": str(report_dir / "baselane_ecogl_source_fix_corrections.md"),
                "correction_validation_csv": str(report_dir / "baselane_ecogl_source_fix_correction_validation.csv"),
                "correction_validation_packet": str(report_dir / "baselane_ecogl_source_fix_correction_validation.md"),
                "actions_csv": str(report_dir / "baselane_ecogl_source_fix_actions.csv"),
                "packet": str(report_dir / "baselane_ecogl_source_fix_plan.md"),
            },
            "ecogl_safe_apply": {
                "status": ecogl_safe_apply.get("status"),
                "mode": ecogl_safe_apply.get("mode"),
                "safe_action_count": ecogl_safe_apply.get("safe_action_count"),
                "already_clean_safe_pattern_count": ecogl_safe_apply.get("already_clean_safe_pattern_count"),
                "output_written": ecogl_safe_apply.get("output_written"),
                "actions_digest": ecogl_safe_apply.get("actions_digest"),
                "output_digest": ecogl_safe_apply.get("output_digest"),
                "clean_reporting_ledger": str(report_dir / "baselane_weekly_clean_reporting_ledger.csv"),
                "actions_csv": str(report_dir / "baselane_ecogl_safe_category_apply_actions.csv"),
                "packet": str(report_dir / "baselane_ecogl_safe_category_apply_report.md"),
            },
            "review_gate": {
                "status": weekly_cf_gate.get("status"),
                "blocker_count": weekly_cf_gate.get("blocker_count"),
                "idempotency_key": weekly_cf_gate.get("idempotency_key"),
                "action_queue_digest": weekly_cf_gate.get("action_queue_digest"),
                "conflict_approval_template": weekly_cf_gate.get("conflict_approval_template"),
                "packet": str(report_dir / "baselane_weekly_cf_review_gate.md"),
                "queue_csv": str(report_dir / "baselane_weekly_cf_review_gate.csv"),
                "action_queue_count": weekly_cf_gate_action_queue_count,
            },
        },
        "external_send_guard": {
            "lofty_cdp_preflight_status": cdp_preflight.get("status"),
            "lofty_login_tab_count": cdp_preflight.get("login_tab_count"),
            "lofty_pm_tab_count": cdp_preflight.get("pm_tab_count"),
            "owner_email_allowed": readiness.get("owner_email_allowed"),
            "owner_email_send_guard_status": owner_email_send_guard.get("status"),
            "owner_email_send_guard_ok": owner_email_send_guard.get("guard_ok"),
            "owner_email_send_guard_send_allowed": owner_email_send_guard.get("send_allowed"),
            "owner_email_send_guard_safe_block": owner_email_send_guard.get("safe_block"),
            "owner_email_send_guard_max_once_monthly_ok": owner_email_send_guard.get("max_once_monthly_ok"),
            "owner_email_send_guard_no_spam_guard_ok": owner_email_send_guard.get("no_spam_guard_ok"),
            "owner_email_send_guard_idempotency_proof": owner_email_send_guard.get("idempotency_proof"),
            "publish_status": publish.get("status"),
            "excluded_property_count": publish.get("excluded_property_count"),
            "excluded_property_names": publish.get("excluded_property_names"),
            "send_decision_digest": send_decision_digest,
            "owner_email_idempotency_send_decision_digest": owner_email_idempotency_send_decision_digest,
            "owner_email_send_decision_digest": owner_email_send_decision_digest,
            "send_decision_digest_consistent": send_decision_digest_consistent,
            "existing_send_lock_decision_digest": existing_send_lock_decision_digest,
            "existing_send_lock_matches_send_decision": existing_send_lock_matches_send_decision,
            "owner_email_idempotency": owner_email_idempotency,
            "owner_email_send_decision": owner_email_send_decision,
            "monthly_readiness_snapshot": readiness_snapshot_for_packet,
            "monthly_readiness_snapshot_current": readiness_snapshot_current,
            "monthly_readiness_snapshot_actionable_fields_refreshed": readiness_snapshot_for_packet
            is not monthly_readiness_snapshot,
            "current_monthly_readiness_blocked_reason": current_readiness_blocked_reason,
            "publish_blocked_reason": publish_blocked_reason,
            "publish_blocked_reason_current": (
                not current_readiness_blocked_reason
                or not publish_blocked_reason
                or publish_blocked_reason == current_readiness_blocked_reason
            ),
            "publish_issue_count": publish.get("issue_count"),
            "publish_issues": publish.get("issues") or [],
            **publish_blocked_summary,
            "do_not_send_reason": current_readiness_blocked_reason or publish_blocked_reason or ("monthly readiness is not ok" if readiness.get("status") != "ok" else ""),
        },
        "action_items": action_items,
        "review_artifacts": {
            "readiness": str(report_dir / "baselane_financials_monthly_readiness.json"),
            "monthly_close_status": str(report_dir / "baselane_financials_monthly_close_status.md"),
            "monthly_blocker_command_index": str(report_dir / "baselane_financials_monthly_blocker_command_index.md"),
            "owner_review_gate": str(report_dir / "baselane_monthly_owner_review_gate.md"),
            "owner_review_gate_csv": str(report_dir / "baselane_monthly_owner_review_gate.csv"),
            "review_manifest": str(report_dir / "baselane_financials_monthly_review_manifest.md"),
            "candidate_packet": str(report_dir / "baselane_financials_monthly_review_candidate_packet.md"),
            "safety_scan": str(report_dir / "baselane_financials_monthly_review_safety_scan.md"),
            "cf_review_packet": weekly_cf.get("review_packet"),
            "cf_conflict_review_packet": weekly_cf.get("conflict_review_markdown"),
            "cf_untagged_review_packet": weekly_cf.get("untagged_review_markdown"),
            "cf_untagged_rule_candidates": str(report_dir / "baselane_cf_untagged_rule_candidates.md"),
            "ecogl_autonomy": str(report_dir / "baselane_ecogl_data_quality_autonomy.md"),
            "ecogl_source_fix": str(report_dir / "baselane_ecogl_source_fix_plan.md"),
            "ecogl_source_fix_actions": str(report_dir / "baselane_ecogl_source_fix_actions.csv"),
            "ecogl_source_fix_evidence": str(report_dir / "baselane_ecogl_source_fix_evidence.md"),
            "ecogl_source_fix_verifier": str(report_dir / "baselane_ecogl_source_fix_verifier.md"),
            "ecogl_source_fix_corrections": str(report_dir / "baselane_ecogl_source_fix_corrections.csv"),
            "ecogl_source_fix_correction_validation": str(report_dir / "baselane_ecogl_source_fix_correction_validation.md"),
            "ecogl_auto_safe_actions": str(report_dir / "baselane_ecogl_auto_safe_actions.csv"),
            "ecogl_safe_apply": str(report_dir / "baselane_ecogl_safe_category_apply_report.md"),
            "ecogl_safe_apply_actions": str(report_dir / "baselane_ecogl_safe_category_apply_actions.csv"),
            "clean_reporting_ledger": str(report_dir / "baselane_weekly_clean_reporting_ledger.csv"),
            "ecogl_exceptions": str(report_dir / "baselane_ecogl_data_quality_exceptions.csv"),
            "cf_review_gate": str(report_dir / "baselane_weekly_cf_review_gate.md"),
            "cf_review_gate_csv": str(report_dir / "baselane_weekly_cf_review_gate.csv"),
            "cf_balance_sheet_consistency": str(report_dir / "baselane_cf_balance_sheet_consistency_audit.json"),
            "yhome_operating_cash_apply_verify": str(report_dir / "yhome_operating_cash_apply_verify_report.json"),
            "rent_roll_gap_review": str(comms_updates / f"{run_month}-rent-roll-gap-review.md"),
            "rent_roll_gap_queue_csv": rent_roll_gap_review.get("queue_csv") or str(comms_updates / f"{run_month}-rent-roll-gap-review.csv"),
            "rent_roll_gap_approval": rent_roll_gap_review.get("approval_path") or str(comms_updates / f"{run_month}-rent-roll-gap-approval.json"),
        },
    }
    packet["decision"] = build_decision(packet)
    weekly_status_needs_review = weekly_files.get("status") == "review" and packet["decision"].get("weekly_review_effectively_safe") is not True
    if packet["decision"].get("gate") != "ok" or action_items or readiness.get("status") != "ok" or weekly_status_needs_review:
        packet["status"] = "review"
    return packet


def render_markdown(packet: dict[str, Any]) -> str:
    decision = packet.get("decision") or {}
    safe_auto = decision.get("safe_auto_actions") or {}
    monthly_close_status = packet.get("monthly_close_status") or {}
    monthly_candidate_coverage = packet.get("monthly_candidate_coverage") or {}
    monthly_ready_manual_top = monthly_close_status.get("monthly_blocker_ready_manual_top") or []
    monthly = packet["monthly_review"]
    monthly_comms = packet.get("monthly_comms") or {}
    readiness = packet["monthly_readiness"]
    weekly_cf = packet["weekly_cf"]
    untagged_rules = weekly_cf.get("untagged_rule_candidates") or {}
    ecogl_autonomy = weekly_cf.get("ecogl_autonomy") or {}
    ecogl_source_fix = weekly_cf.get("ecogl_source_fix") or {}
    ecogl_safe_apply = weekly_cf.get("ecogl_safe_apply") or {}
    weekly_cf_gate = weekly_cf.get("review_gate") or {}
    yhome_operating_cash = weekly_cf.get("yhome_operating_cash") or {}
    external_send_guard = packet["external_send_guard"]
    path_guard = packet["path_guard"]
    artifacts = packet["review_artifacts"]
    discord_context = packet.get("discord_context") or {}
    discord_review = packet.get("discord_review") or {}
    lines = [
        "# Baselane / Lofty Operations Packet",
        "",
        f"- Generated: `{packet['generated_at']}`",
        f"- Status: `{packet['status']}`",
        f"- Canonical updates path: `{packet['canonical_paths']['updates']}`",
        f"- Canonical owner statements path: `{packet['canonical_paths']['financials']}`",
        f"- Discord context: channel `{discord_context.get('channel_id')}`; messages `{discord_context.get('message_count')}`; age `{discord_context.get('checked_age_hours')}`h; fresh `{discord_context.get('fresh')}`",
        f"- Discord monthly review: plan `{discord_review.get('plan_validation_status')}`; valid `{discord_review.get('plan_validation_ok')}`; ready `{discord_review.get('discord_review_ready')}`; dry-run current `{discord_review.get('dry_run_current_for_run')}`; live post ok `{discord_review.get('owner_email_live_post_ok')}`",
        "",
        "## Decision",
        "",
        f"- Gate: `{decision.get('gate')}`",
        f"- Blocker: {decision.get('blocker')}",
        f"- Open: `{decision.get('primary_artifact')}`",
        f"- Next action: {decision.get('next_action')}",
        f"- Hold: {decision.get('hold')}",
        f"- Safe automation already applied: ECO GL rows `{safe_auto.get('ecogl_category_rows_applied')}`; CF rows `{safe_auto.get('cf_workbook_rows_synced_from_gl')}`; sold/delisted/closed skipped `{safe_auto.get('sold_delisted_closed_excluded')}`; external/manual excluded `{safe_auto.get('external_manual_excluded')}`; total excluded `{safe_auto.get('total_excluded_from_publish_email')}`",
        f"- Live publish allowed: `{decision.get('publish_allowed')}`; owner email allowed: `{decision.get('owner_email_allowed')}`; max once monthly: `{decision.get('owner_email_max_once_monthly')}`",
        f"- Active-property policy: {decision.get('active_property_only_policy')}",
        "",
        "## Current Gates",
        "",
        f"- Monthly close status: `{monthly_close_status.get('status')}`; failed step `{monthly_close_status.get('failed_step')}`; gaps `{monthly_close_status.get('monthly_completion_gap_count')}`; blocker commands `{monthly_close_status.get('monthly_blocker_command_index_count')}`; ready manual `{monthly_close_status.get('monthly_blocker_ready_manual_count')}`; safe auto `{monthly_close_status.get('monthly_blocker_safe_auto_count')}`; first ready command `{monthly_ready_manual_top[0].get('command') if monthly_ready_manual_top and isinstance(monthly_ready_manual_top[0], dict) else None}`",
        f"- Monthly candidate coverage: `{monthly_candidate_coverage.get('status')}`; mismatches `{monthly_candidate_coverage.get('mismatch_count')}`; source blockers `{(monthly_candidate_coverage.get('transfer_reconciliation') or {}).get('source_blocker_count')}`; generated `{monthly_candidate_coverage.get('generated_at')}`",
        f"- Monthly readiness: `{readiness.get('status')}`; blockers `{readiness.get('blocker_count')}`; owner email allowed `{readiness.get('owner_email_allowed')}`",
        f"- Monthly reviews: updates `{monthly.get('pending_update_review_count')}`; financials `{monthly.get('pending_financial_review_count')}`; safety scan `{monthly.get('safety_scan_status')}`",
        f"- Monthly owner review gate: `{monthly.get('owner_review_gate_status')}`; blockers `{monthly.get('owner_review_gate_blocker_count')}`; idempotency `{monthly.get('owner_review_gate_idempotency_key')}`; checklist digest `{monthly.get('owner_review_gate_property_checklist_digest')}`; guard workflow `{monthly.get('owner_review_gate_guard_workflow_coverage_status')}` digest `{monthly.get('owner_review_gate_guard_workflow_digest')}`",
        f"- Monthly guarded apply: `{(monthly.get('guarded_apply') or {}).get('status')}` apply `{(monthly.get('guarded_apply') or {}).get('apply')}`; records `{(monthly.get('guarded_apply') or {}).get('record_count')}`; active `{(monthly.get('guarded_apply') or {}).get('active_record_count')}`; excluded `{(monthly.get('guarded_apply') or {}).get('excluded_record_count')}`; skipped `{(monthly.get('guarded_apply') or {}).get('skipped_record_count')}`; guard failed updates `{(monthly.get('guarded_apply') or {}).get('guard_failed_update_count')}`; financials `{(monthly.get('guarded_apply') or {}).get('guard_failed_financial_count')}`; audit `{(monthly.get('guarded_apply') or {}).get('guard_audit_status')}` issues `{(monthly.get('guarded_apply') or {}).get('guard_audit_issue_count')}`",
        f"- Monthly skip policy: sold/delisted/closed exclusions match `{(monthly.get('skip_policy') or {}).get('skipped_exclusion_counts_match')}`; skipped `{(monthly.get('skip_policy') or {}).get('owner_review_gate_property_skipped_count')}`; external/manual `{(monthly.get('skip_policy') or {}).get('owner_review_gate_property_external_excluded_count')}`; total `{(monthly.get('skip_policy') or {}).get('owner_review_gate_property_excluded_total_count')}`; updates skipped `{(monthly.get('skip_policy') or {}).get('live_update_skipped_index_count')}`; financials skipped `{(monthly.get('skip_policy') or {}).get('live_financial_skipped_index_count')}`",
        f"- Monthly live guards: updates `{monthly.get('live_update_check_ok_count')}` / `{monthly.get('live_update_target_count')}` `{monthly.get('live_update_capture_status')}` digest `{monthly.get('live_update_target_digest')}`; financials `{monthly.get('live_financial_check_ok_count')}` / `{monthly.get('live_financial_target_count')}` `{monthly.get('live_financial_capture_status')}` digest `{monthly.get('live_financial_target_digest')}`",
        f"- Monthly rent roll: `{monthly_comms.get('rent_roll_gap_review_status')}`; source blockers `{monthly_comms.get('rent_roll_source_blocker_count')}`; deferred gaps `{monthly_comms.get('rent_roll_deferred_gap_count')}`; pending gaps `{monthly_comms.get('rent_roll_pending_gap_count')}`; pending stale exports `{monthly_comms.get('rent_roll_pending_stale_export_date_count')}`; queue digest `{monthly_comms.get('rent_roll_gap_queue_digest')}`",
        f"- Monthly rent-roll approval template: `{monthly_comms.get('rent_roll_gap_approval_template_coverage_status')}`; digest `{monthly_comms.get('rent_roll_gap_approval_template_digest')}`",
        f"- Yhome operating cash: `{yhome_operating_cash.get('status')}`; stale cells `{yhome_operating_cash.get('update_required_count')}`; target columns `{', '.join(yhome_operating_cash.get('target_columns') or [])}`; external write attempted `{yhome_operating_cash.get('external_write_attempted')}`",
        f"- Weekly CF sync: `{weekly_cf.get('status')}`; retry-safe `{decision.get('weekly_review_effectively_safe')}`; conflicts `{weekly_cf.get('conflict_count')}`; untagged required `{weekly_cf.get('untagged_review_required_count')}`",
        f"- Weekly CF zero-fill auto-apply: approved `{weekly_cf.get('conflict_auto_apply_approved_applicable_count')}`; status `{weekly_cf.get('conflict_auto_apply_status')}`; counts `{weekly_cf.get('conflict_auto_apply_status_counts')}`",
        f"- Weekly CF review gate: `{weekly_cf_gate.get('status')}`; blockers `{weekly_cf_gate.get('blocker_count')}`; queue rows `{weekly_cf_gate.get('action_queue_count')}`; idempotency `{weekly_cf_gate.get('idempotency_key')}`; queue digest `{weekly_cf_gate.get('action_queue_digest')}`",
        f"- Weekly CF approval template: `{(weekly_cf_gate.get('conflict_approval_template') or {}).get('status')}`; approved `{(weekly_cf_gate.get('conflict_approval_template') or {}).get('approved_row_count')}`; blocked `{(weekly_cf_gate.get('conflict_approval_template') or {}).get('blocked_row_count')}`; digest `{(weekly_cf_gate.get('conflict_approval_template') or {}).get('digest')}`",
        f"- Untagged rule candidates: `{untagged_rules.get('candidate_count')}`; high `{untagged_rules.get('high_confidence_count')}`; medium `{untagged_rules.get('medium_confidence_count')}`; covered rows `{untagged_rules.get('covered_row_count')}`; digest `{untagged_rules.get('candidate_digest')}`",
        f"- ECO GL safe apply: `{ecogl_safe_apply.get('status')}`; applied rows `{ecogl_safe_apply.get('safe_action_count')}`; output written `{ecogl_safe_apply.get('output_written')}`; actions digest `{ecogl_safe_apply.get('actions_digest')}`",
        f"- ECO GL autonomy: `{ecogl_autonomy.get('status')}`; auto-safe rows `{ecogl_autonomy.get('safe_auto_untagged_row_count')}`; exceptions `{ecogl_autonomy.get('exception_count')}`; downstream hold `{ecogl_autonomy.get('downstream_hold')}`; safe digest `{ecogl_autonomy.get('safe_auto_action_digest')}`; exception digest `{ecogl_autonomy.get('exception_digest')}`",
        f"- ECO GL source-fix queue: `{ecogl_source_fix.get('status')}`; actions `{ecogl_source_fix.get('action_count')}`; mutation `{ecogl_source_fix.get('mutation_mode')}`; Baselane source write allowed `{ecogl_source_fix.get('baselane_source_write_allowed')}`; digest `{ecogl_source_fix.get('idempotency_digest')}`",
        f"- Live/send guard: Lofty PM tabs `{external_send_guard.get('lofty_pm_tab_count')}`; login tabs `{external_send_guard.get('lofty_login_tab_count')}`; readiness snapshot current `{external_send_guard.get('monthly_readiness_snapshot_current')}`; publish issues `{external_send_guard.get('publish_issue_count')}`; blocked publish records `{external_send_guard.get('publish_blocked_record_count')}`; do-not-send reason `{external_send_guard.get('do_not_send_reason')}`",
        f"- Public path guard: `{path_guard.get('public_path_guard_status')}`; canonical updates `{path_guard.get('canonical_updates_folder')}`; canonical owner statements `{path_guard.get('canonical_financials_folder')}`",
        f"- Discord-public financial source guard: `{path_guard.get('discord_public_financial_source_guard_status')}`; issues `{path_guard.get('discord_public_financial_source_guard_issue_count')}`; deleted GL rows `{path_guard.get('discord_public_financial_source_guard_deleted_gl_rows_count')}`; financial docs `{path_guard.get('discord_public_financial_source_guard_financial_doc_count')}`; update docs `{path_guard.get('discord_public_financial_source_guard_update_doc_count')}`",
        "",
        "## Action Items",
        "",
    ]
    if packet["action_items"]:
        lines.extend(f"- {item}" for item in packet["action_items"])
    else:
        lines.append("- None")
    blocked_publish_samples = external_send_guard.get("publish_blocked_record_samples") or []
    if blocked_publish_samples:
        lines.extend(["", "## Blocked Publish Records", ""])
        for record in blocked_publish_samples:
            lines.append(
                f"- `{record.get('status')}` `{record.get('property_name')}`: {record.get('next_action')} "
                f"Financials `{record.get('financials_md')}` Updates `{record.get('updates_md')}`"
            )
    lines.extend(
        [
            "",
            "## Review Artifacts",
            "",
            f"- Monthly readiness: `{artifacts.get('readiness')}`",
            f"- Monthly close status: `{artifacts.get('monthly_close_status')}`",
            f"- Monthly blocker command index: `{artifacts.get('monthly_blocker_command_index')}`",
            f"- Monthly owner queue CSV: `{artifacts.get('owner_review_gate_csv')}`",
            f"- Monthly owner review gate: `{artifacts.get('owner_review_gate')}`",
            f"- Monthly review manifest: `{artifacts.get('review_manifest')}`",
            f"- Monthly candidate packet: `{artifacts.get('candidate_packet')}`",
            f"- Monthly safety scan: `{artifacts.get('safety_scan')}`",
            f"- Rent-roll queue CSV: `{artifacts.get('rent_roll_gap_queue_csv')}`",
            f"- Rent-roll gap review: `{artifacts.get('rent_roll_gap_review')}`",
            f"- Rent-roll gap approval: `{artifacts.get('rent_roll_gap_approval')}`",
            f"- CF review packet: `{artifacts.get('cf_review_packet')}`",
            f"- CF conflict packet: `{artifacts.get('cf_conflict_review_packet')}`",
            f"- CF untagged packet: `{artifacts.get('cf_untagged_review_packet')}`",
            f"- CF untagged rule candidates: `{artifacts.get('cf_untagged_rule_candidates')}`",
            f"- ECO GL autonomy: `{artifacts.get('ecogl_autonomy')}`",
            f"- ECO GL source-fix queue: `{artifacts.get('ecogl_source_fix')}`",
            f"- ECO GL source-fix corrections: `{artifacts.get('ecogl_source_fix_corrections')}`",
            f"- ECO GL source-fix correction validation: `{artifacts.get('ecogl_source_fix_correction_validation')}`",
            f"- ECO GL source-fix actions: `{artifacts.get('ecogl_source_fix_actions')}`",
            f"- ECO GL auto-safe actions: `{artifacts.get('ecogl_auto_safe_actions')}`",
            f"- ECO GL safe apply: `{artifacts.get('ecogl_safe_apply')}`",
            f"- ECO GL safe apply actions: `{artifacts.get('ecogl_safe_apply_actions')}`",
            f"- ECO GL clean reporting ledger: `{artifacts.get('clean_reporting_ledger')}`",
            f"- ECO GL exceptions: `{artifacts.get('ecogl_exceptions')}`",
            f"- CF queue CSV: `{artifacts.get('cf_review_gate_csv')}`",
            f"- CF review gate: `{artifacts.get('cf_review_gate')}`",
            f"- CF balance consistency: `{artifacts.get('cf_balance_sheet_consistency')}`",
            f"- Yhome operating cash apply/verify: `{artifacts.get('yhome_operating_cash_apply_verify')}`",
            "",
            "## Sample Approval Targets",
            "",
        ]
    )
    for target in monthly.get("update_approval_target_samples") or []:
        lines.append(f"- Update approval target: `{target}`")
    for target in monthly.get("financial_approval_target_samples") or []:
        lines.append(f"- Financial approval target: `{target}`")
    if not monthly.get("update_approval_target_samples") and not monthly.get("financial_approval_target_samples"):
        lines.append("- None")
    lines.extend(["", "## CF Conflict Samples", ""])
    conflict_plan = weekly_cf.get("conflict_plan") or {}
    for record in conflict_plan.get("needs_approval_samples") or []:
        lines.append(
            f"- Needs approval: `{record.get('property')}` `{record.get('label')}` "
            f"`{record.get('action')}` `{record.get('current_value')}` -> `{record.get('new_value')}`"
        )
    for record in conflict_plan.get("blocked_samples") or []:
        lines.append(
            f"- Blocked: `{record.get('property')}` `{record.get('label')}` "
            f"`{record.get('action')}` — {record.get('reason')}"
        )
    if not conflict_plan.get("needs_approval_samples") and not conflict_plan.get("blocked_samples"):
        lines.append("- None")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a local operational review packet for Baselane/Lofty financial reporting gates.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--sample-limit", type=int, default=8)
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    report_path = args.report or root / "reports" / "baselane_financials_operations_packet.json"
    markdown_path = args.markdown or root / "reports" / "baselane_financials_operations_packet.md"
    packet = build_packet(root, args.sample_limit)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(packet), encoding="utf-8")
    print(json.dumps({key: packet[key] for key in ("status", "action_items")}, indent=2, sort_keys=True))
    return 0 if packet["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
