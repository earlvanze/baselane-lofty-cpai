#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import os
import re
import shlex
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from transfer_report_digest import stable_transfer_report_digest


def read_json(path: Path) -> dict:
    if not path.exists():
        return {"status": "missing", "path": str(path)}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "unreadable", "path": str(path), "error": str(exc)}


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return None


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def source_cash_issue_summary(
    source_cash_reconciliation_actions: dict,
    mismatch_count: int,
    no_match_count: int,
    split_scope_missing_count: int,
) -> list[str]:
    if source_cash_reconciliation_actions.get("status") not in {"missing", "unreadable", None}:
        actions = source_cash_reconciliation_actions.get("actions")
        if not isinstance(actions, list):
            actions = source_cash_reconciliation_actions.get("active_monthly_candidate_actions_bounded")
        if isinstance(actions, list):
            active_kind_counts = Counter(
                action.get("kind")
                for action in actions
                if isinstance(action, dict)
                and (action.get("effective_scope") or action.get("scope")) == "active_monthly_candidate"
            )
            mismatch_count = int(active_kind_counts.get("source_cash_mismatch") or 0)
            no_match_count = int(active_kind_counts.get("unmatched_cf_workbook") or 0)
            split_scope_missing_count = int(active_kind_counts.get("split_scope_missing_source_cash") or 0)
        else:
            kind_counts = source_cash_reconciliation_actions.get("action_kind_counts")
            if isinstance(kind_counts, dict):
                mismatch_count = int(kind_counts.get("source_cash_mismatch") or mismatch_count or 0)
                no_match_count = int(kind_counts.get("unmatched_cf_workbook") or no_match_count or 0)
                split_scope_missing_count = int(kind_counts.get("split_scope_missing_source_cash") or split_scope_missing_count or 0)
        active_count = int(source_cash_reconciliation_actions.get("active_monthly_candidate_action_count") or 0)
    else:
        active_count = 0
    issues = []
    if mismatch_count:
        issues.append(f"{mismatch_count} workbook mismatches")
    if no_match_count:
        issues.append(f"{no_match_count} unmatched CF workbooks")
    if split_scope_missing_count:
        issues.append(f"{split_scope_missing_count} split-scope missing properties")
    if active_count:
        issues.append(f"{active_count} active monthly candidate actions")
    return issues


def active_source_cash_action_summary(source_cash_reconciliation_actions: dict) -> dict | None:
    actions = source_cash_reconciliation_actions.get("active_monthly_candidate_actions_bounded")
    if not isinstance(actions, list):
        actions = source_cash_reconciliation_actions.get("actions")
    if not isinstance(actions, list):
        return None
    for action in actions:
        if not isinstance(action, dict):
            continue
        if (action.get("effective_scope") or action.get("scope")) != "active_monthly_candidate":
            continue
        return {
            "kind": action.get("kind"),
            "property": action.get("property"),
            "matched_active_property": action.get("matched_active_property"),
            "file": action.get("file"),
            "action": action.get("action"),
            "candidate_financial_evidence": action.get("candidate_financial_evidence"),
        }
    return None


def zero_row_source_ledger_decision_missing_actions(source_cash_reconciliation_actions: dict) -> list[dict]:
    actions = source_cash_reconciliation_actions.get("active_monthly_candidate_actions_bounded")
    if not isinstance(actions, list):
        actions = source_cash_reconciliation_actions.get("actions")
    if not isinstance(actions, list):
        return []
    missing = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        if (action.get("effective_scope") or action.get("scope")) != "active_monthly_candidate":
            continue
        evidence = action.get("candidate_financial_evidence")
        if not isinstance(evidence, dict):
            continue
        if evidence.get("eco_gl_column_e_source_mode") != "source_ledger_zero_rows":
            continue
        if evidence.get("zero_row_source_ledger_reviewed") is True and str(evidence.get("zero_row_source_ledger_decision") or "") in {
            "include_active_no_activity",
            "exclude_no_dao_activity",
        }:
            continue
        missing.append(
            {
                "kind": action.get("kind"),
                "property": action.get("property"),
                "matched_active_property": action.get("matched_active_property"),
                "file": action.get("file"),
                "eco_gl_column_e_row_count": evidence.get("eco_gl_column_e_row_count"),
                "eco_gl_column_e_sum": evidence.get("eco_gl_column_e_sum"),
                "eco_gl_column_e_source_mode": evidence.get("eco_gl_column_e_source_mode"),
                "financials_md": evidence.get("financials_md"),
            }
        )
    return missing


EXPECTED_LOCAL_MODEL = "ollama-cyber/qwen3.5:35b-a3b"
EXPECTED_LOCAL_PROVIDER = "ollama-cyber"
EXPECTED_LOCAL_MODEL_ID = "qwen3.5:35b-a3b"
EXPECTED_LOCAL_MODEL_TASK_CLASS = "schema_checked_precomputed_status_formatting"
LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS = 30.0
REQUIRE_LOCAL_MODEL_PREFLIGHT = os.environ.get("BASELANE_REQUIRE_LOCAL_MODEL_PREFLIGHT_FOR_MONTHLY_CLOSE", "0") == "1"
LIVE_CAPTURE_MAX_AGE_HOURS = 30.0
MONTHLY_GUARDED_APPLY_MAX_AGE_HOURS = 30.0
LOFTY_PM_PUBLISH_MAX_AGE_HOURS = 30.0
MONTHLY_STATEMENTS_MAX_AGE_HOURS = 45.0 * 24.0
SAFE_MONTHLY_CRON_DRY_RUN_COMMAND = (
    "DRY_RUN=1 SEND_OWNER_EMAILS=0 PUBLISH_LOFTY_PM_UPDATES=0 "
    "RUN_LOFTY_GUARDED_APPLY=1 APPLY_LOFTY_GUARDED_UPDATES=0 bash scripts/baselane_financials_monthly_cron.sh"
)
POST_AUTH_RESUME_COMMAND = "bash scripts/baselane_financials_post_auth_resume.sh"
SAFE_MONTHLY_GUARDED_APPLY_REVIEW_COMMAND = (
    "DRY_RUN=1 SEND_OWNER_EMAILS=0 PUBLISH_LOFTY_PM_UPDATES=0 "
    "RUN_LOFTY_GUARDED_APPLY=1 APPLY_LOFTY_GUARDED_UPDATES=0 bash scripts/baselane_financials_monthly_cron.sh"
)
MONTHLY_READINESS_SELF_FAILED_STEPS = {
    "monthly_readiness",
    "monthly_readiness_report",
    "monthly_readiness_report_post_reconciliation",
}


def property_name(record: dict) -> str:
    value = record.get("property_name") or Path(record.get("property_path") or "").name
    if not value:
        source_path = Path(record.get("updates_md") or record.get("financials_md") or "")
        value = source_path.parents[2].name if len(source_path.parents) > 2 else source_path.name
    return value or "unknown"


def property_identity_keys(record: dict) -> set[str]:
    keys = set()
    for key in ("property_name", "input_property_name", "property_path", "input_property_path"):
        value = str(record.get(key) or "").strip()
        if value:
            keys.add(value)
            if "/" in value or "\\" in value:
                keys.add(Path(value).name)
    name = property_name(record)
    if name:
        keys.add(name)
    return keys


def normalize_property_label(value: object) -> str:
    text = Path(str(value or "").strip()).name
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def runtime_target_property_names(runtime_map: dict) -> set[str]:
    properties = runtime_map.get("properties") if isinstance(runtime_map.get("properties"), list) else []
    names: set[str] = set()
    for record in properties:
        if not isinstance(record, dict):
            continue
        for key in ("property_name", "full_address", "input_property_name", "property_path", "input_property_path"):
            normalized = normalize_property_label(record.get(key))
            if normalized:
                names.add(normalized)
    return names


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except Exception:
        return []


def rent_roll_target_gap_summary(
    rent_roll_gap_review: dict,
    queue_csv: Path,
    runtime_map: dict,
) -> dict:
    target_names = runtime_target_property_names(runtime_map)
    pending_rows = [
        row
        for row in read_csv_rows(queue_csv)
        if row.get("queue_type") == "rent_roll_gap" and str(row.get("approved") or "").strip().lower() != "true"
    ]
    if not target_names or not pending_rows:
        pending_count = count(rent_roll_gap_review.get("pending_gap_count"))
        return {
            "target_scoped": False,
            "target_property_count": len(target_names),
            "pending_gap_count": pending_count,
            "target_pending_gap_count": pending_count,
            "non_target_pending_gap_count": 0,
            "target_pending_gap_properties": [],
        }
    target_pending = []
    non_target_pending = []
    for row in pending_rows:
        normalized = normalize_property_label(row.get("property_path"))
        if normalized in target_names:
            target_pending.append(row)
        else:
            non_target_pending.append(row)
    return {
        "target_scoped": True,
        "target_property_count": len(target_names),
        "pending_gap_count": len(pending_rows),
        "target_pending_gap_count": len(target_pending),
        "non_target_pending_gap_count": len(non_target_pending),
        "target_pending_gap_properties": [
            Path(str(row.get("property_path") or "")).name for row in target_pending[:25]
        ],
    }


def fallback_missing_draft_property_keys(candidate_packet: dict) -> set[str]:
    keys = set()
    for record in candidate_packet.get("records") or []:
        if not isinstance(record, dict):
            continue
        if record.get("update_source_mode") != "financial_summary_fallback_missing_draft":
            continue
        if not str(record.get("update_candidate") or "").strip():
            continue
        keys.update(property_identity_keys(record))
    return keys


def fallback_missing_draft_record_count(candidate_packet: dict) -> int:
    total = 0
    for record in candidate_packet.get("records") or []:
        if not isinstance(record, dict):
            continue
        if record.get("update_source_mode") != "financial_summary_fallback_missing_draft":
            continue
        if str(record.get("update_candidate") or "").strip():
            total += 1
    return total


def count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def autonomy_source_quality_next_action(report: dict[str, Any]) -> str:
    """Name every active live-source hold; one resolved hold must not mask another."""
    queue_types = {
        str(record.get("queue_type") or "").strip()
        for record in report.get("exceptions") or []
        if isinstance(record, dict)
    }
    future_dated = (
        "future_dated_source_transaction" in queue_types
        or count(report.get("future_dated_source_exception_count")) > 0
    )
    pending_unassigned = (
        "pending_unassigned_material_source_transaction" in queue_types
        or count(report.get("pending_unassigned_material_source_exception_count")) > 0
    )
    known_payment_split = (
        "known_property_payment_split" in queue_types
        or count(report.get("known_property_payment_split_exception_count")) > 0
    )
    actions: list[str] = []
    if future_dated:
        actions.append(
            "Reverse or redate future-dated Baselane source journals and retain any forecast outside the live ECO cash ledger"
        )
    if pending_unassigned:
        actions.append(
            "wait for the pending Baselane source transaction to post, then verify its native property/tag split"
        )
    if known_payment_split:
        actions.append(
            "verify the documented parcel allocations, full settled debit total, and property/tag coverage for the posted Baselane payment"
        )
    if not actions:
        actions.append("Resolve the active Baselane source-quality exceptions")
    return (
        "; then ".join(actions)
        + ". Rerun the Baselane daily source-quality reconciliation before any CF, Lofty, Discord, email, or Telegram output."
    )


def zero_or_empty(value: object) -> bool:
    return value in (None, "", 0, "0")


def monthly_accrual_update_detail_text(report: dict) -> str:
    updates = report.get("monthly_accruals_live_plan_update_details")
    mismatches = report.get("monthly_accruals_amount_mismatch_details")
    detail = next((item for item in updates or [] if isinstance(item, dict)), None)
    mismatch = next((item for item in mismatches or [] if isinstance(item, dict)), None)
    if not detail and not mismatch:
        return ""
    property_name = (
        (detail or {}).get("property")
        or (mismatch or {}).get("property")
        or "unknown property"
    )
    kind = (detail or {}).get("kind") or (mismatch or {}).get("kind") or "accrual"
    row_id = (detail or {}).get("id")
    amount = (detail or {}).get("absolute_amount") or (mismatch or {}).get("expected_amount")
    current = (mismatch or {}).get("current_row_amount")
    expected = (mismatch or {}).get("expected_amount")
    pieces = [f"target {property_name} {kind}"]
    if row_id:
        pieces.append(f"Baselane row id {row_id}")
    if current is not None and expected is not None:
        pieces.append(f"current ${float(current):,.2f} expected ${float(expected):,.2f}")
    elif amount is not None:
        pieces.append(f"amount ${float(amount):,.2f}")
    return " (" + "; ".join(pieces) + ")"


def transfer_reconciliation_next_action(report: dict) -> str | None:
    source_blockers = [str(item or "") for item in (report.get("source_blockers") or [])]
    if not any(
        blocker.startswith("monthly_accruals_live_plan_")
        or blocker.startswith("monthly_accruals_amount_mismatch_count")
        for blocker in source_blockers
    ):
        return None

    live_plan_status = str(report.get("monthly_accruals_live_plan_status") or "").strip()
    create_count = count(report.get("monthly_accruals_live_plan_create_count"))
    update_count = count(report.get("monthly_accruals_live_plan_update_count"))
    issue_count = count(report.get("monthly_accruals_live_plan_issue_count"))
    plan_path = str(report.get("monthly_accruals_live_plan_report") or "").strip()
    digest = str(report.get("monthly_accruals_live_plan_target_digest") or "").strip()
    plan_ref = f" from {plan_path}" if plan_path else ""
    digest_ref = f" with digest {digest}" if digest else ""
    detail_ref = monthly_accrual_update_detail_text(report)

    if live_plan_status and live_plan_status != "ok":
        return (
            f"Fix the Baselane monthly accrual live-plan status ({live_plan_status}){plan_ref}, then rerun transfer reconciliation and monthly readiness. "
            "Keep Lofty publish, Discord, Telegram, and owner email disabled until the transfer report is final."
        )
    if issue_count or create_count:
        pieces = []
        if create_count:
            pieces.append(f"{create_count} create(s)")
        if issue_count:
            pieces.append(f"{issue_count} issue(s)")
        return (
            f"Resolve the Baselane monthly accrual live-plan blocker ({', '.join(pieces)}){plan_ref}{digest_ref}, then rerun transfer reconciliation and monthly readiness. "
            "Keep Lofty publish, Discord, Telegram, and owner email disabled until the transfer report is final."
        )
    if update_count:
        return (
            f"Resolve the Baselane monthly accrual live-plan blocker: apply/verify {update_count} guarded live accrual update(s){detail_ref}{plan_ref}{digest_ref}, "
            "then run human-paced Baselane sync and rerun transfer reconciliation and monthly readiness. "
            "Keep Lofty publish, Discord, Telegram, and owner email disabled until the transfer report is final."
        )
    return (
        f"Resolve the Baselane monthly accrual amount mismatch{detail_ref}{plan_ref}{digest_ref}, then rerun transfer reconciliation and monthly readiness. "
        "Keep Lofty publish, Discord, Telegram, and owner email disabled until the transfer report is final."
    )


def pipeline_candidate_coverage_next_action(report: dict) -> str:
    mismatches = report.get("mismatches") if isinstance(report.get("mismatches"), list) else []
    first_mismatch = next((item for item in mismatches if isinstance(item, dict)), {})
    kind = str(first_mismatch.get("kind") or "").strip()
    if kind == "transfer_reconciliation_telegram_delivery_stale":
        return (
            "Regenerate and deliver the monthly transfer reconciliation Telegram DM from the current transfer report digest, "
            "then rerun candidate coverage and monthly readiness. Keep owner email disabled until Telegram proof matches the current report."
        )
    if kind == "discord_all_plan_send_proof_incomplete":
        held_properties = first_mismatch.get("held_financial_review_properties")
        held_blockers = first_mismatch.get("held_financial_review_blockers")
        if isinstance(held_properties, list) and held_properties:
            blocker_text = ""
            if isinstance(held_blockers, list) and held_blockers:
                blocker_text = f" Blocker: {held_blockers[0]}."
            held_property_text = " ".join(str(item) for item in held_properties).lower()
            review_artifact_text = ""
            if "804" in held_property_text and "quitman" in held_property_text:
                review_artifact_text = (
                    " Work `reports/baselane_804_quitman_cash_alignment_group_review_queue.csv`, then run "
                    "`bash reports/baselane_804_quitman_cash_alignment_import_group_review.requires-explicit-approval.sh`; "
                    "do not move 804 cash until validation is effective ok."
                )
            return (
                f"Resolve held financial review for {', '.join(str(item) for item in held_properties[:5])}.{blocker_text} "
                f"{review_artifact_text} Then regenerate the all-property Discord send plan/proof so sent_or_verified_count equals record_count before owner email."
            )
        return (
            "Send or verify every planned per-property Discord update so sent_or_verified_count equals record_count, "
            "then rerun candidate coverage and monthly readiness before owner email."
        )
    if kind:
        return f"Resolve monthly pipeline coverage mismatch `{kind}`, then rerun candidate coverage and monthly readiness before owner email."
    return "Rerun monthly pipeline candidate coverage and resolve any coverage mismatches before owner email."


def current_transfer_coverage_mismatch(coverage: dict, transfer: dict, transfer_path: Path) -> dict | None:
    if not coverage or coverage.get("status") in {None, "", "missing", "unreadable"}:
        return None
    current_digest = stable_transfer_report_digest(transfer_path)
    telegram = coverage.get("telegram_reconciliation") if isinstance(coverage.get("telegram_reconciliation"), dict) else {}
    embedded_transfer = coverage.get("transfer_reconciliation") if isinstance(coverage.get("transfer_reconciliation"), dict) else {}
    coverage_digest = str(
        telegram.get("current_transfer_report_digest") or telegram.get("transfer_report_digest") or ""
    ).strip()
    current_source_blockers = transfer.get("source_blockers") if isinstance(transfer.get("source_blockers"), list) else []
    embedded_final = embedded_transfer.get("recommended_send_to_lofty_total_is_final")
    if current_digest and coverage_digest and coverage_digest != current_digest:
        return {
            "kind": "transfer_reconciliation_coverage_stale",
            "field": "telegram_reconciliation.current_transfer_report_digest",
            "coverage_transfer_report_digest": coverage_digest,
            "current_transfer_report_digest": current_digest,
            "current_transfer_reconciliation_status": transfer.get("status"),
            "coverage_transfer_reconciliation_status": embedded_transfer.get("status"),
        }
    if current_source_blockers and embedded_final is True:
        return {
            "kind": "transfer_reconciliation_coverage_conflicts_with_current_source_blockers",
            "field": "transfer_reconciliation.recommended_send_to_lofty_total_is_final",
            "coverage_recommended_send_to_lofty_total_is_final": embedded_final,
            "current_transfer_reconciliation_status": transfer.get("status"),
            "current_source_blockers": current_source_blockers[:10],
        }
    return None


def reconcile_pipeline_candidate_coverage(coverage: dict, transfer: dict, transfer_path: Path) -> dict:
    mismatch = current_transfer_coverage_mismatch(coverage, transfer, transfer_path)
    if not mismatch:
        return coverage
    reconciled = dict(coverage)
    mismatches = list(coverage.get("mismatches") if isinstance(coverage.get("mismatches"), list) else [])
    if not any(isinstance(item, dict) and item.get("kind") == mismatch["kind"] for item in mismatches):
        mismatches.append(mismatch)
    reconciled["mismatches"] = mismatches
    reconciled["mismatch_count"] = len(mismatches)
    reconciled["status"] = "review"
    reconciled["current_transfer_reconciliation_status"] = transfer.get("status")
    reconciled["current_transfer_report_digest"] = stable_transfer_report_digest(transfer_path)
    return reconciled


def generic_monthly_run_next_action(action: str) -> bool:
    text = str(action or "").strip().lower()
    return not text or "daily sync" in text or "failed monthly run step" in text or "monthly run report is ok" in text


def monthly_finance_truth_auth_next_action(report: dict, run_month: str) -> str | None:
    if report.get("status") != "failed":
        return None
    if report.get("auth_blocked") is not True and report.get("cdp_blocked") is not True:
        return None
    error_tail = str(report.get("error_tail") or "").lower()
    if not any(
        marker in error_tail
        for marker in (
            "recaptcha",
            "captcha",
            "missing cookie",
            "unauthorized_access",
            "no direct authenticated-looking baselane page targets",
            "existing baselane tabs are login/error pages",
            "x-firebase-appcheck",
        )
    ):
        return None
    month_prefix = f"RUN_MONTH={shlex.quote(run_month)} " if run_month else ""
    return (
        "Solve Baselane reCAPTCHA/appcheck or complete login in the visible Baselane CDP tab, then rerun "
        f"`{month_prefix}bash scripts/baselane_monthly_finance_truth_refresh.sh` or "
        f"`{POST_AUTH_RESUME_COMMAND}` before downstream CF/FINANCIALS/Lofty/Discord/email outputs."
    )


def comms_root_for(root: Path) -> Path:
    configured = os.environ.get("COMMS_WORKSPACE")
    candidates = [
        Path(configured) if configured else None,
        root / "workspace-lofty-vp",
        root / "workspace-lofty-vp-comms",
    ]
    for candidate in candidates:
        if candidate and candidate.is_dir():
            return candidate
    return root / "workspace-lofty-vp"


def default_comms_root() -> Path:
    return comms_root_for(Path(__file__).absolute().parents[1].parent)


def hemlane_cdp_command(comms_root: Path, run_month: str | None) -> str:
    month = str(run_month or "YYYY-MM").strip() or "YYYY-MM"
    return f"cd {shlex.quote(str(comms_root))} && bash scripts/monthly_hemlane_cdp.sh --month {shlex.quote(month)} --dry-run"


def hemlane_post_auth_action(auth_action: str | None = None) -> str:
    prefix = f"{auth_action.strip().rstrip(';')}; " if auth_action and auth_action.strip() else ""
    return (
        f"{prefix}run `{POST_AUTH_RESUME_COMMAND}`. "
        "It refreshes Hemlane rent-roll evidence, monthly dry-run readiness, and EOD reporting while keeping owner email, Lofty PM publish, and guarded live writes disabled."
    )


def hemlane_preflight_needs_open_tab(hemlane_cdp_preflight: dict) -> bool:
    return (
        hemlane_cdp_preflight.get("status") == "review"
        and hemlane_cdp_preflight.get("cdp_available") is True
        and count(hemlane_cdp_preflight.get("hemlane_tab_count")) == 0
        and count(hemlane_cdp_preflight.get("login_tab_count")) == 0
        and count(hemlane_cdp_preflight.get("rent_roll_tab_count")) == 0
    )


def hemlane_open_tab_action() -> str:
    return hemlane_post_auth_action("Open Hemlane rent-roll tab; solve CAPTCHA only if shown")


def hemlane_login_screen_recovery_action(attempts: int | None = None, *, captcha_conditional: bool = False) -> str:
    attempt_suffix = f" ({attempts} tries)" if attempts else ""
    captcha_clause = "; solve reCAPTCHA only if still shown" if captcha_conditional else ""
    return (
        f"Hard refresh or close/open the Hemlane rent-roll tab{attempt_suffix}{captcha_clause}; "
        f"authenticate only if still redirected, then run `{POST_AUTH_RESUME_COMMAND}`. "
        "It refreshes Hemlane rent-roll evidence, monthly dry-run readiness, and EOD reporting while keeping owner email, Lofty PM publish, and guarded live writes disabled."
    )


def hemlane_visible_login_after_recovery_action(attempts: int | None = None) -> str:
    attempt_clause = f"; auto recovery tried {attempts}x" if attempts else "; auto recovery done"
    return hemlane_post_auth_action(f"Finish Hemlane login/CAPTCHA{attempt_clause}")


def hemlane_bitwarden_submitted(*reports: dict) -> bool:
    for report in reports:
        if not isinstance(report, dict):
            continue
        bitwarden_login = report.get("bitwarden_login") if isinstance(report.get("bitwarden_login"), dict) else {}
        if (
            report.get("bitwarden_login_submit_ok") is True
            or report.get("hemlane_capture_bitwarden_login_submit_ok") is True
            or bitwarden_login.get("submit_ok") is True
        ):
            return True
    return False


def hemlane_recaptcha_after_bitwarden_action() -> str:
    return hemlane_post_auth_action("Solve Hemlane reCAPTCHA / finish login in the visible tab (Bitwarden credentials already submitted)")


def hemlane_capture_recaptcha_required(report: dict) -> bool:
    if report.get("bitwarden_login_recaptcha_error") is True:
        return True
    values = " ".join(
        str(report.get(key) or "")
        for key in (
            "hemlane_capture_issue",
            "issue",
            "manual_auth_reason",
            "manual_auth_blocker",
            "error",
            "reason",
        )
    ).lower()
    return "recaptcha" in values


def hemlane_capture_login_required(report: dict) -> bool:
    if report.get("manual_auth_required") is not True:
        return False
    values = " ".join(
        str(report.get(key) or "")
        for key in (
            "issue",
            "manual_auth_reason",
            "manual_auth_blocker",
            "error",
            "reason",
        )
    ).lower()
    return "login" in values or "auth" in values or "redirect" in values


def hemlane_capture_attempt_count(report: dict) -> int:
    return count(
        report.get("login_recovery_try_count")
        if "login_recovery_try_count" in report
        else report.get("login_recovery_attempt_count")
    )


def hemlane_capture_recovery_exhausted(report: dict) -> bool:
    return (
        report.get("login_recovery_exhausted") is True
        or report.get("automated_browser_recovery_complete") is True
        or report.get("manual_auth_phase") == "after_browser_recovery"
    )


def hemlane_aux_next_action(capture: dict, source: dict, preflight: dict) -> object:
    if hemlane_preflight_needs_open_tab(preflight) and first_report_newer(preflight, capture, source):
        return hemlane_open_tab_action()
    preflight_action = str(preflight.get("next_action") or "").strip()
    preflight_at_login_after_recovery = (
        preflight.get("status") == "review"
        and preflight.get("cdp_available") is True
        and count(preflight.get("login_tab_count")) > 0
        and count(preflight.get("logged_in_tab_count")) == 0
        and preflight.get("login_recovery_opened_rent_roll") is True
    )
    if (
        (preflight_at_login_after_recovery or "Auth Hemlane visible tab" in preflight_action)
        and first_report_newer(preflight, capture, source)
    ):
        attempts = count(
            preflight.get("login_recovery_try_count")
            if "login_recovery_try_count" in preflight
            else preflight.get("login_recovery_attempt_count")
        )
        return hemlane_visible_login_after_recovery_action(attempts)
    if hemlane_capture_recaptcha_required(capture) and hemlane_bitwarden_submitted(capture, source):
        return hemlane_recaptcha_after_bitwarden_action()
    if hemlane_capture_login_required(capture) and hemlane_capture_recovery_exhausted(capture):
        attempts = hemlane_capture_attempt_count(capture) or count(
            preflight.get("login_recovery_try_count")
            if "login_recovery_try_count" in preflight
            else preflight.get("login_recovery_attempt_count")
        )
        return hemlane_login_screen_recovery_action(attempts)
    if "Auth Hemlane visible tab" in preflight_action:
        attempts = count(
            preflight.get("login_recovery_try_count")
            if "login_recovery_try_count" in preflight
            else preflight.get("login_recovery_attempt_count")
        )
        return hemlane_visible_login_after_recovery_action(attempts)
    return (
        capture.get("next_action")
        or capture.get("manual_auth_next_action")
        or capture.get("rerun_command")
        or source.get("hemlane_capture_next_action")
        or source.get("next_action")
        or preflight.get("login_recovery_action")
        or preflight.get("next_action")
    )


def owner_email_active_property_proof(owner_email_send_guard: dict) -> dict:
    proof = {
        "manual_exclusions_ok": owner_email_send_guard.get("manual_exclusions_ok") is True,
        "yhome_transition_guard_ok": owner_email_send_guard.get("yhome_transition_guard_ok") is True,
        "yhome_transition_guard_column_b_rule_ok": owner_email_send_guard.get("yhome_transition_guard_column_b_rule_ok") is True,
        "yhome_transition_guard_column_b_header": owner_email_send_guard.get("yhome_transition_guard_column_b_header"),
        "yhome_transition_guard_column_b_marker_count": count(owner_email_send_guard.get("yhome_transition_guard_column_b_marker_count")),
        "active_property_policy_mentions_yhome": owner_email_send_guard.get("active_property_policy_mentions_yhome") is True,
        "active_property_policy_mentions_manual_exclusions": owner_email_send_guard.get("active_property_policy_mentions_manual_exclusions") is True,
        "excluded_owner_email_candidate_count": count(owner_email_send_guard.get("excluded_owner_email_candidate_count")),
    }
    proof["ok"] = (
        proof["manual_exclusions_ok"]
        and proof["yhome_transition_guard_ok"]
        and proof["yhome_transition_guard_column_b_rule_ok"]
        and proof["active_property_policy_mentions_yhome"]
        and proof["active_property_policy_mentions_manual_exclusions"]
        and proof["excluded_owner_email_candidate_count"] == 0
    )
    return proof


def section_status_count(records: list[dict], section: str) -> dict[str, int]:
    statuses = Counter()
    for record in records:
        status = ((record.get(section) or {}).get("status")) or "missing"
        statuses[str(status)] += 1
    return dict(sorted(statuses.items()))


def section_status_is_blocking(status: object) -> bool:
    text = str(status or "").strip()
    return bool(text) and text not in {"ok", "ready", "applied", "already_applied", "skipped_no_candidate"} and not text.startswith(("skipped_", "excluded_"))


LIVE_FINANCIAL_CAPTURE_READY_STATUSES = {
    "guard_ok",
    "guard_ok_live_distribution",
    "guard_ok_no_distribution_target",
    "needs_reconcile",
}


def live_capture_registered(report: dict) -> bool:
    target_count = count(report.get("target_count"))
    register_count = count(report.get("register_count") or report.get("registered_count"))
    if target_count > 0 and register_count >= target_count:
        return True
    records = [record for record in (report.get("records") or []) if isinstance(record, dict)]
    registered_records = [
        record for record in records
        if str(record.get("status") or "").strip() in LIVE_FINANCIAL_CAPTURE_READY_STATUSES
    ]
    return target_count > 0 and len(registered_records) >= target_count


def live_capture_unverified_target_labels(report: dict, limit: int = 4) -> list[str]:
    records = [record for record in (report.get("records") or []) if isinstance(record, dict)]
    labels: list[str] = []
    for record in records:
        if str(record.get("status") or "").strip().startswith("guard_ok"):
            continue
        label = str(record.get("property_name") or property_name(record) or "").strip()
        property_id = str(record.get("lofty_property_id") or "").strip()
        if property_id:
            label = f"{label} ({property_id})" if label else property_id
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def format_financial_mismatch_value(value: object) -> str:
    if value is None:
        return "missing"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    text = str(value).strip()
    return text or "missing"


def live_financial_mismatch_target_labels(report: dict, limit: int = 4) -> list[str]:
    records = [record for record in (report.get("records") or []) if isinstance(record, dict)]
    labels: list[str] = []
    checks = [
        ("cash_flow_ok", "cash_flow", "actual", "expected"),
        ("coc_ok", "coc", "actual_coc", "expected_coc"),
        ("projected_rental_yield_ok", "yield", "actual_projected_rental_yield", "expected_projected_rental_yield"),
        ("is_occupied_ok", "occupied", "actual_is_occupied", "expected_is_occupied"),
        ("monthly_loan_repayment_ok", "loan_pmt", "actual_monthly_loan_repayment", "expected_monthly_loan_repayment"),
    ]
    for record in records:
        if str(record.get("status") or "").strip() != "blocked_live_distribution_mismatch":
            continue
        verify = record.get("live_distribution_verify") if isinstance(record.get("live_distribution_verify"), dict) else {}
        label = str(record.get("property_name") or property_name(record) or "").strip()
        property_id = str(record.get("lofty_property_id") or "").strip()
        mismatches: list[str] = []
        for ok_key, field, actual_key, expected_key in checks:
            if verify.get(ok_key) is False:
                mismatches.append(
                    f"{field} {format_financial_mismatch_value(verify.get(actual_key))}->{format_financial_mismatch_value(verify.get(expected_key))}"
                )
        if verify.get("current_loan_ok") is False:
            if verify.get("current_loan_warning"):
                mismatches.append("current_loan backend-readback-warning")
            else:
                mismatches.append(
                    "current_loan "
                    f"{format_financial_mismatch_value(verify.get('actual_current_loan'))}->{format_financial_mismatch_value(verify.get('expected_current_loan'))}"
                )
        if not mismatches:
            mismatches.append(str(record.get("status") or "needs reconcile"))
        if property_id:
            label = f"{label} ({property_id})" if label else property_id
        if label:
            labels.append(f"{label}: {', '.join(mismatches[:3])}")
        if len(labels) >= limit:
            break
    return labels


def report_artifact_path(value: object, report_path: Path | None = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if path.exists() or report_path is None:
        return str(path)
    local = report_path.parent / path.name
    if local.exists():
        return str(local)
    return str(path)


def verified_listing_cleanup_summary(
    queue_report: dict,
    dry_run_verify_report: dict,
    apply_preflight_report: dict,
    queue_report_path: Path | None = None,
) -> dict:
    ready_count = count(queue_report.get("ready_listing_cleanup_count"))
    if ready_count <= 0:
        return {"verified": False, "ready_count": ready_count, "reason": "no_ready_cleanup_records"}
    queue_digest = str(queue_report.get("ready_cleanup_idempotency_digest") or "").strip()
    dry_digest = str(dry_run_verify_report.get("ready_cleanup_idempotency_digest") or "").strip()
    queue_issue_count = count(queue_report.get("issue_count"))
    dry_issue_count = count(dry_run_verify_report.get("issue_count"))
    preflight_issue_count = count(apply_preflight_report.get("issue_count"))
    dry_verified_count = count(dry_run_verify_report.get("verified_record_count"))
    dry_ready_count = count(dry_run_verify_report.get("ready_listing_cleanup_count"))
    preflight_ready_count = count(apply_preflight_report.get("ready_listing_cleanup_count"))
    verified = (
        queue_report.get("status") == "review"
        and queue_issue_count == 0
        and dry_run_verify_report.get("status") == "ok"
        and dry_issue_count == 0
        and dry_ready_count == ready_count
        and dry_verified_count == ready_count
        and apply_preflight_report.get("status") == "ok"
        and preflight_issue_count == 0
        and preflight_ready_count == ready_count
        and (not queue_digest or not dry_digest or queue_digest == dry_digest)
    )
    reason = None
    if not verified:
        reason = "listing_cleanup_queue_not_fully_verified"
    return {
        "verified": verified,
        "reason": reason,
        "ready_count": ready_count,
        "ready_cleanup_csv": report_artifact_path(queue_report.get("ready_cleanup_csv"), queue_report_path),
        "dry_run_commands_file": report_artifact_path(queue_report.get("dry_run_commands_file"), queue_report_path),
        "live_apply_commands_requires_explicit_approval_file": report_artifact_path(
            queue_report.get("live_apply_commands_requires_explicit_approval_file"),
            queue_report_path,
        ),
        "ready_cleanup_idempotency_digest": queue_digest,
        "dry_run_verified_record_count": dry_verified_count,
        "apply_preflight_status": apply_preflight_report.get("status"),
    }


def live_capture_reconcile_action(report: dict, target: str, listing_cleanup_summary: dict | None = None) -> str:
    target_count = count(report.get("target_count"))
    check_ok_count = count(report.get("check_ok_count"))
    mismatch_count = count(report.get("mismatch_count"))
    if target_count > 0 and mismatch_count == 0:
        mismatch_count = max(target_count - check_ok_count, 0)
    check_detail = f" ({check_ok_count}/{target_count} checks pass" if target_count else ""
    if check_detail and mismatch_count:
        check_detail += f"; {mismatch_count} need reconcile"
    if check_detail:
        check_detail += ")"
    target_labels = live_capture_unverified_target_labels(report)
    if target == "FINANCIALS.md":
        financial_labels = live_financial_mismatch_target_labels(report)
        if financial_labels:
            target_labels = financial_labels
    target_detail = f" Targets: {'; '.join(target_labels)}." if target_labels else ""
    if target == "UPDATES.md" and listing_cleanup_summary and listing_cleanup_summary.get("verified"):
        ready_count = count(listing_cleanup_summary.get("ready_count"))
        remaining_count = max(mismatch_count - ready_count, 0)
        remaining_detail = (
            f" {remaining_count} live/local UPDATES.md mismatch remains after cleanup."
            if remaining_count == 1
            else f" {remaining_count} live/local UPDATES.md mismatches remain after cleanup."
            if remaining_count > 1
            else ""
        )
        digest = str(listing_cleanup_summary.get("ready_cleanup_idempotency_digest") or "").strip()
        digest_detail = f" with digest `{digest}`" if digest else ""
        live_apply = listing_cleanup_summary.get("live_apply_commands_requires_explicit_approval_file")
        dry_run = listing_cleanup_summary.get("dry_run_commands_file")
        ready_csv = listing_cleanup_summary.get("ready_cleanup_csv")
        return (
            f"{ready_count} live Lofty listing update fields are verified ready for cleaned-history repair{check_detail}; "
            f"dry-run verification and apply preflight are ok. Review `{ready_csv}` and `{dry_run}`, then apply only after explicit approval "
            f"using `{live_apply}`{digest_detail}. Owner email remains held until live listing guards reconcile.{remaining_detail} "
            f"Rerun `{SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}` after apply; this keeps email, Lofty PM publish, and guarded live writes disabled."
        )
    return (
        f"Registered live {target} snapshots exist{check_detail}; reconcile live/local {target} diffs in the approved monthly artifacts, "
        f"then rerun the safe monthly dry-run.{target_detail} Do not publish or email until checks pass. "
        "If a live apply returns Property not found, refresh the Lofty property-id mapping before retrying. "
        f"Run `{SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}` after fixes; this keeps email, Lofty PM publish, and guarded live writes disabled."
    )


def live_capture_next_action(report: dict, fallback_key: str, listing_cleanup_summary: dict | None = None) -> str:
    action = report.get("next_action") if isinstance(report.get("next_action"), dict) else {}
    summary = str(action.get("summary") or "").strip()
    auth_text = " ".join(
        str(part or "")
        for part in (
            action.get("status"),
            summary,
            report.get("issue"),
            report.get("error"),
            report.get("reason"),
        )
    ).lower()
    target = "UPDATES.md" if "live_update_capture" in fallback_key else "FINANCIALS.md"
    if live_capture_registered(report):
        return live_capture_reconcile_action(report, target, listing_cleanup_summary)
    safe_rerun = f"Run `{SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}` after auth; this keeps email, Lofty PM publish, and guarded live writes disabled."
    if "unauthorized" in auth_text or "code\":401" in auth_text or "httpcode\":401" in auth_text:
        return (
            "Hard-refresh or close/open Lofty property-owners tab; "
            f"authenticate only if still redirected, then rerun live {target} capture through the safe monthly dry-run. {safe_rerun}"
        )
    if summary:
        return f"{summary} {safe_rerun}"
    return f"{next_action(fallback_key)} {safe_rerun}"


def live_guard_next_action(section: str, live_report: dict, listing_cleanup_summary: dict | None = None) -> str:
    target = "UPDATES.md" if section == "updates" else "FINANCIALS.md"
    if live_capture_registered(live_report):
        return live_capture_reconcile_action(live_report, target, listing_cleanup_summary)
    return next_action(f"guard.{section}.failed")


def guarded_apply_summary(guarded_apply: dict, guard_audit: dict) -> dict:
    records = [record for record in (guarded_apply.get("records") or []) if isinstance(record, dict)]
    update_status_counts = section_status_count(records, "updates")
    financial_status_counts = section_status_count(records, "financials")
    active_record_count = 0
    excluded_record_count = 0
    skipped_record_count = 0
    blocking_update_count = 0
    blocking_financial_count = 0
    for record in records:
        statuses = [
            str(((record.get(section) or {}).get("status")) or "")
            for section in ("updates", "financials")
        ]
        if all(status.startswith("excluded_") for status in statuses):
            excluded_record_count += 1
        elif any(status.startswith("skipped_") for status in statuses):
            skipped_record_count += 1
        else:
            active_record_count += 1
        if section_status_is_blocking(statuses[0]):
            blocking_update_count += 1
        if section_status_is_blocking(statuses[1]):
            blocking_financial_count += 1
    audit_records = [record for record in (guard_audit.get("records") or []) if isinstance(record, dict)]
    audit_status_counts = {"updates": Counter(), "financials": Counter()}
    for record in audit_records:
        checks = record.get("checks") if isinstance(record.get("checks"), dict) else {}
        for section in ("updates", "financials"):
            status = ((checks.get(section) or {}).get("status")) or "missing"
            audit_status_counts[section][str(status)] += 1
    return {
        "status": guarded_apply.get("status"),
        "apply": guarded_apply.get("apply"),
        "record_count": count(guarded_apply.get("record_count") if "record_count" in guarded_apply else len(records)),
        "active_record_count": active_record_count,
        "excluded_record_count": excluded_record_count,
        "externally_excluded_property_count": count(
            guarded_apply.get("externally_excluded_property_count")
            if "externally_excluded_property_count" in guarded_apply
            else excluded_record_count
        ),
        "skipped_record_count": skipped_record_count,
        "excluded_total_property_count": count(
            guarded_apply.get("excluded_total_property_count")
            if "excluded_total_property_count" in guarded_apply
            else excluded_record_count + skipped_record_count
        ),
        "update_status_counts": update_status_counts,
        "financial_status_counts": financial_status_counts,
        "blocking_update_count": blocking_update_count,
        "blocking_financial_count": blocking_financial_count,
        "guard_failed_update_count": update_status_counts.get("guard_failed", 0),
        "guard_failed_financial_count": financial_status_counts.get("guard_failed", 0),
        "guard_audit_status": guard_audit.get("status"),
        "guard_audit_issue_count": count(guard_audit.get("issue_count")),
        "guard_audit_record_count": len(audit_records),
        "guard_audit_status_counts": {key: dict(sorted(value.items())) for key, value in audit_status_counts.items()},
    }


def iso_age_hours(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600, 3)


def report_timestamp(report: dict) -> datetime | None:
    for key in ("generated_at", "checked_at", "hemlane_capture_generated_at"):
        raw = str(report.get(key) or "").strip()
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def first_report_newer(primary: dict, *others: dict) -> bool:
    primary_ts = report_timestamp(primary)
    if primary_ts is None:
        return False
    other_timestamps = [timestamp for timestamp in (report_timestamp(other) for other in others) if timestamp is not None]
    return bool(other_timestamps) and all(primary_ts >= timestamp for timestamp in other_timestamps)


def fresh_generated_at(report: dict, max_age_hours: float = LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS) -> bool:
    if not str(report.get("generated_at") or "").strip():
        return True
    age_hours = iso_age_hours(report.get("generated_at"))
    return age_hours is not None and -1 <= age_hours <= max_age_hours


def required_fresh_generated_at(report: dict, max_age_hours: float) -> bool:
    if not str(report.get("generated_at") or "").strip():
        return False
    return fresh_generated_at(report, max_age_hours)


def local_model_preflight_ok(report: dict) -> bool:
    direct_smoke = report.get("direct_smoke") if isinstance(report.get("direct_smoke"), dict) else {}
    finance_smoke = report.get("finance_contract_smoke") if isinstance(report.get("finance_contract_smoke"), dict) else {}
    contract = report.get("validation_contract") if isinstance(report.get("validation_contract"), dict) else {}
    scope = report.get("model_execution_scope") if isinstance(report.get("model_execution_scope"), dict) else {}
    contract_scope = contract.get("model_execution_scope") if isinstance(contract.get("model_execution_scope"), dict) else {}
    policy = report.get("small_model_execution_policy") if isinstance(report.get("small_model_execution_policy"), dict) else {}
    digest = str(report.get("validation_digest") or "")
    expected_finance_response = str(report.get("finance_contract_expected_response") or "").strip()
    deterministic_scope_ok = (
        report.get("small_model_execution_allowed") is False
        and report.get("small_model_pipeline_execution_allowed") is False
        and report.get("small_model_task_scoped_execution_allowed") is True
        and report.get("small_model_financial_authority") is False
        and report.get("small_model_live_side_effects_allowed") is False
        and scope.get("deterministic_only") is True
        and scope.get("pipeline_execution_allowed") is False
        and scope.get("allowed_task_class") == EXPECTED_LOCAL_MODEL_TASK_CLASS
        and scope.get("model_financial_authority") is False
        and scope.get("autonomous_financial_execution_allowed") is False
        and scope.get("live_side_effects_allowed") is False
        and scope.get("requires_external_deterministic_validation") is True
        and policy.get("pipeline_execution_allowed") is False
        and policy.get("model_financial_authority") is False
        and policy.get("autonomous_financial_execution_allowed") is False
        and policy.get("live_side_effects_allowed") is False
        and policy.get("permitted_task_class") == EXPECTED_LOCAL_MODEL_TASK_CLASS
        and policy.get("requires_external_deterministic_validation") is True
        and contract.get("model_scope_deterministic") is True
        and contract.get("model_pipeline_execution_denied") is True
        and contract.get("model_financial_authority_denied") is True
        and contract.get("model_live_side_effects_denied") is True
        and contract.get("model_external_validation_required") is True
        and contract_scope.get("deterministic_only") is True
        and contract_scope.get("pipeline_execution_allowed") is False
        and contract_scope.get("model_financial_authority") is False
        and contract_scope.get("live_side_effects_allowed") is False
    )
    strict_ok = (
        report.get("status") == "ok"
        and int(report.get("issue_count") or 0) == 0
        and report.get("model") == EXPECTED_LOCAL_MODEL
        and report.get("model_id") == EXPECTED_LOCAL_MODEL_ID
        and report.get("model_available") is True
        and direct_smoke.get("attempted") is True
        and direct_smoke.get("ok") is True
        and direct_smoke.get("response") == "BASELANE_MODEL_OK"
        and finance_smoke.get("attempted") is True
        and finance_smoke.get("ok") is True
        and finance_smoke.get("response") == expected_finance_response
        and bool(expected_finance_response)
        and contract.get("direct_smoke_ok") is True
        and contract.get("direct_smoke_response") == "BASELANE_MODEL_OK"
        and contract.get("finance_contract_smoke_ok") is True
        and contract.get("finance_contract_response") == expected_finance_response
        and deterministic_scope_ok
        and bool(digest)
        and fresh_generated_at(report)
    )
    if strict_ok:
        return True
    fallback_smokes = report.get("fallback_smokes") if isinstance(report.get("fallback_smokes"), list) else []
    fallback_ok = any(
        isinstance(item, dict)
        and item.get("attempted") is True
        and item.get("ok") is True
        and item.get("response") == "BASELANE_MODEL_OK"
        and str(item.get("model_id") or "").strip()
        for item in fallback_smokes
    )
    return (
        report.get("model") == EXPECTED_LOCAL_MODEL
        and report.get("provider") == EXPECTED_LOCAL_PROVIDER
        and report.get("model_id") == EXPECTED_LOCAL_MODEL_ID
        and report.get("configured_model_present") is True
        and report.get("selected_endpoint_from_config") is True
        and report.get("model_available") is True
        and report.get("local_model_operational") is True
        and report.get("fallback_smoke_ok") is True
        and deterministic_scope_ok
        and fallback_ok
        and finance_smoke.get("attempted") is True
        and finance_smoke.get("ok") is True
        and finance_smoke.get("response") == expected_finance_response
        and bool(expected_finance_response)
        and contract.get("finance_contract_smoke_ok") is True
        and contract.get("finance_contract_response") == expected_finance_response
        and bool(re.fullmatch(r"[0-9a-f]{64}", digest))
        and fresh_generated_at(report)
    )


def expected_statement_target(run_month: str) -> tuple[int | None, int | None]:
    raw = str(run_month or "").strip()
    if not raw:
        return None, None
    try:
        year_text, month_text = raw.split("-", 1)
        year = int(year_text)
        month = int(month_text)
    except ValueError:
        return None, None
    return year, month


def statement_target_matches(report: dict, run_month: str) -> bool:
    expected_year, expected_month = expected_statement_target(run_month)
    if expected_year is None or expected_month is None:
        return False
    return count(report.get("target_year")) == expected_year and count(report.get("target_month")) == expected_month


def is_portfolio_upstream_blocker(blocker_class: object) -> bool:
    text = str(blocker_class or "")
    return text.startswith(
        (
            "monthly_comms.rent_roll",
            "lofty_cdp_preflight",
            "data_quality.",
            "operational.source_cash_balance",
            "operational.first_day_pm_fee",
            "operational.weekly_cf_review_gate",
            "operational.monthly_bank_statement",
            "operational.local_model_preflight",
            "operational.public_path_guard",
            "operational.tenant_ledger_folder_guard",
            "operational.monthly_run",
            "operational.daily_sync",
        )
    )


def normalize_blocker(blocker: dict) -> dict:
    normalized = dict(blocker)
    blocker_class = str(normalized.get("class") or normalized.get("id") or "").strip()
    blocker_text = str(normalized.get("blocker") or blocker_class).strip()
    normalized.setdefault("id", blocker_class or "none")
    summary_by_class = {
        "monthly_comms.rent_roll_gap_review.review": "Hemlane rent-roll evidence is stale or blocked; hold owner email and Lofty PM publish.",
        "monthly_comms.rent_roll_gap_approval_coverage.review": "Rent-roll gap approvals are incomplete; hold owner email and Lofty PM publish.",
        "operational.monthly_bank_statement.not_ok": "Monthly Baselane bank statements are not captured and verified yet.",
        "operational.local_model_preflight.not_ok": "Local qwen model preflight is not current and passing.",
        "operational.monthly_run.not_ok": "Monthly close run is not OK.",
        "operational.monthly_run.failed": "Monthly close run failed.",
        "operational.monthly_run_disk_space_preflight.not_ok": "Low local disk space blocks monthly close.",
        "operational.daily_sync_disk_space_preflight.not_ok": "Low local disk space blocks daily Baselane sync.",
        "operational.daily_sync_report.not_ok": "Canonical Baselane daily sync report is not OK.",
        "operational.public_path_guard.not_ok": "Dropbox public-path guard found non-canonical owner statement/update paths.",
        "operational.tenant_ledger_folder_guard.not_ok": "Tenant ledger folder guard found misplaced or non-canonical ledger files.",
    }
    if not normalized.get("summary"):
        normalized["summary"] = summary_by_class.get(blocker_class) or blocker_text or None
    return normalized


def safe_candidate_approval_review_is_rent_roll_hold_only(report: dict) -> bool:
    if report.get("status") != "review":
        return False
    issues = [str(issue or "") for issue in (report.get("issues") or [])]
    if not issues or any(not issue.startswith("rent_roll_source_") for issue in issues):
        return False
    property_count = count(report.get("property_count"))
    if property_count <= 0:
        return False
    status_counts = report.get("status_counts") if isinstance(report.get("status_counts"), dict) else {}
    financial_complete = count(report.get("approved_financial_count")) >= property_count
    update_blocked = count(status_counts.get("update.blocked")) >= property_count
    no_bad_financial_statuses = not any(
        str(key).startswith("financial.") and str(key) not in {"financial.approved", "financial.already_approved", "financial.would_approve"}
        for key, value in status_counts.items()
        if count(value) > 0
    )
    return financial_complete and update_blocked and no_bad_financial_statuses


def default_root() -> Path:
    env_root = os.environ.get("WORKSPACE_ROOT")
    if env_root:
        return Path(env_root)
    cwd = Path.cwd()
    if (cwd / "reports").is_dir() and (cwd / "scripts").is_dir():
        return cwd
    return Path(__file__).absolute().parents[1]


def build_report(root: Path, report_path: Path, markdown_path: Path) -> dict:
    daily_run_path = root / "reports" / "baselane_daily_run_report.json"
    daily_sync_report_path = root / "reports" / "baselane_daily_sync_report.json"
    daily_disk_preflight_path = root / "reports" / "baselane_daily_disk_space_preflight_report.json"
    sync_report_path = root / "reports" / "baselane_sync_cdp_report.json"
    export_guard_path = root / "reports" / "baselane_export_guard_last.json"
    weekly_file_updates_path = root / "reports" / "baselane_weekly_file_updates_run_report.json"
    weekly_unprocessed_path = root / "reports" / "baselane_weekly_unprocessed_report.json"
    weekly_cf_sync_path = root / "reports" / "baselane_weekly_cf_statement_sync_report.json"
    daily_source_cash_balance_path = root / "reports" / "baselane_daily_source_cash_balance_report.json"
    source_cash_reconciliation_actions_path = root / "reports" / "baselane_source_cash_reconciliation_actions.json"
    weekly_cf_gate_path = root / "reports" / "baselane_weekly_cf_review_gate.json"
    cf_balance_sheet_consistency_path = root / "reports" / "baselane_cf_balance_sheet_consistency_audit.json"
    yhome_operating_cash_apply_verify_path = root / "reports" / "yhome_operating_cash_apply_verify_report.json"
    monthly_statements_gate_path = root / "reports" / "baselane_monthly_statements_idempotent_report.json"
    monthly_statements_download_path = root / "reports" / "baselane_statements_download_report.json"
    obie_insurance_cleanup_path = root / "reports" / "obie_cash_basis_insurance_cleanup.json"
    first_day_pm_fee_audit_path = root / "reports" / "baselane_first_day_pm_fee_audit.json"
    first_day_pm_fee_quarantine_path = root / "reports" / "baselane_first_day_pm_fee_quarantine_report.json"
    first_day_pm_fee_cleanup_path = root / "reports" / "baselane_first_day_pm_fee_source_cleanup_plan.json"
    first_day_pm_fee_cleanup_actions_path = root / "reports" / "baselane_first_day_pm_fee_source_cleanup_actions.csv"
    ecogl_autonomy_path = root / "reports" / "baselane_ecogl_data_quality_autonomy.json"
    ecogl_source_fix_path = root / "reports" / "baselane_ecogl_source_fix_plan.json"
    ecogl_source_fix_actions_path = root / "reports" / "baselane_ecogl_source_fix_actions.csv"
    ecogl_source_fix_corrections_path = root / "reports" / "baselane_ecogl_source_fix_corrections.csv"
    ecogl_source_fix_corrections_report_path = root / "reports" / "baselane_ecogl_source_fix_corrections.json"
    ecogl_source_fix_verifier_path = root / "reports" / "baselane_ecogl_source_fix_verifier.json"
    ecogl_source_fix_approval_path = root / "reports" / "baselane_ecogl_source_fix_approval.json"
    ecogl_source_fix_approved_corrections_path = root / "reports" / "baselane_ecogl_source_fix_approved_corrections.csv"
    ecogl_source_fix_correction_validation_path = root / "reports" / "baselane_ecogl_source_fix_correction_validation.json"
    ecogl_source_fix_correction_validation_csv_path = root / "reports" / "baselane_ecogl_source_fix_correction_validation.csv"
    ecogl_source_fix_apply_plan_path = root / "reports" / "baselane_ecogl_source_fix_apply_plan.json"
    ecogl_source_fix_apply_plan_csv_path = root / "reports" / "baselane_ecogl_source_fix_apply_plan.csv"
    ecogl_source_fix_action_queue_path = root / "reports" / "baselane_ecogl_source_fix_action_queue.json"
    ecogl_source_fix_evidence_path = root / "reports" / "baselane_ecogl_source_fix_evidence.json"
    scheduler_audit_path = root / "reports" / "baselane_scheduler_audit_report.json"
    local_model_preflight_path = root / "reports" / "baselane_local_model_preflight_report.json"
    public_path_guard_path = root / "reports" / "lofty_public_path_guard_report.json"
    tenant_ledger_guard_path = root / "reports" / "lofty_tenant_ledger_folder_guard_report.json"
    guarded_apply_path = root / "reports" / "baselane_financials_monthly_guarded_apply.json"
    owner_review_gate_path = root / "reports" / "baselane_monthly_owner_review_gate.json"
    review_safety_scan_path = root / "reports" / "baselane_financials_monthly_review_safety_scan.json"
    review_candidate_packet_path = root / "reports" / "baselane_financials_monthly_review_candidate_packet.json"
    guard_audit_path = root / "reports" / "baselane_financials_monthly_guard_audit.json"
    bootstrap_path = root / "reports" / "baselane_financials_monthly_doc_bootstrap.json"
    hemlane_cdp_preflight_path = root / "reports" / "hemlane_cdp_preflight_report.json"
    lofty_cdp_preflight_path = root / "reports" / "lofty_cdp_preflight_report.json"
    live_capture_path = root / "reports" / "baselane_financials_monthly_live_update_capture.json"
    live_financial_capture_path = root / "reports" / "baselane_financials_monthly_live_financial_capture.json"
    listing_cleanup_queue_path = root / "reports" / "lofty_listing_update_cleanup_queue.json"
    listing_cleanup_dry_run_verify_path = root / "reports" / "lofty_listing_cleanup_dry_run_verify.json"
    listing_cleanup_apply_preflight_path = root / "reports" / "lofty_listing_update_cleanup_queue.live-apply-preflight.json"
    safe_candidate_approval_path = root / "reports" / "baselane_financials_monthly_safe_candidate_approval.json"
    owner_email_path = root / "reports" / "baselane_financials_monthly_owner_email_diagnostic.json"
    owner_email_send_guard_path = root / "reports" / "baselane_monthly_owner_email_send_guard.json"
    lofty_pm_publish_path = root / "reports" / "baselane_financials_monthly_lofty_pm_publish.json"
    lofty_financial_patch_readiness_path = root / "reports" / "lofty_financial_patch_readiness.json"
    lofty_pm_runtime_map_path = root / "reports" / "baselane_financials_monthly_lofty_pm_runtime_map.json"
    monthly_run_path = root / "reports" / "baselane_financials_monthly_run_report.json"
    monthly_finance_truth_refresh_path = root / "reports" / "baselane_monthly_finance_truth_refresh.json"
    transfer_reconciliation_path = root / "reports" / "baselane_lofty_transfer_requirements.json"
    pipeline_candidate_coverage_path = root / "reports" / "baselane_monthly_pipeline_candidate_coverage_audit.json"

    daily_run = read_json(daily_run_path)
    daily_sync_report = read_json(daily_sync_report_path)
    daily_disk_preflight = read_json(daily_disk_preflight_path)
    sync_report = read_json(sync_report_path)
    export_guard = read_json(export_guard_path)
    weekly_file_updates = read_json(weekly_file_updates_path)
    weekly_unprocessed = read_json(weekly_unprocessed_path)
    weekly_cf_sync = read_json(weekly_cf_sync_path)
    daily_source_cash_balance = read_json(daily_source_cash_balance_path)
    source_cash_reconciliation_actions = read_json(source_cash_reconciliation_actions_path)
    weekly_cf_gate = read_json(weekly_cf_gate_path)
    cf_balance_sheet_consistency = read_json(cf_balance_sheet_consistency_path)
    yhome_operating_cash_apply_verify = read_json(yhome_operating_cash_apply_verify_path)
    monthly_statements_gate = read_json(monthly_statements_gate_path)
    monthly_statements_download = read_json(monthly_statements_download_path)
    obie_insurance_cleanup = read_json(obie_insurance_cleanup_path)
    first_day_pm_fee_audit = read_json(first_day_pm_fee_audit_path)
    first_day_pm_fee_quarantine = read_json(first_day_pm_fee_quarantine_path)
    first_day_pm_fee_cleanup = read_json(first_day_pm_fee_cleanup_path)
    ecogl_autonomy = read_json(ecogl_autonomy_path)
    ecogl_source_fix = read_json(ecogl_source_fix_path)
    ecogl_source_fix_corrections = read_json(ecogl_source_fix_corrections_report_path)
    ecogl_source_fix_verifier = read_json(ecogl_source_fix_verifier_path)
    ecogl_source_fix_approval = read_json(ecogl_source_fix_approval_path)
    ecogl_source_fix_correction_validation = read_json(ecogl_source_fix_correction_validation_path)
    ecogl_source_fix_apply_plan = read_json(ecogl_source_fix_apply_plan_path)
    ecogl_source_fix_action_queue = read_json(ecogl_source_fix_action_queue_path)
    scheduler_audit = read_json(scheduler_audit_path)
    local_model_preflight = read_json(local_model_preflight_path)
    public_path_guard = read_json(public_path_guard_path)
    tenant_ledger_guard = read_json(tenant_ledger_guard_path)
    guarded_apply = read_json(guarded_apply_path)
    guarded_apply_fresh = fresh_generated_at(guarded_apply, MONTHLY_GUARDED_APPLY_MAX_AGE_HOURS)
    owner_review_gate = read_json(owner_review_gate_path)
    owner_guard_workflow = owner_review_gate.get("guard_workflow_coverage") if isinstance(owner_review_gate.get("guard_workflow_coverage"), dict) else {}
    review_safety_scan = read_json(review_safety_scan_path)
    review_candidate_packet = read_json(review_candidate_packet_path)
    guard_audit = read_json(guard_audit_path)
    guarded_apply_counts = guarded_apply_summary(guarded_apply, guard_audit)
    bootstrap = read_json(bootstrap_path)
    hemlane_cdp_preflight = read_json(hemlane_cdp_preflight_path)
    lofty_cdp_preflight = read_json(lofty_cdp_preflight_path)
    live_capture = read_json(live_capture_path)
    live_financial_capture = read_json(live_financial_capture_path)
    listing_cleanup_queue = read_json(listing_cleanup_queue_path)
    listing_cleanup_dry_run_verify = read_json(listing_cleanup_dry_run_verify_path)
    listing_cleanup_apply_preflight = read_json(listing_cleanup_apply_preflight_path)
    listing_cleanup_summary = verified_listing_cleanup_summary(
        listing_cleanup_queue,
        listing_cleanup_dry_run_verify,
        listing_cleanup_apply_preflight,
        listing_cleanup_queue_path,
    )
    safe_candidate_approval = read_json(safe_candidate_approval_path)
    owner_email = read_json(owner_email_path)
    owner_email_send_guard = read_json(owner_email_send_guard_path)
    owner_email_active_proof = owner_email_active_property_proof(owner_email_send_guard)
    lofty_pm_publish = read_json(lofty_pm_publish_path)
    lofty_pm_publish_fresh = fresh_generated_at(lofty_pm_publish, LOFTY_PM_PUBLISH_MAX_AGE_HOURS)
    lofty_financial_patch_readiness = read_json(lofty_financial_patch_readiness_path)
    lofty_pm_runtime_map = read_json(lofty_pm_runtime_map_path)
    monthly_run = read_json(monthly_run_path)
    monthly_finance_truth_refresh = read_json(monthly_finance_truth_refresh_path)
    transfer_reconciliation = read_json(transfer_reconciliation_path)
    pipeline_candidate_coverage = read_json(pipeline_candidate_coverage_path)
    pipeline_candidate_coverage = reconcile_pipeline_candidate_coverage(
        pipeline_candidate_coverage,
        transfer_reconciliation,
        transfer_reconciliation_path,
    )
    run_month = os.environ.get("RUN_MONTH") or str(monthly_run.get("run_month") or "")
    local_model_direct_smoke = local_model_preflight.get("direct_smoke") if isinstance(local_model_preflight.get("direct_smoke"), dict) else {}
    local_model_finance_smoke = (
        local_model_preflight.get("finance_contract_smoke")
        if isinstance(local_model_preflight.get("finance_contract_smoke"), dict)
        else {}
    )
    local_model_exact_ok = local_model_preflight_ok(local_model_preflight)
    if not run_month:
        run_month = ""
    cf_no_gl_property_match_path = (
        root / "reports" / "cf_statement_sync" / f"no_gl_property_match_{run_month}.json"
        if run_month
        else root / "reports" / "cf_statement_sync" / "no_gl_property_match.json"
    )
    cf_no_gl_property_match = read_json(cf_no_gl_property_match_path)
    if cf_no_gl_property_match.get("status") in {"missing", "unreadable"}:
        no_gl_candidates = sorted((root / "reports" / "cf_statement_sync").glob("no_gl_property_match_*.json"), reverse=True)
        if no_gl_candidates:
            cf_no_gl_property_match_path = no_gl_candidates[0]
            cf_no_gl_property_match = read_json(cf_no_gl_property_match_path)
    comms_root = comms_root_for(root.parent)
    comms_updates_dir = comms_root / "updates"
    rent_roll_gap_review_path = comms_updates_dir / f"{run_month}-rent-roll-gap-review.json" if run_month else comms_updates_dir / "rent-roll-gap-review.json"
    rent_roll_source_path = comms_updates_dir / f"{run_month}-rent-roll-source.json" if run_month else comms_updates_dir / "rent-roll-source.json"
    hemlane_cdp_capture_path = comms_updates_dir / f"{run_month}-hemlane-cdp-capture-report.json" if run_month else comms_updates_dir / "hemlane-cdp-capture-report.json"
    rent_roll_gap_review = read_json(rent_roll_gap_review_path)
    rent_roll_source = read_json(rent_roll_source_path)
    hemlane_cdp_capture = read_json(hemlane_cdp_capture_path)
    rent_roll_gap_review_status = rent_roll_gap_review.get("status")
    safe_candidate_approval_status = safe_candidate_approval.get("status")
    safe_candidate_approval_duplicate_rent_roll_hold = (
        safe_candidate_approval_review_is_rent_roll_hold_only(safe_candidate_approval)
        and rent_roll_gap_review_status in {"failed", "review", "missing", "unreadable"}
    )
    fallback_missing_draft_keys = fallback_missing_draft_property_keys(review_candidate_packet)
    fallback_missing_draft_count = fallback_missing_draft_record_count(review_candidate_packet)
    collapsed_missing_monthly_draft_count = 0
    owner_gate_summary = owner_review_gate.get("summary") if isinstance(owner_review_gate.get("summary"), dict) else {}
    owner_gate_updates_deferred_by_rent_roll = (
        owner_gate_summary.get("safe_update_reviews_deferred_by_rent_roll") is True
        and count(owner_gate_summary.get("pending_update_review_count")) == 0
    )
    collapsed_needs_reviewed_entry_count = 0
    rent_roll_gap_queue_csv = rent_roll_gap_review.get("queue_csv") or str(rent_roll_gap_review_path.with_suffix(".csv"))
    rent_roll_target_gaps = rent_roll_target_gap_summary(
        rent_roll_gap_review,
        Path(rent_roll_gap_queue_csv),
        lofty_pm_runtime_map,
    )
    rent_roll_target_scoped = rent_roll_target_gaps.get("target_scoped") is True
    rent_roll_target_pending_gap_count = count(rent_roll_target_gaps.get("target_pending_gap_count"))
    rent_roll_pending_gap_count_for_block = (
        rent_roll_target_pending_gap_count
        if rent_roll_target_scoped
        else count(rent_roll_gap_review.get("pending_gap_count"))
    )

    blockers = []
    counter = Counter()
    by_property: dict[str, dict] = {}
    ecogl_exception_count = count(ecogl_autonomy.get("exception_count"))
    autonomy_exception_count = ecogl_exception_count
    ecogl_source_fix_action_count = count(ecogl_source_fix.get("action_count"))
    ecogl_source_fix_remaining_count = count(
        ecogl_source_fix_corrections.get("remaining_count")
        if "remaining_count" in ecogl_source_fix_corrections
        else ecogl_source_fix_action_count
    )
    ecogl_source_fix_action_type_counts = (
        ecogl_source_fix.get("action_type_counts") if isinstance(ecogl_source_fix.get("action_type_counts"), dict) else {}
    )
    source_fix_queue_group_counts = (
        ecogl_source_fix_action_queue.get("group_counts")
        if isinstance(ecogl_source_fix_action_queue.get("group_counts"), dict)
        else {}
    )
    source_fix_needs_source_evidence_count = count(source_fix_queue_group_counts.get("needs_source_evidence"))
    source_fix_action_queue_current = ecogl_source_fix_action_queue.get("status") not in {"missing", "unreadable"}
    if source_fix_action_queue_current:
        source_fix_queue_actionable_count = (
            count(ecogl_source_fix_action_queue.get("ready_to_apply_count"))
            + count(ecogl_source_fix_action_queue.get("ready_native_split_count"))
            + count(ecogl_source_fix_action_queue.get("needs_current_source_index_count"))
            + count(ecogl_source_fix_action_queue.get("decision_required_count"))
        )
        ecogl_exception_count = source_fix_queue_actionable_count
        ecogl_source_fix_action_count = source_fix_queue_actionable_count
        ecogl_source_fix_remaining_count = source_fix_queue_actionable_count
        ecogl_source_fix_action_type_counts = {
            key: value
            for key, value in source_fix_queue_group_counts.items()
            if key != "already_applied" and count(value)
        }
    ecogl_source_fix_summary = ", ".join(
        f"{str(key).replace('tag_baselane_transaction_category', 'category')}={count(value)}"
        for key, value in sorted(ecogl_source_fix_action_type_counts.items())
        if count(value)
    )
    source_fix_validation_pending_count = count(ecogl_source_fix_correction_validation.get("pending_count"))
    source_fix_validation_invalid_count = count(ecogl_source_fix_correction_validation.get("invalid_count"))
    source_fix_validation_ready_count = count(ecogl_source_fix_correction_validation.get("ready_count"))
    source_fix_approval_pending_count = count(ecogl_source_fix_approval.get("pending_count"))
    source_fix_approval_invalid_count = count(ecogl_source_fix_approval.get("invalid_count"))
    source_fix_approval_approved_count = count(ecogl_source_fix_approval.get("approved_count"))
    source_fix_apply_ready_count = count(ecogl_source_fix_apply_plan.get("ready_current_source_index_count"))
    source_fix_apply_refresh_count = count(ecogl_source_fix_apply_plan.get("needs_current_source_index_refresh_count"))
    source_fix_apply_blocked_count = count(ecogl_source_fix_apply_plan.get("blocked_count"))
    source_fix_validation_needs_input = source_fix_validation_pending_count > 0 or source_fix_validation_invalid_count > 0
    source_fix_ready_to_apply = source_fix_apply_ready_count > 0 and source_fix_apply_refresh_count == 0 and source_fix_apply_blocked_count == 0
    source_fix_reports_effectively_clear = (
        ecogl_source_fix_remaining_count == 0
        and source_fix_approval_pending_count == 0
        and source_fix_approval_invalid_count == 0
        and source_fix_validation_pending_count == 0
        and source_fix_validation_invalid_count == 0
        and source_fix_apply_ready_count == 0
        and source_fix_apply_refresh_count == 0
        and source_fix_apply_blocked_count == 0
        and (
            ecogl_source_fix_verifier.get("status") in {"ok", "missing"}
            or count(ecogl_source_fix_verifier.get("remaining_count")) == 0
        )
    )
    autonomy_downstream_hold = ecogl_autonomy.get("downstream_hold") is True
    source_fix_quality_blocked = not source_fix_reports_effectively_clear and (
        ecogl_exception_count > 0 or ecogl_source_fix_remaining_count > 0
    )
    # An active native-source exception can be material before any source-fix
    # queue is generated (for example, a pending unassigned county-tax debit).
    # That upstream hold must survive an otherwise clean historical fix queue.
    source_quality_blocked = autonomy_downstream_hold or source_fix_quality_blocked
    source_quality_exception_count = (
        autonomy_exception_count
        if autonomy_downstream_hold
        else ecogl_exception_count or ecogl_source_fix_remaining_count
    )
    daily_source_cash_balance_current = (
        daily_source_cash_balance.get("status") not in {"missing", "unreadable"}
        and report_timestamp(daily_source_cash_balance) is not None
        and (
            report_timestamp(weekly_cf_sync) is None
            or first_report_newer(daily_source_cash_balance, weekly_cf_sync)
        )
    )
    source_cash_balance_violation_count = max(
        count(weekly_cf_sync.get("source_cash_balance_violation_count")),
        count(weekly_file_updates.get("source_cash_balance_violation_count")),
        count(daily_source_cash_balance.get("violation_count")) if daily_source_cash_balance_current else 0,
    )
    source_cash_balance_no_match_count = max(
        count(weekly_cf_sync.get("source_cash_balance_no_match_count")),
        count(weekly_file_updates.get("source_cash_balance_no_match_count")),
    )
    source_cash_balance_split_scope_missing_property_count = max(
        count(weekly_cf_sync.get("source_cash_balance_split_scope_missing_property_count")),
        count(weekly_file_updates.get("source_cash_balance_split_scope_missing_property_count")),
    )
    first_day_pm_fee_count = count(first_day_pm_fee_audit.get("first_day_pm_fee_count"))
    quarantined_first_day_pm_fee_count = max(
        count(first_day_pm_fee_quarantine.get("quarantined_row_count")),
        count(weekly_file_updates.get("first_day_pm_fee_quarantine_count")),
    )
    remaining_reporting_first_day_pm_fee_count = max(
        count(first_day_pm_fee_quarantine.get("remaining_first_day_pm_fee_count")),
        count(weekly_file_updates.get("first_day_pm_fee_quarantine_remaining_count")),
    )
    first_day_pm_fee_reporting_output_clean = (
        (first_day_pm_fee_quarantine.get("reporting_output_clean") is True)
        or (weekly_file_updates.get("first_day_pm_fee_quarantine_reporting_output_clean") is True)
    )
    first_day_pm_fee_cleanup_action_count = count(first_day_pm_fee_cleanup.get("action_count"))
    first_day_pm_fee_cleanup_command = (
        first_day_pm_fee_cleanup.get("cleanup_command_after_review")
        or "BASELANE_FIRST_DAY_PM_FEE_SOURCE_CLEANUP_APPLY=1 bash scripts/baselane_first_day_pm_fee_cleanup_then_refresh.sh"
    )
    source_cash_balance_violation_properties = (
        daily_source_cash_balance.get("violation_properties")
        if daily_source_cash_balance_current and count(daily_source_cash_balance.get("violation_count"))
        else
        weekly_cf_sync.get("source_cash_balance_violation_properties")
        or weekly_file_updates.get("source_cash_balance_violation_properties")
        or []
    )
    source_cash_balance_policy = (
        weekly_cf_sync.get("source_cash_balance_policy")
        or weekly_file_updates.get("source_cash_balance_policy")
        or "raw_gl_cumulative_amount_excluding_earldao_interest"
    )
    source_cash_balance_blocked = (
        source_cash_balance_violation_count > 0
        or source_cash_balance_no_match_count > 0
        or source_cash_balance_split_scope_missing_property_count > 0
    )
    source_cash_actions_available = source_cash_reconciliation_actions.get("status") not in {"missing", "unreadable", None}
    active_source_cash_action_count = count(source_cash_reconciliation_actions.get("active_monthly_candidate_action_count"))
    if source_cash_actions_available:
        source_cash_balance_blocked = (
            source_cash_balance_blocked or active_source_cash_action_count > 0
            if daily_source_cash_balance_current and count(daily_source_cash_balance.get("violation_count")) > 0
            else active_source_cash_action_count > 0
        )
    source_cash_reconciliation_actions_stale = (
        source_cash_actions_available
        and active_source_cash_action_count > 0
        and not first_report_newer(source_cash_reconciliation_actions, weekly_cf_sync)
        and report_timestamp(weekly_cf_sync) is not None
    )
    zero_row_source_ledger_decision_missing = zero_row_source_ledger_decision_missing_actions(source_cash_reconciliation_actions)
    source_cash_balance_blocked = source_cash_balance_blocked or source_cash_reconciliation_actions_stale or bool(zero_row_source_ledger_decision_missing)
    yhome_operating_cash_apply_verify_status = yhome_operating_cash_apply_verify.get("status")
    yhome_operating_cash_apply_verify_reason = yhome_operating_cash_apply_verify.get("reason")
    yhome_post_update_available = "post_yhome_update_required_count" in yhome_operating_cash_apply_verify
    yhome_apply_verify_current = first_report_newer(
        yhome_operating_cash_apply_verify,
        weekly_cf_sync,
    )
    if yhome_apply_verify_current and (
        yhome_operating_cash_apply_verify_status == "ok" and (
        yhome_post_update_available
        or yhome_operating_cash_apply_verify_reason in {"applied_and_verified", "no_updates_required"}
        )
    ):
        yhome_operating_cash_update_required_count = count(
            yhome_operating_cash_apply_verify.get("post_yhome_update_required_count")
            if yhome_post_update_available
            else yhome_operating_cash_apply_verify.get("pre_yhome_update_required_count")
        )
    elif yhome_apply_verify_current:
        yhome_operating_cash_update_required_count = count(
            yhome_operating_cash_apply_verify.get("pre_yhome_update_required_count")
        )
    else:
        yhome_operating_cash_update_required_count = max(
            count(weekly_cf_sync.get("cf_balance_sheet_consistency_yhome_update_required_count")),
            count(cf_balance_sheet_consistency.get("yhome_update_required_count")),
            count(yhome_operating_cash_apply_verify.get("pre_yhome_update_required_count")),
        )
    yhome_missing_candidate_count = count(
        yhome_operating_cash_apply_verify.get("pre_yhome_missing_candidate_count")
        if yhome_apply_verify_current
        else cf_balance_sheet_consistency.get("yhome_missing_candidate_count")
    )
    yhome_missing_candidates = (
        yhome_operating_cash_apply_verify.get("pre_yhome_missing_candidates")
        if yhome_apply_verify_current
        else cf_balance_sheet_consistency.get("yhome_missing_candidates")
    )
    yhome_operating_cash_target_columns = (
        cf_balance_sheet_consistency.get("target_columns")
        or yhome_operating_cash_apply_verify.get("target_columns")
        or ["Lofty Operating Cash", "ECO Net DAO Funds"]
    )
    yhome_operating_cash_needs_attention = (
        yhome_operating_cash_update_required_count > 0
        or yhome_missing_candidate_count > 0
        or (
            yhome_operating_cash_apply_verify_status == "review"
            and yhome_operating_cash_apply_verify_reason == "dry_run_updates_required"
        )
    )
    audit_error_class_counts = weekly_cf_sync.get("audit_error_class_counts") if isinstance(weekly_cf_sync.get("audit_error_class_counts"), dict) else {}
    audit_error_count = count(weekly_cf_sync.get("audit_error_count"))
    no_gl_property_match_total_count = max(
        count(cf_no_gl_property_match.get("no_gl_property_match_count")),
        count(audit_error_class_counts.get("no_gl_property_match")),
    )
    no_gl_property_match_active_count = count(cf_no_gl_property_match.get("active_monthly_scope_count"))
    no_gl_property_match_blocking_count = (
        no_gl_property_match_active_count
        if cf_no_gl_property_match.get("status") not in {"missing", "unreadable", None}
        else no_gl_property_match_total_count
    )
    audit_errors_all_no_gl = (
        audit_error_count > 0
        and count(audit_error_class_counts.get("no_gl_property_match")) == audit_error_count
    )
    monthly_relevant_audit_error_count = no_gl_property_match_blocking_count if audit_errors_all_no_gl else audit_error_count
    weekly_cf_hard_blocker_counts = {
        "audit_error_count": monthly_relevant_audit_error_count,
        "no_gl_property_match_count": no_gl_property_match_blocking_count,
        "conflict_count": count(weekly_cf_sync.get("conflict_count")),
        "missing_canonical_cf_count": count(weekly_cf_sync.get("missing_canonical_cf_count")),
        "no_mortgage_debt_violation_count": count(weekly_cf_sync.get("no_mortgage_debt_violation_count")),
    }
    weekly_cf_hard_blockers_clear = (
        not source_cash_balance_blocked
        and all(value == 0 for value in weekly_cf_hard_blocker_counts.values())
    )
    weekly_cf_inactive_no_gl_only = (
        audit_errors_all_no_gl
        and no_gl_property_match_total_count > 0
        and no_gl_property_match_blocking_count == 0
        and source_cash_balance_violation_count == 0
        and source_cash_balance_no_match_count == 0
        and source_cash_balance_split_scope_missing_property_count == 0
        and weekly_cf_hard_blockers_clear
        and weekly_cf_gate.get("status") == "ok"
    )
    if isinstance(weekly_cf_sync.get("effective_ok"), bool):
        weekly_cf_base_effective_ok = (
            weekly_cf_sync.get("effective_ok") is True
            or weekly_cf_inactive_no_gl_only
        ) and weekly_cf_hard_blockers_clear
    else:
        weekly_cf_base_effective_ok = (
            weekly_cf_sync.get("status") == "ok"
            or (
                weekly_cf_sync.get("status") == "review"
                and weekly_cf_gate.get("status") == "ok"
                and source_fix_reports_effectively_clear
                and weekly_cf_hard_blockers_clear
            )
        )
    weekly_cf_effective_ok = weekly_cf_base_effective_ok
    monthly_statements_captured = count(monthly_statements_gate.get("captured_unique_count"))
    monthly_statements_min_captured = count(monthly_statements_gate.get("min_captured_required"))
    monthly_statements_fresh = required_fresh_generated_at(monthly_statements_gate, MONTHLY_STATEMENTS_MAX_AGE_HOURS)
    monthly_statements_target_matches = statement_target_matches(monthly_statements_gate, run_month)
    monthly_statements_download_error = str(monthly_statements_download.get("error") or "")
    monthly_statements_download_error_class = str(
        monthly_statements_gate.get("download_error_class")
        or monthly_statements_download.get("error_class")
        or ""
    ).strip()
    monthly_statements_auth_required = (
        monthly_statements_gate.get("reason") == "auth-required"
        or monthly_statements_gate.get("action") == "auth-baselane"
        or monthly_statements_download_error_class == "auth-required"
        or "AUTH_REQUIRED" in monthly_statements_download_error
        or "login form submission failed" in monthly_statements_download_error
    )
    monthly_statements_waiting_for_posted_statements = (
        monthly_statements_gate.get("reason") == "no-statement-buttons"
        or monthly_statements_gate.get("action") == "wait-for-statements"
        or monthly_statements_download_error_class == "no-statement-buttons"
    )
    monthly_statements_gate_next_action = str(monthly_statements_gate.get("next_action") or "").strip()
    monthly_statements_gate_refresh_command = str(monthly_statements_gate.get("gate_refresh_command") or "").strip()
    monthly_statements_next_action = (
        monthly_statements_gate_next_action
        or (
            "Authenticate Baselane in the visible browser tab, then run "
            f"{monthly_statements_gate_refresh_command or 'BASELANE_MONTHLY_STATEMENTS_GATE_ONLY=1 bash scripts/baselane_monthly_statements_idempotent.sh'} "
            "to refresh verified current target-month bank statement evidence, then rerun monthly readiness."
        )
        if monthly_statements_auth_required
        else (
            monthly_statements_gate_next_action
            or (
                "Baselane has no target-month bank statement download buttons yet; retry after statements post with "
                f"{monthly_statements_gate.get('retry_command') or 'RUN_MONTH=2026-06 bash scripts/baselane_monthly_statements_idempotent.sh'}, "
                "then rerun monthly readiness."
            )
        )
        if monthly_statements_waiting_for_posted_statements
        else next_action("operational.monthly_bank_statement_capture.not_ok")
    )
    monthly_statements_ok = (
        monthly_statements_gate.get("status") == "ok"
        and int(monthly_statements_gate.get("monthly_script_return_code") or 0) == 0
        and monthly_statements_gate.get("download_ok") is True
        and monthly_statements_captured >= monthly_statements_min_captured
        and monthly_statements_min_captured > 0
        and monthly_statements_target_matches
        and monthly_statements_fresh
    )
    source_quality_gate = {
        "status": "blocked" if source_quality_blocked else "ok",
        "blocker": f"ECO GL source quality ({source_quality_exception_count} exceptions)" if source_quality_blocked else None,
        "exception_count": source_quality_exception_count,
        "autonomy_downstream_hold": autonomy_downstream_hold,
        "source_fix_action_count": ecogl_source_fix_action_count,
        "source_fix_remaining_count": ecogl_source_fix_remaining_count,
        "source_fix_action_type_counts": ecogl_source_fix_action_type_counts,
        "source_fix_summary": ecogl_source_fix_summary,
        "source_fix_status": ecogl_source_fix.get("status"),
        "source_fix_actions_csv": str(ecogl_source_fix_actions_path),
        "source_fix_corrections_csv": str(ecogl_source_fix_corrections_path),
        "source_fix_corrections_status": ecogl_source_fix_corrections.get("status"),
        "source_fix_approval": str(ecogl_source_fix_approval_path),
        "source_fix_approval_status": ecogl_source_fix_approval.get("status"),
        "source_fix_approval_approved_count": source_fix_approval_approved_count,
        "source_fix_approval_pending_count": source_fix_approval_pending_count,
        "source_fix_approval_invalid_count": source_fix_approval_invalid_count,
        "source_fix_approved_corrections_csv": str(ecogl_source_fix_approved_corrections_path),
        "source_fix_correction_validation_status": ecogl_source_fix_correction_validation.get("status"),
        "source_fix_correction_validation_ready_count": source_fix_validation_ready_count,
        "source_fix_correction_validation_pending_count": source_fix_validation_pending_count,
        "source_fix_correction_validation_invalid_count": source_fix_validation_invalid_count,
        "source_fix_correction_validation_csv": str(ecogl_source_fix_correction_validation_csv_path),
        "source_fix_apply_plan": str(ecogl_source_fix_apply_plan_path),
        "source_fix_apply_plan_csv": str(ecogl_source_fix_apply_plan_csv_path),
        "source_fix_apply_plan_status": ecogl_source_fix_apply_plan.get("status"),
        "source_fix_apply_plan_ready_current_source_index_count": source_fix_apply_ready_count,
        "source_fix_apply_plan_needs_current_source_index_refresh_count": source_fix_apply_refresh_count,
        "source_fix_apply_plan_blocked_count": source_fix_apply_blocked_count,
        "source_fix_report": str(ecogl_source_fix_path),
        "source_fix_evidence": str(ecogl_source_fix_evidence_path),
        "source_fix_action_queue": str(ecogl_source_fix_action_queue_path),
        "source_fix_needs_source_evidence_count": source_fix_needs_source_evidence_count,
        "source_fix_verifier": str(ecogl_source_fix_verifier_path),
        "source_fix_verifier_status": ecogl_source_fix_verifier.get("status"),
        "source_fix_verifier_verified_fixed_count": count(ecogl_source_fix_verifier.get("verified_fixed_count")),
        "source_fix_effectively_clear": source_fix_reports_effectively_clear,
        "autonomy_status": ecogl_autonomy.get("status"),
        "autonomy_report": str(ecogl_autonomy_path),
        "next_action": (
            autonomy_source_quality_next_action(ecogl_autonomy)
            if autonomy_downstream_hold
            else
            f"Run {first_day_pm_fee_cleanup_command} first; source category apply is blocked while 1st-day AOPS PM-fee source rows remain."
            if source_fix_quality_blocked and first_day_pm_fee_count
            else
            f"Reconcile the actual Baselane/ECO source transaction or an accounting-approved accrual basis for {source_fix_needs_source_evidence_count} source-evidence case(s) in reports/baselane_ecogl_source_fix_action_queue.json. Do not use reports/baselane_ecogl_source_fix_approved_corrections.csv as a substitute for missing PM-fee source evidence; export again, then rerun scripts/baselane_weekly_file_updates_cron.sh."
            if source_fix_quality_blocked and source_fix_needs_source_evidence_count
            else
            f"Run BASELANE_SOURCE_FIX_APPLY=1 bash scripts/baselane_apply_source_fix_then_refresh.sh for {source_fix_apply_ready_count} current-ID approved correction(s); resolve remaining pending categories in reports/baselane_ecogl_source_fix_approval.json."
            if source_fix_quality_blocked and source_fix_ready_to_apply and source_fix_validation_needs_input
            else "Approve exact categories in reports/baselane_ecogl_source_fix_approval.json, validate generated approved corrections, update Baselane source rows, export again, then rerun scripts/baselane_weekly_file_updates_cron.sh."
            if source_fix_quality_blocked and source_fix_validation_needs_input
            else "Use reports/baselane_ecogl_source_fix_approved_corrections.csv to fix exact Baselane source categories, export again, then rerun scripts/baselane_weekly_file_updates_cron.sh."
            if source_fix_quality_blocked
            else "Source-fix reports are clean; continue monthly guarded apply, live capture, and owner-email gates."
        ),
    }

    def ensure_property(name: str) -> dict:
        entry = by_property.setdefault(name, {"property_name": name, "blockers": []})
        return entry

    def add_portfolio_blocker(key: str, path: Path, detail: dict | None = None, next_action_override: str | None = None) -> None:
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(path),
            "next_action": next_action_override or next_action(key),
        }
        if detail:
            item["detail"] = detail
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)

    coownership_blocked_properties = (
        monthly_run.get("coownership_gl_policy_validation_blocked_properties")
        if isinstance(monthly_run.get("coownership_gl_policy_validation_blocked_properties"), list)
        else []
    )
    coownership_failed_step = str(monthly_run.get("failed_step") or "") == "coownership_gl_policy_validation"
    if monthly_run.get("status") == "failed" and (coownership_failed_step or coownership_blocked_properties):
        key = "monthly_close.coownership_gl_policy_validation.blocked"
        first_blocked = next((item for item in coownership_blocked_properties if isinstance(item, dict)), {})
        blocked_property_name = str(first_blocked.get("property") or first_blocked.get("property_name") or "portfolio")
        detail = {
            "run_month": monthly_run.get("run_month"),
            "failed_step": monthly_run.get("failed_step"),
            "blocked_count": monthly_run.get("coownership_gl_policy_validation_blocked_count")
            or len(coownership_blocked_properties),
            "blocked_properties": coownership_blocked_properties[:10],
            "coownership_report": monthly_run.get("coownership_gl_policy_validation_report"),
            "retag_status": monthly_run.get("baselane_85104_preclosing_retag_status"),
            "retag_ready_count": monthly_run.get("baselane_85104_preclosing_retag_ready_count"),
            "retag_payload_transaction_count": monthly_run.get("baselane_85104_preclosing_retag_payload_transaction_count"),
            "retag_apply_ready": monthly_run.get("baselane_85104_preclosing_retag_apply_ready"),
            "retag_apply_readiness_status": monthly_run.get("baselane_85104_preclosing_retag_apply_readiness_status"),
            "retag_apply_readiness_blockers": monthly_run.get("baselane_85104_preclosing_retag_apply_readiness_blockers"),
            "retag_report": monthly_run.get("artifacts", {}).get("baselane_85104_preclosing_retag")
            if isinstance(monthly_run.get("artifacts"), dict)
            else None,
            "retag_payload": monthly_run.get("artifacts", {}).get("baselane_85104_preclosing_retag_payload")
            if isinstance(monthly_run.get("artifacts"), dict)
            else None,
        }
        retag_blockers = detail["retag_apply_readiness_blockers"] if isinstance(detail["retag_apply_readiness_blockers"], list) else []
        retag_auth_blocked = "cdp_auth_status=review" in {str(blocker) for blocker in retag_blockers}
        counter[key] += 1
        item = {
            "property_name": blocked_property_name,
            "class": key,
            "path": str(monthly_run_path),
            "next_action": (
                "Authenticate the Baselane CDP browser, then apply the prepared 85-104 pre-closing retag with "
                "BASELANE_85104_PRECLOSING_RETAG_APPLY=1 python3 scripts/baselane_85104_preclosing_property_retag.py --apply; "
                "rerun Baselane sync, public financial split, coownership validation, monthly transfer reconciliation, Discord review, then email gate."
                if retag_auth_blocked
                else first_blocked.get("next_action")
                or "Resolve co-ownership GL policy blockers upstream, rerun Baselane sync/split, then rerun the monthly close report."
            ),
            "detail": detail,
            "summary": f"Co-ownership GL policy validation blocked monthly close for {detail['blocked_count']} propert(ies).",
        }
        ensure_property(blocked_property_name)["blockers"].append(item)
        blockers.append(item)

    rent_roll_gap_review_status = rent_roll_gap_review.get("status")
    rent_roll_approval_coverage = rent_roll_gap_review.get("approval_template_coverage") if isinstance(rent_roll_gap_review.get("approval_template_coverage"), dict) else {}
    rent_roll_gap_review_blocks = rent_roll_gap_review_status in {"failed", "review", "missing", "unreadable"} and not (
        rent_roll_gap_review_status == "review"
        and rent_roll_target_scoped
        and rent_roll_target_pending_gap_count == 0
        and count(rent_roll_gap_review.get("pending_stale_export_date_count")) == 0
    )
    if rent_roll_gap_review_blocks:
        key = f"monthly_comms.rent_roll_gap_review.{rent_roll_gap_review_status}"
        counter[key] += 1
        rent_roll_action = rent_roll_next_action(key, rent_roll_gap_review, rent_roll_source, hemlane_cdp_preflight, comms_root, hemlane_cdp_capture)
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(rent_roll_gap_review_path),
            "next_action": rent_roll_action,
            "detail": {
                "run_month": run_month,
                "gap_count": rent_roll_gap_review.get("gap_count"),
                "source_report": str(rent_roll_source_path),
                "source_report_status": rent_roll_source.get("status"),
                "source_freshness_status": rent_roll_source.get("freshness_status"),
                "source_owner_email_allowed": rent_roll_source.get("owner_email_allowed"),
                "source_live_update_allowed": rent_roll_source.get("live_update_allowed"),
                "pending_gap_count": rent_roll_gap_review.get("pending_gap_count"),
                "target_scoped": rent_roll_target_scoped,
                "target_property_count": rent_roll_target_gaps.get("target_property_count"),
                "target_pending_gap_count": rent_roll_target_pending_gap_count,
                "non_target_pending_gap_count": rent_roll_target_gaps.get("non_target_pending_gap_count"),
                "target_pending_gap_properties": rent_roll_target_gaps.get("target_pending_gap_properties"),
                "stale_export_dates": rent_roll_gap_review.get("stale_export_dates"),
                "pending_stale_export_date_count": rent_roll_gap_review.get("pending_stale_export_date_count"),
                "action_queue_digest": rent_roll_gap_review.get("action_queue_digest"),
                "approval_template_coverage_status": rent_roll_approval_coverage.get("status"),
                "approval_template_digest": rent_roll_gap_review.get("approval_template_digest") or rent_roll_approval_coverage.get("digest"),
                "approval_path": rent_roll_gap_review.get("approval_path"),
                "queue_csv": rent_roll_gap_queue_csv,
                "hemlane_cdp_preflight_status": hemlane_cdp_preflight.get("status"),
                "hemlane_cdp_preflight_issue_summary": hemlane_cdp_preflight.get("issue_summary"),
                "hemlane_cdp_available": hemlane_cdp_preflight.get("cdp_available"),
                "hemlane_logged_in_tab_count": hemlane_cdp_preflight.get("logged_in_tab_count"),
                "hemlane_login_tab_count": hemlane_cdp_preflight.get("login_tab_count"),
                "hemlane_rent_roll_tab_count": hemlane_cdp_preflight.get("rent_roll_tab_count"),
            },
        }
        if rent_roll_target_scoped and rent_roll_pending_gap_count_for_block:
            item["summary"] = (
                f"Rent-roll gaps remain for {rent_roll_pending_gap_count_for_block} active Lofty PM targets; "
                "hold owner email and Lofty PM publish."
            )
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)
    elif rent_roll_approval_coverage and rent_roll_approval_coverage.get("status") != "ok":
        key = "monthly_comms.rent_roll_gap_approval_coverage.review"
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(rent_roll_gap_review_path),
            "next_action": next_action(key),
            "detail": {
                "run_month": run_month,
                "approval_template_coverage_status": rent_roll_approval_coverage.get("status"),
                "approval_template_digest": rent_roll_gap_review.get("approval_template_digest") or rent_roll_approval_coverage.get("digest"),
                "missing_gap_count": rent_roll_approval_coverage.get("missing_gap_count"),
                "missing_stale_export_date_count": rent_roll_approval_coverage.get("missing_stale_export_date_count"),
            },
        }
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)

    operational_gates = {
        "daily_run": {
            "status": daily_run.get("status"),
            "effective_status": daily_run.get("status"),
            "path": str(daily_run_path),
        },
        "daily_sync_report": {
            "status": daily_sync_report.get("status"),
            "effective_status": daily_sync_report.get("effective_status"),
            "issue_count": daily_sync_report.get("issue_count"),
            "next_action": daily_sync_report.get("next_action"),
            "sync_report_status": daily_sync_report.get("sync_report_status"),
            "effective_failed_step": daily_sync_report.get("effective_failed_step"),
            "disk_space_preflight_status": daily_sync_report.get("disk_space_preflight_status"),
            "disk_space_preflight_report": daily_sync_report.get("disk_space_preflight_report"),
            "disk_space_preflight_issues": daily_sync_report.get("disk_space_preflight_issues") or [],
            "current_disk_space_preflight_status": daily_disk_preflight.get("status"),
            "current_disk_space_preflight_issue_count": daily_disk_preflight.get("issue_count"),
            "current_disk_space_preflight_path": str(daily_disk_preflight_path),
            "wrapper_consistency_issues": daily_sync_report.get("wrapper_consistency_issues"),
            "path": str(daily_sync_report_path),
        },
        "baselane_sync": {"status": sync_report.get("status"), "path": str(sync_report_path)},
        "monthly_run": {
            "status": monthly_run.get("status"),
            "path": str(monthly_run_path),
            "run_month": monthly_run.get("run_month"),
            "failed_step": monthly_run.get("failed_step"),
            "effective_failed_step": monthly_run.get("effective_failed_step"),
            "next_action": monthly_run.get("next_action"),
            "monthly_finance_truth_refresh_status": monthly_run.get("monthly_finance_truth_refresh_status"),
            "monthly_finance_truth_refresh_auth_blocked": monthly_run.get("monthly_finance_truth_refresh_auth_blocked"),
            "monthly_finance_truth_refresh_cdp_blocked": monthly_run.get("monthly_finance_truth_refresh_cdp_blocked"),
            "monthly_finance_truth_refresh_report": monthly_run.get("monthly_finance_truth_refresh_report"),
            "monthly_finance_truth_refresh_direct_status": monthly_finance_truth_refresh.get("status"),
            "monthly_finance_truth_refresh_direct_generated_at": monthly_finance_truth_refresh.get("generated_at"),
            "monthly_finance_truth_refresh_direct_failed_step": monthly_finance_truth_refresh.get("failed_step"),
            "monthly_finance_truth_refresh_direct_auth_blocked": monthly_finance_truth_refresh.get("auth_blocked"),
            "monthly_finance_truth_refresh_direct_cdp_blocked": monthly_finance_truth_refresh.get("cdp_blocked"),
            "monthly_finance_truth_refresh_direct_report": str(monthly_finance_truth_refresh_path),
            "disk_space_preflight_report": (
                monthly_run.get("artifacts", {}).get("disk_space_preflight")
                if isinstance(monthly_run.get("artifacts"), dict)
                else None
            ),
        },
        "monthly_pipeline_candidate_coverage": {
            "status": pipeline_candidate_coverage.get("status"),
            "generated_at": pipeline_candidate_coverage.get("generated_at"),
            "path": str(pipeline_candidate_coverage_path),
            "mismatch_count": pipeline_candidate_coverage.get("mismatch_count"),
            "mismatches": pipeline_candidate_coverage.get("mismatches"),
            "input_digests": pipeline_candidate_coverage.get("input_digests"),
            "transfer_reconciliation": pipeline_candidate_coverage.get("transfer_reconciliation"),
            "telegram_reconciliation": pipeline_candidate_coverage.get("telegram_reconciliation"),
            "discord_all_property_send": pipeline_candidate_coverage.get("discord_all_property_send"),
            "lofty_publish": pipeline_candidate_coverage.get("lofty_publish"),
            "owner_email_packet": pipeline_candidate_coverage.get("owner_email_packet"),
        },
        "export_guard": {"status": export_guard.get("status"), "ok": export_guard.get("ok"), "path": str(export_guard_path)},
        "weekly_file_updates": {
            "status": weekly_file_updates.get("status"),
            "path": str(weekly_file_updates_path),
            "weekly_duplicate_review_pending_count": weekly_file_updates.get("weekly_duplicate_review_pending_count"),
            "weekly_duplicate_review_blocking_count": weekly_file_updates.get("weekly_duplicate_review_blocking_count"),
            "weekly_candidate_duplicate_pending_count": weekly_file_updates.get("weekly_candidate_duplicate_pending_count"),
            "weekly_raw_duplicate_key_count": weekly_file_updates.get("weekly_raw_duplicate_key_count"),
            "weekly_raw_exact_duplicate_extra_row_count": weekly_file_updates.get("weekly_raw_exact_duplicate_extra_row_count"),
            "weekly_deduped_reporting_ledger_removed_row_count": weekly_file_updates.get("weekly_deduped_reporting_ledger_removed_row_count"),
            "cf_statement_sync_status": weekly_file_updates.get("cf_statement_sync_status"),
        },
        "weekly_unprocessed": {
            "status": weekly_unprocessed.get("status"),
            "path": str(weekly_unprocessed_path),
            "duplicate_review_pending_count": weekly_unprocessed.get("duplicate_review_pending_count"),
            "duplicate_review_blocking_count": weekly_unprocessed.get("duplicate_review_blocking_count"),
            "candidate_duplicate_pending_count": weekly_unprocessed.get("candidate_duplicate_pending_count"),
            "deduped_reporting_ledger_removed_row_count": weekly_unprocessed.get("deduped_reporting_ledger_removed_row_count"),
        },
        "weekly_cf_sync": {
            "status": weekly_cf_sync.get("status"),
            "path": str(weekly_cf_sync_path),
            "effective_status": weekly_cf_sync.get("effective_status"),
            "effective_ok": weekly_cf_sync.get("effective_ok"),
            "effective_reason": weekly_cf_sync.get("effective_reason"),
            "effective_blockers": weekly_cf_sync.get("effective_blockers"),
            "base_effective_ok": weekly_cf_base_effective_ok,
            "hard_blocker_counts": weekly_cf_hard_blocker_counts,
            "audit_error_count": weekly_cf_sync.get("audit_error_count"),
            "audit_error_class_counts": weekly_cf_sync.get("audit_error_class_counts"),
            "no_gl_property_match_status": cf_no_gl_property_match.get("status"),
            "no_gl_property_match_count": cf_no_gl_property_match.get("no_gl_property_match_count"),
            "no_gl_property_match_active_monthly_scope_count": cf_no_gl_property_match.get("active_monthly_scope_count"),
            "no_gl_property_match_blocking_count": no_gl_property_match_blocking_count,
            "no_gl_property_match_total_count": no_gl_property_match_total_count,
            "inactive_no_gl_only": weekly_cf_inactive_no_gl_only,
            "no_gl_property_match_report": str(cf_no_gl_property_match_path),
            "conflict_count": weekly_cf_sync.get("conflict_count"),
            "source_cash_balance_policy": source_cash_balance_policy,
            "source_cash_balance_violation_count": source_cash_balance_violation_count,
            "source_cash_balance_violation_properties": source_cash_balance_violation_properties,
            "daily_source_cash_balance_status": daily_source_cash_balance.get("status"),
            "daily_source_cash_balance_report": str(daily_source_cash_balance_path),
            "daily_source_cash_balance_generated_at": daily_source_cash_balance.get("generated_at"),
            "daily_source_cash_balance_current": daily_source_cash_balance_current,
            "daily_source_cash_balance_violation_count": count(daily_source_cash_balance.get("violation_count")),
            "daily_source_cash_balance_raw_no_dao_mortgage_guard": daily_source_cash_balance.get("raw_no_dao_mortgage_guard"),
            "source_cash_balance_no_match_count": source_cash_balance_no_match_count,
            "source_cash_balance_no_match_properties": weekly_cf_sync.get("source_cash_balance_no_match_properties") or weekly_file_updates.get("source_cash_balance_no_match_properties") or [],
            "source_cash_balance_split_scope_expected_property_count": weekly_cf_sync.get("source_cash_balance_split_scope_expected_property_count") or weekly_file_updates.get("source_cash_balance_split_scope_expected_property_count"),
            "source_cash_balance_split_scope_missing_property_count": source_cash_balance_split_scope_missing_property_count,
            "source_cash_balance_split_scope_missing_properties": weekly_cf_sync.get("source_cash_balance_split_scope_missing_properties") or weekly_file_updates.get("source_cash_balance_split_scope_missing_properties") or [],
            "source_cash_reconciliation_action_status": source_cash_reconciliation_actions.get("status"),
            "source_cash_reconciliation_action_count": source_cash_reconciliation_actions.get("action_count"),
            "source_cash_reconciliation_action_kind_counts": source_cash_reconciliation_actions.get("action_kind_counts"),
            "source_cash_reconciliation_action_scope_counts": source_cash_reconciliation_actions.get("action_scope_counts"),
            "source_cash_reconciliation_active_monthly_candidate_action_count": source_cash_reconciliation_actions.get("active_monthly_candidate_action_count"),
            "source_cash_reconciliation_actions_stale": source_cash_reconciliation_actions_stale,
            "zero_row_source_ledger_decision_missing_count": len(zero_row_source_ledger_decision_missing),
            "zero_row_source_ledger_decision_missing_actions": zero_row_source_ledger_decision_missing[:25],
            "source_cash_reconciliation_actions_report": str(source_cash_reconciliation_actions_path),
            "conflict_resolution_plan": weekly_cf_sync.get("conflict_resolution_plan"),
            "conflict_resolution_approval_template": weekly_cf_sync.get("conflict_resolution_approval_template"),
            "conflict_resolution_applicable_count": weekly_cf_sync.get("conflict_resolution_applicable_count"),
            "conflict_resolution_blocked_count": weekly_cf_sync.get("conflict_resolution_blocked_count"),
            "conflict_resolution_approved_applicable_count": weekly_cf_sync.get("conflict_resolution_approved_applicable_count"),
            "conflict_resolution_status_counts": weekly_cf_sync.get("conflict_resolution_status_counts"),
            "untagged_gl_rows": weekly_cf_sync.get("untagged_gl_rows"),
            "untagged_exception_row_count": ecogl_exception_count,
            "ecogl_source_fix_action_count": ecogl_source_fix_action_count,
            "ecogl_source_fix_summary": ecogl_source_fix_summary,
            "ecogl_auto_safe_untagged_row_count": ecogl_autonomy.get("safe_auto_untagged_row_count"),
            "ecogl_auto_safe_rule_count": ecogl_autonomy.get("safe_auto_rule_count"),
            "missing_canonical_cf_count": weekly_cf_sync.get("missing_canonical_cf_count"),
        },
        "yhome_operating_cash": {
            "status": "review" if yhome_operating_cash_needs_attention else "ok",
            "authoritative": False,
            "blocks_downstream": False,
            "blocked": False,
            "work_product_needs_attention": yhome_operating_cash_needs_attention,
            "path": str(yhome_operating_cash_apply_verify_path),
            "cf_balance_sheet_consistency_path": str(cf_balance_sheet_consistency_path),
            "cf_balance_sheet_consistency_status": cf_balance_sheet_consistency.get("status"),
            "cf_balance_sheet_consistency_issue_count": cf_balance_sheet_consistency.get("issue_count"),
            "target_columns": yhome_operating_cash_target_columns,
            "update_required_count": yhome_operating_cash_update_required_count,
            "missing_candidate_count": yhome_missing_candidate_count,
            "missing_candidates": yhome_missing_candidates,
            "apply_verify_current": yhome_apply_verify_current,
            "apply_verify_status": yhome_operating_cash_apply_verify_status,
            "apply_verify_reason": yhome_operating_cash_apply_verify_reason,
            "pre_update_required_count": yhome_operating_cash_apply_verify.get("pre_yhome_update_required_count"),
            "post_update_required_count": yhome_operating_cash_apply_verify.get("post_yhome_update_required_count"),
            "applied_update_count": yhome_operating_cash_apply_verify.get("applied_update_count"),
            "external_write_attempted": yhome_operating_cash_apply_verify.get("external_write_attempted"),
        },
        "weekly_cf_review_gate": {
            "status": weekly_cf_gate.get("status"),
            "path": str(weekly_cf_gate_path),
            "blocker_count": weekly_cf_gate.get("blocker_count"),
            "idempotency_key": weekly_cf_gate.get("idempotency_key"),
        },
        "monthly_bank_statement_capture": {
            "status": monthly_statements_gate.get("status"),
            "path": str(monthly_statements_gate_path),
            "reason": monthly_statements_gate.get("reason"),
            "action": monthly_statements_gate.get("action"),
            "generated_at": monthly_statements_gate.get("generated_at"),
            "report_age_hours": iso_age_hours(monthly_statements_gate.get("generated_at")),
            "max_age_hours": MONTHLY_STATEMENTS_MAX_AGE_HOURS,
            "fresh": monthly_statements_fresh,
            "target_year": monthly_statements_gate.get("target_year"),
            "target_month": monthly_statements_gate.get("target_month"),
            "target_matches_run_month": monthly_statements_target_matches,
            "run_month": run_month,
            "download_ok": monthly_statements_gate.get("download_ok"),
            "download_report": str(monthly_statements_download_path),
            "download_error_class": monthly_statements_download_error_class or ("auth-required" if monthly_statements_auth_required else None),
            "download_new_files_count": monthly_statements_download.get("new_files_count"),
            "captured_unique_count": monthly_statements_captured,
            "min_captured_required": monthly_statements_min_captured,
            "monthly_script_return_code": monthly_statements_gate.get("monthly_script_return_code"),
            "gate_refresh_command": monthly_statements_gate.get("gate_refresh_command"),
            "retry_command": monthly_statements_gate.get("retry_command"),
            "auth_recovery_hint": monthly_statements_gate.get("auth_recovery_hint"),
            "next_action": monthly_statements_next_action,
        },
        "obie_cash_basis_insurance_cleanup": {
            "status": obie_insurance_cleanup.get("status"),
            "path": str(obie_insurance_cleanup_path),
            "mode": obie_insurance_cleanup.get("mode"),
            "cash_basis_insurance_states": obie_insurance_cleanup.get("cash_basis_insurance_states"),
            "removed_local_accrual_count": obie_insurance_cleanup.get("removed_local_accrual_count"),
            "live_delete_candidate_count": obie_insurance_cleanup.get("live_delete_candidate_count"),
            "osc_payment_count": obie_insurance_cleanup.get("osc_payment_count"),
            "duplicate_flag_count": obie_insurance_cleanup.get("duplicate_flag_count"),
            "skip_review_candidate_count": obie_insurance_cleanup.get("skip_review_candidate_count"),
            "local_apply_status": obie_insurance_cleanup.get("local_apply_status"),
            "live_apply_status": obie_insurance_cleanup.get("live_apply_status"),
            "digest": obie_insurance_cleanup.get("digest"),
            "markdown_report": "/mnt/c/Users/digit/Dropbox/Real Estate/Lofty PM/Obie/OH-IL-TN Cash-Basis Insurance Duplicate Audit.md",
            "csv_report": "/mnt/c/Users/digit/Dropbox/Real Estate/Lofty PM/Obie/OH-IL-TN Cash-Basis Insurance Duplicate Audit.csv",
            "policy": (
                "OH/IL/TN insurance is cash-basis via OSC Risk Secure; Insurance Accrual rows must be removed/excluded. "
                "Duplicate OSC payment flags are operator review items, not automatic skip instructions."
            ),
        },
        "first_day_pm_fee_audit": {
            "status": first_day_pm_fee_audit.get("status"),
            "path": str(first_day_pm_fee_audit_path),
            "scope": first_day_pm_fee_audit.get("scope"),
            "month": first_day_pm_fee_audit.get("month"),
            "first_day_pm_fee_count": first_day_pm_fee_count,
            "month_counts": first_day_pm_fee_audit.get("month_counts") or {},
            "quarantine_status": first_day_pm_fee_quarantine.get("status"),
            "quarantine_path": str(first_day_pm_fee_quarantine_path),
            "quarantined_reporting_row_count": quarantined_first_day_pm_fee_count,
            "remaining_reporting_first_day_pm_fee_count": remaining_reporting_first_day_pm_fee_count,
            "reporting_output_clean": first_day_pm_fee_reporting_output_clean,
            "quarantine_digest": first_day_pm_fee_quarantine.get("quarantine_digest")
            or weekly_file_updates.get("first_day_pm_fee_quarantine_digest"),
            "source_cleanup_status": first_day_pm_fee_cleanup.get("status"),
            "source_cleanup_plan": str(first_day_pm_fee_cleanup_path),
            "source_cleanup_actions_csv": str(first_day_pm_fee_cleanup_actions_path),
            "source_cleanup_action_count": first_day_pm_fee_cleanup_action_count,
            "source_cleanup_digest": first_day_pm_fee_cleanup.get("idempotency_digest"),
        },
        "scheduler_audit": {
            "status": scheduler_audit.get("status"),
            "path": str(scheduler_audit_path),
            "issue_count": scheduler_audit.get("issue_count"),
            "issues": scheduler_audit.get("issues"),
            "actionable_summary": scheduler_audit.get("actionable_summary"),
            "primary_blocker": scheduler_audit.get("primary_blocker"),
        },
        "local_model_preflight": {
            "status": local_model_preflight.get("status"),
            "path": str(local_model_preflight_path),
            "model": local_model_preflight.get("model"),
            "provider": local_model_preflight.get("provider"),
            "model_id": local_model_preflight.get("model_id"),
            "issue_count": local_model_preflight.get("issue_count"),
            "local_model_operational": local_model_preflight.get("local_model_operational"),
            "operational_model_id": local_model_preflight.get("operational_model_id"),
            "model_available": local_model_preflight.get("model_available"),
            "configured_model_present": local_model_preflight.get("configured_model_present"),
            "selected_endpoint_from_config": local_model_preflight.get("selected_endpoint_from_config"),
            "fallback_smoke_ok": local_model_preflight.get("fallback_smoke_ok"),
            "generated_at": local_model_preflight.get("generated_at"),
            "report_age_hours": iso_age_hours(local_model_preflight.get("generated_at")),
            "max_age_hours": LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS,
            "direct_smoke_attempted": local_model_direct_smoke.get("attempted"),
            "direct_smoke_ok": local_model_direct_smoke.get("ok"),
            "direct_smoke_response": local_model_direct_smoke.get("response"),
            "finance_contract_smoke_attempted": local_model_finance_smoke.get("attempted"),
            "finance_contract_smoke_ok": local_model_finance_smoke.get("ok"),
            "finance_contract_smoke_response": local_model_finance_smoke.get("response"),
            "finance_contract_expected_response": local_model_preflight.get("finance_contract_expected_response"),
            "validation_digest": local_model_preflight.get("validation_digest"),
            "required_for_monthly_close": REQUIRE_LOCAL_MODEL_PREFLIGHT,
            "audit_only_policy": (
                "Local model smoke is audited for deterministic helper health, but it does not prove ECO cash correctness, accrual completeness, "
                "Cash Flow Statement propagation, Lofty listing publish safety, Discord posting, or owner email send safety."
            ),
        },
        "public_path_guard": {
            "status": public_path_guard.get("status"),
            "ok": public_path_guard.get("ok"),
            "path": str(public_path_guard_path),
            "issue_count": public_path_guard.get("issue_count"),
        },
        "tenant_ledger_folder_guard": {
            "status": tenant_ledger_guard.get("status"),
            "path": str(tenant_ledger_guard_path),
            "checked_count": tenant_ledger_guard.get("checked_count"),
            "issue_count": tenant_ledger_guard.get("issue_count"),
        },
    }

    weekly_reason_parts = [part for part in str(weekly_file_updates.get("reason") or "").split(";") if part]
    weekly_review_safe_idempotency = (
        weekly_file_updates.get("review_safe_idempotency")
        if isinstance(weekly_file_updates.get("review_safe_idempotency"), dict)
        else {}
    )
    weekly_cf_gate_digest = weekly_cf_gate.get("action_queue_digest")
    weekly_cf_gate_report_digest = weekly_review_safe_idempotency.get("cf_review_gate_action_queue_digest")
    weekly_cf_gate_idempotency_key = weekly_cf_gate.get("idempotency_key")
    weekly_cf_gate_report_idempotency_key = weekly_review_safe_idempotency.get("cf_review_gate_idempotency_key")
    weekly_cf_gate_action_queue_count = weekly_cf_gate.get("action_queue_count") or (weekly_cf_gate.get("summary") or {}).get("action_queue_count")
    weekly_cf_gate_report_action_queue_count = weekly_review_safe_idempotency.get("cf_review_gate_action_queue_count")
    weekly_cf_gate_snapshot_current = (
        weekly_cf_gate_report_digest == weekly_cf_gate_digest
        and weekly_cf_gate_report_idempotency_key == weekly_cf_gate_idempotency_key
        and int(weekly_cf_gate_report_action_queue_count or 0) == int(weekly_cf_gate_action_queue_count or 0)
    )
    weekly_review_retry_safe = (
        weekly_file_updates.get("status") == "review"
        and weekly_review_safe_idempotency.get("retry_required") is True
        and weekly_review_safe_idempotency.get("safe_to_skip_next_run") is False
        and weekly_review_safe_idempotency.get("weekly_unprocessed_idempotent") is True
        and weekly_cf_gate_snapshot_current
    )
    stale_weekly_reason_parts = {
        "cf_statement_sync_review" if weekly_cf_base_effective_ok else "",
        "cf_review_gate_review" if weekly_cf_gate.get("status") == "ok" else "",
        "ecogl_data_quality_hold" if source_fix_reports_effectively_clear else "",
        "ecogl_source_fix_queue" if source_fix_reports_effectively_clear else "",
        "ecogl_source_fix_evidence" if source_fix_reports_effectively_clear else "",
        "ecogl_source_fix_correction_validation" if source_fix_reports_effectively_clear else "",
        "ecogl_source_fix_apply_plan" if source_fix_reports_effectively_clear else "",
    }
    active_weekly_reason_parts = [part for part in weekly_reason_parts if part not in stale_weekly_reason_parts]
    weekly_file_updates_effective_ok = weekly_file_updates.get("status") == "ok" or (
        weekly_file_updates.get("status") == "review"
        and not active_weekly_reason_parts
        and (
            weekly_file_updates.get("deterministic_verification_idempotent") is True
            or weekly_review_retry_safe
        )
    )
    weekly_unprocessed_idempotent = (
        weekly_file_updates.get("weekly_unprocessed_idempotent")
        if "weekly_unprocessed_idempotent" in weekly_file_updates
        else weekly_review_safe_idempotency.get("weekly_unprocessed_idempotent")
    )
    weekly_state_file_unmarked = (
        weekly_file_updates.get("state_file_unmarked")
        if "state_file_unmarked" in weekly_file_updates
        else weekly_review_safe_idempotency.get("state_file_unmarked")
    )
    weekly_retry_required = (
        weekly_file_updates.get("retry_required")
        if "retry_required" in weekly_file_updates
        else weekly_review_safe_idempotency.get("retry_required")
    )
    weekly_live_duplicate_and_cf_gates_clear = (
        int(weekly_unprocessed.get("candidate_duplicate_pending_count") or weekly_file_updates.get("weekly_candidate_duplicate_pending_count") or 0) == 0
        and int(weekly_unprocessed.get("duplicate_review_blocking_count") or weekly_file_updates.get("weekly_duplicate_review_blocking_count") or 0) == 0
        and weekly_cf_gate.get("status") == "ok"
        and int(weekly_cf_gate_action_queue_count or 0) == 0
    )
    weekly_unprocessed_effectively_idempotent = weekly_unprocessed_idempotent is True or (
        weekly_unprocessed_idempotent is None
        and weekly_live_duplicate_and_cf_gates_clear
    ) or (
        weekly_file_updates.get("status") == "skipped_not_friday"
        and weekly_live_duplicate_and_cf_gates_clear
    )
    weekly_state_file_effectively_unmarked = weekly_state_file_unmarked is False or weekly_file_updates.get("state_file_unmarked") is False
    weekly_retry_effectively_not_required = weekly_retry_required is False or weekly_retry_required is None
    weekly_scheduled_skip_ok = (
        weekly_file_updates.get("status") == "skipped_not_friday"
        and weekly_file_updates.get("reason") == "not_friday"
        and int(weekly_file_updates.get("return_code") or 0) == 0
        and (weekly_file_updates.get("day_of_week") != 5 if weekly_file_updates.get("day_of_week") is not None else True)
        and weekly_unprocessed_effectively_idempotent
        and weekly_state_file_effectively_unmarked
        and weekly_retry_effectively_not_required
    ) or (
        weekly_file_updates.get("status") == "already_done_for_week"
        and weekly_file_updates.get("reason") == "state_file_matches_iso_week"
        and int(weekly_file_updates.get("return_code") or 0) == 0
        and bool(weekly_file_updates.get("iso_week"))
        and weekly_file_updates.get("last_completed_week") == weekly_file_updates.get("iso_week")
    )
    weekly_file_updates_effective_ok = weekly_file_updates_effective_ok or weekly_scheduled_skip_ok
    operational_gates["weekly_file_updates"]["effective_ok"] = weekly_file_updates_effective_ok
    operational_gates["weekly_file_updates"]["active_reason_parts"] = active_weekly_reason_parts
    operational_gates["weekly_file_updates"]["scheduled_skip_ok"] = weekly_scheduled_skip_ok
    operational_gates["weekly_file_updates"]["review_retry_safe"] = weekly_review_retry_safe
    operational_gates["weekly_file_updates"]["live_duplicate_and_cf_gates_clear"] = weekly_live_duplicate_and_cf_gates_clear
    operational_gates["weekly_file_updates"]["cf_gate_snapshot_current"] = weekly_cf_gate_snapshot_current
    operational_gates["weekly_cf_sync"]["effective_ok"] = weekly_cf_effective_ok
    operational_gates["weekly_cf_sync"]["base_effective_ok"] = weekly_cf_base_effective_ok
    operational_gates["weekly_cf_sync"]["source_fix_effectively_clear"] = source_fix_reports_effectively_clear
    if source_fix_reports_effectively_clear:
        operational_gates["weekly_cf_sync"]["ecogl_source_fix_action_count"] = 0

    daily_sync_report_present = daily_sync_report.get("status") not in {"missing", "unreadable"}
    daily_sync_report_effective_ok = (
        daily_sync_report.get("effective_status") == "ok"
        and daily_sync_report.get("sync_report_status") == "ok"
        and zero_or_empty(daily_sync_report.get("effective_return_code", daily_sync_report.get("return_code")))
        and str(daily_sync_report.get("effective_failed_step") or daily_sync_report.get("failed_step") or "").strip() in {"", "none"}
    )
    daily_sync_report_ok = (
        (
            daily_sync_report.get("status") == "ok"
            and daily_sync_report.get("effective_status") in {None, "ok"}
            and int(daily_sync_report.get("issue_count") or 0) == 0
        )
        or daily_sync_report_effective_ok
    )
    daily_run_recovered_by_sync_report = daily_run.get("status") != "ok" and daily_sync_report_present and daily_sync_report_ok
    operational_gates["daily_run"]["recovered_by_canonical_daily_sync_report"] = daily_run_recovered_by_sync_report
    if daily_run_recovered_by_sync_report:
        operational_gates["daily_run"]["effective_status"] = "recovered"
        operational_gates["daily_run"]["recovery_source"] = "daily_sync_report"
    if daily_run.get("status") != "ok" and not daily_run_recovered_by_sync_report:
        add_portfolio_blocker("operational.daily_run.not_ok", daily_run_path, operational_gates["daily_run"])
    if daily_sync_report_present and not daily_sync_report_ok:
        daily_sync_next_action = str(daily_sync_report.get("next_action") or "").strip()
        daily_sync_blocker_key = "operational.daily_sync_report.not_ok"
        daily_sync_blocker_path = daily_sync_report_path
        daily_sync_detail = operational_gates["daily_sync_report"]
        if daily_sync_report.get("disk_space_preflight_status") == "review":
            daily_sync_blocker_key = "operational.daily_sync_disk_space_preflight.not_ok"
            disk_report = str(daily_sync_report.get("disk_space_preflight_report") or "").strip()
            if disk_report:
                daily_sync_blocker_path = Path(disk_report)
                if not daily_sync_blocker_path.is_absolute():
                    daily_sync_blocker_path = root / daily_sync_blocker_path
            if (
                daily_disk_preflight.get("status") == "ok"
                and int(daily_disk_preflight.get("issue_count") or 0) == 0
            ):
                daily_sync_blocker_key = "operational.daily_sync_report.not_ok"
                daily_sync_blocker_path = daily_sync_report_path
                daily_sync_next_action = (
                    "The current disk preflight is clean; rerun the authenticated Baselane daily sync "
                    "to replace the historical failed sync evidence before monthly publish/email."
                )
        add_portfolio_blocker(
            daily_sync_blocker_key,
            daily_sync_blocker_path,
            daily_sync_detail,
            next_action_override=daily_sync_next_action or None,
        )
    monthly_run_status = str(monthly_run.get("status") or "").strip()
    monthly_run_failed_step = str(monthly_run.get("effective_failed_step") or monthly_run.get("failed_step") or "").strip()
    monthly_run_self_referential_readiness_block = (
        monthly_run_status in {"failed", "review"}
        and monthly_run_failed_step in MONTHLY_READINESS_SELF_FAILED_STEPS
    )
    operational_gates["monthly_run"]["self_referential_readiness_block_ignored"] = monthly_run_self_referential_readiness_block
    if (
        monthly_run_status in {"failed", "review"}
        and not monthly_run_self_referential_readiness_block
        and not (coownership_failed_step or coownership_blocked_properties)
    ):
        monthly_run_next_action = str(monthly_run.get("next_action") or "").strip()
        if monthly_run_failed_step == "baselane_monthly_finance_truth_refresh":
            finance_truth_next_action = monthly_finance_truth_auth_next_action(
                monthly_finance_truth_refresh,
                run_month,
            )
            finance_truth_is_current = report_timestamp(monthly_finance_truth_refresh) is not None and (
                report_timestamp(monthly_run) is None
                or first_report_newer(monthly_finance_truth_refresh, monthly_run)
                or str(monthly_finance_truth_refresh.get("run_month") or "").strip() == run_month
                or str(monthly_run.get("monthly_finance_truth_refresh_report") or "").strip()
                == str(monthly_finance_truth_refresh_path)
            )
            if (
                finance_truth_next_action
                and finance_truth_is_current
                and (
                    generic_monthly_run_next_action(monthly_run_next_action)
                    or "baselane browser recovery" in monthly_run_next_action.lower()
                    or "finance-truth refresh blocker" in monthly_run_next_action.lower()
                )
            ):
                monthly_run_next_action = finance_truth_next_action
        transfer_next_action = transfer_reconciliation_next_action(transfer_reconciliation)
        if transfer_next_action and generic_monthly_run_next_action(monthly_run_next_action):
            monthly_run_next_action = transfer_next_action
        monthly_run_blocker_key = "operational.monthly_run.failed" if monthly_run_status == "failed" else "operational.monthly_run.not_ok"
        monthly_run_blocker_path = monthly_run_path
        if monthly_run_failed_step == "baselane_disk_space_preflight":
            monthly_run_blocker_key = "operational.monthly_run_disk_space_preflight.not_ok"
            disk_report = str(operational_gates["monthly_run"].get("disk_space_preflight_report") or "").strip()
            if disk_report:
                monthly_run_blocker_path = Path(disk_report)
                if not monthly_run_blocker_path.is_absolute():
                    monthly_run_blocker_path = root / monthly_run_blocker_path
        add_portfolio_blocker(
            monthly_run_blocker_key,
            monthly_run_blocker_path,
            operational_gates["monthly_run"],
            next_action_override=monthly_run_next_action or None,
        )
    pipeline_candidate_coverage_status = str(pipeline_candidate_coverage.get("status") or "").strip()
    pipeline_candidate_coverage_mismatch_count = count(pipeline_candidate_coverage.get("mismatch_count"))
    if (
        pipeline_candidate_coverage_status not in {"", "ok", "missing"}
        or pipeline_candidate_coverage_mismatch_count
    ):
        add_portfolio_blocker(
            "operational.monthly_pipeline_candidate_coverage.not_ok",
            pipeline_candidate_coverage_path,
            operational_gates["monthly_pipeline_candidate_coverage"],
            next_action_override=pipeline_candidate_coverage_next_action(pipeline_candidate_coverage),
        )
    if sync_report.get("status") != "ok":
        add_portfolio_blocker("operational.baselane_sync.not_ok", sync_report_path, operational_gates["baselane_sync"])
    if export_guard.get("ok") is not True:
        add_portfolio_blocker("operational.export_guard.not_ok", export_guard_path, operational_gates["export_guard"])
    if not weekly_file_updates_effective_ok:
        weekly_primary = weekly_file_updates.get("primary_blocker") if isinstance(weekly_file_updates.get("primary_blocker"), dict) else {}
        weekly_detail = dict(operational_gates["weekly_file_updates"])
        if weekly_primary:
            weekly_detail["primary_blocker"] = weekly_primary
        add_portfolio_blocker(
            "operational.weekly_file_updates.not_ok",
            weekly_file_updates_path,
            weekly_detail,
            weekly_primary.get("next_action") if weekly_primary else None,
        )
    pending_weekly_duplicates = int(weekly_unprocessed.get("duplicate_review_pending_count") or weekly_file_updates.get("weekly_duplicate_review_pending_count") or 0)
    blocking_weekly_duplicates = int(weekly_unprocessed.get("duplicate_review_blocking_count") or weekly_file_updates.get("weekly_duplicate_review_blocking_count") or 0)
    pending_candidate_duplicates = int(weekly_unprocessed.get("candidate_duplicate_pending_count") or weekly_file_updates.get("weekly_candidate_duplicate_pending_count") or 0)
    if blocking_weekly_duplicates or pending_candidate_duplicates:
        add_portfolio_blocker(
            "operational.weekly_duplicates.pending_review",
            weekly_unprocessed_path,
            {
                "duplicate_review_pending_count": pending_weekly_duplicates,
                "duplicate_review_blocking_count": blocking_weekly_duplicates,
                "candidate_duplicate_pending_count": pending_candidate_duplicates,
                "deduped_reporting_ledger_removed_row_count": weekly_unprocessed.get("deduped_reporting_ledger_removed_row_count") or weekly_file_updates.get("weekly_deduped_reporting_ledger_removed_row_count"),
            },
        )
    if not weekly_cf_base_effective_ok:
        add_portfolio_blocker("operational.weekly_cf_sync.not_ok", weekly_cf_sync_path, operational_gates["weekly_cf_sync"])
    if source_cash_balance_blocked:
        add_portfolio_blocker("operational.source_cash_balance.not_ok", weekly_cf_sync_path, operational_gates["weekly_cf_sync"])
    if weekly_cf_gate.get("status") != "ok":
        add_portfolio_blocker("operational.weekly_cf_review_gate.not_ok", weekly_cf_gate_path, operational_gates["weekly_cf_review_gate"])
    if not monthly_statements_ok:
        add_portfolio_blocker(
            "operational.monthly_bank_statement_capture.not_ok",
            monthly_statements_gate_path,
            operational_gates["monthly_bank_statement_capture"],
            monthly_statements_next_action,
        )
    if obie_insurance_cleanup.get("status") not in {"ok", "missing"}:
        add_portfolio_blocker(
            "operational.obie_cash_basis_insurance_cleanup.not_ok",
            obie_insurance_cleanup_path,
            operational_gates["obie_cash_basis_insurance_cleanup"],
            "Rerun `python3 scripts/obie_cash_basis_insurance_cleanup.py --apply-local`; keep OH/IL/TN insurance on OSC cash basis and review duplicate flags in `Dropbox/Real Estate/Lofty PM/Obie` before skipping any month.",
        )
    if first_day_pm_fee_audit.get("status") not in {"ok", "missing"} or first_day_pm_fee_count:
        add_portfolio_blocker("operational.first_day_pm_fee_audit.not_ok", first_day_pm_fee_audit_path, operational_gates["first_day_pm_fee_audit"])
    if scheduler_audit.get("status") != "ok" or int(scheduler_audit.get("issue_count") or 0) != 0:
        add_portfolio_blocker("operational.scheduler_audit.not_ok", scheduler_audit_path, operational_gates["scheduler_audit"])
    if not local_model_exact_ok and REQUIRE_LOCAL_MODEL_PREFLIGHT:
        add_portfolio_blocker("operational.local_model_preflight.not_ok", local_model_preflight_path, operational_gates["local_model_preflight"])
    if public_path_guard.get("status") != "ok" or int(public_path_guard.get("issue_count") or 0) != 0:
        add_portfolio_blocker("operational.public_path_guard.not_ok", public_path_guard_path, operational_gates["public_path_guard"])
    if tenant_ledger_guard.get("status") != "ok" or int(tenant_ledger_guard.get("issue_count") or 0) != 0:
        add_portfolio_blocker("operational.tenant_ledger_folder_guard.not_ok", tenant_ledger_guard_path, operational_gates["tenant_ledger_folder_guard"])
    review_candidate_issue_count = count(review_candidate_packet.get("issue_count"))
    review_candidate_financial_gate_issue_count = count(review_candidate_packet.get("financial_candidate_gate_issue_count"))
    review_candidate_manifest_source_issues = (
        review_candidate_packet.get("review_manifest_source_issues")
        if isinstance(review_candidate_packet.get("review_manifest_source_issues"), list)
        else []
    )
    review_candidate_manifest_source_issue_count = count(review_candidate_packet.get("manifest_source_issue_count")) or len(review_candidate_manifest_source_issues)
    review_candidate_manifest_record_count = count(review_candidate_packet.get("manifest_record_count"))
    review_candidate_record_count = len(review_candidate_packet.get("records") or []) if isinstance(review_candidate_packet.get("records"), list) else 0
    review_candidate_expected_record_count = count(guarded_apply_counts.get("active_record_count")) or review_candidate_manifest_record_count
    review_candidate_partial_coverage = bool(
        review_candidate_packet.get("status") != "missing"
        and
        review_candidate_expected_record_count
        and review_candidate_record_count < review_candidate_expected_record_count
    )
    if (
        review_candidate_packet.get("status") not in {"ok", "missing"}
        or review_candidate_issue_count
        or review_candidate_financial_gate_issue_count
        or review_candidate_manifest_source_issue_count
        or review_candidate_partial_coverage
    ):
        candidate_next_action = None
        if review_candidate_manifest_source_issue_count or review_candidate_partial_coverage:
            if review_candidate_partial_coverage:
                candidate_next_action = (
                    "Complete monthly guarded apply review generation before transfer reconciliation, Discord, Lofty publish, or owner email. "
                    f"Run `{SAFE_MONTHLY_GUARDED_APPLY_REVIEW_COMMAND}` and wait for guarded_apply_record_count to cover every active monthly index row."
                )
            elif "guarded_apply_in_progress" in {str(issue) for issue in review_candidate_manifest_source_issues}:
                candidate_next_action = (
                    "Finish monthly guarded apply checks before transfer reconciliation, Discord, Lofty publish, or owner email. "
                    f"Run `{SAFE_MONTHLY_GUARDED_APPLY_REVIEW_COMMAND}` and resolve pending_guard_check or approved-entry read errors until the guarded apply report is no longer in_progress."
                )
            else:
                candidate_next_action = (
                    "Resolve monthly review manifest source issues before transfer reconciliation, Discord, Lofty publish, or owner email; "
                    f"then rerun `{SAFE_MONTHLY_GUARDED_APPLY_REVIEW_COMMAND}`."
                )
        add_portfolio_blocker(
            "monthly_candidate_packet.financial_gate_issues",
            review_candidate_packet_path,
            {
                "status": review_candidate_packet.get("status"),
                "property_count": review_candidate_packet.get("property_count"),
                "record_count": review_candidate_record_count,
                "manifest_record_count": review_candidate_manifest_record_count,
                "expected_active_record_count": review_candidate_expected_record_count,
                "issue_count": review_candidate_packet.get("issue_count"),
                "marker_count": review_candidate_packet.get("marker_count"),
                "financial_candidate_gate_issue_count": review_candidate_packet.get("financial_candidate_gate_issue_count"),
                "manifest_source_issue_count": review_candidate_manifest_source_issue_count,
                "review_manifest_source_issues": review_candidate_manifest_source_issues,
                "partial_candidate_coverage": review_candidate_partial_coverage,
            },
            next_action_override=candidate_next_action,
        )
    if owner_email_send_guard.get("status") not in {"ok", "missing"} or int(owner_email_send_guard.get("issue_count") or 0) != 0:
        nested_owner_email_primary = (
            owner_email_send_guard.get("primary_blocker")
            if isinstance(owner_email_send_guard.get("primary_blocker"), dict)
            else {}
        )
        owner_email_issue_count = int(owner_email_send_guard.get("issue_count") or 0)
        owner_email_clean_safe_block = (
            owner_email_send_guard.get("guard_ok") is True
            and owner_email_issue_count == 0
            and owner_email_send_guard.get("safe_block") is True
            and nested_owner_email_primary
        )
        blocker_class = (
            str(nested_owner_email_primary.get("class") or nested_owner_email_primary.get("id") or "owner_email.send_guard.not_ok")
            if owner_email_clean_safe_block
            else "owner_email.send_guard.not_ok"
        )
        blocker_detail = {
            "status": owner_email_send_guard.get("status"),
            "guard_ok": owner_email_send_guard.get("guard_ok"),
            "send_allowed": owner_email_send_guard.get("send_allowed"),
            "safe_block": owner_email_send_guard.get("safe_block"),
            "max_once_monthly_ok": owner_email_send_guard.get("max_once_monthly_ok"),
            "no_spam_guard_ok": owner_email_send_guard.get("no_spam_guard_ok"),
            "issue_count": owner_email_send_guard.get("issue_count"),
            "issues": owner_email_send_guard.get("issues"),
            "send_lock_file_unreadable": owner_email_send_guard.get("send_lock_file_unreadable"),
            "sent_state_file_matches_run_month": owner_email_send_guard.get("sent_state_file_matches_run_month"),
            "idempotency_proof": owner_email_send_guard.get("idempotency_proof"),
        }
        if nested_owner_email_primary:
            blocker_detail["primary_blocker"] = nested_owner_email_primary
        add_portfolio_blocker(
            blocker_class,
            owner_email_send_guard_path,
            blocker_detail,
            next_action_override=(
                nested_owner_email_primary.get("next_action")
                if owner_email_clean_safe_block
                else None
            ),
        )
    owner_email_final_send_blocked = owner_email_send_guard.get("status") == "ok" and owner_email_send_guard.get("send_allowed") is not True
    if owner_email_send_guard.get("status") == "ok" and not owner_email_active_proof["ok"]:
        add_portfolio_blocker(
            "owner_email.active_property_guard.not_ok",
            owner_email_send_guard_path,
            {
                "manual_exclusions_ok": owner_email_active_proof["manual_exclusions_ok"],
                "yhome_transition_guard_ok": owner_email_active_proof["yhome_transition_guard_ok"],
                "yhome_transition_guard_column_b_rule_ok": owner_email_active_proof["yhome_transition_guard_column_b_rule_ok"],
                "yhome_transition_guard_column_b_header": owner_email_active_proof["yhome_transition_guard_column_b_header"],
                "yhome_transition_guard_column_b_marker_count": owner_email_active_proof["yhome_transition_guard_column_b_marker_count"],
                "active_property_policy_mentions_yhome": owner_email_active_proof["active_property_policy_mentions_yhome"],
                "active_property_policy_mentions_manual_exclusions": owner_email_active_proof["active_property_policy_mentions_manual_exclusions"],
                "excluded_owner_email_candidate_count": owner_email_active_proof["excluded_owner_email_candidate_count"],
                "manual_excluded_properties": owner_email_send_guard.get("manual_excluded_properties"),
                "sold_property_source": owner_email_send_guard.get("sold_property_source"),
                "idempotency_proof": owner_email_send_guard.get("idempotency_proof"),
            },
        )
    guarded_apply_status = guarded_apply.get("status")
    if guarded_apply_status in {"failed", "review", "missing", "unreadable"}:
        add_portfolio_blocker(
            f"monthly_guarded_apply.{guarded_apply_status}",
            guarded_apply_path,
            {
                "status": guarded_apply_status,
                "generated_at": guarded_apply.get("generated_at"),
                "report_age_hours": iso_age_hours(guarded_apply.get("generated_at")),
                "record_count": guarded_apply_counts.get("record_count"),
                "guard_failed_update_count": guarded_apply_counts.get("guard_failed_update_count"),
                "guard_failed_financial_count": guarded_apply_counts.get("guard_failed_financial_count"),
            },
        )
    elif guarded_apply_status == "ok" and not guarded_apply_fresh:
        add_portfolio_blocker(
            "monthly_guarded_apply.stale",
            guarded_apply_path,
            {
                "status": guarded_apply_status,
                "generated_at": guarded_apply.get("generated_at"),
                "report_age_hours": iso_age_hours(guarded_apply.get("generated_at")),
                "max_age_hours": MONTHLY_GUARDED_APPLY_MAX_AGE_HOURS,
            },
        )
    lofty_pm_publish_status = lofty_pm_publish.get("status")
    lofty_pm_publish_has_apply_evidence = any(
        key in lofty_pm_publish
        for key in (
            "apply",
            "property_count",
            "publish_result_count",
            "publish_failed_count",
            "financial_publish_result_count",
            "financial_publish_failed_count",
        )
    )
    lofty_pm_publish_property_count = count(lofty_pm_publish.get("property_count"))
    lofty_pm_publish_apply = lofty_pm_publish.get("apply") is True
    lofty_pm_publish_result_count = count(lofty_pm_publish.get("publish_result_count"))
    lofty_pm_publish_failed_count = count(lofty_pm_publish.get("publish_failed_count"))
    lofty_pm_updates_publish_result_count = count(lofty_pm_publish.get("updates_publish_result_count"))
    lofty_pm_updates_publish_failed_count = count(lofty_pm_publish.get("updates_publish_failed_count"))
    lofty_pm_financial_publish_result_count = count(lofty_pm_publish.get("financial_publish_result_count"))
    lofty_pm_financial_publish_failed_count = count(lofty_pm_publish.get("financial_publish_failed_count"))
    lofty_pm_publish_failure_count = (
        count(lofty_pm_publish.get("issue_count"))
        + lofty_pm_publish_failed_count
        + lofty_pm_updates_publish_failed_count
        + lofty_pm_financial_publish_failed_count
    )
    lofty_pm_publish_attempt_count = lofty_pm_publish_result_count + lofty_pm_financial_publish_result_count
    if lofty_pm_publish_status in {"failed", "review", "missing", "unreadable"}:
        add_portfolio_blocker(
            f"lofty_pm_publish.{lofty_pm_publish_status}",
            lofty_pm_publish_path,
            {
                "status": lofty_pm_publish_status,
                "generated_at": lofty_pm_publish.get("generated_at"),
                "report_age_hours": iso_age_hours(lofty_pm_publish.get("generated_at")),
                "issue_count": lofty_pm_publish.get("issue_count"),
                "guarded_apply_status": lofty_pm_publish.get("guarded_apply_status"),
                "apply": lofty_pm_publish.get("apply"),
                "property_count": lofty_pm_publish.get("property_count"),
                "publish_result_count": lofty_pm_publish.get("publish_result_count"),
                "publish_failed_count": lofty_pm_publish.get("publish_failed_count"),
                "financial_publish_result_count": lofty_pm_publish.get("financial_publish_result_count"),
                "financial_publish_failed_count": lofty_pm_publish.get("financial_publish_failed_count"),
                "active_property_only_policy": lofty_pm_publish.get("active_property_only_policy"),
            },
        )
    elif lofty_pm_publish_status == "ok" and not lofty_pm_publish_fresh:
        add_portfolio_blocker(
            "lofty_pm_publish.stale",
            lofty_pm_publish_path,
            {
                "status": lofty_pm_publish_status,
                "generated_at": lofty_pm_publish.get("generated_at"),
                "report_age_hours": iso_age_hours(lofty_pm_publish.get("generated_at")),
                "max_age_hours": LOFTY_PM_PUBLISH_MAX_AGE_HOURS,
                "active_property_only_policy": lofty_pm_publish.get("active_property_only_policy"),
            },
        )
    elif lofty_pm_publish_status == "ok" and lofty_pm_publish_has_apply_evidence and (
        not lofty_pm_publish_apply
        or lofty_pm_publish_failure_count
        or (
            lofty_pm_publish_property_count
            and lofty_pm_publish_attempt_count < lofty_pm_publish_property_count
        )
    ) and lofty_pm_publish.get("discord_review_handoff_ready") is not True:
        add_portfolio_blocker(
            "lofty_pm_publish.incomplete_apply",
            lofty_pm_publish_path,
            {
                "status": lofty_pm_publish_status,
                "generated_at": lofty_pm_publish.get("generated_at"),
                "report_age_hours": iso_age_hours(lofty_pm_publish.get("generated_at")),
                "apply": lofty_pm_publish.get("apply"),
                "property_count": lofty_pm_publish_property_count,
                "attempt_count": lofty_pm_publish_attempt_count,
                "failure_count": lofty_pm_publish_failure_count,
                "publish_result_count": lofty_pm_publish_result_count,
                "publish_failed_count": lofty_pm_publish_failed_count,
                "updates_publish_result_count": lofty_pm_updates_publish_result_count,
                "updates_publish_failed_count": lofty_pm_updates_publish_failed_count,
                "financial_publish_result_count": lofty_pm_financial_publish_result_count,
                "financial_publish_failed_count": lofty_pm_financial_publish_failed_count,
                "active_property_only_policy": lofty_pm_publish.get("active_property_only_policy"),
            },
        )
    lofty_financial_patch_status = lofty_financial_patch_readiness.get("status")
    if lofty_financial_patch_status in {"failed", "review", "missing", "unreadable"}:
        add_portfolio_blocker(
            f"lofty_financial_patch_readiness.{lofty_financial_patch_status}",
            lofty_financial_patch_readiness_path,
            {
                "status": lofty_financial_patch_status,
                "generated_at": lofty_financial_patch_readiness.get("generated_at"),
                "issue_count": lofty_financial_patch_readiness.get("issue_count"),
                "property_count": lofty_financial_patch_readiness.get("property_count"),
                "ready_financial_patch_count": lofty_financial_patch_readiness.get("ready_financial_patch_count"),
                "guard_reconcile_required_count": lofty_financial_patch_readiness.get("guard_reconcile_required_count"),
                "blocked_count": lofty_financial_patch_readiness.get("blocked_count"),
                "field_count_total": lofty_financial_patch_readiness.get("field_count_total"),
                "record_status_counts": lofty_financial_patch_readiness.get("record_status_counts"),
                "mutates_lofty_listing": lofty_financial_patch_readiness.get("mutates_lofty_listing"),
                "sends_owner_email": lofty_financial_patch_readiness.get("sends_owner_email"),
            },
        )

    guard_audit_has_records = bool(guard_audit.get("records") or [])
    for record in guarded_apply.get("records") or []:
        name = property_name(record)
        entry = ensure_property(name)
        for section in ("updates", "financials"):
            status = ((record.get(section) or {}).get("status"))
            if status == "guard_failed" and guard_audit_has_records:
                continue
            if (
                section == "updates"
                and status == "missing_monthly_draft"
                and safe_candidate_approval_duplicate_rent_roll_hold
                and property_identity_keys(record) & fallback_missing_draft_keys
            ):
                collapsed_missing_monthly_draft_count += 1
                continue
            if (
                section == "updates"
                and status == "needs_reviewed_entry"
                and safe_candidate_approval_duplicate_rent_roll_hold
                and owner_gate_updates_deferred_by_rent_roll
            ):
                collapsed_needs_reviewed_entry_count += 1
                continue
            if (
                status
                and status not in {"ok", "ready", "applied", "already_applied", "skipped_no_candidate"}
                and not status.startswith("skipped_")
                and not status.startswith("excluded_")
            ):
                key = f"{section}.{status}"
                counter[key] += 1
                item = {
                    "property_name": name,
                    "class": key,
                    "path": record.get("updates_md") if section == "updates" else record.get("financials_md"),
                    "next_action": next_action(key),
                }
                entry["blockers"].append(item)
                blockers.append(item)

    review_safety_scan_status = review_safety_scan.get("status")
    if review_safety_scan_status in {"failed", "review", "missing", "unreadable"}:
        key = f"review_safety_scan.{review_safety_scan_status}"
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(review_safety_scan_path),
            "next_action": lofty_cdp_preflight.get("next_action") or next_action(key),
        }
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)

    owner_gate_summary = owner_review_gate.get("summary") if isinstance(owner_review_gate.get("summary"), dict) else {}
    owner_skipped_count = count(owner_review_gate.get("property_skipped_count"))
    owner_external_excluded_count = count(
        owner_review_gate.get("property_external_excluded_count")
        if "property_external_excluded_count" in owner_review_gate
        else owner_gate_summary.get("property_external_excluded_count")
    )
    owner_excluded_total_present = (
        "property_excluded_total_count" in owner_review_gate
        or "property_excluded_total_count" in owner_gate_summary
    )
    owner_excluded_total_count = count(
        owner_review_gate.get("property_excluded_total_count")
        if "property_excluded_total_count" in owner_review_gate
        else owner_gate_summary.get("property_excluded_total_count")
    )
    live_update_skipped_count = count(live_capture.get("skipped_index_count"))
    live_financial_skipped_count = count(live_financial_capture.get("skipped_index_count"))
    live_update_excluded_count = count(live_capture.get("excluded_property_count"))
    live_financial_excluded_count = count(live_financial_capture.get("excluded_property_count"))
    publish_excluded_count = count(lofty_pm_publish.get("excluded_property_count"))
    publish_excluded_payload_file_count = count(lofty_pm_publish.get("excluded_payload_file_count"))
    publish_excluded_owner_email_candidate_count = count(lofty_pm_publish.get("excluded_owner_email_candidate_count"))
    skipped_index_counts_match = live_update_skipped_count == live_financial_skipped_count
    publish_exclusion_report_present = lofty_pm_publish.get("status") not in {"missing", "unreadable"}
    live_publish_exclusion_counts_match = (
        live_update_excluded_count == live_financial_excluded_count
        and (not publish_exclusion_report_present or live_update_excluded_count == publish_excluded_count)
    )
    owner_excluded_total_authoritative = owner_excluded_total_present and not (
        owner_excluded_total_count < live_update_excluded_count
        and live_update_excluded_count > 0
        and live_publish_exclusion_counts_match
    )
    total_exclusion_counts_match = (
        (
            owner_excluded_total_count == live_update_excluded_count == live_financial_excluded_count == publish_excluded_count
            if owner_excluded_total_authoritative
            else live_update_excluded_count == live_financial_excluded_count == publish_excluded_count
        )
        if publish_exclusion_report_present
        else (
            owner_excluded_total_count == live_update_excluded_count == live_financial_excluded_count
            if owner_excluded_total_authoritative
            else live_update_excluded_count == live_financial_excluded_count
        )
    )
    publish_exclusion_counts_match = (not publish_exclusion_report_present) or total_exclusion_counts_match
    skipped_exclusion_counts_match = skipped_index_counts_match
    publish_excluded_no_payload_or_email = (
        not publish_exclusion_report_present
        or (publish_excluded_payload_file_count == 0 and publish_excluded_owner_email_candidate_count == 0)
    )
    if not total_exclusion_counts_match:
        key = "monthly_review.skipped_exclusion_count_mismatch"
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(owner_review_gate_path),
            "next_action": lofty_cdp_preflight.get("next_action") or next_action(key),
            "detail": {
                "owner_review_gate_property_skipped_count": owner_skipped_count,
                "owner_review_gate_property_external_excluded_count": owner_external_excluded_count,
                "owner_review_gate_property_excluded_total_count": owner_excluded_total_count,
                "owner_review_gate_property_excluded_total_authoritative": owner_excluded_total_authoritative,
                "live_update_skipped_index_count": live_update_skipped_count,
                "live_financial_skipped_index_count": live_financial_skipped_count,
                "live_update_excluded_property_count": live_update_excluded_count,
                "live_financial_excluded_property_count": live_financial_excluded_count,
                "publish_excluded_property_count": publish_excluded_count,
                "skipped_index_counts_match": skipped_index_counts_match,
                "total_exclusion_counts_match": total_exclusion_counts_match,
                "live_update_skipped_index_status_counts": live_capture.get("skipped_index_status_counts"),
                "live_financial_skipped_index_status_counts": live_financial_capture.get("skipped_index_status_counts"),
                "live_update_skipped_index_digest": live_capture.get("skipped_index_digest"),
                "live_financial_skipped_index_digest": live_financial_capture.get("skipped_index_digest"),
                "live_update_skipped_index_records": live_capture.get("skipped_index_records"),
                "live_financial_skipped_index_records": live_financial_capture.get("skipped_index_records"),
            },
        }
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)
    if not publish_exclusion_counts_match or not publish_excluded_no_payload_or_email:
        key = "monthly_review.publish_exclusion_guard_failed"
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(lofty_pm_publish_path),
            "next_action": next_action(key),
            "detail": {
                "owner_review_gate_property_skipped_count": owner_skipped_count,
                "owner_review_gate_property_external_excluded_count": owner_external_excluded_count,
                "owner_review_gate_property_excluded_total_count": owner_excluded_total_count,
                "publish_excluded_property_count": publish_excluded_count,
                "publish_excluded_payload_file_count": publish_excluded_payload_file_count,
                "publish_excluded_owner_email_candidate_count": publish_excluded_owner_email_candidate_count,
                "active_property_only_policy": lofty_pm_publish.get("active_property_only_policy"),
            },
        }
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)

    if safe_candidate_approval_status in {"failed", "review", "unreadable"} and not safe_candidate_approval_duplicate_rent_roll_hold:
        key = f"safe_candidate_approval.{safe_candidate_approval_status}"
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(safe_candidate_approval_path),
            "next_action": lofty_cdp_preflight.get("next_action") or next_action(key),
        }
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)

    for record in guard_audit.get("records") or []:
        name = property_name(record)
        entry = ensure_property(name)
        checks = record.get("checks") or {}
        for section in ("updates", "financials"):
            status = ((checks.get(section) or {}).get("status"))
            if status and status != "ok":
                key = f"guard.{section}.{status}"
                counter[key] += 1
                item = {
                    "property_name": name,
                    "class": key,
                    "path": record.get("updates_md") if section == "updates" else record.get("financials_md"),
                    "next_action": live_guard_next_action(
                        section,
                        live_capture if section == "updates" else live_financial_capture,
                        listing_cleanup_summary if section == "updates" else None,
                    ),
                }
                entry["blockers"].append(item)
                blockers.append(item)

    for record in bootstrap.get("records") or []:
        name = property_name(record)
        entry = ensure_property(name)
        for remaining in record.get("remaining") or []:
            status = remaining.get("status")
            target = remaining.get("target")
            if status:
                key = f"bootstrap.{target}.{status}"
                counter[key] += 1
                item = {
                    "property_name": name,
                    "class": key,
                    "path": record.get("financials_md") if target == "financials" else record.get("updates_md"),
                    "next_action": next_action(key),
                }
                entry["blockers"].append(item)
                blockers.append(item)

    lofty_cdp_preflight_status = lofty_cdp_preflight.get("status")
    if lofty_cdp_preflight_status in {"failed", "review", "missing", "unreadable"}:
        key = f"lofty_cdp_preflight.{lofty_cdp_preflight_status}"
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(lofty_cdp_preflight_path),
            "detail": {
                "status": lofty_cdp_preflight.get("status"),
                "pm_tab_count": lofty_cdp_preflight.get("pm_tab_count"),
                "login_tab_count": lofty_cdp_preflight.get("login_tab_count"),
                "login_recovery_performed": lofty_cdp_preflight.get("login_recovery_performed"),
                "login_recovery_opened_property_owners": lofty_cdp_preflight.get("login_recovery_opened_property_owners"),
                "login_recovery_attempt_count": lofty_cdp_preflight.get("login_recovery_attempt_count")
                if "login_recovery_attempt_count" in lofty_cdp_preflight
                else len(lofty_cdp_preflight.get("login_recovery_attempts") or []),
                "login_recovery_try_count": lofty_cdp_preflight.get("login_recovery_try_count")
                if "login_recovery_try_count" in lofty_cdp_preflight
                else (
                    lofty_cdp_preflight.get("login_recovery_attempt_count")
                    if "login_recovery_attempt_count" in lofty_cdp_preflight
                    else len(lofty_cdp_preflight.get("login_recovery_attempts") or [])
                ),
                "login_recovery_hard_refresh_attempted": lofty_cdp_preflight.get("login_recovery_hard_refresh_attempted"),
                "login_recovery_closed_login_tab_count": lofty_cdp_preflight.get("login_recovery_closed_login_tab_count"),
                "login_recovery_reopened_property_owners_count": lofty_cdp_preflight.get("login_recovery_reopened_property_owners_count"),
                "manual_auth_required": lofty_cdp_preflight.get("manual_auth_required"),
                "manual_auth_reason": lofty_cdp_preflight.get("manual_auth_reason"),
                "next_action": lofty_cdp_preflight.get("next_action"),
            },
            "next_action": lofty_cdp_preflight.get("next_action") or next_action(key),
        }
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)

    live_capture_status = live_capture.get("status")
    live_update_capture_fresh = fresh_generated_at(live_capture, LIVE_CAPTURE_MAX_AGE_HOURS)
    if live_capture_status in {"failed", "review", "missing", "unreadable"}:
        key = f"live_update_capture.{live_capture_status}"
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(live_capture_path),
            "next_action": live_capture_next_action(live_capture, key, listing_cleanup_summary),
        }
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)
    elif live_capture_status == "ok" and not live_update_capture_fresh:
        key = "live_update_capture.stale"
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(live_capture_path),
            "next_action": live_capture_next_action(live_capture, key, listing_cleanup_summary),
            "detail": {
                "generated_at": live_capture.get("generated_at"),
                "report_age_hours": iso_age_hours(live_capture.get("generated_at")),
                "max_age_hours": LIVE_CAPTURE_MAX_AGE_HOURS,
                "target_digest": live_capture.get("target_digest"),
            },
        }
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)
    live_update_target_count = int(live_capture.get("target_count") or 0)
    live_update_check_ok_count = int(live_capture.get("check_ok_count") or 0)
    live_update_unverified_count = max(int(live_capture.get("unverified_count") or 0), live_update_target_count - live_update_check_ok_count)
    if live_capture_status == "ok" and live_update_unverified_count > 0:
        key = "live_update_capture.unverified"
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(live_capture_path),
            "next_action": live_capture_next_action(live_capture, key, listing_cleanup_summary),
            "detail": {
                "target_count": live_update_target_count,
                "check_ok_count": live_update_check_ok_count,
                "unverified_count": live_update_unverified_count,
                "target_digest": live_capture.get("target_digest"),
            },
        }
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)

    live_financial_capture_status = live_financial_capture.get("status")
    live_financial_capture_fresh = fresh_generated_at(live_financial_capture, LIVE_CAPTURE_MAX_AGE_HOURS)
    live_financial_target_count = int(live_financial_capture.get("target_count") or 0)
    live_financial_check_ok_count = int(live_financial_capture.get("check_ok_count") or 0)
    live_financial_apply_ready_count = sum(
        1
        for record in live_financial_capture.get("records") or []
        if isinstance(record, dict)
        and record.get("status") in LIVE_FINANCIAL_CAPTURE_READY_STATUSES
        and count(record.get("live_financials_length")) > 0
        and bool(str(record.get("snapshot_path") or record.get("next_action_file") or "").strip())
    )
    live_financial_unverified_count = max(
        int(live_financial_capture.get("unverified_count") or 0)
        if live_financial_capture_status not in {"ok", "review"}
        else 0,
        live_financial_target_count - live_financial_apply_ready_count,
    )
    if live_financial_capture_status in {"failed", "missing", "unreadable"}:
        key = f"live_financial_capture.{live_financial_capture_status}"
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(live_financial_capture_path),
            "next_action": live_capture_next_action(live_financial_capture, key),
        }
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)
    elif live_financial_capture_status in {"ok", "review"} and not live_financial_capture_fresh:
        key = "live_financial_capture.stale"
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(live_financial_capture_path),
            "next_action": live_capture_next_action(live_financial_capture, key),
            "detail": {
                "generated_at": live_financial_capture.get("generated_at"),
                "report_age_hours": iso_age_hours(live_financial_capture.get("generated_at")),
                "max_age_hours": LIVE_CAPTURE_MAX_AGE_HOURS,
                "target_digest": live_financial_capture.get("target_digest"),
            },
        }
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)
    elif live_financial_capture_status == "review" and live_financial_unverified_count > 0:
        key = "live_financial_capture.review"
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(live_financial_capture_path),
            "next_action": live_capture_next_action(live_financial_capture, key),
            "detail": {
                "target_count": live_financial_target_count,
                "apply_ready_count": live_financial_apply_ready_count,
                "check_ok_count": live_financial_check_ok_count,
                "unverified_count": live_financial_unverified_count,
                "target_digest": live_financial_capture.get("target_digest"),
            },
        }
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)
    if live_financial_capture_status == "ok" and live_financial_unverified_count > 0:
        key = "live_financial_capture.unverified"
        counter[key] += 1
        item = {
            "property_name": "portfolio",
            "class": key,
            "path": str(live_financial_capture_path),
            "next_action": live_capture_next_action(live_financial_capture, key),
            "detail": {
                "target_count": live_financial_target_count,
                "apply_ready_count": live_financial_apply_ready_count,
                "check_ok_count": live_financial_check_ok_count,
                "unverified_count": live_financial_unverified_count,
                "target_digest": live_financial_capture.get("target_digest"),
            },
        }
        ensure_property("portfolio")["blockers"].append(item)
        blockers.append(item)

    unique_blocked_properties = [entry for entry in by_property.values() if entry["blockers"]]
    operational_counts = {
        key: value for key, value in sorted(counter.items())
        if key.startswith("operational.")
    }
    owner_email_blocked = (
        bool(blockers)
        or source_quality_blocked
        or guarded_apply.get("status") != "ok"
        or review_safety_scan.get("status") not in {"ok", None}
        or guard_audit.get("status") != "ok"
        or tenant_ledger_guard.get("status") != "ok"
        or int(tenant_ledger_guard.get("issue_count") or 0) != 0
        or lofty_cdp_preflight.get("status") not in {"ok", None}
        or live_capture.get("status") not in {"ok", None}
        or live_financial_capture.get("status") not in {"ok", "review", None}
        or live_update_unverified_count > 0
        or live_financial_unverified_count > 0
    )
    blocker_actions_by_class = {}
    for blocker in blockers:
        blocker_class = str(blocker.get("class") or "")
        if blocker_class and blocker.get("next_action") and blocker_class not in blocker_actions_by_class:
            blocker_actions_by_class[blocker_class] = blocker.get("next_action")
    top_audit_blockers = [
        {"class": key, "count": value, "next_action": blocker_actions_by_class.get(key) or next_action(key)}
        for key, value in counter.most_common(8)
    ]
    if first_day_pm_fee_count:
        primary_blocker = {
            "class": "operational.first_day_pm_fee_audit.not_ok",
            "blocker": f"1st-day AOPS PM fee rows ({first_day_pm_fee_count})",
            "artifact": str(first_day_pm_fee_audit_path),
            "next_action": next_action("operational.first_day_pm_fee_audit.not_ok"),
            "hold": "Lofty PM publish and investor email",
        }
    elif source_quality_blocked:
        primary_blocker = {
            "class": "data_quality.ecogl_source_categories",
            "blocker": source_quality_gate["blocker"],
            "artifact": (
                str(ecogl_autonomy_path)
                if autonomy_downstream_hold
                else
                str(ecogl_source_fix_action_queue_path)
                if source_fix_needs_source_evidence_count
                else str(ecogl_source_fix_approval_path)
            ),
            "next_action": source_quality_gate["next_action"],
            "hold": "Lofty PM publish and investor email",
        }
    elif source_cash_balance_blocked:
        daily_source_cash_mismatches_block = (
            daily_source_cash_balance_current
            and count(daily_source_cash_balance.get("violation_count")) > 0
        )
        source_cash_issues = (
            [f"{count(daily_source_cash_balance.get('violation_count'))} workbook mismatches"]
            if daily_source_cash_mismatches_block
            else source_cash_issue_summary(
                source_cash_reconciliation_actions,
                source_cash_balance_violation_count,
                source_cash_balance_no_match_count,
                source_cash_balance_split_scope_missing_property_count,
            )
        )
        source_cash_first_action = active_source_cash_action_summary(source_cash_reconciliation_actions)
        if source_cash_reconciliation_actions_stale:
            source_cash_issues.append("source-cash reconciliation action report is stale")
        if zero_row_source_ledger_decision_missing:
            source_cash_issues.append(f"{len(zero_row_source_ledger_decision_missing)} zero-row active candidate decisions missing")
        source_cash_next_action = source_cash_reconciliation_actions.get("next_action") or next_action("operational.source_cash_balance.not_ok")
        if daily_source_cash_mismatches_block:
            raw_mortgage_guard = daily_source_cash_balance.get("raw_no_dao_mortgage_guard")
            raw_mortgage_count = count(raw_mortgage_guard.get("count")) if isinstance(raw_mortgage_guard, dict) else 0
            source_cash_next_action = (
                f"Reconcile the {raw_mortgage_count} raw no-DAO mortgage exception(s) and the "
                f"{count(daily_source_cash_balance.get('violation_count'))} direct source-cash mismatch(es); "
                "then regenerate source-cash reconciliation actions, rerun the daily source-cash audit, "
                "transfer reconciliation, and monthly readiness."
            )
        if source_cash_first_action and source_cash_first_action.get("action"):
            source_cash_next_action = f"{source_cash_first_action['action']} Then rerun daily source-cash audit, transfer reconciliation, and monthly readiness."
        if zero_row_source_ledger_decision_missing:
            source_cash_next_action = "Review each zero-row active monthly candidate and set an explicit include/exclude decision before monthly close. Then rerun daily source-cash audit, transfer reconciliation, and monthly readiness."
        primary_blocker = {
            "class": "operational.source_cash_balance.not_ok",
            "blocker": f"ECO GL source cash balance ({'; '.join(source_cash_issues)})",
            "artifact": (
                str(daily_source_cash_balance_path)
                if daily_source_cash_mismatches_block
                else str(source_cash_reconciliation_actions_path)
                if source_cash_reconciliation_actions.get("status") not in {"missing", "unreadable", None}
                else str(weekly_cf_sync_path)
            ),
            "next_action": source_cash_next_action,
            "hold": "Lofty PM publish and investor email",
        }
        if source_cash_first_action:
            primary_blocker["evidence"] = {"first_active_source_cash_action": source_cash_first_action}
        if daily_source_cash_mismatches_block:
            primary_blocker.setdefault("evidence", {})["daily_source_cash_balance"] = {
                "generated_at": daily_source_cash_balance.get("generated_at"),
                "violation_count": count(daily_source_cash_balance.get("violation_count")),
                "violation_properties": daily_source_cash_balance.get("violation_properties") or [],
                "raw_no_dao_mortgage_guard": daily_source_cash_balance.get("raw_no_dao_mortgage_guard"),
            }
        if zero_row_source_ledger_decision_missing:
            primary_blocker.setdefault("evidence", {})["zero_row_source_ledger_decision_missing_actions"] = zero_row_source_ledger_decision_missing[:25]
    elif blockers:
        priority_prefixes = [
            "operational.monthly_run",
            "operational.daily_sync_report",
            "operational.daily_sync",
            "operational.baselane_sync",
            "operational.daily_run",
            "operational.export_guard",
            "operational.local_model_preflight",
            "monthly_comms.rent_roll_gap_review",
            "monthly_comms.rent_roll_gap_approval_coverage",
            "operational.scheduler_audit",
            "operational.monthly_bank_statement_capture",
            "lofty_cdp_preflight",
            "live_update_capture",
            "live_financial_capture",
            "guard.updates",
            "guard.financials",
        ]
        first = next(
            (
                blocker
                for prefix in priority_prefixes
                for blocker in blockers
                if str(blocker.get("class") or "").startswith(prefix)
            ),
            blockers[0],
        )
        first_detail = first.get("detail") if isinstance(first.get("detail"), dict) else {}
        primary_artifact = first.get("path")
        primary_evidence = None
        if str(first.get("class") or "").startswith("monthly_comms.rent_roll"):
            primary_artifact = first_detail.get("queue_csv") or first.get("path")
            primary_evidence = first_detail.get("source_report") or first.get("path")
        scheduler_primary = {}
        nested_primary = {}
        if str(first.get("class") or "") == "operational.scheduler_audit.not_ok":
            actionable = first_detail.get("actionable_summary") if isinstance(first_detail.get("actionable_summary"), dict) else {}
            scheduler_primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
            if scheduler_primary:
                primary_artifact = scheduler_primary.get("artifact") or primary_artifact
        if str(first.get("class") or "") == "operational.weekly_file_updates.not_ok":
            nested_primary = first_detail.get("primary_blocker") if isinstance(first_detail.get("primary_blocker"), dict) else {}
            if nested_primary:
                primary_artifact = nested_primary.get("artifact") or primary_artifact
                primary_evidence = nested_primary.get("evidence") or primary_evidence
        delegated_primary = scheduler_primary or nested_primary
        primary_blocker = {
            "class": delegated_primary.get("class") or delegated_primary.get("id") or first.get("class"),
            "id": delegated_primary.get("id") or delegated_primary.get("class") or first.get("class"),
            "blocker": delegated_primary.get("blocker") or delegated_primary.get("class") or first.get("class"),
            "artifact": primary_artifact,
            "evidence": primary_evidence,
            "summary": delegated_primary.get("summary") or first.get("summary"),
            "next_action": delegated_primary.get("next_action") or first.get("next_action"),
            "hold": delegated_primary.get("hold") or "Lofty PM publish and investor email",
        }
    else:
        primary_blocker = {
            "class": None,
            "blocker": None,
            "artifact": None,
            "next_action": (
                "Post/review the Discord handoff and rerun the owner email send guard."
                if owner_email_final_send_blocked
                else "No action required; owner email gate is open."
            ),
            "hold": None,
        }
    primary_blocker = normalize_blocker(primary_blocker)
    primary_blocker_class = str(primary_blocker.get("class") or "")
    if primary_blocker_class:
        primary_top_blocker = {
            "class": primary_blocker_class,
            "count": counter.get(primary_blocker_class, 1),
            "next_action": primary_blocker.get("next_action") or blocker_actions_by_class.get(primary_blocker_class) or next_action(primary_blocker_class),
            "primary": True,
        }
        top_audit_blockers = [
            primary_top_blocker,
            *[
                blocker
                for blocker in top_audit_blockers
                if blocker.get("class") != primary_blocker_class
            ],
        ][:8]
    actionable_summary = {
        "primary_blocker": primary_blocker,
        "actionable_blocker_count": 1 if primary_blocker.get("blocker") else 0,
        "audit_blocker_count": len(blockers),
        "top_audit_blockers": top_audit_blockers,
        "source_quality_is_upstream_blocker": source_quality_blocked,
        "downstream_live_and_email_held": owner_email_blocked,
        "downstream_audit_collapsed": is_portfolio_upstream_blocker(primary_blocker.get("class")) and len(unique_blocked_properties) > 1,
        "blocked_property_audit_count": len(unique_blocked_properties),
        "noise_policy": "Use primary_blocker for operator action; counts/top_audit_blockers remain for transparency; per-property downstream detail is collapsed in markdown when an upstream portfolio blocker is active.",
    }
    primary_blocker_text = str(primary_blocker.get("blocker") or primary_blocker.get("class") or "").strip()
    actionable_blocker_count = actionable_summary["actionable_blocker_count"]
    owner_email_blocked_reason = (
        None
        if not owner_email_blocked
        else (
            f"monthly readiness owner_email_allowed=false; primary={primary_blocker_text}; "
            f"actionable={actionable_blocker_count}"
            if primary_blocker_text
            else f"monthly readiness owner_email_allowed=false; actionable={actionable_blocker_count}"
        )
    )
    blockers_bounded = sorted(
        blockers,
        key=lambda item: (
            str(item.get("class") or "") != primary_blocker_class,
            str(item.get("property_name") or ""),
            str(item.get("class") or ""),
        ),
    )[:25]
    report = {
        "generated_at": iso_z(),
        "run_month": run_month,
        "status": "ok" if not owner_email_blocked else "review",
        "owner_email_allowed": not owner_email_blocked,
        "owner_email_status": owner_email.get("status"),
        "owner_email_send_guard_status": owner_email_send_guard.get("status"),
        "owner_email_send_guard_ok": owner_email_send_guard.get("guard_ok"),
        "owner_email_send_guard_send_allowed": owner_email_send_guard.get("send_allowed"),
        "owner_email_send_guard_safe_block": owner_email_send_guard.get("safe_block"),
        "owner_email_final_send_blocked": owner_email_final_send_blocked,
        "owner_email_final_send_blocked_reason": (
            "Email is the final step and remains blocked until the reviewed Discord handoff and owner email packet send guard explicitly allow send."
            if owner_email_final_send_blocked
            else None
        ),
        "owner_email_send_guard_max_once_monthly_ok": owner_email_send_guard.get("max_once_monthly_ok"),
        "owner_email_send_guard_no_spam_guard_ok": owner_email_send_guard.get("no_spam_guard_ok"),
        "owner_email_send_guard_send_lock_file_unreadable": owner_email_send_guard.get("send_lock_file_unreadable") is True,
        "owner_email_send_guard_manual_exclusions_ok": owner_email_active_proof["manual_exclusions_ok"],
        "owner_email_send_guard_yhome_transition_guard_ok": owner_email_active_proof["yhome_transition_guard_ok"],
        "owner_email_send_guard_yhome_transition_guard_column_b_rule_ok": owner_email_active_proof["yhome_transition_guard_column_b_rule_ok"],
        "owner_email_send_guard_yhome_transition_guard_column_b_header": owner_email_active_proof["yhome_transition_guard_column_b_header"],
        "owner_email_send_guard_yhome_transition_guard_column_b_marker_count": owner_email_active_proof["yhome_transition_guard_column_b_marker_count"],
        "owner_email_send_guard_active_property_policy_mentions_yhome": owner_email_active_proof["active_property_policy_mentions_yhome"],
        "owner_email_send_guard_active_property_policy_mentions_manual_exclusions": owner_email_active_proof["active_property_policy_mentions_manual_exclusions"],
        "owner_email_send_guard_excluded_owner_email_candidate_count": owner_email_active_proof["excluded_owner_email_candidate_count"],
        "owner_email_send_guard_active_property_proof_ok": owner_email_active_proof["ok"],
        "owner_email_blocked_reason": owner_email_blocked_reason,
        "blocker_count": len(blockers),
        "blocker_class_counts": dict(sorted(counter.items())),
        "blockers_bounded": blockers_bounded,
        "blockers_bounded_count": len(blockers_bounded),
        "primary_blocker": primary_blocker,
        "next_action": primary_blocker.get("next_action"),
        "hold": primary_blocker.get("hold"),
        "actionable_summary": actionable_summary,
        "listing_cleanup_summary": listing_cleanup_summary,
        "commands": {
            "hemlane_cdp_dry_run": hemlane_cdp_command(comms_root, run_month),
            "post_auth_resume": POST_AUTH_RESUME_COMMAND,
        },
        "data_quality_gate": source_quality_gate,
        "operational_blocker_count": sum(operational_counts.values()),
        "operational_counts": operational_counts,
        "blocked_property_count": len(unique_blocked_properties),
        "counts": dict(sorted(counter.items())),
        "artifacts": {
            "daily_run": str(daily_run_path),
            "daily_sync_report": str(daily_sync_report_path),
            "baselane_sync": str(sync_report_path),
            "export_guard": str(export_guard_path),
            "weekly_file_updates": str(weekly_file_updates_path),
            "weekly_unprocessed": str(weekly_unprocessed_path),
            "weekly_cf_sync": str(weekly_cf_sync_path),
            "weekly_cf_review_gate": str(weekly_cf_gate_path),
            "source_cash_reconciliation_actions": str(source_cash_reconciliation_actions_path),
            "monthly_bank_statement_capture": str(monthly_statements_gate_path),
            "monthly_bank_statement_download": str(monthly_statements_download_path),
            "first_day_pm_fee_audit": str(first_day_pm_fee_audit_path),
            "first_day_pm_fee_quarantine": str(first_day_pm_fee_quarantine_path),
            "first_day_pm_fee_source_cleanup_plan": str(first_day_pm_fee_cleanup_path),
            "first_day_pm_fee_source_cleanup_actions": str(first_day_pm_fee_cleanup_actions_path),
            "ecogl_autonomy": str(ecogl_autonomy_path),
            "ecogl_source_fix": str(ecogl_source_fix_path),
            "ecogl_source_fix_actions": str(ecogl_source_fix_actions_path),
            "ecogl_source_fix_corrections": str(ecogl_source_fix_corrections_path),
            "ecogl_source_fix_correction_validation": str(ecogl_source_fix_correction_validation_path),
            "ecogl_source_fix_correction_validation_csv": str(ecogl_source_fix_correction_validation_csv_path),
            "ecogl_source_fix_apply_plan": str(ecogl_source_fix_apply_plan_path),
            "ecogl_source_fix_apply_plan_csv": str(ecogl_source_fix_apply_plan_csv_path),
            "ecogl_source_fix_evidence": str(ecogl_source_fix_evidence_path),
            "scheduler_audit": str(scheduler_audit_path),
            "local_model_preflight": str(local_model_preflight_path),
            "public_path_guard": str(public_path_guard_path),
            "tenant_ledger_folder_guard": str(tenant_ledger_guard_path),
            "guarded_apply": str(guarded_apply_path),
            "owner_review_gate": str(owner_review_gate_path),
            "review_safety_scan": str(review_safety_scan_path),
            "guard_audit": str(guard_audit_path),
            "doc_bootstrap": str(bootstrap_path),
            "hemlane_cdp_preflight": str(hemlane_cdp_preflight_path),
            "lofty_cdp_preflight": str(lofty_cdp_preflight_path),
            "live_update_capture": str(live_capture_path),
            "live_financial_capture": str(live_financial_capture_path),
            "listing_cleanup_queue": str(listing_cleanup_queue_path),
            "listing_cleanup_dry_run_verify": str(listing_cleanup_dry_run_verify_path),
            "listing_cleanup_apply_preflight": str(listing_cleanup_apply_preflight_path),
            "safe_candidate_approval": str(safe_candidate_approval_path),
            "owner_email_diagnostic": str(owner_email_path),
            "owner_email_send_guard": str(owner_email_send_guard_path),
            "lofty_pm_publish": str(lofty_pm_publish_path),
            "lofty_financial_patch_readiness": str(lofty_financial_patch_readiness_path),
            "monthly_run": str(monthly_run_path),
            "rent_roll_gap_review": str(rent_roll_gap_review_path),
            "rent_roll_gap_queue_csv": rent_roll_gap_queue_csv,
            "markdown": str(markdown_path),
        },
        "operational_gates": operational_gates,
        "monthly_comms_gates": {
            "run_month": run_month,
            "rent_roll_gap_review_status": rent_roll_gap_review.get("status"),
            "rent_roll_source_report": str(rent_roll_source_path),
            "rent_roll_source_report_status": rent_roll_source.get("status"),
            "rent_roll_source_freshness_status": rent_roll_source.get("freshness_status"),
            "rent_roll_source_owner_email_allowed": rent_roll_source.get("owner_email_allowed"),
            "rent_roll_source_live_update_allowed": rent_roll_source.get("live_update_allowed"),
            "hemlane_cdp_capture_status": hemlane_cdp_capture.get("status"),
            "hemlane_cdp_capture_issue": hemlane_cdp_capture.get("issue"),
            "hemlane_cdp_capture_manual_auth_reason": hemlane_cdp_capture.get("manual_auth_reason"),
            "rent_roll_gap_count": rent_roll_gap_review.get("gap_count"),
            "rent_roll_pending_gap_count": rent_roll_gap_review.get("pending_gap_count"),
            "rent_roll_target_scoped": rent_roll_target_scoped,
            "rent_roll_target_property_count": rent_roll_target_gaps.get("target_property_count"),
            "rent_roll_target_pending_gap_count": rent_roll_target_pending_gap_count,
            "rent_roll_non_target_pending_gap_count": rent_roll_target_gaps.get("non_target_pending_gap_count"),
            "rent_roll_target_pending_gap_properties": rent_roll_target_gaps.get("target_pending_gap_properties"),
            "rent_roll_stale_export_dates": rent_roll_gap_review.get("stale_export_dates"),
            "rent_roll_pending_stale_export_date_count": rent_roll_gap_review.get("pending_stale_export_date_count"),
            "rent_roll_gap_queue_digest": rent_roll_gap_review.get("action_queue_digest"),
            "rent_roll_gap_approval_template_coverage_status": rent_roll_approval_coverage.get("status"),
            "rent_roll_gap_approval_template_digest": rent_roll_gap_review.get("approval_template_digest") or rent_roll_approval_coverage.get("digest"),
            "rent_roll_gap_queue_csv": rent_roll_gap_queue_csv,
            "rent_roll_gap_approval": rent_roll_gap_review.get("approval_path"),
            "hemlane_cdp_preflight_status": hemlane_cdp_preflight.get("status"),
            "hemlane_cdp_preflight_issue_summary": hemlane_cdp_preflight.get("issue_summary"),
            "hemlane_cdp_available": hemlane_cdp_preflight.get("cdp_available"),
            "hemlane_logged_in_tab_count": hemlane_cdp_preflight.get("logged_in_tab_count"),
            "hemlane_login_tab_count": hemlane_cdp_preflight.get("login_tab_count"),
            "hemlane_rent_roll_tab_count": hemlane_cdp_preflight.get("rent_roll_tab_count"),
            "hemlane_login_recovery_attempt_count": hemlane_cdp_preflight.get("login_recovery_attempt_count")
            if "login_recovery_attempt_count" in hemlane_cdp_preflight
            else len(hemlane_cdp_preflight.get("login_recovery_attempts") or []),
            "hemlane_login_recovery_try_count": hemlane_cdp_preflight.get("login_recovery_try_count")
            if "login_recovery_try_count" in hemlane_cdp_preflight
            else (
                hemlane_cdp_preflight.get("login_recovery_attempt_count")
                if "login_recovery_attempt_count" in hemlane_cdp_preflight
                else len(hemlane_cdp_preflight.get("login_recovery_attempts") or [])
            ),
            "hemlane_manual_auth_required": hemlane_cdp_preflight.get("manual_auth_required"),
            "hemlane_manual_auth_reason": hemlane_cdp_preflight.get("manual_auth_reason"),
            "hemlane_next_action": hemlane_aux_next_action(hemlane_cdp_capture, rent_roll_source, hemlane_cdp_preflight),
            "lofty_cdp_preflight_status": lofty_cdp_preflight.get("status"),
            "lofty_cdp_preflight_issue_summary": lofty_cdp_preflight.get("issue_summary"),
            "lofty_pm_tab_count": lofty_cdp_preflight.get("pm_tab_count"),
            "lofty_login_tab_count": lofty_cdp_preflight.get("login_tab_count"),
            "lofty_login_recovery_performed": lofty_cdp_preflight.get("login_recovery_performed"),
            "lofty_login_recovery_opened_property_owners": lofty_cdp_preflight.get("login_recovery_opened_property_owners"),
            "lofty_login_recovery_attempt_count": lofty_cdp_preflight.get("login_recovery_attempt_count")
            if "login_recovery_attempt_count" in lofty_cdp_preflight
            else len(lofty_cdp_preflight.get("login_recovery_attempts") or []),
            "lofty_login_recovery_try_count": lofty_cdp_preflight.get("login_recovery_try_count")
            if "login_recovery_try_count" in lofty_cdp_preflight
            else (
                lofty_cdp_preflight.get("login_recovery_attempt_count")
                if "login_recovery_attempt_count" in lofty_cdp_preflight
                else len(lofty_cdp_preflight.get("login_recovery_attempts") or [])
            ),
            "lofty_login_recovery_hard_refresh_attempted": lofty_cdp_preflight.get("login_recovery_hard_refresh_attempted"),
            "lofty_login_recovery_closed_login_tab_count": lofty_cdp_preflight.get("login_recovery_closed_login_tab_count"),
            "lofty_login_recovery_reopened_property_owners_count": lofty_cdp_preflight.get("login_recovery_reopened_property_owners_count"),
            "lofty_manual_auth_required": lofty_cdp_preflight.get("manual_auth_required"),
            "lofty_manual_auth_reason": lofty_cdp_preflight.get("manual_auth_reason"),
            "lofty_next_action": lofty_cdp_preflight.get("next_action"),
            "owner_email_send_guard_status": owner_email_send_guard.get("status"),
            "owner_email_send_guard_ok": owner_email_send_guard.get("guard_ok"),
            "owner_email_send_guard_send_allowed": owner_email_send_guard.get("send_allowed"),
            "owner_email_send_guard_safe_block": owner_email_send_guard.get("safe_block"),
            "owner_email_send_guard_max_once_monthly_ok": owner_email_send_guard.get("max_once_monthly_ok"),
            "owner_email_send_guard_no_spam_guard_ok": owner_email_send_guard.get("no_spam_guard_ok"),
            "owner_email_send_guard_issue_count": owner_email_send_guard.get("issue_count"),
            "owner_email_send_guard_send_lock_file_unreadable": owner_email_send_guard.get("send_lock_file_unreadable") is True,
            "owner_email_send_guard_sent_state_file_matches_run_month": owner_email_send_guard.get("sent_state_file_matches_run_month"),
            "owner_email_send_guard_idempotency_proof": owner_email_send_guard.get("idempotency_proof"),
            "owner_email_send_guard_manual_exclusions_ok": owner_email_active_proof["manual_exclusions_ok"],
            "owner_email_send_guard_yhome_transition_guard_ok": owner_email_active_proof["yhome_transition_guard_ok"],
            "owner_email_send_guard_yhome_transition_guard_column_b_rule_ok": owner_email_active_proof["yhome_transition_guard_column_b_rule_ok"],
            "owner_email_send_guard_yhome_transition_guard_column_b_header": owner_email_active_proof["yhome_transition_guard_column_b_header"],
            "owner_email_send_guard_yhome_transition_guard_column_b_marker_count": owner_email_active_proof["yhome_transition_guard_column_b_marker_count"],
            "owner_email_send_guard_active_property_policy_mentions_yhome": owner_email_active_proof["active_property_policy_mentions_yhome"],
            "owner_email_send_guard_active_property_policy_mentions_manual_exclusions": owner_email_active_proof["active_property_policy_mentions_manual_exclusions"],
            "owner_email_send_guard_excluded_owner_email_candidate_count": owner_email_active_proof["excluded_owner_email_candidate_count"],
            "owner_email_send_guard_active_property_proof_ok": owner_email_active_proof["ok"],
            "safe_candidate_approval_status": safe_candidate_approval.get("status"),
            "safe_candidate_approval_duplicate_rent_roll_hold": safe_candidate_approval_duplicate_rent_roll_hold,
            "safe_candidate_approval_approved_financial_count": safe_candidate_approval.get("approved_financial_count"),
            "safe_candidate_approval_approved_update_count": safe_candidate_approval.get("approved_update_count"),
            "review_candidate_packet_status": review_candidate_packet.get("status"),
            "review_candidate_packet_record_count": review_candidate_record_count,
            "review_candidate_packet_manifest_record_count": review_candidate_manifest_record_count,
            "review_candidate_packet_issue_count": review_candidate_packet.get("issue_count"),
            "review_candidate_packet_marker_count": review_candidate_packet.get("marker_count"),
            "review_candidate_packet_financial_gate_issue_count": review_candidate_packet.get("financial_candidate_gate_issue_count"),
            "review_candidate_packet_manifest_source_issue_count": review_candidate_manifest_source_issue_count,
            "review_candidate_packet_manifest_source_issues": review_candidate_manifest_source_issues,
            "review_candidate_packet_partial_candidate_coverage": review_candidate_partial_coverage,
            "fallback_missing_monthly_draft_candidate_count": fallback_missing_draft_count,
            "missing_monthly_draft_collapsed_by_rent_roll_hold_count": collapsed_missing_monthly_draft_count,
            "owner_gate_updates_deferred_by_rent_roll": owner_gate_updates_deferred_by_rent_roll,
            "needs_reviewed_entry_collapsed_by_rent_roll_hold_count": collapsed_needs_reviewed_entry_count,
        },
        "monthly_owner_review_gate": {
            "status": owner_review_gate.get("status"),
            "blocker_count": owner_review_gate.get("blocker_count"),
            "idempotency_key": owner_review_gate.get("idempotency_key"),
            "property_checklist_digest": owner_review_gate.get("property_checklist_digest"),
            "guard_workflow_coverage_status": owner_guard_workflow.get("status"),
            "guard_workflow_digest": owner_guard_workflow.get("digest"),
        },
        "monthly_guarded_apply": guarded_apply_counts,
        "monthly_apply_publish_gates": {
            "guarded_apply_status": guarded_apply.get("status"),
            "guarded_apply_generated_at": guarded_apply.get("generated_at"),
            "guarded_apply_report_age_hours": iso_age_hours(guarded_apply.get("generated_at")),
            "guarded_apply_fresh": guarded_apply_fresh,
            "guarded_apply_max_age_hours": MONTHLY_GUARDED_APPLY_MAX_AGE_HOURS,
            "lofty_pm_publish_status": lofty_pm_publish.get("status"),
            "lofty_pm_publish_generated_at": lofty_pm_publish.get("generated_at"),
            "lofty_pm_publish_report_age_hours": iso_age_hours(lofty_pm_publish.get("generated_at")),
            "lofty_pm_publish_fresh": lofty_pm_publish_fresh,
            "lofty_pm_publish_max_age_hours": LOFTY_PM_PUBLISH_MAX_AGE_HOURS,
            "lofty_pm_publish_issue_count": lofty_pm_publish.get("issue_count"),
            "lofty_pm_publish_guarded_apply_status": lofty_pm_publish.get("guarded_apply_status"),
            "lofty_pm_publish_has_apply_evidence": lofty_pm_publish_has_apply_evidence,
            "lofty_pm_publish_apply": lofty_pm_publish.get("apply"),
            "lofty_pm_publish_property_count": lofty_pm_publish_property_count,
            "lofty_pm_publish_attempt_count": lofty_pm_publish_attempt_count,
            "lofty_pm_publish_failure_count": lofty_pm_publish_failure_count,
            "lofty_pm_publish_result_count": lofty_pm_publish_result_count,
            "lofty_pm_publish_failed_count": lofty_pm_publish_failed_count,
            "lofty_pm_updates_publish_result_count": lofty_pm_updates_publish_result_count,
            "lofty_pm_updates_publish_failed_count": lofty_pm_updates_publish_failed_count,
            "lofty_pm_financial_publish_result_count": lofty_pm_financial_publish_result_count,
            "lofty_pm_financial_publish_failed_count": lofty_pm_financial_publish_failed_count,
            "lofty_financial_patch_readiness_status": lofty_financial_patch_readiness.get("status"),
            "lofty_financial_patch_readiness_issue_count": lofty_financial_patch_readiness.get("issue_count"),
            "lofty_financial_patch_ready_count": lofty_financial_patch_readiness.get("ready_financial_patch_count"),
            "lofty_financial_patch_guard_reconcile_required_count": lofty_financial_patch_readiness.get("guard_reconcile_required_count"),
            "lofty_financial_patch_blocked_count": lofty_financial_patch_readiness.get("blocked_count"),
            "lofty_financial_patch_field_count_total": lofty_financial_patch_readiness.get("field_count_total"),
            "lofty_financial_patch_record_status_counts": lofty_financial_patch_readiness.get("record_status_counts"),
        },
        "monthly_skip_policy": {
            "owner_review_gate_property_skipped_count": owner_skipped_count,
            "owner_review_gate_property_external_excluded_count": owner_external_excluded_count,
            "owner_review_gate_property_excluded_total_count": owner_excluded_total_count,
            "owner_review_gate_property_excluded_total_authoritative": owner_excluded_total_authoritative,
            "live_update_skipped_index_count": live_update_skipped_count,
            "live_financial_skipped_index_count": live_financial_skipped_count,
            "live_update_excluded_property_count": live_update_excluded_count,
            "live_financial_excluded_property_count": live_financial_excluded_count,
            "live_update_skipped_index_digest": live_capture.get("skipped_index_digest"),
            "live_financial_skipped_index_digest": live_financial_capture.get("skipped_index_digest"),
            "live_update_skipped_index_records": live_capture.get("skipped_index_records"),
            "live_financial_skipped_index_records": live_financial_capture.get("skipped_index_records"),
            "skipped_index_counts_match": skipped_index_counts_match,
            "total_exclusion_counts_match": total_exclusion_counts_match,
            "skipped_exclusion_counts_match": skipped_exclusion_counts_match,
            "policy": "sold/delisted/closed properties must stay excluded from live updates and owner email; total excluded targets are blocking, while skipped-vs-external subclass buckets are diagnostic.",
            "lofty_pm_publish_excluded_property_count": publish_excluded_count,
            "lofty_pm_publish_excluded_payload_file_count": publish_excluded_payload_file_count,
            "lofty_pm_publish_excluded_owner_email_candidate_count": publish_excluded_owner_email_candidate_count,
            "lofty_pm_publish_exclusion_counts_match": publish_exclusion_counts_match,
            "lofty_pm_publish_excluded_no_payload_or_email": publish_excluded_no_payload_or_email,
            "active_property_only_policy": lofty_pm_publish.get("active_property_only_policy"),
        },
        "blocked_properties": sorted(unique_blocked_properties, key=lambda item: item["property_name"]),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return report


def rent_roll_next_action(
    key: str,
    rent_roll_gap_review: dict,
    rent_roll_source: dict,
    hemlane_cdp_preflight: dict,
    comms_root: Path | None = None,
    hemlane_cdp_capture: dict | None = None,
) -> str:
    freshness = str(rent_roll_source.get("freshness_status") or "").strip()
    run_month = str(rent_roll_source.get("run_month") or "YYYY-MM").strip()
    comms_root = comms_root or default_comms_root()
    command = hemlane_cdp_command(comms_root, run_month)
    source_current = rent_roll_source.get("source_current")
    pending_stale = count(rent_roll_gap_review.get("pending_stale_export_date_count"))
    hemlane_at_login = (
        hemlane_cdp_preflight.get("status") == "review"
        and hemlane_cdp_preflight.get("cdp_available") is True
        and count(hemlane_cdp_preflight.get("login_tab_count")) > 0
        and count(hemlane_cdp_preflight.get("logged_in_tab_count")) == 0
    )
    hemlane_recovery_opened_rent_roll = hemlane_cdp_preflight.get("login_recovery_opened_rent_roll") is True
    hemlane_preflight_recovery_exhausted = hemlane_capture_recovery_exhausted(hemlane_cdp_preflight)
    hemlane_recovery_attempt_count = (
        hemlane_cdp_preflight.get("login_recovery_try_count")
        if "login_recovery_try_count" in hemlane_cdp_preflight
        else (
            hemlane_cdp_preflight.get("login_recovery_attempt_count")
            if "login_recovery_attempt_count" in hemlane_cdp_preflight
            else len(hemlane_cdp_preflight.get("login_recovery_attempts") or [])
        )
    )
    source_next_action = str(rent_roll_source.get("next_action") or "").strip()
    capture = hemlane_cdp_capture if isinstance(hemlane_cdp_capture, dict) else {}
    capture_current = capture.get("status") not in {"missing", "unreadable", None}
    preflight_newer_than_capture = first_report_newer(hemlane_cdp_preflight, capture, rent_roll_source)
    if (
        key.startswith("monthly_comms.rent_roll_gap_review")
        and (freshness == "stale" or source_current is False or pending_stale)
        and hemlane_preflight_needs_open_tab(hemlane_cdp_preflight)
        and preflight_newer_than_capture
    ):
        return hemlane_open_tab_action()
    if (
        key.startswith("monthly_comms.rent_roll_gap_review")
        and (freshness == "stale" or source_current is False or pending_stale)
        and hemlane_at_login
        and hemlane_recovery_opened_rent_roll
        and preflight_newer_than_capture
    ):
        attempts = count(hemlane_recovery_attempt_count)
        return hemlane_visible_login_after_recovery_action(attempts or None)
    if (
        key.startswith("monthly_comms.rent_roll_gap_review")
        and (freshness == "stale" or source_current is False or pending_stale)
        and rent_roll_source.get("hemlane_capture_issue") == "recaptcha_required"
        and (not capture_current or hemlane_capture_recaptcha_required(capture))
        and hemlane_bitwarden_submitted(capture, rent_roll_source)
    ):
        return hemlane_recaptcha_after_bitwarden_action()
    if (
        key.startswith("monthly_comms.rent_roll_gap_review")
        and (freshness == "stale" or source_current is False or pending_stale)
        and hemlane_at_login
        and preflight_newer_than_capture
    ):
        attempts = count(hemlane_recovery_attempt_count)
        return hemlane_login_screen_recovery_action(attempts or None, captcha_conditional=hemlane_capture_recaptcha_required(capture) or "recaptcha" in source_next_action.lower())
    if (
        key.startswith("monthly_comms.rent_roll_gap_review")
        and (freshness == "stale" or source_current is False or pending_stale)
        and rent_roll_source.get("hemlane_capture_issue") == "recaptcha_required"
        and (not capture_current or hemlane_capture_recaptcha_required(capture))
        and source_next_action
    ):
        if hemlane_bitwarden_submitted(capture, rent_roll_source):
            return hemlane_recaptcha_after_bitwarden_action()
        attempts = hemlane_capture_attempt_count(capture) or count(hemlane_recovery_attempt_count)
        return hemlane_login_screen_recovery_action(attempts, captcha_conditional=True)
    if (
        key.startswith("monthly_comms.rent_roll_gap_review")
        and (freshness == "stale" or source_current is False or pending_stale)
        and capture_current
        and hemlane_capture_login_required(capture)
    ):
        if hemlane_capture_recaptcha_required(capture) and hemlane_bitwarden_submitted(capture, rent_roll_source):
            return hemlane_recaptcha_after_bitwarden_action()
        attempts = hemlane_capture_attempt_count(capture) or hemlane_recovery_attempt_count
        suffix = " "
        if attempts:
            suffix = f" ({attempts} tries); "
        if hemlane_capture_recovery_exhausted(capture):
            return hemlane_login_screen_recovery_action(attempts)
        return (
            f"Hard refresh or close/open the Hemlane rent-roll tab{suffix}"
            f"authenticate only if still redirected, then run `{POST_AUTH_RESUME_COMMAND}`. "
            "It refreshes Hemlane rent-roll evidence, monthly dry-run readiness, and EOD reporting while keeping owner email, Lofty PM publish, and guarded live writes disabled."
        )
    if (
        key.startswith("monthly_comms.rent_roll_gap_review")
        and (freshness == "stale" or source_current is False or pending_stale)
        and hemlane_at_login
    ):
        if hemlane_recovery_opened_rent_roll and hemlane_recovery_attempt_count:
            return hemlane_visible_login_after_recovery_action(hemlane_recovery_attempt_count)
        return hemlane_login_screen_recovery_action()
    if key.startswith("monthly_comms.rent_roll_gap_review") and (freshness == "stale" or source_current is False or pending_stale) and source_next_action:
        return source_next_action
    if key.startswith("monthly_comms.rent_roll_gap_review") and (freshness == "stale" or source_current is False or pending_stale):
        return hemlane_post_auth_action("Capture or authenticate a current Hemlane rent roll for the run month")
    return next_action(key)


def next_action(key: str) -> str:
    if "missing_financials_md" in key or "missing_verified_local_source" in key:
        return "Recover or fetch verified financial source, then create canonical Public/00 - README & Property Snapshot/FINANCIALS.md."
    if "no_approved_financials_draft" in key:
        return "Review financials draft, save as approved/reviewed financials artifact, then rerun guarded apply."
    if "needs_reviewed_entry" in key:
        return "Review owner update draft for public safety, save as approved/reviewed update artifact, then rerun guarded apply."
    if "lofty_cdp_preflight" in key:
        return "Hard-refresh or close/open Lofty property-owners tab; authenticate only if still redirected, then rerun monthly readiness."
    if "review_safety_scan" in key:
        return "Clear high/medium review-safety scan findings from update and financial approval artifacts before publish/send."
    if "monthly_guarded_apply" in key:
        return "Rerun monthly guarded apply after fresh approvals and live guard captures; do not publish or email from stale/failed apply evidence."
    if "lofty_pm_publish" in key:
        return "Rerun Lofty PM publish in dry-run or guarded apply mode until active-property-only publish evidence is fresh and ok."
    if "guard.updates" in key:
        return (
            "Auth Lofty visible tab (3 tries), then refresh live UPDATES.md guard evidence through the safe monthly dry-run. "
            f"Run `{SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}`; this keeps email, Lofty PM publish, and guarded live writes disabled."
        )
    if "live_update_capture" in key:
        return "Run Lofty PM live update capture with authenticated CDP, reconcile mismatches, then rerun monthly readiness."
    if "live_financial_capture" in key:
        return "Run Lofty PM live financial capture with authenticated CDP, reconcile missing/mismatched FINANCIALS.md, then rerun monthly readiness."
    if "safe_candidate_approval" in key:
        return "Fix safe candidate approval inputs or safety findings, then rerun monthly readiness before guarded apply/publish."
    if "guard.financials" in key:
        return (
            "Auth Lofty visible tab (3 tries), then refresh live FINANCIALS.md guard evidence through the safe monthly dry-run. "
            f"Run `{SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}`; this keeps email, Lofty PM publish, and guarded live writes disabled."
        )
    if key.startswith("operational.daily_run"):
        return "Fix the deterministic daily Baselane run and verify the latest daily run report is ok before monthly publish/email."
    if key.startswith("operational.monthly_run"):
        return "Rerun the monthly cron only after the failed monthly run step is resolved; keep owner email, Lofty PM publish, and guarded live writes disabled until the monthly run report is ok."
    if key.startswith("operational.daily_sync_report"):
        return "Reconcile the canonical Baselane daily sync report so wrapper status, deterministic sync status, and required steps agree before monthly publish/email."
    if key.startswith("operational.baselane_sync"):
        return "Fix Baselane CDP sync/export and verify the sync report is ok before monthly publish/email."
    if key.startswith("operational.export_guard"):
        return "Verify the Baselane export guard proves all selected rows were exported locally before monthly publish/email."
    if key.startswith("operational.weekly_file_updates"):
        return "Resolve weekly file update review/failures and rerun the weekly cron until status is ok."
    if key.startswith("operational.weekly_duplicates"):
        return "Review duplicate ledger keys, correct source duplicates or explicitly allow intentional duplicates before monthly publish/email."
    if key.startswith("operational.source_cash_balance"):
        return "Inspect reports/cf_statement_sync/audit_*.json and the workbook ECO GL Net Cash Balance row, fix raw GL/source-cash mismatch, then rerun bash scripts/baselane_weekly_file_updates_cron.sh."
    if key.startswith("operational.first_day_pm_fee_audit"):
        return "Run BASELANE_FIRST_DAY_PM_FEE_SOURCE_CLEANUP_APPLY=1 bash scripts/baselane_first_day_pm_fee_cleanup_then_refresh.sh to delete the exact 1st-day AOPS PM-fee source rows from the local ECO GL CSV with backup and refresh daily/weekly/monthly evidence; derived reporting quarantine is containment only."
    if key.startswith("operational.weekly_cf_sync"):
        return "Resolve CF audit errors/conflicts/untagged GL review items and rerun CF sync until status is ok."
    if key.startswith("owner_email.active_property_guard"):
        return "Regenerate owner email send-guard proof from Yhome Transition Reconciliation and manual exclusions; sold/delisted/excluded properties must produce zero owner-email candidates."
    if key.startswith("owner_email.send_guard"):
        return "Fix owner email send-guard issues; require matching send evidence and current-month sent-state file before investor email."
    if key.startswith("monthly_candidate_packet.financial_gate_issues"):
        return "Replace generated-ledger FINANCIALS review candidates with reviewed monthly financial snapshots before guarded apply, Lofty PM publish, or owner email."
    if key.startswith("operational.weekly_cf_review_gate"):
        return "Work reports/baselane_weekly_cf_review_gate.csv, use reports/baselane_weekly_cf_review_gate.md for detail, then rerun weekly/monthly readiness."
    if key.startswith("operational.monthly_bank_statement_capture"):
        return "Run BASELANE_MONTHLY_STATEMENTS_GATE_ONLY=1 bash scripts/baselane_monthly_statements_idempotent.sh to refresh verified current target-month bank statement evidence, then rerun monthly readiness."
    if key.startswith("operational.local_model_preflight"):
        return "Restore local qwen preflight health for deterministic small-model operation, or run monthly readiness with BASELANE_REQUIRE_LOCAL_MODEL_PREFLIGHT_FOR_MONTHLY_CLOSE=0 when financial-source gates are otherwise clean."
    if key.startswith("operational.scheduler_audit"):
        return "Send one concise non-dry-run EOD Telegram report after approval, then rerun scheduler audit so eod_telegram has current send proof."
    if key.startswith("operational.public_path_guard"):
        return "Remove legacy Financials-folder targets and rerun the public path guard before monthly publish/email."
    if key.startswith("operational.tenant_ledger_folder_guard"):
        return "Move or remove cross-property tenant ledger files, rerun scripts/lofty_tenant_ledger_folder_guard.py, then rerun monthly readiness before publish/email."
    if key.startswith("monthly_comms.rent_roll_gap_approval_coverage"):
        return "Regenerate the rent-roll gap review so the approval template covers every current gap/stale-export row before monthly publish/email."
    if key.startswith("monthly_comms.rent_roll_gap_review"):
        return "Work the rent-roll queue CSV, download a current Hemlane rent roll, fix property matches, or approve every rent-roll gap/stale export before monthly publish/email."
    if key.startswith("monthly_review.skipped_exclusion_count_mismatch"):
        return "Regenerate owner review, live update capture, and live financial capture so sold/delisted/closed skip counts match before owner email."
    if key.startswith("monthly_review.publish_exclusion_guard_failed"):
        return "Regenerate Lofty PM publish payloads; excluded sold/delisted/closed properties must have zero payload files and zero owner-email candidates."
    return "Review this blocker and rerun monthly readiness after fixing it."


def render_markdown(report: dict) -> str:
    actionable = report.get("actionable_summary") if isinstance(report.get("actionable_summary"), dict) else {}
    primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
    data_quality = report.get("data_quality_gate") if isinstance(report.get("data_quality_gate"), dict) else {}
    run_month = str(report.get("run_month") or "YYYY-MM")
    primary_class = str(primary.get("class") or primary.get("blocker") or "")
    if primary_class.startswith("monthly_comms.rent_roll_gap_review"):
        commands = report.get("commands") if isinstance(report.get("commands"), dict) else {}
        run_after_fix = str(commands.get("post_auth_resume") or "").strip()
        if not run_after_fix:
            run_after_fix = POST_AUTH_RESUME_COMMAND
    elif primary_class.startswith(("operational.daily_sync", "operational.daily_run", "operational.baselane_sync")):
        run_after_fix = "bash scripts/baselane_cron_run.sh && python3 scripts/baselane_eod_telegram_report.py --dry-run"
    elif primary_class.startswith("lofty_cdp_preflight") or primary_class.startswith("live_update_capture") or primary_class.startswith("live_financial_capture"):
        run_after_fix = SAFE_MONTHLY_CRON_DRY_RUN_COMMAND
    else:
        run_after_fix = "bash scripts/baselane_weekly_file_updates_cron.sh"
    lines = [
        "# Baselane Monthly Lofty Readiness",
        "",
        f"- Status: `{report['status']}`",
        f"- Owner email allowed: `{str(report['owner_email_allowed']).lower()}`",
        f"- Primary blocker: `{primary.get('blocker') or 'none'}`",
        f"- Next action: {primary.get('next_action') or 'No action required.'}",
        f"- Open: `{primary.get('artifact') or 'none'}`",
        f"- Hold: `{primary.get('hold') or 'none'}`",
        f"- Audit blockers: `{actionable.get('audit_blocker_count', report['blocker_count'])}`",
        f"- Source quality gate: `{data_quality.get('status') or 'unknown'}`"
        + (
            f" ({data_quality.get('source_fix_summary')})"
            if data_quality.get("source_fix_summary")
            else ""
        ),
        "",
        "## Action Required",
        "",
        f"- Work: `{primary.get('artifact') or 'none'}`",
        f"- Run after fix: `{run_after_fix}`",
        f"- Do not send: `{str(bool(actionable.get('downstream_live_and_email_held'))).lower()}`",
        "",
        "## Audit Summary",
        "",
        f"- Blocked properties: `{report['blocked_property_count']}`",
        f"- Blockers: `{report['blocker_count']}`",
        f"- Blocked property detail collapsed: `{str(bool(actionable.get('downstream_audit_collapsed'))).lower()}`",
        "",
        "## Counts",
    ]
    for key, value in report["counts"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Operational Gates"])
    for key, value in report.get("operational_gates", {}).items():
        status = value.get("effective_status") or value.get("status")
        detail = []
        for detail_key in (
            "ok",
            "issue_count",
            "recovered_by_canonical_daily_sync_report",
            "recovery_source",
            "weekly_duplicate_review_pending_count",
            "weekly_candidate_duplicate_pending_count",
            "audit_error_count",
            "conflict_count",
            "source_cash_balance_violation_count",
            "first_day_pm_fee_count",
            "quarantined_reporting_row_count",
            "remaining_reporting_first_day_pm_fee_count",
            "reporting_output_clean",
            "source_cleanup_action_count",
            "removed_local_accrual_count",
            "live_delete_candidate_count",
            "osc_payment_count",
            "duplicate_flag_count",
            "skip_review_candidate_count",
            "untagged_exception_row_count",
            "ecogl_source_fix_action_count",
            "ecogl_auto_safe_untagged_row_count",
            "missing_canonical_cf_count",
        ):
            if value.get(detail_key) is not None:
                detail.append(f"{detail_key}={value.get(detail_key)}")
        suffix = f" ({', '.join(detail)})" if detail else ""
        lines.append(f"- `{key}`: `{status}`{suffix}")
    monthly_comms = report.get("monthly_comms_gates") or {}
    if monthly_comms:
        lines.extend(["", "## Monthly Comms Gates"])
        lines.append(
            "- `rent_roll_gap_review`: "
            f"`{monthly_comms.get('rent_roll_gap_review_status')}` "
            f"(pending_gap_count={monthly_comms.get('rent_roll_pending_gap_count')}, "
            f"pending_stale_export_date_count={monthly_comms.get('rent_roll_pending_stale_export_date_count')})"
        )
        lines.append(f"- `rent_roll_gap_queue_csv`: `{monthly_comms.get('rent_roll_gap_queue_csv')}`")
        lines.append(f"- `rent_roll_gap_queue_digest`: `{monthly_comms.get('rent_roll_gap_queue_digest')}`")
        lines.append(f"- `rent_roll_gap_approval_template_coverage`: `{monthly_comms.get('rent_roll_gap_approval_template_coverage_status')}`")
        lines.append(f"- `rent_roll_gap_approval_template_digest`: `{monthly_comms.get('rent_roll_gap_approval_template_digest')}`")
        lines.append(
            "- `hemlane_cdp_preflight`: "
            f"`{monthly_comms.get('hemlane_cdp_preflight_status')}` "
            f"(login_tabs={monthly_comms.get('hemlane_login_tab_count')}, "
            f"rent_roll_tabs={monthly_comms.get('hemlane_rent_roll_tab_count')})"
        )
        lines.append(
            "- `lofty_cdp_preflight`: "
            f"`{monthly_comms.get('lofty_cdp_preflight_status')}` "
            f"(pm_tabs={monthly_comms.get('lofty_pm_tab_count')}, "
            f"login_tabs={monthly_comms.get('lofty_login_tab_count')}, "
            f"recovery={monthly_comms.get('lofty_login_recovery_performed')}, "
            f"opened_property_owners={monthly_comms.get('lofty_login_recovery_opened_property_owners')}, "
            f"attempts={monthly_comms.get('lofty_login_recovery_attempt_count')}, "
            f"tries={monthly_comms.get('lofty_login_recovery_try_count')})"
        )
    skip_policy = report.get("monthly_skip_policy") or {}
    if skip_policy:
        lines.extend(["", "## Monthly Skip Policy"])
        lines.append(
            "- `sold_delisted_closed_exclusions`: "
            f"`{skip_policy.get('skipped_exclusion_counts_match')}` "
            f"(owner={skip_policy.get('owner_review_gate_property_skipped_count')}, "
            f"external={skip_policy.get('owner_review_gate_property_external_excluded_count')}, "
            f"total={skip_policy.get('owner_review_gate_property_excluded_total_count')}, "
            f"updates={skip_policy.get('live_update_skipped_index_count')}, "
            f"financials={skip_policy.get('live_financial_skipped_index_count')})"
        )
    lines.extend(["", "## Blocked Properties"])
    if actionable.get("downstream_audit_collapsed"):
        lines.append(
            "- Per-property downstream details collapsed because the primary upstream blocker holds all live publish/email work."
        )
        lines.append(f"- Full audit property count: `{report['blocked_property_count']}`")
        lines.append("- Full per-property audit remains in the JSON report.")
    else:
        for entry in report["blocked_properties"]:
            lines.append(f"- {entry['property_name']}")
            seen = set()
            for blocker in entry["blockers"]:
                marker = (blocker["class"], blocker.get("path"))
                if marker in seen:
                    continue
                seen.add(marker)
                lines.append(f"  - `{blocker['class']}`: {blocker['next_action']}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(default_root()))
    parser.add_argument("--report", default="")
    parser.add_argument("--markdown", default="")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    report_path = Path(args.report) if args.report else root / "reports" / "baselane_financials_monthly_readiness.json"
    markdown_path = Path(args.markdown) if args.markdown else root / "reports" / "baselane_financials_monthly_readiness.md"
    report = build_report(root, report_path, markdown_path)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
