#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def default_root() -> Path:
    env_root = os.environ.get("WORKSPACE_ROOT")
    if env_root:
        return Path(env_root)
    cwd = Path.cwd()
    if (cwd / "reports").is_dir() and (cwd / "scripts").is_dir():
        return cwd
    return Path(__file__).absolute().parents[1]


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


ROOT = default_root()


def default_openclaw_root() -> Path:
    env_root = os.environ.get("OPENCLAW_ROOT")
    if env_root:
        candidate = Path(env_root)
        if (candidate / "workspace").resolve() == ROOT.resolve():
            return candidate
    return ROOT.parent


OPENCLAW_ROOT = default_openclaw_root()
REPORT_DIR = ROOT / "reports"
DEFAULT_REPORT_DIR = REPORT_DIR
ORIGINAL_REPORT_DIR = REPORT_DIR
OUT_REPORT = REPORT_DIR / "baselane_eod_telegram_report.json"
EOD_DRY_RUN_JSON_REPORT = REPORT_DIR / "baselane_eod_telegram_preview_report.json"
EOD_MESSAGE_PREVIEW_REPORT = REPORT_DIR / "baselane_daily_eod_telegram_preview.txt"
EOD_SEND_STATE_REPORT = REPORT_DIR / "baselane_eod_telegram_send_state.json"
EOD_SEND_PROOF_DIR = REPORT_DIR / "eod_telegram_send_proofs"
EOD_SEND_PROOF_INTEGRITY_CODES = {
    "eod_send_state_success_missing_message_digest",
    "eod_send_state_success_missing_source_report_generated_at",
}
LOFTY_CDP_ENSURE_REPORT = REPORT_DIR / "lofty_cdp_ensure_report.json"
LOFTY_CDP_PREFLIGHT_REPORT = REPORT_DIR / "lofty_cdp_preflight_report.json"
HEMLANE_CDP_PREFLIGHT_REPORT = REPORT_DIR / "hemlane_cdp_preflight_report.json"
DEFAULT_RENT_ROLL_DIR = "/mnt/c/Users/digit/Dropbox/Real Estate/Lofty PM/Rent Rolls"
LOFTY_PUBLIC_PATH_GUARD_REPORT = REPORT_DIR / "lofty_public_path_guard_report.json"
LOFTY_TENANT_LEDGER_GUARD_REPORT = REPORT_DIR / "lofty_tenant_ledger_folder_guard_report.json"
DISCORD_PUBLIC_FINANCIAL_SOURCE_GUARD_REPORT = REPORT_DIR / "discord_public_financial_source_guard_report.json"
BASELANE_SCHEDULER_AUDIT_REPORT = REPORT_DIR / "baselane_scheduler_audit_report.json"
BASELANE_GOAL_AUDIT_REPORT = REPORT_DIR / "baselane_financials_goal_audit.json"
BASELANE_WEEKLY_RECONCILE_REPORT = REPORT_DIR / "baselane_weekly_report_reconcile.json"
BASELANE_DAILY_SYNC_REPORT = REPORT_DIR / "baselane_daily_sync_report.json"
BASELANE_DAILY_DISK_SPACE_PREFLIGHT_REPORT = REPORT_DIR / "baselane_daily_disk_space_preflight_report.json"
BASELANE_DAILY_SOURCE_CASH_BALANCE_REPORT = REPORT_DIR / "baselane_daily_source_cash_balance_report.json"
BASELANE_CDP_AUTH_RECOVERY_REPORT = REPORT_DIR / "baselane_cdp_auth_recovery_report.json"
BASELANE_LOCAL_MODEL_PREFLIGHT_REPORT = REPORT_DIR / "baselane_local_model_preflight_report.json"
NATIVE_OWNER_EMAIL_OVERRIDE_ENV = "LOFTY_ALLOW_NATIVE_OWNER_EMAIL_FULL_FIELD_RISK"
POST_AUTH_RESUME_COMMAND = "bash scripts/baselane_financials_post_auth_resume.sh"


def comms_root() -> Path:
    env_comms = os.environ.get("COMMS_WORKSPACE")
    candidates = [
        Path(env_comms) if env_comms else None,
        ROOT.parent / "workspace-lofty-vp",
        OPENCLAW_ROOT / "workspace-lofty-vp",
        Path("/home/digit/.openclaw/workspace-lofty-vp"),
        ROOT.parent / "workspace-lofty-vp-comms",
        OPENCLAW_ROOT / "workspace-lofty-vp-comms",
        Path("/home/digit/.openclaw/workspace-lofty-vp-comms"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_dir():
            return candidate
    return ROOT.parent / "workspace-lofty-vp"


def comms_shell_prefix() -> str:
    return f"cd {shlex.quote(str(comms_root()))} &&"


def hemlane_preflight_needs_open_tab(preflight_report: dict) -> bool:
    return (
        preflight_report.get("status") == "review"
        and preflight_report.get("cdp_available") is True
        and compact_count(preflight_report.get("hemlane_tab_count")) == 0
        and compact_count(preflight_report.get("login_tab_count")) == 0
        and compact_count(preflight_report.get("rent_roll_tab_count")) == 0
    )


def hemlane_post_auth_resume_action(capture_next_action: object = None, preflight_report: dict | None = None) -> str:
    text = str(capture_next_action or "").strip()
    lower = text.lower()
    if hemlane_preflight_has_current_visible_login(preflight_report or {}):
        attempts = compact_count(
            (preflight_report or {}).get("login_recovery_try_count")
            or (preflight_report or {}).get("login_recovery_attempt_count")
        )
        suffix = f"; auto recovery tried {attempts}x" if attempts else "; auto recovery done"
        prefix = f"Finish Hemlane login/CAPTCHA{suffix}"
    elif hemlane_preflight_needs_open_tab(preflight_report or {}):
        prefix = "Open Hemlane rent-roll tab; solve CAPTCHA only if shown"
    elif any(marker in lower for marker in ("hard refresh", "hard-refresh", "close/open", "reopen", "reopened")):
        prefix = "Hard refresh/reopen Hemlane; solve reCAPTCHA only if still shown"
    elif "recaptcha" in lower:
        prefix = "Solve Hemlane reCAPTCHA / finish login in the visible tab"
    elif text:
        prefix = "Finish Hemlane auth in the visible tab"
    else:
        prefix = "Finish Hemlane auth if prompted"
    return (
        f"{prefix}; run `{POST_AUTH_RESUME_COMMAND}`. "
        "It refreshes Hemlane rent-roll evidence, monthly dry-run readiness, and EOD reporting while keeping owner email, "
        "Lofty PM publish, and guarded live writes disabled."
    )


def hemlane_preflight_prefers_login_refresh(preflight_report: dict) -> bool:
    if preflight_report.get("status") != "review":
        return False
    if preflight_report.get("cdp_available") is False:
        return False
    if compact_count(preflight_report.get("login_tab_count")) <= 0:
        return False
    if compact_count(preflight_report.get("logged_in_tab_count")) > 0:
        return False
    issue_text = " ".join(
        str(preflight_report.get(key) or "")
        for key in ("issue_summary", "next_action", "login_recovery_action")
    ).lower()
    return (
        preflight_report.get("login_recovery_opened_rent_roll") is True
        or preflight_report.get("login_recovery_performed") is True
        or "sign-in" in issue_text
        or "hard refresh" in issue_text
        or "close/open" in issue_text
    )


def hemlane_preflight_has_current_visible_login(preflight_report: dict) -> bool:
    return (
        hemlane_preflight_prefers_login_refresh(preflight_report)
        and compact_count(preflight_report.get("login_tab_count")) > 0
        and compact_count(preflight_report.get("logged_in_tab_count")) == 0
        and (
            preflight_report.get("login_recovery_opened_rent_roll") is True
            or preflight_report.get("login_recovery_performed") is True
        )
    )
BASELANE_SOURCE_FIX_ACTION_QUEUE_REPORT = REPORT_DIR / "baselane_ecogl_source_fix_action_queue.json"
BASELANE_SOURCE_FIX_APPLY_REPORT = REPORT_DIR / "baselane_ecogl_source_fix_apply.json"
BASELANE_NATIVE_SPLIT_PLAN_REPORT = REPORT_DIR / "baselane_native_split_plan.json"
BASELANE_NATIVE_SPLIT_APPLY_REPORT = REPORT_DIR / "baselane_native_split_apply_report.json"
BASELANE_NATIVE_SPLIT_PLAN_FALLBACK_REPORTS = (
    REPORT_DIR / "baselane_native_split_plan_window_reconciled.json",
    REPORT_DIR / "baselane_native_split_plan_window.json",
)
BASELANE_NATIVE_SPLIT_APPLY_FALLBACK_REPORTS = (
    REPORT_DIR / "baselane_native_split_apply_report_window_idempotent.json",
    REPORT_DIR / "baselane_native_split_apply_report_window_nopreflight.json",
    REPORT_DIR / "baselane_native_split_apply_report_window.json",
)
BASELANE_NO_MORTGAGE_FINANCIALS_GUARD_REPORT = REPORT_DIR / "baselane_no_mortgage_financials_cleanup_report.json"
BASELANE_LOFTY_TRANSFER_REQUIREMENTS_REPORT = REPORT_DIR / "baselane_lofty_transfer_requirements.json"
BASELANE_CF_BALANCE_SHEET_CASH_APPLY_REPORT = REPORT_DIR / "baselane_cf_balance_sheet_cash_apply_report.json"
BASELANE_REPORT_INTEGRITY_GUARD_REPORT = REPORT_DIR / "baselane_report_integrity_guard.json"
OWNER_EMAIL_SEND_GUARD_REPORT = REPORT_DIR / "baselane_monthly_owner_email_send_guard.json"
MONTHLY_READINESS_REPORT = REPORT_DIR / "baselane_financials_monthly_readiness.json"
MONTHLY_CLOSE_STATUS_REPORT = REPORT_DIR / "baselane_financials_monthly_close_status.json"
BASELANE_MONTHLY_RECOVERY_REPORT = REPORT_DIR / "baselane_financials_monthly_recovery_cron.json"
LOFTY_PM_PUBLISH_REPORT = REPORT_DIR / "baselane_financials_monthly_lofty_pm_publish.json"
MONTHLY_RUN_REPORT = REPORT_DIR / "baselane_financials_monthly_run_report.json"
MONTHLY_DISCORD_PROPERTY_UPDATE_SEND_REPORT = REPORT_DIR / "baselane_financials_monthly_discord_property_update_send.json"
MONTHLY_DISCORD_ALL_SEND_PLAN_VALIDATION_REPORT = REPORT_DIR / "baselane_financials_monthly_discord_all_send_plan_validation.json"
OWNER_EMAIL_PACKET_REPORT = REPORT_DIR / "baselane_monthly_owner_email_packet.json"
OWNER_EMAIL_PACKET_RECIPIENTS_CSV = REPORT_DIR / "lofty_owner_email_recipients.csv"
OWNER_EMAIL_PACKET_SENT_STATE = ROOT / "scripts" / ".baselane_financials_monthly_state" / "non_native_owner_email_sent_state.json"
OWNER_EMAIL_PACKET_PREVIEW_DIR = REPORT_DIR / "monthly_owner_email_packet"
LOFTY_PM_RUNTIME_MAP = REPORT_DIR / "baselane_financials_monthly_lofty_pm_runtime_map.json"
LOFTY_LIVE_UPDATE_CAPTURE_REPORT = REPORT_DIR / "baselane_financials_monthly_live_update_capture.json"
LOFTY_LISTING_CLEANUP_QUEUE_REPORT = REPORT_DIR / "lofty_listing_update_cleanup_queue.json"
LOFTY_LISTING_CLEANUP_DRY_RUN_VERIFY_REPORT = REPORT_DIR / "lofty_listing_cleanup_dry_run_verify.json"
LOFTY_EMPTY_UPDATES_BACKFILL_QUEUE_REPORT = REPORT_DIR / "lofty_empty_updates_backfill_queue.json"
LOFTY_FINANCIAL_PATCH_READINESS_REPORT = REPORT_DIR / "lofty_financial_patch_readiness.json"
LOFTY_UNREVIEWED_FINANCIAL_APPROVAL_QUARANTINE_REPORT = REPORT_DIR / "lofty_unreviewed_financial_approval_quarantine.json"
LOFTY_UNREVIEWED_FINANCIAL_APPROVAL_QUARANTINE_COMMANDS = (
    REPORT_DIR / "lofty_unreviewed_financial_approval_quarantine.requires-explicit-approval.sh"
)
LOFTY_REVIEW_CANDIDATE_PACKET_REPORT = REPORT_DIR / "baselane_financials_monthly_review_candidate_packet.json"
EXPECTED_LOCAL_MODEL = "ollama-cyber/qwen3.5:35b-a3b"
EXPECTED_LOCAL_PROVIDER = "ollama-cyber"
EXPECTED_LOCAL_MODEL_ID = "qwen3.5:35b-a3b"
EXPECTED_FINANCE_CONTRACT_RESPONSE = '{"category":"Rents","column_e_sum":177679.32,"ok":true}'
LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS = 30.0
DAILY_RUN_REPORT_MAX_AGE_HOURS = 30.0
EOD_REFRESH_FALLBACK_MAX_AGE_HOURS = 6.0
TELEGRAM_SAFE_MESSAGE_LIMIT = 3900
EOD_ACTIONABLE_MESSAGE_MAX_LINES = 7
EOD_ACTIONABLE_MESSAGE_MAX_CHARS = 400
EOD_ACTIONABLE_MESSAGE_HOLD_COMPACT_CHARS = 360
EOD_NOISE_MARKERS = ("ALSO:", "Citadel", "Marlowe", "/mnt/f", "parity=", "qwen=", "paths=", "goal=")
EOD_TELEGRAM_SEND_APPROVAL_ENV = "BASELANE_EOD_TELEGRAM_SEND_APPROVED"
EOD_TELEGRAM_SEND_DIGEST_ENV = "BASELANE_EOD_TELEGRAM_SEND_DIGEST"
MONTHLY_RUN_EOD_FIELDS = [
    "dry_run",
    "effective_status",
    "effective_failed_step",
    "require_lofty_guards",
    "require_guarded_monthly_apply",
    "owner_email_pending_count",
    "owner_email_send_requested",
    "owner_email_send_blocked_reason",
    "owner_email_publish_will_send_count",
    "owner_email_publish_send_evidence_count",
    "owner_email_publish_send_evidence_issue_count",
    "owner_email_publish_sent_or_would_send_count",
    "owner_email_publish_skipped_count",
    "owner_email_publish_send_lock_status",
    "owner_email_send_guard_status",
    "owner_email_send_guard_issue_count",
    "owner_email_send_guard_send_allowed",
    "owner_email_send_guard_safe_block",
    "owner_email_send_guard_no_spam_ok",
    "owner_email_send_guard_send_lock_file_unreadable",
    "owner_email_packet_status",
    "owner_email_packet_issue_count",
    "owner_email_packet_property_unavailable_count",
    "owner_email_packet_monthly_financial_summary_missing_property_count",
    "owner_email_packet_monthly_financial_summary_present_total_property_count",
    "owner_email_packet_property_unavailable_candidate_update_source_count",
    "owner_email_packet_property_unavailable_candidate_update_approval_target_count",
    "owner_email_packet_property_unavailable_candidate_financial_approval_target_count",
    "owner_email_packet_recipient_count",
    "owner_email_packet_packet_count",
    "owner_email_packet_full_history_leak_count",
    "owner_email_packet_body_guard_issue_count",
    "owner_email_packet_send_result_count",
    "owner_email_packet_send_failed_count",
    "owner_email_packet_safe_to_send_now",
    "owner_email_sent_state_month",
    "monthly_owner_review_gate_status",
    "monthly_owner_review_gate_blocker_count",
    "monthly_readiness_owner_email_allowed",
    "monthly_readiness_blocker_count",
]


def current_run_month() -> str:
    return os.environ.get("RUN_MONTH") or datetime.now(timezone.utc).strftime("%Y-%m")


def report_run_month(*reports: dict) -> str:
    env_month = str(os.environ.get("RUN_MONTH") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", env_month):
        return env_month
    for report in reports:
        raw = str((report or {}).get("run_month") or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}", raw):
            return raw
    return current_run_month()


def hemlane_capture_report_path(run_month: str) -> Path:
    return comms_root() / "updates" / f"{run_month}-hemlane-cdp-capture-report.json"


def hemlane_rent_roll_source_path(run_month: str) -> Path:
    return comms_root() / "updates" / f"{run_month}-rent-roll-source.json"


def infer_run_month_from_text(value: object) -> str:
    match = re.search(r"\b(20\d{2}-\d{2})\b", str(value or ""))
    return match.group(1) if match else ""


def current_report_path(path: Path) -> Path:
    if path.is_absolute() and path.parent not in {DEFAULT_REPORT_DIR, ORIGINAL_REPORT_DIR, REPORT_DIR}:
        return path
    return REPORT_DIR / path.name


def named_report_path(name: str) -> Path:
    return REPORT_DIR / name


def report_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def latest_existing_report_path(paths: list[Path] | tuple[Path, ...], default: Path) -> Path:
    candidates = [current_report_path(path) for path in paths]
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return current_report_path(default)
    return max(existing, key=report_mtime)


def latest_source_transaction_index_path() -> Path:
    timestamped = sorted(REPORT_DIR.glob("baselane_source_transaction_index.*.csv"), key=report_mtime)
    if timestamped:
        return timestamped[-1]
    return named_report_path("baselane_source_transaction_index.csv")


def latest_native_split_plan_report_path() -> Path:
    return latest_existing_report_path(
        (BASELANE_NATIVE_SPLIT_PLAN_REPORT, *BASELANE_NATIVE_SPLIT_PLAN_FALLBACK_REPORTS),
        BASELANE_NATIVE_SPLIT_PLAN_REPORT,
    )


def latest_native_split_apply_report_path() -> Path:
    return latest_existing_report_path(
        (BASELANE_NATIVE_SPLIT_APPLY_REPORT, *BASELANE_NATIVE_SPLIT_APPLY_FALLBACK_REPORTS),
        BASELANE_NATIVE_SPLIT_APPLY_REPORT,
    )


LOFTY_PM_PUBLISH_EOD_FIELDS = [
    "issue_count",
    "property_count",
    "publish_result_count",
    "publish_failed_count",
    "sent_state_write_status",
    "send_lock_status",
    "send_lock_file",
    "send_blocked_reason",
    "owner_email_will_send_count",
    "owner_email_send_evidence_count",
    "owner_email_send_evidence_issue_count",
    "owner_email_sent_or_would_send_count",
    "owner_email_skipped_count",
]
LOFTY_SAFE_APPROVAL_EOD_FIELDS = [
    "reason",
    "apply",
    "candidate_property_count",
    "update_candidate_count",
    "financial_candidate_count",
    "candidate_issue_count",
    "candidate_marker_count",
    "safety_scan_status",
    "safety_high_count",
    "safety_medium_count",
    "safety_missing_count",
    "approved_update_count",
    "approved_financial_count",
    "issue_count",
]
OPERATIONS_PACKET_EOD_FIELDS = [
    "action_item_count",
    "monthly_blocker_count",
    "pending_update_review_count",
    "pending_financial_review_count",
    "weekly_cf_conflict_count",
    "weekly_cf_untagged_required_count",
    "owner_email_allowed",
]
GOAL_AUDIT_EOD_FIELDS = [
    "achieved",
    "ok_count",
    "review_count",
    "requirement_count",
]


def read_json(path: Path) -> dict:
    if not path.exists():
        return {"status": "missing", "path": str(path)}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "unreadable", "path": str(path), "error": str(exc)}


def load_env_file(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_token_ref(value: object, env: dict) -> str:
    raw = str(value or "").strip()
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", raw)
    if match:
        return env.get(match.group(1), "")
    return raw


def default_telegram_allow_from() -> str:
    data = read_json(OPENCLAW_ROOT / "credentials" / "telegram-default-allowFrom.json")
    values = data.get("allowFrom") if isinstance(data, dict) else []
    if isinstance(values, list) and values:
        return str(values[0] or "").strip()
    return ""


def first_telegram_config_allow_from(telegram: dict) -> str:
    for key in ("allowFrom", "groupAllowFrom"):
        values = telegram.get(key)
        if isinstance(values, list) and values:
            return str(values[0] or "").strip()
    return ""


def telegram_config() -> tuple[str, str]:
    config_path = OPENCLAW_ROOT / "openclaw.json"
    config = read_json(config_path)
    telegram = ((config.get("channels") or {}).get("telegram") or {}) if isinstance(config, dict) else {}
    env = {**load_env_file(OPENCLAW_ROOT / ".env"), **os.environ}
    token = (
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or resolve_token_ref(telegram.get("botToken"), env)
        or env.get("TELEGRAM_BOT_TOKEN")
        or env.get("OPENCLAW_BOTTOKEN")
    )
    chat_id = (
        os.environ.get("BASELANE_EOD_TELEGRAM_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or env.get("BASELANE_EOD_TELEGRAM_CHAT_ID")
        or env.get("TELEGRAM_CHAT_ID")
        or first_telegram_config_allow_from(telegram)
        or default_telegram_allow_from()
    )
    return str(token or "").strip(), str(chat_id or "").strip()


def status_line(label: str, data: dict, extra: list[str] | None = None) -> str:
    status = data.get("status") or ("ok" if data.get("ok") is True else "review" if data.get("ok") is False else "unknown")
    fields = [f"{label}: {status}"]
    for key in extra or []:
        value = data.get(key)
        if value not in (None, "", []):
            fields.append(f"{key}={value}")
    return " | ".join(fields)


def compact_bool(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def compact_count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def compact_money_short(value: object) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return "$0"
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if amount >= 1_000_000:
        text = f"{amount / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{sign}${text}M"
    if amount >= 10_000:
        text = f"{amount / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{sign}${text}k"
    return f"{sign}${amount:,.0f}"


def compact_money_exact(value: object) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return str(value or "").strip()
    return f"${amount:,.0f}" if amount == round(amount) else f"${amount:,.2f}"


def monthly_live_accrual_hold_marker(close_status: dict) -> str:
    updates = close_status.get("monthly_accruals_live_plan_update_details")
    updates = updates if isinstance(updates, list) else []
    mismatches = close_status.get("monthly_accruals_amount_mismatch_details")
    mismatches = mismatches if isinstance(mismatches, list) else []
    if not updates and not mismatches:
        return ""
    update = updates[0] if updates and isinstance(updates[0], dict) else {}
    mismatch = mismatches[0] if mismatches and isinstance(mismatches[0], dict) else {}
    property_name = compact_property_label(update.get("property") or mismatch.get("property"), max_len=18)
    row_id = str(update.get("id") or update.get("transaction_id") or "").strip()
    current = compact_money_exact(mismatch.get("current_marker_amount") or mismatch.get("current_row_amount"))
    expected = compact_money_exact(mismatch.get("expected_amount") or update.get("absolute_amount"))
    if not property_name or not row_id or not current or not expected:
        return ""
    return f"{property_name} accrual id {row_id} {current}->{expected}"


def compact_monthly_step_label(step: object) -> str:
    text = str(step or "").strip()
    if not text:
        return ""
    replacements = {
        "baselane_monthly_finance_truth_refresh": "finance-truth",
        "baselane_monthly_statements_idempotent": "statements",
        "baselane_financials_monthly_cron": "monthly-cron",
    }
    if text in replacements:
        return replacements[text]
    text = re.sub(r"^(baselane_)?monthly_", "", text)
    text = re.sub(r"^(baselane_)?", "", text)
    text = text.replace("_refresh", "")
    text = text.replace("_idempotent", "")
    text = text.replace("_", "-")
    return text[:28]


def monthly_close_hold_marker(close_status: dict, monthly: dict | None = None) -> str:
    monthly = monthly or {}
    status = str(
        close_status.get("effective_status")
        or close_status.get("status")
        or monthly.get("effective_status")
        or monthly.get("status")
        or ""
    ).strip()
    failed_step = (
        close_status.get("effective_failed_step")
        or close_status.get("failed_step")
        or monthly.get("effective_failed_step")
        or monthly.get("failed_step")
    )
    gap_count = compact_count(
        first_present(
            close_status.get("monthly_completion_gap_count"),
            monthly.get("monthly_completion_gap_count"),
        )
    )
    command_count = compact_count(
        first_present(
            close_status.get("monthly_blocker_command_index_count"),
            len(monthly.get("monthly_blocker_command_index") or [])
            if isinstance(monthly.get("monthly_blocker_command_index"), list)
            else None,
        )
    )
    ready_manual_count = compact_count(
        first_present(
            close_status.get("monthly_blocker_ready_manual_count"),
            monthly.get("monthly_blocker_ready_manual_count"),
        )
    )
    safe_auto_count = compact_count(
        first_present(
            close_status.get("monthly_blocker_safe_auto_count"),
            monthly.get("monthly_blocker_safe_auto_count"),
        )
    )
    if status not in {"failed", "review", "blocked"} and not gap_count and not command_count:
        return ""
    parts = ["monthly"]
    if status in {"failed", "review", "blocked"}:
        parts.append(status)
    step_label = compact_monthly_step_label(failed_step)
    if step_label:
        parts.append(step_label)
    if gap_count:
        parts.append(f"gaps{gap_count}")
    if command_count:
        parts.append(f"cmds{command_count}")
    if ready_manual_count:
        parts.append(f"ready{ready_manual_count}")
    if safe_auto_count:
        parts.append(f"auto{safe_auto_count}")
    return " ".join(parts)


def monthly_recovery_eod_marker(report: dict) -> str:
    if not isinstance(report, dict) or report.get("status") in {"missing", "unreadable"}:
        return ""
    status = str(report.get("status") or "").strip()
    run_month = str(report.get("run_month") or "").strip()
    suffix = f" {run_month}" if re.fullmatch(r"\d{4}-\d{2}", run_month) else ""
    if status == "eligible_check_only" and report.get("eligible") is True:
        return f"monthly recovery armed{suffix}"
    if status in {"started", "running"}:
        return f"monthly recovery running{suffix}"
    if status in {"ok", "complete", "completed"}:
        return f"monthly recovery complete{suffix}"
    if status in {"failed", "review", "blocked"}:
        return f"monthly recovery {status}{suffix}"
    return ""


def compact_accrual_hold_marker(text: str) -> str:
    return re.sub(
        r"\b([A-Za-z0-9 .'-]{1,18}) accrual id (\d+) (\$[\d,]+(?:\.\d+)?)->(\$[\d,]+(?:\.\d+)?)",
        r"\1 accrual#\2 \3→\4",
        str(text or ""),
    )


def zero_or_empty(value: object) -> bool:
    return value in (None, "", 0, "0")


def goal_completion_marker(goal_report: dict) -> str:
    requirement_count = compact_count(goal_report.get("requirement_count"))
    ok_count = compact_count(goal_report.get("ok_count"))
    if not requirement_count:
        requirements = goal_report.get("requirements")
        if isinstance(requirements, list):
            requirement_count = len(requirements)
            ok_count = sum(1 for item in requirements if isinstance(item, dict) and item.get("status") == "ok")
    if not requirement_count:
        return "done=100%" if goal_report.get("achieved") is True or goal_report.get("status") == "ok" else ""
    ok_count = max(0, min(ok_count, requirement_count))
    percent = round((ok_count / requirement_count) * 100)
    return f"done={percent}%"


def goal_audit_summary(goal_report: dict) -> dict:
    actionable = goal_report.get("actionable_summary") if isinstance(goal_report.get("actionable_summary"), dict) else {}
    primary = goal_report.get("primary_blocker") if isinstance(goal_report.get("primary_blocker"), dict) else {}
    actionable_primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
    primary = primary or actionable_primary
    completion = first_present(goal_report.get("completion_percent"), actionable.get("completion_percent"))
    if completion in (None, ""):
        marker = goal_completion_marker(goal_report)
        if marker.startswith("done=") and marker.endswith("%"):
            completion = marker.removeprefix("done=").removesuffix("%")
    try:
        completion_percent = int(completion) if completion not in (None, "") else None
    except (TypeError, ValueError):
        completion_percent = None
    return {
        "status": goal_report.get("status") or None,
        "achieved": goal_report.get("achieved") is True,
        "completion_percent": completion_percent,
        "requirement_count": compact_count(first_present(goal_report.get("requirement_count"), actionable.get("requirement_count"))),
        "ok_requirement_count": compact_count(first_present(goal_report.get("ok_requirement_count"), goal_report.get("ok_count"), actionable.get("ok_requirement_count"))),
        "review_requirement_count": compact_count(first_present(goal_report.get("review_requirement_count"), actionable.get("review_requirement_count"))),
        "primary_blocker_id": primary.get("id") or primary.get("requirement") or None,
        "primary_blocker_title": primary.get("title") or None,
        "primary_blocker_summary": primary.get("summary") or primary.get("blocker") or None,
        "primary_blocker_hold": primary.get("hold") or None,
        "primary_blocker_artifact": primary.get("artifact") or None,
        "next_action": goal_report.get("next_action") or primary.get("next_action") or None,
    }


def first_present(*values: object) -> object:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def report_timestamp(report: dict) -> str:
    return str(report.get("generated_at") or report.get("checked_at") or "").strip()


def parsed_report_timestamp(report: dict) -> datetime:
    raw = report_timestamp(report)
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def report_has_timestamp(report: dict) -> bool:
    return bool(report_timestamp(report))


def hemlane_preflight_supersedes_capture_auth(preflight_report: dict, capture_report: dict) -> bool:
    return (
        hemlane_preflight_prefers_login_refresh(preflight_report)
        and report_has_timestamp(preflight_report)
        and parsed_report_timestamp(preflight_report) >= parsed_report_timestamp(capture_report)
    )


def hemlane_monthly_wait_auth_hint(capture_report: dict, preflight_report: dict) -> str:
    issue = str(capture_report.get("issue") or "").strip()
    status = str(capture_report.get("status") or "").strip()
    if hemlane_preflight_needs_open_tab(preflight_report):
        return "open Hemlane tab"
    if issue == "recaptcha_required":
        return "solve Hemlane CAPTCHA (BW ok)" if capture_report.get("bitwarden_login_submit_ok") is True else "solve Hemlane CAPTCHA"
    if issue == "login_required" or capture_report.get("manual_auth_required") is True:
        return "auth Hemlane"
    if status == "review" and str(capture_report.get("next_action") or "").strip():
        return "finish Hemlane auth"
    if hemlane_preflight_prefers_login_refresh(preflight_report):
        return "hard refresh Hemlane"
    if preflight_report.get("status") == "review" and preflight_report.get("cdp_available") is False:
        return "start Hemlane CDP"
    if preflight_report.get("status") == "review" and compact_count(preflight_report.get("login_tab_count")):
        return "auth Hemlane"
    return ""


def monthly_statements_waiting_for_posted_statements(
    monthly_statements_gate: dict,
    monthly_report: dict,
    monthly_statements_download: dict,
) -> bool:
    gate_status = str(
        monthly_statements_gate.get("status")
        or monthly_report.get("monthly_statements_gate_status")
        or ""
    ).strip()
    gate_action = str(
        monthly_statements_gate.get("action")
        or monthly_report.get("monthly_statements_gate_action")
        or ""
    ).strip()
    gate_reason = str(
        monthly_statements_gate.get("reason")
        or monthly_report.get("monthly_statements_gate_reason")
        or ""
    ).strip()
    download_error_class = str(
        monthly_statements_gate.get("download_error_class")
        or monthly_report.get("monthly_statements_download_error_class")
        or ""
    ).strip()
    download_error = str(monthly_statements_download.get("error") or "")
    no_statement_buttons = (
        gate_reason == "no-statement-buttons"
        or download_error_class == "no-statement-buttons"
        or "no statement download buttons discovered" in download_error
    )
    return gate_status == "review" and gate_action == "wait-for-statements" and no_statement_buttons


def compact_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def report_timestamp(report: dict) -> float | None:
    raw = str(report.get("generated_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def compact_property_label(value: object, *, max_len: int = 34) -> str:
    label = str(value or "").strip()
    if not label:
        return ""
    label = label.split(",", 1)[0].strip()
    if len(label) <= max_len:
        return label
    return label[: max_len - 1].rstrip() + "…"


def guild_test_hold_suffix(snapshot: object) -> str:
    if not isinstance(snapshot, dict):
        return "guild test first"
    selected = snapshot.get("selected") if isinstance(snapshot.get("selected"), dict) else {}
    label = compact_property_label(
        selected.get("route_property_name")
        or selected.get("property_name")
        or snapshot.get("property_name")
    )
    return f"post guild test: {label}" if label else "guild test first"


def native_owner_email_blocked(lofty_pm_publish: dict) -> bool:
    decision = lofty_pm_publish.get("owner_email_send_decision")
    if not isinstance(decision, dict):
        decision = {}
    reasons = [
        lofty_pm_publish.get("send_blocked_reason"),
        decision.get("blocked_reason"),
        lofty_pm_publish.get("native_owner_email_disabled_reason"),
    ]
    reason_text = " ".join(str(reason or "") for reason in reasons)
    return (
        "native Lofty owner email disabled" in reason_text
        or "send-property-updates emails the saved full updates field" in reason_text
    )


def add_native_email_hold_marker(hold: str, lofty_pm_publish: dict) -> str:
    native_email_disabled = (
        native_owner_email_blocked(lofty_pm_publish)
        or os.environ.get(NATIVE_OWNER_EMAIL_OVERRIDE_ENV) != "1"
    )
    if not native_email_disabled:
        return hold
    if hold == "none":
        return "native email off"
    replacements = {
        "email/Lofty PM publish": "native email off; Lofty PM publish",
        "email/Lofty PM": "native email off; Lofty PM",
        "investor email or Lofty PM publish": "native email off; Lofty PM publish",
        "owner email or Lofty PM publish": "native email off; Lofty PM publish",
        "Lofty PM publish or investor email": "native email off; Lofty PM publish",
        "monthly document publish or investor email": "monthly docs; native email off",
        "automated investor-facing updates": "native email off; automated updates held",
    }
    for source, target in replacements.items():
        if source in hold:
            return hold.replace(source, target)
    if "email" in hold:
        return f"{hold}; native email off"
    return hold


def lofty_guard_hold_marker(update_count: object, financial_count: object) -> str:
    update = compact_count(update_count)
    financial = compact_count(financial_count)
    if update <= 0 and financial <= 0:
        return ""
    return f"guards U{update}/F{financial}"


def live_capture_guard_problem_count(capture: dict) -> int:
    unverified_count = compact_count(capture.get("unverified_count"))
    mismatch_count = compact_count(capture.get("mismatch_count"))
    if str(capture.get("status") or "").strip() in {"failed", "review"}:
        return max(unverified_count, mismatch_count)
    return unverified_count


def add_lofty_guard_hold_marker(hold: str, marker: str) -> str:
    marker = str(marker or "").strip()
    if not marker:
        return hold
    if marker in hold:
        return hold
    if hold == "none":
        return marker
    replacements = {
        "native email off; Lofty PM publish": f"native email off; {marker}; Lofty PM",
        "native email off; Lofty PM": f"native email off; {marker}; Lofty PM",
        "email/Lofty PM publish": f"{marker}; email/Lofty PM publish",
        "email/Lofty PM": f"{marker}; email/Lofty PM",
        "investor email or Lofty PM publish": f"{marker}; investor email or Lofty PM publish",
        "owner email or Lofty PM publish": f"{marker}; owner email or Lofty PM publish",
        "Lofty PM publish or investor email": f"{marker}; Lofty PM publish or investor email",
    }
    for source, target in replacements.items():
        if source in hold:
            return hold.replace(source, target)
    return f"{hold}; {marker}"


def owner_email_packet_summary(packet_report: dict | None = None) -> dict:
    packet_report = packet_report if packet_report is not None else read_json(current_report_path(OWNER_EMAIL_PACKET_REPORT))
    if packet_report.get("status") in {"missing", "unreadable"}:
        return {
            "status": packet_report.get("status"),
            "path": packet_report.get("path") or str(current_report_path(OWNER_EMAIL_PACKET_REPORT)),
            "available": False,
        }
    return {
        "status": packet_report.get("status"),
        "available": True,
        "issue_count": compact_count(packet_report.get("issue_count")),
        "run_month": packet_report.get("run_month"),
        "property_count": compact_count(packet_report.get("property_count")),
        "available_property_count": compact_count(packet_report.get("available_property_count")),
        "property_unavailable_count": compact_count(packet_report.get("property_unavailable_count")),
        "property_unavailable_financial_summary_enriched_count": compact_count(
            packet_report.get("property_unavailable_financial_summary_enriched_count")
        ),
        "property_unavailable_monthly_financial_summary_present_count": compact_count(
            packet_report.get("property_unavailable_monthly_financial_summary_present_count")
        ),
        "monthly_financial_summary_present_property_count": compact_count(
            packet_report.get("monthly_financial_summary_present_property_count")
        ),
        "monthly_financial_summary_present_total_property_count": compact_count(
            packet_report.get("monthly_financial_summary_present_total_property_count")
        ),
        "monthly_financial_summary_missing_property_count": compact_count(
            packet_report.get("monthly_financial_summary_missing_property_count")
        ),
        "property_unavailable_candidate_update_source_count": compact_count(
            packet_report.get("property_unavailable_candidate_update_source_count")
        ),
        "property_unavailable_candidate_update_approval_target_count": compact_count(
            packet_report.get("property_unavailable_candidate_update_approval_target_count")
        ),
        "property_unavailable_candidate_financial_approval_target_count": compact_count(
            packet_report.get("property_unavailable_candidate_financial_approval_target_count")
        ),
        "property_unavailable_reason_counts": packet_report.get("property_unavailable_reason_counts") or {},
        "property_gap_csv": packet_report.get("property_gap_csv"),
        "recipient_count": compact_count(packet_report.get("recipient_count")),
        "packet_count": compact_count(packet_report.get("packet_count")),
        "full_history_leak_count": compact_count(packet_report.get("full_history_leak_count")),
        "body_guard_issue_count": compact_count(packet_report.get("body_guard_issue_count")),
        "send_result_count": compact_count(packet_report.get("send_result_count")),
        "send_failed_count": compact_count(packet_report.get("send_failed_count")),
        "already_sent_for_run_month": packet_report.get("already_sent_for_run_month") is True,
        "max_once_monthly_ok": packet_report.get("max_once_monthly_ok") is True,
        "send_requested": packet_report.get("send_requested") is True,
        "safe_to_send_now": packet_report.get("safe_to_send_now") is True,
        "sent_state_written": packet_report.get("sent_state_written") is True,
        "preview_dir": packet_report.get("preview_dir"),
        "preview_file_write_allowed": packet_report.get("preview_file_write_allowed") is True,
        "preview_write_blocked_reason": packet_report.get("preview_write_blocked_reason"),
        "unsafe_preview_packet_count": compact_count(packet_report.get("unsafe_preview_packet_count")),
        "stale_preview_file_removed_count": compact_count(packet_report.get("stale_preview_file_removed_count")),
        "stale_preview_cleanup_error_count": compact_count(packet_report.get("stale_preview_cleanup_error_count")),
        "packet_digest": packet_report.get("packet_digest"),
    }


def owner_email_gap_summary(
    packet_summary: dict | None = None,
    listing_cleanup_summary: dict | None = None,
) -> dict:
    packet_summary = packet_summary or owner_email_packet_summary()
    listing_cleanup_summary = listing_cleanup_summary or {}
    reason_counts = (
        packet_summary.get("property_unavailable_reason_counts")
        if isinstance(packet_summary.get("property_unavailable_reason_counts"), dict)
        else {}
    )
    listing_cleanup_count = compact_count(reason_counts.get("listing_history_cleanup_required"))
    live_guard_count = compact_count(reason_counts.get("live_update_guard_not_reconciled"))
    latest_copy_guard_count = compact_count(reason_counts.get("latest_update_body_guard"))
    missing_update_count = compact_count(reason_counts.get("updates_md_empty")) + compact_count(
        reason_counts.get("updates_md_missing")
    )
    listing_gap_count = listing_cleanup_count + live_guard_count
    compact_parts = []
    if listing_gap_count:
        compact_parts.append(f"L{listing_gap_count}")
    if latest_copy_guard_count:
        compact_parts.append(f"C{latest_copy_guard_count}")
    if missing_update_count:
        compact_parts.append(f"M{missing_update_count}")
    return {
        "legend": {
            "L": "listing history cleanup or live listing guard",
            "C": "latest update copy/body guard",
            "M": "missing or empty approved update",
        },
        "compact": "/".join(compact_parts),
        "listing_gap_count": listing_gap_count,
        "listing_history_cleanup_required_count": listing_cleanup_count,
        "live_update_guard_not_reconciled_count": live_guard_count,
        "latest_update_body_guard_count": latest_copy_guard_count,
        "missing_or_empty_update_count": missing_update_count,
        "ready_listing_cleanup_count": compact_count(listing_cleanup_summary.get("ready_listing_cleanup_count")),
        "listing_cleanup_dry_run_verify_ok": listing_cleanup_summary.get("dry_run_verify_ok") is True,
        "recipient_count": compact_count(packet_summary.get("recipient_count")),
        "packet_count": compact_count(packet_summary.get("packet_count")),
        "property_unavailable_count": compact_count(packet_summary.get("property_unavailable_count")),
        "property_unavailable_reason_counts": dict(reason_counts),
    }


def lofty_listing_cleanup_queue_summary(queue_report: dict | None = None, verify_report: dict | None = None) -> dict:
    queue_report = queue_report if queue_report is not None else read_json(current_report_path(LOFTY_LISTING_CLEANUP_QUEUE_REPORT))
    verify_report = verify_report if verify_report is not None else {}
    if queue_report.get("status") in {"missing", "unreadable"}:
        return {
            "status": queue_report.get("status"),
            "path": queue_report.get("path") or str(current_report_path(LOFTY_LISTING_CLEANUP_QUEUE_REPORT)),
            "available": False,
            "ready_listing_cleanup_count": 0,
            "issue_count": 0,
        }
    ready_count = compact_count(queue_report.get("ready_listing_cleanup_count"))
    verified_count = compact_count(verify_report.get("verified_record_count"))
    verify_issue_count = compact_count(verify_report.get("issue_count"))
    ready_cleanup_digest = str(queue_report.get("ready_cleanup_idempotency_digest") or "").strip()
    verify_ready_cleanup_digest = str(verify_report.get("ready_cleanup_idempotency_digest") or "").strip()
    dry_run_digest_ok = bool(ready_cleanup_digest) and verify_ready_cleanup_digest == ready_cleanup_digest
    dry_run_verify_ok = (
        verify_report.get("status") == "ok"
        and verify_issue_count == 0
        and verified_count == ready_count
        and dry_run_digest_ok
        and verify_report.get("mutates_lofty_listing") is False
        and verify_report.get("sends_owner_email") is False
        and verify_report.get("dry_run_only") is True
    )
    return {
        "status": queue_report.get("status"),
        "available": True,
        "issue_count": compact_count(queue_report.get("issue_count")),
        "property_count": compact_count(queue_report.get("property_count")),
        "ready_listing_cleanup_count": ready_count,
        "blocked_count": compact_count(queue_report.get("blocked_count")),
        "blocked_unsafe_latest_update_count": compact_count(queue_report.get("blocked_unsafe_latest_update_count")),
        "blocked_unsafe_update_candidate_source_count": compact_count(queue_report.get("blocked_unsafe_update_candidate_source_count")),
        "blocked_unsafe_update_candidate_quality_issue_count": compact_count(queue_report.get("blocked_unsafe_update_candidate_quality_issue_count")),
        "blocked_unsafe_update_candidate_financial_gate_issue_count": compact_count(
            queue_report.get("blocked_unsafe_update_candidate_financial_gate_issue_count")
        ),
        "blocked_unsafe_update_candidate_financial_gate_hold_count": compact_count(
            queue_report.get("blocked_unsafe_update_candidate_financial_gate_hold_count")
        ),
        "blocked_unsafe_update_approval_target_exists_count": compact_count(queue_report.get("blocked_unsafe_update_approval_target_exists_count")),
        "candidate_update_approval_copy_command_requires_current_rent_roll_count": compact_count(
            queue_report.get("candidate_update_approval_copy_command_requires_current_rent_roll_count")
        ),
        "candidate_update_approval_copy_requires_current_rent_roll": queue_report.get("candidate_update_approval_copy_requires_current_rent_roll") is True,
        "candidate_update_approval_csv": queue_report.get("candidate_update_approval_csv"),
        "candidate_update_approval_copy_commands_requires_current_rent_roll_and_explicit_approval_file": queue_report.get(
            "candidate_update_approval_copy_commands_requires_current_rent_roll_and_explicit_approval_file"
        ),
        "mutates_lofty_listing": queue_report.get("mutates_lofty_listing") is True,
        "sends_owner_email": queue_report.get("sends_owner_email") is True,
        "live_apply_requires_explicit_approval": queue_report.get("live_apply_requires_explicit_approval") is True,
        "live_snapshot_listing_issue_counts": queue_report.get("live_snapshot_listing_issue_counts") or {},
        "ready_cleanup_csv": queue_report.get("ready_cleanup_csv"),
        "dry_run_commands_file": queue_report.get("dry_run_commands_file"),
        "live_apply_commands_requires_explicit_approval_file": queue_report.get("live_apply_commands_requires_explicit_approval_file"),
        "dry_run_verify_status": verify_report.get("status"),
        "dry_run_verify_issue_count": verify_issue_count,
        "dry_run_verified_count": verified_count,
        "dry_run_verify_digest_ok": dry_run_digest_ok,
        "ready_cleanup_idempotency_digest": ready_cleanup_digest,
        "dry_run_verify_ready_cleanup_idempotency_digest": verify_ready_cleanup_digest,
        "dry_run_verify_ok": dry_run_verify_ok,
        "dry_run_verify_report": str(current_report_path(LOFTY_LISTING_CLEANUP_DRY_RUN_VERIFY_REPORT)),
    }


def lofty_empty_updates_backfill_queue_summary(queue_report: dict | None = None) -> dict:
    queue_report = queue_report if queue_report is not None else read_json(current_report_path(LOFTY_EMPTY_UPDATES_BACKFILL_QUEUE_REPORT))
    if queue_report.get("status") in {"missing", "unreadable"}:
        return {
            "status": queue_report.get("status"),
            "path": queue_report.get("path") or str(current_report_path(LOFTY_EMPTY_UPDATES_BACKFILL_QUEUE_REPORT)),
            "available": False,
            "property_count": 0,
            "needs_update_approval_target_count": 0,
            "ready_local_backfill_from_approved_count": 0,
            "blocked_count": 0,
            "issue_count": 0,
        }
    return {
        "status": queue_report.get("status"),
        "available": True,
        "issue_count": compact_count(queue_report.get("issue_count")),
        "property_count": compact_count(queue_report.get("property_count")),
        "ready_local_backfill_from_approved_count": compact_count(queue_report.get("ready_local_backfill_from_approved_count")),
        "needs_update_approval_target_count": compact_count(queue_report.get("needs_update_approval_target_count")),
        "blocked_count": compact_count(queue_report.get("blocked_count")),
        "mutates_dropbox_files": queue_report.get("mutates_dropbox_files") is True,
        "mutates_lofty_listing": queue_report.get("mutates_lofty_listing") is True,
        "sends_owner_email": queue_report.get("sends_owner_email") is True,
        "commands_require_explicit_approval": queue_report.get("commands_require_explicit_approval") is True,
        "approval_copy_requires_current_rent_roll": queue_report.get("approval_copy_requires_current_rent_roll") is True,
        "queue_csv": queue_report.get("queue_csv"),
        "queue_markdown": queue_report.get("queue_markdown"),
        "local_backfill_from_approved_commands_file": queue_report.get("local_backfill_from_approved_commands_file"),
        "approval_copy_commands_requires_current_rent_roll_and_explicit_approval_file": queue_report.get(
            "approval_copy_commands_requires_current_rent_roll_and_explicit_approval_file"
        ),
        "empty_updates_backfill_idempotency_digest": queue_report.get("empty_updates_backfill_idempotency_digest"),
    }


def listing_cleanup_hold_marker(queue_summary: dict) -> str:
    if queue_summary.get("available") is False:
        return ""
    issue_count = compact_count(queue_summary.get("issue_count"))
    if issue_count:
        return f"cleanup queue err {issue_count}"
    ready_count = compact_count(queue_summary.get("ready_listing_cleanup_count"))
    if ready_count:
        approval_ready_count = compact_count(queue_summary.get("candidate_update_approval_copy_command_requires_current_rent_roll_count"))
        financial_hold_count = compact_count(queue_summary.get("blocked_unsafe_update_candidate_financial_gate_hold_count"))
        details = []
        if approval_ready_count > 0:
            details.append(f"updates approve {approval_ready_count}")
        if financial_hold_count > 0:
            details.append(f"fin guard {financial_hold_count}")
        detail_suffix = f"; {'; '.join(details)}" if details else ""
        if queue_summary.get("dry_run_verify_ok") is True:
            return f"cleanup {ready_count} dry{detail_suffix}"
        return f"cleanup {ready_count} listings{detail_suffix}"
    return ""


def add_listing_cleanup_hold_marker(hold: str, queue_summary: dict) -> str:
    marker = listing_cleanup_hold_marker(queue_summary)
    if not marker:
        return hold
    if hold == "monthly docs/email/listings held until statements pass":
        return hold
    ready_count = compact_count(queue_summary.get("ready_listing_cleanup_count"))
    if queue_summary.get("dry_run_verify_ok") is True and ready_count:
        plain = f"{ready_count} cleanup"
        verified = f"{ready_count} cleanup dry-ok"
        verified_dry = f"{ready_count} cleanup dry"
        plain_alt = f"cleanup {ready_count}"
        verified_alt = f"cleanup {ready_count} dry-ok"
        verified_alt_dry = f"cleanup {ready_count} dry"
        if verified_dry in hold and marker not in hold:
            return hold.replace(verified_dry, marker)
        if verified_alt_dry in hold and marker not in hold:
            return hold.replace(verified_alt_dry, marker)
        if verified in hold and marker not in hold:
            return hold.replace(verified, marker)
        if verified_alt in hold and marker not in hold:
            return hold.replace(verified_alt, marker)
        if plain in hold and verified not in hold and verified_dry not in hold:
            return hold.replace(plain, verified)
        if plain_alt in hold and verified_alt not in hold and verified_alt_dry not in hold:
            return hold.replace(plain_alt, verified_alt)
        compact_plain = f"{ready_count}cleanup"
        compact_verified = f"{ready_count}cleanup-dry-ok"
        compact_marker = marker.replace(f"cleanup {ready_count} dry-ok", f"{ready_count}cleanup-dry-ok")
        if compact_verified in hold and compact_marker not in hold:
            return hold.replace(compact_verified, compact_marker)
        if compact_plain in hold and compact_verified not in hold:
            return hold.replace(compact_plain, compact_verified)
    if "cleanup" in hold:
        return hold
    if hold == "none":
        return marker
    if marker in hold:
        return hold
    replacements = {
        "email/listing until rent roll current": f"email/listing until rent roll current; {marker}",
        "native email off; Lofty PM publish": f"native email off; {marker}; Lofty PM",
        "native email off; Lofty PM": f"native email off; {marker}; Lofty PM",
        "email/Lofty PM publish": f"{marker}; email/Lofty PM publish",
        "email/Lofty PM": f"{marker}; email/Lofty PM",
        "Lofty PM publish or investor email": f"{marker}; Lofty PM publish or investor email",
    }
    for source, target in replacements.items():
        if source in hold:
            return hold.replace(source, target)
    return f"{hold}; {marker}"


def financial_patch_hold_marker(readiness_report: dict) -> str:
    if str(readiness_report.get("status") or "").strip().lower() in {"missing", "unreadable", "ok"}:
        return ""
    blocked_empty_count = compact_count(readiness_report.get("blocked_empty_patch_count"))
    if blocked_empty_count:
        return f"fin snap {blocked_empty_count} review"
    blocked_count = compact_count(readiness_report.get("blocked_count"))
    if blocked_count:
        return f"fin snap {blocked_count} blocked"
    return ""


def add_financial_patch_hold_marker(hold: str, readiness_report: dict) -> str:
    marker = financial_patch_hold_marker(readiness_report)
    if not marker:
        return hold
    if hold == "none":
        return marker
    if marker in hold:
        return hold
    return f"{hold}; {marker}"


def owner_email_packet_hold_marker(packet_summary: dict) -> str:
    status = str(packet_summary.get("status") or "").strip().lower()
    if status in {"missing", "unreadable"}:
        return "packet missing"
    if compact_count(packet_summary.get("full_history_leak_count")) > 0:
        return "packet leak"
    body_guard_issue_count = compact_count(packet_summary.get("body_guard_issue_count"))
    if body_guard_issue_count > 0:
        return f"packet bodyguard {body_guard_issue_count}"
    send_failed_count = compact_count(packet_summary.get("send_failed_count"))
    if send_failed_count > 0:
        send_result_count = compact_count(packet_summary.get("send_result_count"))
        return f"packet sendfail {send_failed_count}/{send_result_count}"
    packet_count = compact_count(packet_summary.get("packet_count"))
    unavailable_count = compact_count(packet_summary.get("property_unavailable_count"))
    recipient_count = compact_count(packet_summary.get("recipient_count"))
    preview_block = str(packet_summary.get("preview_write_blocked_reason") or "").strip().lower()
    if recipient_count == 0 and packet_count == 0 and unavailable_count == 0 and "no recipient" in preview_block:
        return ""
    unavailable_label = f"{unavailable_count}u"
    marker_prefix = "packet"
    candidate_ready_count = 0
    if unavailable_count > 0:
        candidate_ready_count = min(
            unavailable_count,
            compact_count(packet_summary.get("property_unavailable_candidate_update_source_count")),
            compact_count(packet_summary.get("property_unavailable_candidate_update_approval_target_count")),
            compact_count(packet_summary.get("property_unavailable_candidate_financial_approval_target_count")),
        )
    reason_counts = packet_summary.get("property_unavailable_reason_counts")
    if (
        isinstance(reason_counts, dict)
        and unavailable_count > 0
        and compact_count(reason_counts.get("updates_md_empty")) == unavailable_count
    ):
        unavailable_label = f"{unavailable_count}empty"
    elif isinstance(reason_counts, dict) and unavailable_count > 0:
        compact_reason_labels = {
            "listing_history_cleanup_required": "cleanup",
            "live_update_guard_not_reconciled": "guard",
            "updates_md_empty": "empty",
            "updates_md_missing": "missing",
            "latest_update_body_guard": "body",
        }
        parts = []
        for reason, label in compact_reason_labels.items():
            count = compact_count(reason_counts.get(reason))
            if count:
                parts.append(f"{count}{label}")
        if parts and sum(compact_count(reason_counts.get(reason)) for reason in compact_reason_labels) == unavailable_count:
            unavailable_label = "+".join(parts)
            marker_prefix = "pkt"
    if candidate_ready_count > 0:
        unavailable_label = f"{unavailable_label}+{candidate_ready_count}cand"
        marker_prefix = "pkt"
    missing_summary_count = compact_count(packet_summary.get("monthly_financial_summary_missing_property_count"))
    if missing_summary_count > 0:
        unavailable_label = f"{unavailable_label}+{missing_summary_count}sum"
        marker_prefix = "pkt"
    if status and status not in {"ok", "not_started"}:
        if unavailable_count == 0 and recipient_count == 0:
            return ""
        if unavailable_count or recipient_count == 0:
            return f"{marker_prefix} {unavailable_label}/{recipient_count}r"
        return "packet review"
    if compact_count(packet_summary.get("issue_count")) > 0:
        if unavailable_count == 0 and recipient_count == 0:
            return ""
        if unavailable_count or recipient_count == 0:
            return f"{marker_prefix} {unavailable_label}/{recipient_count}r"
        return "packet review"
    return ""


def add_owner_email_packet_hold_marker(hold: str, packet_summary: dict) -> str:
    marker = owner_email_packet_hold_marker(packet_summary)
    if not marker:
        return hold
    if hold == "none":
        return marker
    if marker in hold:
        return hold
    replacements = {
        "EOD proof; native email off; Lofty PM; post guild test:": f"EOD proof; native email off; {marker}; guild test:",
        "native email off; Lofty PM; post guild test:": f"native email off; {marker}; guild test:",
        "EOD proof; native email off; Lofty PM publish": f"EOD proof; native email off; {marker}; Lofty PM publish",
        "native email off; Lofty PM publish": f"native email off; {marker}; Lofty PM publish",
        "native email off; Lofty PM": f"native email off; {marker}; Lofty PM",
        "monthly docs; native email off": f"monthly docs; native email off; {marker}",
        "native email off": f"native email off; {marker}",
    }
    for source, target in replacements.items():
        if source in hold:
            return hold.replace(source, target)
    return f"{hold}; {marker}"


def actionable_hold_summary(hold: str, blocker: str) -> str:
    if blocker == "monthly Baselane bank statements" or blocker.startswith("Baselane monthly statements not posted"):
        return "monthly docs/email/listings held until statements pass"
    if blocker.startswith(("rent-roll", "stale rent roll")):
        if "email recipients missing" in hold:
            return "email/listing until rent roll current; email recipients missing"
        packet_match = re.search(r"(?:^|;\s*)((?:pkt|packet)\s+[^;]+)", hold)
        if packet_match:
            return f"email/listing until rent roll current; {compact_packet_hold_suffix(packet_match.group(1))}"
        return "email/listing until rent roll current"
    return readable_hold_summary(hold)


def readable_hold_summary(hold: str) -> str:
    text = str(hold or "").strip()
    if not text:
        return text
    text = re.sub(
        r"(?:^|;\s*)((?:pkt|packet)\s+[^;]+)",
        lambda match: ("; " if match.group(0).startswith(";") else "") + readable_packet_hold_suffix(match.group(1)),
        text,
    )
    text = re.sub(
        r"guards U(\d+)/F(\d+)",
        r"live guards updates=\1 financials=\2",
        text,
    )
    text = re.sub(r"\s*;\s*", "; ", text).strip("; ")
    return text


def budget_hold_summary(hold: str) -> str:
    text = str(hold or "").strip()
    if not text or text == "none":
        return text or "none"
    if text in {
        "weekly/monthly document updates",
        "monthly document publish",
        "monthly document publish or investor email",
    }:
        return text
    if "statements pass" in text:
        return "monthly docs/email/listings held until statements pass"
    if "rent roll current" in text and "email recipients missing" in text:
        return "email/listing until rent roll current"
    parts: list[str] = []
    has_statement_wait = "statement wait" in text or "stmt wait" in text
    if "EOD proof" in text:
        parts.append("EOD proof")
    accrual_match = re.search(r"\b[^;]*\baccrual id \d+ \$[\d,]+(?:\.\d+)?->\$[\d,]+(?:\.\d+)?", text)
    if accrual_match:
        parts.append(compact_accrual_hold_marker(accrual_match.group(0).strip()))
    if "rent roll current" in text:
        parts.append("email/listing until rent roll current")
    if has_statement_wait:
        parts.append("stmt wait")
    if "native email off" in text:
        parts.append("native email off")
    if "weekly/monthly document updates" in text:
        parts.append("monthly docs")
    monthly_close_match = re.search(r"\bmonthly\s+(failed|review|blocked)\s+[^;]+", text)
    if monthly_close_match:
        parts.append(monthly_close_match.group(0).strip())
    if "email recipients missing" in text:
        parts.append("email gaps" if "live guards" in text else "email recipients missing")
    else:
        compact_gap = ""
        gap_match = re.search(r"email gaps\s+listing(\d+)(?:/copy(\d+))?(?:/missing(\d+))?", text)
        if gap_match:
            labels = [f"L{gap_match.group(1)}"]
            if gap_match.group(2):
                labels.append(f"C{gap_match.group(2)}")
            if gap_match.group(3):
                labels.append(f"M{gap_match.group(3)}")
            compact_gap = ("gaps " if has_statement_wait else "email gaps ") + "/".join(labels)
        ready_match = re.search(r"email gaps\s+(\d+)\s+ready/(\d+)\s+blocked", text)
        if ready_match and not compact_gap:
            compact_gap = f"{'gaps' if has_statement_wait else 'email gaps'} {ready_match.group(1)}R/{ready_match.group(2)}B"
        if compact_gap and ("rent roll current" not in text or has_statement_wait):
            parts.append(compact_gap)
        elif ("rent roll current" not in text or has_statement_wait) and (
            "email gaps" in text or "packet" in text or "recipients" in text
        ):
            parts.append("gaps" if has_statement_wait else "email gaps")
    live_guard_match = re.search(r"live guards updates=(\d+) financials=(\d+)", text)
    if live_guard_match:
        if "email gaps" in text:
            parts.append(f"live guards U{live_guard_match.group(1)}/F{live_guard_match.group(2)} review")
        else:
            parts.append(f"guards U{live_guard_match.group(1)}/F{live_guard_match.group(2)}")
    elif "live guards" in text:
        parts.append("live guards")
    cleanup_match = (
        re.search(r"\bcleanup\s+(\d+)(?:\s+(dry-ok|dry|listings?))?(?P<tail>(?:\+upd\d+\s+(?:approve(?:/\d+\s+fin)?|fin))*)", text)
        or re.search(r"\b(\d+)\s+cleanup(?:\s+(dry-ok|dry|listings?))?(?P<tail>(?:\+upd\d+\s+(?:approve(?:/\d+\s+fin)?|fin))*)", text)
    )
    if cleanup_match and not any("cleanup" in part for part in parts):
        count = cleanup_match.group(1)
        status = cleanup_match.group(2) or ""
        suffix = "" if ("email gaps" in text and "live guards" in text) else (" dry" if status in {"dry-ok", "dry"} else "")
        parts.append(f"cleanup {count}{suffix}{cleanup_match.group('tail') or ''}")
    elif "cleanup" in text and not any("cleanup" in part for part in parts):
        parts.append("listing cleanup")
    update_approval_match = re.search(r"\bupdates?\s+approve\s+(\d+)", text)
    if update_approval_match and not any("updates approve" in part for part in parts):
        parts.append(f"upd{update_approval_match.group(1)} app")
    fin_guard_match = re.search(r"\bfin(?:ancial)?\s+guards?\s+(\d+)", text)
    if fin_guard_match and not any("fin guard" in part for part in parts):
        parts.append(f"fin guard{fin_guard_match.group(1)}")
    raw_mortgage_match = re.search(r"\bECO\s+raw-mtg(\d+)", text)
    if raw_mortgage_match and not any("raw-mtg" in part for part in parts):
        parts.append(f"ECO raw-mtg{raw_mortgage_match.group(1)}")
    financial_patch_match = re.search(r"\b(?:financial patches?|fin snapshots?|fin snaps?)\s+(\d+)\s+(review|blocked)", text)
    if financial_patch_match:
        parts.append(f"snap{financial_patch_match.group(1)}")
    if "Lofty PM" in text and not any("Lofty PM" in part for part in parts):
        parts.append("Lofty PM")
    if "monthly" in text and not any("monthly" in part for part in parts):
        parts.append("monthly docs")
    deduped = list(dict.fromkeys(part for part in parts if part))
    if deduped:
        return "; ".join(deduped)
    return text[:120].rstrip()


def compact_packet_hold_suffix(marker: str) -> str:
    text = str(marker or "").strip()
    match = re.fullmatch(r"(?:pkt|packet)\s+(.+?)/\d+r", text)
    if not match:
        return text
    labels = {
        "cleanup": "cleanup",
        "empty": "empty",
        "guard": "guard",
        "body": "body",
        "cand": "ready",
        "missing": "missing",
        "u": "missing",
    }
    parts = []
    counts_by_label: dict[str, int] = {}
    for raw_part in match.group(1).split("+"):
        part_match = re.fullmatch(r"(\d+)([A-Za-z-]+)", raw_part.strip())
        if not part_match:
            parts.append(raw_part.strip())
            continue
        count_value, label = part_match.groups()
        counts_by_label[label] = int(count_value)
        parts.append(f"{count_value} {labels.get(label, label)}")
    if any(label in counts_by_label for label in ("cleanup", "guard", "body", "empty", "missing")):
        listing_count = counts_by_label.get("cleanup", 0) + counts_by_label.get("guard", 0)
        copy_count = counts_by_label.get("body", 0)
        missing_count = counts_by_label.get("empty", 0) + counts_by_label.get("missing", 0)
        compact_parts = []
        if listing_count:
            compact_parts.append(f"listing{listing_count}")
        if copy_count:
            compact_parts.append(f"copy{copy_count}")
        if missing_count:
            compact_parts.append(f"missing{missing_count}")
        if compact_parts:
            return "email gaps " + "/".join(compact_parts)
    if "u" in counts_by_label and "cand" in counts_by_label:
        ready_count = counts_by_label["cand"]
        blocked_count = max(counts_by_label["u"] - ready_count, 0)
        return f"email gaps {ready_count} ready/{blocked_count} blocked"
    return "email gaps " + "/".join(part for part in parts if part)


def readable_packet_hold_suffix(marker: str) -> str:
    text = str(marker or "").strip()
    match = re.fullmatch(r"(?:pkt|packet)\s+(.+?)/\d+r", text)
    if not match:
        return text
    parts = []
    for raw_part in match.group(1).split("+"):
        part_match = re.fullmatch(r"(\d+)([A-Za-z-]+)", raw_part.strip())
        if not part_match:
            parts.append(raw_part.strip())
            continue
        count_value, label = part_match.groups()
        if label == "empty":
            label = "empty updates"
        elif label == "cleanup":
            label = "cleanup"
        elif label == "cleanup-dry-ok":
            label = "cleanup dry-ok"
        elif label == "guard":
            label = "live guards"
        elif label == "body":
            label = "body guards"
        elif label == "cand":
            label = "ready candidates"
        elif label == "u":
            label = "unavailable"
        parts.append(f"{count_value} {label}")
    readable = ", ".join(parts)
    recipient_match = re.search(r"/(\d+)r$", text)
    if recipient_match:
        readable = f"{readable}, {recipient_match.group(1)} recipients"
    return f"email gaps: {readable}" if readable else text


def monthly_owner_exclusion_summary(
    owner_review_gate: dict | None = None,
    lofty_pm_publish: dict | None = None,
    owner_email_send_guard: dict | None = None,
    live_capture: dict | None = None,
    live_financial_capture: dict | None = None,
    guard_audit: dict | None = None,
) -> dict:
    owner_review_gate = owner_review_gate if owner_review_gate is not None else read_json(named_report_path("baselane_monthly_owner_review_gate.json"))
    lofty_pm_publish = lofty_pm_publish if lofty_pm_publish is not None else read_json(named_report_path("baselane_financials_monthly_lofty_pm_publish.json"))
    owner_email_send_guard = owner_email_send_guard if owner_email_send_guard is not None else read_json(current_report_path(OWNER_EMAIL_SEND_GUARD_REPORT))
    live_capture = live_capture if live_capture is not None else read_json(named_report_path("baselane_financials_monthly_live_update_capture.json"))
    live_financial_capture = live_financial_capture if live_financial_capture is not None else read_json(named_report_path("baselane_financials_monthly_live_financial_capture.json"))
    guard_audit = guard_audit if guard_audit is not None else read_json(named_report_path("baselane_financials_monthly_guard_audit.json"))

    owner_gate_summary = owner_review_gate.get("summary") or {}
    skipped_index_count = compact_count(owner_review_gate.get("property_skipped_count") or owner_gate_summary.get("property_skipped_count"))
    external_excluded_count = compact_count(
        owner_review_gate.get("property_external_excluded_count")
        or owner_gate_summary.get("property_external_excluded_count")
    )
    owner_gate_total = compact_count(
        owner_review_gate.get("property_excluded_total_count")
        or owner_gate_summary.get("property_excluded_total_count")
    )
    if not owner_gate_total and (skipped_index_count or external_excluded_count):
        owner_gate_total = skipped_index_count + external_excluded_count

    component_counts = {
        "owner_review_gate_property_skipped_count": skipped_index_count,
        "owner_review_gate_property_external_excluded_count": external_excluded_count,
        "guard_audit_externally_excluded_count": compact_count(guard_audit.get("externally_excluded_count")),
    }
    full_scope_total_counts = {
        "live_update_excluded_property_count": compact_count(live_capture.get("excluded_property_count")),
        "live_financial_excluded_property_count": compact_count(live_financial_capture.get("excluded_property_count")),
    }
    live_capture_timestamps = [
        timestamp
        for timestamp in (report_timestamp(live_capture), report_timestamp(live_financial_capture))
        if timestamp is not None
    ]
    capture_reference_timestamp = max(live_capture_timestamps) if live_capture_timestamps else None
    stale_source_reports: list[dict[str, object]] = []

    def source_count(label: str, report: dict) -> int:
        timestamp = report_timestamp(report)
        explicitly_stale = label == "owner_email_send_guard" and report.get("publish_fresh") is False
        stale = explicitly_stale or (
            capture_reference_timestamp is not None
            and timestamp is not None
            and timestamp < capture_reference_timestamp
        )
        if stale:
            stale_source_reports.append(
                {
                    "source": label,
                    "generated_at": report.get("generated_at"),
                    "excluded_property_count": compact_count(report.get("excluded_property_count")),
                    "replacement_source": "live_update_and_live_financial_capture",
                    "replacement_count": max(
                        full_scope_total_counts["live_update_excluded_property_count"],
                        full_scope_total_counts["live_financial_excluded_property_count"],
                    ),
                }
            )
            return max(
                full_scope_total_counts["live_update_excluded_property_count"],
                full_scope_total_counts["live_financial_excluded_property_count"],
            )
        return compact_count(report.get("excluded_property_count"))

    full_scope_total_counts = {
        "lofty_pm_publish_excluded_property_count": source_count("lofty_pm_publish", lofty_pm_publish),
        "owner_email_send_guard_excluded_property_count": source_count("owner_email_send_guard", owner_email_send_guard),
        **full_scope_total_counts,
    }
    full_scope_max = max(full_scope_total_counts.values() or [0])
    active_total_source_counts = dict(full_scope_total_counts)
    if owner_gate_total >= full_scope_max:
        active_total_source_counts["owner_review_gate_property_excluded_total_count"] = owner_gate_total
    active_excluded_count = max(active_total_source_counts.values() or [0])
    yhome_guard = (
        lofty_pm_publish.get("yhome_transition_guard")
        or live_capture.get("yhome_transition_guard")
        or live_financial_capture.get("yhome_transition_guard")
        or owner_email_send_guard.get("yhome_transition_guard")
        or {}
    )
    manual_excluded_names = sorted(
        {
            *compact_string_list(lofty_pm_publish.get("manual_excluded_property_names")),
            *compact_string_list(live_capture.get("manual_excluded_property_names")),
            *compact_string_list(live_financial_capture.get("manual_excluded_property_names")),
            *compact_string_list(owner_email_send_guard.get("manual_excluded_property_names")),
        }
    )
    nonzero_active_total_source_counts = {key: value for key, value in active_total_source_counts.items() if value}
    return {
        "active_excluded_count": active_excluded_count,
        "message_skip_count": active_excluded_count,
        "active_total_source_counts": active_total_source_counts,
        "nonzero_active_total_source_counts": nonzero_active_total_source_counts,
        "active_total_source_counts_match": len(set(nonzero_active_total_source_counts.values())) <= 1,
        "component_counts": component_counts,
        "owner_review_gate_property_skipped_count": skipped_index_count,
        "owner_review_gate_property_external_excluded_count": external_excluded_count,
        "owner_review_gate_property_excluded_total_count": owner_gate_total,
        "yhome_transition_guard_status": yhome_guard.get("status"),
        "yhome_column_b_marker_count": compact_count(yhome_guard.get("column_b_marker_count")),
        "yhome_excluded_count": compact_count(yhome_guard.get("excluded_count")),
        "stale_source_reports": stale_source_reports,
        "manual_excluded_count": len(manual_excluded_names),
        "manual_excluded_property_names": manual_excluded_names,
        "policy": "EOD message reports active excluded PM/email targets; raw Yhome column-B markers may include rows outside the active monthly publish set.",
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


def fresh_generated_at(report: dict, max_age_hours: float = LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS) -> bool:
    if not str(report.get("generated_at") or "").strip():
        return False
    age_hours = iso_age_hours(report.get("generated_at"))
    return age_hours is not None and -1 <= age_hours <= max_age_hours


def local_model_preflight_ok(report: dict) -> bool:
    direct_smoke = report.get("direct_smoke") if isinstance(report.get("direct_smoke"), dict) else {}
    finance_smoke = report.get("finance_contract_smoke") if isinstance(report.get("finance_contract_smoke"), dict) else {}
    contract = report.get("validation_contract") if isinstance(report.get("validation_contract"), dict) else {}
    scope = report.get("model_execution_scope") if isinstance(report.get("model_execution_scope"), dict) else {}
    contract_scope = contract.get("model_execution_scope") if isinstance(contract.get("model_execution_scope"), dict) else {}
    digest = str(report.get("validation_digest") or "")
    strict_ok = (
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
        and direct_smoke.get("attempted") is True
        and direct_smoke.get("ok") is True
        and direct_smoke.get("response") == "BASELANE_MODEL_OK"
        and finance_smoke.get("attempted") is True
        and finance_smoke.get("ok") is True
        and finance_smoke.get("response") == EXPECTED_FINANCE_CONTRACT_RESPONSE
        and report.get("finance_contract_expected_response") == EXPECTED_FINANCE_CONTRACT_RESPONSE
        and scope.get("deterministic_only") is True
        and scope.get("pipeline_execution_allowed") is False
        and contract.get("selected_endpoint_from_config") is True
        and contract.get("direct_smoke_ok") is True
        and contract.get("direct_smoke_response") == "BASELANE_MODEL_OK"
        and contract.get("finance_contract_smoke_ok") is True
        and contract.get("finance_contract_response") == EXPECTED_FINANCE_CONTRACT_RESPONSE
        and contract.get("model_scope_deterministic") is True
        and contract.get("model_pipeline_execution_denied") is True
        and contract_scope.get("deterministic_only") is True
        and contract_scope.get("pipeline_execution_allowed") is False
        and bool(re.fullmatch(r"[0-9a-f]{64}", digest))
        and fresh_generated_at(report)
    )
    return strict_ok


def local_model_preflight_blocker_action(report: dict) -> str:
    blocker = report.get("blocker") if isinstance(report.get("blocker"), dict) else {}
    blocker_code = str(blocker.get("code") or "").strip()
    if blocker_code == "ollama_model_disabled":
        model_id = str(blocker.get("model_id") or report.get("model_id") or EXPECTED_LOCAL_MODEL_ID).strip()
        return f"enable {model_id} in Ollama dashboard; rerun preflight"
    if blocker_code == "smoke_timeout":
        if report.get("fallback_smoke_ok") is True:
            return "primary 35B cold-load timed out; fallback local qwen passed; keep 35B warm or shorten primary model"
        return f"warm {EXPECTED_LOCAL_MODEL_ID} or switch cron to faster local endpoint; rerun preflight"
    blocker_action = str(blocker.get("action") or "").strip()
    if blocker_action:
        return blocker_action.replace("scripts/baselane_local_model_preflight.py", "preflight")
    direct_smoke = report.get("direct_smoke") if isinstance(report.get("direct_smoke"), dict) else {}
    error_code = str(direct_smoke.get("error_code") or "").strip()
    if error_code == "model_disabled":
        model_id = str(report.get("model_id") or EXPECTED_LOCAL_MODEL_ID).strip()
        base_url = str(report.get("base_url") or "").strip()
        endpoint_suffix = f" for {base_url}" if base_url else ""
        return f"enable {model_id} in Ollama dashboard{endpoint_suffix}; rerun preflight"
    if error_code == "smoke_timeout":
        if report.get("fallback_smoke_ok") is True:
            return "primary 35B cold-load timed out; fallback local qwen passed; keep 35B warm or shorten primary model"
        return f"warm {EXPECTED_LOCAL_MODEL_ID} or switch cron to faster local endpoint; rerun preflight"
    if error_code:
        return f"fix {EXPECTED_LOCAL_MODEL_ID} preflight ({error_code}); rerun daily report"
    issues = [str(issue) for issue in (report.get("issues") or []) if issue]
    model_issue = next((issue for issue in issues if "direct local-model smoke failed:" in issue), "")
    if model_issue:
        return f"fix {EXPECTED_LOCAL_MODEL_ID} preflight ({model_issue.rsplit(':', 1)[-1].strip()}); rerun daily report"
    return f"refresh {EXPECTED_LOCAL_MODEL_ID} preflight; rerun daily report"


def monthly_readiness_blocked_reason(readiness: dict) -> str:
    actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
    primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
    primary_text = str(primary.get("blocker") or primary.get("class") or "").strip()
    actionable_count = int(actionable.get("actionable_blocker_count") or 0)
    if primary_text:
        return f"monthly readiness owner_email_allowed=false; primary={primary_text}; actionable={actionable_count}"
    return f"monthly readiness owner_email_allowed=false; actionable={actionable_count}"


def plural_label(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or f"{singular}s")


def rent_roll_source_blocker_label(evidence: dict) -> str:
    source = evidence.get("rent_roll_source") if isinstance(evidence.get("rent_roll_source"), dict) else {}
    freshness = str(evidence.get("rent_roll_freshness_status") or source.get("freshness_status") or "").strip()
    stale_dates = evidence.get("rent_roll_stale_export_dates") or source.get("stale_export_dates") or evidence.get("rent_roll_export_dates") or source.get("export_dates") or []
    if not isinstance(stale_dates, list):
        stale_dates = [stale_dates]
    stale_dates = [str(date).strip() for date in stale_dates if str(date or "").strip()]
    latest_export = str(evidence.get("rent_roll_latest_exported_on") or source.get("latest_exported_on") or "").strip()
    current_month_count = compact_count(source.get("current_month_export_count"))
    if freshness and freshness != "current":
        suffix = latest_export if latest_export else freshness
        if stale_dates:
            suffix = stale_dates[-1]
        if current_month_count == 0:
            return f"stale rent roll ({suffix}; no current export)"
        return f"stale rent roll ({suffix})"
    deferred = compact_count(evidence.get("gap_review_deferred_gap_count") or source.get("deferred_gap_count"))
    if deferred:
        return f"rent-roll source ({deferred} deferred {plural_label(deferred, 'gap')})"
    gaps = compact_count(evidence.get("rent_roll_gap_count") or source.get("gap_count"))
    if gaps:
        return f"rent-roll source ({gaps} {plural_label(gaps, 'gap')})"
    return "rent-roll source"


def compact_path(value: object, *, json_to_md: bool = True) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    if json_to_md and path.suffix == ".json":
        path = path.with_suffix(".md")
    try:
        if path.is_absolute():
            return compact_display_path(path.relative_to(ROOT))
    except ValueError:
        pass
    try:
        if path.is_absolute():
            return compact_display_path(path.relative_to(ROOT.parent))
    except ValueError:
        pass
    return compact_display_path(path)


def compact_display_path(path: object) -> str:
    display = str(path)
    replacements = {
        "../workspace-lofty-vp/": "lofty-vp/",
        "workspace-lofty-vp/": "lofty-vp/",
        "../workspace-lofty-vp-comms/": "lofty-comms/",
        "workspace-lofty-vp-comms/": "lofty-comms/",
    }
    for source, target in replacements.items():
        if display.startswith(source):
            return target + display[len(source) :]
    return display


def compact_owner_primary_action(primary: dict) -> str:
    blocker = str(primary.get("blocker") or primary.get("class") or "").lower()
    action = re.sub(r"\s+", " ", str(primary.get("action") or "").strip())
    if "verified ready for cleaned-history repair" in action.lower() or "copied-history listing fields" in action.lower():
        cleanup_match = re.search(r"(?:cleanup\s+|^)(\d+)\s+(?:copied-history\s+)?listing", action, re.IGNORECASE)
        if not cleanup_match:
            cleanup_match = re.search(r"(\d+)\s+live Lofty listing update fields", action, re.IGNORECASE)
        snapshot_match = re.search(r"replace\s+(\d+)\s+generated-ledger FINANCIALS", action, re.IGNORECASE)
        parts = []
        if cleanup_match:
            parts.append(f"cleanup {cleanup_match.group(1)} listings")
        if snapshot_match:
            parts.append(f"replace {snapshot_match.group(1)} FINANCIALS snapshot")
        suffix = "; " + "; ".join(parts) if parts else ""
        return f"review cleanup CSV + FIN patch{suffix}; no email/live apply"
    if "rent-roll queue" in action.lower() or "rent_roll_active_target_gap_count" in str(primary.get("summary") or "").lower():
        return "work rent-roll gap queue; fix/approve target gaps"
    if "live_update_capture" in blocker or "live update capture" in blocker:
        targets_match = re.search(r"Targets:\s*(.+?)(?:\.\s|$)", action)
        if targets_match:
            targets = targets_match.group(1).strip()
            targets = re.sub(r"\s*\([A-Z0-9]{12,}\)", "", targets)
            targets = targets.rstrip(".")
            targets = "; ".join(
                re.sub(r"\s+", " ", target.split(",", 1)[0]).strip()
                .replace(" Street", " St")
                .replace(" Avenue", " Ave")
                .replace(" Road", " Rd")
                .replace(" Ave Albany", " Ave")
                for target in targets.split(";")
                if target.strip()
            )
            if len(targets) > 90:
                targets = targets[:87].rstrip() + "..."
            return f"reconcile UPDATES: {targets}; rerun dry-run"
        reconcile_match = re.search(r"(\d+)\s+need reconcile", action, re.IGNORECASE)
        count = reconcile_match.group(1) if reconcile_match else ""
        if count:
            return f"reconcile {count} live/local UPDATES diffs; rerun dry-run"
        return "reconcile live/local UPDATES diffs; rerun dry-run"
    if "lofty_pm_publish" in blocker:
        if "incomplete_apply" in blocker:
            return "run guarded live Lofty PM apply for active targets"
        return "fix Lofty PM publish path; rerun active targets"
    if "rent_roll" in blocker or "rent-roll" in blocker or "hemlane" in action.lower():
        return "finish Hemlane login; capture rent roll; rerun"
    if "live_guard" in blocker or "live guard" in action.lower():
        return "capture/register live PM guards"
    if "lofty_pm_cdp" in blocker:
        return "auth Lofty CDP; capture live PM guards"
    if "approvals_pending" in blocker:
        return "approve update/financial candidates"
    if "owner_email" in blocker:
        return "keep owner email disabled until readiness is clean"
    if action:
        return action.split(". ", 1)[0].rstrip(".")
    return "resolve primary monthly owner blocker"


def compact_duration(value: object) -> str:
    seconds = compact_count(value)
    if seconds <= 0:
        return "unknown"
    if seconds < 90:
        return f"{seconds}s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes}m"
    return f"{round(minutes / 60, 1)}h"


def compact_age_hours(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return str(value)
    if hours < 0:
        return str(value)
    if hours < 10:
        return f"{hours:.2f}".rstrip("0").rstrip(".")
    if hours < 100:
        return f"{hours:.1f}".rstrip("0").rstrip(".")
    return str(round(hours))


def compact_mib_amount(value: object) -> str:
    try:
        mib = float(value)
    except (TypeError, ValueError):
        return str(value)
    if mib >= 1024:
        gib = mib / 1024
        return f"{gib:.1f}".rstrip("0").rstrip(".") + "GiB"
    return f"{mib:.0f}MiB"


def disk_preflight_shortfall(daily: dict) -> str:
    issue_sources = []
    issue_sources.extend(daily.get("disk_space_preflight_issues") or [])
    issue_sources.extend(daily.get("issues") or [])
    for issue in issue_sources:
        match = re.search(
            r"low_free_space:[^:]+:(?P<free>[0-9.]+)MiB<(?P<minimum>[0-9.]+)MiB",
            str(issue),
        )
        if match:
            return f"{compact_mib_amount(match.group('free'))}<{compact_mib_amount(match.group('minimum'))}"
    return ""


def disk_preflight_action(disk_report: dict | None = None) -> str:
    disk_report = disk_report or {}
    required_free_mib = disk_report.get("required_free_mib")
    hint = (disk_report.get("disk_pressure_hints") or {}).get("top_known_large_consumer")
    hint_suffix = ""
    if isinstance(hint, dict) and hint.get("path"):
        hint_name = Path(str(hint.get("path"))).name
        hint_size = hint.get("size_gib")
        hint_suffix = f"; check {hint_name} {hint_size}GiB" if hint_size not in {None, ""} else f"; check {hint_name}"
    if required_free_mib not in {None, ""}:
        return f"free {compact_mib_amount(required_free_mib)} C:/Dropbox{hint_suffix}; rerun daily sync"
    return f"free C:/Dropbox disk space{hint_suffix}; rerun daily sync"


def daily_sync_status_line(
    daily: dict,
    daily_job: dict,
    daily_run: dict | None = None,
    refresh_health: dict | None = None,
) -> str:
    daily_run = daily_run or {}
    status = daily.get("effective_status") or daily.get("status") or "unknown"
    recovery_status = str(daily.get("deterministic_sync_recovery_status") or "").strip()
    recovery_repeat_count = compact_count(daily.get("daily_recovered_sync_repeat_count"))
    display_status = (
        f"recoveredx{recovery_repeat_count}" if recovery_repeat_count > 1 else "recovered"
        if status == "ok" and recovery_status == "recovered_by_newer_successful_sync"
        else str(status)
    )
    parts = [display_status]
    sync_status = daily.get("sync_report_status")
    if sync_status and str(sync_status) != str(display_status):
        parts.append(f"sync={sync_status}")
    canonical_daily_clean = (
        daily.get("status") == "ok"
        and daily.get("issue_count") is not None
        and compact_count(daily.get("issue_count")) == 0
        and not (daily.get("issues") or [])
    )
    step_aliases = {
        "baselane_sync_cdp_deterministic": "det_sync",
        "baselane_sync_cdp_human_paced_fallback": "human_sync",
        "baselane_local_model_preflight": "model",
        "baselane_disk_space_preflight": "disk",
        "assetrail_git_push": "assetrail",
        "alawa_loandepot_cleanup": "alawa",
    }
    failed_step = daily.get("effective_failed_step") or daily.get("failed_step")
    recovered_failed_step = None
    if status == "ok" and recovery_status.startswith("recovered_by_"):
        recovered_failed_step = "det_sync"
    elif (
        status == "ok"
        and daily.get("failed_step")
        and not daily.get("effective_failed_step")
        and str(daily.get("return_code") or "0") not in {"0", ""}
        and daily.get("effective_return_code") in {0, "0"}
    ):
        recovered_failed_step = step_aliases.get(str(daily.get("failed_step") or ""), daily.get("failed_step"))
    if recovered_failed_step and not canonical_daily_clean:
        parts.append(f"recovered={recovered_failed_step or 'unknown'}")
    wrapper_parts = []
    daily_run_status = str(daily_run.get("status") or "").strip()
    daily_run_failed_step = daily_run.get("failed_step")
    original_wrapper_failed = (
        daily_run_status in {"failed", "review"}
        and bool(daily_run_failed_step)
    ) or (
        status == "ok"
        and daily.get("failed_step")
        and not daily.get("effective_failed_step")
        and str(daily.get("return_code") or "0") not in {"0", ""}
        and daily.get("effective_return_code") in {0, "0"}
    )
    if status == "ok" and canonical_daily_clean and original_wrapper_failed:
        wrapper_status = "recovered" if recovery_status.startswith("recovered_by_") else (daily_run_status or "failed")
        wrapper_parts.append(f"cron={wrapper_status}")
        wrapper_failed_step = daily_run_failed_step or daily.get("failed_step")
        wrapper_failed_step = step_aliases.get(str(wrapper_failed_step or ""), wrapper_failed_step)
        if wrapper_failed_step:
            wrapper_parts.append(f"cron_step={wrapper_failed_step}")
        assetrail_status = str(daily.get("assetrail_push_status") or "").strip()
        if assetrail_status in {"verified_current_clean", "committed_and_pushed", "pushed_no_ledger_changes"}:
            wrapper_parts.append("backup=assetrail")
    sync_reason = str(daily.get("sync_report_reason") or "").strip()
    sync_failure_class = str(daily.get("sync_report_failure_class") or "").strip()
    sync_reason_aliases = {
        "interrupted_by_operator": "interrupt",
        "baselane_sync_interrupted_by_operator": "interrupt",
        "export_guard_review": "export_guard",
        "baselane_export_guard_review": "export_guard",
        "baselane_login_auth_401": "auth401",
        "cdp_login_failed": "login",
        "interrupted_by_signal_15": "sig15",
    }
    disk_preflight_status = str(daily.get("disk_space_preflight_status") or "").strip()
    disk_preflight_blocked = disk_preflight_status and disk_preflight_status not in {"ok", "missing"}
    reason_display = "disk" if disk_preflight_blocked else sync_reason_aliases.get(sync_reason) or sync_reason_aliases.get(sync_failure_class)
    if not reason_display and sync_reason:
        reason_display = re.sub(r"[^a-zA-Z0-9_:-]+", "-", sync_reason).strip("-")[:24]
    if reason_display and sync_status not in {None, "", "ok"}:
        parts.append(f"reason={reason_display}")
    if disk_preflight_blocked:
        disk_shortfall = disk_preflight_shortfall(daily)
        parts.append(f"disk={disk_shortfall}" if disk_shortfall else "disk=low")
    duration = compact_duration(daily.get("duration_seconds"))
    # Keep the duration token stable even when a preflight stops the job before
    # the child process records a runtime.
    parts.append(f"dur={duration}")
    age = daily.get("daily_health_age_hours")
    if age in (None, ""):
        age = daily.get("daily_run_age_hours")
    if age in (None, ""):
        age = iso_age_hours(daily.get("ended_at"))
    if age in (None, ""):
        age = daily_job.get("report_age_hours")
    if age not in (None, ""):
        parts.append(f"age={compact_age_hours(age)}h")
    recent_failure_count = compact_count(daily.get("daily_wrapper_failure_distinct_run_count"))
    recent_failure_window = daily.get("daily_wrapper_failure_window_hours")
    if status == "ok" and recent_failure_count:
        window = compact_age_hours(recent_failure_window) if recent_failure_window not in (None, "") else "?"
        parts.append(f"prior={recent_failure_count}/{window}h")
    refresh_timeout_count = compact_count((refresh_health or {}).get("timeout_count"))
    if refresh_timeout_count:
        parts.append(f"refresh={refresh_timeout_count}t")
    parts.extend(wrapper_parts)
    failed_step_display = step_aliases.get(str(failed_step or ""), failed_step)
    if (status != "ok" or failed_step) and not recovered_failed_step:
        parts.append(f"step={failed_step_display or 'none'}")
    split_mismatches = compact_count(daily.get("split_output_mismatch_count"))
    unresolved = compact_count(daily.get("split_unresolved_property_count"))
    if split_mismatches:
        missing = compact_count(daily.get("split_output_missing_count"))
        stale = compact_count(daily.get("split_output_stale_count"))
        parts.append(f"split=not_current({split_mismatches};m={missing};s={stale})")
    elif daily.get("split_write_attempted") is True and unresolved == 0:
        parts.append("split=current")
    elif daily.get("split_write_attempted") is False and daily.get("sync_report_status") == "ok":
        parts.append("split=unconfirmed")
    if unresolved:
        parts.append(f"unresolved={unresolved}")
    hemlane_live_status = str(daily.get("hemlane_live_transaction_status") or "").strip()
    hemlane_live_required = daily.get("hemlane_live_transaction_required") is True
    if hemlane_live_required and hemlane_live_status and hemlane_live_status != "ok":
        hemlane_aliases = {
            "auth_unavailable": "auth",
            "query_failed": "query",
            "missing": "missing",
            "unreadable": "unreadable",
        }
        parts.append(f"hemlane={hemlane_aliases.get(hemlane_live_status, hemlane_live_status[:16])}")
    elif hemlane_live_required and hemlane_live_status == "ok":
        hemlane_count = compact_count(daily.get("hemlane_live_transaction_count"))
        parts.append(f"hemlane=ok/{hemlane_count}tx" if hemlane_count else "hemlane=ok")
    source_cash_status = str(daily.get("source_cash_balance_status") or "").strip()
    source_cash_violations = compact_count(daily.get("source_cash_balance_violation_count"))
    source_cash_updates = compact_count(daily.get("source_cash_balance_update_count"))
    source_cash_fresh = daily.get("source_cash_balance_report_fresh")
    if source_cash_violations:
        parts.append(f"cash=violations({source_cash_violations})")
    elif source_cash_fresh is False:
        parts.append("cash=stale")
    elif source_cash_status == "ok":
        cash_part = "cash=ok"
        if source_cash_updates:
            cash_part += f"/{source_cash_updates}upd"
        parts.append(cash_part)
    elif source_cash_status and source_cash_status not in {"missing", "unreadable"}:
        parts.append(f"cash={re.sub(r'[^a-zA-Z0-9_:-]+', '-', source_cash_status).strip('-')[:24]}")
    assetrail_live_status = str(daily.get("assetrail_live_status") or "").strip()
    assetrail_temp_count = compact_count(daily.get("assetrail_live_temp_ledger_status_count"))
    assetrail_status = str(daily.get("assetrail_push_status") or "").strip()
    assetrail_live_clean = assetrail_live_status == "ok" and not assetrail_temp_count
    if assetrail_temp_count:
        parts.append(f"assetrail=temp({assetrail_temp_count})")
    elif assetrail_live_clean:
        parts.append("assetrail=clean")
    elif assetrail_live_status and assetrail_live_status not in {"missing", "unreadable"}:
        parts.append(f"assetrail={re.sub(r'[^a-zA-Z0-9_:-]+', '-', assetrail_live_status).strip('-')[:24]}")
    elif assetrail_status in {"verified_current_clean", "committed_and_pushed", "pushed_no_ledger_changes"}:
        parts.append("assetrail=clean")
    elif assetrail_status and assetrail_status not in {"missing", "unreadable"}:
        parts.append(f"assetrail={re.sub(r'[^a-zA-Z0-9_:-]+', '-', assetrail_status).strip('-')[:24]}")
    steps = daily.get("steps") if isinstance(daily.get("steps"), dict) else {}
    step_warnings = []
    if not canonical_daily_clean:
        for name, value in sorted(steps.items()):
            step_status = str(value or "").strip()
            if not step_status:
                continue
            if step_status in {"ok", "skipped", "skipped_by_env", "not_requested"}:
                continue
            if step_status.startswith(("already_", "committed_")):
                continue
            if name == "assetrail_git_push" and assetrail_live_clean:
                continue
            if sync_reason == "cdp_login_failed" and name != "deterministic_sync":
                continue
            name_aliases = {
                "deterministic_sync": "det_sync",
                "disk_space_preflight": "disk",
                "assetrail_git_push": "assetrail",
                "alawa_loandepot_cleanup": "alawa",
            }
            status_aliases = {"review_nonfatal": "review", "not_started": "ns", "failed_nonfatal_rc_1": "fail_rc1"}
            step_warnings.append(f"{name_aliases.get(name, name)}:{status_aliases.get(step_status, step_status)}")
    if step_warnings and not disk_preflight_blocked:
        parts.append(f"warn={','.join(step_warnings[:2])}")
    timing_issue_count = compact_count(daily.get("sync_report_timing_issue_count"))
    if timing_issue_count and not disk_preflight_blocked:
        timing_label = "mismatch"
        if compact_count(daily.get("sync_report_finished_after_daily_end_seconds")) > 0:
            timing_label = "child_after_end"
        elif compact_count(daily.get("sync_report_started_after_daily_end_seconds")) > 0:
            timing_label = "child_start_after_end"
        elif compact_count(daily.get("sync_report_started_before_daily_start_seconds")) > 0:
            timing_label = "child_before_start"
        parts.append(f"time={timing_label}")
    issue_count = compact_count(daily.get("issue_count"))
    if not issue_count:
        issue_count = len([str(issue) for issue in (daily_job.get("issues") or []) if issue])
    if issue_count and not disk_preflight_blocked:
        parts.append(f"issues={issue_count}")
    return f"Sync: {' '.join(parts)}"


def compact_daily_sync_status_line(line: str) -> str:
    if not line.startswith("Sync: "):
        return line
    tokens = line.split()
    if len(tokens) <= 2:
        return line
    kept = tokens[:2]
    important_prefixes = (
        "sync=",
        "reason=",
        "disk=",
        "dur=",
        "age=",
        "step=",
        "split=",
        "unresolved=",
        "cash=",
        "assetrail=",
        "time=",
        "hemlane=",
        "issues=",
    )
    for token in tokens[2:]:
        if token.startswith(important_prefixes):
            if token == "reason=interrupted_by_signal_15":
                token = "reason=sig15"
            kept.append(token)
    compacted = " ".join(kept)
    return compacted if len(compacted) < len(line) else line


def tight_compact_daily_sync_status_line(line: str) -> str:
    if not line.startswith("Sync: "):
        return line
    tokens = line.split()
    if len(tokens) <= 2:
        return line
    keep_prefixes = ("sync=", "reason=", "dur=", "age=", "step=", "unresolved=", "hemlane=", "cash=", "issues=")
    compacted_tokens = tokens[:2] + [token for token in tokens[2:] if token.startswith(keep_prefixes)]
    compacted = " ".join(compacted_tokens)
    return compacted if len(compacted) < len(line) else line


def compact_eod_action_line(line: str) -> str:
    if line.startswith(("DO: ", "NEXT: ")) and any(
        marker in line.lower() for marker in ("ollama", "qwen", "local model")
    ):
        label = line.split(":", 1)[0]
        return f"{label}: enable {EXPECTED_LOCAL_MODEL_ID} in Ollama dashboard; rerun preflight"
    match = re.fullmatch(
        r"(DO|NEXT): map/approve unresolved GL properties=(\d+) rows=(\d+) amount=(-?\d+(?:\.\d+)?)",
        line,
    )
    if match:
        label, properties, rows, amount = match.groups()
        return f"{label}: map/approve split gaps props={properties} rows={rows} amt={amount}"
    if line.startswith(("DO: ", "NEXT: ")) and "monthly finance-truth" in line.lower():
        label = line.split(":", 1)[0]
        return f"{label}: hard-refresh/reopen Baselane; rerun monthly finance-truth"
    return line


def monthly_finance_truth_eod_action(monthly: dict, readiness: dict, readiness_primary: dict) -> str:
    text = str(
        monthly.get("monthly_finance_truth_refresh_next_action")
        or monthly.get("next_action")
        or readiness.get("next_action")
        or readiness_primary.get("next_action")
        or ""
    ).strip()
    lower = text.lower()
    if any(marker in lower for marker in ("blank", "hard-refresh", "hard refresh", "close/open", "reopen", "reopened")):
        if "post_auth_resume" in lower or "post-auth resume" in lower or "baselane_financials_post_auth_resume.sh" in lower:
            return "hard-refresh/reopen Baselane; run post-auth resume"
        return "hard-refresh/reopen Baselane; rerun monthly finance-truth"
    if any(marker in lower for marker in ("recaptcha", "captcha", "appcheck", "login")):
        if "post_auth_resume" in lower or "post-auth resume" in lower or "baselane_financials_post_auth_resume.sh" in lower:
            return "solve Baselane reCAPTCHA/appcheck; run post-auth resume"
        return "solve Baselane reCAPTCHA/appcheck; rerun monthly finance-truth"
    if "post_auth_resume" in lower or "post-auth resume" in lower or "baselane_financials_post_auth_resume.sh" in lower:
        return "run post-auth resume"
    if text:
        return text
    return "rerun monthly finance-truth refresh before downstream publish"


def clamp_eod_message(message: str, max_chars: int = EOD_ACTIONABLE_MESSAGE_MAX_CHARS) -> str:
    if len(message) <= max_chars:
        return message
    lines = message.splitlines()
    for prefix in ("Sync: ", "HOLD: ", "DO: ", "NEXT: "):
        for index, line in enumerate(lines):
            if not line.startswith(prefix):
                continue
            overage = len("\n".join(lines)) - max_chars
            if overage <= 0:
                return "\n".join(lines)
            min_len = len(prefix) + 8
            target_len = max(min_len, len(line) - overage - 3)
            if target_len < len(line):
                lines[index] = line[:target_len].rstrip(" ;,") + "..."
                break
        if len("\n".join(lines)) <= max_chars:
            return "\n".join(lines)
    return "\n".join(lines)[:max_chars].rstrip()


def scheduler_job(data: dict, name: str) -> dict:
    for job in data.get("jobs") or []:
        if isinstance(job, dict) and job.get("name") == name:
            return job
    return {}


def scheduler_job_has_issue(data: dict, name: str) -> bool:
    job = scheduler_job(data, name)
    return bool(job.get("issues") or [])


def scheduler_only_eod_send_proof_issue(issues: list[str]) -> bool:
    if not issues:
        return False
    proof_markers = (
        "eod_telegram:unexpected_report_status:failed",
        "eod_telegram:report_empty_field:telegram_http_statuses",
        "eod_telegram:report_missing_field:telegram_http_statuses",
        "eod_telegram:report_missing_field:send_requested",
        "eod_telegram:report_missing_field:telegram_send_ok",
        "eod_telegram:report_unexpected_value:dry_run=True",
        "eod_telegram:report_unexpected_value:send_requested=False",
        "eod_telegram:report_unexpected_value:telegram_send_ok=False",
    )
    proof_prefixes = (
        "eod_telegram:last_successful_send_state_unusable:",
    )
    return all(
        any(issue == marker for marker in proof_markers)
        or any(issue.startswith(prefix) for prefix in proof_prefixes)
        for issue in issues
    )


def scheduler_only_eod_telegram_issue(issues: list[str]) -> bool:
    return bool(issues) and all(issue.startswith("eod_telegram:") for issue in issues)


def eod_telegram_credentials_missing(report: dict) -> bool:
    report_missing = (
        report.get("telegram_token_present") is False
        or report.get("telegram_chat_id_present") is False
    )
    if not report_missing:
        return False
    token, chat_id = telegram_config()
    return not (token and chat_id)


def refresh_timeout_seconds(env_name: str, default: int = 12) -> int:
    try:
        value = int(os.environ.get(env_name, str(default)))
    except ValueError:
        value = default
    return max(1, value)


def text_tail(value: object, limit: int = 1000) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[-limit:]
    if isinstance(value, str):
        return value[-limit:]
    return ""


def run_refresh_command(
    command: list[str],
    *,
    timeout_env: str,
    default_timeout: int = 12,
    ok_return_codes: set[int] | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
    include_stdout: bool = False,
    include_stderr: bool = False,
) -> dict:
    timeout = refresh_timeout_seconds(timeout_env, default_timeout)
    start_new_session = os.name == "posix"
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            start_new_session=start_new_session,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "attempted": True,
            "return_code": None,
            "ok": False,
            "timed_out": True,
            "timeout_seconds": exc.timeout,
            "stdout_tail": text_tail(exc.stdout),
            "stderr_tail": text_tail(exc.stderr),
        }
    ok_return_codes = ok_return_codes or {0}
    report = {
        "attempted": True,
        "return_code": process.returncode,
        "ok": process.returncode in ok_return_codes,
        "stdout_tail": text_tail(process.stdout),
        "stderr_tail": text_tail(process.stderr),
    }
    if include_stdout:
        report["stdout"] = process.stdout
    if include_stderr:
        report["stderr"] = process.stderr
    return report


def python_with_module(module_name: str, env_name: str) -> str:
    override = os.environ.get(env_name)
    candidates = [
        override,
        sys.executable,
        "/home/linuxbrew/.linuxbrew/bin/python3",
        "python3",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            process = subprocess.run(
                [candidate, "-c", f"import {module_name}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if process.returncode == 0:
            return candidate
    return override or sys.executable


def recover_timed_out_refresh_from_report(
    result: dict,
    report_path: Path,
    *,
    ok_statuses: set[str] | None = None,
    max_age_hours: float = EOD_REFRESH_FALLBACK_MAX_AGE_HOURS,
    require_zero_issues: bool = True,
) -> dict:
    if result.get("timed_out") is not True:
        return result
    payload = read_json(report_path)
    status = str(payload.get("status") or "").strip()
    ok_statuses = ok_statuses or {"ok"}
    age_hours = iso_age_hours(payload.get("generated_at") or payload.get("checked_at"))
    age_ok = age_hours is not None and -1 <= age_hours <= max_age_hours
    issue_count = compact_count(payload.get("issue_count"))
    status_ok = status in ok_statuses
    issues_ok = (issue_count == 0) if require_zero_issues else True
    result["timeout_report_path"] = str(report_path)
    result["timeout_report_status"] = status or payload.get("status")
    result["timeout_report_age_hours"] = age_hours
    result["timeout_report_issue_count"] = issue_count
    if status_ok and age_ok and issues_ok:
        result["ok"] = True
        result["timeout_recovered_by_report"] = True
        result["timeout_recovery_max_age_hours"] = max_age_hours
    else:
        result["timeout_recovered_by_report"] = False
        result["timeout_recovery_blocked_reason"] = (
            "report_not_usable_after_timeout:"
            f"status_ok={status_ok};age_ok={age_ok};issues_ok={issues_ok}"
        )
    return result


def skipped_refresh(reason: str, *, blocker: str | None = None) -> dict:
    result = {
        "attempted": False,
        "reason": reason,
    }
    if blocker:
        result["blocker"] = blocker
    return result


def daily_sync_auth_blocker_reason(daily: dict | None = None) -> str | None:
    daily = daily if daily is not None else read_json(current_report_path(BASELANE_DAILY_SYNC_REPORT))
    if daily.get("status") in {"missing", "unreadable"}:
        return None
    daily_reason = str(daily.get("daily_sync_auth_blocker_reason") or "").strip()
    if daily_reason in {"baselane_login_auth_401", "baselane_login_recaptcha_required", "baselane_login_required", "baselane_manual_auth_required"}:
        return daily_reason
    sync_failure_class = str(daily.get("sync_report_failure_class") or "").strip()
    sync_status = str(daily.get("sync_report_status") or "").strip()
    effective_status = str(daily.get("effective_status") or daily.get("status") or "").strip()
    sync_blocked = sync_status in {"failed", "review", "error"} and effective_status not in {"", "ok"}
    if not sync_blocked:
        return None
    if sync_failure_class == "baselane_login_auth_401":
        return "baselane_login_auth_401"
    login_wait = read_json(named_report_path("baselane_login_wait_report.json"))
    login_reason = str(login_wait.get("reason") or "").strip()
    if login_reason == "baselane_login_recaptcha_required" or login_wait.get("recaptcha_present") is True:
        return "baselane_login_recaptcha_required"
    if login_reason in {"baselane_login_required", "baselane_login_wait_failed", "cdp_login_failed"}:
        return "baselane_login_required"
    cdp_auth_recovery = read_json(named_report_path("baselane_auth_recovery_report.json"))
    if cdp_auth_recovery.get("manual_auth_required") is True:
        manual_auth_reason = str(cdp_auth_recovery.get("manual_auth_reason") or "").strip()
        if manual_auth_reason == "recovery_attempted_but_baselane_not_verified":
            return "baselane_login_required"
        return "baselane_manual_auth_required"
    return None


def daily_sync_disk_space_blocker_reason(daily: dict | None = None) -> str | None:
    daily = daily if daily is not None else read_json(current_report_path(BASELANE_DAILY_SYNC_REPORT))
    if daily.get("status") in {"missing", "unreadable"}:
        return None
    status = str(daily.get("disk_space_preflight_status") or "").strip()
    if status and status not in {"ok", "missing"}:
        return "low_local_disk_space"
    issues = [str(item) for item in (daily.get("issues") or []) if item]
    if any(item.startswith("disk_space_preflight=") for item in issues):
        return "low_local_disk_space"
    return None


def daily_sync_auth_blocker_active(daily: dict | None = None) -> bool:
    return daily_sync_auth_blocker_reason(daily) is not None


def integrity_review_is_eod_send_proof_only(payload: dict) -> bool:
    if str(payload.get("status") or "").strip() != "review":
        return False
    issues = payload.get("issues")
    if not isinstance(issues, list) or not issues:
        return False
    for item in issues:
        if not isinstance(item, dict):
            return False
        if item.get("report") != EOD_SEND_STATE_REPORT.name:
            return False
        if item.get("code") not in EOD_SEND_PROOF_INTEGRITY_CODES:
            return False
    reports = payload.get("reports") if isinstance(payload.get("reports"), dict) else {}
    issue_reports = {
        name
        for name, report in reports.items()
        if isinstance(report, dict) and compact_count(report.get("issue_count")) > 0
    }
    return issue_reports in ({EOD_SEND_STATE_REPORT.name}, set())


def integrity_review_is_eod_preview_only(payload: dict) -> bool:
    if str(payload.get("status") or "").strip() != "review":
        return False
    issues = payload.get("issues")
    if not isinstance(issues, list) or not issues:
        return False
    for item in issues:
        if not isinstance(item, dict):
            return False
        code = str(item.get("code") or "")
        report = str(item.get("report") or "")
        if report == "baselane_eod_telegram_preview_report.json":
            if not code.startswith("eod_report_") and code != "report_read_error":
                return False
            continue
        if report == "cross_report_consistency" and code in {
            "cross_eod_message_missing_assetrail_status",
            "cross_eod_message_missing_recovery_status",
        }:
            continue
        return False
    reports = payload.get("reports") if isinstance(payload.get("reports"), dict) else {}
    issue_reports = {
        name
        for name, report in reports.items()
        if isinstance(report, dict) and compact_count(report.get("issue_count")) > 0
    }
    return issue_reports <= {"baselane_eod_telegram_preview_report.json", "cross_report_consistency"}


def recover_report_integrity_timeout(result: dict, report_path: Path, payload: dict) -> dict:
    if result.get("timed_out") is True and integrity_review_is_eod_send_proof_only(payload):
        age_hours = iso_age_hours(payload.get("generated_at") or payload.get("checked_at"))
        age_ok = age_hours is not None and -1 <= age_hours <= EOD_REFRESH_FALLBACK_MAX_AGE_HOURS
        result["timeout_report_path"] = str(report_path)
        result["timeout_report_status"] = payload.get("status")
        result["timeout_report_age_hours"] = age_hours
        result["timeout_report_issue_count"] = compact_count(payload.get("issue_count"))
        if age_ok:
            result["ok"] = True
            result["timeout_recovered_by_report"] = True
            result["timeout_recovery_expected_review_only"] = "eod_send_proof"
            result["timeout_recovery_max_age_hours"] = EOD_REFRESH_FALLBACK_MAX_AGE_HOURS
            return result
        result["timeout_recovered_by_report"] = False
        result["timeout_recovery_blocked_reason"] = "report_not_usable_after_timeout:eod_send_proof_review_stale"
        return result
    return recover_timed_out_refresh_from_report(result, report_path)


def refresh_lofty_cdp_preflight() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_LOFTY_CDP_PREFLIGHT") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "lofty_cdp_preflight_report.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    command = [sys.executable, str(script), "--report", str(current_report_path(LOFTY_CDP_PREFLIGHT_REPORT))]
    if os.environ.get("BASELANE_EOD_LOFTY_CDP_RECOVER_LOGIN", "1") != "0":
        command.append("--recover-login")
    else:
        command.append("--no-recover-login")
    if os.environ.get("LOFTY_CDP_BASE"):
        command.extend(["--base-url", os.environ["LOFTY_CDP_BASE"]])
    return run_refresh_command(command, timeout_env="BASELANE_EOD_LOFTY_CDP_PREFLIGHT_TIMEOUT_SEC")


def refresh_lofty_cdp_ensure() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_LOFTY_CDP_ENSURE") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "lofty_cdp_ensure.sh"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    env = os.environ.copy()
    env["LOFTY_CDP_ENSURE_REPORT"] = str(current_report_path(LOFTY_CDP_ENSURE_REPORT))
    return run_refresh_command([str(script)], timeout_env="BASELANE_EOD_LOFTY_CDP_ENSURE_TIMEOUT_SEC", env=env)


def refresh_hemlane_cdp_preflight() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_HEMLANE_CDP_PREFLIGHT") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "hemlane_cdp_preflight.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    command = [sys.executable, str(script), "--report", str(current_report_path(HEMLANE_CDP_PREFLIGHT_REPORT))]
    if os.environ.get("BASELANE_EOD_HEMLANE_CDP_RECOVER_LOGIN", "1") != "0":
        command.append("--recover-login")
    return run_refresh_command(
        command,
        timeout_env="BASELANE_EOD_HEMLANE_CDP_PREFLIGHT_TIMEOUT_SEC",
        default_timeout=20,
    )


def hemlane_capture_recaptcha_manual_auth_required(capture_report: dict) -> bool:
    return (
        capture_report.get("issue") == "recaptcha_required"
        or capture_report.get("manual_auth_reason") == "recaptcha_required"
        or capture_report.get("manual_auth_blocker") == "recaptcha_required"
        or capture_report.get("bitwarden_login_recaptcha_error") is True
    )


def hemlane_preflight_ready_for_capture(preflight_report: dict) -> bool:
    return (
        preflight_report.get("status") == "ok"
        and compact_count(preflight_report.get("logged_in_tab_count")) > 0
    )


def hemlane_rent_roll_source_current(source_report: dict) -> bool:
    return (
        source_report.get("status") == "ok"
        and source_report.get("source_current") is True
        and source_report.get("freshness_status") == "current"
        and compact_count(source_report.get("source_file_count")) > 0
        and compact_count(source_report.get("source_blocker_count")) == 0
    )


def hemlane_capture_refresh_needed(goal_audit: dict, capture_report: dict, preflight_report: dict | None = None) -> bool:
    if os.environ.get("BASELANE_EOD_FORCE_HEMLANE_CDP_CAPTURE") == "1":
        return True
    if hemlane_capture_recaptcha_manual_auth_required(capture_report):
        if hemlane_preflight_ready_for_capture(preflight_report or {}):
            return True
        return False
    if capture_report.get("status") in {"missing", "unreadable", "review"}:
        return True
    for requirement in goal_audit.get("requirements") or []:
        if not isinstance(requirement, dict):
            continue
        if requirement.get("id") != "monthly_comms_rent_roll_context":
            continue
        if requirement.get("status") != "ok":
            return True
        evidence = requirement.get("evidence") if isinstance(requirement.get("evidence"), dict) else {}
        source = evidence.get("rent_roll_source") if isinstance(evidence.get("rent_roll_source"), dict) else {}
        freshness = evidence.get("rent_roll_freshness_status") or source.get("freshness_status")
        if freshness not in {None, "", "current"}:
            return True
        if evidence.get("rent_roll_stale_export_dates") or source.get("stale_export_dates"):
            return True
        if compact_count(evidence.get("rent_roll_matched_count")) == 0:
            return True
    return False


def refresh_hemlane_cdp_capture() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_HEMLANE_CDP_CAPTURE") == "1":
        return {"attempted": False, "reason": "disabled"}
    comms_dir = comms_root()
    script = comms_dir / "scripts" / "monthly_hemlane_cdp.sh"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    monthly = read_json(named_report_path("baselane_financials_monthly_run_report.json"))
    readiness = read_json(named_report_path("baselane_financials_monthly_readiness.json"))
    run_month = report_run_month(monthly, readiness)
    capture_report_path = hemlane_capture_report_path(run_month)
    rent_roll_source = read_json(hemlane_rent_roll_source_path(run_month))
    goal_audit = read_json(current_report_path(BASELANE_GOAL_AUDIT_REPORT))
    capture_report = read_json(capture_report_path)
    hemlane_preflight = read_json(current_report_path(HEMLANE_CDP_PREFLIGHT_REPORT))
    if hemlane_rent_roll_source_current(rent_roll_source):
        return {
            "attempted": False,
            "reason": "rent_roll_source_current",
            "run_month": run_month,
            "capture_report": str(capture_report_path),
            "rent_roll_source": str(hemlane_rent_roll_source_path(run_month)),
            "next_action": "Continue monthly owner update workflow.",
            "rerun_command": POST_AUTH_RESUME_COMMAND,
        }
    if not hemlane_capture_refresh_needed(goal_audit, capture_report, hemlane_preflight):
        reason = (
            "hemlane_recaptcha_manual_auth_required"
            if hemlane_capture_recaptcha_manual_auth_required(capture_report)
            else "rent_roll_source_current"
        )
        return {
            "attempted": False,
            "reason": reason,
            "run_month": run_month,
            "capture_report": str(capture_report_path),
            "capture_next_action": capture_report.get("next_action"),
            "next_action": hemlane_post_auth_resume_action(capture_report.get("next_action"), hemlane_preflight),
            "rerun_command": POST_AUTH_RESUME_COMMAND,
        }
    command = ["bash", "scripts/monthly_hemlane_cdp.sh", "--month", run_month, "--dry-run"]
    env = os.environ.copy()
    # Do not submit stored credentials. A human must establish the portal
    # session in a visible browser before this read-only refresh runs.
    env["HEMLANE_CDP_TRY_BITWARDEN_LOGIN"] = "0"
    result = run_refresh_command(
        command,
        timeout_env="BASELANE_EOD_HEMLANE_CDP_CAPTURE_TIMEOUT_SEC",
        default_timeout=90,
        cwd=comms_dir,
        env=env,
        include_stdout=True,
    )
    stdout = result.pop("stdout", "")
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {}
        if payload:
            attempts = payload.get("login_recovery_attempts") if isinstance(payload.get("login_recovery_attempts"), list) else []
            recovery_try_count = payload.get("login_recovery_try_count")
            if recovery_try_count is None:
                recovery_try_count = payload.get("login_recovery_attempt_count")
            if recovery_try_count is None:
                recovery_try_count = len(attempts)
            summary = {
                "status": payload.get("status"),
                "issue": payload.get("issue"),
                "run_month": payload.get("run_month") or run_month,
                "row_count": payload.get("row_count"),
                "login_recovery_attempt_count": recovery_try_count,
                "login_recovery_try_count": recovery_try_count,
                "manual_auth_required": payload.get("manual_auth_required"),
                "manual_auth_reason": payload.get("manual_auth_reason"),
                "manual_auth_phase": payload.get("manual_auth_phase"),
                "manual_auth_blocker": payload.get("manual_auth_blocker"),
                "bitwarden_login_status": payload.get("bitwarden_login_status"),
                "bitwarden_login_attempted": payload.get("bitwarden_login_attempted"),
                "bitwarden_login_submit_ok": payload.get("bitwarden_login_submit_ok"),
                "bitwarden_login_recaptcha_error": payload.get("bitwarden_login_recaptcha_error"),
                "safe_to_retry_after_manual_auth": payload.get("safe_to_retry_after_manual_auth"),
                "capture_next_action": payload.get("next_action"),
                "next_action": hemlane_post_auth_resume_action(payload.get("next_action"), hemlane_preflight),
                "rerun_command": POST_AUTH_RESUME_COMMAND,
            }
            result["summary"] = summary
            result["stdout_tail"] = json.dumps(summary, sort_keys=True)
    if not result.get("ok"):
        result["monthly_lofty_updates_context_refresh"] = refresh_monthly_lofty_updates_context(run_month, comms_dir)
    return {
        **result,
        "run_month": run_month,
        "cwd": str(comms_dir),
        "command": command,
        "capture_report": str(capture_report_path),
    }


def refresh_monthly_lofty_updates_context(run_month: str, comms_dir: Path | None = None) -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_MONTHLY_LOFTY_UPDATES_CONTEXT") == "1":
        return {"attempted": False, "reason": "disabled"}
    comms_dir = comms_dir or comms_root()
    script = comms_dir / "scripts" / "monthly_lofty_updates.sh"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    rent_roll_dir = os.environ.get("BASELANE_RENT_ROLL_DIR") or os.environ.get("RENT_ROLL_DIR") or DEFAULT_RENT_ROLL_DIR
    command = [
        "bash",
        "scripts/monthly_lofty_updates.sh",
        "--month",
        run_month,
        "--out-dir",
        "updates",
        "--rent-roll-dir",
        rent_roll_dir,
        "--dry-run",
    ]
    dom_json = comms_dir / "updates" / f"{run_month}-hemlane-rent-roll-live-dom.json"
    if dom_json.is_file():
        command.extend(["--rent-roll-dom-json", str(dom_json)])
    result = run_refresh_command(
        command,
        timeout_env="BASELANE_EOD_MONTHLY_LOFTY_UPDATES_CONTEXT_TIMEOUT_SEC",
        default_timeout=90,
        cwd=comms_dir,
    )
    return {
        **result,
        "run_month": run_month,
        "cwd": str(comms_dir),
        "command": command,
    }


def refresh_lofty_public_path_guard() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_PUBLIC_PATH_GUARD") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "lofty_public_path_guard.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    report_path = current_report_path(LOFTY_PUBLIC_PATH_GUARD_REPORT)
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--root",
            str(ROOT),
            "--report",
            str(report_path),
            "--skip-artifact-public-target-checks",
        ],
        timeout_env="BASELANE_EOD_PUBLIC_PATH_GUARD_TIMEOUT_SEC",
        default_timeout=10,
    )
    return recover_timed_out_refresh_from_report(result, report_path)


def refresh_discord_public_financial_source_guard() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_DISCORD_PUBLIC_FINANCIAL_SOURCE_GUARD") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "discord_public_financial_source_guard.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--root",
            str(ROOT),
            "--report",
            str(DISCORD_PUBLIC_FINANCIAL_SOURCE_GUARD_REPORT),
            "--delete-gl-rows",
        ],
        timeout_env="BASELANE_EOD_DISCORD_PUBLIC_GUARD_TIMEOUT_SEC",
        default_timeout=30,
        include_stdout=True,
    )
    stdout = result.pop("stdout", "")
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return recover_timed_out_refresh_from_report(
                result,
                current_report_path(DISCORD_PUBLIC_FINANCIAL_SOURCE_GUARD_REPORT),
            )
        summary = {
            "status": payload.get("status"),
            "issue_count": int(payload.get("issue_count") or 0),
            "financial_source_policy_ok": bool(payload.get("financial_source_policy_ok")),
            "canonical_financial_dir": payload.get("canonical_financial_dir"),
            "canonical_snapshot_dir": payload.get("canonical_snapshot_dir"),
            "financial_doc_count": int(payload.get("financial_doc_count") or 0),
            "update_doc_count": int(payload.get("update_doc_count") or 0),
            "legacy_financials_folder_count": int(payload.get("legacy_financials_folder_count") or 0),
            "legacy_financials_folder_ignored_count": int(payload.get("legacy_financials_folder_ignored_count") or 0),
            "legacy_financials_folder_policy": payload.get("legacy_financials_folder_policy"),
            "deleted_gl_rows_count": int(payload.get("deleted_gl_rows_count") or 0),
        }
        result["summary"] = summary
        result["stdout_tail"] = json.dumps(summary, sort_keys=True)
    return recover_timed_out_refresh_from_report(
        result,
        current_report_path(DISCORD_PUBLIC_FINANCIAL_SOURCE_GUARD_REPORT),
    )


def refresh_local_model_preflight() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_LOCAL_MODEL_PREFLIGHT") == "1":
        return {"attempted": False, "reason": "disabled"}
    report_path = current_report_path(BASELANE_LOCAL_MODEL_PREFLIGHT_REPORT)
    report = read_json(report_path)
    if os.environ.get("BASELANE_EOD_FORCE_LOCAL_MODEL_PREFLIGHT") != "1" and local_model_preflight_ok(report):
        return {
            "attempted": False,
            "reason": "current",
            "report": str(report_path),
            "generated_at": report.get("generated_at"),
            "model": report.get("model"),
        }
    script = ROOT / "scripts" / "baselane_local_model_preflight.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--report",
            str(report_path),
            "--json",
        ],
        timeout_env="BASELANE_EOD_LOCAL_MODEL_PREFLIGHT_TIMEOUT_SEC",
        default_timeout=1020,
        include_stdout=True,
    )
    stdout = result.pop("stdout", "")
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return result
        direct_smoke = payload.get("direct_smoke") if isinstance(payload.get("direct_smoke"), dict) else {}
        summary = {
            "status": payload.get("status"),
            "issue_count": int(payload.get("issue_count") or 0),
            "model": payload.get("model"),
            "provider": payload.get("provider"),
            "model_id": payload.get("model_id"),
            "model_available": payload.get("model_available"),
            "direct_smoke_attempted": direct_smoke.get("attempted"),
            "direct_smoke_ok": direct_smoke.get("ok"),
            "direct_smoke_response": direct_smoke.get("response"),
            "validation_digest": payload.get("validation_digest"),
            "generated_at": payload.get("generated_at"),
        }
        result["summary"] = summary
        result["stdout_tail"] = json.dumps(summary, sort_keys=True)
    return result


def refresh_scheduler_audit() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_SCHEDULER_AUDIT") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "baselane_scheduler_audit.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    return run_refresh_command(
        [sys.executable, str(script), "--root", str(ROOT), "--report", str(current_report_path(BASELANE_SCHEDULER_AUDIT_REPORT))],
        timeout_env="BASELANE_EOD_SCHEDULER_AUDIT_TIMEOUT_SEC",
    )


def refresh_daily_source_cash_balance_audit() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_SOURCE_CASH_BALANCE_AUDIT") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "baselane_daily_source_cash_balance_audit.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    report_path = current_report_path(BASELANE_DAILY_SOURCE_CASH_BALANCE_REPORT)
    python = python_with_module("openpyxl", "BASELANE_EOD_WORKBOOK_PYTHON")
    result = run_refresh_command(
        [python, str(script), "--apply", "--report", str(report_path)],
        timeout_env="BASELANE_EOD_SOURCE_CASH_BALANCE_TIMEOUT_SEC",
        default_timeout=60,
        ok_return_codes={0, 2},
    )
    result["ok"] = bool(result.get("ok")) and report_path.is_file()
    return recover_timed_out_refresh_from_report(
        result,
        report_path,
        ok_statuses={"ok", "review"},
        require_zero_issues=False,
    )


def default_dropbox_root() -> Path:
    for candidate in (
        Path(os.environ.get("DROPBOX_ROOT", "")) if os.environ.get("DROPBOX_ROOT") else None,
        Path("/mnt/c/Users/digit/Dropbox"),
        Path("/data/Dropbox"),
        Path.home() / "Dropbox",
        Path("/home/digit/Dropbox"),
    ):
        if candidate and candidate.is_dir():
            return candidate
    return Path("/mnt/c/Users/digit/Dropbox")


def default_baselane_ledger_dir(dropbox_root: Path) -> Path:
    for candidate in (
        Path(os.environ.get("BASELANE_LEDGER_DIR", "")) if os.environ.get("BASELANE_LEDGER_DIR") else None,
        dropbox_root / "Projects" / "assetrail",
        dropbox_root / "Projects" / "transaction_tracker",
    ):
        if candidate and candidate.is_dir():
            return candidate
    return dropbox_root / "Projects" / "assetrail"


def refresh_daily_disk_space_preflight() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_DISK_SPACE_PREFLIGHT") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "baselane_disk_space_preflight.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    dropbox_root = default_dropbox_root()
    baselane_ledger_dir = default_baselane_ledger_dir(dropbox_root)
    report_path = current_report_path(BASELANE_DAILY_DISK_SPACE_PREFLIGHT_REPORT)
    min_free_mib = os.environ.get("BASELANE_DISK_PREFLIGHT_MIN_FREE_MIB", "10240")
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--path",
            f"dropbox_root={dropbox_root}",
            "--path",
            f"baselane_ledger_dir={baselane_ledger_dir}",
            "--min-free-mib",
            str(min_free_mib),
            "--report",
            str(report_path),
        ],
        timeout_env="BASELANE_EOD_DISK_SPACE_PREFLIGHT_TIMEOUT_SEC",
        default_timeout=30,
        ok_return_codes={0, 2},
    )
    result["ok"] = bool(result.get("ok")) and report_path.is_file()
    return recover_timed_out_refresh_from_report(
        result,
        report_path,
        ok_statuses={"ok", "review"},
        require_zero_issues=False,
    )


def refresh_daily_sync_report() -> dict:
    script = ROOT / "scripts" / "baselane_daily_sync_report.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    report_path = current_report_path(BASELANE_DAILY_SYNC_REPORT)
    result = run_refresh_command(
        [sys.executable, str(script), "--root", str(ROOT), "--report", str(report_path)],
        timeout_env="BASELANE_EOD_DAILY_SYNC_REPORT_TIMEOUT_SEC",
        ok_return_codes={0, 2},
    )
    result["ok"] = bool(result.get("ok")) and report_path.is_file()
    return recover_timed_out_refresh_from_report(
        result,
        report_path,
        ok_statuses={"ok", "review"},
        require_zero_issues=False,
    )


def refresh_goal_audit() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_GOAL_AUDIT") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "baselane_financials_goal_audit.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    return run_refresh_command(
        [
            sys.executable,
            str(script),
            "--root",
            str(ROOT),
            "--report",
            str(current_report_path(BASELANE_GOAL_AUDIT_REPORT)),
            "--markdown",
            str(named_report_path("baselane_financials_goal_audit.md")),
        ],
        timeout_env="BASELANE_EOD_GOAL_AUDIT_TIMEOUT_SEC",
        ok_return_codes={0, 2},
    )


def refresh_operations_packet() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_OPERATIONS_PACKET") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "baselane_financials_operations_packet.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    return run_refresh_command(
        [
            sys.executable,
            str(script),
            "--root",
            str(ROOT),
            "--report",
            str(named_report_path("baselane_financials_operations_packet.json")),
            "--markdown",
            str(named_report_path("baselane_financials_operations_packet.md")),
        ],
        timeout_env="BASELANE_EOD_OPERATIONS_PACKET_TIMEOUT_SEC",
        default_timeout=20,
        ok_return_codes={0, 2},
    )


def refresh_weekly_report_reconcile() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_WEEKLY_RECONCILE") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "baselane_reconcile_weekly_report_counts.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    result = run_refresh_command(
        [sys.executable, str(script), "--root", str(ROOT)],
        timeout_env="BASELANE_EOD_WEEKLY_RECONCILE_TIMEOUT_SEC",
        include_stdout=True,
    )
    stdout = result.pop("stdout", "")
    parsed = {}
    try:
        parsed = json.loads(stdout or "{}")
        report_path = current_report_path(BASELANE_WEEKLY_RECONCILE_REPORT)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(parsed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        parsed = {}
    result["result"] = parsed
    return result


def refresh_source_fix_action_queue() -> dict:
    script = ROOT / "scripts" / "baselane_ecogl_source_fix_action_queue.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    report_path = current_report_path(BASELANE_SOURCE_FIX_ACTION_QUEUE_REPORT)
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--root",
            str(ROOT),
            "--report",
            str(report_path),
            "--csv",
            str(named_report_path("baselane_ecogl_source_fix_action_queue.csv")),
            "--markdown",
            str(named_report_path("baselane_ecogl_source_fix_action_queue.md")),
        ],
        timeout_env="BASELANE_EOD_SOURCE_FIX_ACTION_QUEUE_TIMEOUT_SEC",
        ok_return_codes={0, 2},
    )
    result["ok"] = bool(result.get("ok")) and report_path.is_file()
    return result


def refresh_source_fix_apply_dry_run() -> dict:
    script = ROOT / "scripts" / "baselane_ecogl_source_fix_apply.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    report_path = current_report_path(BASELANE_SOURCE_FIX_APPLY_REPORT)
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--root",
            str(ROOT),
            "--report",
            str(report_path),
            "--csv",
            str(named_report_path("baselane_ecogl_source_fix_apply.csv")),
            "--markdown",
            str(named_report_path("baselane_ecogl_source_fix_apply.md")),
        ],
        timeout_env="BASELANE_EOD_SOURCE_FIX_APPLY_TIMEOUT_SEC",
        ok_return_codes={0, 2},
    )
    result["ok"] = bool(result.get("ok")) and report_path.is_file()
    return result


def refresh_native_split_plan() -> dict:
    script = ROOT / "scripts" / "baselane_native_split_plan.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    report_path = current_report_path(BASELANE_NATIVE_SPLIT_PLAN_REPORT)
    source_index_path = latest_source_transaction_index_path()
    apply_report_path = latest_native_split_apply_report_path()
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--source-index",
            str(source_index_path),
            "--report",
            str(report_path),
            "--csv",
            str(named_report_path("baselane_native_split_plan.csv")),
            "--markdown",
            str(named_report_path("baselane_native_split_plan.md")),
            "--apply-report",
            str(apply_report_path),
        ],
        timeout_env="BASELANE_EOD_NATIVE_SPLIT_PLAN_TIMEOUT_SEC",
        default_timeout=60,
        ok_return_codes={0, 2},
    )
    result["source_index"] = str(source_index_path)
    result["apply_report"] = str(apply_report_path)
    result["ok"] = bool(result.get("ok")) and report_path.is_file()
    return result


def refresh_owner_email_send_guard() -> dict:
    script = ROOT / "scripts" / "baselane_owner_email_send_guard.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--root",
            str(ROOT),
            "--readiness-report",
            str(current_report_path(MONTHLY_READINESS_REPORT)),
            "--publish-report",
            str(current_report_path(LOFTY_PM_PUBLISH_REPORT)),
            "--monthly-run-report",
            str(current_report_path(MONTHLY_RUN_REPORT)),
            "--owner-email-packet-report",
            str(current_report_path(OWNER_EMAIL_PACKET_REPORT)),
            "--discord-send-report",
            str(current_report_path(MONTHLY_DISCORD_PROPERTY_UPDATE_SEND_REPORT)),
            "--transfer-reconciliation-report",
            str(current_report_path(BASELANE_LOFTY_TRANSFER_REQUIREMENTS_REPORT)),
            "--discord-plan-validation-report",
            str(current_report_path(MONTHLY_DISCORD_ALL_SEND_PLAN_VALIDATION_REPORT)),
            "--report",
            str(current_report_path(OWNER_EMAIL_SEND_GUARD_REPORT)),
        ],
        timeout_env="BASELANE_EOD_OWNER_EMAIL_SEND_GUARD_TIMEOUT_SEC",
        ok_return_codes={0, 2},
        include_stdout=True,
    )
    result.pop("stdout", "")
    payload = read_json(current_report_path(OWNER_EMAIL_SEND_GUARD_REPORT))
    if payload.get("status") not in {"missing", "unreadable"}:
        summary = {
            "status": payload.get("status"),
            "issue_count": int(payload.get("issue_count") or 0),
            "run_month": payload.get("run_month"),
            "send_allowed": payload.get("send_allowed"),
            "safe_block": payload.get("safe_block"),
            "no_spam_guard_ok": payload.get("no_spam_guard_ok"),
            "max_once_monthly_ok": payload.get("max_once_monthly_ok"),
            "readiness_owner_email_allowed": payload.get("readiness_owner_email_allowed"),
            "send_requested": payload.get("send_requested"),
            "effective_send_owner_emails": payload.get("effective_send_owner_emails"),
            "send_lock_status": payload.get("send_lock_status"),
            "send_lock_file_loaded": payload.get("send_lock_file_loaded"),
            "send_lock_file_status": payload.get("send_lock_file_status"),
            "send_lock_file_unreadable": payload.get("send_lock_file_unreadable"),
            "send_lock_run_month": payload.get("send_lock_run_month"),
            "send_lock_run_month_matches": payload.get("send_lock_run_month_matches"),
            "send_lock_manual_review_required": payload.get("send_lock_manual_review_required"),
            "send_lock_manual_review_reason": payload.get("send_lock_manual_review_reason"),
            "send_lock_safe_retry_without_duplicate_owner_email": payload.get("send_lock_safe_retry_without_duplicate_owner_email"),
            "send_lock_owner_email_send_intended_count": int(payload.get("send_lock_owner_email_send_intended_count") or 0),
            "send_lock_owner_email_send_attempted": payload.get("send_lock_owner_email_send_attempted"),
            "send_lock_owner_email_send_proven_complete": payload.get("send_lock_owner_email_send_proven_complete"),
            "owner_email_will_send_count": int(payload.get("owner_email_will_send_count") or 0),
            "owner_email_send_evidence_count": int(payload.get("owner_email_send_evidence_count") or 0),
            "owner_email_send_evidence_issue_count": int(payload.get("owner_email_send_evidence_issue_count") or 0),
        }
        result["summary"] = summary
        result["stdout_tail"] = json.dumps(summary, sort_keys=True)
    result["ok"] = bool(result.get("ok")) and current_report_path(OWNER_EMAIL_SEND_GUARD_REPORT).is_file()
    return result


def refresh_owner_email_packet() -> dict:
    script = ROOT / "scripts" / "lofty_monthly_owner_email_packet.py"
    runtime_map = current_report_path(LOFTY_PM_RUNTIME_MAP)
    recipients_csv = current_report_path(OWNER_EMAIL_PACKET_RECIPIENTS_CSV)
    report_path = current_report_path(OWNER_EMAIL_PACKET_REPORT)
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    if not runtime_map.is_file():
        return {"attempted": False, "reason": "missing_runtime_map", "runtime_map": str(runtime_map)}
    monthly = read_json(named_report_path("baselane_financials_monthly_run_report.json"))
    readiness = read_json(named_report_path("baselane_financials_monthly_readiness.json"))
    publish = read_json(named_report_path("baselane_financials_monthly_lofty_pm_publish.json"))
    run_month = report_run_month(monthly, readiness, publish)
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--runtime-map",
            str(runtime_map),
            "--recipients-csv",
            str(recipients_csv),
            "--run-month",
            run_month,
            "--sent-state-file",
            str(current_report_path(OWNER_EMAIL_PACKET_SENT_STATE)),
            "--out-dir",
            str(current_report_path(OWNER_EMAIL_PACKET_PREVIEW_DIR)),
            "--report",
            str(report_path),
            "--live-update-capture-report",
            str(current_report_path(LOFTY_LIVE_UPDATE_CAPTURE_REPORT)),
            "--listing-cleanup-queue-report",
            str(current_report_path(LOFTY_LISTING_CLEANUP_QUEUE_REPORT)),
            "--review-candidate-packet-report",
            str(current_report_path(LOFTY_REVIEW_CANDIDATE_PACKET_REPORT)),
            "--dry-run",
        ],
        timeout_env="BASELANE_EOD_OWNER_EMAIL_PACKET_TIMEOUT_SEC",
        ok_return_codes={0, 2},
    )
    result = recover_timed_out_refresh_from_report(
        result,
        report_path,
        ok_statuses={"ok", "review"},
        require_zero_issues=False,
    )
    result["run_month"] = run_month
    payload = read_json(report_path)
    result["summary"] = owner_email_packet_summary(payload)
    result["ok"] = bool(result.get("ok")) and payload.get("status") not in {"missing", "unreadable"}
    return result


def refresh_lofty_empty_updates_backfill_queue() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_LOFTY_EMPTY_UPDATES_BACKFILL_QUEUE") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "lofty_empty_updates_backfill_queue.py"
    property_gap_csv = current_report_path(REPORT_DIR / "lofty_owner_email_property_gaps.csv")
    report_path = current_report_path(LOFTY_EMPTY_UPDATES_BACKFILL_QUEUE_REPORT)
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    if not property_gap_csv.is_file():
        return {"attempted": False, "reason": "missing_property_gap_csv", "property_gap_csv": str(property_gap_csv)}
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--property-gap-csv",
            str(property_gap_csv),
            "--report",
            str(report_path),
        ],
        timeout_env="BASELANE_EOD_EMPTY_UPDATES_BACKFILL_QUEUE_TIMEOUT_SEC",
        ok_return_codes={0, 2},
    )
    payload = read_json(report_path)
    result["summary"] = lofty_empty_updates_backfill_queue_summary(payload)
    result["ok"] = bool(result.get("ok")) and payload.get("status") not in {"missing", "unreadable"}
    return result


def refresh_lofty_listing_cleanup_queue() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_LOFTY_LISTING_CLEANUP_QUEUE") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "lofty_listing_update_cleanup_queue.py"
    runtime_map = current_report_path(LOFTY_PM_RUNTIME_MAP)
    live_update_capture = current_report_path(LOFTY_LIVE_UPDATE_CAPTURE_REPORT)
    report_path = current_report_path(LOFTY_LISTING_CLEANUP_QUEUE_REPORT)
    publish_script = ROOT / "skills" / "lofty-pm" / "scripts" / "publish_latest_update_to_lofty.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    if not runtime_map.is_file():
        return {"attempted": False, "reason": "missing_runtime_map", "runtime_map": str(runtime_map)}
    if not live_update_capture.is_file():
        return {"attempted": False, "reason": "missing_live_update_capture", "live_update_capture": str(live_update_capture)}
    monthly = read_json(named_report_path("baselane_financials_monthly_run_report.json"))
    readiness = read_json(named_report_path("baselane_financials_monthly_readiness.json"))
    publish = read_json(named_report_path("baselane_financials_monthly_lofty_pm_publish.json"))
    run_month = report_run_month(monthly, readiness, publish)
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--live-update-capture-report",
            str(live_update_capture),
            "--runtime-map",
            str(runtime_map),
            "--report",
            str(report_path),
            "--publish-script",
            str(publish_script),
            "--review-candidate-packet-report",
            str(current_report_path(LOFTY_REVIEW_CANDIDATE_PACKET_REPORT)),
            "--run-month",
            run_month,
            "--require-monthly-financial-summary",
        ],
        timeout_env="BASELANE_EOD_LOFTY_LISTING_CLEANUP_QUEUE_TIMEOUT_SEC",
        ok_return_codes={0, 2},
    )
    result = recover_timed_out_refresh_from_report(
        result,
        report_path,
        ok_statuses={"ok", "review"},
        require_zero_issues=False,
    )
    result["run_month"] = run_month
    payload = read_json(report_path)
    result["dry_run_verify_refresh"] = refresh_lofty_listing_cleanup_dry_run_verify(payload)
    verify_payload = read_json(current_report_path(LOFTY_LISTING_CLEANUP_DRY_RUN_VERIFY_REPORT))
    result["summary"] = lofty_listing_cleanup_queue_summary(payload, verify_payload)
    result["ok"] = (
        bool(result.get("ok"))
        and payload.get("status") not in {"missing", "unreadable"}
        and result["summary"].get("dry_run_verify_ok") is True
    )
    return result


def unreviewed_financial_quarantine_summary(report: dict | None = None) -> dict:
    report = report if report is not None else read_json(current_report_path(LOFTY_UNREVIEWED_FINANCIAL_APPROVAL_QUARANTINE_REPORT))
    if report.get("status") in {"missing", "unreadable"}:
        return {
            "status": report.get("status"),
            "available": False,
            "unreviewed_approved_financial_count": 0,
            "ready_to_quarantine_count": 0,
            "command_count": 0,
        }
    return {
        "status": report.get("status"),
        "available": True,
        "unreviewed_approved_financial_count": compact_count(report.get("unreviewed_approved_financial_count")),
        "ready_to_quarantine_count": compact_count(report.get("ready_to_quarantine_count")),
        "review_count": compact_count(report.get("review_count")),
        "command_count": compact_count(report.get("command_count")),
        "mutates_dropbox_files": report.get("mutates_dropbox_files") is True,
        "mutates_lofty_listing": report.get("mutates_lofty_listing") is True,
        "sends_owner_email": report.get("sends_owner_email") is True,
        "commands_file": report.get("commands_file"),
        "approval_env_var": report.get("approval_env_var"),
        "digest_env_var": report.get("digest_env_var"),
        "digest_required_value": report.get("digest_required_value"),
    }


def refresh_unreviewed_financial_quarantine() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_UNREVIEWED_FINANCIAL_QUARANTINE") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "lofty_unreviewed_financial_approval_quarantine.py"
    guarded_apply_report = current_report_path(REPORT_DIR / "baselane_financials_monthly_guarded_apply.json")
    report_path = current_report_path(LOFTY_UNREVIEWED_FINANCIAL_APPROVAL_QUARANTINE_REPORT)
    commands_file = current_report_path(LOFTY_UNREVIEWED_FINANCIAL_APPROVAL_QUARANTINE_COMMANDS)
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    if not guarded_apply_report.is_file():
        return {"attempted": False, "reason": "missing_guarded_apply_report", "guarded_apply_report": str(guarded_apply_report)}
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--guarded-apply-report",
            str(guarded_apply_report),
            "--report",
            str(report_path),
            "--commands-file",
            str(commands_file),
        ],
        timeout_env="BASELANE_EOD_UNREVIEWED_FINANCIAL_QUARANTINE_TIMEOUT_SEC",
        ok_return_codes={0, 2},
    )
    payload = read_json(report_path)
    result["summary"] = unreviewed_financial_quarantine_summary(payload)
    result["ok"] = bool(result.get("ok")) and payload.get("status") not in {"missing", "unreadable"}
    return result


def refresh_lofty_listing_cleanup_dry_run_verify(queue_payload: dict) -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_LOFTY_LISTING_CLEANUP_DRY_RUN_VERIFY") == "1":
        return {"attempted": False, "reason": "disabled"}
    ready_count = compact_count(queue_payload.get("ready_listing_cleanup_count"))
    stdout_log = current_report_path(REPORT_DIR / "lofty_listing_update_cleanup_queue.dry-run-commands.stdout.log")
    stderr_log = current_report_path(REPORT_DIR / "lofty_listing_update_cleanup_queue.dry-run-commands.stderr.log")
    dry_run_commands_raw = str(queue_payload.get("dry_run_commands_file") or "").strip()
    if not dry_run_commands_raw:
        return {"attempted": False, "reason": "missing_dry_run_commands_file"}
    dry_run_commands = Path(dry_run_commands_raw)
    if not dry_run_commands.is_absolute():
        dry_run_commands = current_report_path(dry_run_commands)
    if not dry_run_commands.is_file():
        return {"attempted": False, "reason": "dry_run_commands_file_not_found", "dry_run_commands_file": str(dry_run_commands)}
    verifier_script = ROOT / "scripts" / "lofty_listing_cleanup_dry_run_verify.py"
    if not verifier_script.is_file():
        return {"attempted": False, "reason": "missing_verifier_script", "script": str(verifier_script)}
    if ready_count <= 0:
        stdout_log.write_text("", encoding="utf-8")
        stderr_log.write_text("", encoding="utf-8")
        dry_run_result = {
            "attempted": False,
            "reason": "no_ready_listing_cleanup",
            "ready_listing_cleanup_count": ready_count,
            "stdout_log_cleared": str(stdout_log),
            "stderr_log_cleared": str(stderr_log),
        }
    else:
        dry_run_result = run_refresh_command(
            ["bash", str(dry_run_commands)],
            timeout_env="BASELANE_EOD_LOFTY_LISTING_CLEANUP_DRY_RUN_TIMEOUT_SEC",
            default_timeout=180,
            ok_return_codes={0},
            cwd=ROOT,
            include_stdout=True,
            include_stderr=True,
        )
        stdout_log.write_text(str(dry_run_result.get("stdout") or ""), encoding="utf-8")
        stderr_log.write_text(str(dry_run_result.get("stderr") or ""), encoding="utf-8")
        dry_run_result.pop("stdout", None)
        dry_run_result.pop("stderr", None)
    verify_result = run_refresh_command(
        [
            sys.executable,
            str(verifier_script),
            "--queue-report",
            str(current_report_path(LOFTY_LISTING_CLEANUP_QUEUE_REPORT)),
            "--stdout-log",
            str(stdout_log),
            "--stderr-log",
            str(stderr_log),
            "--report",
            str(current_report_path(LOFTY_LISTING_CLEANUP_DRY_RUN_VERIFY_REPORT)),
        ],
        timeout_env="BASELANE_EOD_LOFTY_LISTING_CLEANUP_VERIFY_TIMEOUT_SEC",
        default_timeout=60,
        ok_return_codes={0, 2},
        cwd=ROOT,
    )
    verify_payload = read_json(current_report_path(LOFTY_LISTING_CLEANUP_DRY_RUN_VERIFY_REPORT))
    return {
        "attempted": True,
        "ready_listing_cleanup_count": ready_count,
        "dry_run_commands_file": str(dry_run_commands),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "dry_run_command": dry_run_result,
        "verify_command": verify_result,
        "verify_status": verify_payload.get("status"),
        "verify_issue_count": compact_count(verify_payload.get("issue_count")),
        "verify_digest_ok": str(verify_payload.get("ready_cleanup_idempotency_digest") or "").strip()
        == str(queue_payload.get("ready_cleanup_idempotency_digest") or "").strip(),
    }


def refresh_monthly_owner_review_gate() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_OWNER_REVIEW_GATE") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "baselane_monthly_owner_review_gate.py"
    report = named_report_path("baselane_monthly_owner_review_gate.json")
    markdown = named_report_path("baselane_monthly_owner_review_gate.md")
    csv_path = named_report_path("baselane_monthly_owner_review_gate.csv")
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--root",
            str(ROOT),
            "--report",
            str(report),
            "--markdown",
            str(markdown),
            "--csv",
            str(csv_path),
        ],
        timeout_env="BASELANE_EOD_OWNER_REVIEW_GATE_TIMEOUT_SEC",
        default_timeout=30,
        ok_return_codes={0, 2},
    )
    current_report = read_json(report)
    current_report_valid = owner_review_gate_payload_valid(current_report)
    result["report_valid"] = current_report_valid
    if not current_report_valid:
        result["invalid_report_reason"] = "owner_review_gate_missing_required_fields"
    result["ok"] = bool(result.get("ok")) and report.is_file() and current_report_valid
    return result


def owner_review_gate_payload_valid(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if str(payload.get("status") or "").strip() not in {"ok", "review", "failed"}:
        return False
    required_keys = {"blocker_count", "idempotency_key", "actionable_summary", "summary", "property_checklist"}
    if not required_keys.issubset(payload):
        return False
    if not isinstance(payload.get("actionable_summary"), dict):
        return False
    if not isinstance(payload.get("summary"), dict):
        return False
    if not isinstance(payload.get("property_checklist"), list):
        return False
    property_count = compact_count(payload["summary"].get("property_count"))
    if property_count <= 0:
        return False
    if len(payload["property_checklist"]) != property_count:
        return False
    if payload.get("status") == "review":
        actionable = payload.get("actionable_summary")
        if not isinstance(actionable.get("primary_blocker"), dict):
            return False
    return True


def refresh_monthly_readiness() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_MONTHLY_READINESS") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "baselane_monthly_readiness_report.py"
    report = named_report_path("baselane_financials_monthly_readiness.json")
    markdown = named_report_path("baselane_financials_monthly_readiness.md")
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--root",
            str(ROOT),
            "--report",
            str(report),
            "--markdown",
            str(markdown),
        ],
        timeout_env="BASELANE_EOD_MONTHLY_READINESS_TIMEOUT_SEC",
        default_timeout=30,
        ok_return_codes={0, 2},
        include_stdout=True,
    )
    stdout = result.pop("stdout", "")
    payload = {}
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {}
        if payload:
            primary = payload.get("primary_blocker") if isinstance(payload.get("primary_blocker"), dict) else {}
            summary = {
                "status": payload.get("status"),
                "run_month": payload.get("run_month"),
                "blocker_count": int(payload.get("blocker_count") or 0),
                "owner_email_allowed": payload.get("owner_email_allowed"),
                "owner_email_blocked_reason": payload.get("owner_email_blocked_reason"),
                "primary_blocker": {
                    "blocker": primary.get("blocker") or primary.get("class"),
                    "artifact": primary.get("artifact"),
                    "hold": primary.get("hold"),
                    "next_action": primary.get("next_action"),
                },
            }
            result["summary"] = summary
            result["stdout_tail"] = json.dumps(summary, sort_keys=True)
    current_report = read_json(report)
    current_report_valid = monthly_readiness_payload_valid(current_report)
    stdout_payload_valid = monthly_readiness_payload_valid(payload)
    if not current_report_valid and stdout_payload_valid:
        report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        current_report_valid = True
        result["repaired_report_from_stdout"] = True
    result["report_valid"] = current_report_valid
    if not current_report_valid:
        result["invalid_report_reason"] = "monthly_readiness_report_missing_required_fields"
    result["ok"] = bool(result.get("ok")) and report.is_file() and current_report_valid
    return result


def monthly_readiness_payload_valid(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if str(payload.get("status") or "").strip() not in {"ok", "review", "failed"}:
        return False
    return "blocker_count" in payload and "owner_email_allowed" in payload


def refresh_no_mortgage_financials_guard() -> dict:
    script = ROOT / "scripts" / "baselane_no_mortgage_financials_guard.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    report_path = current_report_path(BASELANE_NO_MORTGAGE_FINANCIALS_GUARD_REPORT)
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--report",
            str(report_path),
        ],
        timeout_env="BASELANE_EOD_NO_MORTGAGE_FINANCIALS_GUARD_TIMEOUT_SEC",
        ok_return_codes={0, 2},
    )
    result["ok"] = bool(result.get("ok")) and report_path.is_file()
    return result


def refresh_report_integrity_guard() -> dict:
    if os.environ.get("BASELANE_EOD_SKIP_REPORT_INTEGRITY_GUARD") == "1":
        return {"attempted": False, "reason": "disabled"}
    script = ROOT / "scripts" / "baselane_report_integrity_guard.py"
    if not script.is_file():
        return {"attempted": False, "reason": "missing_script", "script": str(script)}
    report_path = current_report_path(BASELANE_REPORT_INTEGRITY_GUARD_REPORT)
    result = run_refresh_command(
        [
            sys.executable,
            str(script),
            "--root",
            str(ROOT),
            "--report-dir",
            str(REPORT_DIR),
            "--report",
            str(report_path),
        ],
        timeout_env="BASELANE_EOD_REPORT_INTEGRITY_GUARD_TIMEOUT_SEC",
        default_timeout=20,
        ok_return_codes={0, 2},
        include_stdout=True,
    )
    result.pop("stdout", "")
    payload = read_json(report_path)
    if payload.get("status") not in {"missing", "unreadable"}:
        summary = {
            "status": payload.get("status"),
            "issue_count": compact_count(payload.get("issue_count")),
            "report_count": len(payload.get("reports") or {}),
            "issue_reports": sorted(
                name
                for name, report in (payload.get("reports") or {}).items()
                if isinstance(report, dict) and compact_count(report.get("issue_count")) > 0
            ),
        }
        result["summary"] = summary
        result["stdout_tail"] = json.dumps(summary, sort_keys=True)
    result["guard_status"] = payload.get("status")
    result["issue_count"] = compact_count(payload.get("issue_count"))
    result["ok"] = bool(result.get("ok")) and report_path.is_file() and payload.get("status") == "ok"
    return recover_report_integrity_timeout(result, report_path, payload)


def backfill_monthly_run_gate_fields(
    monthly_path: Path | None = None,
    readiness_path: Path | None = None,
    owner_gate_path: Path | None = None,
    owner_email_send_guard_path: Path | None = None,
    monthly_statements_gate_path: Path | None = None,
    owner_email_packet_path: Path | None = None,
    pipeline_candidate_coverage_path: Path | None = None,
    source_cash_reconciliation_actions_path: Path | None = None,
    transfer_reconciliation_path: Path | None = None,
    quitman_804_cash_alignment_path: Path | None = None,
) -> dict:
    explicit_monthly_path = monthly_path is not None
    monthly_path = monthly_path or named_report_path("baselane_financials_monthly_run_report.json")
    readiness_path = readiness_path or named_report_path("baselane_financials_monthly_readiness.json")
    owner_gate_path = owner_gate_path or named_report_path("baselane_monthly_owner_review_gate.json")
    owner_email_send_guard_path = owner_email_send_guard_path or current_report_path(OWNER_EMAIL_SEND_GUARD_REPORT)
    monthly_statements_gate_path = monthly_statements_gate_path or named_report_path("baselane_monthly_statements_idempotent_report.json")
    pipeline_candidate_coverage_path = pipeline_candidate_coverage_path or (
        monthly_path.parent / "baselane_monthly_pipeline_candidate_coverage_audit.json"
        if explicit_monthly_path
        else named_report_path("baselane_monthly_pipeline_candidate_coverage_audit.json")
    )
    source_cash_reconciliation_actions_path = source_cash_reconciliation_actions_path or (
        monthly_path.parent / "baselane_source_cash_reconciliation_actions.json"
        if explicit_monthly_path
        else named_report_path("baselane_source_cash_reconciliation_actions.json")
    )
    transfer_reconciliation_path = transfer_reconciliation_path or (
        monthly_path.parent / "baselane_monthly_transfer_reconciliation_report.json"
        if explicit_monthly_path
        else named_report_path("baselane_monthly_transfer_reconciliation_report.json")
    )
    quitman_804_cash_alignment_path = quitman_804_cash_alignment_path or (
        monthly_path.parent / "baselane_804_quitman_cash_alignment_decision_validation.json"
        if explicit_monthly_path
        else named_report_path("baselane_804_quitman_cash_alignment_decision_validation.json")
    )
    if owner_email_packet_path is None:
        owner_email_packet_path = (
            monthly_path.parent / OWNER_EMAIL_PACKET_REPORT.name
            if explicit_monthly_path
            else current_report_path(OWNER_EMAIL_PACKET_REPORT)
        )
    monthly = read_json(monthly_path)
    readiness = read_json(readiness_path)
    owner_gate = read_json(owner_gate_path)
    owner_email_send_guard = read_json(owner_email_send_guard_path)
    monthly_statements_gate = read_json(monthly_statements_gate_path)
    owner_email_packet = read_json(owner_email_packet_path)
    pipeline_candidate_coverage = read_json(pipeline_candidate_coverage_path)
    source_cash_reconciliation_actions = read_json(source_cash_reconciliation_actions_path)
    transfer_reconciliation = read_json(transfer_reconciliation_path)
    quitman_804_cash_alignment = read_json(quitman_804_cash_alignment_path)
    if monthly.get("status") in {"missing", "unreadable"}:
        return {"attempted": False, "reason": monthly.get("status"), "path": str(monthly_path)}
    if readiness.get("status") in {"missing", "unreadable"}:
        return {"attempted": False, "reason": f"readiness_{readiness.get('status')}", "path": str(readiness_path)}

    actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
    primary = readiness.get("primary_blocker") if isinstance(readiness.get("primary_blocker"), dict) else {}
    if not primary:
        primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
    desired = {
        "monthly_readiness_owner_email_allowed": readiness.get("owner_email_allowed") is True,
        "monthly_readiness_blocker_count": int(readiness.get("blocker_count") or 0),
        "monthly_readiness_actionable_blocker_count": int(actionable.get("actionable_blocker_count") or 0),
        "monthly_readiness_primary_blocker": primary.get("blocker") or primary.get("class"),
    }
    if owner_gate.get("status") not in {"missing", "unreadable"}:
        desired["monthly_owner_review_gate_status"] = owner_gate.get("status")
        desired["monthly_owner_review_gate_blocker_count"] = int(owner_gate.get("blocker_count") or 0)
    if owner_email_send_guard.get("status") not in {"missing", "unreadable"}:
        desired["owner_email_send_guard_status"] = owner_email_send_guard.get("status")
        desired["owner_email_send_guard_issue_count"] = int(owner_email_send_guard.get("issue_count") or 0)
        desired["owner_email_send_guard_send_allowed"] = owner_email_send_guard.get("send_allowed") is True
        desired["owner_email_send_guard_safe_block"] = owner_email_send_guard.get("safe_block") is True
        desired["owner_email_send_guard_no_spam_ok"] = owner_email_send_guard.get("no_spam_guard_ok") is True
        desired["owner_email_send_guard_send_lock_file_unreadable"] = owner_email_send_guard.get("send_lock_file_unreadable") is True
        desired["effective_send_owner_emails"] = owner_email_send_guard.get("effective_send_owner_emails") is True
    if owner_email_packet.get("status") not in {"missing", "unreadable"}:
        desired["owner_email_packet_status"] = owner_email_packet.get("status")
        desired["owner_email_packet_issue_count"] = int(owner_email_packet.get("issue_count") or 0)
        desired["owner_email_packet_property_unavailable_count"] = int(owner_email_packet.get("property_unavailable_count") or 0)
        desired["owner_email_packet_monthly_financial_summary_missing_property_count"] = int(
            owner_email_packet.get("monthly_financial_summary_missing_property_count") or 0
        )
        desired["owner_email_packet_monthly_financial_summary_present_total_property_count"] = int(
            owner_email_packet.get("monthly_financial_summary_present_total_property_count") or 0
        )
        desired["owner_email_packet_property_unavailable_candidate_update_source_count"] = int(
            owner_email_packet.get("property_unavailable_candidate_update_source_count") or 0
        )
        desired["owner_email_packet_property_unavailable_candidate_update_approval_target_count"] = int(
            owner_email_packet.get("property_unavailable_candidate_update_approval_target_count") or 0
        )
        desired["owner_email_packet_property_unavailable_candidate_financial_approval_target_count"] = int(
            owner_email_packet.get("property_unavailable_candidate_financial_approval_target_count") or 0
        )
        if isinstance(owner_email_packet.get("property_unavailable_reason_counts"), dict):
            desired["owner_email_packet_property_unavailable_reason_counts"] = owner_email_packet.get("property_unavailable_reason_counts")
        if owner_email_packet.get("property_gap_csv"):
            desired["owner_email_packet_property_gap_csv"] = owner_email_packet.get("property_gap_csv")
        desired["owner_email_packet_recipient_count"] = int(owner_email_packet.get("recipient_count") or 0)
        desired["owner_email_packet_packet_count"] = int(owner_email_packet.get("packet_count") or 0)
        desired["owner_email_packet_full_history_leak_count"] = int(owner_email_packet.get("full_history_leak_count") or 0)
        desired["owner_email_packet_body_guard_issue_count"] = int(owner_email_packet.get("body_guard_issue_count") or 0)
        desired["owner_email_packet_send_result_count"] = int(owner_email_packet.get("send_result_count") or 0)
        desired["owner_email_packet_send_failed_count"] = int(owner_email_packet.get("send_failed_count") or 0)
        desired["owner_email_packet_safe_to_send_now"] = owner_email_packet.get("safe_to_send_now") is True
        desired["owner_email_packet_already_sent_for_run_month"] = owner_email_packet.get("already_sent_for_run_month") is True
        desired["owner_email_packet_max_once_monthly_ok"] = owner_email_packet.get("max_once_monthly_ok") is True
        desired["owner_email_packet_sent_state_written"] = owner_email_packet.get("sent_state_written") is True
        if owner_email_packet.get("recipient_template_csv"):
            desired["owner_email_packet_recipient_template_csv"] = owner_email_packet.get("recipient_template_csv")
    if monthly_statements_gate.get("status") not in {"missing", "unreadable"}:
        desired["monthly_statements_gate_status"] = monthly_statements_gate.get("status")
        desired["monthly_statements_gate_reason"] = monthly_statements_gate.get("reason")
        desired["monthly_statements_gate_action"] = monthly_statements_gate.get("action")
        desired["monthly_statements_gate_generated_at"] = monthly_statements_gate.get("generated_at")
        desired["monthly_statements_target_year"] = monthly_statements_gate.get("target_year")
        desired["monthly_statements_target_month"] = monthly_statements_gate.get("target_month")
        desired["monthly_statements_captured_unique_count"] = int(monthly_statements_gate.get("captured_unique_count") or 0)
        desired["monthly_statements_min_captured_required"] = int(monthly_statements_gate.get("min_captured_required") or 0)
        desired["monthly_statements_download_ok"] = monthly_statements_gate.get("download_ok") is True
        desired["monthly_statements_download_new_files_count"] = monthly_statements_gate.get("download_new_files_count")
        desired["monthly_statements_download_error_class"] = monthly_statements_gate.get("download_error_class")
        desired["monthly_statements_operator_status"] = monthly_statements_gate.get("operator_status")
        desired["monthly_statements_operator_issue_count"] = int(monthly_statements_gate.get("operator_issue_count") or 0)
        desired["monthly_statements_auth_recovery_status"] = monthly_statements_gate.get("auth_recovery_status")
        desired["monthly_statements_auth_recovery_attempted"] = monthly_statements_gate.get("auth_recovery_attempted") is True
        desired["monthly_statements_auth_recovery_manual_auth_required"] = (
            monthly_statements_gate.get("auth_recovery_manual_auth_required") is True
        )
    if pipeline_candidate_coverage.get("status") not in {"missing", "unreadable"}:
        desired["pipeline_candidate_coverage_status"] = pipeline_candidate_coverage.get("status")
        desired["pipeline_candidate_coverage_mismatch_count"] = int(pipeline_candidate_coverage.get("mismatch_count") or 0)
    if source_cash_reconciliation_actions.get("status") not in {"missing", "unreadable"}:
        desired["source_cash_reconciliation_action_status"] = source_cash_reconciliation_actions.get("status")
        desired["source_cash_reconciliation_action_count"] = int(source_cash_reconciliation_actions.get("action_count") or 0)
        desired["source_cash_reconciliation_active_monthly_candidate_action_count"] = int(
            source_cash_reconciliation_actions.get("active_monthly_candidate_action_count") or 0
        )
        desired["source_cash_reconciliation_active_monthly_candidate_source_cash_mismatch_count"] = int(
            source_cash_reconciliation_actions.get("active_monthly_candidate_source_cash_mismatch_count") or 0
        )
    if transfer_reconciliation.get("status") not in {"missing", "unreadable"}:
        desired["transfer_reconciliation_status"] = transfer_reconciliation.get("status")
        desired["transfer_reconciliation_ready_count"] = int(transfer_reconciliation.get("ready_to_send_property_count") or 0)
        desired["transfer_reconciliation_held_count"] = int(transfer_reconciliation.get("held_property_count") or 0)
        desired["transfer_reconciliation_recommended_total"] = transfer_reconciliation.get("recommended_send_to_lofty_total")
        desired["transfer_reconciliation_recommended_total_is_final"] = (
            transfer_reconciliation.get("recommended_send_to_lofty_total_is_final") is True
        )
        desired["transfer_reconciliation_source_blocker_count"] = int(transfer_reconciliation.get("source_blocker_count") or 0)
        desired["transfer_reconciliation_required_source_blocker_count"] = int(
            transfer_reconciliation.get("required_source_blocker_count") or 0
        )
        desired["transfer_reconciliation_missing_bank_action_count"] = int(transfer_reconciliation.get("missing_bank_action_count") or 0)
        desired["transfer_reconciliation_missing_lofty_reserve_count"] = int(
            transfer_reconciliation.get("missing_lofty_reserve_count") or 0
        )
    if quitman_804_cash_alignment.get("status") not in {"missing", "unreadable"}:
        desired["quitman_804_cash_alignment_status"] = quitman_804_cash_alignment.get("status")
        desired["quitman_804_cash_alignment_active_transfer_blocking_status"] = (
            "ok"
            if transfer_reconciliation.get("status") == "ok"
            and int(transfer_reconciliation.get("property_cash_review_blocked_property_count") or 0) == 0
            else quitman_804_cash_alignment.get("status")
        )
        desired["quitman_804_cash_alignment_source_clean_status"] = quitman_804_cash_alignment.get("source_clean_status")
        desired["quitman_804_cash_alignment_reviewed_decision_full_clearance"] = (
            quitman_804_cash_alignment.get("reviewed_decision_full_clearance") is True
        )
        desired["quitman_804_cash_alignment_review_count"] = int(quitman_804_cash_alignment.get("effective_issue_count") or 0)
    if desired["monthly_readiness_owner_email_allowed"] is False:
        desired["monthly_readiness_owner_email_blocked_reason"] = readiness.get("owner_email_blocked_reason") or monthly_readiness_blocked_reason(readiness)
        desired["effective_send_owner_emails"] = False
    def report_status(report: dict) -> str | None:
        status = str(report.get("status") or "").strip()
        if not status or status in {"missing", "unreadable"}:
            return None
        return status

    steps = dict(monthly.get("steps") if isinstance(monthly.get("steps"), dict) else {})
    quitman_step_status = report_status(quitman_804_cash_alignment)
    if (
        quitman_step_status
        and transfer_reconciliation.get("status") == "ok"
        and int(transfer_reconciliation.get("property_cash_review_blocked_property_count") or 0) == 0
    ):
        quitman_step_status = "ok"
    current_step_statuses = {
        "monthly_readiness": report_status(readiness),
        "monthly_owner_review_gate": report_status(owner_gate),
        "owner_email_send_guard": report_status(owner_email_send_guard),
        "pipeline_candidate_coverage": report_status(pipeline_candidate_coverage),
        "source_cash_reconciliation_actions": report_status(source_cash_reconciliation_actions),
        "transfer_reconciliation": report_status(transfer_reconciliation),
        "quitman_804_cash_alignment": quitman_step_status,
    }
    for step_name, current_step_status in current_step_statuses.items():
        if current_step_status is not None:
            steps[step_name] = current_step_status
    blocking_step_statuses = {
        str(name): status
        for name, status in steps.items()
        if str(status or "").startswith(("failed", "review"))
    }
    if readiness.get("status") == "review" and "monthly_readiness" not in blocking_step_statuses:
        blocking_step_statuses["monthly_readiness"] = "review"
    reported_status = monthly.get("reported_status") or monthly.get("status") or "unknown"
    reported_return_code = monthly.get("reported_return_code", monthly.get("return_code"))
    reported_failed_step = monthly.get("reported_failed_step", monthly.get("failed_step"))
    current_status = monthly.get("status") or "unknown"
    statements_failure_recovered = (
        reported_status == "failed"
        and reported_failed_step == "baselane_monthly_statements_idempotent"
        and monthly_statements_gate.get("status") == "ok"
    )
    if statements_failure_recovered:
        desired["monthly_statements_recovery_status"] = "recovered_by_current_statement_gate"
        desired["monthly_statements_recovered_by"] = "baselane_monthly_statements_idempotent_report"
        desired["monthly_statements_recovery_report"] = str(monthly_statements_gate_path)
    current_failed_step = monthly.get("failed_step")
    stale_failed_step_recovered = (
        current_status == "review"
        and current_failed_step
        and str(current_failed_step) not in blocking_step_statuses
        and not blocking_step_statuses
    )
    stale_failed_step_replaced = (
        current_status == "review"
        and current_failed_step
        and str(current_failed_step) not in blocking_step_statuses
        and bool(blocking_step_statuses)
    )
    if (
        current_status == "ok"
        or statements_failure_recovered
        or stale_failed_step_recovered
        or stale_failed_step_replaced
    ) and blocking_step_statuses:
        desired["effective_status"] = "review"
        desired["effective_return_code"] = 2
        desired["effective_failed_step"] = next(iter(blocking_step_statuses))
    elif statements_failure_recovered or stale_failed_step_recovered:
        desired["effective_status"] = "ok"
        desired["effective_return_code"] = 0
        desired["effective_failed_step"] = None
    else:
        desired["effective_status"] = current_status
        desired["effective_return_code"] = int(monthly.get("return_code") or 0)
        desired["effective_failed_step"] = monthly.get("failed_step")
    desired["reported_status"] = reported_status
    desired["reported_return_code"] = reported_return_code
    desired["reported_failed_step"] = reported_failed_step
    desired["status"] = desired["effective_status"]
    desired["return_code"] = desired["effective_return_code"]
    desired["failed_step"] = desired["effective_failed_step"]
    desired["steps"] = steps
    desired["review_step_names"] = sorted(blocking_step_statuses)
    desired["blocking_step_statuses"] = blocking_step_statuses

    changed = {}
    updated = dict(monthly)
    for key, value in desired.items():
        if updated.get(key) != value:
            updated[key] = value
            changed[key] = value
    if changed:
        monthly_path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "attempted": True,
        "ok": True,
        "changed": bool(changed),
        "changed_fields": sorted(changed),
        "monthly_path": str(monthly_path),
        "readiness_path": str(readiness_path),
        "owner_gate_path": str(owner_gate_path),
        "owner_email_send_guard_path": str(owner_email_send_guard_path),
        "pipeline_candidate_coverage_path": str(pipeline_candidate_coverage_path),
        "source_cash_reconciliation_actions_path": str(source_cash_reconciliation_actions_path),
        "transfer_reconciliation_path": str(transfer_reconciliation_path),
        "quitman_804_cash_alignment_path": str(quitman_804_cash_alignment_path),
        "owner_email_packet_path": str(owner_email_packet_path),
        "owner_email_allowed": desired["monthly_readiness_owner_email_allowed"],
        "blocker_count": desired["monthly_readiness_blocker_count"],
        "owner_review_gate_status": desired.get("monthly_owner_review_gate_status"),
        "owner_review_gate_blocker_count": desired.get("monthly_owner_review_gate_blocker_count"),
        "owner_email_send_guard_status": desired.get("owner_email_send_guard_status"),
        "owner_email_send_guard_issue_count": desired.get("owner_email_send_guard_issue_count"),
        "owner_email_packet_status": desired.get("owner_email_packet_status"),
        "owner_email_packet_issue_count": desired.get("owner_email_packet_issue_count"),
        "owner_email_packet_property_unavailable_count": desired.get("owner_email_packet_property_unavailable_count"),
        "owner_email_packet_monthly_financial_summary_missing_property_count": desired.get(
            "owner_email_packet_monthly_financial_summary_missing_property_count"
        ),
        "owner_email_packet_monthly_financial_summary_present_total_property_count": desired.get(
            "owner_email_packet_monthly_financial_summary_present_total_property_count"
        ),
        "owner_email_packet_property_unavailable_candidate_update_source_count": desired.get(
            "owner_email_packet_property_unavailable_candidate_update_source_count"
        ),
        "owner_email_packet_property_unavailable_candidate_update_approval_target_count": desired.get(
            "owner_email_packet_property_unavailable_candidate_update_approval_target_count"
        ),
        "owner_email_packet_property_unavailable_candidate_financial_approval_target_count": desired.get(
            "owner_email_packet_property_unavailable_candidate_financial_approval_target_count"
        ),
        "owner_email_packet_property_unavailable_reason_counts": desired.get("owner_email_packet_property_unavailable_reason_counts"),
        "owner_email_packet_property_gap_csv": desired.get("owner_email_packet_property_gap_csv"),
        "owner_email_packet_full_history_leak_count": desired.get("owner_email_packet_full_history_leak_count"),
        "owner_email_packet_body_guard_issue_count": desired.get("owner_email_packet_body_guard_issue_count"),
        "owner_email_packet_send_result_count": desired.get("owner_email_packet_send_result_count"),
        "owner_email_packet_send_failed_count": desired.get("owner_email_packet_send_failed_count"),
    }


def refresh_health_summary(report: dict) -> dict:
    timed_out = {}
    recovered_timeouts = {}
    attempted_failure = {}
    for key, value in report.items():
        if not (
            key.endswith("_refresh")
            or key in {"weekly_report_reconcile", "monthly_run_gate_backfill"}
        ):
            continue
        if not isinstance(value, dict):
            continue
        if value.get("timed_out") is True and value.get("timeout_recovered_by_report") is True and value.get("ok") is True:
            recovered_timeouts[key] = {
                "timeout_seconds": value.get("timeout_seconds"),
                "report_path": value.get("timeout_report_path"),
                "report_age_hours": value.get("timeout_report_age_hours"),
            }
            continue
        if value.get("timed_out") is True:
            timed_out[key] = {
                "timeout_seconds": value.get("timeout_seconds"),
                "return_code": value.get("return_code"),
            }
            continue
        return_code = value.get("return_code")
        if (
            value.get("attempted") is not False
            and value.get("ok") is False
            and value.get("report_valid") is not True
            and value.get("timed_out") is not False
            and (return_code not in {None, 0, 2, "0", "2"} or value.get("status") == "failed")
        ):
            attempted_failure[key] = {
                "return_code": return_code,
                "reason": value.get("reason"),
            }
    issue_count = len(timed_out) + len(attempted_failure)
    return {
        "status": "review" if issue_count else "ok",
        "issue_count": issue_count,
        "timeout_count": len(timed_out),
        "recovered_timeout_count": len(recovered_timeouts),
        "attempted_failure_count": len(attempted_failure),
        "timed_out_refreshes": timed_out,
        "recovered_timed_out_refreshes": recovered_timeouts,
        "attempted_failed_refreshes": attempted_failure,
    }


def apply_refresh_health(report: dict) -> dict:
    summary = refresh_health_summary(report)
    report["refresh_health"] = summary
    report["refresh_health_status"] = summary["status"]
    report["refresh_health_issue_count"] = summary["issue_count"]
    report["refresh_timeout_count"] = summary["timeout_count"]
    report["refresh_recovered_timeout_count"] = summary["recovered_timeout_count"]
    report["refresh_attempted_failure_count"] = summary["attempted_failure_count"]
    if summary["status"] != "ok" and report.get("effective_status") in {None, "", "ok"}:
        report["effective_status"] = "review"
        report["effective_failed_step"] = "eod_refresh_health"
    elif report.get("effective_status") in {None, ""}:
        report["effective_status"] = report.get("status")
        report["effective_failed_step"] = report.get("failed_step")
    return summary


def build_message(*, send_requested: bool = False, refresh_health: dict | None = None) -> str:
    daily = read_json(current_report_path(BASELANE_DAILY_SYNC_REPORT))
    daily_run = read_json(named_report_path("baselane_daily_run_report.json"))
    if daily.get("status") in {"missing", "unreadable"}:
        daily = daily_run
    daily_disk_preflight = read_json(current_report_path(BASELANE_DAILY_DISK_SPACE_PREFLIGHT_REPORT))
    daily_source_cash_balance = read_json(current_report_path(BASELANE_DAILY_SOURCE_CASH_BALANCE_REPORT))
    sync = read_json(named_report_path("baselane_sync_cdp_report.json"))
    login_wait = read_json(named_report_path("baselane_login_wait_report.json"))
    baselane_auth_recovery = read_json(current_report_path(BASELANE_CDP_AUTH_RECOVERY_REPORT))
    guard = read_json(named_report_path("baselane_export_guard_last.json"))
    weekly_file_updates = read_json(named_report_path("baselane_weekly_file_updates_run_report.json"))
    ecogl_safe_apply = read_json(named_report_path("baselane_ecogl_safe_category_apply_report.json"))
    weekly_raw_duplicates = read_json(named_report_path("baselane_weekly_raw_duplicate_report.json"))
    weekly_unprocessed = read_json(named_report_path("baselane_weekly_unprocessed_report.json"))
    weekly_cf_sync = read_json(named_report_path("baselane_weekly_cf_statement_sync_report.json"))
    cf_balance_sheet_consistency = read_json(named_report_path("baselane_cf_balance_sheet_consistency_audit.json"))
    yhome_operating_cash_apply_verify = read_json(named_report_path("yhome_operating_cash_apply_verify_report.json"))
    lofty_transfer_requirements = read_json(current_report_path(BASELANE_LOFTY_TRANSFER_REQUIREMENTS_REPORT))
    cf_balance_sheet_cash_apply = read_json(current_report_path(BASELANE_CF_BALANCE_SHEET_CASH_APPLY_REPORT))
    weekly_cf_gate = read_json(named_report_path("baselane_weekly_cf_review_gate.json"))
    ecogl_autonomy = read_json(named_report_path("baselane_ecogl_data_quality_autonomy.json"))
    ecogl_source_fix = read_json(named_report_path("baselane_ecogl_source_fix_plan.json"))
    ecogl_source_fix_verifier = read_json(named_report_path("baselane_ecogl_source_fix_verifier.json"))
    ecogl_source_fix_corrections = read_json(named_report_path("baselane_ecogl_source_fix_corrections.json"))
    ecogl_source_fix_approval = read_json(named_report_path("baselane_ecogl_source_fix_approval.json"))
    ecogl_source_fix_correction_validation = read_json(named_report_path("baselane_ecogl_source_fix_correction_validation.json"))
    ecogl_source_fix_action_queue = read_json(current_report_path(BASELANE_SOURCE_FIX_ACTION_QUEUE_REPORT))
    ecogl_source_fix_apply = read_json(current_report_path(BASELANE_SOURCE_FIX_APPLY_REPORT))
    native_split_plan = read_json(latest_native_split_plan_report_path())
    native_split_apply = read_json(latest_native_split_apply_report_path())
    first_day_pm_fee_cleanup = read_json(named_report_path("baselane_first_day_pm_fee_source_cleanup_plan.json"))
    source_cleanup_queue = read_json(named_report_path("baselane_source_cleanup_queue.json"))
    assetrail_push = read_json(named_report_path("baselane_assetrail_push_report.json"))
    scheduler_audit = read_json(current_report_path(BASELANE_SCHEDULER_AUDIT_REPORT))
    eod_telegram_report = read_json(current_report_path(OUT_REPORT))
    preflight = read_json(current_report_path(BASELANE_LOCAL_MODEL_PREFLIGHT_REPORT))
    monthly = read_json(named_report_path("baselane_financials_monthly_run_report.json"))
    monthly_recovery = read_json(current_report_path(BASELANE_MONTHLY_RECOVERY_REPORT))
    monthly_run_month = str((monthly or {}).get("run_month") or "").strip()
    explicit_run_month = str(os.environ.get("RUN_MONTH") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", monthly_run_month):
        monthly_run_month = ""
    if not re.fullmatch(r"\d{4}-\d{2}", explicit_run_month):
        explicit_run_month = ""
    eod_run_month = explicit_run_month or monthly_run_month or current_run_month()
    cf_no_gl_property_match = read_json(REPORT_DIR / "cf_statement_sync" / f"no_gl_property_match_{eod_run_month}.json")
    monthly_statements_gate = read_json(named_report_path("baselane_monthly_statements_idempotent_report.json"))
    monthly_statements_download = read_json(named_report_path("baselane_statements_download_report.json"))
    bootstrap = read_json(named_report_path("baselane_financials_monthly_doc_bootstrap.json"))
    lofty_cdp_ensure = read_json(current_report_path(LOFTY_CDP_ENSURE_REPORT))
    lofty_cdp_preflight = read_json(current_report_path(LOFTY_CDP_PREFLIGHT_REPORT))
    public_path_guard = read_json(current_report_path(LOFTY_PUBLIC_PATH_GUARD_REPORT))
    discord_public_guard = read_json(current_report_path(DISCORD_PUBLIC_FINANCIAL_SOURCE_GUARD_REPORT))
    live_capture = read_json(named_report_path("baselane_financials_monthly_live_update_capture.json"))
    live_financial_capture = read_json(named_report_path("baselane_financials_monthly_live_financial_capture.json"))
    guard_audit = read_json(named_report_path("baselane_financials_monthly_guard_audit.json"))
    guarded_apply = read_json(named_report_path("baselane_financials_monthly_guarded_apply.json"))
    review_manifest = read_json(named_report_path("baselane_financials_monthly_review_manifest.json"))
    owner_review_gate = read_json(named_report_path("baselane_monthly_owner_review_gate.json"))
    review_candidate_packet = read_json(named_report_path("baselane_financials_monthly_review_candidate_packet.json"))
    review_safety_scan = read_json(named_report_path("baselane_financials_monthly_review_safety_scan.json"))
    safe_candidate_approval = read_json(named_report_path("baselane_financials_monthly_safe_candidate_approval.json"))
    readiness = read_json(named_report_path("baselane_financials_monthly_readiness.json"))
    monthly_close_status = read_json(current_report_path(MONTHLY_CLOSE_STATUS_REPORT))
    lofty_pm_publish = read_json(named_report_path("baselane_financials_monthly_lofty_pm_publish.json"))
    lofty_listing_cleanup_queue = read_json(current_report_path(LOFTY_LISTING_CLEANUP_QUEUE_REPORT))
    lofty_listing_cleanup_dry_run_verify = read_json(current_report_path(LOFTY_LISTING_CLEANUP_DRY_RUN_VERIFY_REPORT))
    lofty_financial_patch_readiness = read_json(current_report_path(LOFTY_FINANCIAL_PATCH_READINESS_REPORT))
    listing_cleanup_summary = lofty_listing_cleanup_queue_summary(
        lofty_listing_cleanup_queue,
        lofty_listing_cleanup_dry_run_verify,
    )
    listing_cleanup_ready_count = compact_count(listing_cleanup_summary.get("ready_listing_cleanup_count"))
    listing_cleanup_issue_count = compact_count(listing_cleanup_summary.get("issue_count"))
    hemlane_cdp_preflight = read_json(current_report_path(HEMLANE_CDP_PREFLIGHT_REPORT))
    hemlane_rent_roll_capture_report = hemlane_capture_report_path(eod_run_month)
    hemlane_capture_report = read_json(hemlane_rent_roll_capture_report)
    tenant_ledger_guard = read_json(current_report_path(LOFTY_TENANT_LEDGER_GUARD_REPORT))
    no_mortgage_guard = read_json(current_report_path(BASELANE_NO_MORTGAGE_FINANCIALS_GUARD_REPORT))
    goal_audit = read_json(current_report_path(BASELANE_GOAL_AUDIT_REPORT))
    report_integrity_guard = read_json(current_report_path(BASELANE_REPORT_INTEGRITY_GUARD_REPORT))
    operations_packet = read_json(named_report_path("baselane_financials_operations_packet.json"))
    if operations_packet.get("status") not in {"missing", "unreadable"}:
        operations_packet = {
            **operations_packet,
            "action_item_count": len(operations_packet.get("action_items") or []),
            "monthly_blocker_count": (operations_packet.get("monthly_readiness") or {}).get("blocker_count"),
            "pending_update_review_count": (operations_packet.get("monthly_review") or {}).get("pending_update_review_count"),
            "pending_financial_review_count": (operations_packet.get("monthly_review") or {}).get("pending_financial_review_count"),
            "weekly_cf_conflict_count": (operations_packet.get("weekly_cf") or {}).get("conflict_count"),
            "weekly_cf_untagged_required_count": (operations_packet.get("weekly_cf") or {}).get("untagged_review_required_count"),
            "owner_email_allowed": (operations_packet.get("monthly_readiness") or {}).get("owner_email_allowed"),
        }

    daily_job = scheduler_job(scheduler_audit, "daily_sync")
    daily_run_sync_report_status = str(daily.get("sync_report_status") or "").strip()
    sync_effective_status = str(sync.get("effective_status") or sync.get("status") or "").strip()
    sync_report_sync_status = str(sync.get("sync_report_status") or sync.get("status") or "").strip()
    daily_sync_report_status = str(daily_run_sync_report_status or sync_report_sync_status or "").strip()
    daily_effective_status = str(daily.get("effective_status") or daily.get("status") or "").strip()
    daily_effective_failed_step = str(daily.get("effective_failed_step") or daily.get("failed_step") or "").strip()
    daily_report_reconciled_ok = daily.get("status") == "ok" and not (daily.get("issues") or [])
    sync_report_effective_ok = (
        sync_effective_status == "ok"
        and sync_report_sync_status == "ok"
        and zero_or_empty(first_present(sync.get("effective_return_code"), sync.get("return_code")))
        and str(sync.get("effective_failed_step") or sync.get("failed_step") or "").strip() in {"", "none"}
    )
    daily_effective_sync_ok = (
        daily_effective_status == "ok"
        and (daily_sync_report_status == "ok" or sync_report_effective_ok)
        and zero_or_empty(first_present(daily.get("effective_return_code"), daily.get("return_code")))
        and daily_effective_failed_step in {"", "none"}
    )
    stale_daily_scheduler_issue_prefixes = (
        "unexpected_report_status:",
        "report_unexpected_value:return_code=",
        "report_unexpected_value:sync_report_status=",
    )
    if daily_report_reconciled_ok and daily_job.get("issues"):
        daily_job = {**daily_job, "issues": []}
    elif daily_effective_sync_ok and daily_job.get("issues"):
        filtered_daily_job_issues = [
            str(issue)
            for issue in (daily_job.get("issues") or [])
            if not str(issue).startswith(stale_daily_scheduler_issue_prefixes)
        ]
        daily_job = {**daily_job, "issues": filtered_daily_job_issues}
    daily_scheduler_issue = bool(daily_job.get("issues"))
    scheduler_issues = [str(issue) for issue in (scheduler_audit.get("issues") or []) if issue]
    if daily_report_reconciled_ok or daily_effective_sync_ok:
        scheduler_issues = [
            issue
            for issue in scheduler_issues
            if not issue.startswith(tuple(f"daily_sync:{prefix}" for prefix in stale_daily_scheduler_issue_prefixes))
        ]
    canonical_daily_sync_ok = (
        daily_report_reconciled_ok
        and daily_sync_report_status == "ok"
    ) or daily_effective_sync_ok
    daily_ok = canonical_daily_sync_ok and not daily_scheduler_issue
    raw_scheduler_ok = scheduler_audit.get("status") == "ok" or not scheduler_issues
    eod_send_proof_only = (
        (not raw_scheduler_ok)
        and scheduler_only_eod_send_proof_issue(scheduler_issues)
        and not eod_telegram_credentials_missing(eod_telegram_report)
    )
    eod_send_state_actionable = (
        eod_send_proof_only
        and str(eod_telegram_report.get("status") or "").strip() not in {"", "ok", "missing", "unreadable"}
    )
    scheduler_ok = raw_scheduler_ok or eod_send_proof_only
    local_model_ok = local_model_preflight_ok(preflight)
    local_model_status = str(preflight.get("status") or "").strip()
    local_model_explicit_contract = any(
        key in preflight
        for key in (
            "model",
            "provider",
            "model_id",
            "validation_digest",
            "direct_smoke",
            "validation_contract",
            "configured_model_present",
        )
    )
    local_model_blocker_active = (
        not local_model_ok
        and local_model_status not in {"", "missing", "unreadable"}
        and (local_model_status != "ok" or local_model_explicit_contract)
    )
    daily_issue_text = [str(issue) for issue in (daily.get("issues") or []) if issue]
    daily_disk_preflight_status = str(
        daily.get("disk_space_preflight_status") or daily_disk_preflight.get("status") or ""
    ).strip()
    daily_disk_preflight_blocked = (
        daily_disk_preflight_status not in {"", "ok", "missing"}
        or any(issue.startswith("disk_space_preflight=") for issue in daily_issue_text)
    )
    paths_ok = public_path_guard.get("status") == "ok" and int(public_path_guard.get("issue_count") or 0) == 0
    discord_public_financial_sources_ok = discord_public_guard.get("status") == "ok" and int(discord_public_guard.get("issue_count") or 0) == 0
    tenant_ledgers_ok = tenant_ledger_guard.get("status") == "ok" and int(tenant_ledger_guard.get("issue_count") or 0) == 0
    no_mortgage_status = no_mortgage_guard.get("status")
    no_mortgage_remaining = int(no_mortgage_guard.get("remaining_nonzero_count") or 0)
    no_mortgage_financials_ok = no_mortgage_status in {"missing", "unreadable", "ok"} and no_mortgage_remaining == 0
    report_integrity_status = str(report_integrity_guard.get("status") or "").strip()
    report_integrity_issue_codes = {
        str(item.get("code") or "")
        for item in (report_integrity_guard.get("issues") or [])
        if isinstance(item, dict)
    }
    report_integrity_financial_candidate_issue_codes = {
        code
        for code in report_integrity_issue_codes
        if code.startswith("lofty_financial_patch_readiness_empty_candidate_quality_")
        or code.startswith("lofty_financial_patch_readiness_empty_csv_candidate_quality_")
        or code
        in {
            "monthly_candidate_packet_issue_count_nonzero",
            "monthly_candidate_financial_gate_issue_count_nonzero",
        }
    }
    report_integrity_financial_candidate_only = (
        bool(report_integrity_issue_codes)
        and report_integrity_issue_codes <= report_integrity_financial_candidate_issue_codes
    )
    report_integrity_send_proof_issue_codes = {
        "eod_send_state_success_missing_send_request",
        "eod_send_state_success_missing_message",
        "eod_send_state_success_missing_message_digest",
        "eod_send_state_success_message_digest_mismatch",
        "eod_send_state_success_sent_digest_mismatch",
        "eod_send_state_success_missing_source_report_generated_at",
    }
    report_integrity_send_proof_only = (
        bool(report_integrity_issue_codes)
        and report_integrity_issue_codes <= report_integrity_send_proof_issue_codes
    )
    report_integrity_future_cf_issue = (
        "future_cf_statement_values_clear_report.json"
        in {
            name
            for name, report in (report_integrity_guard.get("reports") or {}).items()
            if isinstance(report, dict) and compact_count(report.get("issue_count")) > 0
        }
        or any(code.startswith("future_cf_values_") for code in report_integrity_issue_codes)
    )
    report_integrity_eod_preview_only = integrity_review_is_eod_preview_only(report_integrity_guard)
    report_integrity_ok = (
        report_integrity_status == "ok"
        and compact_count(report_integrity_guard.get("issue_count")) == 0
    ) or report_integrity_send_proof_only or report_integrity_eod_preview_only
    visibility_send_proof_only = (
        not send_requested
        and (eod_send_proof_only or report_integrity_send_proof_only)
    )
    current_statement_gate_ok = monthly_statements_gate.get("status") == "ok"
    monthly_statement_step_failed = (
        not current_statement_gate_ok
        and monthly.get("status") == "failed"
        and monthly.get("failed_step") == "baselane_monthly_statements_idempotent"
    )
    monthly_statement_gate_status = (
        monthly_statements_gate.get("status")
        or monthly.get("monthly_statements_gate_status")
    )
    monthly_statement_expected_wait = monthly_statements_waiting_for_posted_statements(
        monthly_statements_gate,
        monthly,
        monthly_statements_download,
    )
    monthly_statement_gate_failed = monthly_statement_gate_status in {"error", "review"}
    readiness_statement_gate = (
        (readiness.get("operational_gates") or {}).get("monthly_bank_statement_capture")
        if isinstance(readiness.get("operational_gates"), dict)
        else {}
    )
    monthly_statement_evidence_blocked = (
        "operational.monthly_bank_statement_capture.not_ok" in (readiness.get("operational_counts") or {})
        or readiness_statement_gate.get("fresh") is False
        or readiness_statement_gate.get("target_matches_run_month") is False
    )
    goal_ok = goal_audit.get("status") == "ok"
    goal_primary = ((goal_audit.get("actionable_summary") or {}).get("primary_blocker") or {})
    goal_primary_id = str(goal_primary.get("id") or goal_primary.get("requirement") or "").strip()
    goal_primary_blocker = str(goal_primary.get("blocker") or "").strip()
    source_quality_goal_blocker = (
        not goal_ok
        and (
            (goal_audit.get("actionable_summary") or {}).get("source_quality_is_upstream_blocker") is True
            or goal_primary_blocker.startswith("ECO GL source quality")
        )
    )
    report_integrity_deferred_by_source_quality = (
        source_quality_goal_blocker
        and not report_integrity_send_proof_only
        and not report_integrity_future_cf_issue
    )
    goal_primary_eod_visibility = (
        not send_requested
        and not goal_ok
        and (
            goal_primary_id == "eod_telegram_visibility"
            or goal_primary_blocker.startswith(("eod_not_sent_to_telegram", "eod_send_state_"))
        )
    )
    operational_goal_blocker_pending = not goal_ok and not goal_primary_eod_visibility
    readiness_primary = ((readiness.get("actionable_summary") or {}).get("primary_blocker") or {})
    readiness_primary_class = str(readiness_primary.get("class") or readiness_primary.get("blocker") or "")
    monthly_finance_truth_blocked = (
        monthly.get("status") == "failed"
        and monthly.get("failed_step") == "baselane_monthly_finance_truth_refresh"
    ) or monthly.get("monthly_finance_truth_refresh_auth_blocked") is True or (
        readiness.get("status") == "review"
        and readiness_primary_class == "operational.monthly_run.failed"
        and "finance-truth" in str(readiness.get("next_action") or readiness_primary.get("next_action") or "").lower()
    )
    monthly_recovery_marker = monthly_recovery_eod_marker(monthly_recovery)
    title_status = (
        "OK"
        if (
            daily_ok
            and scheduler_ok
            and local_model_ok
            and paths_ok
            and no_mortgage_financials_ok
            and report_integrity_ok
            and goal_ok
            and listing_cleanup_ready_count == 0
            and listing_cleanup_issue_count == 0
            and not visibility_send_proof_only
        )
        else "BLOCKED"
    )
    rent_roll_evidence = {}
    for requirement in goal_audit.get("requirements") or []:
        if requirement.get("id") == "monthly_comms_rent_roll_context":
            rent_roll_evidence = requirement.get("evidence") or {}
            break
    evidence_run_month = (
        infer_run_month_from_text(rent_roll_evidence.get("gap_review"))
        or infer_run_month_from_text(rent_roll_evidence.get("source"))
        or infer_run_month_from_text(rent_roll_evidence.get("rent_roll_source"))
    )
    if not explicit_run_month and not monthly_run_month and evidence_run_month and evidence_run_month != eod_run_month:
        eod_run_month = evidence_run_month
        hemlane_rent_roll_capture_report = hemlane_capture_report_path(eod_run_month)
        hemlane_capture_report = read_json(hemlane_rent_roll_capture_report)
    rent_roll_review_path = compact_path(
        rent_roll_evidence.get("gap_review")
        or f"../workspace-lofty-vp-comms/updates/{eod_run_month}-rent-roll-gap-review.md"
    )
    rent_roll_queue_path = rent_roll_review_path
    if rent_roll_queue_path.endswith(".json") or rent_roll_queue_path.endswith(".md"):
        rent_roll_queue_path = rent_roll_queue_path.rsplit(".", 1)[0] + ".csv"
    rent_roll_needs_source = bool(
        rent_roll_evidence
        and (
            rent_roll_evidence.get("rent_roll_freshness_status") not in {None, "", "current"}
            or bool(rent_roll_evidence.get("rent_roll_stale_export_dates") or [])
            or compact_count(rent_roll_evidence.get("rent_roll_matched_count")) == 0
        )
    )
    owner_gate_summary = owner_review_gate.get("summary") or {}
    owner_review_count = compact_count(owner_review_gate.get("property_review_count") or owner_gate_summary.get("property_review_count"))
    owner_update_pending = compact_count(
        owner_gate_summary.get("pending_update_review_count")
        if "pending_update_review_count" in owner_gate_summary
        else review_manifest.get("pending_update_review_count")
    )
    owner_financial_pending = compact_count(
        owner_gate_summary.get("pending_financial_review_count")
        if "pending_financial_review_count" in owner_gate_summary
        else review_manifest.get("pending_financial_review_count")
    )
    owner_exclusion = monthly_owner_exclusion_summary(
        owner_review_gate=owner_review_gate,
        lofty_pm_publish=lofty_pm_publish,
        live_capture=live_capture,
        live_financial_capture=live_financial_capture,
        guard_audit=guard_audit,
    )
    owner_skipped_count = compact_count(owner_exclusion.get("message_skip_count"))
    owner_live_update_unverified = compact_count(owner_gate_summary.get("live_update_unverified_count"))
    owner_live_financial_unverified = compact_count(owner_gate_summary.get("live_financial_unverified_count"))
    owner_lofty_pm_tabs = compact_count(owner_gate_summary.get("lofty_pm_tab_count"))
    owner_lofty_login_tabs = compact_count(owner_gate_summary.get("lofty_login_tab_count") or lofty_cdp_preflight.get("login_tab_count"))
    guarded_apply_update_failed = 0
    guarded_apply_financial_failed = 0
    for record in guarded_apply.get("records") or []:
        if ((record.get("updates") or {}).get("status")) == "guard_failed":
            guarded_apply_update_failed += 1
        if ((record.get("financials") or {}).get("status")) == "guard_failed":
            guarded_apply_financial_failed += 1
    guarded_apply_guard_issue_count = compact_count(guard_audit.get("issue_count"))
    if owner_lofty_pm_tabs:
        lofty_live_guard_auth_action = "capture live PM guards"
    elif owner_lofty_login_tabs:
        lofty_live_guard_auth_action = "refresh/reopen Lofty login tab; capture live PM guards"
    else:
        lofty_live_guard_auth_action = "auth Lofty CDP; capture live PM guards"
    lofty_live_guard_auth_blocked = (
        lofty_cdp_preflight.get("status") == "review"
        and owner_lofty_pm_tabs < 1
        and (
            live_capture.get("status") in {"failed", "review"}
            or live_financial_capture.get("status") in {"failed", "review"}
            or owner_live_update_unverified
            or owner_live_financial_unverified
        )
    )
    cf_summary = weekly_cf_gate.get("summary") or {}
    cf_conflicts = compact_count(cf_summary.get("conflict_count") or weekly_cf_sync.get("conflict_count"))
    cf_missing_canonical = compact_count(
        weekly_cf_sync.get("missing_canonical_cf_count")
        or weekly_file_updates.get("cf_statement_sync_missing_canonical_cf_count")
    )
    cf_audit_errors = compact_count(
        weekly_cf_sync.get("audit_error_count")
        or weekly_file_updates.get("cf_statement_sync_audit_error_count")
    )
    cf_audit_error_classes = weekly_cf_sync.get("audit_error_class_counts")
    if not isinstance(cf_audit_error_classes, dict):
        cf_audit_error_classes = {}
    cf_no_gl_total = compact_count(
        cf_no_gl_property_match.get("no_gl_property_match_count")
        or weekly_file_updates.get("cf_no_gl_property_match_count")
        or cf_audit_error_classes.get("no_gl_property_match")
    )
    cf_no_gl_active = compact_count(
        cf_no_gl_property_match.get("active_monthly_scope_count")
        or weekly_file_updates.get("cf_no_gl_property_match_active_monthly_scope_count")
    )
    cf_untagged = compact_count(cf_summary.get("untagged_review_required_count") or weekly_cf_sync.get("untagged_review_required_count"))
    cf_rule_candidates = compact_count(cf_summary.get("untagged_rule_candidate_count") or weekly_cf_sync.get("untagged_rule_candidate_count"))
    cf_gate_action_queue_count = compact_count(cf_summary.get("action_queue_count") or weekly_cf_gate.get("action_queue_count"))
    cf_gate_blocker_count = compact_count(weekly_cf_gate.get("blocker_count") or cf_summary.get("blocker_count"))
    cf_manual_untagged_actions = compact_count(cf_summary.get("manual_untagged_action_count"))
    cf_manual_rule_candidate_actions = compact_count(cf_summary.get("manual_rule_candidate_action_count"))
    cf_action_counts = weekly_cf_sync.get("conflict_review_action_counts")
    if not isinstance(cf_action_counts, dict):
        cf_action_counts = {}
    cf_stale_workbook_cell_conflicts = sum(
        compact_count(cf_action_counts.get(key))
        for key in (
            "fill_from_gl",
            "overwrite",
            "overwrite_formula_from_gl",
            "overwrite_formula",
        )
    )
    ecogl_auto_safe = compact_count(
        cf_summary.get("ecogl_safe_apply_action_count")
        or weekly_file_updates.get("ecogl_safe_apply_action_count")
        or ecogl_safe_apply.get("safe_action_count")
        or
        cf_summary.get("ecogl_auto_safe_untagged_row_count")
        or ecogl_autonomy.get("safe_auto_untagged_row_count")
    )
    ecogl_exceptions = compact_count(
        cf_summary.get("ecogl_exception_count")
        or ecogl_autonomy.get("exception_count")
    )
    source_cash_violations = compact_count(weekly_cf_sync.get("source_cash_balance_violation_count"))
    cf_balance_sheet_consistency_loaded = cf_balance_sheet_consistency.get("status") not in {"missing", "unreadable"}
    cf_balance_sheet_consistency_issues = compact_count(
        cf_balance_sheet_consistency.get("issue_count")
        if cf_balance_sheet_consistency_loaded
        else weekly_cf_sync.get("cf_balance_sheet_consistency_issue_count")
    )
    lofty_transfer_status = str(lofty_transfer_requirements.get("status") or "").strip()
    lofty_transfer_loaded = lofty_transfer_status not in {"", "missing", "unreadable"}
    lofty_transfer_blocked = lofty_transfer_loaded and lofty_transfer_status != "ok"
    lofty_transfer_held_count = compact_count(lofty_transfer_requirements.get("held_property_count"))
    lofty_transfer_ready_count = compact_count(lofty_transfer_requirements.get("ready_to_send_property_count"))
    lofty_transfer_property_count = compact_count(lofty_transfer_requirements.get("property_count"))
    lofty_transfer_provisional_total = lofty_transfer_requirements.get("provisional_send_to_lofty_total")
    lofty_transfer_shortfall_total = lofty_transfer_requirements.get(
        "combined_reserve_shortfall_total",
        lofty_transfer_requirements.get("eco_cash_shortfall_total"),
    )
    lofty_transfer_recommended_total = lofty_transfer_requirements.get("recommended_send_to_lofty_total")
    lofty_transfer_source_clean = lofty_transfer_requirements.get("source_clean_for_final_transfer_amounts")
    cf_balance_sheet_cash_apply_status = str(cf_balance_sheet_cash_apply.get("status") or "").strip()
    cf_balance_sheet_cash_apply_change_count = compact_count(cf_balance_sheet_cash_apply.get("change_count"))
    ecogl_conflict_exceptions = compact_count(ecogl_autonomy.get("conflict_exception_count"))
    cf_zero_fills = compact_count(
        weekly_file_updates.get("cf_statement_sync_conflict_auto_apply_approved_applicable_count")
        or weekly_cf_sync.get("conflict_auto_apply_approved_applicable_count")
        or weekly_cf_sync.get("conflict_auto_approval_count")
    )
    ecogl_untagged_exceptions = compact_count(
        cf_summary.get("ecogl_untagged_exception_row_count")
        or ecogl_autonomy.get("untagged_exception_row_count")
    )
    ecogl_raw_no_dao_mortgage_exceptions = compact_count(
        ecogl_autonomy.get("raw_no_dao_mortgage_exception_count")
    )
    ecogl_reason_counts = ecogl_autonomy.get("exception_reason_counts")
    if not isinstance(ecogl_reason_counts, dict):
        ecogl_reason_counts = {}
    ecogl_reason_summary = []
    reason_aliases = [
        ("No-DAO-mortgage property has a raw Baselane mortgage payment row.", "raw-mortgage"),
        ("No-DAO-mortgage property has raw principal/interest debt language.", "raw-debt-notes"),
        ("needs_specific_category", "untagged"),
        (
            "CF has a manual/accrual value while Baselane GL is empty; fix Baselane tagging/accrual or explicitly review manually.",
            "GL-empty",
        ),
        ("overwrite", "overwrite"),
        (
            "Workbook cell is a formula; preserve formula and review Baselane accrual/category tagging.",
            "formula",
        ),
    ]
    for reason_key, reason_label in reason_aliases:
        count = compact_count(ecogl_reason_counts.get(reason_key))
        if count:
            ecogl_reason_summary.append(f"{reason_label}={count}")
    source_fix_action_count = compact_count(
        ecogl_source_fix.get("action_count") if ecogl_source_fix.get("status") not in {"missing", "unreadable"} else weekly_file_updates.get("ecogl_source_fix_action_count")
    )
    source_fix_counts = (
        ecogl_source_fix.get("action_type_counts")
        if ecogl_source_fix.get("status") not in {"missing", "unreadable"}
        else weekly_file_updates.get("ecogl_source_fix_action_type_counts")
    )
    if not isinstance(source_fix_counts, dict):
        source_fix_counts = {}
    source_fix_summary = []
    source_fix_aliases = [
        ("book_or_tag_baselane_accrual", "GL-empty"),
        ("reconcile_formula_to_baselane_accrual_or_tagging", "formula"),
        ("tag_baselane_transaction_category", "untagged"),
    ]
    for action_key, action_label in source_fix_aliases:
        count = compact_count(source_fix_counts.get(action_key))
        if count:
            source_fix_summary.append(f"{action_label}={count}")
    source_fix_verified_count = compact_count(ecogl_source_fix_verifier.get("verified_fixed_count"))
    source_fix_remaining_count = compact_count(ecogl_source_fix_verifier.get("remaining_count"))
    source_fix_total_count = source_fix_verified_count + source_fix_remaining_count
    source_fix_verifier_summary = (
        f"verified {source_fix_verified_count}/{source_fix_total_count}"
        if source_fix_total_count
        else ""
    )
    source_fix_validation_pending_count = compact_count(ecogl_source_fix_correction_validation.get("pending_count"))
    source_fix_validation_invalid_count = compact_count(ecogl_source_fix_correction_validation.get("invalid_count"))
    source_fix_validation_ready_count = compact_count(ecogl_source_fix_correction_validation.get("ready_count"))
    source_fix_approval_pending_count = compact_count(ecogl_source_fix_approval.get("pending_count"))
    source_fix_approval_invalid_count = compact_count(ecogl_source_fix_approval.get("invalid_count"))
    source_fix_approval_approved_count = compact_count(ecogl_source_fix_approval.get("approved_count"))
    source_fix_queue_row_count = compact_count(ecogl_source_fix_action_queue.get("row_count"))
    source_fix_queue_ready_count = compact_count(ecogl_source_fix_action_queue.get("ready_to_apply_count"))
    source_fix_queue_needs_current_source_index_count = compact_count(
        ecogl_source_fix_action_queue.get("needs_current_source_index_count")
    )
    source_fix_queue_decision_required_count = compact_count(ecogl_source_fix_action_queue.get("decision_required_count"))
    source_fix_queue_group_counts = ecogl_source_fix_action_queue.get("group_counts")
    if not isinstance(source_fix_queue_group_counts, dict):
        source_fix_queue_group_counts = {}
    source_fix_queue_native_split_count = compact_count(
        ecogl_source_fix_action_queue.get("ready_native_split_count")
        or source_fix_queue_group_counts.get("ready_native_split")
    )
    source_fix_queue_actionable_count = (
        source_fix_queue_ready_count
        + source_fix_queue_native_split_count
        + source_fix_queue_needs_current_source_index_count
        + source_fix_queue_decision_required_count
    )
    if ecogl_source_fix_action_queue.get("status") not in {"missing", "unreadable"}:
        source_fix_action_count = source_fix_queue_actionable_count
    source_fix_ready_count = source_fix_queue_ready_count
    source_fix_effectively_clear = (
        ecogl_source_fix_verifier.get("status") == "ok"
        and source_fix_total_count > 0
        and source_fix_remaining_count == 0
        and source_fix_queue_row_count == 0
        and source_fix_queue_ready_count == 0
        and source_fix_queue_needs_current_source_index_count == 0
        and source_fix_queue_decision_required_count == 0
    )
    if source_fix_effectively_clear:
        source_fix_validation_pending_count = 0
        source_fix_validation_invalid_count = 0
        source_fix_validation_ready_count = 0
        source_fix_approval_pending_count = 0
        source_fix_approval_invalid_count = 0
        source_fix_approval_approved_count = 0
        source_fix_ready_count = 0
        source_fix_queue_needs_current_source_index_count = 0
    native_split_ready_count = compact_count(
        native_split_plan.get("ready_native_split_count")
        or source_fix_queue_native_split_count
    )
    native_split_applied_count = compact_count(native_split_apply.get("applied_count"))
    native_split_already_applied_count = compact_count(native_split_apply.get("already_applied_count"))
    native_split_handled_count = compact_count(
        native_split_plan.get("handled_native_split_count")
        or native_split_apply.get("handled_native_split_count")
        or (native_split_applied_count + native_split_already_applied_count)
    )
    native_split_failure_count = compact_count(native_split_apply.get("failure_count"))
    native_split_blocked_count = compact_count(
        native_split_apply.get("blocked_count")
        or native_split_plan.get("blocked_count")
    )
    if (
        native_split_apply.get("status") == "ok"
        and compact_count(native_split_apply.get("row_count")) == native_split_ready_count
        and compact_count(native_split_apply.get("ready_count")) == 0
        and native_split_failure_count == 0
    ):
        native_split_ready_count = 0
    if native_split_handled_count and native_split_failure_count == 0 and native_split_blocked_count == 0:
        native_split_ready_count = 0
    source_fix_queue_needs_evidence_count = compact_count(source_fix_queue_group_counts.get("needs_source_evidence"))
    source_fix_queue_needs_decision_count = compact_count(source_fix_queue_group_counts.get("needs_category_decision"))
    source_fix_queue_candidate_count = max(0, source_fix_queue_ready_count - source_fix_ready_count)
    source_fix_recommendations = ecogl_source_fix_approval.get("autonomy_recommendation_counts")
    if not isinstance(source_fix_recommendations, dict):
        source_fix_recommendations = {}
    source_fix_queue_email_invoice_count = compact_count(source_fix_recommendations.get("blocked_email_invoice_required"))
    source_fix_queue_email_review_count = compact_count(source_fix_recommendations.get("review_email_invoice_evidence"))
    source_fix_queue_generic_evidence_count = max(0, source_fix_queue_needs_evidence_count - source_fix_queue_email_invoice_count)
    source_fix_queue_conflict_decision_count = max(0, source_fix_queue_needs_decision_count - source_fix_queue_email_review_count)
    source_fix_recommendation_labels = [
        ("blocked_no_support", "no-support"),
        ("review_weak_support", "weak"),
        ("blocked_conflicting_support", "conflict"),
        ("blocked_insufficient_evidence", "insufficient"),
    ]
    source_fix_blocked_recommendation_summary = " ".join(
        f"{label}={compact_count(source_fix_recommendations.get(key))}"
        for key, label in source_fix_recommendation_labels
        if compact_count(source_fix_recommendations.get(key))
    )
    source_fix_apply_preflight = ecogl_source_fix_apply.get("apply_preflight")
    if not isinstance(source_fix_apply_preflight, dict):
        source_fix_apply_preflight = {}
    source_fix_apply_preflight_status = str(source_fix_apply_preflight.get("status") or "unknown")
    source_fix_apply_preflight_issue_count = compact_count(source_fix_apply_preflight.get("issue_count"))
    source_fix_apply_preflight_suffix = (
        f"; preflight={source_fix_apply_preflight_status}"
        if source_fix_ready_count
        else ""
    )
    weekly_cf_effectively_ok = (
        weekly_cf_sync.get("status") == "ok" and cf_balance_sheet_consistency_issues == 0
        or (
            weekly_cf_gate.get("status") == "ok"
            and cf_gate_blocker_count == 0
            and cf_gate_action_queue_count == 0
            and source_fix_effectively_clear
            and source_cash_violations == 0
            and cf_balance_sheet_consistency_issues == 0
            and cf_conflicts == 0
        )
    )

    blocker = "none"
    action = "none"
    run_command = ""
    command_label = "RUN"
    issue_label = "BLOCKER"
    action_label = "DO"
    open_target = "none"
    hold = ""
    suppress_run_command = False
    if not daily_ok:
        title_status = "DAILY SYNC FAILED"
        daily_auth_blocker = daily_sync_auth_blocker_reason(daily)
        split_unresolved_count = compact_count(daily.get("split_unresolved_property_count"))
        split_unresolved_rows = compact_count(daily.get("split_unresolved_row_count"))
        split_unresolved_amount = daily.get("split_unresolved_amount_total")
        split_mismatch_count = compact_count(daily.get("split_output_mismatch_count"))
        sync_guard_review = (
            sync.get("reason") == "export_guard_review"
            or sync.get("export_failure_class") == "baselane_export_guard_review"
            or daily.get("sync_report_reason") == "export_guard_review"
            or daily.get("sync_report_failure_class") == "baselane_export_guard_review"
        )
        if daily_disk_preflight_blocked:
            blocker = "daily Baselane disk space"
            open_target = "reports/baselane_daily_disk_space_preflight_report.json"
            action = disk_preflight_action(daily_disk_preflight)
            command_label = "VERIFY"
            run_command = "df -h /mnt/c"
            hold = "weekly/monthly document updates"
        elif daily_auth_blocker:
            title_status = "BASELANE AUTH BLOCKED"
            blocker = daily_auth_blocker.replace("baselane_", "").replace("_", " ")
            login_wait = read_json(named_report_path("baselane_login_wait_report.json"))
            login_wait_has_recaptcha = (
                str(login_wait.get("reason") or "").strip() == "baselane_login_recaptcha_required"
                or login_wait.get("recaptcha_present") is True
            )
            if daily_auth_blocker == "baselane_login_recaptcha_required" or login_wait_has_recaptcha:
                open_target = "reports/baselane_login_wait_report.json"
                action = "solve Baselane reCAPTCHA in the visible CDP tab; rerun daily sync"
            else:
                open_target = "reports/baselane_cdp_auth_recovery_report.json"
                action = "authenticate the visible Baselane CDP tab; rerun daily sync"
            run_command = "bash scripts/baselane_cron_run.sh"
            hold = "weekly/monthly document updates"
        elif split_unresolved_count:
            title_status = "BASELANE DATA BLOCKED"
            blocker = "daily Baselane split routing"
            open_target = "reports/split_ledger_public_financials_last.json"
            action = (
                f"map/approve unresolved GL properties={split_unresolved_count} "
                f"rows={split_unresolved_rows} amount={split_unresolved_amount}"
            )
            run_command = "python3 scripts/split_ledger_public_financials.py --report reports/split_ledger_public_financials_last.json"
        elif split_mismatch_count:
            title_status = "BASELANE DATA BLOCKED"
            blocker = "daily Baselane property CSV freshness"
            open_target = "reports/split_ledger_public_financials_last.json"
            action = f"refresh stale/missing property GL CSV outputs={split_mismatch_count}"
            run_command = "python3 scripts/split_ledger_public_financials.py --report reports/split_ledger_public_financials_last.json"
        elif compact_count(daily.get("source_cash_balance_violation_count")):
            title_status = "BASELANE DATA BLOCKED"
            blocker = f"daily source-cash balance freshness ({compact_count(daily.get('source_cash_balance_violation_count'))} violations)"
            open_target = "reports/baselane_daily_source_cash_balance_report.json"
            if daily_source_cash_balance.get("apply_blocked_by_raw_no_dao_mortgage_guard"):
                raw_count = compact_count((daily_source_cash_balance.get("raw_no_dao_mortgage_guard") or {}).get("count"))
                if native_split_ready_count:
                    open_target = "reports/baselane_native_split_plan.csv"
                    action = f"apply guarded native mortgage splits ({native_split_ready_count} ready; raw-mtg {raw_count})"
                    run_command = "BASELANE_NATIVE_SPLIT_APPLY=1 python3 scripts/baselane_apply_native_splits.py --apply"
                elif native_split_handled_count and native_split_failure_count == 0 and native_split_blocked_count == 0:
                    action = f"refresh source-cash audit after native mortgage splits handled={native_split_handled_count}"
                    run_command = "python3 scripts/baselane_daily_source_cash_balance_audit.py --apply && python3 scripts/baselane_daily_sync_report.py"
                else:
                    open_target = "reports/baselane_native_split_plan.csv"
                    action = f"resolve guarded native mortgage split evidence (raw-mtg {raw_count})"
                    run_command = "python3 scripts/baselane_native_split_plan.py --source-index reports/baselane_source_transaction_index.csv"
            else:
                action = "refresh CF cash-balance rows from latest ECO GL before publish"
                run_command = "python3 scripts/baselane_daily_source_cash_balance_audit.py --apply"
            hold = "weekly/monthly document updates"
        elif daily.get("source_cash_balance_report_fresh") is False:
            title_status = "BASELANE DATA BLOCKED"
            blocker = "daily source-cash balance audit is stale"
            open_target = "reports/baselane_daily_source_cash_balance_report.json"
            action = "refresh CF cash-balance audit from latest ECO GL before publish"
            run_command = "python3 scripts/baselane_daily_source_cash_balance_audit.py --apply"
            hold = "weekly/monthly document updates"
        elif compact_count(daily.get("source_cash_balance_missing_row_count")) or compact_count(daily.get("source_cash_balance_missing_month_column_count")):
            title_status = "BASELANE DATA BLOCKED"
            blocker = "daily source-cash balance workbook structure"
            open_target = "reports/baselane_daily_source_cash_balance_report.json"
            action = (
                f"fix missing rows={compact_count(daily.get('source_cash_balance_missing_row_count'))} "
                f"missing month columns={compact_count(daily.get('source_cash_balance_missing_month_column_count'))}"
            )
            run_command = "python3 scripts/baselane_daily_source_cash_balance_audit.py"
            hold = "weekly/monthly document updates"
        elif local_model_blocker_active:
            blocker = "local qwen preflight"
            open_target = "reports/baselane_local_model_preflight_report.json"
            action = local_model_preflight_blocker_action(preflight)
            run_command = "python3 scripts/baselane_local_model_preflight.py && python3 scripts/baselane_daily_sync_report.py"
            hold = "automated investor-facing updates"
        elif compact_count(daily.get("pm_fee_duplicate_lane_count")):
            title_status = "BASELANE DATA BLOCKED"
            blocker = f"duplicate 1st/end PM fee lanes ({compact_count(daily.get('pm_fee_duplicate_lane_count'))})"
            open_target = "reports/baselane_pm_fee_duplicate_lane_audit.csv"
            action = f"solve Baselane auth, then apply guarded cleanup queue ({compact_count(source_cleanup_queue.get('action_count')) or compact_count(daily.get('pm_fee_duplicate_lane_count'))} rows)"
            run_command = (
                "BASELANE_SOURCE_CLEANUP_APPLY=1 scripts/baselane_source_cleanup_apply_then_refresh.sh"
            )
            hold = "weekly/monthly document updates"
        elif daily.get("hemlane_live_transaction_issue") is True:
            title_status = "BASELANE DATA BLOCKED"
            blocker = "Hemlane-backed source tagging"
            open_target = "reports/hemlane_live_transactions.json"
            action = "finish Hemlane auth in Brave/CDP; rerun daily sync; auto-tag fails closed until ok"
            run_command = "bash scripts/baselane_cron_run.sh"
            hold = "weekly/monthly document updates"
        elif compact_count(daily.get("first_day_pm_fee_count")):
            title_status = "BASELANE DATA BLOCKED"
            blocker = f"1st-day AOPS PM fee rows ({compact_count(daily.get('first_day_pm_fee_count'))})"
            open_target = "reports/baselane_first_day_pm_fee_source_cleanup_actions.csv"
            action = f"work exact source cleanup queue ({compact_count(daily.get('first_day_pm_fee_source_cleanup_action_count')) or compact_count(daily.get('first_day_pm_fee_count'))} rows)"
            run_command = (
                first_day_pm_fee_cleanup.get("cleanup_command_after_review")
                or "python3 scripts/baselane_first_day_pm_fee_source_cleanup_plan.py --all-months"
            )
            hold = "weekly/monthly document updates"
        elif sync_guard_review:
            title_status = "BASELANE DATA BLOCKED"
            coverage = guard.get("category_coverage_pct")
            minimum = guard.get("min_category_coverage")
            minimum_pct = round(float(minimum) * 100, 2) if isinstance(minimum, (int, float)) else minimum
            blocker = f"Baselane export guard (category coverage {coverage}%<{minimum_pct}%)"
            open_target = "reports/baselane_ecogl_source_fix_action_queue.md"
            decision_parts = []
            if source_fix_ready_count:
                decision_parts.append(f"apply ready={source_fix_ready_count}")
            elif source_fix_queue_candidate_count:
                decision_parts.append(f"approve candidates={source_fix_queue_candidate_count}")
            if source_fix_queue_needs_current_source_index_count:
                decision_parts.append(f"refresh IDs={source_fix_queue_needs_current_source_index_count}")
            if native_split_ready_count:
                decision_parts.append(f"split plan={native_split_ready_count}")
            if source_fix_queue_email_invoice_count:
                decision_parts.append(f"find invoices={source_fix_queue_email_invoice_count}")
            if source_fix_queue_generic_evidence_count:
                decision_parts.append(f"find evidence={source_fix_queue_generic_evidence_count}")
            if source_fix_queue_email_review_count:
                decision_parts.append(f"review email={source_fix_queue_email_review_count}")
            if source_fix_queue_conflict_decision_count:
                decision_parts.append(f"resolve conflicts={source_fix_queue_conflict_decision_count}")
            if not decision_parts:
                decision_parts.append("fix Baselane source categories")
            action = f"{'; '.join(decision_parts)}; canonical blocked"
            run_command = (
                "BASELANE_SOURCE_FIX_APPLY=1 bash scripts/baselane_apply_source_fix_then_refresh.sh"
                if source_fix_ready_count
                else "bash scripts/baselane_cron_run.sh"
                if source_fix_queue_needs_current_source_index_count
                else "python3 scripts/baselane_ecogl_source_fix_approval.py"
            )
            hold = "weekly/monthly document updates"
        else:
            blocker = "daily Baselane sync"
            daily_issues = daily_issue_text
            daily_run_status = str(daily_run.get("status") or "").strip()
            daily_run_failed_step = str(daily_run.get("failed_step") or "").strip()
            if daily_run_status in {"failed", "review"} or daily_run_failed_step:
                open_target = "reports/baselane_daily_run_report.json"
            else:
                open_target = "reports/baselane_daily_sync_report.json"
            sync_failure_class = str(sync.get("export_failure_class") or daily.get("sync_report_failure_class") or "").strip()
            if daily_disk_preflight_blocked:
                open_target = "reports/baselane_daily_disk_space_preflight_report.json"
                action = disk_preflight_action(daily_disk_preflight)
                command_label = "VERIFY"
                run_command = "df -h /mnt/c"
            elif sync_failure_class == "baselane_login_auth_401":
                open_target = "reports/baselane_sync_cdp_report.json"
                session_seed_status = str(daily.get("session_seed_status") or "").strip()
                auth_recovery_issue = str(baselane_auth_recovery.get("issue_summary") or "").lower()
                auth_probe_timed_out = (
                    baselane_auth_recovery.get("status") == "review"
                    and (
                        "content probe timed out" in auth_recovery_issue
                        or "page content probe timed out" in auth_recovery_issue
                        or "renderer" in auth_recovery_issue and "timed out" in auth_recovery_issue
                    )
                )
                auth_content_unverified = (
                    baselane_auth_recovery.get("status") == "review"
                    and "content did not verify" in auth_recovery_issue
                )
                if auth_probe_timed_out:
                    action = "close/reopen Baselane CDP tab; renderer timed out; rerun daily sync"
                elif auth_content_unverified:
                    action = "auth visible Baselane CDP tab; rerun daily sync"
                elif daily.get("auth401_with_seed_failure") is True or session_seed_status not in {"", "ok", "not_started", "skipped"}:
                    action = "hard-refresh/reopen Baselane CDP tab; rerun daily sync"
                else:
                    action = "verify Baselane BW creds/CDP login; rerun daily sync"
            elif sync.get("interrupted") is True or str(sync.get("reason") or "").startswith("interrupted"):
                assetrail_status = str(daily.get("assetrail_push_status") or assetrail_push.get("status") or "").strip()
                assetrail_head = str(daily.get("assetrail_git_head") or assetrail_push.get("git_head") or "").strip()
                if assetrail_status in {"verified_current_clean", "committed_and_pushed", "pushed_no_ledger_changes"}:
                    head_suffix = f" ({assetrail_head})" if assetrail_head else ""
                    action = f"run human-paced Baselane sync once; AssetRail clean{head_suffix}"
                    command_label = "VERIFY"
                    run_command = (
                        "python3 scripts/baselane_sync_cdp_human_paced.py && "
                        "python3 scripts/baselane_daily_sync_report.py"
                    )
                else:
                    action = "reconcile interrupted sync state; rerun only if ledger not current"
            elif any(issue == "sync=running" for issue in daily_issues):
                action = "clear interrupted Baselane sync state; rerun deterministic daily sync"
            elif any(issue.startswith("canonical_ledger=") for issue in daily_issues):
                action = "reconcile AssetRail ledger with latest filtered Baselane export"
            elif str(sync.get("reason") or "").strip() == "cdp_login_failed":
                open_target = "reports/baselane_login_wait_report.json"
                login_reason = str(login_wait.get("reason") or "").strip()
                if login_reason == "baselane_login_recaptcha_required" or login_wait.get("recaptcha_present") is True:
                    action = "solve Baselane reCAPTCHA in CDP tab; rerun daily sync"
                elif login_reason in {"baselane_authenticated_content_not_confirmed", "baselane_login_required"}:
                    action = "finish Baselane login/CAPTCHA in CDP tab; rerun daily sync"
                else:
                    action = "fix Baselane CDP login; rerun daily sync"
            elif any(issue.startswith("daily_steps_not_ok=") for issue in daily_issues):
                daily_next_action = str(daily.get("next_action") or "").lower()
                if "recaptcha" in daily_next_action:
                    action = "solve Baselane reCAPTCHA in CDP tab; rerun daily sync"
                elif "captcha" in daily_next_action or "login" in daily_next_action:
                    action = "finish Baselane login/CAPTCHA in CDP tab; rerun daily sync"
                else:
                    action = "rerun daily sync; required step failed"
            else:
                scheduler_issue_count = len(daily_job.get("issues") or [])
                action = (
                    f"daily={daily.get('status')} sync={sync.get('status')} "
                    f"age={compact_age_hours(daily.get('daily_run_age_hours') or iso_age_hours(daily.get('ended_at')) or daily_job.get('report_age_hours'))}h "
                    f"step={daily.get('failed_step') or 'none'} "
                    f"scheduler_issues={scheduler_issue_count}"
                )
            if not run_command:
                run_command = "bash scripts/baselane_cron_run.sh"
        hold = "weekly/monthly document updates"
    elif monthly_statement_expected_wait and not rent_roll_needs_source:
        title_status = "REVIEW / MONTHLY WAIT"
        issue_label = "WAIT"
        action_label = "NEXT"
        blocker = "Baselane monthly statements not posted yet"
        open_target = "reports/baselane_monthly_statements_idempotent_report.json"
        hemlane_auth_hint = hemlane_monthly_wait_auth_hint(hemlane_capture_report, hemlane_cdp_preflight)
        if hemlane_auth_hint:
            action = f"retry statements; {hemlane_auth_hint}; no email/listing"
            hold = "monthly docs/email/listings held until statements+rent roll pass"
        else:
            action = "retry after statement buttons appear; no email/listing publish"
            hold = "monthly document publish or investor email"
    elif (monthly_statement_step_failed or monthly_statement_gate_failed) and not (
        monthly_statement_expected_wait and rent_roll_needs_source
    ):
        blocker = "monthly Baselane bank statements"
        open_target = "reports/baselane_monthly_statements_idempotent_report.json"
        captured = compact_count(
            first_present(
                monthly_statements_gate.get("captured_unique_count"),
                monthly.get("monthly_statements_captured_unique_count"),
            )
        )
        minimum = compact_count(
            first_present(
                monthly_statements_gate.get("min_captured_required"),
                monthly.get("monthly_statements_min_captured_required"),
            )
        )
        reason = (
            monthly_statements_gate.get("reason")
            or monthly.get("monthly_statements_gate_reason")
            or monthly.get("failed_step")
            or "statement gate failed"
        )
        download_error = str(monthly_statements_download.get("error") or "")
        if "CDP command timed out:" in download_error:
            reason = "cdp-timeout " + download_error.split("CDP command timed out:", 1)[1].splitlines()[0].strip()
        elif "login form submission failed" in download_error or "AUTH_REQUIRED" in download_error:
            reason = "auth-required"
        elif "unitAPINonSensitiveToken" in download_error and "404" in download_error:
            reason = "unit-token-404"
        elif "no statement download buttons discovered" in download_error:
            reason = "no-statement-buttons"
        elif "no new PDF files" in download_error:
            reason = "no-new-pdfs"
        elif monthly_statements_download.get("ok") is False and download_error:
            reason = "download-failed"
        auth_recovery_attempted = monthly_statements_gate.get("auth_recovery_attempted") is True
        auth_recovery_manual_required = monthly_statements_gate.get("auth_recovery_manual_auth_required") is True
        auth_recovery_status = str(monthly_statements_gate.get("auth_recovery_status") or "")
        display_reason = reason
        if reason == "auth-required" and auth_recovery_attempted and auth_recovery_manual_required:
            action_prefix = "finish Baselane login; gate refresh"
        elif reason == "auth-required" and auth_recovery_attempted and auth_recovery_status == "ok":
            action_prefix = "run statement capture"
            display_reason = "auth-recovered"
        elif reason == "no-statement-buttons":
            action_prefix = "retry capture after Baselane posts statements"
        else:
            action_prefix = "hard-refresh Baselane; gate refresh" if reason == "auth-required" else "refresh statement gate"
        action = f"{action_prefix}; captured={captured}/{minimum} reason={display_reason}"
        if display_reason == "auth-recovered":
            run_command = "bash scripts/baselane_monthly_statements_idempotent.sh"
        elif display_reason == "no-statement-buttons":
            run_command = "bash scripts/baselane_monthly_statements_idempotent.sh"
        else:
            run_command = "BASELANE_MONTHLY_STATEMENTS_GATE_ONLY=1 bash scripts/baselane_monthly_statements_idempotent.sh"
        hold = "monthly document publish or investor email"
    elif monthly_finance_truth_blocked:
        blocker = "monthly finance-truth refresh"
        open_target = "reports/baselane_monthly_finance_truth_refresh.json"
        action = monthly_finance_truth_eod_action(monthly, readiness, readiness_primary)
        if monthly_recovery_marker:
            action = f"{action}; {monthly_recovery_marker}"
        suppress_run_command = True
        hold = "downstream CF/FINANCIALS/Lofty/Discord/email outputs"
    elif not scheduler_ok and not eod_send_proof_only:
        scheduler_primary = (
            (scheduler_audit.get("actionable_summary") or {}).get("primary_blocker")
            if isinstance(scheduler_audit.get("actionable_summary"), dict)
            else None
        )
        scheduler_primary = scheduler_primary if isinstance(scheduler_primary, dict) else {}
        if (
            scheduler_only_eod_telegram_issue(scheduler_issues)
            and eod_telegram_credentials_missing(eod_telegram_report)
        ):
            open_target = "reports/baselane_eod_telegram_report.json"
            blocker = "EOD Telegram credentials/proof"
            action = "set TELEGRAM_BOT_TOKEN+BASELANE_EOD_TELEGRAM_CHAT_ID; send EOD; rerun audit"
            run_command = "python3 scripts/baselane_eod_telegram_report.py --send"
            hold = "failure visibility until Telegram send is proven"
        elif scheduler_only_eod_telegram_issue(scheduler_issues):
            open_target = "reports/baselane_eod_telegram_send_state.json"
            blocker = "EOD Telegram send proof"
            action = "send digest-bound EOD via explicit flag or scheduled delivery"
            run_command = "python3 scripts/baselane_eod_telegram_report.py --send"
            hold = "failure visibility until Telegram send is proven"
        elif scheduler_primary.get("class") == "scheduler.openclaw_stale_enabled_jobs":
            open_target = "reports/baselane_scheduler_audit_report.json"
            blocker = "scheduler audit"
            stale_count = compact_count(scheduler_primary.get("count")) or compact_count(scheduler_audit.get("issue_count"))
            action = f"review gated disable script for {stale_count} stale OpenClaw job(s)"
            run_command = str(scheduler_primary.get("artifact") or "reports/baselane_scheduler_remediation.requires-explicit-approval.sh")
            hold = "weekly/monthly document updates; EOD clarity"
        else:
            open_target = "reports/baselane_scheduler_audit_report.json"
            blocker = "scheduler audit"
            action = f"fix {len(scheduler_issues) or scheduler_audit.get('issue_count')} scheduler issue(s)"
            run_command = "python3 scripts/baselane_scheduler_audit.py --root /home/digit/.openclaw/workspace"
            hold = "weekly/monthly document updates"
    elif daily_disk_preflight_blocked:
        blocker = "daily Baselane disk space"
        open_target = "reports/baselane_daily_disk_space_preflight_report.json"
        action = disk_preflight_action(daily_disk_preflight)
        command_label = "VERIFY"
        run_command = "df -h /mnt/c"
        hold = "weekly/monthly document updates"
    elif local_model_blocker_active:
        blocker = "local qwen preflight"
        open_target = "reports/baselane_local_model_preflight_report.json"
        action = local_model_preflight_blocker_action(preflight)
        run_command = "rerun the local model preflight, then rerun EOD"
        hold = "automated investor-facing updates"
    elif not paths_ok:
        blocker = "public folder path guard"
        open_target = "reports/lofty_public_path_guard_report.json"
        action = "use real Dropbox roots and canonical README/P&L folders"
        run_command = "python3 scripts/lofty_public_path_guard.py --root /home/digit/.openclaw/workspace"
        hold = "monthly document publish or investor email"
    elif not discord_public_financial_sources_ok:
        blocker = "discord-public financial source guard"
        open_target = "reports/discord_public_financial_source_guard_report.json"
        action = "remove legacy raw GL exports and keep public financial reads on Dropbox FINANCIALS.md/UPDATES.md"
        run_command = "python3 scripts/discord_public_financial_source_guard.py --root /home/digit/.openclaw/workspace"
        hold = "public investor financial answers"
    elif not tenant_ledgers_ok:
        blocker = f"tenant ledger routing ({compact_count(tenant_ledger_guard.get('issue_count'))} issues)"
        open_target = "reports/lofty_tenant_ledger_folder_guard_report.md"
        action = "fix cross-property or legacy tenant ledger destinations"
        run_command = "python3 scripts/lofty_tenant_ledger_folder_guard.py --real-estate-root /mnt/c/Users/digit/Dropbox/Real\\ Estate"
        hold = "monthly document publish or investor email"
    elif not no_mortgage_financials_ok:
        blocker = f"IL/OH/TN mortgage rows ({compact_count(no_mortgage_remaining)} nonzero)"
        open_target = "reports/baselane_no_mortgage_financials_cleanup_report.json"
        action = "zero IL/OH/TN mortgage rows from public FINANCIALS.md auto cash-flow blocks"
        run_command = "python3 scripts/baselane_no_mortgage_financials_guard.py --report reports/baselane_no_mortgage_financials_cleanup_report.json --apply"
        hold = "Lofty PM publish or investor email"
    elif (
        not report_integrity_ok
        and not report_integrity_deferred_by_source_quality
        and not (rent_roll_needs_source and report_integrity_financial_candidate_only)
    ):
        issue_reports = [
            name
            for name, report in (report_integrity_guard.get("reports") or {}).items()
            if isinstance(report, dict) and compact_count(report.get("issue_count")) > 0
        ]
        integrity_issue_count = compact_count(report_integrity_guard.get("issue_count"))
        detail = ",".join(
            name.replace("baselane_", "").replace("_report.json", "").replace(".json", "")
            for name in issue_reports[:2]
        ) if issue_reports else str(report_integrity_guard.get("status") or "missing")
        if report_integrity_issue_codes & report_integrity_send_proof_issue_codes:
            blocker = "EOD Telegram send proof missing"
            open_target = "reports/baselane_eod_telegram_send_state.json"
            action = "send digest-bound EOD via explicit flag or scheduled delivery"
            run_command = "python3 scripts/baselane_eod_telegram_report.py --send"
        elif "future_cf_statement_values_clear_report.json" in issue_reports or any(
            code.startswith("future_cf_values_") for code in report_integrity_issue_codes
        ):
            blocker = f"future CF Revenue/OpEx actuals ({integrity_issue_count} issues)"
            open_target = "reports/future_cf_statement_values_clear_report.json"
            run_command = "python3 scripts/clear_future_cf_statement_values.py --year 2026 --start-month 7 --include-archive --include-conflicts --apply"
            action = run_command.replace("python3 scripts/", "run ")
        elif any(code.startswith("lofty_financial_patch_readiness_empty_candidate_quality_") for code in report_integrity_issue_codes):
            bad_candidate_count = compact_count(lofty_financial_patch_readiness.get("blocked_empty_patch_count"))
            quality_issue_count = compact_count(lofty_financial_patch_readiness.get("blocked_empty_patch_candidate_quality_issue_count"))
            blocker = (
                f"FIN candidates unsafe ({bad_candidate_count}p/{quality_issue_count}q)"
                if bad_candidate_count or quality_issue_count
                else f"FIN candidates unsafe ({integrity_issue_count} issues)"
            )
            open_target = "lofty_financial_patch_readiness.blocked-empty-patch.md"
            action = "regen reviewed FINANCIALS; no ledger summaries"
            run_command = "python3 scripts/lofty_financial_patch_readiness.py --report reports/lofty_financial_patch_readiness.json"
        else:
            blocker = f"critical report integrity ({integrity_issue_count} issues)" if integrity_issue_count else f"critical report integrity ({report_integrity_status or 'unknown'})"
            open_target = "reports/baselane_report_integrity_guard.json"
            action = f"repair report artifacts: {detail}"
            run_command = "python3 scripts/baselane_report_integrity_guard.py"
        hold = "email/Lofty PM publish"
    elif not weekly_cf_effectively_ok:
        if cf_balance_sheet_consistency_issues and not (
            source_quality_goal_blocker and not source_fix_effectively_clear
        ):
            blocker = f"CF balance sheet source consistency ({cf_balance_sheet_consistency_issues} issues)"
            open_target = "reports/baselane_cf_balance_sheet_consistency_audit.json"
            action = "sync authoritative CF Lofty/ECO cash rows"
            run_command = "bash scripts/baselane_weekly_file_updates_cron.sh"
            hold = "Lofty PM publish or investor email"
        elif source_cash_violations:
            blocker = f"ECO GL source cash balance ({source_cash_violations} violations)"
            open_target = "reports/baselane_weekly_cf_statement_sync_report.md"
            action = "fix raw GL cumulative Amount vs CF cash-balance row before publish"
            run_command = "bash scripts/baselane_weekly_file_updates_cron.sh"
            hold = "Lofty PM publish or investor email"
        else:
            effective_exceptions = (
                source_fix_action_count
                if source_quality_goal_blocker and source_fix_action_count
                else ecogl_exceptions or (cf_conflicts + cf_untagged)
            )
            effective_conflicts = ecogl_conflict_exceptions or cf_conflicts
            if source_fix_effectively_clear:
                if cf_conflicts:
                    if cf_stale_workbook_cell_conflicts:
                        blocker = f"CF workbook stale cells ({cf_stale_workbook_cell_conflicts} unresolved)"
                        action = "rerun weekly CF sync; apply GL-authoritative workbook updates"
                    else:
                        blocker = f"CF statement review ({cf_conflicts} conflicts)"
                        action = (
                            f"review GL-empty CF values={cf_conflicts}; "
                            f"source fixes verified={source_fix_verified_count}/{source_fix_total_count}"
                        )
                    open_target = "reports/baselane_cf_conflict_review_packet.json"
                else:
                    actionable_category_count = cf_gate_action_queue_count or cf_untagged
                    blocker = f"CF category gate ({actionable_category_count} actions)"
                    open_target = "reports/baselane_weekly_cf_review_gate.csv"
                    action = (
                        f"classify untagged={cf_manual_untagged_actions}; "
                        f"rules={cf_manual_rule_candidate_actions}; "
                        f"auto-safe={ecogl_auto_safe}; blockers={cf_gate_blocker_count}"
                    )
                run_command = "bash scripts/baselane_weekly_file_updates_cron.sh"
            else:
                blocker = f"ECO GL data quality ({effective_exceptions} exceptions)"
                open_target = "reports/baselane_ecogl_source_fix_action_queue.md" if source_fix_action_count or ecogl_source_fix_corrections.get("status") == "review" else "reports/baselane_ecogl_data_quality_exceptions.csv"
                fix_target = ", ".join(source_fix_summary or ecogl_reason_summary)
                if not fix_target:
                    parts = []
                    if ecogl_raw_no_dao_mortgage_exceptions:
                        parts.append(f"{ecogl_raw_no_dao_mortgage_exceptions} raw mortgage")
                    parts.append(f"{ecogl_untagged_exceptions} untagged")
                    parts.append(f"{effective_conflicts} conflicts")
                    fix_target = " + ".join(parts)
                verb = "source-fix" if source_fix_action_count else "fix"
                verifier_suffix = f"; {source_fix_verifier_summary}" if source_fix_verifier_summary else ""
                if source_fix_approval_pending_count or source_fix_approval_invalid_count:
                    if source_fix_queue_decision_required_count:
                        decision_parts = []
                        if source_fix_ready_count:
                            decision_parts.append(f"apply ready={source_fix_ready_count}")
                        elif source_fix_queue_candidate_count:
                            decision_parts.append(f"approve candidates={source_fix_queue_candidate_count}")
                        else:
                            decision_parts.append("approve candidates=0")
                        if source_fix_queue_needs_current_source_index_count:
                            decision_parts.append(f"refresh IDs={source_fix_queue_needs_current_source_index_count}")
                        if native_split_ready_count:
                            decision_parts.append(f"split plan={native_split_ready_count}")
                        if source_fix_queue_email_invoice_count:
                            decision_parts.append(f"find invoices={source_fix_queue_email_invoice_count}")
                        if source_fix_queue_generic_evidence_count:
                            decision_parts.append(f"find evidence={source_fix_queue_generic_evidence_count}")
                        if source_fix_queue_email_review_count:
                            decision_parts.append(f"review email={source_fix_queue_email_review_count}")
                        decision_parts.append(f"resolve conflicts={source_fix_queue_conflict_decision_count}")
                        if not decision_parts:
                            decision_parts.append(f"decide categories={source_fix_queue_decision_required_count}")
                        action = f"{'; '.join(decision_parts)}; invalid={source_fix_approval_invalid_count}{source_fix_apply_preflight_suffix}"
                    elif source_fix_blocked_recommendation_summary:
                        ready_parts = []
                        if source_fix_ready_count:
                            ready_parts.append(f"apply ready={source_fix_ready_count}")
                        elif source_fix_queue_candidate_count:
                            ready_parts.append(f"approve candidates={source_fix_queue_candidate_count}")
                        if source_fix_queue_needs_current_source_index_count:
                            ready_parts.append(f"refresh IDs={source_fix_queue_needs_current_source_index_count}")
                        if native_split_ready_count:
                            ready_parts.append(f"split plan={native_split_ready_count}")
                        ready_prefix = f"{'; '.join(ready_parts)}; " if ready_parts else ""
                        action = f"{ready_prefix}resolve categories: {source_fix_blocked_recommendation_summary}; invalid={source_fix_approval_invalid_count}{source_fix_apply_preflight_suffix}"
                    else:
                        action = f"approve {source_fix_approval_pending_count} source categorization row(s); invalid={source_fix_approval_invalid_count}{source_fix_apply_preflight_suffix}"
                elif source_fix_validation_pending_count or source_fix_validation_invalid_count:
                    action = f"validate {source_fix_validation_pending_count} approved row(s); invalid={source_fix_validation_invalid_count}"
                else:
                    action = f"auto {ecogl_auto_safe} category rows + {cf_zero_fills} CF {plural_label(cf_zero_fills, 'sync')}; {verb} {fix_target}{verifier_suffix}"
                run_command = (
                    "BASELANE_SOURCE_FIX_APPLY=1 bash scripts/baselane_apply_source_fix_then_refresh.sh"
                    if source_fix_ready_count
                    else "python3 scripts/baselane_ecogl_source_fix_approval.py"
                )
            hold = "Lofty PM publish or investor email"
    elif lofty_transfer_blocked and goal_primary_id != "monthly_review_and_guarded_apply":
        blocker = f"Lofty transfer readiness ({lofty_transfer_held_count or lofty_transfer_property_count} held)"
        open_target = "reports/baselane_lofty_transfer_requirements.json"
        if lofty_transfer_source_clean is False:
            action = (
                "clean source + sync CF cash rows; "
                f"final send amounts held; provisional={compact_money_short(lofty_transfer_provisional_total)}"
            )
        elif cf_balance_sheet_cash_apply_change_count:
            action = (
                f"apply guarded CF balance cash rows ({cf_balance_sheet_cash_apply_change_count} changes); "
                f"provisional={compact_money_short(lofty_transfer_provisional_total)}"
            )
        else:
            action = (
                "resolve transfer holds; "
                f"provisional={compact_money_short(lofty_transfer_provisional_total)} "
                f"combined ECO+Lofty OR shortfall={compact_money_short(lofty_transfer_shortfall_total)}"
            )
        run_command = "bash scripts/baselane_weekly_file_updates_cron.sh"
        hold = "Lofty transfers; Lofty PM publish or investor email"
    elif eod_send_proof_only or goal_primary_eod_visibility or (
        report_integrity_send_proof_only and not operational_goal_blocker_pending
    ):
        open_target = (
            "reports/baselane_eod_telegram_send_state.json"
            if report_integrity_send_proof_only or goal_primary_eod_visibility or eod_send_state_actionable
            else "reports/baselane_scheduler_audit_report.json"
        )
        blocker = (
            "EOD Telegram send proof missing"
            if report_integrity_send_proof_only or goal_primary_eod_visibility
            else "EOD Telegram send proof"
        )
        action = "send digest-bound EOD via explicit flag or scheduled delivery"
        run_command = "python3 scripts/baselane_eod_telegram_report.py --send"
        hold = "failure visibility until Telegram send is proven"
    elif rent_roll_needs_source:
        blocker = rent_roll_source_blocker_label(rent_roll_evidence)
        open_target = rent_roll_queue_path
        capture_attempt_count = (
            hemlane_capture_report.get("login_recovery_try_count")
            or hemlane_capture_report.get("login_recovery_attempt_count")
            or len(hemlane_capture_report.get("login_recovery_attempts") or [])
        )
        preflight_needs_open_tab = hemlane_preflight_needs_open_tab(hemlane_cdp_preflight)
        if preflight_needs_open_tab:
            open_target = "reports/hemlane_cdp_preflight_report.json"
        elif hemlane_capture_report.get("status") == "review":
            open_target = compact_path(str(hemlane_rent_roll_capture_report), json_to_md=False)
        hemlane_auth_state = "auth Hemlane CDP"
        preflight_login_refresh_pending = hemlane_preflight_prefers_login_refresh(hemlane_cdp_preflight)
        recovery_exhausted = (
            hemlane_capture_report.get("login_recovery_exhausted") is True
            or hemlane_capture_report.get("automated_browser_recovery_complete") is True
            or hemlane_capture_report.get("manual_auth_phase") == "after_browser_recovery"
            or (
                compact_count(capture_attempt_count) > 0
                and hemlane_capture_report.get("issue") in {"login_required", "recaptcha_required"}
            )
        )
        if hemlane_capture_report.get("issue") == "recaptcha_required":
            if hemlane_capture_report.get("bitwarden_login_submit_ok") is True:
                attempt_suffix = f"; auto tried {capture_attempt_count}x" if capture_attempt_count else ""
                hemlane_auth_state = f"solve Hemlane reCAPTCHA (BW ok{attempt_suffix})"
            elif recovery_exhausted and capture_attempt_count:
                hemlane_auth_state = f"finish Hemlane login/CAPTCHA (auto tried {capture_attempt_count}x)"
            else:
                attempt_suffix = f" ({capture_attempt_count} tries)" if capture_attempt_count else ""
                hemlane_auth_state = f"finish Hemlane login/CAPTCHA{attempt_suffix}"
        elif hemlane_capture_report.get("issue") == "login_required":
            if recovery_exhausted and capture_attempt_count:
                hemlane_auth_state = f"finish Hemlane login (auto tried {capture_attempt_count}x)"
            else:
                attempt_suffix = f" ({capture_attempt_count} tries)" if capture_attempt_count else ""
                hemlane_auth_state = f"finish Hemlane login{attempt_suffix}"
        elif hemlane_cdp_preflight.get("status") == "ok":
            hemlane_auth_state = ""
        elif hemlane_cdp_preflight.get("cdp_available") is False:
            hemlane_auth_state = "start Hemlane CDP"
        elif compact_count(hemlane_cdp_preflight.get("login_tab_count")):
            hemlane_auth_state = (
                "finish Hemlane login; auto tried"
                if hemlane_preflight_has_current_visible_login(hemlane_cdp_preflight)
                else "finish Hemlane login"
            )
        elif compact_count(hemlane_cdp_preflight.get("hemlane_tab_count")) == 0:
            hemlane_auth_state = "open Hemlane rent-roll tab"
        post_auth_command = "bash scripts/baselane_financials_post_auth_resume.sh"
        if preflight_needs_open_tab:
            hemlane_auth_state = "open Hemlane tab; CAPTCHA if shown"
        elif hemlane_capture_report.get("issue") == "recaptcha_required":
            capture_next_action = str(hemlane_capture_report.get("next_action") or "")
            if hemlane_preflight_supersedes_capture_auth(hemlane_cdp_preflight, hemlane_capture_report):
                hemlane_auth_state = (
                    "finish Hemlane login/CAPTCHA; auto tried"
                    if hemlane_preflight_has_current_visible_login(hemlane_cdp_preflight)
                    else "hard refresh; CAPTCHA if shown"
                )
            elif hemlane_capture_report.get("bitwarden_login_submit_ok") is True:
                hemlane_auth_state = "solve reCAPTCHA (BW ok)"
            elif preflight_login_refresh_pending or any(
                marker in capture_next_action.lower()
                for marker in ("hard refresh", "hard-refresh", "close/open", "reopen", "reopened")
            ):
                hemlane_auth_state = "hard refresh; CAPTCHA if shown"
            else:
                hemlane_auth_state = "solve reCAPTCHA"
        elif hemlane_auth_state.startswith("auth Hemlane"):
            hemlane_auth_state = "hard refresh; auth if redirected" if preflight_login_refresh_pending else "auth Hemlane"
        action_prefix = f"{hemlane_auth_state}; " if hemlane_auth_state else ""
        action = f"{action_prefix}{post_auth_command}"
        if lofty_live_guard_auth_blocked:
            action = f"{action_prefix}auth Lofty PM tab; run post-auth resume"
        run_command = post_auth_command
        suppress_run_command = bool(hemlane_auth_state)
        hold = "owner email or Lofty PM publish"
    elif owner_review_gate.get("status") == "review" or review_manifest.get("status") != "ok":
        owner_actionable = owner_review_gate.get("actionable_summary") if isinstance(owner_review_gate.get("actionable_summary"), dict) else {}
        owner_primary = owner_actionable.get("primary_blocker") if isinstance(owner_actionable.get("primary_blocker"), dict) else {}
        owner_primary_label = str(owner_primary.get("blocker") or owner_primary.get("class") or "").strip()
        owner_primary_artifact = compact_path(owner_primary.get("artifact") or "")
        owner_primary_command = str(owner_primary.get("command") or "").strip()
        if owner_primary_label:
            blocker = (
                "Lofty PM publish pending live apply"
                if owner_primary_label.startswith("lofty_pm_publish.incomplete_apply")
                else "Lofty PM publish failed"
                if owner_primary_label.startswith("lofty_pm_publish.")
                else f"monthly owner automation ({owner_primary_label})"
            )
            open_target = owner_primary_artifact or "reports/baselane_monthly_owner_review_gate.csv"
            if open_target.startswith("updates/"):
                open_target = f"lofty-comms/{open_target}"
            action = compact_owner_primary_action(owner_primary)
            run_command = owner_primary_command
            suppress_run_command = bool("auth " in action.lower())
        else:
            blocker = f"monthly owner automation ({owner_review_count or owner_update_pending or owner_financial_pending or guarded_apply_update_failed or guarded_apply_financial_failed} rows)"
            open_target = "reports/baselane_monthly_owner_review_gate.csv"
        if owner_primary_label:
            pass
        elif not owner_update_pending and not owner_financial_pending and (
            owner_live_update_unverified or owner_live_financial_unverified or owner_lofty_pm_tabs < 1
            or guarded_apply_update_failed or guarded_apply_financial_failed or guarded_apply_guard_issue_count
        ):
            update_guard_count = max(owner_live_update_unverified, guarded_apply_update_failed)
            financial_guard_count = max(owner_live_financial_unverified, guarded_apply_financial_failed)
            action = (
                f"{lofty_live_guard_auth_action} updates={update_guard_count} "
                f"financials={financial_guard_count}"
            )
            run_command = (
                "DRY_RUN=1 SEND_OWNER_EMAILS=0 PUBLISH_LOFTY_PM_UPDATES=0 "
                "RUN_LOFTY_GUARDED_APPLY=1 APPLY_LOFTY_GUARDED_UPDATES=0 bash scripts/baselane_financials_monthly_cron.sh"
            )
        else:
            action = "rerun guarded owner update generation after upstream data is clean"
            run_command = "python3 scripts/baselane_monthly_owner_review_gate.py --root /home/digit/.openclaw/workspace"
        hold = "investor email or Lofty PM publish"
    elif rent_roll_evidence and (
        compact_count(rent_roll_evidence.get("rent_roll_gap_count")) > 0
        or compact_count(rent_roll_evidence.get("rent_roll_matched_count")) == 0
        or bool(rent_roll_evidence.get("rent_roll_stale_export_dates") or [])
    ):
        blocker = f"rent-roll review ({compact_count(rent_roll_evidence.get('rent_roll_gap_count'))} gaps)"
        open_target = rent_roll_queue_path
        action = "fix Hemlane matches or explicitly approve safe stale/gap rows"
        run_command = f"python3 /home/digit/.openclaw/workspace-lofty-vp-comms/scripts/monthly_rent_roll_gap_review.py --updates-dir /home/digit/.openclaw/workspace-lofty-vp-comms/updates --month {eod_run_month}"
        hold = "owner email"
    elif not goal_ok and goal_primary.get("blocker"):
        blocker = str(goal_primary.get("blocker") or "goal audit review")
        open_target = compact_path(goal_primary.get("artifact") or "reports/baselane_financials_goal_audit.md")
        action = str(goal_primary.get("next_action") or "resolve the primary goal-audit blocker; rerun EOD")
        hold = str(goal_primary.get("hold") or "Lofty PM publish or investor email")
    elif listing_cleanup_issue_count:
        blocker = f"Lofty listing cleanup queue ({listing_cleanup_issue_count} issues)"
        open_target = "reports/lofty_listing_update_cleanup_queue.json"
        action = "repair cleanup queue inputs; do not publish listing/email"
        run_command = f"python3 scripts/lofty_listing_update_cleanup_queue.py --live-update-capture-report reports/baselane_financials_monthly_live_update_capture.json --runtime-map reports/baselane_financials_monthly_lofty_pm_runtime_map.json --report reports/lofty_listing_update_cleanup_queue.json --publish-script skills/lofty-pm/scripts/publish_latest_update_to_lofty.py --review-candidate-packet-report reports/baselane_financials_monthly_review_candidate_packet.json --run-month {eod_run_month} --require-monthly-financial-summary"
        suppress_run_command = True
        hold = "Lofty PM publish or investor email"
    elif listing_cleanup_ready_count:
        blocker = f"Lofty listing copied-history cleanup ({listing_cleanup_ready_count} fields)"
        open_target = compact_path(listing_cleanup_summary.get("ready_cleanup_csv") or "reports/lofty_listing_update_cleanup_queue.json")
        action = "review CSV; dry-run cleaned-history commands; apply only after approval"
        run_command = f"python3 scripts/lofty_listing_update_cleanup_queue.py --live-update-capture-report reports/baselane_financials_monthly_live_update_capture.json --runtime-map reports/baselane_financials_monthly_lofty_pm_runtime_map.json --report reports/lofty_listing_update_cleanup_queue.json --publish-script skills/lofty-pm/scripts/publish_latest_update_to_lofty.py --review-candidate-packet-report reports/baselane_financials_monthly_review_candidate_packet.json --run-month {eod_run_month} --require-monthly-financial-summary"
        suppress_run_command = True
        hold = "Lofty PM publish or investor email"
    elif visibility_send_proof_only:
        open_target = (
            "reports/baselane_eod_telegram_send_state.json"
            if report_integrity_send_proof_only or eod_send_state_actionable
            else "reports/baselane_scheduler_audit_report.json"
        )
        blocker = "EOD Telegram send proof missing" if report_integrity_send_proof_only else "EOD Telegram send proof"
        action = "send digest-bound EOD via explicit flag or scheduled delivery"
        run_command = "python3 scripts/baselane_eod_telegram_report.py --send"
        hold = "failure visibility until Telegram send is proven"
    else:
        action = "none; keep cron running"
        hold = "none"

    issue_line = f"{issue_label}: {blocker}"
    goal_marker = goal_completion_marker(goal_audit) if compact_count(goal_audit.get("requirement_count")) else ""
    if goal_marker and not goal_ok:
        issue_line = f"{issue_line}; {goal_marker}"
    lines = [
        f"EOD: {title_status}",
        issue_line,
        f"OPEN: {open_target}",
        f"{action_label}: {action}",
    ]
    skip_line_needed = bool(owner_skipped_count)
    reserved_tail_lines = 2 + (1 if skip_line_needed else 0)
    run_line_fits = len(lines) + 1 + reserved_tail_lines <= EOD_ACTIONABLE_MESSAGE_MAX_LINES
    if run_command and not suppress_run_command and run_line_fits:
        lines.append(f"{command_label}: {run_command}")
    if skip_line_needed:
        lines.append(f"SKIP: {owner_skipped_count} excluded")
    rent_roll_primary_blocker = blocker.startswith(("rent-roll", "stale rent roll"))
    statement_also = monthly_statement_evidence_blocked and blocker != "monthly Baselane bank statements"
    mortgage_workflow_needs_review = (
        weekly_file_updates.get("mortgage_workflow_gate_status") == "review"
        or weekly_file_updates.get("mortgage_downloader_citadel_report_status") == "auth_failed"
        or compact_count(weekly_file_updates.get("mortgage_workflow_citadel_download_rc")) > 0
        or bool(weekly_file_updates.get("mortgage_downloader_citadel_credential_state_drift_suspected"))
    )
    secondary_lofty_auth = (
        lofty_live_guard_auth_blocked
        and not blocker.startswith("monthly owner automation")
        and not rent_roll_primary_blocker
    )
    secondary_hemlane_rent_roll = (
        rent_roll_needs_source
        and not rent_roll_primary_blocker
    )
    if secondary_hemlane_rent_roll or secondary_lofty_auth or statement_also or (
        mortgage_workflow_needs_review and not rent_roll_primary_blocker
    ):
        hold = hold if hold != "none" else "email/Lofty PM publish"
    if visibility_send_proof_only and blocker not in {"EOD Telegram send proof", "EOD Telegram send proof missing"}:
        hold = f"EOD proof; {hold}" if hold != "none" else "EOD proof"
    if hold in {"owner email or Lofty PM publish", "investor email or Lofty PM publish", "Lofty PM publish or investor email"}:
        hold = "email/Lofty PM publish"
    if hold == "EOD proof; owner email or Lofty PM publish":
        hold = "EOD proof; email/Lofty PM publish"
    elif hold == "EOD proof; investor email or Lofty PM publish":
        hold = "EOD proof; email/Lofty PM publish"
    elif hold == "EOD proof; Lofty PM publish or investor email":
        hold = "EOD proof; email/Lofty PM publish"
    monthly_accrual_hold_marker = monthly_live_accrual_hold_marker(monthly_close_status)
    if monthly_accrual_hold_marker and monthly_accrual_hold_marker not in hold:
        hold = monthly_accrual_hold_marker if hold in {"", "none"} else f"{monthly_accrual_hold_marker}; {hold}"
    hold = add_native_email_hold_marker(hold, lofty_pm_publish)
    lofty_guard_marker = lofty_guard_hold_marker(
        max(
            owner_live_update_unverified,
            guarded_apply_update_failed,
            live_capture_guard_problem_count(live_capture),
        ),
        max(
            owner_live_financial_unverified,
            guarded_apply_financial_failed,
            live_capture_guard_problem_count(live_financial_capture),
        ),
    )
    hold = add_lofty_guard_hold_marker(hold, lofty_guard_marker)
    guild_test_post = lofty_pm_publish.get("guild_test_post_snapshot")
    if (
        not lofty_guard_marker
        and
        hold in {
            "email/Lofty PM publish",
            "EOD proof; email/Lofty PM publish",
            "native email off; Lofty PM publish",
            "EOD proof; native email off; Lofty PM publish",
        }
        and isinstance(guild_test_post, dict)
        and guild_test_post.get("prepared") is True
        and guild_test_post.get("posted") is not True
        and guild_test_post.get("valid") is not True
    ):
        guild_suffix = guild_test_hold_suffix(guild_test_post)
        if hold.startswith("EOD proof; native email off"):
            hold = f"EOD proof; native email off; Lofty PM; {guild_suffix}"
        elif hold.startswith("EOD proof;"):
            hold = f"EOD proof; email/Lofty PM; {guild_suffix}"
        elif hold.startswith("native email off"):
            hold = f"native email off; Lofty PM; {guild_suffix}"
        else:
            hold = f"email/Lofty PM; {guild_suffix}"
    hold = add_owner_email_packet_hold_marker(hold, owner_email_packet_summary())
    hold = actionable_hold_summary(hold, blocker)
    if monthly_statement_expected_wait and rent_roll_needs_source and "statement wait" not in hold:
        hold = f"{hold}; statement wait"
    hold = add_listing_cleanup_hold_marker(hold, listing_cleanup_summary)
    hold = add_financial_patch_hold_marker(hold, lofty_financial_patch_readiness)
    cf_hold_parts = []
    if cf_missing_canonical:
        cf_hold_parts.append(f"{cf_missing_canonical}m")
    if cf_conflicts:
        cf_hold_parts.append(f"{cf_conflicts}c")
    if source_cash_violations:
        cf_hold_parts.append(f"{source_cash_violations}cash")
    if cf_audit_errors:
        if compact_count(cf_audit_error_classes.get("no_gl_property_match")) == cf_audit_errors:
            if cf_no_gl_active and cf_no_gl_total and cf_no_gl_active != cf_no_gl_total:
                cf_hold_parts.append(f"{cf_no_gl_active}glA/{cf_no_gl_total}glT")
            elif cf_no_gl_total:
                cf_hold_parts.append(f"{cf_no_gl_total}gl")
            else:
                cf_hold_parts.append(f"{cf_audit_errors}gl")
        else:
            cf_hold_parts.append(f"{cf_audit_errors}err")
    if cf_hold_parts and "CF" not in hold:
        cf_marker = "CF" + "/".join(cf_hold_parts)
        hold = cf_marker if hold == "none" else f"{hold}; {cf_marker}"
    native_split_mortgage_clean = (
        native_split_handled_count > 0
        and native_split_ready_count == 0
        and native_split_failure_count == 0
        and native_split_blocked_count == 0
    )
    if ecogl_raw_no_dao_mortgage_exceptions and "raw-mtg" not in hold and not native_split_mortgage_clean:
        ecogl_marker = f"ECO raw-mtg{ecogl_raw_no_dao_mortgage_exceptions}"
        hold = ecogl_marker if hold == "none" else f"{hold}; {ecogl_marker}"
    if not daily_ok and blocker in {
        "daily Baselane sync",
        "login recaptcha required",
        "login auth 401",
        "manual auth required",
    }:
        monthly_close_marker = monthly_close_hold_marker(monthly_close_status, monthly)
        hold = "weekly/monthly document updates"
        if monthly_close_marker:
            hold = f"{hold}; {monthly_close_marker}"
    lines.append(f"HOLD: {'none' if hold == 'none' else hold}")
    lines.append(daily_sync_status_line(daily, daily_job, daily_run=daily_run, refresh_health=refresh_health))
    message = "\n".join(lines)
    if len(message) > EOD_ACTIONABLE_MESSAGE_HOLD_COMPACT_CHARS:
        for index, line in enumerate(lines):
            if line.startswith("HOLD: "):
                lines[index] = f"HOLD: {budget_hold_summary(line.removeprefix('HOLD: '))}"
                message = "\n".join(lines)
                break
    if len(message) > EOD_ACTIONABLE_MESSAGE_MAX_CHARS:
        for index, line in enumerate(lines):
            if line.startswith("Sync: "):
                lines[index] = compact_daily_sync_status_line(line)
                message = "\n".join(lines)
                break
    if len(message) > EOD_ACTIONABLE_MESSAGE_MAX_CHARS:
        for index, line in enumerate(lines):
            if line.startswith(("DO: ", "NEXT: ")):
                lines[index] = compact_eod_action_line(line)
                message = "\n".join(lines)
                break
    if len(message) > EOD_ACTIONABLE_MESSAGE_MAX_CHARS:
        for index, line in enumerate(lines):
            if line.startswith("Sync: "):
                lines[index] = tight_compact_daily_sync_status_line(line)
                message = "\n".join(lines)
                break
    return clamp_eod_message(message)


def daily_sync_eod_summary(report_dir: Path | None = None) -> dict:
    report_dir = report_dir or REPORT_DIR
    sync_report_path = report_dir / BASELANE_DAILY_SYNC_REPORT.name
    run_report_path = report_dir / "baselane_daily_run_report.json"
    sync_report = read_json(sync_report_path)
    run_report = read_json(run_report_path)
    daily_run_age_hours = iso_age_hours(run_report.get("ended_at") or run_report.get("generated_at"))
    daily_run_fresh = daily_run_age_hours is not None and -1 <= daily_run_age_hours <= DAILY_RUN_REPORT_MAX_AGE_HOURS
    return {
        "daily_sync_report": str(sync_report_path),
        "daily_run_report": str(run_report_path),
        "daily_run_status": run_report.get("status"),
        "daily_run_return_code": run_report.get("return_code"),
        "daily_run_failed_step": run_report.get("failed_step"),
        "daily_run_generated_at": run_report.get("generated_at"),
        "daily_run_started_at": run_report.get("started_at"),
        "daily_run_ended_at": run_report.get("ended_at"),
        "daily_run_duration_seconds": run_report.get("duration_seconds"),
        "daily_run_age_hours": daily_run_age_hours,
        "daily_run_max_age_hours": DAILY_RUN_REPORT_MAX_AGE_HOURS,
        "daily_run_fresh": daily_run_fresh,
        "daily_run_workspace_root": run_report.get("workspace_root"),
        "daily_run_openclaw_root": run_report.get("openclaw_root"),
        "daily_run_foreign_workspace_root": sync_report.get("daily_run_foreign_workspace_root"),
        "daily_run_workspace_root_aliases_current": sync_report.get("daily_run_workspace_root_aliases_current"),
        "daily_run_workspace_root_raw_matches_current": sync_report.get("daily_run_workspace_root_raw_matches_current"),
        "daily_run_workspace_root_matches_current": sync_report.get("daily_run_workspace_root_matches_current"),
        "status": sync_report.get("status"),
        "effective_status": sync_report.get("effective_status"),
        "issue_count": compact_count(sync_report.get("issue_count")),
        "return_code": sync_report.get("return_code"),
        "effective_return_code": sync_report.get("effective_return_code"),
        "failed_step": sync_report.get("failed_step"),
        "effective_failed_step": sync_report.get("effective_failed_step"),
        "sync_report_status": sync_report.get("sync_report_status"),
        "deterministic_sync_original_status": sync_report.get("deterministic_sync_original_status"),
        "deterministic_sync_recovery_status": sync_report.get("deterministic_sync_recovery_status"),
        "deterministic_sync_recovered_by": sync_report.get("deterministic_sync_recovered_by"),
        "deterministic_sync_recovery_report": sync_report.get("deterministic_sync_recovery_report"),
        "daily_wrapper_failure_window_hours": sync_report.get("daily_wrapper_failure_window_hours"),
        "daily_wrapper_failure_distinct_run_count": sync_report.get("daily_wrapper_failure_distinct_run_count"),
        "daily_wrapper_failure_records_bounded": sync_report.get("daily_wrapper_failure_records_bounded") or [],
        "daily_wrapper_failure_last_record": sync_report.get("daily_wrapper_failure_last_record"),
        "daily_wrapper_failure_last_ended_at": sync_report.get("daily_wrapper_failure_last_ended_at"),
        "daily_wrapper_failure_last_failed_step": sync_report.get("daily_wrapper_failure_last_failed_step"),
        "daily_recovered_sync_repeat_count": sync_report.get("daily_recovered_sync_repeat_count"),
        "assetrail_live_status": sync_report.get("assetrail_live_status"),
        "source_cash_balance_status": sync_report.get("source_cash_balance_status"),
        "source_cash_balance_report_fresh": sync_report.get("source_cash_balance_report_fresh"),
        "source_cash_balance_update_count": compact_count(sync_report.get("source_cash_balance_update_count")),
        "source_cash_balance_violation_count": compact_count(sync_report.get("source_cash_balance_violation_count")),
        "monthly_statements_gate_status": sync_report.get("monthly_statements_gate_status"),
        "monthly_statements_gate_reason": sync_report.get("monthly_statements_gate_reason"),
        "monthly_statements_gate_action": sync_report.get("monthly_statements_gate_action"),
        "monthly_statements_gate_expected_wait": sync_report.get("monthly_statements_gate_expected_wait"),
        "monthly_statements_gate_captured_unique_count": compact_count(
            sync_report.get("monthly_statements_gate_captured_unique_count")
        ),
        "monthly_statements_gate_min_captured_required": compact_count(
            sync_report.get("monthly_statements_gate_min_captured_required")
        ),
        "monthly_statement_staging_status": sync_report.get("monthly_statement_staging_status"),
        "local_model_ready": sync_report.get("local_model_ready"),
        "next_action": sync_report.get("next_action"),
    }


def weekly_file_updates_eod_summary(report_dir: Path | None = None) -> dict:
    report_dir = report_dir or REPORT_DIR
    run_report_path = report_dir / "baselane_weekly_file_updates_run_report.json"
    disk_report_path = report_dir / "baselane_weekly_disk_space_preflight_report.json"
    run_report = read_json(run_report_path)
    disk_report = read_json(disk_report_path)
    primary_blocker = run_report.get("primary_blocker") if isinstance(run_report.get("primary_blocker"), dict) else {}
    started_at = run_report.get("started_at") or run_report.get("generated_at")
    return {
        "weekly_file_updates_report": str(run_report_path),
        "weekly_disk_space_preflight_report": str(disk_report_path),
        "status": run_report.get("status"),
        "reason": run_report.get("reason"),
        "return_code": run_report.get("return_code"),
        "started_at": run_report.get("started_at"),
        "generated_at": run_report.get("generated_at"),
        "age_hours": iso_age_hours(started_at),
        "next_action": run_report.get("next_action") or primary_blocker.get("next_action"),
        "hold": run_report.get("hold") or primary_blocker.get("hold"),
        "primary_blocker_id": primary_blocker.get("id"),
        "primary_blocker_artifact": primary_blocker.get("artifact"),
        "disk_space_preflight_status": run_report.get("disk_space_preflight_status") or disk_report.get("status"),
        "disk_space_preflight_required_free_mib": first_present(
            run_report.get("disk_space_preflight_required_free_mib"),
            disk_report.get("required_free_mib"),
        ),
        "stale_downstream_gate_suppressed": run_report.get("stale_downstream_gate_suppressed") is True,
        "state_file_marked_complete": run_report.get("state_file_marked_complete"),
        "deterministic_verification_idempotent_reason": run_report.get("deterministic_verification_idempotent_reason"),
    }


def monthly_financials_eod_summary(report_dir: Path | None = None) -> dict:
    report_dir = report_dir or REPORT_DIR
    run_report_path = report_dir / "baselane_financials_monthly_run_report.json"
    close_status_path = report_dir / "baselane_financials_monthly_close_status.json"
    disk_report_path = report_dir / "baselane_financials_monthly_disk_space_preflight_report.json"
    statement_gate_path = report_dir / "baselane_monthly_statements_idempotent_report.json"
    recovery_report_path = report_dir / BASELANE_MONTHLY_RECOVERY_REPORT.name
    run_report = read_json(run_report_path)
    close_status = read_json(close_status_path)
    disk_report = read_json(disk_report_path)
    statement_gate = read_json(statement_gate_path)
    recovery_report = read_json(recovery_report_path)
    started_at = run_report.get("started_at") or run_report.get("generated_at")
    return {
        "monthly_financials_report": str(run_report_path),
        "monthly_close_status_report": str(close_status_path),
        "monthly_disk_space_preflight_report": str(disk_report_path),
        "monthly_statement_gate_report": str(statement_gate_path),
        "monthly_recovery_report": str(recovery_report_path),
        "status": run_report.get("status"),
        "effective_status": run_report.get("effective_status"),
        "close_status": close_status.get("status"),
        "failed_step": run_report.get("failed_step"),
        "effective_failed_step": run_report.get("effective_failed_step"),
        "close_failed_step": close_status.get("failed_step"),
        "monthly_completion_gap_count": first_present(
            close_status.get("monthly_completion_gap_count"),
            run_report.get("monthly_completion_gap_count"),
        ),
        "monthly_blocker_command_index_count": first_present(
            close_status.get("monthly_blocker_command_index_count"),
            len(run_report.get("monthly_blocker_command_index") or [])
            if isinstance(run_report.get("monthly_blocker_command_index"), list)
            else None,
        ),
        "monthly_blocker_ready_manual_count": first_present(
            close_status.get("monthly_blocker_ready_manual_count"),
            run_report.get("monthly_blocker_ready_manual_count"),
        ),
        "monthly_blocker_safe_auto_count": first_present(
            close_status.get("monthly_blocker_safe_auto_count"),
            run_report.get("monthly_blocker_safe_auto_count"),
        ),
        "monthly_close_hold_marker": monthly_close_hold_marker(close_status, run_report),
        "monthly_recovery_marker": monthly_recovery_eod_marker(recovery_report),
        "monthly_recovery_status": recovery_report.get("status"),
        "monthly_recovery_eligible": recovery_report.get("eligible"),
        "reason": run_report.get("reason"),
        "run_month": run_report.get("run_month"),
        "started_at": run_report.get("started_at"),
        "generated_at": run_report.get("generated_at"),
        "age_hours": iso_age_hours(started_at),
        "next_action": run_report.get("next_action"),
        "disk_space_preflight_status": run_report.get("disk_space_preflight_status") or disk_report.get("status"),
        "disk_space_preflight_required_free_mib": first_present(
            run_report.get("disk_space_preflight_required_free_mib"),
            disk_report.get("required_free_mib"),
        ),
        "monthly_statements_gate_status": run_report.get("monthly_statements_gate_status") or statement_gate.get("status"),
        "monthly_statements_gate_reason": run_report.get("monthly_statements_gate_reason") or statement_gate.get("reason"),
        "lofty_pm_publish_status": run_report.get("lofty_pm_publish_status"),
        "owner_email_packet_status": run_report.get("owner_email_packet_status"),
        "owner_email_send_guard_status": run_report.get("owner_email_send_guard_status"),
        "owner_email_allowed": run_report.get("owner_email_allowed"),
        "publish_updates_enabled": run_report.get("publish_lofty_pm_updates"),
        "send_owner_emails_enabled": run_report.get("send_owner_emails"),
    }


def telegram_request(endpoint: str, token: str, payload: dict | None = None, attempts: int = 3) -> dict:
    encoded_payload = urllib.parse.urlencode(payload).encode("utf-8") if payload else None
    method = "POST" if encoded_payload else "GET"
    last_error = "unknown telegram request failure"
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{endpoint}",
            data=encoded_payload,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8", errors="replace")
                return {
                    "attempts": attempt,
                    "http_status": response.status,
                    "body": json.loads(body),
                }
        except urllib.error.HTTPError as exc:
            last_error = f"telegram {endpoint} HTTP {exc.code}"
            if 400 <= exc.code < 500 and exc.code != 429:
                break
        except Exception as exc:
            last_error = f"telegram {endpoint} request failed: {exc.__class__.__name__}: {exc}"
        if attempt < attempts:
            time.sleep(min(2 ** (attempt - 1), 4))
    raise RuntimeError(last_error)


def message_quality(message: str) -> dict:
    nonblank_lines = [line for line in message.splitlines() if line.strip()]
    character_count = len(message)
    noise_markers = [marker for marker in EOD_NOISE_MARKERS if marker in message]
    issues: list[str] = []
    if len(nonblank_lines) > EOD_ACTIONABLE_MESSAGE_MAX_LINES:
        issues.append(f"too_many_lines={len(nonblank_lines)}")
    if character_count > EOD_ACTIONABLE_MESSAGE_MAX_CHARS:
        issues.append(f"too_many_chars={character_count}")
    if noise_markers:
        issues.append(f"noise_markers={','.join(noise_markers)}")
    return {
        "ok": not issues,
        "line_count": len(nonblank_lines),
        "character_count": character_count,
        "max_lines": EOD_ACTIONABLE_MESSAGE_MAX_LINES,
        "max_chars": EOD_ACTIONABLE_MESSAGE_MAX_CHARS,
        "noise_markers": noise_markers,
        "issues": issues,
    }


def telegram_send_approval(message: str, *, required: bool) -> dict:
    approval_value = str(os.environ.get(EOD_TELEGRAM_SEND_APPROVAL_ENV) or "").strip()
    expected_digest = str(os.environ.get(EOD_TELEGRAM_SEND_DIGEST_ENV) or "").strip().lower()
    message_digest = sha256_text(message)
    issues: list[str] = []
    approval_ok = approval_value == "1"
    if required and not approval_ok:
        issues.append(f"{EOD_TELEGRAM_SEND_APPROVAL_ENV}=1 required")
    digest_present = bool(expected_digest)
    digest_format_ok = not digest_present or bool(re.fullmatch(r"[0-9a-f]{64}", expected_digest))
    digest_matches = digest_present and expected_digest == message_digest
    if digest_present and not digest_format_ok:
        issues.append(f"{EOD_TELEGRAM_SEND_DIGEST_ENV} must be a 64-char sha256")
    if digest_present and digest_format_ok and not digest_matches:
        issues.append(f"{EOD_TELEGRAM_SEND_DIGEST_ENV} does not match message")
    return {
        "required": required,
        "ok": (not required or approval_ok) and digest_format_ok and (not digest_present or digest_matches),
        "approval_env_var": EOD_TELEGRAM_SEND_APPROVAL_ENV,
        "approval_value_present": bool(approval_value),
        "approval_ok": approval_ok,
        "digest_env_var": EOD_TELEGRAM_SEND_DIGEST_ENV,
        "digest_present": digest_present,
        "digest_format_ok": digest_format_ok,
        "digest_matches": digest_matches,
        "message_sha256": message_digest,
        "expected_message_sha256": expected_digest,
        "issues": issues,
    }


def apply_telegram_send_approval(report: dict, message: str, *, required: bool) -> dict:
    approval = telegram_send_approval(message, required=required)
    report["telegram_send_approval"] = approval
    report["telegram_send_approval_ok"] = approval["ok"]
    report["telegram_send_message_sha256"] = approval["message_sha256"]
    report["telegram_send_expected_message_sha256"] = approval["expected_message_sha256"]
    return approval


def eod_message_status(message: str) -> str:
    first_line = next((line.strip() for line in str(message or "").splitlines() if line.strip()), "")
    if not first_line.lower().startswith("eod:"):
        return "unknown"
    status = first_line.split(":", 1)[1].strip().lower()
    return status or "unknown"


def update_eod_status_fields(report: dict) -> None:
    message_status = eod_message_status(str(report.get("message") or ""))
    quality = report.get("message_quality") if isinstance(report.get("message_quality"), dict) else {}
    quality_ok = bool(quality.get("ok"))
    delivery_proven = (
        bool(report.get("send_requested"))
        and not bool(report.get("dry_run"))
        and bool(report.get("telegram_send_ok"))
        and bool(report.get("telegram_last_successful_send_scheduler_usable"))
    )
    if report.get("status") == "failed":
        workflow_status = "failed"
    elif message_status == "ok" and quality_ok:
        workflow_status = "ok"
    elif message_status == "blocked":
        workflow_status = "review"
    else:
        workflow_status = "review"
    if delivery_proven:
        delivery_status = "proven"
    elif bool(report.get("dry_run")):
        delivery_status = "preview_only"
    elif bool(report.get("send_requested")):
        delivery_status = "send_requested_unproven"
    else:
        delivery_status = "not_requested"
    report.update(
        {
            "eod_message_status": message_status,
            "eod_workflow_status": workflow_status,
            "eod_action_required": workflow_status != "ok" or not delivery_proven,
            "telegram_delivery_proven": delivery_proven,
            "telegram_delivery_status": delivery_status,
        }
    )
    if workflow_status != "ok" and report.get("status") != "failed":
        if report.get("effective_status") in {None, "", "ok"}:
            report["effective_status"] = workflow_status
        if not report.get("effective_failed_step"):
            report["effective_failed_step"] = "eod_message"
    elif workflow_status == "ok" and report.get("effective_status") in {None, ""}:
        report["effective_status"] = report.get("status")


def post_telegram(token: str, chat_id: str, text: str) -> dict:
    return telegram_request(
        "sendMessage",
        token,
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        },
    )


def chunk_message(text: str, limit: int = TELEGRAM_SAFE_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    body_limit = max(1, limit - 20)
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= body_limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > body_limit:
            chunks.append(line[:body_limit])
            line = line[body_limit:]
        current = line
    if current:
        chunks.append(current)
    total = len(chunks)
    if total <= 1:
        return chunks
    labeled = []
    for index, chunk in enumerate(chunks, start=1):
        prefix = f"({index}/{total}) "
        if len(prefix) + len(chunk) <= limit:
            labeled.append(prefix + chunk)
        else:
            labeled.append(prefix + chunk[: limit - len(prefix)])
    return labeled


def check_telegram(token: str) -> dict:
    result = telegram_request("getMe", token)
    parsed = result.get("body") or {}
    bot = (parsed.get("result") or {}).get("username")
    return {
        "attempts": result.get("attempts"),
        "http_status": result.get("http_status"),
        "ok": parsed.get("ok"),
        "bot": bot,
    }


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def eod_send_state_report() -> Path:
    return REPORT_DIR / EOD_SEND_STATE_REPORT.name


def eod_send_proof_dir() -> Path:
    return REPORT_DIR / EOD_SEND_PROOF_DIR.name


def eod_message_preview_report() -> Path:
    return REPORT_DIR / EOD_MESSAGE_PREVIEW_REPORT.name


def eod_out_report() -> Path:
    return REPORT_DIR / OUT_REPORT.name


def eod_dry_run_json_report() -> Path:
    return REPORT_DIR / EOD_DRY_RUN_JSON_REPORT.name


def write_successful_send_source_report(report: dict) -> Path:
    generated_at = str(report.get("generated_at") or iso_z())
    safe_generated_at = re.sub(r"[^0-9A-Za-z_-]+", "-", generated_at).strip("-") or "unknown"
    proof_dir = eod_send_proof_dir()
    proof_dir.mkdir(parents=True, exist_ok=True)
    proof_path = proof_dir / f"baselane_eod_telegram_report.{safe_generated_at}.json"
    proof_payload = dict(report)
    proof_payload["send_proof_snapshot"] = True
    proof_payload["send_proof_snapshot_path"] = str(proof_path)
    proof_path.write_text(json.dumps(proof_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return proof_path


def write_successful_send_state(report: dict, state_path: Path | None = None) -> dict:
    state_path = state_path or eod_send_state_report()
    message = str(report.get("message") or "")
    source_report = write_successful_send_source_report(report)
    state = {
        "status": "ok",
        "generated_at": report.get("generated_at"),
        "last_successful_send_at": report.get("generated_at"),
        "dry_run": False,
        "send_requested": True,
        "telegram_send_ok": True,
        "telegram_http_statuses": report.get("telegram_http_statuses") or [],
        "message": message,
        "source_report_generated_at": report.get("generated_at"),
        "source_report_message_sha256": sha256_text(message),
        "telegram_sent_message_sha256": sha256_text(message),
        "message_character_count": report.get("message_character_count"),
        "message_quality": report.get("message_quality") or {},
        "message_chunk_count": report.get("message_chunk_count"),
        "message_chunk_character_counts": report.get("message_chunk_character_counts") or [],
        "source_report": str(source_report),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state


def successful_send_state_source_report_scope_ok(send_state: dict, root: Path | None = None) -> bool:
    source_report = str(send_state.get("source_report") or "").strip()
    if not source_report:
        return False
    root = (root or ROOT).expanduser().absolute()
    source_path = Path(source_report).expanduser()
    if not source_path.is_absolute():
        source_path = root / source_path
    try:
        source_path.absolute().relative_to(root)
    except ValueError:
        return False
    return True


def successful_send_state_usable_for_scheduler(send_state: dict, quality: dict, root: Path | None = None) -> bool:
    message = str(send_state.get("message") or "")
    digest = str(send_state.get("source_report_message_sha256") or "")
    return bool(
        send_state.get("status") == "ok"
        and send_state.get("dry_run") is False
        and send_state.get("send_requested") is True
        and send_state.get("telegram_send_ok") is True
        and bool(send_state.get("telegram_http_statuses") or [])
        and quality.get("ok") is True
        and re.fullmatch(r"[0-9a-f]{64}", digest)
        and digest == sha256_text(message)
        and str(send_state.get("source_report_generated_at") or "").strip()
        and successful_send_state_source_report_scope_ok(send_state, root)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually send the EOD Telegram message. Without this flag the script only writes reports/previews.",
    )
    parser.add_argument("--check-token", action="store_true")
    args = parser.parse_args()
    preview_only = bool(args.dry_run)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    send_state_path = eod_send_state_report()
    token, chat_id = telegram_config()
    daily_disk_space_preflight_refresh = refresh_daily_disk_space_preflight()
    source_cash_balance_audit_refresh = refresh_daily_source_cash_balance_audit()
    daily_sync_report_refresh = refresh_daily_sync_report()
    source_cash_balance_report = read_json(current_report_path(BASELANE_DAILY_SOURCE_CASH_BALANCE_REPORT))
    source_cash_downstream_blocked = (
        bool(source_cash_balance_report.get("apply_blocked_by_raw_no_dao_mortgage_guard"))
        or compact_count(source_cash_balance_report.get("violation_count")) > 0
    )
    daily_sync_disk_space_blocker_reason_value = daily_sync_disk_space_blocker_reason()
    daily_sync_disk_space_blocked = daily_sync_disk_space_blocker_reason_value is not None
    daily_sync_auth_blocker_reason_value = None if daily_sync_disk_space_blocked else daily_sync_auth_blocker_reason()
    daily_sync_auth_blocked = daily_sync_auth_blocker_reason_value is not None
    daily_sync_blocker_skip_reason = (
        "daily_sync_disk_space_blocker"
        if daily_sync_disk_space_blocked
        else "daily_sync_auth_blocker"
        if daily_sync_auth_blocked
        else "source_cash_balance_blocker"
        if source_cash_downstream_blocked
        else None
    )
    daily_sync_blocker_reason_value = (
        daily_sync_disk_space_blocker_reason_value
        or daily_sync_auth_blocker_reason_value
        or ("source_cash_balance_blocked" if source_cash_downstream_blocked else None)
    )
    daily_sync_blocked = daily_sync_blocker_skip_reason is not None
    if daily_sync_blocked:
        skipped = skipped_refresh(
            daily_sync_blocker_skip_reason,
            blocker=daily_sync_blocker_reason_value,
        )
        lofty_cdp_ensure_refresh = dict(skipped)
        lofty_cdp_preflight_refresh = dict(skipped)
        hemlane_cdp_preflight_refresh = dict(skipped)
        hemlane_cdp_capture_refresh = dict(skipped)
        public_path_guard_refresh = dict(skipped)
        discord_public_financial_source_guard_refresh = dict(skipped)
        local_model_preflight_refresh = refresh_local_model_preflight() if daily_sync_auth_blocked else dict(skipped)
        scheduler_audit_refresh = dict(skipped)
        weekly_report_reconcile = dict(skipped)
        source_fix_action_queue_refresh = refresh_source_fix_action_queue() if source_cash_downstream_blocked else dict(skipped)
        source_fix_apply_dry_run_refresh = refresh_source_fix_apply_dry_run() if source_cash_downstream_blocked else dict(skipped)
        native_split_plan_refresh = refresh_native_split_plan() if source_cash_downstream_blocked else dict(skipped)
        monthly_owner_review_gate_refresh = (
            refresh_monthly_owner_review_gate() if daily_sync_auth_blocked else dict(skipped)
        )
        monthly_readiness_refresh = dict(skipped)
        monthly_owner_review_gate_post_readiness_refresh = dict(skipped)
        if daily_sync_auth_blocked:
            owner_email_packet_refresh = refresh_owner_email_packet()
            owner_email_send_guard_refresh = refresh_owner_email_send_guard()
        else:
            owner_email_packet_refresh = dict(skipped)
            owner_email_send_guard_refresh = dict(skipped)
        lofty_empty_updates_backfill_queue_refresh = dict(skipped)
        lofty_listing_cleanup_queue_refresh = dict(skipped)
        unreviewed_financial_quarantine_refresh = dict(skipped)
        no_mortgage_financials_guard_refresh = dict(skipped)
        operations_packet_refresh = refresh_operations_packet() if daily_sync_auth_blocked else dict(skipped)
        goal_audit_refresh = dict(skipped)
        monthly_run_gate_backfill = backfill_monthly_run_gate_fields()
    else:
        lofty_cdp_ensure_refresh = refresh_lofty_cdp_ensure()
        lofty_cdp_preflight_refresh = refresh_lofty_cdp_preflight()
        hemlane_cdp_preflight_refresh = refresh_hemlane_cdp_preflight()
        hemlane_cdp_capture_refresh = refresh_hemlane_cdp_capture()
        public_path_guard_refresh = refresh_lofty_public_path_guard()
        discord_public_financial_source_guard_refresh = (
            skipped_refresh("source_cash_balance_blocker")
            if source_cash_downstream_blocked
            else refresh_discord_public_financial_source_guard()
        )
        local_model_preflight_refresh = refresh_local_model_preflight()
        scheduler_audit_refresh = refresh_scheduler_audit()
        weekly_report_reconcile = refresh_weekly_report_reconcile()
        source_fix_action_queue_refresh = refresh_source_fix_action_queue()
        source_fix_apply_dry_run_refresh = refresh_source_fix_apply_dry_run()
        native_split_plan_refresh = refresh_native_split_plan()
        monthly_owner_review_gate_refresh = refresh_monthly_owner_review_gate()
        monthly_readiness_refresh = refresh_monthly_readiness()
        monthly_owner_review_gate_post_readiness_refresh = refresh_monthly_owner_review_gate()
        owner_email_packet_refresh = refresh_owner_email_packet()
        owner_email_send_guard_refresh = refresh_owner_email_send_guard()
        lofty_empty_updates_backfill_queue_refresh = refresh_lofty_empty_updates_backfill_queue()
        lofty_listing_cleanup_queue_refresh = (
            skipped_refresh("source_cash_balance_blocker")
            if source_cash_downstream_blocked
            else refresh_lofty_listing_cleanup_queue()
        )
        unreviewed_financial_quarantine_refresh = refresh_unreviewed_financial_quarantine()
        no_mortgage_financials_guard_refresh = refresh_no_mortgage_financials_guard()
        operations_packet_refresh = refresh_operations_packet()
        goal_audit_refresh = refresh_goal_audit()
        monthly_run_gate_backfill = backfill_monthly_run_gate_fields()
    initial_refresh_health = refresh_health_summary(
        {
            "lofty_cdp_ensure_refresh": lofty_cdp_ensure_refresh,
            "lofty_cdp_preflight_refresh": lofty_cdp_preflight_refresh,
            "hemlane_cdp_preflight_refresh": hemlane_cdp_preflight_refresh,
            "hemlane_cdp_capture_refresh": hemlane_cdp_capture_refresh,
            "public_path_guard_refresh": public_path_guard_refresh,
            "discord_public_financial_source_guard_refresh": discord_public_financial_source_guard_refresh,
            "local_model_preflight_refresh": local_model_preflight_refresh,
            "scheduler_audit_refresh": scheduler_audit_refresh,
            "daily_disk_space_preflight_refresh": daily_disk_space_preflight_refresh,
            "source_cash_balance_audit_refresh": source_cash_balance_audit_refresh,
            "daily_sync_report_refresh": daily_sync_report_refresh,
            "weekly_report_reconcile": weekly_report_reconcile,
            "source_fix_action_queue_refresh": source_fix_action_queue_refresh,
            "source_fix_apply_dry_run_refresh": source_fix_apply_dry_run_refresh,
            "native_split_plan_refresh": native_split_plan_refresh,
            "monthly_owner_review_gate_refresh": monthly_owner_review_gate_refresh,
            "monthly_readiness_refresh": monthly_readiness_refresh,
            "monthly_owner_review_gate_post_readiness_refresh": monthly_owner_review_gate_post_readiness_refresh,
            "owner_email_send_guard_refresh": owner_email_send_guard_refresh,
            "owner_email_packet_refresh": owner_email_packet_refresh,
            "lofty_empty_updates_backfill_queue_refresh": lofty_empty_updates_backfill_queue_refresh,
            "lofty_listing_cleanup_queue_refresh": lofty_listing_cleanup_queue_refresh,
            "unreviewed_financial_quarantine_refresh": unreviewed_financial_quarantine_refresh,
            "no_mortgage_financials_guard_refresh": no_mortgage_financials_guard_refresh,
            "operations_packet_refresh": operations_packet_refresh,
            "goal_audit_refresh": goal_audit_refresh,
            "monthly_run_gate_backfill": monthly_run_gate_backfill,
        }
    )
    message = build_message(send_requested=args.send and not args.dry_run, refresh_health=initial_refresh_health)
    message_chunks = chunk_message(message)
    generated_at = iso_z()
    message_preview_path = eod_message_preview_report()
    canonical_report_path = eod_out_report()
    preview_json_report_path = eod_dry_run_json_report()
    out_report_path = preview_json_report_path if preview_only else canonical_report_path
    telegram_last_successful_send = read_json(send_state_path)
    telegram_last_successful_send_quality = message_quality(str(telegram_last_successful_send.get("message") or ""))
    active_report_root = REPORT_DIR.parent
    telegram_last_successful_send_scope_ok = successful_send_state_source_report_scope_ok(
        telegram_last_successful_send,
        active_report_root,
    )
    telegram_last_successful_send_scheduler_usable = successful_send_state_usable_for_scheduler(
        telegram_last_successful_send,
        telegram_last_successful_send_quality,
        active_report_root,
    )
    current_owner_email_packet_summary = owner_email_packet_summary()
    current_daily_sync_summary = daily_sync_eod_summary()
    current_weekly_file_updates_summary = weekly_file_updates_eod_summary()
    current_monthly_financials_summary = monthly_financials_eod_summary()
    current_listing_cleanup_queue_summary = lofty_listing_cleanup_queue_summary(
        read_json(current_report_path(LOFTY_LISTING_CLEANUP_QUEUE_REPORT)),
        read_json(current_report_path(LOFTY_LISTING_CLEANUP_DRY_RUN_VERIFY_REPORT)),
    )
    current_goal_audit_summary = goal_audit_summary(read_json(current_report_path(BASELANE_GOAL_AUDIT_REPORT)))
    report = {
        "status": "ok",
        "generated_at": generated_at,
        "checked_at": generated_at,
        "dry_run": preview_only,
        "send_requested": args.send,
        "check_token": args.check_token,
        "telegram_token_present": bool(token),
        "telegram_chat_id_present": bool(chat_id),
        "telegram_send_ok": False,
        "telegram_http_statuses": [],
        "telegram_send_mode": "send" if args.send and not args.dry_run else "dry_run",
        "telegram_send_blocked_reason": None if args.send or args.dry_run else "missing_explicit_send_flag",
        "telegram_send_state_path": str(send_state_path),
        "telegram_last_successful_send": telegram_last_successful_send,
        "telegram_last_successful_send_quality": telegram_last_successful_send_quality,
        "telegram_last_successful_send_scope_ok": telegram_last_successful_send_scope_ok,
        "telegram_last_successful_send_scheduler_usable": telegram_last_successful_send_scheduler_usable,
        "daily_sync_disk_space_blocked": daily_sync_disk_space_blocked,
        "daily_sync_disk_space_blocker_reason": daily_sync_disk_space_blocker_reason_value,
        "daily_sync_auth_blocked": daily_sync_auth_blocked,
        "daily_sync_auth_blocker_reason": daily_sync_auth_blocker_reason_value,
        "lofty_cdp_ensure_refresh": lofty_cdp_ensure_refresh,
        "lofty_cdp_preflight_refresh": lofty_cdp_preflight_refresh,
        "hemlane_cdp_preflight_refresh": hemlane_cdp_preflight_refresh,
        "hemlane_cdp_capture_refresh": hemlane_cdp_capture_refresh,
        "public_path_guard_refresh": public_path_guard_refresh,
        "discord_public_financial_source_guard_refresh": discord_public_financial_source_guard_refresh,
        "local_model_preflight_refresh": local_model_preflight_refresh,
        "scheduler_audit_refresh": scheduler_audit_refresh,
        "daily_disk_space_preflight_refresh": daily_disk_space_preflight_refresh,
        "source_cash_balance_audit_refresh": source_cash_balance_audit_refresh,
        "daily_sync_report_refresh": daily_sync_report_refresh,
        "daily_sync_summary": current_daily_sync_summary,
        "weekly_file_updates_summary": current_weekly_file_updates_summary,
        "monthly_financials_summary": current_monthly_financials_summary,
        "daily_run_summary": current_daily_sync_summary,
        "weekly_run_summary": current_weekly_file_updates_summary,
        "monthly_run_summary": current_monthly_financials_summary,
        "owner_exclusion_summary": monthly_owner_exclusion_summary(),
        "weekly_report_reconcile": weekly_report_reconcile,
        "source_fix_action_queue_refresh": source_fix_action_queue_refresh,
        "source_fix_apply_dry_run_refresh": source_fix_apply_dry_run_refresh,
        "native_split_plan_refresh": native_split_plan_refresh,
        "monthly_owner_review_gate_refresh": monthly_owner_review_gate_refresh,
        "monthly_readiness_refresh": monthly_readiness_refresh,
        "monthly_owner_review_gate_post_readiness_refresh": monthly_owner_review_gate_post_readiness_refresh,
        "owner_email_send_guard_refresh": owner_email_send_guard_refresh,
        "owner_email_packet_refresh": owner_email_packet_refresh,
        "owner_email_packet_summary": current_owner_email_packet_summary,
        "owner_email_gap_summary": owner_email_gap_summary(
            current_owner_email_packet_summary,
            current_listing_cleanup_queue_summary,
        ),
        "lofty_empty_updates_backfill_queue_refresh": lofty_empty_updates_backfill_queue_refresh,
        "lofty_empty_updates_backfill_queue_summary": lofty_empty_updates_backfill_queue_summary(),
        "lofty_listing_cleanup_queue_refresh": lofty_listing_cleanup_queue_refresh,
        "lofty_listing_cleanup_queue_summary": current_listing_cleanup_queue_summary,
        "unreviewed_financial_quarantine_refresh": unreviewed_financial_quarantine_refresh,
        "unreviewed_financial_quarantine_summary": unreviewed_financial_quarantine_summary(),
        "no_mortgage_financials_guard_refresh": no_mortgage_financials_guard_refresh,
        "operations_packet_refresh": operations_packet_refresh,
        "goal_audit_refresh": goal_audit_refresh,
        "goal_audit_summary": current_goal_audit_summary,
        "monthly_run_gate_backfill": monthly_run_gate_backfill,
        "report_integrity_guard_refresh": {"attempted": False, "reason": "post_write_refresh_pending"},
        "message_preview": message_chunks[0][:4000],
        "message": message,
        "message_chunks": message_chunks,
        "message_character_count": len(message),
        "message_quality": message_quality(message),
        "telegram_message_limit": TELEGRAM_SAFE_MESSAGE_LIMIT,
        "message_chunk_count": len(message_chunks),
        "message_chunk_character_counts": [len(chunk) for chunk in message_chunks],
        "report_path": str(out_report_path),
        "canonical_report_path": str(canonical_report_path),
        "preview_json_report_path": str(preview_json_report_path),
        "canonical_report_written": not preview_only,
        "message_preview_path": str(message_preview_path),
    }
    apply_telegram_send_approval(report, message, required=args.send and not args.dry_run)
    apply_refresh_health(report)
    def write_current_report() -> None:
        update_eod_status_fields(report)
        serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
        out_report_path.write_text(serialized, encoding="utf-8")
        if out_report_path != preview_json_report_path:
            preview_json_report_path.write_text(serialized, encoding="utf-8")

    telegram_credentials_required = bool((args.send and not args.dry_run) or args.check_token)
    try:
        if telegram_credentials_required and not token:
            raise RuntimeError("missing telegram bot token")
        if telegram_credentials_required and not chat_id:
            raise RuntimeError("missing telegram chat id")
        if args.check_token:
            report["telegram_check"] = check_telegram(token)
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
    message_preview_path.write_text(message + "\n", encoding="utf-8")
    write_current_report()
    report["report_integrity_guard_refresh"] = (
        skipped_refresh(daily_sync_blocker_skip_reason, blocker=daily_sync_blocker_reason_value)
        if daily_sync_blocked
        else refresh_report_integrity_guard()
    )
    refresh_health = apply_refresh_health(report)
    message = build_message(send_requested=args.send and not args.dry_run, refresh_health=refresh_health)
    message_chunks = chunk_message(message)
    report.update(
        {
            "message_preview": message_chunks[0][:4000],
            "message": message,
            "message_chunks": message_chunks,
            "message_character_count": len(message),
            "message_quality": message_quality(message),
            "message_chunk_count": len(message_chunks),
            "message_chunk_character_counts": [len(chunk) for chunk in message_chunks],
        }
    )
    apply_telegram_send_approval(report, message, required=args.send and not args.dry_run)
    message_preview_path.write_text(message + "\n", encoding="utf-8")
    write_current_report()
    report["report_integrity_guard_refresh"] = (
        skipped_refresh(daily_sync_blocker_skip_reason, blocker=daily_sync_blocker_reason_value)
        if daily_sync_blocked
        else refresh_report_integrity_guard()
    )
    refresh_health = apply_refresh_health(report)
    message = build_message(send_requested=args.send and not args.dry_run, refresh_health=refresh_health)
    message_chunks = chunk_message(message)
    report.update(
        {
            "message_preview": message_chunks[0][:4000],
            "message": message,
            "message_chunks": message_chunks,
            "message_character_count": len(message),
            "message_quality": message_quality(message),
            "message_chunk_count": len(message_chunks),
            "message_chunk_character_counts": [len(chunk) for chunk in message_chunks],
        }
    )
    apply_telegram_send_approval(report, message, required=args.send and not args.dry_run)
    apply_refresh_health(report)
    message_preview_path.write_text(message + "\n", encoding="utf-8")
    write_current_report()
    if args.send and not args.dry_run and report.get("status") == "ok":
        try:
            approval = apply_telegram_send_approval(report, message, required=True)
            if not approval["ok"]:
                report["status"] = "failed"
                report["telegram_send_blocked_reason"] = (
                    "send_approval_failed:" + ";".join(approval["issues"])
                )
                raise RuntimeError(report["telegram_send_blocked_reason"])
            if not report["message_quality"]["ok"]:
                report["status"] = "failed"
                report["telegram_send_blocked_reason"] = (
                    "message_quality_failed:" + ";".join(report["message_quality"]["issues"])
                )
                raise RuntimeError(report["telegram_send_blocked_reason"])
            send_results = [post_telegram(token, chat_id, chunk) for chunk in message_chunks]
            report["telegram_send_ok"] = all(bool(result.get("body", {}).get("ok")) for result in send_results)
            report["telegram_http_statuses"] = [result.get("http_status") for result in send_results]
            if report["telegram_send_ok"]:
                report["telegram_last_successful_send"] = write_successful_send_state(report)
                report["telegram_last_successful_send_quality"] = message_quality(
                    str(report["telegram_last_successful_send"].get("message") or "")
                )
                report["telegram_last_successful_send_scope_ok"] = successful_send_state_source_report_scope_ok(
                    report["telegram_last_successful_send"],
                    active_report_root,
                )
                report["telegram_last_successful_send_scheduler_usable"] = successful_send_state_usable_for_scheduler(
                    report["telegram_last_successful_send"],
                    report["telegram_last_successful_send_quality"],
                    active_report_root,
                )
        except Exception as exc:
            report["status"] = "failed"
            report["error"] = str(exc)
    report["post_write_goal_audit_refresh"] = (
        skipped_refresh(daily_sync_blocker_skip_reason, blocker=daily_sync_blocker_reason_value)
        if daily_sync_blocked
        else refresh_goal_audit()
    )
    refresh_health = apply_refresh_health(report)
    message = build_message(send_requested=args.send and not args.dry_run, refresh_health=refresh_health)
    message_chunks = chunk_message(message)
    report.update(
        {
            "message_preview": message_chunks[0][:4000],
            "message": message,
            "message_chunks": message_chunks,
            "message_character_count": len(message),
            "message_quality": message_quality(message),
            "message_chunk_count": len(message_chunks),
            "message_chunk_character_counts": [len(chunk) for chunk in message_chunks],
        }
    )
    apply_telegram_send_approval(report, message, required=args.send and not args.dry_run)
    message_preview_path.write_text(message + "\n", encoding="utf-8")
    write_current_report()
    report["report_integrity_guard_refresh"] = (
        skipped_refresh(daily_sync_blocker_skip_reason, blocker=daily_sync_blocker_reason_value)
        if daily_sync_blocked
        else refresh_report_integrity_guard()
    )
    refresh_health = apply_refresh_health(report)
    message = build_message(send_requested=args.send and not args.dry_run, refresh_health=refresh_health)
    message_chunks = chunk_message(message)
    report.update(
        {
            "message_preview": message_chunks[0][:4000],
            "message": message,
            "message_chunks": message_chunks,
            "message_character_count": len(message),
            "message_quality": message_quality(message),
            "message_chunk_count": len(message_chunks),
            "message_chunk_character_counts": [len(chunk) for chunk in message_chunks],
        }
    )
    apply_telegram_send_approval(report, message, required=args.send and not args.dry_run)
    message_preview_path.write_text(message + "\n", encoding="utf-8")
    write_current_report()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
