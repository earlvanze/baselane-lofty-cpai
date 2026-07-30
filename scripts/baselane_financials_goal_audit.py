#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from transfer_report_digest import stable_transfer_report_digest


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


SAFE_MONTHLY_CRON_DRY_RUN_COMMAND = (
    "DRY_RUN=1 SEND_OWNER_EMAILS=0 PUBLISH_LOFTY_PM_UPDATES=0 "
    "RUN_LOFTY_GUARDED_APPLY=1 APPLY_LOFTY_GUARDED_UPDATES=0 bash scripts/baselane_financials_monthly_cron.sh"
)
POST_AUTH_RESUME_COMMAND = "bash scripts/baselane_financials_post_auth_resume.sh"
LIVE_CAPTURE_APPLY_RE = re.compile(r"python3\s+scripts/lofty_capture_live_(?:update|financial)_guards\.py\b.*--apply")
STALE_LIVE_GUARD_ACTIONS = {
    "Capture/register live Lofty UPDATES.md fetch with lofty-updates-guard before applying.": "UPDATES.md",
    "Capture/register live Lofty FINANCIALS.md fetch with lofty-live-file-guard before applying.": "FINANCIALS.md",
}


def sanitize_operator_action_text(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: sanitize_operator_action_text(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [sanitize_operator_action_text(nested) for nested in value]
    if not isinstance(value, str):
        return value
    target = STALE_LIVE_GUARD_ACTIONS.get(value)
    if target is None:
        if not LIVE_CAPTURE_APPLY_RE.search(value):
            return value
        target = "FINANCIALS.md" if "lofty_capture_live_financial_guards.py" in value else "UPDATES.md"
    return (
        f"Auth Lofty visible tab (3 tries); then rerun live {target} capture through the safe monthly dry-run. "
        f"Run `{SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}` after auth; this keeps email, Lofty PM publish, and guarded live writes disabled."
    )


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def ok_status(data: dict[str, Any]) -> bool:
    return data.get("status") == "ok" or data.get("ok") is True


def non_negative_int(value: object) -> bool:
    return isinstance(value, int) and value >= 0


def count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_property_label(value: object) -> str:
    text = Path(str(value or "").strip()).name
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def runtime_target_property_names(runtime_map: dict[str, Any]) -> set[str]:
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


def rent_roll_target_gap_summary(queue_csv: Path, runtime_map: dict[str, Any]) -> dict[str, Any]:
    target_names = runtime_target_property_names(runtime_map)
    pending_rows = [
        row
        for row in read_csv_rows(queue_csv)
        if row.get("queue_type") == "rent_roll_gap" and str(row.get("approved") or "").strip().lower() != "true"
    ]
    if not target_names or not pending_rows:
        return {
            "target_scoped": False,
            "target_property_count": len(target_names),
            "pending_gap_count": len(pending_rows),
            "target_pending_gap_count": len(pending_rows),
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


def hemlane_capture_recaptcha_required(report: dict[str, Any]) -> bool:
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


def hemlane_capture_login_required(report: dict[str, Any]) -> bool:
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


def hemlane_capture_attempt_count(report: dict[str, Any]) -> int:
    return count(
        report.get("login_recovery_try_count")
        if "login_recovery_try_count" in report
        else report.get("login_recovery_attempt_count")
    )


def hemlane_capture_recovery_exhausted(report: dict[str, Any]) -> bool:
    return (
        report.get("login_recovery_exhausted") is True
        or report.get("automated_browser_recovery_complete") is True
        or report.get("manual_auth_phase") == "after_browser_recovery"
    )


def hemlane_preflight_at_login_after_recovery(hemlane_cdp_preflight: dict[str, Any]) -> bool:
    return (
        hemlane_cdp_preflight.get("status") == "review"
        and hemlane_cdp_preflight.get("cdp_available") is True
        and count(hemlane_cdp_preflight.get("login_tab_count")) > 0
        and count(hemlane_cdp_preflight.get("logged_in_tab_count")) == 0
        and hemlane_cdp_preflight.get("login_recovery_opened_rent_roll") is True
    )


def hemlane_preflight_needs_open_tab(hemlane_cdp_preflight: dict[str, Any]) -> bool:
    return (
        hemlane_cdp_preflight.get("status") == "review"
        and hemlane_cdp_preflight.get("cdp_available") is True
        and count(hemlane_cdp_preflight.get("hemlane_tab_count")) == 0
        and count(hemlane_cdp_preflight.get("login_tab_count")) == 0
        and count(hemlane_cdp_preflight.get("rent_roll_tab_count")) == 0
    )


def report_has_timestamp(report: dict[str, Any]) -> bool:
    return bool(report_timestamp(report))


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


def hemlane_bitwarden_submitted(*reports: dict[str, Any]) -> bool:
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


def normalized_hemlane_preflight_snapshot(preflight: dict[str, Any]) -> dict[str, Any]:
    snapshot = dict(preflight or {})
    action = str(snapshot.get("next_action") or "").strip()
    if "Auth Hemlane visible tab" in action:
        attempts = count(
            snapshot.get("login_recovery_try_count")
            if "login_recovery_try_count" in snapshot
            else snapshot.get("login_recovery_attempt_count")
        )
        snapshot["next_action"] = hemlane_visible_login_after_recovery_action(attempts)
    return snapshot


def path_within_root(path_text: object, root: Path) -> bool:
    text = str(path_text or "").strip()
    if not text:
        return True
    root = root.expanduser().absolute()
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        path.absolute().relative_to(root)
    except ValueError:
        return False
    return True


def hemlane_rent_roll_next_action(
    hemlane_cdp_preflight: dict[str, Any],
    run_month: str | None,
    comms_root: Path | None = None,
    hemlane_cdp_capture: dict[str, Any] | None = None,
) -> str:
    run_month = run_month or "YYYY-MM"
    comms_root = comms_root or default_comms_root()
    capture = hemlane_cdp_capture if isinstance(hemlane_cdp_capture, dict) else {}
    if hemlane_preflight_needs_open_tab(hemlane_cdp_preflight) and parsed_report_timestamp(hemlane_cdp_preflight) >= parsed_report_timestamp(capture):
        return hemlane_open_tab_action()
    if (
        hemlane_preflight_at_login_after_recovery(hemlane_cdp_preflight)
        and report_has_timestamp(hemlane_cdp_preflight)
        and parsed_report_timestamp(hemlane_cdp_preflight) >= parsed_report_timestamp(capture)
    ):
        attempts = count(hemlane_cdp_preflight.get("login_recovery_attempt_count")) or hemlane_capture_attempt_count(capture)
        return hemlane_visible_login_after_recovery_action(attempts)
    if (
        capture.get("status") not in {"missing", "unreadable", None}
        and hemlane_capture_recaptcha_required(capture)
        and hemlane_bitwarden_submitted(capture)
    ):
        return hemlane_recaptcha_after_bitwarden_action()
    if capture.get("status") not in {"missing", "unreadable", None} and hemlane_capture_login_required(capture):
        if hemlane_capture_recaptcha_required(capture) and hemlane_bitwarden_submitted(capture):
            return hemlane_recaptcha_after_bitwarden_action()
        attempts = hemlane_capture_attempt_count(capture)
        return hemlane_login_screen_recovery_action(attempts)
    login_recovery_attempts = hemlane_cdp_preflight.get("login_recovery_attempts")
    attempt_count = (
        hemlane_cdp_preflight.get("login_recovery_attempt_count")
        if "login_recovery_attempt_count" in hemlane_cdp_preflight
        else len(login_recovery_attempts) if isinstance(login_recovery_attempts, list) else 0
    )
    login_recovery_opened_rent_roll = hemlane_cdp_preflight.get("login_recovery_opened_rent_roll") is True
    preflight_recovery_exhausted = hemlane_capture_recovery_exhausted(hemlane_cdp_preflight)
    hemlane_at_login = (
        hemlane_cdp_preflight.get("status") == "review"
        and hemlane_cdp_preflight.get("cdp_available") is True
        and count(hemlane_cdp_preflight.get("login_tab_count")) > 0
        and count(hemlane_cdp_preflight.get("logged_in_tab_count")) == 0
    )
    if hemlane_at_login and login_recovery_opened_rent_roll and attempt_count:
        return hemlane_visible_login_after_recovery_action(attempt_count)
    return hemlane_login_screen_recovery_action()


def rent_roll_source_next_action(
    source: dict[str, Any],
    hemlane_cdp_preflight: dict[str, Any],
    run_month: str | None,
    comms_root: Path | None = None,
    hemlane_cdp_capture: dict[str, Any] | None = None,
) -> str:
    comms_root = comms_root or default_comms_root()
    source_next_action = str(source.get("next_action") or "").strip()
    capture = hemlane_cdp_capture if isinstance(hemlane_cdp_capture, dict) else {}
    capture_current = capture.get("status") not in {"missing", "unreadable", None}
    if (
        hemlane_preflight_needs_open_tab(hemlane_cdp_preflight)
        and parsed_report_timestamp(hemlane_cdp_preflight) >= parsed_report_timestamp(capture)
        and parsed_report_timestamp(hemlane_cdp_preflight) >= parsed_report_timestamp(source)
    ):
        return hemlane_open_tab_action()
    if (
        hemlane_preflight_at_login_after_recovery(hemlane_cdp_preflight)
        and report_has_timestamp(hemlane_cdp_preflight)
        and parsed_report_timestamp(hemlane_cdp_preflight) >= parsed_report_timestamp(capture)
        and parsed_report_timestamp(hemlane_cdp_preflight) >= parsed_report_timestamp(source)
    ):
        attempts = count(hemlane_cdp_preflight.get("login_recovery_attempt_count")) or hemlane_capture_attempt_count(capture)
        return hemlane_visible_login_after_recovery_action(attempts)
    if (
        source.get("hemlane_capture_issue") == "recaptcha_required"
        and (not capture_current or hemlane_capture_recaptcha_required(capture))
        and hemlane_bitwarden_submitted(capture, source)
    ):
        return hemlane_recaptcha_after_bitwarden_action()
    if (
        source.get("hemlane_capture_issue") == "recaptcha_required"
        and source_next_action
        and (not capture_current or hemlane_capture_recaptcha_required(capture))
    ):
        if (
            hemlane_preflight_at_login_after_recovery(hemlane_cdp_preflight)
            or any(marker in source_next_action.lower() for marker in ("hard refresh", "hard-refresh", "close/open", "reopened"))
        ):
            attempts = hemlane_capture_attempt_count(capture) or count(hemlane_cdp_preflight.get("login_recovery_attempt_count"))
            return hemlane_login_screen_recovery_action(attempts, captcha_conditional=True)
        if hemlane_bitwarden_submitted(capture, source):
            return hemlane_recaptcha_after_bitwarden_action()
        attempts = hemlane_capture_attempt_count(capture) or count(hemlane_cdp_preflight.get("login_recovery_attempt_count"))
        return hemlane_login_screen_recovery_action(attempts, captcha_conditional=True)
    return (
        hemlane_rent_roll_next_action(hemlane_cdp_preflight, run_month, comms_root, capture)
        or source_next_action
        or hemlane_post_auth_action("Capture or authenticate a current Hemlane rent roll for the run month")
    )


def lofty_pm_next_action(cdp_preflight: dict[str, Any]) -> str:
    direct = str(cdp_preflight.get("next_action") or "").strip()
    command = str(cdp_preflight.get("manual_auth_next_command") or "").strip()
    command_suffix = f" Run `{command}` after auth." if command and command not in direct else ""
    if direct:
        if "UPDATES.md/FINANCIALS.md guard captures" in direct:
            return f"{direct}{command_suffix}"
        return f"{direct}{command_suffix} Then rerun live UPDATES.md/FINANCIALS.md guard captures."
    return (
        "Hard-refresh or reopen the Lofty PM browser tab, confirm PM tabs are authenticated, "
        f"then rerun live UPDATES.md/FINANCIALS.md guard captures.{command_suffix}"
    )


def owner_email_guild_test_handoff(owner_email_send_guard: dict[str, Any]) -> dict[str, Any]:
    nested = owner_email_send_guard.get("guild_test_post_handoff")
    nested = nested if isinstance(nested, dict) else {}
    return {
        "prepared": owner_email_send_guard.get("guild_test_post_prepared", nested.get("prepared")),
        "posted": owner_email_send_guard.get("guild_test_post_posted", nested.get("posted")),
        "valid": owner_email_send_guard.get("guild_test_post_valid", nested.get("valid")),
        "route_proof_ok": owner_email_send_guard.get("guild_test_post_route_proof_ok", nested.get("route_proof_ok")),
        "target": owner_email_send_guard.get("guild_test_post_target", nested.get("target")),
        "selected_property_name": owner_email_send_guard.get("guild_test_post_selected_property_name", nested.get("selected_property_name")),
        "message_file": owner_email_send_guard.get("guild_test_post_message_file", nested.get("message_file")),
        "envelope_file": owner_email_send_guard.get("guild_test_post_envelope_file", nested.get("envelope_file")),
        "next_action": owner_email_send_guard.get("guild_test_post_next_action", nested.get("next_action")),
    }


def owner_email_guild_test_next_action(handoff: dict[str, Any]) -> str:
    if handoff.get("valid") is True:
        return "Guild test post is proven; owner email may proceed only after every readiness and idempotency guard is clean."
    if handoff.get("prepared") is True and handoff.get("posted") is not True:
        property_name = str(handoff.get("selected_property_name") or "selected property").strip()
        target = str(handoff.get("target") or "selected Lofty guild channel").strip()
        message_file = str(handoff.get("message_file") or "prepared message_file").strip()
        return (
            f"After readiness is clean and explicit posting approval is intended, post {message_file} "
            f"for {property_name} to {target}; then rerun the guard with posted message/channel IDs before email."
        )
    return "Prepare and prove one Lofty guild property-channel test post before owner email can send."


def eod_delivery_proof_only(item: dict[str, Any]) -> bool:
    blockers = [str(blocker or "") for blocker in (item.get("blockers") or [])]
    return item.get("id") == "eod_telegram_visibility" and bool(blockers) and all(
        blocker.startswith("eod_not_sent_to_telegram")
        or blocker in {"missing daily run summary", "missing weekly run summary", "missing monthly run summary"}
        for blocker in blockers
    )


MORTGAGE_TOKENOMICS_WEEKLY_REASON_PARTS = {
    "mortgage_workflow_gate_review",
    "mortgage_workflow_tokenomics_workbook_write_guard_blocked",
    "coownership_tokenomics_workbook_write_review",
    "coownership_tokenomics_workbook_write_not_ready",
}


def weekly_mortgage_tokenomics_only(item: dict[str, Any]) -> bool:
    if item.get("id") != "weekly_idempotent_file_updates":
        return False
    blockers = [str(blocker or "") for blocker in (item.get("blockers") or [])]
    weekly_blockers = [blocker for blocker in blockers if blocker.startswith("weekly_file_updates=review:")]
    if len(weekly_blockers) != 1 or len(blockers) != 1:
        return False
    _, _, reason_text = weekly_blockers[0].partition(":")
    reason_parts = {part for part in reason_text.split(";") if part}
    return bool(reason_parts) and reason_parts.issubset(MORTGAGE_TOKENOMICS_WEEKLY_REASON_PARTS)


def primary_blocker_priority(item: dict[str, Any]) -> int:
    if eod_delivery_proof_only(item):
        return 90
    if weekly_mortgage_tokenomics_only(item):
        return 55
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    if (
        item.get("id") == "monthly_bank_statement_capture"
        and evidence.get("download_error_class") == "no-statement-buttons"
    ):
        return 58
    priorities = {
        "daily_deterministic_sync": 10,
        "monthly_15th_scheduler": 15,
        "weekly_idempotent_file_updates": 30,
        "monthly_bank_statement_capture": 40,
        "monthly_transfer_reconciliation_telegram": 42,
        "monthly_canonical_docs": 45,
        "monthly_comms_rent_roll_context": 50,
        "monthly_review_and_guarded_apply": 60,
        "lofty_pm_live_guard_workflow": 65,
        "owner_email_idempotent_no_spam": 80,
        "eod_telegram_visibility": 35,
    }
    return priorities.get(str(item.get("id") or ""), 70)


def actionable_blocker_summary(item: dict[str, Any]) -> dict[str, Any]:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    return {
        "id": item.get("id"),
        "requirement": item.get("requirement") or item.get("id"),
        "title": item.get("title"),
        "summary": item.get("summary") or (item.get("blockers") or ["review"])[0],
        "blocker": item.get("blocker") or (item.get("blockers") or ["review"])[0],
        "artifact": item.get("artifact"),
        "evidence": item.get("evidence") if not isinstance(item.get("evidence"), dict) else evidence.get("report") or evidence.get("gap_review_csv") or evidence.get("gap_review"),
        "next_action": item.get("next_action"),
        "hold": item.get("hold"),
    }


def section_status_count(records: list[dict[str, Any]], section: str) -> dict[str, int]:
    statuses: dict[str, int] = {}
    for record in records:
        status = str(((record.get(section) or {}).get("status")) or "missing")
        statuses[status] = statuses.get(status, 0) + 1
    return dict(sorted(statuses.items()))


def section_status_is_blocking(status: object) -> bool:
    text = str(status or "").strip()
    return bool(text) and text not in {"ok", "applied", "already_applied", "skipped_no_candidate"} and not text.startswith(("skipped_", "excluded_"))


def guarded_apply_summary(guarded_apply: dict[str, Any], guard_audit: dict[str, Any]) -> dict[str, Any]:
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
    audit_status_counts: dict[str, dict[str, int]] = {"updates": {}, "financials": {}}
    for record in audit_records:
        checks = record.get("checks") if isinstance(record.get("checks"), dict) else {}
        for section in ("updates", "financials"):
            status = str(((checks.get(section) or {}).get("status")) or "missing")
            audit_status_counts[section][status] = audit_status_counts[section].get(status, 0) + 1
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
        "guard_audit_status_counts": audit_status_counts,
    }


def listing_cleanup_queue_summary(queue: dict[str, Any], verify: dict[str, Any]) -> dict[str, Any]:
    ready_count = count(queue.get("ready_listing_cleanup_count"))
    verified_count = count(verify.get("verified_record_count"))
    queue_digest = str(queue.get("ready_cleanup_idempotency_digest") or "").strip()
    verify_digest = str(verify.get("ready_cleanup_idempotency_digest") or "").strip()
    live_apply_file = str(queue.get("live_apply_commands_requires_explicit_approval_file") or "").strip()
    ready_csv = str(queue.get("ready_cleanup_csv") or "").strip()
    digest_env_var = str(queue.get("live_apply_approval_digest_env_var") or "LOFTY_LISTING_CLEANUP_APPLY_DIGEST").strip()
    digest_required = str(queue.get("live_apply_approval_digest_required_value") or queue_digest).strip()
    verified = (
        ready_count > 0
        and queue.get("status") == "review"
        and count(queue.get("issue_count")) == 0
        and verify.get("status") == "ok"
        and count(verify.get("issue_count")) == 0
        and verified_count == ready_count
        and sha256ish(queue_digest)
        and verify_digest == queue_digest
        and digest_required == queue_digest
        and bool(live_apply_file)
    )
    return {
        "ready_count": ready_count,
        "verified_record_count": verified_count,
        "verified": verified,
        "ready_cleanup_csv": ready_csv,
        "live_apply_commands_requires_explicit_approval_file": live_apply_file,
        "ready_cleanup_idempotency_digest": queue_digest,
        "dry_run_verify_ready_cleanup_idempotency_digest": verify_digest,
        "live_apply_approval_env_var": queue.get("live_apply_approval_env_var") or "LOFTY_LISTING_CLEANUP_APPLY_APPROVED",
        "live_apply_approval_env_required_value": queue.get("live_apply_approval_env_required_value") or "1",
        "live_apply_approval_digest_env_var": digest_env_var,
        "live_apply_approval_digest_required_value": digest_required,
    }


def listing_cleanup_next_action(summary: dict[str, Any], root: Path) -> str | None:
    if summary.get("verified") is not True:
        return None
    live_apply_file = str(summary.get("live_apply_commands_requires_explicit_approval_file") or "")
    ready_csv = str(summary.get("ready_cleanup_csv") or "")
    try:
        live_apply_file = str(Path(live_apply_file).relative_to(root)) if Path(live_apply_file).is_absolute() else live_apply_file
    except ValueError:
        pass
    try:
        ready_csv = str(Path(ready_csv).relative_to(root)) if Path(ready_csv).is_absolute() else ready_csv
    except ValueError:
        pass
    ready_count = count(summary.get("ready_count"))
    digest = str(summary.get("ready_cleanup_idempotency_digest") or "").strip()
    digest_env = str(summary.get("live_apply_approval_digest_env_var") or "LOFTY_LISTING_CLEANUP_APPLY_DIGEST").strip()
    return (
        f"Verified cleaned-history repair queue is ready for {ready_count} copied-history listing fields. "
        f"Review {ready_csv}; if explicit live cleanup approval is intended, run the gated script {live_apply_file} "
        f"with LOFTY_LISTING_CLEANUP_APPLY_APPROVED=1 and {digest_env}={digest}, then recapture live guards."
    )


def financial_patch_next_action(readiness: dict[str, Any], root: Path) -> str | None:
    guard_count = count(readiness.get("guard_reconcile_required_count"))
    blocked_empty_count = count(readiness.get("blocked_empty_patch_count"))
    generated_ledger_review_count = count(readiness.get("blocked_generated_ledger_review_required_count"))
    digest = str(readiness.get("financial_patch_readiness_digest") or "").strip()
    if guard_count <= 0 and blocked_empty_count <= 0:
        return None
    guard_csv = str(readiness.get("guard_reconcile_csv") or "reports/lofty_financial_patch_readiness.guard-reconcile.csv")
    blocked_csv = str(readiness.get("blocked_empty_patch_csv") or "reports/lofty_financial_patch_readiness.blocked-empty-patch.csv")
    blocked_review = str(
        readiness.get("blocked_empty_patch_markdown")
        or readiness.get("blocked_empty_patch_csv")
        or "reports/lofty_financial_patch_readiness.blocked-empty-patch.md"
    )
    for name, raw in (("guard_csv", guard_csv), ("blocked_csv", blocked_csv), ("blocked_review", blocked_review)):
        path = Path(raw)
        try:
            if path.is_absolute():
                if name == "guard_csv":
                    guard_csv = str(path.relative_to(root))
                elif name == "blocked_csv":
                    blocked_csv = str(path.relative_to(root))
                else:
                    blocked_review = str(path.relative_to(root))
        except ValueError:
            pass
    digest_clause = f" readiness digest {digest}" if sha256ish(digest) else ""
    if guard_count > 0:
        return (
            f"Review {guard_csv}; {guard_count} FINANCIALS.md patches are safe/non-empty but require live guard "
            f"reconciliation before any Lofty PM financial listing apply.{digest_clause}"
        )
    if generated_ledger_review_count == blocked_empty_count and blocked_empty_count > 0:
        return (
            f"Review {blocked_review}; {blocked_empty_count} FINANCIALS.md targets are generated ledger-summary files "
            "that require reviewed monthly financial snapshots before any Lofty PM financial listing apply."
        )
    return (
        f"Review {blocked_review}; {blocked_empty_count} FINANCIALS.md targets produced empty patches and should not be applied."
    )


def root_relative_path(raw: object, root: Path) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    path = Path(value)
    try:
        return str(path.relative_to(root)) if path.is_absolute() else value
    except ValueError:
        return value


def disk_space_preflight_issues(report: dict[str, Any]) -> list[str]:
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    return [str(issue) for issue in issues if str(issue).startswith("low_free_space:")]


def daily_disk_space_blocker_summary(issues: list[str]) -> str:
    if issues:
        return f"Daily Baselane sync blocked by low local disk space ({issues[0]})"
    return "Daily Baselane sync blocked by low local disk space"


def combined_listing_financial_patch_next_action(
    listing_summary: dict[str, Any],
    financial_readiness: dict[str, Any],
    root: Path,
) -> str | None:
    ready_count = count(listing_summary.get("ready_count"))
    blocked_count = count(financial_readiness.get("blocked_empty_patch_count") or financial_readiness.get("blocked_count"))
    if ready_count <= 0 or blocked_count <= 0:
        return None
    ready_csv = root_relative_path(
        listing_summary.get("ready_cleanup_csv") or "reports/lofty_listing_update_cleanup_queue.ready.csv",
        root,
    )
    blocked_review = root_relative_path(
        financial_readiness.get("blocked_empty_patch_markdown")
        or financial_readiness.get("blocked_empty_patch_csv")
        or "reports/lofty_financial_patch_readiness.blocked-empty-patch.md",
        root,
    )
    return (
        f"Review {ready_csv} and {blocked_review}; cleanup {ready_count} copied-history listing fields "
        f"and replace {blocked_count} generated-ledger FINANCIALS snapshots with reviewed monthly financial snapshots "
        "before any live apply, Lofty PM publish, or owner email."
    )


EXPECTED_LOCAL_MODEL = "ollama-cyber/qwen3.5:35b-a3b"
EXPECTED_LOCAL_PROVIDER = "ollama-cyber"
EXPECTED_LOCAL_MODEL_ID = "qwen3.5:35b-a3b"
EXPECTED_LOCAL_MODEL_TASK_CLASS = "schema_checked_precomputed_status_formatting"
LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS = 30.0
LIVE_CAPTURE_MAX_AGE_HOURS = 30.0
MONTHLY_STATEMENTS_MAX_AGE_HOURS = 45.0 * 24.0
EOD_REPORT_MAX_AGE_HOURS = 30.0
MONTHLY_SCHEDULER_MAX_AGE_HOURS = 35.0 * 24.0
MONTHLY_TRANSFER_TELEGRAM_MAX_AGE_HOURS = 35.0 * 24.0


def sha256ish(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


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


def fresh_generated_at(report: dict[str, Any], max_age_hours: float = LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS) -> bool:
    if not str(report.get("generated_at") or "").strip():
        return True
    age_hours = iso_age_hours(report.get("generated_at"))
    return age_hours is not None and -1 <= age_hours <= max_age_hours


def required_fresh_generated_at(report: dict[str, Any], max_age_hours: float) -> bool:
    if not str(report.get("generated_at") or "").strip():
        return False
    return fresh_generated_at(report, max_age_hours)


def local_model_preflight_ok(report: dict[str, Any]) -> bool:
    direct = report.get("direct_smoke") if isinstance(report.get("direct_smoke"), dict) else {}
    finance = report.get("finance_contract_smoke") if isinstance(report.get("finance_contract_smoke"), dict) else {}
    contract = report.get("validation_contract") if isinstance(report.get("validation_contract"), dict) else {}
    scope = report.get("model_execution_scope") if isinstance(report.get("model_execution_scope"), dict) else {}
    policy = report.get("small_model_execution_policy") if isinstance(report.get("small_model_execution_policy"), dict) else {}
    contract_scope = contract.get("model_execution_scope") if isinstance(contract.get("model_execution_scope"), dict) else {}
    return (
        report.get("status") == "ok"
        and report.get("model") == EXPECTED_LOCAL_MODEL
        and report.get("provider") == EXPECTED_LOCAL_PROVIDER
        and report.get("model_id") == EXPECTED_LOCAL_MODEL_ID
        and count(report.get("issue_count")) == 0
        and report.get("configured_model_present") is True
        and report.get("selected_endpoint_from_config") is True
        and report.get("model_available") is True
        and report.get("small_model_execution_allowed") is False
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
        and direct.get("attempted") is True
        and direct.get("ok") is True
        and direct.get("response") == "BASELANE_MODEL_OK"
        and finance.get("attempted") is True
        and finance.get("ok") is True
        and finance.get("response") == report.get("finance_contract_expected_response")
        and contract.get("selected_endpoint_from_config") is True
        and contract.get("direct_smoke_ok") is True
        and contract.get("direct_smoke_response") == "BASELANE_MODEL_OK"
        and contract.get("finance_contract_smoke_ok") is True
        and contract.get("finance_contract_response") == report.get("finance_contract_expected_response")
        and contract.get("model_scope_deterministic") is True
        and contract.get("model_pipeline_execution_denied") is True
        and contract.get("model_financial_authority_denied") is True
        and contract.get("model_live_side_effects_denied") is True
        and contract.get("model_external_validation_required") is True
        and contract_scope.get("deterministic_only") is True
        and contract_scope.get("pipeline_execution_allowed") is False
        and contract_scope.get("model_financial_authority") is False
        and contract_scope.get("live_side_effects_allowed") is False
        and sha256ish(report.get("validation_digest"))
        and fresh_generated_at(report)
    )


def report_timestamp(report: dict[str, Any]) -> str:
    return str(report.get("last_successful_send_at") or report.get("generated_at") or report.get("checked_at") or "").strip()


def parsed_report_timestamp(report: dict[str, Any]) -> datetime:
    raw = report_timestamp(report)
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def freshest_report(
    first: dict[str, Any],
    first_source: str,
    second: dict[str, Any],
    second_source: str,
) -> tuple[dict[str, Any], str]:
    first_available = first.get("status") not in {"missing", "unreadable"}
    second_available = second.get("status") not in {"missing", "unreadable"}
    if first_available and not second_available:
        return first, first_source
    if second_available and not first_available:
        return second, second_source
    if second_available and parsed_report_timestamp(second) > parsed_report_timestamp(first):
        return second, second_source
    return first, first_source


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
    if month < 1 or month > 12:
        return None, None
    return year, month


def statement_target_matches(report: dict[str, Any], run_month: str) -> bool:
    expected_year, expected_month = expected_statement_target(run_month)
    if expected_year is None or expected_month is None:
        return False
    return count(report.get("target_year")) == expected_year and count(report.get("target_month")) == expected_month


def monthly_readiness_primary_blocker_text(readiness: dict[str, Any]) -> str:
    actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
    primary = readiness.get("primary_blocker") if isinstance(readiness.get("primary_blocker"), dict) else {}
    if not primary:
        primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
    return str(primary.get("blocker") or primary.get("class") or "").strip()


def monthly_readiness_daily_sync_blocker_stale(readiness: dict[str, Any], daily_sync_ok: bool) -> bool:
    if not daily_sync_ok or readiness.get("owner_email_allowed") is True:
        return False
    text = " ".join(
        str(value or "")
        for value in (
            readiness.get("owner_email_blocked_reason"),
            monthly_readiness_primary_blocker_text(readiness),
        )
    )
    return "operational.daily_sync_report.not_ok" in text or "daily_sync_report.not_ok" in text


def monthly_readiness_blocked_reason(readiness: dict[str, Any], daily_sync_ok: bool = False) -> str:
    if monthly_readiness_daily_sync_blocker_stale(readiness, daily_sync_ok):
        return "monthly readiness stale after daily sync recovery; rerun monthly safe dry-run"
    actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
    primary_text = monthly_readiness_primary_blocker_text(readiness)
    actionable_count = count(actionable.get("actionable_blocker_count"))
    if primary_text:
        return f"monthly readiness owner_email_allowed=false; primary={primary_text}; actionable={actionable_count}"
    return f"monthly readiness owner_email_allowed=false; actionable={actionable_count}"


def owner_email_blocked_reason(readiness: dict[str, Any], daily_sync_ok: bool) -> str | None:
    if readiness.get("owner_email_allowed") is True:
        return None
    if monthly_readiness_daily_sync_blocker_stale(readiness, daily_sync_ok):
        return monthly_readiness_blocked_reason(readiness, daily_sync_ok=True)
    return readiness.get("owner_email_blocked_reason") or monthly_readiness_blocked_reason(readiness)


def readiness_primary_blocker(readiness: dict[str, Any]) -> dict[str, Any]:
    primary = readiness.get("primary_blocker") if isinstance(readiness.get("primary_blocker"), dict) else {}
    if primary:
        return primary
    actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
    return actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}


def requirement(requirement_id: str, title: str, ok: bool, evidence: dict[str, Any], blockers: list[str] | None = None) -> dict[str, Any]:
    blockers = blockers or []
    return {
        "id": requirement_id,
        "title": title,
        "status": "ok" if ok else "review",
        "evidence": evidence,
        "blockers": blockers,
        "blocker": blockers[0] if blockers else None,
        "artifact": None,
        "next_action": None,
    }


def count_csv_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open(newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def scheduler_job(data: dict[str, Any], name: str) -> dict[str, Any]:
    for job in data.get("jobs") or []:
        if isinstance(job, dict) and job.get("name") == name:
            return job
    return {}


def monthly_comms_paths(root: Path, run_month: str | None) -> dict[str, Path]:
    comms = comms_root_for(root.parent)
    month = run_month or datetime.now(timezone.utc).strftime("%Y-%m")
    updates = comms / "updates"
    return {
        "index": updates / f"{month}-portfolio-update-index.csv",
        "summary": updates / f"{month}-portfolio-update-summary.md",
        "rent_roll_csv": updates / f"{month}-rent-roll-occupancy-summary.csv",
        "rent_roll_md": updates / f"{month}-rent-roll-occupancy-summary.md",
        "gap_review": updates / f"{month}-rent-roll-gap-review.json",
        "gap_review_md": updates / f"{month}-rent-roll-gap-review.md",
        "gap_review_csv": updates / f"{month}-rent-roll-gap-review.csv",
        "gap_approval": updates / f"{month}-rent-roll-gap-approval.json",
        "rent_roll_source": updates / f"{month}-rent-roll-source.json",
        "hemlane_cdp_capture": updates / f"{month}-hemlane-cdp-capture-report.json",
        "checklist": updates / f"{month}-monthly-review-checklist.md",
    }


def monthly_comms_stats(root: Path, run_month: str | None) -> dict[str, Any]:
    paths = monthly_comms_paths(root, run_month)
    month = run_month or datetime.now(timezone.utc).strftime("%Y-%m")
    runtime_map = read_json(root / "reports" / "baselane_financials_monthly_lofty_pm_runtime_map.json")
    index_rows = []
    if paths["index"].is_file():
        with paths["index"].open(newline="", encoding="utf-8") as handle:
            index_rows = list(csv.DictReader(handle))
    rent_roll_rows = []
    if paths["rent_roll_csv"].is_file():
        with paths["rent_roll_csv"].open(newline="", encoding="utf-8") as handle:
            rent_roll_rows = list(csv.DictReader(handle))
    gap_review = read_json(paths["gap_review"])
    approval_coverage = gap_review.get("approval_template_coverage") if isinstance(gap_review.get("approval_template_coverage"), dict) else {}
    rent_roll_source_report = read_json(paths["rent_roll_source"])
    rent_roll_source = (
        rent_roll_source_report
        if rent_roll_source_report.get("status") not in {"missing", "unreadable"}
        else gap_review.get("rent_roll_source") if isinstance(gap_review.get("rent_roll_source"), dict) else {}
    )
    hemlane_cdp_capture = read_json(paths["hemlane_cdp_capture"])
    target_names = runtime_target_property_names(runtime_map)
    rent_roll_gap_rows = [
        row
        for row in index_rows
        if "RENT_ROLL_GAP" in str(row.get("notes") or "")
        and (not target_names or normalize_property_label(row.get("property_path")) in target_names)
    ]
    target_gap_summary = rent_roll_target_gap_summary(paths["gap_review_csv"], runtime_map)
    blocking_gap_count = (
        target_gap_summary["target_pending_gap_count"]
        if target_gap_summary["target_scoped"]
        else len(rent_roll_gap_rows)
    )
    export_dates = sorted({str(row.get("exported_on") or "").strip() for row in rent_roll_rows if str(row.get("exported_on") or "").strip()})
    stale_export_dates = [date for date in export_dates if date < f"{month}-01"]
    return {
        "index_path": str(paths["index"]),
        "index_exists": paths["index"].is_file(),
        "summary": str(paths["summary"]),
        "summary_exists": paths["summary"].is_file(),
        "property_count": len(index_rows),
        "rent_roll_gap_count": len(rent_roll_gap_rows),
        "rent_roll_blocking_gap_count": blocking_gap_count,
        "rent_roll_target_scoped": target_gap_summary["target_scoped"],
        "rent_roll_target_property_count": target_gap_summary["target_property_count"],
        "rent_roll_target_pending_gap_count": target_gap_summary["target_pending_gap_count"],
        "rent_roll_non_target_pending_gap_count": target_gap_summary["non_target_pending_gap_count"],
        "rent_roll_target_pending_gap_properties": target_gap_summary["target_pending_gap_properties"],
        "rent_roll_gap_samples": [
            {
                "property_path": row.get("property_path"),
                "notes": row.get("notes"),
            }
            for row in rent_roll_gap_rows[:8]
        ],
        "rent_roll_csv": str(paths["rent_roll_csv"]),
        "rent_roll_csv_exists": paths["rent_roll_csv"].is_file(),
        "rent_roll_matched_count": len(rent_roll_rows),
        "rent_roll_export_dates": export_dates,
        "rent_roll_stale_export_dates": stale_export_dates,
        "rent_roll_markdown": str(paths["rent_roll_md"]),
        "rent_roll_markdown_exists": paths["rent_roll_md"].is_file(),
        "gap_review": str(paths["gap_review"]),
        "gap_review_exists": paths["gap_review"].is_file(),
        "gap_review_csv": str(paths["gap_review_csv"]),
        "gap_review_csv_exists": paths["gap_review_csv"].is_file(),
        "gap_review_status": gap_review.get("status"),
        "gap_review_action_queue_count": gap_review.get("action_queue_count"),
        "gap_review_action_queue_digest": gap_review.get("action_queue_digest"),
        "gap_review_deferred_gap_count": gap_review.get("deferred_gap_count"),
        "gap_review_source_blocker_count": gap_review.get("source_blocker_count"),
        "gap_review_approval_template_coverage_status": approval_coverage.get("status"),
        "gap_review_approval_template_digest": gap_review.get("approval_template_digest") or approval_coverage.get("digest"),
        "gap_review_monthly_dry_run_command": gap_review.get("monthly_dry_run_command"),
        "rent_roll_source_report": str(paths["rent_roll_source"]),
        "rent_roll_source_report_exists": paths["rent_roll_source"].is_file(),
        "rent_roll_source_report_status": rent_roll_source_report.get("status"),
        "rent_roll_source": rent_roll_source,
        "hemlane_cdp_capture_report": str(paths["hemlane_cdp_capture"]),
        "hemlane_cdp_capture_report_exists": paths["hemlane_cdp_capture"].is_file(),
        "hemlane_cdp_capture_report_status": hemlane_cdp_capture.get("status"),
        "hemlane_cdp_capture": hemlane_cdp_capture,
        "rent_roll_freshness_status": rent_roll_source.get("freshness_status"),
        "rent_roll_latest_exported_on": rent_roll_source.get("latest_exported_on"),
        "rent_roll_latest_local_file": rent_roll_source.get("latest_local_rent_roll_file"),
        "rent_roll_owner_email_allowed": rent_roll_source.get("owner_email_allowed"),
        "rent_roll_live_update_allowed": rent_roll_source.get("live_update_allowed"),
        "pending_gap_approval_count": gap_review.get("pending_gap_count"),
        "pending_stale_export_date_approval_count": gap_review.get("pending_stale_export_date_count"),
        "gap_approval": str(paths["gap_approval"]),
        "gap_approval_exists": paths["gap_approval"].is_file(),
        "checklist": str(paths["checklist"]),
        "checklist_exists": paths["checklist"].is_file(),
        "hemlane_cdp_preflight": normalized_hemlane_preflight_snapshot(read_json(root / "reports" / "hemlane_cdp_preflight_report.json")),
    }


def build_report(root: Path) -> dict[str, Any]:
    comms_root = comms_root_for(root.parent)
    reports = root / "reports"
    legacy_daily = read_json(reports / "baselane_daily_run_report.json")
    canonical_daily = read_json(reports / "baselane_daily_sync_report.json")
    daily = canonical_daily if canonical_daily.get("status") not in {"missing", "unreadable"} else legacy_daily
    sync = read_json(reports / "baselane_sync_cdp_report.json")
    scheduler = read_json(reports / "baselane_scheduler_audit_report.json")
    daily_disk_preflight = read_json(reports / "baselane_daily_disk_space_preflight_report.json")
    local_model = read_json(reports / "baselane_local_model_preflight_report.json")
    eod = read_json(reports / "baselane_eod_telegram_report.json")
    eod_preview = read_json(reports / "baselane_eod_telegram_preview_report.json")
    latest_eod_report, latest_eod_report_source = freshest_report(
        eod,
        "baselane_eod_telegram_report.json",
        eod_preview,
        "baselane_eod_telegram_preview_report.json",
    )
    eod_send_state = read_json(reports / "baselane_eod_telegram_send_state.json")
    transfer_reconciliation = read_json(reports / "baselane_lofty_transfer_requirements.json")
    transfer_telegram_send = read_json(reports / "baselane_lofty_transfer_requirements_telegram_send.json")
    transfer_telegram_sent_state = read_json(reports / "baselane_lofty_transfer_requirements_telegram_send_state.json")
    weekly = read_json(reports / "baselane_weekly_file_updates_run_report.json")
    weekly_cf = read_json(reports / "baselane_weekly_cf_statement_sync_report.json")
    weekly_cf_gate = read_json(reports / "baselane_weekly_cf_review_gate.json")
    cf_no_gl_property_match_path = reports / "cf_statement_sync" / f"no_gl_property_match_{os.environ.get('RUN_MONTH') or ''}.json"
    cf_no_gl_property_match = read_json(cf_no_gl_property_match_path)
    if cf_no_gl_property_match.get("status") in {"missing", "unreadable"}:
        cf_no_gl_property_match_path = reports / "cf_statement_sync" / f"no_gl_property_match_{weekly_cf.get('month') or ''}.json"
        cf_no_gl_property_match = read_json(cf_no_gl_property_match_path)
    if cf_no_gl_property_match.get("status") in {"missing", "unreadable"}:
        no_gl_candidates = sorted((reports / "cf_statement_sync").glob("no_gl_property_match_*.json"), reverse=True)
        if no_gl_candidates:
            cf_no_gl_property_match_path = no_gl_candidates[0]
            cf_no_gl_property_match = read_json(cf_no_gl_property_match_path)
    cf_balance_sheet_consistency = read_json(reports / "baselane_cf_balance_sheet_consistency_audit.json")
    yhome_operating_cash_apply_verify = read_json(reports / "yhome_operating_cash_apply_verify_report.json")
    ecogl_source_fix_evidence = read_json(reports / "baselane_ecogl_source_fix_evidence.json")
    ecogl_source_fix_plan = read_json(reports / "baselane_ecogl_source_fix_plan.json")
    ecogl_source_fix_verifier = read_json(reports / "baselane_ecogl_source_fix_verifier.json")
    ecogl_source_fix_corrections = read_json(reports / "baselane_ecogl_source_fix_corrections.json")
    ecogl_source_fix_approval = read_json(reports / "baselane_ecogl_source_fix_approval.json")
    ecogl_source_fix_correction_validation = read_json(reports / "baselane_ecogl_source_fix_correction_validation.json")
    ecogl_source_fix_apply_plan = read_json(reports / "baselane_ecogl_source_fix_apply_plan.json")
    ecogl_source_fix_action_queue = read_json(reports / "baselane_ecogl_source_fix_action_queue.json")
    first_day_pm_fee_cleanup = read_json(reports / "baselane_first_day_pm_fee_source_cleanup_plan.json")
    doc_bootstrap = read_json(reports / "baselane_financials_monthly_doc_bootstrap.json")
    path_guard = read_json(reports / "lofty_public_path_guard_report.json")
    discord_public_guard = read_json(reports / "discord_public_financial_source_guard_report.json")
    review_manifest = read_json(reports / "baselane_financials_monthly_review_manifest.json")
    owner_review_gate = read_json(reports / "baselane_monthly_owner_review_gate.json")
    owner_guard_workflow = owner_review_gate.get("guard_workflow_coverage") if isinstance(owner_review_gate.get("guard_workflow_coverage"), dict) else {}
    guarded_apply = read_json(reports / "baselane_financials_monthly_guarded_apply.json")
    guard_audit = read_json(reports / "baselane_financials_monthly_guard_audit.json")
    guarded_apply_counts = guarded_apply_summary(guarded_apply, guard_audit)
    cdp_preflight = read_json(reports / "lofty_cdp_preflight_report.json")
    hemlane_cdp_preflight = read_json(reports / "hemlane_cdp_preflight_report.json")
    live_update_capture = read_json(reports / "baselane_financials_monthly_live_update_capture.json")
    live_financial_capture = read_json(reports / "baselane_financials_monthly_live_financial_capture.json")
    readiness = read_json(reports / "baselane_financials_monthly_readiness.json")
    publish = read_json(reports / "baselane_financials_monthly_lofty_pm_publish.json")
    lofty_financial_patch_readiness = read_json(reports / "lofty_financial_patch_readiness.json")
    owner_email_send_guard = read_json(reports / "baselane_monthly_owner_email_send_guard.json")
    owner_email_packet = read_json(reports / "baselane_monthly_owner_email_packet.json")
    owner_email_guild_handoff = owner_email_guild_test_handoff(owner_email_send_guard)
    listing_cleanup_queue = read_json(reports / "lofty_listing_update_cleanup_queue.json")
    listing_cleanup_verify = read_json(reports / "lofty_listing_cleanup_dry_run_verify.json")
    listing_cleanup_summary = listing_cleanup_queue_summary(listing_cleanup_queue, listing_cleanup_verify)
    listing_cleanup_action = listing_cleanup_next_action(listing_cleanup_summary, root)
    financial_patch_action = financial_patch_next_action(lofty_financial_patch_readiness, root)
    combined_listing_financial_patch_action = combined_listing_financial_patch_next_action(
        listing_cleanup_summary,
        lofty_financial_patch_readiness,
        root,
    )
    monthly_run = read_json(reports / "baselane_financials_monthly_run_report.json")
    monthly_statements_gate = read_json(reports / "baselane_monthly_statements_idempotent_report.json")
    monthly_statements_download = read_json(reports / "baselane_statements_download_report.json")
    run_month = os.environ.get("RUN_MONTH") or str(monthly_run.get("run_month") or "")
    comms_stats = monthly_comms_stats(root, run_month)
    weekly_review_safe_idempotency = weekly.get("review_safe_idempotency") if isinstance(weekly.get("review_safe_idempotency"), dict) else {}
    weekly_primary_blocker = weekly.get("primary_blocker") if isinstance(weekly.get("primary_blocker"), dict) else {}
    weekly_disk_space_preflight_blocked = (
        weekly_primary_blocker.get("id") == "weekly_disk_space_preflight"
        or str(weekly.get("reason") or "") == "disk_space_preflight_review"
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
    weekly_retry_safe_without_duplicate_outputs = weekly_review_safe_idempotency.get("retry_safe_without_duplicate_outputs")
    if weekly_retry_safe_without_duplicate_outputs is None:
        weekly_state_file_marked_complete = (
            weekly.get("state_file_marked_complete")
            if "state_file_marked_complete" in weekly
            else weekly_review_safe_idempotency.get("state_file_marked_complete")
        )
        weekly_retry_safe_without_duplicate_outputs = (
            weekly.get("status") == "review"
            and weekly_review_safe_idempotency.get("retry_required") is True
            and weekly_review_safe_idempotency.get("safe_to_skip_next_run") is False
            and weekly_review_safe_idempotency.get("weekly_unprocessed_idempotent") is True
            and weekly_state_file_marked_complete is False
        )
    weekly_review_retry_safe = (
        weekly.get("status") == "review"
        and weekly_review_safe_idempotency.get("retry_required") is True
        and weekly_review_safe_idempotency.get("safe_to_skip_next_run") is False
        and weekly_review_safe_idempotency.get("weekly_unprocessed_idempotent") is True
        and weekly_cf_gate_snapshot_current
    )
    weekly_review_safe_idempotency_evidence = {
        "cf_review_gate_action_queue_count": weekly_review_safe_idempotency.get("cf_review_gate_action_queue_count"),
        "cf_review_gate_action_queue_digest": weekly_review_safe_idempotency.get("cf_review_gate_action_queue_digest"),
        "cf_review_gate_idempotency_key": weekly_review_safe_idempotency.get("cf_review_gate_idempotency_key"),
        "cf_review_gate_snapshot_current": weekly_cf_gate_snapshot_current,
        "deterministic_verification_idempotent": weekly_review_safe_idempotency.get("deterministic_verification_idempotent"),
        "retry_required": weekly_review_safe_idempotency.get("retry_required"),
        "retry_safe_without_duplicate_outputs": weekly_retry_safe_without_duplicate_outputs,
        "safe_to_skip_next_run": weekly_review_safe_idempotency.get("safe_to_skip_next_run"),
        "state_file_unmarked": weekly_review_safe_idempotency.get("state_file_unmarked"),
        "weekly_unprocessed_idempotent": weekly_review_safe_idempotency.get("weekly_unprocessed_idempotent"),
    }
    source_cash_balance_violation_count = max(
        count(weekly_cf.get("source_cash_balance_violation_count")),
        count(weekly.get("source_cash_balance_violation_count")),
    )
    source_cash_balance_violation_properties = (
        weekly_cf.get("source_cash_balance_violation_properties")
        or weekly.get("source_cash_balance_violation_properties")
        or []
    )
    yhome_operating_cash_apply_verify_status = yhome_operating_cash_apply_verify.get("status")
    yhome_operating_cash_apply_verify_reason = yhome_operating_cash_apply_verify.get("reason")
    yhome_post_update_available = "post_yhome_update_required_count" in yhome_operating_cash_apply_verify
    if yhome_operating_cash_apply_verify_status == "ok" and (
        yhome_post_update_available
        or yhome_operating_cash_apply_verify_reason in {"applied_and_verified", "no_updates_required"}
    ):
        cf_balance_sheet_yhome_update_required_count = count(
            yhome_operating_cash_apply_verify.get("post_yhome_update_required_count")
            if yhome_post_update_available
            else yhome_operating_cash_apply_verify.get("pre_yhome_update_required_count")
        )
    else:
        cf_balance_sheet_yhome_update_required_count = max(
            count(cf_balance_sheet_consistency.get("yhome_update_required_count")),
            count(yhome_operating_cash_apply_verify.get("pre_yhome_update_required_count")),
        )
    cf_balance_sheet_consistency_issue_count = count(cf_balance_sheet_consistency.get("issue_count"))
    cf_balance_sheet_run_month = str(cf_balance_sheet_consistency.get("run_month") or "").strip()
    # The balance-sheet audit is an as-of-month control. Do not let a clean
    # report from a different month clear the current closed-month workflow.
    cf_balance_sheet_run_month_matches = bool(run_month) and cf_balance_sheet_run_month == run_month
    cf_balance_sheet_consistency_clean = (
        cf_balance_sheet_consistency.get("status") == "ok"
        and cf_balance_sheet_consistency_issue_count == 0
    )
    cf_balance_sheet_target_columns = (
        cf_balance_sheet_consistency.get("target_columns")
        or yhome_operating_cash_apply_verify.get("target_columns")
        or ["Lofty Operating Cash", "ECO Net DAO Funds"]
    )
    yhome_operating_cash_work_product_needs_attention = (
        cf_balance_sheet_yhome_update_required_count > 0
        or (
            yhome_operating_cash_apply_verify_status == "review"
            and yhome_operating_cash_apply_verify_reason == "dry_run_updates_required"
        )
    )
    source_fix_row_count = count(ecogl_source_fix_evidence.get("row_count"))
    source_fix_evidence_status_counts = (
        ecogl_source_fix_evidence.get("historical_evidence_status_counts")
        if isinstance(ecogl_source_fix_evidence.get("historical_evidence_status_counts"), dict)
        else {}
    )
    source_fix_automation_safe_count = count(ecogl_source_fix_evidence.get("historical_evidence_automation_safe_count"))
    source_fix_verified_fixed_count = count(ecogl_source_fix_verifier.get("verified_fixed_count"))
    source_fix_remaining_count = count(ecogl_source_fix_verifier.get("remaining_count") if "remaining_count" in ecogl_source_fix_verifier else source_fix_row_count)
    source_fix_verifier_status_counts = (
        ecogl_source_fix_verifier.get("status_counts")
        if isinstance(ecogl_source_fix_verifier.get("status_counts"), dict)
        else {}
    )
    source_fix_plan_actions = (
        ecogl_source_fix_plan.get("actions")
        if isinstance(ecogl_source_fix_plan.get("actions"), list)
        else []
    )
    source_fix_action_type_counts: dict[str, int] = {}
    for action in source_fix_plan_actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("action_type") or "").strip()
        if action_type:
            source_fix_action_type_counts[action_type] = source_fix_action_type_counts.get(action_type, 0) + 1
    future_journal_action_count = source_fix_action_type_counts.get(
        "reverse_or_delete_future_dated_source_journal", 0
    )
    source_fix_queue_group_counts = (
        ecogl_source_fix_action_queue.get("group_counts")
        if isinstance(ecogl_source_fix_action_queue.get("group_counts"), dict)
        else {}
    )
    source_fix_action_queue_current = ecogl_source_fix_action_queue.get("status") not in {"missing", "unreadable"}
    if source_fix_action_queue_current:
        source_fix_queue_ready_to_apply_count = count(ecogl_source_fix_action_queue.get("ready_to_apply_count"))
        source_fix_queue_ready_native_split_count = count(ecogl_source_fix_action_queue.get("ready_native_split_count"))
        source_fix_queue_needs_current_source_index_count = count(ecogl_source_fix_action_queue.get("needs_current_source_index_count"))
        source_fix_queue_decision_required_count = count(ecogl_source_fix_action_queue.get("decision_required_count"))
        source_fix_queue_already_applied_count = count(
            ecogl_source_fix_action_queue.get("already_applied_count")
            or source_fix_queue_group_counts.get("already_applied")
        )
        source_fix_row_count = (
            source_fix_queue_ready_to_apply_count
            + source_fix_queue_ready_native_split_count
            + source_fix_queue_needs_current_source_index_count
            + source_fix_queue_decision_required_count
        )
        source_fix_verified_fixed_count += source_fix_queue_already_applied_count
        source_fix_remaining_count = source_fix_row_count
    source_fix_validation_ready_count = count(ecogl_source_fix_correction_validation.get("ready_count"))
    source_fix_validation_pending_count = count(ecogl_source_fix_correction_validation.get("pending_count"))
    source_fix_validation_invalid_count = count(ecogl_source_fix_correction_validation.get("invalid_count"))
    source_fix_approval_approved_count = count(ecogl_source_fix_approval.get("approved_count"))
    source_fix_approval_pending_count = count(ecogl_source_fix_approval.get("pending_count"))
    source_fix_approval_invalid_count = count(ecogl_source_fix_approval.get("invalid_count"))
    source_fix_apply_ready_count = count(ecogl_source_fix_apply_plan.get("ready_current_source_index_count"))
    source_fix_apply_refresh_count = count(ecogl_source_fix_apply_plan.get("needs_current_source_index_refresh_count"))
    source_fix_apply_blocked_count = count(ecogl_source_fix_apply_plan.get("blocked_count"))
    if source_fix_action_queue_current:
        source_fix_apply_ready_count = count(ecogl_source_fix_action_queue.get("ready_to_apply_count"))
        source_fix_apply_refresh_count = count(ecogl_source_fix_action_queue.get("needs_current_source_index_count"))
        source_fix_apply_blocked_count = 0
    source_fix_validation_needs_input = source_fix_validation_pending_count > 0 or source_fix_validation_invalid_count > 0
    source_fix_ready_to_apply = source_fix_apply_ready_count > 0 and source_fix_apply_refresh_count == 0 and source_fix_apply_blocked_count == 0
    first_day_pm_fee_cleanup_action_count = count(first_day_pm_fee_cleanup.get("action_count"))
    first_day_pm_fee_cleanup_command = (
        first_day_pm_fee_cleanup.get("cleanup_command_after_review")
        or "BASELANE_FIRST_DAY_PM_FEE_SOURCE_CLEANUP_APPLY=1 bash scripts/baselane_first_day_pm_fee_cleanup_then_refresh.sh"
    )

    eod_sent_to_telegram = eod.get("dry_run") is False and eod.get("telegram_send_ok") is True
    eod_sent_report_timestamp = eod.get("last_successful_send_at") or eod.get("generated_at") or eod.get("checked_at")
    eod_sent_report_fresh = required_fresh_generated_at(
        {"generated_at": eod_sent_report_timestamp},
        EOD_REPORT_MAX_AGE_HOURS,
    )
    eod_sent_to_telegram_current = eod_sent_to_telegram and eod_sent_report_fresh
    eod_send_state_message = str(eod_send_state.get("message") or "")
    eod_send_state_line_count = len([line for line in eod_send_state_message.splitlines() if line.strip()])
    eod_send_state_noise_markers = [
        marker
        for marker in ("ALSO:", "Citadel", "Marlowe", "/mnt/f", "parity=", "qwen=", "paths=", "goal=")
        if marker in eod_send_state_message
    ]
    eod_send_state_message_concise = (
        eod_send_state_line_count <= 8
        and int(eod_send_state.get("message_character_count") or len(eod_send_state_message)) <= 520
        and not eod_send_state_noise_markers
    )
    eod_send_state_message_digest = str(eod_send_state.get("source_report_message_sha256") or "")
    eod_send_state_sent_digest = str(eod_send_state.get("telegram_sent_message_sha256") or "")
    eod_send_state_digest_ok = (
        sha256ish(eod_send_state_message_digest)
        and eod_send_state_message_digest == sha256_text(eod_send_state_message)
        and (not eod_send_state_sent_digest or eod_send_state_sent_digest == eod_send_state_message_digest)
    )
    eod_send_state_source_generated_at_present = bool(str(eod_send_state.get("source_report_generated_at") or "").strip())
    eod_send_state_base_ok = (
        eod_send_state.get("status") == "ok"
        and eod_send_state.get("dry_run") is False
        and eod_send_state.get("send_requested") is True
        and eod_send_state.get("telegram_send_ok") is True
        and bool(eod_send_state.get("telegram_http_statuses") or [])
    )
    eod_send_state_send_requested = eod_send_state.get("send_requested") is True
    eod_send_state_source_report_present = bool(str(eod_send_state.get("source_report") or "").strip())
    eod_send_state_scope_ok = eod_send_state_source_report_present and path_within_root(eod_send_state.get("source_report"), root)
    eod_send_state_source_report_path = None
    eod_send_state_source_report_payload: dict[str, Any] = {}
    if eod_send_state_source_report_present and eod_send_state_scope_ok:
        eod_send_state_source_report_path = Path(str(eod_send_state.get("source_report") or "")).expanduser()
        if not eod_send_state_source_report_path.is_absolute():
            eod_send_state_source_report_path = root / eod_send_state_source_report_path
        eod_send_state_source_report_payload = read_json(eod_send_state_source_report_path)
    eod_send_state_source_report_status = str(eod_send_state_source_report_payload.get("status") or "")
    eod_send_state_source_report_exists = eod_send_state_source_report_status not in {"missing", "unreadable", ""}
    eod_send_state_source_report_readable = eod_send_state_source_report_status not in {"missing", "unreadable", ""}
    eod_send_state_source_report_status_ok = eod_send_state_source_report_status == "ok"
    eod_send_state_source_report_non_dry_run_sent = (
        eod_send_state_source_report_payload.get("dry_run") is False
        and eod_send_state_source_report_payload.get("send_requested") is True
        and eod_send_state_source_report_payload.get("telegram_send_ok") is True
    )
    eod_send_state_source_report_message = str(eod_send_state_source_report_payload.get("message") or "")
    eod_send_state_source_report_message_matches = (
        bool(eod_send_state_message)
        and bool(eod_send_state_source_report_message)
        and eod_send_state_source_report_message == eod_send_state_message
    )
    eod_send_state_source_report_digest_matches = (
        sha256ish(eod_send_state_message_digest)
        and bool(eod_send_state_source_report_message)
        and eod_send_state_message_digest == sha256_text(eod_send_state_source_report_message)
    )
    eod_send_state_source_report_generated_at = str(
        eod_send_state_source_report_payload.get("generated_at") or ""
    ).strip()
    eod_send_state_source_report_generated_at_matches = (
        eod_send_state_source_generated_at_present
        and bool(eod_send_state_source_report_generated_at)
        and str(eod_send_state.get("source_report_generated_at") or "").strip()
        == eod_send_state_source_report_generated_at
    )
    eod_send_state_source_report_ok = (
        eod_send_state_source_report_exists
        and eod_send_state_source_report_readable
        and eod_send_state_source_report_status_ok
        and eod_send_state_source_report_non_dry_run_sent
        and eod_send_state_source_report_message_matches
        and eod_send_state_source_report_digest_matches
        and eod_send_state_source_report_generated_at_matches
    )
    eod_send_state_ok = (
        eod_send_state_base_ok
        and eod_send_state_message_concise
        and eod_send_state_scope_ok
        and eod_send_state_digest_ok
        and eod_send_state_source_generated_at_present
        and eod_send_state_source_report_ok
    )
    eod_send_state_timestamp = (
        eod_send_state.get("last_successful_send_at")
        or eod_send_state.get("source_report_generated_at")
        or eod_send_state.get("generated_at")
    )
    eod_send_state_fresh = required_fresh_generated_at(
        {"generated_at": eod_send_state_timestamp},
        EOD_REPORT_MAX_AGE_HOURS,
    )
    eod_send_state_current_ok = eod_send_state_ok and eod_send_state_fresh
    effective_eod = eod if eod_sent_to_telegram_current else eod_send_state if eod_send_state_current_ok else latest_eod_report
    if effective_eod is eod_send_state and eod_send_state_source_report_ok:
        hydrated = dict(eod_send_state)
        for summary_key in (
            "daily_sync_summary",
            "weekly_file_updates_summary",
            "monthly_financials_summary",
            "daily_run_summary",
            "weekly_run_summary",
            "monthly_run_summary",
        ):
            if isinstance(eod_send_state_source_report_payload.get(summary_key), dict) and not isinstance(
                hydrated.get(summary_key),
                dict,
            ):
                hydrated[summary_key] = eod_send_state_source_report_payload[summary_key]
        effective_eod = hydrated
    if effective_eod is eod:
        effective_eod_source = "baselane_eod_telegram_report.json"
    elif effective_eod is eod_send_state or (
        eod_send_state_source_report_present
        and bool(eod_send_state.get("last_successful_send_at"))
        and effective_eod.get("source_report") == eod_send_state.get("source_report")
        and effective_eod.get("last_successful_send_at") == eod_send_state.get("last_successful_send_at")
    ):
        effective_eod_source = "baselane_eod_telegram_send_state.json"
    else:
        effective_eod_source = latest_eod_report_source
    eod_message = str(effective_eod.get("message") or "")
    eod_timestamp = effective_eod.get("last_successful_send_at") or effective_eod.get("generated_at") or effective_eod.get("checked_at")
    eod_fresh = required_fresh_generated_at({"generated_at": eod_timestamp}, EOD_REPORT_MAX_AGE_HOURS)
    effective_eod_sent_to_telegram = eod_sent_to_telegram_current or eod_send_state_current_ok
    has_daily_visibility = any(
        marker in eod_message
        for marker in (
            "Baselane daily:",
            "Daily sync:",
            "Daily sync ",
            "Daily=",
            "daily=",
            "HEALTH: daily ",
            "OK: daily=",
            "daily ok",
            "\nSync:",
            "Sync:",
        )
    )
    has_monthly_visibility = any(marker in eod_message for marker in ("Monthly run:", "Monthly owner gate:", "monthly readiness:", "Monthly readiness:", "Goal audit:", "goal ", "goal=", "DO NOW:", "\nNEXT:", "\nOPEN:"))
    has_action_section = any(marker in eod_message for marker in ("Do Next", "Act Now", "\nNEXT\n", "DO NOW:", "\nNEXT:", "\nDO:", "\nSTOP:", "\nACTION:", "\nHOLD:"))
    eod_message_line_count = len([line for line in eod_message.splitlines() if line.strip()])
    eod_noise_markers = [
        marker
        for marker in ("ALSO:", "Citadel", "Marlowe", "/mnt/f", "parity=", "qwen=", "paths=", "goal=")
        if marker in eod_message
    ]
    eod_message_concise = (
        eod_message_line_count <= 8
        and int(effective_eod.get("message_character_count") or len(eod_message)) <= 520
        and not eod_noise_markers
    )
    eod_daily_run_summary = effective_eod.get("daily_run_summary") if isinstance(effective_eod.get("daily_run_summary"), dict) else {}
    if not eod_daily_run_summary:
        eod_daily_run_summary = effective_eod.get("daily_sync_summary") if isinstance(effective_eod.get("daily_sync_summary"), dict) else {}
    eod_weekly_run_summary = effective_eod.get("weekly_run_summary") if isinstance(effective_eod.get("weekly_run_summary"), dict) else {}
    if not eod_weekly_run_summary:
        eod_weekly_run_summary = effective_eod.get("weekly_file_updates_summary") if isinstance(effective_eod.get("weekly_file_updates_summary"), dict) else {}
    eod_monthly_run_summary = effective_eod.get("monthly_run_summary") if isinstance(effective_eod.get("monthly_run_summary"), dict) else {}
    if not eod_monthly_run_summary:
        eod_monthly_run_summary = effective_eod.get("monthly_financials_summary") if isinstance(effective_eod.get("monthly_financials_summary"), dict) else {}
    has_daily_run_summary = bool(eod_daily_run_summary.get("daily_sync_report") and eod_daily_run_summary.get("daily_run_report"))
    has_weekly_run_summary = bool(eod_weekly_run_summary.get("weekly_file_updates_report"))
    has_monthly_run_summary = bool(eod_monthly_run_summary.get("monthly_financials_report"))
    publish_send_interval_days = publish.get("send_interval_days")
    publish_send_interval_ok = isinstance(publish_send_interval_days, int) and publish_send_interval_days >= 7
    publish_evidence_issue_count = int(publish.get("owner_email_send_evidence_issue_count") or 0)
    publish_will_send_count = int(publish.get("owner_email_will_send_count") or 0)
    publish_evidence_count = int(publish.get("owner_email_send_evidence_count") or 0)
    publish_sent_state_status = publish.get("sent_state_write_status")
    publish_send_lock_status = publish.get("send_lock_status")
    publish_effective_send = publish.get("effective_send_owner_emails") is True
    publish_idempotency = publish.get("owner_email_idempotency") if isinstance(publish.get("owner_email_idempotency"), dict) else {}
    publish_send_decision = publish.get("owner_email_send_decision") if isinstance(publish.get("owner_email_send_decision"), dict) else {}
    publish_send_decision_digest = publish.get("send_decision_digest")
    publish_idempotency_send_decision_digest = publish_idempotency.get("send_decision_digest")
    publish_send_decision_nested_digest = publish_send_decision.get("send_decision_digest")
    publish_send_decision_digest_configured = (
        sha256ish(publish_send_decision_digest)
        and publish_send_decision_digest == publish_idempotency_send_decision_digest
        and publish_send_decision_digest == publish_send_decision_nested_digest
    )
    owner_email_send_guard_ok = owner_email_send_guard.get("status") == "ok" and owner_email_send_guard.get("guard_ok") is True
    owner_email_packet_packet_count = count(owner_email_packet.get("packet_count"))
    owner_email_packet_no_leak = count(owner_email_packet.get("full_history_leak_count")) == 0
    owner_email_packet_live_guard_ok = (
        owner_email_packet.get("requires_live_update_guard") is True
        and Path(str(owner_email_packet.get("live_update_capture_report") or "")).name == "baselane_financials_monthly_live_update_capture.json"
    )
    owner_email_packet_preview_file_write_allowed = owner_email_packet.get("preview_file_write_allowed")
    owner_email_packet_preview_write_blocked_reason = owner_email_packet.get("preview_write_blocked_reason")
    owner_email_packet_unsafe_preview_packet_count = count(owner_email_packet.get("unsafe_preview_packet_count"))
    owner_email_packet_stale_preview_file_removed_count = count(owner_email_packet.get("stale_preview_file_removed_count"))
    owner_email_packet_stale_preview_cleanup_error_count = count(owner_email_packet.get("stale_preview_cleanup_error_count"))
    owner_email_packet_previews = owner_email_packet.get("previews") if isinstance(owner_email_packet.get("previews"), list) else []
    owner_email_packet_preview_count = len(owner_email_packet_previews)
    owner_email_packet_preview_telemetry_present = (
        "preview_file_write_allowed" in owner_email_packet
        and "preview_write_blocked_reason" in owner_email_packet
        and "unsafe_preview_packet_count" in owner_email_packet
        and "stale_preview_file_removed_count" in owner_email_packet
        and "stale_preview_cleanup_error_count" in owner_email_packet
    )
    owner_email_packet_preview_allowed_without_packets = (
        owner_email_packet_preview_file_write_allowed is True
        and owner_email_packet_packet_count <= 0
    )
    owner_email_packet_previews_written_when_blocked = (
        owner_email_packet_preview_file_write_allowed is False
        and owner_email_packet_preview_count > 0
    )
    owner_email_packet_preview_block_reason_current = (
        owner_email_packet_preview_file_write_allowed is not False
        or (
            owner_email_packet_unsafe_preview_packet_count > 0
            and owner_email_packet_preview_write_blocked_reason == "owner email body guard blocked preview artifact writes"
        )
        or (
            owner_email_packet_packet_count <= 0
            and owner_email_packet_preview_write_blocked_reason == "no recipient email packets generated"
        )
        or (
            owner_email_packet_packet_count > 0
            and owner_email_packet_unsafe_preview_packet_count == 0
            and owner_email_packet_preview_write_blocked_reason in {None, ""}
        )
    )
    owner_email_packet_preview_safe = (
        owner_email_packet_preview_telemetry_present
        and not owner_email_packet_preview_allowed_without_packets
        and not owner_email_packet_previews_written_when_blocked
        and owner_email_packet_unsafe_preview_packet_count == 0
        and owner_email_packet_stale_preview_cleanup_error_count == 0
        and owner_email_packet_preview_block_reason_current
    )
    owner_email_packet_ready = (
        owner_email_packet.get("status") == "ok"
        and count(owner_email_packet.get("recipient_count")) > 0
        and owner_email_packet_packet_count > 0
        and owner_email_packet_no_leak
        and owner_email_packet_live_guard_ok
        and owner_email_packet_preview_safe
        and owner_email_packet.get("max_once_monthly_ok") is True
    )
    native_owner_email_ready = (
        owner_email_send_guard.get("status") == "ok"
        and owner_email_send_guard.get("guard_ok") is True
        and owner_email_send_guard.get("no_spam_guard_ok") is True
        and owner_email_send_guard.get("owner_email_packet_native_property_coverage_ok") is True
        and owner_email_send_guard.get("owner_email_packet_signal_only_ok") is True
        and count(owner_email_send_guard.get("owner_email_packet_full_history_leak_count")) == 0
        and count(owner_email_send_guard.get("owner_email_packet_monthly_financial_summary_missing_property_count")) == 0
        and count(owner_email_send_guard.get("owner_email_packet_issue_count")) == 0
    )
    owner_email_packet_required_now = readiness.get("owner_email_allowed") is True and not native_owner_email_ready
    publish_existing_send_lock_decision_digest = (
        publish.get("existing_send_lock_decision_digest")
        if "existing_send_lock_decision_digest" in publish
        else publish_idempotency.get("existing_send_lock_decision_digest")
    )
    publish_existing_send_lock_matches_send_decision = (
        publish.get("existing_send_lock_matches_send_decision")
        if "existing_send_lock_matches_send_decision" in publish
        else publish_idempotency.get("existing_send_lock_matches_send_decision")
    )
    publish_readiness_snapshot = publish.get("monthly_readiness_snapshot") if isinstance(publish.get("monthly_readiness_snapshot"), dict) else {}
    publish_readiness_snapshot_configured = publish_readiness_snapshot.get("status") not in {None, "not_configured", "missing", "unreadable"}
    publish_readiness_snapshot_current = (
        publish_readiness_snapshot_configured
        and publish_readiness_snapshot.get("status") == readiness.get("status")
        and publish_readiness_snapshot.get("owner_email_allowed") == readiness.get("owner_email_allowed")
        and int(publish_readiness_snapshot.get("blocker_count") or 0) == int(readiness.get("blocker_count") or 0)
    )
    current_owner_email_blocked_reason = (
        readiness.get("owner_email_blocked_reason") or monthly_readiness_blocked_reason(readiness)
        if readiness.get("owner_email_allowed") is not True
        else None
    )
    publish_send_blocked_reason = publish.get("send_blocked_reason")
    publish_send_decision_blocked_reason = publish_send_decision.get("blocked_reason")
    publish_idempotency_blocked_reason = publish_idempotency.get("send_blocked_reason")
    publish_blocked_reason_current = (
        current_owner_email_blocked_reason is None
        or publish_send_blocked_reason == current_owner_email_blocked_reason
        or publish_send_blocked_reason in {None, ""}
    )
    publish_nested_blocked_reasons_current = (
        current_owner_email_blocked_reason is None
        or (
            publish_send_decision_blocked_reason in {None, "", current_owner_email_blocked_reason}
            and publish_idempotency_blocked_reason in {None, "", current_owner_email_blocked_reason}
        )
    )
    publish_idempotency_configured = publish_idempotency.get("configured") is True or bool(publish.get("sent_state_file"))
    publish_send_safely_recorded = (
        (publish_sent_state_status == "written" and publish_evidence_count == publish_will_send_count and publish_will_send_count > 0)
        or (publish_sent_state_status == "skipped_no_sends_needed" and publish_will_send_count == 0)
        or (publish_sent_state_status == "not_requested" and not publish_effective_send and publish_will_send_count == 0)
    )
    publish_lock_safe = publish_send_lock_status in {
        "not_requested",
        "blocked_existing_lock",
        "cleared_after_sent_state",
        "cleared_no_sends_needed",
    }
    publish_existing_lock_digest_safe = (
        publish_send_lock_status != "blocked_existing_lock"
        or (
            sha256ish(publish_existing_send_lock_decision_digest)
            and publish_existing_send_lock_matches_send_decision is True
        )
    )
    owner_skipped_count = count(owner_review_gate.get("property_skipped_count"))
    owner_gate_summary = owner_review_gate.get("summary") if isinstance(owner_review_gate.get("summary"), dict) else {}
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
    owner_publish_excluded_count = count(owner_gate_summary.get("publish_excluded_property_count"))
    owner_pending_update_count = count(
        owner_gate_summary.get("pending_update_review_count")
        if "pending_update_review_count" in owner_gate_summary
        else review_manifest.get("pending_update_review_count")
    )
    owner_pending_financial_count = count(
        owner_gate_summary.get("pending_financial_review_count")
        if "pending_financial_review_count" in owner_gate_summary
        else review_manifest.get("pending_financial_review_count")
    )
    review_manifest_effectively_clean = (
        review_manifest.get("status") == "ok"
        or (owner_pending_update_count == 0 and owner_pending_financial_count == 0)
    )
    live_update_skipped_count = count(live_update_capture.get("skipped_index_count"))
    live_financial_skipped_count = count(live_financial_capture.get("skipped_index_count"))
    live_update_excluded_count = count(live_update_capture.get("excluded_property_count"))
    live_financial_excluded_count = count(live_financial_capture.get("excluded_property_count"))
    live_update_external_count = count(live_update_capture.get("externally_excluded_count"))
    live_financial_external_count = count(live_financial_capture.get("externally_excluded_count"))
    publish_excluded_property_count = count(publish.get("excluded_property_count"))
    publish_excluded_property_count_present = "excluded_property_count" in publish
    publish_excluded_payload_file_count = count(publish.get("excluded_payload_file_count"))
    publish_excluded_owner_email_candidate_count = count(publish.get("excluded_owner_email_candidate_count"))
    publish_excluded_no_payload_or_email = publish_excluded_payload_file_count == 0 and publish_excluded_owner_email_candidate_count == 0
    owner_review_gate_full_exclusion_scope = (
        owner_excluded_total_present
        and (
            (
                publish_excluded_property_count_present
                and owner_excluded_total_count == publish_excluded_property_count
            )
            or owner_excluded_total_count == live_update_excluded_count
            or owner_excluded_total_count == live_financial_excluded_count
        )
    )
    owner_effective_excluded_total_count = (
        owner_excluded_total_count
        if owner_review_gate_full_exclusion_scope
        else owner_publish_excluded_count
    )
    owner_effective_excluded_total_present = owner_review_gate_full_exclusion_scope or owner_publish_excluded_count > 0
    split_skipped_index_counts_match = (
        owner_skipped_count == live_update_skipped_count == live_financial_skipped_count
        if owner_review_gate_full_exclusion_scope or owner_skipped_count > 0
        else live_update_skipped_count == live_financial_skipped_count
    )
    total_exclusion_counts_match = (
        owner_effective_excluded_total_count == live_update_excluded_count == live_financial_excluded_count == publish_excluded_property_count
        if owner_effective_excluded_total_present
        else (
            live_update_excluded_count == live_financial_excluded_count == publish_excluded_property_count
            if publish_excluded_property_count_present
            else live_update_excluded_count == live_financial_excluded_count
        )
    )
    collapsed_skipped_index_counts_match = bool(
        owner_review_gate_full_exclusion_scope
        and total_exclusion_counts_match
        and live_update_external_count == 0
        and live_financial_external_count == 0
        and live_update_skipped_count == live_financial_skipped_count == owner_excluded_total_count
    )
    skipped_index_counts_match = split_skipped_index_counts_match or collapsed_skipped_index_counts_match
    skipped_index_count_mode = "collapsed_total" if collapsed_skipped_index_counts_match else "split_manual_external"
    skipped_exclusion_counts_match = total_exclusion_counts_match
    daily_scheduler_job = scheduler_job(scheduler, "daily_sync")
    daily_scheduler_issues = [str(issue) for issue in daily_scheduler_job.get("issues") or []]
    monthly_scheduler_job = scheduler_job(scheduler, "monthly_financials")
    # A monthly run can legitimately be in review or failed while its protected
    # 15th schedule remains installed. Keep execution/data failures with their
    # respective gates instead of telling the operator to recreate cron.
    monthly_scheduler_issues = [
        str(issue)
        for issue in monthly_scheduler_job.get("issues") or []
        if str(issue).startswith(("script_missing", "scheduler_", "duplicate_scheduler_references"))
    ]
    if scheduler.get("status") in {"missing", "unreadable"}:
        monthly_scheduler_issues.append(f"scheduler_audit={scheduler.get('status')}")
    if not monthly_scheduler_job:
        monthly_scheduler_issues.append("monthly_scheduler_job_missing")
    monthly_scheduler_line_guard_ok = monthly_scheduler_job.get("scheduler_line_guard_ok") is True
    if monthly_scheduler_job and not monthly_scheduler_line_guard_ok:
        monthly_scheduler_issues.append("monthly_scheduler_line_guard_not_ok")
    monthly_scheduler_report_fresh = not monthly_scheduler_issues
    monthly_scheduler_effective_ok = not monthly_scheduler_issues
    scheduler_actionable = scheduler.get("actionable_summary") if isinstance(scheduler.get("actionable_summary"), dict) else {}
    scheduler_primary = scheduler.get("primary_blocker") if isinstance(scheduler.get("primary_blocker"), dict) else {}
    if not scheduler_primary:
        scheduler_primary = (
            scheduler_actionable.get("primary_blocker")
            if isinstance(scheduler_actionable.get("primary_blocker"), dict)
            else {}
        )
    daily_scheduler_primary = (
        scheduler_primary
        if str(scheduler_primary.get("id") or "").startswith("daily_sync")
        or str(scheduler_primary.get("class") or "").startswith("scheduler.daily_sync")
        else {}
    )
    daily_disk_preflight_blocked = (
        "disk_space_preflight" in str(daily_scheduler_primary.get("id") or "")
        or "disk_space_preflight" in str(daily_scheduler_primary.get("class") or "")
        or str(daily.get("disk_space_preflight_status") or "").strip() not in {"", "ok", "missing"}
    )
    daily_disk_preflight_artifact = (
        daily_scheduler_primary.get("artifact") or "reports/baselane_daily_disk_space_preflight_report.json"
    )
    daily_disk_preflight_next_action = str(
        daily_scheduler_primary.get("next_action")
        or daily_disk_preflight.get("next_action")
        or daily.get("next_action")
        or "Free local Dropbox/Windows disk space, rerun scripts/baselane_cron_run.sh, then rerun EOD."
    )
    current_daily_disk_preflight_issues = disk_space_preflight_issues(daily_disk_preflight)
    if daily_disk_preflight_blocked and current_daily_disk_preflight_issues:
        daily_scheduler_primary = dict(daily_scheduler_primary or {})
        daily_scheduler_primary.setdefault("id", "daily_sync_disk_space_preflight")
        daily_scheduler_primary.setdefault("class", "scheduler.daily_sync_disk_space_preflight")
        daily_scheduler_primary["artifact"] = "reports/baselane_daily_disk_space_preflight_report.json"
        daily_scheduler_primary["blocker"] = (
            daily_scheduler_primary.get("blocker") or "Low local disk space blocks daily Baselane sync"
        )
        daily_scheduler_primary["summary"] = daily_disk_space_blocker_summary(current_daily_disk_preflight_issues)
        daily_scheduler_primary["disk_space_preflight_issues"] = current_daily_disk_preflight_issues
        if daily_disk_preflight.get("next_action"):
            daily_scheduler_primary.setdefault("next_action", daily_disk_preflight.get("next_action"))
        daily_scheduler_primary.setdefault("source_report", "reports/baselane_daily_sync_report.json")
    canonical_daily_available = canonical_daily.get("status") not in {"missing", "unreadable"}
    canonical_daily_effective_status = canonical_daily.get("effective_status") or canonical_daily.get("status")
    canonical_daily_reconciled_ok = (
        canonical_daily_available
        and canonical_daily_effective_status == "ok"
        and canonical_daily.get("effective_return_code", canonical_daily.get("return_code")) == 0
        and canonical_daily.get("sync_report_status") == "ok"
        and daily.get("sync_report_status") == "ok"
        and sync.get("status") == "ok"
    )
    if canonical_daily_reconciled_ok:
        daily_scheduler_issues = [
            issue
            for issue in daily_scheduler_issues
            if not (
                issue.startswith("unexpected_report_status:")
                or issue.startswith("report_unexpected_value:return_code=")
                or issue.startswith("report_unexpected_value:sync_report_status=")
            )
        ]
        if canonical_daily.get("human_paced_backup_policy") == "deterministic_primary_human_paced_backup":
            daily_scheduler_issues = [
                issue
                for issue in daily_scheduler_issues
                if issue != "report_missing_field:human_paced_backup_policy"
            ]
        if canonical_daily.get("human_paced_backup_enabled") is True:
            daily_scheduler_issues = [
                issue
                for issue in daily_scheduler_issues
                if issue != "report_missing_field:human_paced_backup_enabled"
            ]
        if canonical_daily.get("human_paced_backup_script_exists") is True:
            daily_scheduler_issues = [
                issue
                for issue in daily_scheduler_issues
                if issue != "report_missing_field:human_paced_backup_script_exists"
            ]
    daily_report_age_hours = daily_scheduler_job.get("report_age_hours")
    daily_report_max_age_hours = daily_scheduler_job.get("max_report_age_hours")
    daily_run_age_hours = daily.get("daily_run_age_hours")
    if daily_run_age_hours in (None, ""):
        daily_run_age_hours = iso_age_hours(daily.get("ended_at"))
    try:
        daily_run_age_value = float(daily_run_age_hours)
    except (TypeError, ValueError):
        daily_run_age_value = None
    try:
        daily_run_max_age_hours = float(daily_report_max_age_hours)
    except (TypeError, ValueError):
        daily_run_max_age_hours = 36.0
    daily_run_fresh = (
        daily_run_age_value is not None
        and daily_run_age_value >= -1
        and daily_run_age_value <= daily_run_max_age_hours
    )
    daily_report_fresh = not daily_scheduler_issues
    scheduler_effective_ok = not daily_scheduler_issues
    daily_steps = daily.get("steps") if isinstance(daily.get("steps"), dict) else {}
    daily_required_step_names = ["deterministic_sync"]
    daily_missing_step_names = [name for name in daily_required_step_names if name not in daily_steps]
    daily_effective_status = daily.get("effective_status") or daily.get("status")
    daily_effective_return_code = (
        daily.get("effective_return_code")
        if "effective_return_code" in daily
        else daily.get("return_code")
    )
    daily_effective_failed_step = (
        daily.get("effective_failed_step")
        if "effective_failed_step" in daily
        else daily.get("failed_step")
    )
    daily_report_complete = (
        "ended_at" in daily
        and non_negative_int(daily.get("duration_seconds"))
        and daily_effective_return_code == 0
        and "failed_step" in daily
        and daily_effective_failed_step in {None, ""}
        and daily.get("sync_report_status") == "ok"
        and not daily_missing_step_names
    )
    daily_deterministic_sync_ok = (
        daily_effective_status == "ok"
        and ok_status(sync)
        and scheduler_effective_ok
        and daily_report_fresh
        and daily_report_complete
        and daily_run_fresh
    )
    transfer_report_path = reports / "baselane_lofty_transfer_requirements.json"
    current_transfer_report_digest = stable_transfer_report_digest(transfer_report_path)
    transfer_telegram_send_status_ok = transfer_telegram_send.get("status") in {"ok", "ok_previous"}
    transfer_telegram_state_status_ok = transfer_telegram_sent_state.get("status") in {"ok", "ok_previous"}
    transfer_telegram_report_digest = str(transfer_telegram_send.get("transfer_report_digest") or "")
    transfer_telegram_state_report_digest = str(transfer_telegram_sent_state.get("transfer_report_digest") or "")
    transfer_telegram_message_digest = str(transfer_telegram_send.get("message_sha256") or "")
    transfer_telegram_state_message_digest = str(transfer_telegram_sent_state.get("message_sha256") or "")
    transfer_telegram_state_message_text = str(transfer_telegram_sent_state.get("message_text") or "")
    transfer_telegram_state_message_text_digest_ok = (
        not transfer_telegram_state_message_text
        or transfer_telegram_state_message_digest == sha256_text(transfer_telegram_state_message_text.strip())
    )
    transfer_telegram_digest_current = (
        sha256ish(current_transfer_report_digest)
        and transfer_telegram_report_digest == current_transfer_report_digest
        and transfer_telegram_state_report_digest == current_transfer_report_digest
    )
    transfer_telegram_message_digest_bound = (
        sha256ish(transfer_telegram_message_digest)
        and transfer_telegram_state_message_digest == transfer_telegram_message_digest
        and transfer_telegram_state_message_text_digest_ok
    )
    transfer_telegram_send_state_fresh = required_fresh_generated_at(
        {"generated_at": transfer_telegram_sent_state.get("sent_at")},
        MONTHLY_TRANSFER_TELEGRAM_MAX_AGE_HOURS,
    )
    transfer_telegram_send_fresh = required_fresh_generated_at(
        transfer_telegram_send,
        MONTHLY_TRANSFER_TELEGRAM_MAX_AGE_HOURS,
    )
    transfer_telegram_current_for_run_ok = transfer_telegram_send.get("transfer_report_current_for_run") is not False
    transfer_telegram_delivery_ok = (
        transfer_reconciliation.get("status") in {"ok", "review", "blocked_source_not_clean"}
        and transfer_telegram_send_status_ok
        and transfer_telegram_send.get("send_requested") is True
        and transfer_telegram_send.get("dry_run") is False
        and transfer_telegram_send.get("telegram_send_ok") is True
        and bool(transfer_telegram_send.get("telegram_http_statuses") or [])
        and transfer_telegram_send.get("message_quality_ok") is True
        and transfer_telegram_send.get("send_safe") is True
        and transfer_telegram_send.get("transfer_report_digest_matches_expected") is True
        and transfer_telegram_send.get("transfer_report_digest_matches_current") is True
        and transfer_telegram_send.get("message_matches_transfer_report_telegram_summary") is True
        and transfer_telegram_state_status_ok
        and transfer_telegram_sent_state.get("telegram_send_ok") is True
        and transfer_telegram_digest_current
        and transfer_telegram_message_digest_bound
        and transfer_telegram_send_fresh
        and transfer_telegram_send_state_fresh
        and transfer_telegram_current_for_run_ok
    )
    local_model_direct_smoke = local_model.get("direct_smoke") if isinstance(local_model.get("direct_smoke"), dict) else {}
    local_model_finance_smoke = local_model.get("finance_contract_smoke") if isinstance(local_model.get("finance_contract_smoke"), dict) else {}
    local_model_contract = local_model.get("validation_contract") if isinstance(local_model.get("validation_contract"), dict) else {}
    local_model_scope = local_model.get("model_execution_scope") if isinstance(local_model.get("model_execution_scope"), dict) else {}
    local_model_policy = local_model.get("small_model_execution_policy") if isinstance(local_model.get("small_model_execution_policy"), dict) else {}
    local_model_contract_scope = (
        local_model_contract.get("model_execution_scope") if isinstance(local_model_contract.get("model_execution_scope"), dict) else {}
    )
    local_model_ok = local_model_preflight_ok(local_model)
    local_model_blockers = [] if local_model_ok else [
        blocker
        for blocker in [
            None if local_model.get("status") == "ok" else f"local_model={local_model.get('status')}",
            None if local_model.get("model") == EXPECTED_LOCAL_MODEL else f"model={local_model.get('model')}",
            None if local_model.get("provider") == EXPECTED_LOCAL_PROVIDER else f"provider={local_model.get('provider')}",
            None if local_model.get("model_id") == EXPECTED_LOCAL_MODEL_ID else f"model_id={local_model.get('model_id')}",
            None if int(local_model.get("issue_count") or 0) == 0 else f"issue_count={local_model.get('issue_count')}",
            None if local_model.get("configured_model_present") is True else "configured_model_present=false",
            None
            if local_model.get("selected_endpoint_from_config") is True
            else f"selected_endpoint_from_config={local_model.get('selected_endpoint_from_config')}",
            None if local_model.get("model_available") is True else "model_available=false",
            None if local_model.get("local_model_operational") is True else "local_model_operational=false",
            None if local_model.get("small_model_execution_allowed") is False else "small_model_execution_allowed=true",
            None if local_model.get("small_model_pipeline_execution_allowed") is False else "small_model_pipeline_execution_allowed=true",
            None if local_model.get("small_model_task_scoped_execution_allowed") is True else "small_model_task_scoped_execution_allowed=false",
            None if local_model.get("small_model_financial_authority") is False else "small_model_financial_authority=true",
            None if local_model.get("small_model_live_side_effects_allowed") is False else "small_model_live_side_effects_allowed=true",
            None if local_model_scope.get("deterministic_only") is True else "model_scope_deterministic_only=false",
            None if local_model_scope.get("pipeline_execution_allowed") is False else "model_scope_pipeline_execution_allowed=true",
            None
            if local_model_scope.get("allowed_task_class") == EXPECTED_LOCAL_MODEL_TASK_CLASS
            else f"model_scope_allowed_task_class={local_model_scope.get('allowed_task_class')}",
            None if local_model_scope.get("model_financial_authority") is False else "model_scope_financial_authority=true",
            None if local_model_scope.get("live_side_effects_allowed") is False else "model_scope_live_side_effects_allowed=true",
            None
            if local_model_policy.get("permitted_task_class") == EXPECTED_LOCAL_MODEL_TASK_CLASS
            else f"small_model_policy_task_class={local_model_policy.get('permitted_task_class')}",
            None if local_model_policy.get("pipeline_execution_allowed") is False else "small_model_policy_pipeline_execution_allowed=true",
            None if local_model_policy.get("model_financial_authority") is False else "small_model_policy_financial_authority=true",
            None if local_model_policy.get("live_side_effects_allowed") is False else "small_model_policy_live_side_effects_allowed=true",
            None if local_model_direct_smoke.get("attempted") is True else "direct_smoke_not_attempted",
            None if local_model_finance_smoke.get("attempted") is True else "finance_contract_smoke_not_attempted",
            None if local_model_finance_smoke.get("ok") is True else "finance_contract_smoke_not_ok",
            None if local_model_contract.get("selected_endpoint_from_config") is True else "validation_contract_endpoint_not_from_config",
            None if local_model_contract.get("direct_smoke_ok") is True else "validation_contract_direct_smoke_not_ok",
            None if local_model_contract.get("finance_contract_smoke_ok") is True else "validation_contract_finance_smoke_not_ok",
            None if local_model_contract.get("model_scope_deterministic") is True else "validation_contract_model_scope_deterministic=false",
            None if local_model_contract.get("model_pipeline_execution_denied") is True else "validation_contract_pipeline_execution_denied=false",
            None if local_model_contract.get("model_financial_authority_denied") is True else "validation_contract_financial_authority_denied=false",
            None if local_model_contract.get("model_live_side_effects_denied") is True else "validation_contract_live_side_effects_denied=false",
            None
            if local_model_contract_scope.get("deterministic_only") is True
            else "validation_contract_scope_deterministic_only=false",
            None
            if local_model_contract_scope.get("pipeline_execution_allowed") is False
            else "validation_contract_scope_pipeline_execution_allowed=true",
            None if sha256ish(local_model.get("validation_digest")) else "validation_digest_missing",
            None if fresh_generated_at(local_model) else f"local_model_preflight_stale_hours={iso_age_hours(local_model.get('generated_at'))}",
        ]
        if blocker
    ]
    source_fix_reports_effectively_clear = (
        source_fix_remaining_count == 0
        and source_fix_approval_pending_count == 0
        and source_fix_approval_invalid_count == 0
        and source_fix_validation_pending_count == 0
        and source_fix_validation_invalid_count == 0
        and source_fix_apply_ready_count == 0
        and source_fix_apply_refresh_count == 0
        and source_fix_apply_blocked_count == 0
    )
    audit_error_class_counts = weekly_cf.get("audit_error_class_counts") if isinstance(weekly_cf.get("audit_error_class_counts"), dict) else {}
    no_gl_property_match_count = max(
        count(cf_no_gl_property_match.get("no_gl_property_match_count")),
        count(audit_error_class_counts.get("no_gl_property_match")),
    )
    weekly_cf_hard_blocker_counts = {
        "audit_error_count": count(weekly_cf.get("audit_error_count")),
        "no_gl_property_match_count": no_gl_property_match_count,
        "conflict_count": count(weekly_cf.get("conflict_count")),
        "missing_canonical_cf_count": count(weekly_cf.get("missing_canonical_cf_count")),
        "no_mortgage_debt_violation_count": count(weekly_cf.get("no_mortgage_debt_violation_count")),
    }
    weekly_cf_hard_blockers_clear = source_cash_balance_violation_count == 0 and all(
        value == 0 for value in weekly_cf_hard_blocker_counts.values()
    )
    if isinstance(weekly_cf.get("effective_ok"), bool):
        weekly_cf_base_effective_ok = weekly_cf.get("effective_ok") is True and weekly_cf_hard_blockers_clear
    else:
        weekly_cf_base_effective_ok = (
            weekly_cf.get("status") == "ok"
            or (
                weekly_cf.get("status") == "review"
                and weekly_cf_gate.get("status") == "ok"
                and source_fix_reports_effectively_clear
                and weekly_cf_hard_blockers_clear
            )
        )
    weekly_cf_effective_ok = weekly_cf_base_effective_ok
    weekly_reason_parts = [part for part in str(weekly.get("reason") or "").split(";") if part]
    stale_weekly_reason_parts = {
        "cf_statement_sync_review" if weekly_cf_base_effective_ok else "",
        "cf_review_gate_review" if weekly_cf_gate.get("status") == "ok" else "",
        "ecogl_data_quality_hold" if source_fix_reports_effectively_clear else "",
        "ecogl_source_fix_queue" if source_fix_reports_effectively_clear else "",
        "ecogl_source_fix_evidence" if source_fix_reports_effectively_clear else "",
        "ecogl_source_fix_correction_validation" if source_fix_reports_effectively_clear else "",
        "ecogl_source_fix_apply_plan" if source_fix_reports_effectively_clear else "",
    }
    active_weekly_reason_parts = [
        part for part in weekly_reason_parts if part not in stale_weekly_reason_parts
    ]
    weekly_file_updates_effective_ok = weekly.get("status") == "ok" or (
        weekly.get("status") == "review"
        and not active_weekly_reason_parts
        and (
            weekly.get("deterministic_verification_idempotent") is True
            or weekly_review_retry_safe
        )
    )
    weekly_unprocessed_idempotent = (
        weekly.get("weekly_unprocessed_idempotent")
        if "weekly_unprocessed_idempotent" in weekly
        else weekly_review_safe_idempotency.get("weekly_unprocessed_idempotent")
    )
    weekly_state_file_unmarked = (
        weekly.get("state_file_unmarked")
        if "state_file_unmarked" in weekly
        else weekly_review_safe_idempotency.get("state_file_unmarked")
    )
    weekly_retry_required = (
        weekly.get("retry_required")
        if "retry_required" in weekly
        else weekly_review_safe_idempotency.get("retry_required")
    )
    weekly_scheduled_skip_ok = (
        weekly.get("status") == "skipped_not_friday"
        and weekly.get("reason") == "not_friday"
        and int(weekly.get("return_code") or 0) == 0
        and (weekly.get("day_of_week") != 5 if weekly.get("day_of_week") is not None else True)
        and (
            weekly_unprocessed_idempotent is True
            or (
                weekly_review_safe_idempotency.get("scheduled_noop") is True
                and weekly_review_safe_idempotency.get("safe_to_skip_next_run") is True
                and weekly_review_safe_idempotency.get("deterministic_verification_idempotent") is True
            )
        )
        and weekly_state_file_unmarked is False
        and weekly_retry_required is False
    ) or (
        weekly.get("status") == "already_done_for_week"
        and weekly.get("reason") == "state_file_matches_iso_week"
        and int(weekly.get("return_code") or 0) == 0
        and bool(weekly.get("iso_week"))
        and weekly.get("last_completed_week") == weekly.get("iso_week")
    )
    if weekly_scheduled_skip_ok and weekly.get("stale_downstream_gate_suppressed") is True:
        weekly_cf_gate_snapshot_current = True
        weekly_review_safe_idempotency_evidence["cf_review_gate_snapshot_current"] = True
    weekly_file_updates_effective_ok = weekly_file_updates_effective_ok or weekly_scheduled_skip_ok
    monthly_statements_captured = count(monthly_statements_gate.get("captured_unique_count"))
    monthly_statements_min_captured = count(monthly_statements_gate.get("min_captured_required"))
    monthly_statements_download_error = str(monthly_statements_download.get("error") or "")
    monthly_statements_download_error_class = None
    if "CDP command timed out:" in monthly_statements_download_error:
        monthly_statements_download_error_class = (
            "cdp-timeout "
            + monthly_statements_download_error.split("CDP command timed out:", 1)[1].splitlines()[0].strip()
        )
    elif "login form submission failed" in monthly_statements_download_error or "AUTH_REQUIRED" in monthly_statements_download_error:
        monthly_statements_download_error_class = "auth-required"
    elif "unitAPINonSensitiveToken" in monthly_statements_download_error and "404" in monthly_statements_download_error:
        monthly_statements_download_error_class = "unit-token-404"
    elif "no statement download buttons discovered" in monthly_statements_download_error:
        monthly_statements_download_error_class = "no-statement-buttons"
    elif "no new PDF files" in monthly_statements_download_error:
        monthly_statements_download_error_class = "no-new-pdfs"
    elif monthly_statements_download.get("ok") is False and monthly_statements_download_error:
        monthly_statements_download_error_class = "download-failed"
    monthly_statements_verified_existing_capture = (
        monthly_statements_gate.get("download_report_not_used") is True
        and monthly_statements_captured >= monthly_statements_min_captured
        and monthly_statements_min_captured > 0
    )
    monthly_statements_download_verified = (
        monthly_statements_gate.get("download_ok") is True or monthly_statements_verified_existing_capture
    )
    monthly_statements_ok = (
        monthly_statements_gate.get("status") == "ok"
        and int(monthly_statements_gate.get("monthly_script_return_code") or 0) == 0
        and monthly_statements_download_verified
        and monthly_statements_captured >= monthly_statements_min_captured
        and monthly_statements_min_captured > 0
        and statement_target_matches(monthly_statements_gate, run_month)
        and required_fresh_generated_at(monthly_statements_gate, MONTHLY_STATEMENTS_MAX_AGE_HOURS)
    )
    weekly_idempotency_blockers = []
    if weekly.get("deterministic_verification_idempotent") is not True and not weekly_review_retry_safe and not weekly_scheduled_skip_ok:
        if weekly_review_safe_idempotency.get("weekly_unprocessed_idempotent") is not True:
            weekly_idempotency_blockers.append("weekly_unprocessed_idempotent=false")
        if weekly_review_safe_idempotency.get("state_file_unmarked") is not True:
            weekly_idempotency_blockers.append("weekly_state_file_unmarked=false")
        if weekly_review_safe_idempotency.get("retry_required") is not True:
            weekly_idempotency_blockers.append("weekly_retry_required=false")
        if weekly_review_safe_idempotency.get("safe_to_skip_next_run") is not False:
            weekly_idempotency_blockers.append("weekly_safe_to_skip_next_run_not_false")
        if not weekly_idempotency_blockers:
            weekly_idempotency_blockers.append("weekly_review_safe_idempotency_missing_or_stale")
    readiness_primary = readiness_primary_blocker(readiness)
    readiness_primary_text = str(readiness_primary.get("blocker") or readiness_primary.get("class") or "").strip()
    monthly_readiness_daily_sync_stale = monthly_readiness_daily_sync_blocker_stale(
        readiness,
        daily_deterministic_sync_ok,
    )
    current_owner_email_blocked_reason = owner_email_blocked_reason(readiness, daily_deterministic_sync_ok)
    monthly_readiness_blocks_downstream = readiness.get("owner_email_allowed") is False and bool(readiness_primary_text)
    live_update_target_count = int(live_update_capture.get("target_count") or 0)
    live_update_check_ok_count = int(live_update_capture.get("check_ok_count") or 0)
    live_update_mismatch_count = int(live_update_capture.get("mismatch_count") or 0)
    live_update_register_count = int(live_update_capture.get("register_count") if live_update_capture.get("register_count") is not None else live_update_check_ok_count)
    live_financial_target_count = int(live_financial_capture.get("target_count") or 0)
    live_financial_check_ok_count = int(live_financial_capture.get("check_ok_count") or 0)
    live_financial_mismatch_count = int(live_financial_capture.get("mismatch_count") or 0)
    live_financial_register_count = int(live_financial_capture.get("register_count") if live_financial_capture.get("register_count") is not None else live_financial_check_ok_count)
    live_financial_capture_ready_statuses = {
        "guard_ok",
        "guard_ok_live_distribution",
        "guard_ok_no_distribution_target",
        "needs_reconcile",
    }
    live_financial_capture_apply_ready_count = sum(
        1
        for record in live_financial_capture.get("records") or []
        if isinstance(record, dict)
        and record.get("status") in live_financial_capture_ready_statuses
        and count(record.get("live_financials_length")) > 0
        and bool(str(record.get("snapshot_path") or record.get("next_action_file") or "").strip())
    )
    live_update_capture_fresh = fresh_generated_at(live_update_capture, LIVE_CAPTURE_MAX_AGE_HOURS)
    live_financial_capture_fresh = fresh_generated_at(live_financial_capture, LIVE_CAPTURE_MAX_AGE_HOURS)
    live_update_capture_registered = (
        live_update_capture.get("status") in {"ok", "review"}
        and live_update_capture_fresh
        and live_update_target_count == live_update_register_count
    )
    live_financial_capture_registered = (
        live_financial_capture.get("status") in {"ok", "review"}
        and live_financial_capture_fresh
        and live_financial_target_count == live_financial_register_count
    )
    live_update_capture_ready = (
        live_update_capture.get("status") == "ok"
        and live_update_capture_fresh
        and live_update_target_count == live_update_check_ok_count
        and live_update_mismatch_count == 0
    )
    live_financial_capture_ready = (
        live_financial_capture.get("status") in {"ok", "review"}
        and live_financial_capture_fresh
        and live_financial_target_count > 0
        and live_financial_target_count == live_financial_capture_apply_ready_count
        and live_financial_mismatch_count == 0
    )
    lofty_financial_patch_property_count = count(lofty_financial_patch_readiness.get("property_count"))
    lofty_financial_patch_status_counts = (
        lofty_financial_patch_readiness.get("record_status_counts")
        if isinstance(lofty_financial_patch_readiness.get("record_status_counts"), dict)
        else {}
    )
    lofty_financial_patch_skipped_count = sum(
        count(value)
        for status, value in lofty_financial_patch_status_counts.items()
        if str(status).startswith("skipped_")
    )
    lofty_financial_patch_active_count = max(
        0,
        lofty_financial_patch_property_count - lofty_financial_patch_skipped_count,
    )
    lofty_financial_patch_ready_count = count(lofty_financial_patch_readiness.get("ready_financial_patch_count"))
    lofty_financial_patch_guard_reconcile_required_count = count(lofty_financial_patch_readiness.get("guard_reconcile_required_count"))
    lofty_financial_patch_blocked_count = count(lofty_financial_patch_readiness.get("blocked_count"))
    lofty_financial_patch_field_count_total = count(lofty_financial_patch_readiness.get("field_count_total"))
    lofty_financial_patch_ready = (
        lofty_financial_patch_readiness.get("status") == "ok"
        and lofty_financial_patch_readiness.get("mutates_lofty_listing") is False
        and lofty_financial_patch_readiness.get("sends_owner_email") is False
        and lofty_financial_patch_active_count > 0
        and lofty_financial_patch_ready_count == lofty_financial_patch_active_count
        and lofty_financial_patch_guard_reconcile_required_count == 0
        and lofty_financial_patch_blocked_count == 0
        and lofty_financial_patch_field_count_total > 0
    )
    lofty_cdp_ready = cdp_preflight.get("status") == "ok"
    live_guard_workflow_ready = (
        lofty_cdp_ready
        and live_update_capture_ready
        and live_financial_capture_ready
        and lofty_financial_patch_ready
        and skipped_exclusion_counts_match
        and publish_excluded_no_payload_or_email
    )
    live_guard_capture_prereqs_ready = lofty_cdp_ready and live_update_capture_registered and live_financial_capture_registered
    live_guard_blocker = f"lofty_cdp={cdp_preflight.get('status')}:pm_tabs={cdp_preflight.get('pm_tab_count')},login_tabs={cdp_preflight.get('login_tab_count')}"
    live_guard_summary = (
        live_guard_blocker
        if not lofty_cdp_ready
        else (
            "live_guard_capture_missing="
            f"updates:{live_update_register_count}/{live_update_target_count},"
            f"financials:{live_financial_register_count}/{live_financial_target_count}"
            if not (live_update_capture_registered and live_financial_capture_registered)
            else "live_guard_reconcile_required="
            f"updates:{live_update_check_ok_count}/{live_update_target_count},"
            f"financials:{live_financial_capture_apply_ready_count}/{live_financial_target_count}"
        )
    )
    rent_roll_source = comms_stats.get("rent_roll_source") if isinstance(comms_stats.get("rent_roll_source"), dict) else {}
    rent_roll_blocks_live_apply = (
        str(rent_roll_source.get("freshness_status") or comms_stats.get("rent_roll_freshness_status") or "").strip()
        in {"missing", "stale"}
        or rent_roll_source.get("source_current") is False
        or comms_stats.get("rent_roll_live_update_allowed") is False
        or int(comms_stats.get("gap_review_source_blocker_count") or 0) > 0
    )
    monthly_local_dry_run_ready = (
        review_manifest_effectively_clean
        and owner_pending_update_count == 0
        and owner_pending_financial_count == 0
        and owner_guard_workflow.get("status") in {None, "ok"}
        and guarded_apply.get("status") == "ok"
        and guarded_apply_counts.get("guard_audit_status") in {None, "ok"}
        and publish.get("status") == "ok"
    )
    guarded_apply_waiting_for_live_guards = (guarded_apply.get("status") != "ok" or guarded_apply.get("apply") is not True) and not live_guard_capture_prereqs_ready
    guarded_apply_waiting_for_current_rent_roll = (
        guarded_apply.get("apply") is not True
        and rent_roll_blocks_live_apply
        and monthly_local_dry_run_ready
    )
    monthly_guarded_apply_held_by_daily_disk_preflight = bool(daily_disk_preflight_blocked)
    owner_review_gate_waiting_for_live_guards = (
        owner_review_gate.get("status") in {"ok", "review"}
        and owner_pending_update_count == 0
        and owner_pending_financial_count == 0
        and owner_guard_workflow.get("status") in {None, "ok"}
        and guarded_apply_waiting_for_live_guards
    )
    owner_review_gate_waiting_for_current_rent_roll = (
        owner_review_gate.get("status") in {"ok", "review"}
        and owner_pending_update_count == 0
        and owner_pending_financial_count == 0
        and owner_guard_workflow.get("status") in {None, "ok"}
        and guarded_apply_waiting_for_current_rent_roll
    )
    owner_review_gate_primary_blocker = (
        owner_review_gate.get("primary_blocker")
        if isinstance(owner_review_gate.get("primary_blocker"), dict)
        else {}
    )
    monthly_guarded_apply_blockers = [
        blocker
        for blocker in [
            "monthly_guarded_apply_held_by_daily_disk_preflight"
            if monthly_guarded_apply_held_by_daily_disk_preflight
            else None,
            None if monthly_guarded_apply_held_by_daily_disk_preflight or owner_pending_update_count == 0 else f"pending_update_reviews={owner_pending_update_count}",
            None if monthly_guarded_apply_held_by_daily_disk_preflight or owner_pending_financial_count == 0 else f"pending_financial_reviews={owner_pending_financial_count}",
            None
            if monthly_guarded_apply_held_by_daily_disk_preflight
            or owner_review_gate.get("status") == "ok"
            or owner_review_gate_waiting_for_live_guards
            or owner_review_gate_waiting_for_current_rent_roll
            else f"owner_review_gate={owner_review_gate.get('status')}:{owner_review_gate.get('blocker_count')}",
            None if monthly_guarded_apply_held_by_daily_disk_preflight or owner_guard_workflow.get("status") in {None, "ok"} else f"owner_review_gate_guard_workflow={owner_guard_workflow.get('status')}",
            (
                None
                if monthly_guarded_apply_held_by_daily_disk_preflight
                else
                f"guarded_apply_waiting_for_live_guards={live_guard_summary}"
                if guarded_apply_waiting_for_live_guards
                else "guarded_apply_waiting_for_current_rent_roll"
                if guarded_apply_waiting_for_current_rent_roll
                else None if guarded_apply.get("status") == "ok" and guarded_apply.get("apply") is True else f"guarded_apply={guarded_apply.get('status')},apply={guarded_apply.get('apply')}"
            ),
            (
                None
                if monthly_guarded_apply_held_by_daily_disk_preflight
                or guarded_apply_waiting_for_live_guards
                or guarded_apply_waiting_for_current_rent_roll
                or not (guarded_apply_counts.get("guard_failed_update_count") or guarded_apply_counts.get("guard_failed_financial_count"))
                else f"guarded_apply_guard_failed=updates:{guarded_apply_counts.get('guard_failed_update_count')},financials:{guarded_apply_counts.get('guard_failed_financial_count')}"
            ),
            (
                None
                if monthly_guarded_apply_held_by_daily_disk_preflight
                or guarded_apply_waiting_for_live_guards
                or guarded_apply_waiting_for_current_rent_roll
                or guarded_apply_counts.get("guard_audit_status") in {None, "ok"}
                else f"guard_audit={guarded_apply_counts.get('guard_audit_status')}:{guarded_apply_counts.get('guard_audit_issue_count')}"
            ),
        ]
        if blocker
    ]
    live_guard_workflow_blockers = [
        blocker
        for blocker in [
            None if lofty_cdp_ready else live_guard_blocker,
            None if not lofty_cdp_ready or live_update_capture_fresh else f"live_updates_capture_stale_hours={iso_age_hours(live_update_capture.get('generated_at'))}",
            None if not lofty_cdp_ready or live_financial_capture_fresh else f"live_financials_capture_stale_hours={iso_age_hours(live_financial_capture.get('generated_at'))}",
            None
            if not lofty_cdp_ready or live_update_capture_registered
            else f"live UPDATES.md guards not captured for all targets ({live_update_register_count}/{live_update_target_count})",
            None
            if not lofty_cdp_ready or live_financial_capture_registered
            else f"live FINANCIALS.md guards not captured for all targets ({live_financial_register_count}/{live_financial_target_count})",
            None
            if not lofty_cdp_ready or not live_update_capture_registered or live_update_capture_ready
            else f"live UPDATES.md guard reconcile required ({live_update_check_ok_count}/{live_update_target_count})",
            None
            if not lofty_cdp_ready or not live_update_mismatch_count
            else f"live UPDATES.md mismatch count={live_update_mismatch_count}",
            None
            if not lofty_cdp_ready or not live_financial_capture_registered or live_financial_capture_ready
            else f"live FINANCIALS.md capture not apply-ready ({live_financial_capture_apply_ready_count}/{live_financial_target_count})",
            None
            if not lofty_cdp_ready or not live_financial_mismatch_count
            else f"live FINANCIALS.md distribution mismatch count={live_financial_mismatch_count}",
            (
                None
                if lofty_financial_patch_ready
                else (
                    f"lofty_financial_patch_readiness={lofty_financial_patch_readiness.get('status')}:"
                    f"ready={lofty_financial_patch_ready_count}/{lofty_financial_patch_active_count},"
                    f"guard_reconcile={lofty_financial_patch_guard_reconcile_required_count},"
                    f"blocked={lofty_financial_patch_blocked_count}"
                )
            ),
            None if skipped_exclusion_counts_match else "skipped_closed_exclusion_count_mismatch",
            None if publish_excluded_no_payload_or_email else "excluded_closed_property_has_publish_or_email_artifact",
        ]
        if blocker
    ]
    requirements = [
        requirement(
            "daily_deterministic_sync",
            "Deterministic Baselane daily sync cron is healthy",
            daily_deterministic_sync_ok,
            {
                "canonical_daily_sync_report_status": canonical_daily.get("status"),
                "canonical_daily_sync_report_issue_count": canonical_daily.get("issue_count"),
                "legacy_daily_run_report_status": legacy_daily.get("status"),
                "daily_status": daily.get("status"),
                "daily_return_code": daily.get("return_code"),
                "daily_failed_step": daily.get("failed_step"),
                "daily_effective_status": daily_effective_status,
                "daily_effective_return_code": daily_effective_return_code,
                "daily_effective_failed_step": daily_effective_failed_step,
                "daily_wrapper_consistency_issues": daily.get("wrapper_consistency_issues") or [],
                "daily_wrapper_failure_window_hours": daily.get("daily_wrapper_failure_window_hours"),
                "daily_wrapper_failure_distinct_run_count": daily.get("daily_wrapper_failure_distinct_run_count"),
                "daily_recovered_sync_repeat_count": daily.get("daily_recovered_sync_repeat_count"),
                "daily_wrapper_failure_records_bounded": daily.get("daily_wrapper_failure_records_bounded") or [],
                "daily_started_at": daily.get("started_at"),
                "daily_ended_at": daily.get("ended_at"),
                "daily_duration_seconds": daily.get("duration_seconds"),
                "daily_run_age_hours": daily_run_age_hours,
                "daily_run_max_age_hours": daily_run_max_age_hours,
                "daily_sync_report_status": daily.get("sync_report_status"),
                "daily_steps": daily_steps,
                "daily_wrapper_steps": daily.get("wrapper_steps") if isinstance(daily.get("wrapper_steps"), dict) else {},
                "daily_missing_step_names": daily_missing_step_names,
                "sync_status": sync.get("status"),
                "scheduler_status": scheduler.get("status"),
                "scheduler_issue_count": scheduler.get("issue_count"),
                "scheduler_effective_ok": scheduler_effective_ok,
                "daily_report_age_hours": daily_report_age_hours,
                "daily_report_max_age_hours": daily_report_max_age_hours,
                "daily_scheduler_issues": daily_scheduler_issues,
                "daily_scheduler_primary_blocker": daily_scheduler_primary,
            },
            [
                blocker
                for blocker in [
                    None if daily_effective_status == "ok" else f"daily_run={daily_effective_status}",
                    None if canonical_daily_available else f"canonical_daily_sync_report={canonical_daily.get('status')}",
                    None if ok_status(sync) else f"sync={sync.get('status')}",
                    None if scheduler_effective_ok else f"scheduler={scheduler.get('status')}",
                    None if daily_report_fresh else f"daily_scheduler_issues={','.join(daily_scheduler_issues)}",
                    None if "ended_at" in daily else "daily_report_missing_ended_at",
                    None if non_negative_int(daily.get("duration_seconds")) else "daily_report_missing_duration_seconds",
                    None if daily_effective_return_code == 0 else f"daily_effective_return_code={daily_effective_return_code}",
                    None if "failed_step" in daily else "daily_report_missing_failed_step",
                    None if daily_effective_failed_step in {None, ""} else f"daily_effective_failed_step={daily_effective_failed_step}",
                    None if daily.get("sync_report_status") == "ok" else f"daily_sync_report_status={daily.get('sync_report_status')}",
                    None if daily_run_fresh else f"daily_run_stale_or_missing_age={daily_run_age_hours}",
                    None if not daily_missing_step_names else f"daily_steps_missing={','.join(daily_missing_step_names)}",
                ]
                if blocker
            ],
        ),
        requirement(
            "monthly_15th_scheduler",
            "Monthly financials cron is scheduled on the 15th",
            monthly_scheduler_effective_ok and monthly_scheduler_report_fresh,
            {
                "scheduler_status": scheduler.get("status"),
                "scheduler_issue_count": scheduler.get("issue_count"),
                "monthly_scheduler_job_present": bool(monthly_scheduler_job),
                "monthly_scheduler_script": monthly_scheduler_job.get("script"),
                "monthly_scheduler_report": monthly_scheduler_job.get("report"),
                "monthly_scheduler_report_status": monthly_scheduler_job.get("report_status"),
                "monthly_scheduler_report_age_hours": monthly_scheduler_job.get("report_age_hours"),
                "monthly_scheduler_max_report_age_hours": monthly_scheduler_job.get("max_report_age_hours"),
                "monthly_scheduler_owner_reference_count": monthly_scheduler_job.get("owner_reference_count"),
                "monthly_scheduler_line_guard_ok": monthly_scheduler_job.get("scheduler_line_guard_ok"),
                "monthly_scheduler_line_guard_matches": monthly_scheduler_job.get("scheduler_line_guard_matches"),
                "monthly_scheduler_required_line_fragment_sets": monthly_scheduler_job.get("required_scheduler_line_fragment_sets"),
                "monthly_scheduler_issues": monthly_scheduler_issues,
            },
            [
                blocker
                for blocker in [
                    None if monthly_scheduler_job else "monthly_scheduler_job_missing",
                    None if scheduler.get("status") not in {"missing", "unreadable"} else f"scheduler_audit={scheduler.get('status')}",
                    None if not monthly_scheduler_job or monthly_scheduler_line_guard_ok else "monthly_scheduler_line_guard_not_ok",
                    None if not monthly_scheduler_issues else f"monthly_scheduler_issues={','.join(monthly_scheduler_issues)}",
                ]
                if blocker
            ],
        ),
        requirement(
            "local_model_validation",
            "Local-model preflight is informational only",
            True,
            {
                "observability_status": "ok" if local_model_ok else "review",
                "observability_blockers": local_model_blockers,
                "status": local_model.get("status"),
                "model": local_model.get("model"),
                "provider": local_model.get("provider"),
                "model_id": local_model.get("model_id"),
                "issue_count": local_model.get("issue_count"),
                "base_url": local_model.get("base_url"),
                "configured_model_present": local_model.get("configured_model_present"),
                "selected_endpoint_from_config": local_model.get("selected_endpoint_from_config"),
                "model_available": local_model.get("model_available"),
                "selected_endpoint_source": local_model.get("selected_endpoint_source"),
                "model_lock_base_url": local_model.get("model_lock_base_url"),
                "model_lock_endpoint_reachable": local_model.get("model_lock_endpoint_reachable"),
                "model_lock_model_available": local_model.get("model_lock_model_available"),
                "local_model_operational": local_model.get("local_model_operational"),
                "operational_model_id": local_model.get("operational_model_id"),
                "fallback_smoke_ok": local_model.get("fallback_smoke_ok"),
                "small_model_execution_allowed": local_model.get("small_model_execution_allowed"),
                "small_model_pipeline_execution_allowed": local_model.get("small_model_pipeline_execution_allowed"),
                "small_model_task_scoped_execution_allowed": local_model.get("small_model_task_scoped_execution_allowed"),
                "small_model_financial_authority": local_model.get("small_model_financial_authority"),
                "small_model_live_side_effects_allowed": local_model.get("small_model_live_side_effects_allowed"),
                "model_execution_scope": local_model_scope,
                "small_model_execution_policy": local_model_policy,
                "blocker_detail": local_model.get("blocker") if isinstance(local_model.get("blocker"), dict) else None,
                "issues": local_model.get("issues") if isinstance(local_model.get("issues"), list) else [],
                "generated_at": local_model.get("generated_at"),
                "report_age_hours": iso_age_hours(local_model.get("generated_at")),
                "max_age_hours": LOCAL_MODEL_PREFLIGHT_MAX_AGE_HOURS,
                "direct_smoke_attempted": local_model_direct_smoke.get("attempted"),
                "direct_smoke_ok": local_model_direct_smoke.get("ok"),
                "direct_smoke_response": local_model_direct_smoke.get("response"),
                "finance_contract_smoke_attempted": local_model_finance_smoke.get("attempted"),
                "finance_contract_smoke_ok": local_model_finance_smoke.get("ok"),
                "finance_contract_smoke_response": local_model_finance_smoke.get("response"),
                "finance_contract_expected_response": local_model.get("finance_contract_expected_response"),
                "validation_digest": local_model.get("validation_digest"),
            },
            [],
        ),
        requirement(
            "eod_telegram_visibility",
            "EOD Telegram summary includes daily/weekly/monthly run visibility",
            effective_eod.get("status") == "ok"
            and eod_fresh
            and effective_eod_sent_to_telegram
            and has_daily_visibility
            and has_monthly_visibility
            and has_action_section
            and eod_message_concise
            and has_daily_run_summary
            and has_weekly_run_summary
            and has_monthly_run_summary,
            {
                "status": effective_eod.get("status"),
                "source": effective_eod_source,
                "latest_report_source": latest_eod_report_source,
                "latest_report_generated_at": latest_eod_report.get("generated_at"),
                "latest_report_checked_at": latest_eod_report.get("checked_at"),
                "latest_report_dry_run": latest_eod_report.get("dry_run"),
                "latest_report_eod_message_status": latest_eod_report.get("eod_message_status"),
                "latest_report_eod_workflow_status": latest_eod_report.get("eod_workflow_status"),
                "latest_report_eod_action_required": latest_eod_report.get("eod_action_required"),
                "latest_report_telegram_delivery_proven": latest_eod_report.get("telegram_delivery_proven"),
                "latest_report_telegram_delivery_status": latest_eod_report.get("telegram_delivery_status"),
                "canonical_report_generated_at": eod.get("generated_at"),
                "canonical_report_dry_run": eod.get("dry_run"),
                "canonical_report_telegram_send_ok": eod.get("telegram_send_ok"),
                "canonical_report_sent_to_telegram": eod_sent_to_telegram,
                "canonical_report_sent_to_telegram_current": eod_sent_to_telegram_current,
                "canonical_report_age_hours": iso_age_hours(eod_sent_report_timestamp),
                "dry_run": effective_eod.get("dry_run"),
                "eod_message_status": effective_eod.get("eod_message_status"),
                "eod_workflow_status": effective_eod.get("eod_workflow_status"),
                "eod_action_required": effective_eod.get("eod_action_required"),
                "telegram_delivery_proven": effective_eod.get("telegram_delivery_proven"),
                "telegram_delivery_status": effective_eod.get("telegram_delivery_status"),
                "telegram_send_ok": effective_eod.get("telegram_send_ok"),
                "telegram_http_statuses": effective_eod.get("telegram_http_statuses"),
                "generated_at": effective_eod.get("generated_at"),
                "checked_at": effective_eod.get("checked_at"),
                "last_successful_send_at": effective_eod.get("last_successful_send_at"),
                "report_age_hours": iso_age_hours(eod_timestamp),
                "max_age_hours": EOD_REPORT_MAX_AGE_HOURS,
                "message_chunk_count": effective_eod.get("message_chunk_count"),
                "message_character_count": effective_eod.get("message_character_count"),
                "message_line_count": eod_message_line_count,
                "message_noise_markers": eod_noise_markers,
                "message_concise": eod_message_concise,
                "send_state_base_ok": eod_send_state_base_ok,
                "send_state_send_requested": eod_send_state_send_requested,
                "send_state_source_report": eod_send_state.get("source_report"),
                "send_state_source_report_present": eod_send_state_source_report_present,
                "send_state_source_report_scope_ok": eod_send_state_scope_ok,
                "send_state_source_report_exists": eod_send_state_source_report_exists,
                "send_state_source_report_readable": eod_send_state_source_report_readable,
                "send_state_source_report_status_ok": eod_send_state_source_report_status_ok,
                "send_state_source_report_non_dry_run_sent": eod_send_state_source_report_non_dry_run_sent,
                "send_state_source_report_message_matches": eod_send_state_source_report_message_matches,
                "send_state_source_report_digest_matches": eod_send_state_source_report_digest_matches,
                "send_state_source_report_generated_at": eod_send_state_source_report_generated_at or None,
                "send_state_source_report_generated_at_matches": eod_send_state_source_report_generated_at_matches,
                "send_state_source_report_ok": eod_send_state_source_report_ok,
                "send_state_message_concise": eod_send_state_message_concise,
                "send_state_message_line_count": eod_send_state_line_count,
                "send_state_message_noise_markers": eod_send_state_noise_markers,
                "send_state_message_digest_ok": eod_send_state_digest_ok,
                "send_state_source_report_generated_at_present": eod_send_state_source_generated_at_present,
                "send_state_timestamp": eod_send_state_timestamp,
                "send_state_age_hours": iso_age_hours(eod_send_state_timestamp),
                "send_state_fresh": eod_send_state_fresh,
                "send_state_current_ok": eod_send_state_current_ok,
                "send_state_rejected_missing_send_request": not eod_send_state_send_requested,
                "send_state_rejected_message_quality": eod_send_state_base_ok and not eod_send_state_message_concise,
                "send_state_rejected_missing_source_report": eod_send_state_base_ok and not eod_send_state_source_report_present,
                "send_state_rejected_foreign_source_report": eod_send_state_base_ok and not eod_send_state_scope_ok,
                "send_state_rejected_message_digest": eod_send_state_base_ok and not eod_send_state_digest_ok,
                "send_state_rejected_missing_source_report_generated_at": eod_send_state_base_ok and not eod_send_state_source_generated_at_present,
                "send_state_rejected_source_report_not_send_proof": eod_send_state_base_ok and not eod_send_state_source_report_non_dry_run_sent,
                "send_state_rejected_source_report_status_not_ok": eod_send_state_base_ok and not eod_send_state_source_report_status_ok,
                "send_state_rejected_source_report_message_mismatch": eod_send_state_base_ok and not eod_send_state_source_report_message_matches,
                "send_state_rejected_source_report_digest_mismatch": eod_send_state_base_ok and not eod_send_state_source_report_digest_matches,
                "send_state_rejected_source_report_generated_at_mismatch": eod_send_state_base_ok and not eod_send_state_source_report_generated_at_matches,
                "has_daily_visibility": has_daily_visibility,
                "has_monthly_visibility": has_monthly_visibility,
                "has_action_section": has_action_section,
                "has_daily_run_summary": has_daily_run_summary,
                "has_weekly_run_summary": has_weekly_run_summary,
                "has_monthly_run_summary": has_monthly_run_summary,
                "daily_run_summary": eod_daily_run_summary,
                "weekly_run_summary": eod_weekly_run_summary,
                "monthly_run_summary": eod_monthly_run_summary,
            },
            [
                blocker
                for blocker in [
                    None if effective_eod.get("status") == "ok" else f"eod={effective_eod.get('status')}",
                    None if eod_fresh else f"eod_report_stale_or_missing_generated_at={iso_age_hours(eod_timestamp)}",
                    None if effective_eod_sent_to_telegram else f"eod_not_sent_to_telegram:dry_run={latest_eod_report.get('dry_run')},send_ok={latest_eod_report.get('telegram_send_ok')}",
                    None
                    if (not effective_eod_sent_to_telegram or not eod_send_state_ok or eod_send_state_fresh)
                    else f"eod_send_state_stale_hours={iso_age_hours(eod_send_state_timestamp)}",
                    None if (not eod_send_state_base_ok or eod_send_state_digest_ok) else "eod_send_state_digest_missing_or_mismatch",
                    None if (not eod_send_state_base_ok or eod_send_state_source_generated_at_present) else "eod_send_state_missing_source_report_generated_at",
                    None if (not eod_send_state_base_ok or eod_send_state_source_report_non_dry_run_sent) else "eod_send_state_source_report_not_send_proof",
                    None if (not eod_send_state_base_ok or eod_send_state_source_report_status_ok) else "eod_send_state_source_report_status_not_ok",
                    None if (not eod_send_state_base_ok or eod_send_state_source_report_message_matches) else "eod_send_state_source_report_message_mismatch",
                    None if (not eod_send_state_base_ok or eod_send_state_source_report_digest_matches) else "eod_send_state_source_report_digest_mismatch",
                    None if (not eod_send_state_base_ok or eod_send_state_source_report_generated_at_matches) else "eod_send_state_source_report_generated_at_mismatch",
                    None if has_daily_visibility else "missing daily visibility",
                    None if has_monthly_visibility else "missing monthly visibility",
                    None if has_action_section else "missing action section",
                    None if has_daily_run_summary else "missing daily run summary",
                    None if has_weekly_run_summary else "missing weekly run summary",
                    None if has_monthly_run_summary else "missing monthly run summary",
                    None if eod_message_concise else f"eod_message_not_concise:lines={eod_message_line_count},noise={','.join(eod_noise_markers) or 'none'}",
                ]
                if blocker
            ],
        ),
        requirement(
            "weekly_idempotent_file_updates",
            "Weekly file updates are idempotent and financially review-clean",
            weekly_file_updates_effective_ok
            and weekly_cf_effective_ok
            and weekly_cf_gate.get("status") == "ok"
            and (
                weekly.get("deterministic_verification_idempotent") is True
                or weekly_review_retry_safe
                or weekly_scheduled_skip_ok
            )
            and weekly_cf_gate_snapshot_current
            and source_cash_balance_violation_count == 0
            and cf_balance_sheet_run_month_matches
            and cf_balance_sheet_consistency_clean,
            {
                "weekly_status": weekly.get("status"),
                "weekly_reason": weekly.get("reason"),
                "weekly_return_code": weekly.get("return_code"),
                "weekly_active_reason_parts": active_weekly_reason_parts,
                "weekly_primary_blocker": weekly_primary_blocker,
                "weekly_disk_space_preflight_blocked": weekly_disk_space_preflight_blocked,
                "weekly_file_updates_effective_ok": weekly_file_updates_effective_ok,
                "weekly_scheduled_skip_ok": weekly_scheduled_skip_ok,
                "deterministic_verification_idempotent": weekly.get("deterministic_verification_idempotent"),
                "review_safe_idempotency": weekly_review_safe_idempotency_evidence,
                "weekly_retry_required": weekly_retry_required,
                "weekly_safe_to_skip_next_run": weekly_review_safe_idempotency.get("safe_to_skip_next_run"),
                "weekly_state_file_unmarked": weekly_state_file_unmarked,
                "weekly_review_retry_safe": weekly_review_retry_safe,
                "weekly_cf_status": weekly_cf.get("status"),
                "weekly_cf_reason": weekly_cf.get("reason"),
                "weekly_cf_report": str(reports / "baselane_weekly_cf_statement_sync_report.json"),
                "weekly_cf_return_code": weekly_cf.get("return_code"),
                "weekly_cf_effective_status": weekly_cf.get("effective_status"),
                "weekly_cf_effective_reason": weekly_cf.get("effective_reason"),
                "weekly_cf_effective_blockers": weekly_cf.get("effective_blockers"),
                "weekly_cf_base_effective_ok": weekly_cf_base_effective_ok,
                "weekly_cf_effective_ok": weekly_cf_effective_ok,
                "weekly_cf_hard_blocker_counts": weekly_cf_hard_blocker_counts,
                "weekly_cf_no_gl_property_match_status": cf_no_gl_property_match.get("status"),
                "weekly_cf_no_gl_property_match_count": cf_no_gl_property_match.get("no_gl_property_match_count"),
                "weekly_cf_no_gl_property_match_active_monthly_scope_count": cf_no_gl_property_match.get("active_monthly_scope_count"),
                "weekly_cf_no_gl_property_match_report": str(cf_no_gl_property_match_path),
                "conflict_count": weekly_cf.get("conflict_count"),
                "audit_error_count": weekly_cf.get("audit_error_count"),
                "audit_error_class_counts": weekly_cf.get("audit_error_class_counts"),
                "untagged_review_required_count": weekly_cf.get("untagged_review_required_count"),
                "source_cash_balance_violation_count": source_cash_balance_violation_count,
                "source_cash_balance_violation_properties": source_cash_balance_violation_properties,
                "cf_balance_sheet_consistency_status": cf_balance_sheet_consistency.get("status"),
                "cf_balance_sheet_consistency_run_month": cf_balance_sheet_run_month or None,
                "cf_balance_sheet_consistency_expected_run_month": run_month or None,
                "cf_balance_sheet_consistency_run_month_matches": cf_balance_sheet_run_month_matches,
                "cf_balance_sheet_consistency_clean": cf_balance_sheet_consistency_clean,
                "cf_balance_sheet_consistency_issue_count": cf_balance_sheet_consistency_issue_count,
                "cf_balance_sheet_consistency_yhome_update_required_count": cf_balance_sheet_yhome_update_required_count,
                "cf_balance_sheet_consistency_target_columns": cf_balance_sheet_target_columns,
                "yhome_work_product_authoritative": False,
                "yhome_work_product_blocks_downstream": False,
                "yhome_work_product_needs_attention": yhome_operating_cash_work_product_needs_attention,
                "yhome_operating_cash_apply_verify_status": yhome_operating_cash_apply_verify_status,
                "yhome_operating_cash_apply_verify_reason": yhome_operating_cash_apply_verify_reason,
                "yhome_operating_cash_apply_verify_pre_update_required_count": yhome_operating_cash_apply_verify.get("pre_yhome_update_required_count"),
                "yhome_operating_cash_apply_verify_post_update_required_count": yhome_operating_cash_apply_verify.get("post_yhome_update_required_count"),
                "yhome_operating_cash_apply_verify_applied_update_count": yhome_operating_cash_apply_verify.get("applied_update_count"),
                "yhome_operating_cash_apply_verify_external_write_attempted": yhome_operating_cash_apply_verify.get("external_write_attempted"),
                "yhome_operating_cash_apply_verify_report": str(reports / "yhome_operating_cash_apply_verify_report.json"),
                "weekly_cf_gate_status": weekly_cf_gate.get("status"),
                "weekly_cf_gate_blocker_count": weekly_cf_gate.get("blocker_count"),
                "weekly_cf_gate_idempotency_key": weekly_cf_gate.get("idempotency_key"),
                "weekly_cf_gate_action_queue_digest": weekly_cf_gate.get("action_queue_digest"),
                "weekly_cf_gate_action_queue_count": weekly_cf_gate_action_queue_count,
                "weekly_report_cf_gate_idempotency_key": weekly_cf_gate_report_idempotency_key,
                "weekly_report_cf_gate_action_queue_digest": weekly_cf_gate_report_digest,
                "weekly_report_cf_gate_action_queue_count": weekly_cf_gate_report_action_queue_count,
                "weekly_cf_gate_snapshot_current": weekly_cf_gate_snapshot_current,
                "weekly_cf_gate": str(reports / "baselane_weekly_cf_review_gate.md"),
                "ecogl_source_fix_evidence": str(reports / "baselane_ecogl_source_fix_evidence.md"),
                "ecogl_source_fix_verifier": str(reports / "baselane_ecogl_source_fix_verifier.md"),
                "ecogl_source_fix_approval": str(reports / "baselane_ecogl_source_fix_approval.json"),
                "ecogl_source_fix_approved_corrections": str(reports / "baselane_ecogl_source_fix_approved_corrections.csv"),
                "ecogl_source_fix_corrections": str(reports / "baselane_ecogl_source_fix_corrections.csv"),
                "ecogl_source_fix_correction_validation": str(reports / "baselane_ecogl_source_fix_correction_validation.md"),
                "ecogl_source_fix_apply_plan": str(reports / "baselane_ecogl_source_fix_apply_plan.md"),
                "ecogl_source_fix_apply_plan_csv": str(reports / "baselane_ecogl_source_fix_apply_plan.csv"),
                "ecogl_source_fix_evidence_status_counts": source_fix_evidence_status_counts,
                "ecogl_source_fix_evidence_automation_safe_count": source_fix_automation_safe_count,
                "ecogl_source_fix_verified_fixed_count": source_fix_verified_fixed_count,
                "ecogl_source_fix_remaining_count": source_fix_remaining_count,
                "ecogl_source_fix_verifier_status_counts": source_fix_verifier_status_counts,
                "ecogl_source_fix_corrections_status": ecogl_source_fix_corrections.get("status"),
                "ecogl_source_fix_approval_status": ecogl_source_fix_approval.get("status"),
                "ecogl_source_fix_approval_approved_count": source_fix_approval_approved_count,
                "ecogl_source_fix_approval_pending_count": source_fix_approval_pending_count,
                "ecogl_source_fix_approval_invalid_count": source_fix_approval_invalid_count,
                "ecogl_source_fix_correction_validation_status": ecogl_source_fix_correction_validation.get("status"),
                "ecogl_source_fix_correction_validation_ready_count": source_fix_validation_ready_count,
                "ecogl_source_fix_correction_validation_pending_count": source_fix_validation_pending_count,
                "ecogl_source_fix_correction_validation_invalid_count": source_fix_validation_invalid_count,
                "ecogl_source_fix_apply_plan_status": ecogl_source_fix_apply_plan.get("status"),
                "ecogl_source_fix_apply_plan_ready_current_source_index_count": source_fix_apply_ready_count,
                "ecogl_source_fix_apply_plan_needs_current_source_index_refresh_count": source_fix_apply_refresh_count,
                "ecogl_source_fix_apply_plan_blocked_count": source_fix_apply_blocked_count,
            },
            [
                blocker
                for blocker in [
                    None if weekly_file_updates_effective_ok else f"weekly_file_updates={weekly.get('status')}:{';'.join(active_weekly_reason_parts) or weekly.get('reason')}",
                    None
                    if weekly_cf_base_effective_ok
                    else f"weekly_cf_sync={weekly_cf.get('effective_status') or weekly_cf.get('status')}:{weekly_cf.get('effective_reason') or weekly_cf.get('reason')}",
                    None
                    if weekly_cf_hard_blocker_counts["no_gl_property_match_count"] == 0
                    else f"cf_no_gl_property_match_count={weekly_cf_hard_blocker_counts['no_gl_property_match_count']}",
                    None if source_cash_balance_violation_count == 0 else f"source_cash_balance_violation_count={source_cash_balance_violation_count}",
                    None
                    if cf_balance_sheet_run_month_matches
                    else f"cf_balance_sheet_audit_run_month_mismatch=expected:{run_month or 'missing'},actual:{cf_balance_sheet_run_month or 'missing'}",
                    None
                    if cf_balance_sheet_consistency_clean
                    else f"cf_balance_sheet_consistency={cf_balance_sheet_consistency.get('status')}:{cf_balance_sheet_consistency_issue_count}",
                    None if weekly_cf_gate.get("status") == "ok" else f"weekly_cf_gate={weekly_cf_gate.get('status')}:{weekly_cf_gate.get('blocker_count')}",
                    None
                    if weekly_disk_space_preflight_blocked
                    or weekly.get("deterministic_verification_idempotent") is True
                    or weekly_review_retry_safe
                    or weekly_scheduled_skip_ok
                    else ",".join(weekly_idempotency_blockers),
                    None if weekly_disk_space_preflight_blocked or weekly_cf_gate_snapshot_current else "weekly_cf_gate_snapshot_stale",
                ]
                if blocker
            ],
        ),
        requirement(
            "monthly_bank_statement_capture",
            "Monthly Baselane bank statements are captured idempotently",
            monthly_statements_ok,
            {
                "status": monthly_statements_gate.get("status"),
                "reason": monthly_statements_gate.get("reason"),
                "action": monthly_statements_gate.get("action"),
                "monthly_script_return_code": monthly_statements_gate.get("monthly_script_return_code"),
                "download_ok": monthly_statements_gate.get("download_ok"),
                "download_report_not_used": monthly_statements_gate.get("download_report_not_used"),
                "verified_existing_capture": monthly_statements_verified_existing_capture,
                "download_verified": monthly_statements_download_verified,
                "captured_unique_count": monthly_statements_captured,
                "min_captured_required": monthly_statements_min_captured,
                "target_year": monthly_statements_gate.get("target_year"),
                "target_month": monthly_statements_gate.get("target_month"),
                "run_month": run_month,
                "target_matches_run_month": statement_target_matches(monthly_statements_gate, run_month),
                "stamp": monthly_statements_gate.get("stamp"),
                "generated_at": monthly_statements_gate.get("generated_at"),
                "report_age_hours": iso_age_hours(monthly_statements_gate.get("generated_at")),
                "max_age_hours": MONTHLY_STATEMENTS_MAX_AGE_HOURS,
                "download_error_class": monthly_statements_download_error_class,
                "auth_recovery_attempted": monthly_statements_gate.get("auth_recovery_attempted"),
                "auth_recovery_status": monthly_statements_gate.get("auth_recovery_status"),
                "auth_recovery_manual_auth_required": monthly_statements_gate.get("auth_recovery_manual_auth_required"),
                "auth_recovery_attempt_count": monthly_statements_gate.get("auth_recovery_attempt_count"),
                "report": str(reports / "baselane_monthly_statements_idempotent_report.json"),
            },
            [
                blocker
                for blocker in [
                    None if monthly_statements_gate.get("status") == "ok" else f"monthly_statements={monthly_statements_gate.get('status')}:{monthly_statements_gate.get('reason')}",
                    None if int(monthly_statements_gate.get("monthly_script_return_code") or 0) == 0 else f"monthly_statements_return_code={monthly_statements_gate.get('monthly_script_return_code')}",
                    None if monthly_statements_download_verified else f"monthly_statements_download_ok={monthly_statements_gate.get('download_ok')}",
                    None if monthly_statements_captured >= monthly_statements_min_captured and monthly_statements_min_captured > 0 else f"monthly_statements_captured={monthly_statements_captured}/{monthly_statements_min_captured}",
                    None if statement_target_matches(monthly_statements_gate, run_month) else f"monthly_statements_target_mismatch=run_month:{run_month},target:{monthly_statements_gate.get('target_year')}-{monthly_statements_gate.get('target_month')}",
                    None if required_fresh_generated_at(monthly_statements_gate, MONTHLY_STATEMENTS_MAX_AGE_HOURS) else f"monthly_statements_stale_or_missing_generated_at={iso_age_hours(monthly_statements_gate.get('generated_at'))}",
                    None if not monthly_statements_download_error_class else f"monthly_statements_download_error={monthly_statements_download_error_class}",
                ]
                if blocker
            ],
        ),
        requirement(
            "monthly_transfer_reconciliation_telegram",
            "Monthly transfer reconciliation report is delivered to Telegram DM",
            transfer_telegram_delivery_ok,
            {
                "transfer_report_status": transfer_reconciliation.get("status"),
                "transfer_report_generated_at": transfer_reconciliation.get("generated_at"),
                "transfer_report_source_blockers": transfer_reconciliation.get("source_blockers"),
                "transfer_report_recommended_send_to_lofty_total": transfer_reconciliation.get("recommended_send_to_lofty_total"),
                "transfer_report_recommended_send_to_lofty_total_is_final": transfer_reconciliation.get("recommended_send_to_lofty_total_is_final"),
                "transfer_report_bank_transfer_actions_final": transfer_reconciliation.get("bank_transfer_actions_final"),
                "transfer_report_telegram_summary": transfer_reconciliation.get("telegram_summary"),
                "current_transfer_report_digest": current_transfer_report_digest,
                "send_report_status": transfer_telegram_send.get("status"),
                "send_report_generated_at": transfer_telegram_send.get("generated_at"),
                "send_report_age_hours": iso_age_hours(transfer_telegram_send.get("generated_at")),
                "send_report_fresh": transfer_telegram_send_fresh,
                "send_requested": transfer_telegram_send.get("send_requested"),
                "dry_run": transfer_telegram_send.get("dry_run"),
                "telegram_send_ok": transfer_telegram_send.get("telegram_send_ok"),
                "telegram_http_statuses": transfer_telegram_send.get("telegram_http_statuses"),
                "message_quality_ok": transfer_telegram_send.get("message_quality_ok"),
                "message_quality_issues": transfer_telegram_send.get("message_quality_issues"),
                "send_safe": transfer_telegram_send.get("send_safe"),
                "send_blockers": transfer_telegram_send.get("send_blockers"),
                "transfer_report_digest": transfer_telegram_send.get("transfer_report_digest"),
                "transfer_report_digest_matches_expected": transfer_telegram_send.get("transfer_report_digest_matches_expected"),
                "transfer_report_digest_matches_current": transfer_telegram_send.get("transfer_report_digest_matches_current"),
                "message_matches_transfer_report_telegram_summary": transfer_telegram_send.get("message_matches_transfer_report_telegram_summary"),
                "transfer_report_current_for_run": transfer_telegram_send.get("transfer_report_current_for_run"),
                "sent_state_status": transfer_telegram_sent_state.get("status"),
                "sent_state_sent_at": transfer_telegram_sent_state.get("sent_at"),
                "sent_state_age_hours": iso_age_hours(transfer_telegram_sent_state.get("sent_at")),
                "sent_state_fresh": transfer_telegram_send_state_fresh,
                "sent_state_telegram_send_ok": transfer_telegram_sent_state.get("telegram_send_ok"),
                "sent_state_transfer_report_digest": transfer_telegram_sent_state.get("transfer_report_digest"),
                "digest_current": transfer_telegram_digest_current,
                "message_digest_bound": transfer_telegram_message_digest_bound,
                "sent_state_message_text_digest_ok": transfer_telegram_state_message_text_digest_ok,
            },
            [
                blocker
                for blocker in [
                    None if transfer_reconciliation.get("status") in {"ok", "review", "blocked_source_not_clean"} else f"transfer_report={transfer_reconciliation.get('status')}",
                    None if transfer_telegram_send_status_ok else f"transfer_telegram_send={transfer_telegram_send.get('status')}",
                    None if transfer_telegram_send.get("send_requested") is True else "transfer_telegram_send_not_requested",
                    None if transfer_telegram_send.get("dry_run") is False else "transfer_telegram_delivery_is_dry_run",
                    None if transfer_telegram_send.get("telegram_send_ok") is True else f"transfer_telegram_send_ok={transfer_telegram_send.get('telegram_send_ok')}",
                    None if bool(transfer_telegram_send.get("telegram_http_statuses") or []) else "transfer_telegram_http_statuses_missing",
                    None if transfer_telegram_send.get("message_quality_ok") is True else f"transfer_telegram_message_quality={transfer_telegram_send.get('message_quality_issues')}",
                    None if transfer_telegram_send.get("send_safe") is True else f"transfer_telegram_send_blockers={transfer_telegram_send.get('send_blockers')}",
                    None if transfer_telegram_send.get("transfer_report_digest_matches_expected") is True else "transfer_telegram_expected_digest_missing_or_mismatch",
                    None if transfer_telegram_send.get("transfer_report_digest_matches_current") is True else "transfer_telegram_current_digest_missing",
                    None if transfer_telegram_send.get("message_matches_transfer_report_telegram_summary") is True else "transfer_telegram_message_not_bound_to_summary",
                    None if transfer_telegram_state_status_ok else f"transfer_telegram_sent_state={transfer_telegram_sent_state.get('status')}",
                    None if transfer_telegram_sent_state.get("telegram_send_ok") is True else f"transfer_telegram_sent_state_ok={transfer_telegram_sent_state.get('telegram_send_ok')}",
                    None if transfer_telegram_digest_current else "transfer_telegram_digest_not_current",
                    None if transfer_telegram_message_digest_bound else "transfer_telegram_message_digest_not_bound",
                    None if transfer_telegram_send_fresh else f"transfer_telegram_send_stale_hours={iso_age_hours(transfer_telegram_send.get('generated_at'))}",
                    None if transfer_telegram_send_state_fresh else f"transfer_telegram_sent_state_stale_hours={iso_age_hours(transfer_telegram_sent_state.get('sent_at'))}",
                    None if transfer_telegram_current_for_run_ok else "transfer_report_not_current_for_run",
                ]
                if blocker
            ],
        ),
        requirement(
            "monthly_canonical_docs",
            "Monthly canonical public docs exist in current folders",
            doc_bootstrap.get("status") == "ok"
            and path_guard.get("status") == "ok"
            and int(path_guard.get("issue_count") or 0) == 0
            and discord_public_guard.get("status") == "ok"
            and int(discord_public_guard.get("issue_count") or 0) == 0,
            {
                "doc_bootstrap_status": doc_bootstrap.get("status"),
                "doc_counts": doc_bootstrap.get("counts"),
                "public_path_guard_status": path_guard.get("status"),
                "public_path_guard_issue_count": path_guard.get("issue_count"),
                "discord_public_financial_source_guard_status": discord_public_guard.get("status"),
                "discord_public_financial_source_guard_issue_count": discord_public_guard.get("issue_count"),
                "discord_public_financial_source_guard_deleted_gl_rows_count": discord_public_guard.get("deleted_gl_rows_count"),
                "discord_public_financial_source_guard_financial_doc_count": discord_public_guard.get("financial_doc_count"),
                "discord_public_financial_source_guard_update_doc_count": discord_public_guard.get("update_doc_count"),
                "canonical_updates_folder": path_guard.get("canonical_updates_folder"),
                "canonical_financials_folder": path_guard.get("canonical_financials_folder"),
            },
            [
                blocker
                for blocker in [
                    None if doc_bootstrap.get("status") == "ok" else "monthly_doc_bootstrap_not_ok",
                    None if path_guard.get("status") == "ok" and int(path_guard.get("issue_count") or 0) == 0 else "canonical public path guard is not clean",
                    (
                        None
                        if discord_public_guard.get("status") == "ok" and int(discord_public_guard.get("issue_count") or 0) == 0
                        else "discord public financial source guard is not clean"
                    ),
                ]
                if blocker
            ],
        ),
        requirement(
            "monthly_comms_rent_roll_context",
            "Monthly comms include rent-roll context or explicit gap review",
            comms_stats["index_exists"]
            and comms_stats["rent_roll_csv_exists"]
            and comms_stats["checklist_exists"]
            and int(comms_stats["rent_roll_matched_count"] or 0) > 0
            and (
                (
                    int(comms_stats["rent_roll_blocking_gap_count"] or 0) == 0
                    and not comms_stats["rent_roll_stale_export_dates"]
                )
                or (
                    comms_stats["gap_review_status"] == "ok"
                    and comms_stats.get("gap_review_approval_template_coverage_status") in {None, "ok"}
                )
            ),
            comms_stats,
            [
                blocker
                for blocker in [
                    None if comms_stats["index_exists"] else "monthly portfolio index missing",
                    None if comms_stats["rent_roll_csv_exists"] else "rent-roll summary CSV missing",
                    None if comms_stats["checklist_exists"] else "monthly checklist missing",
                    None if int(comms_stats["rent_roll_matched_count"] or 0) > 0 else "rent-roll summary has zero matched properties",
                    (
                        None
                        if int(comms_stats["rent_roll_blocking_gap_count"] or 0) == 0
                        and not comms_stats["rent_roll_stale_export_dates"]
                        or comms_stats["gap_review_exists"]
                        else "rent-roll gap review missing"
                    ),
                    (
                        None
                        if not int(comms_stats.get("gap_review_source_blocker_count") or 0)
                        else (
                            f"rent_roll_current_source_required={comms_stats.get('rent_roll_freshness_status')},"
                            f"deferred_gaps={comms_stats.get('gap_review_deferred_gap_count')}"
                        )
                    ),
                    (
                        None
                        if int(comms_stats.get("rent_roll_blocking_gap_count") or 0) == 0 or comms_stats["gap_review_status"] == "ok"
                        else (
                            "rent_roll_active_target_gap_count="
                            f"{comms_stats.get('rent_roll_target_pending_gap_count')},"
                            f"non_target_gap_count={comms_stats.get('rent_roll_non_target_pending_gap_count')}"
                            if comms_stats.get("rent_roll_target_scoped")
                            else f"rent_roll_gap_count={comms_stats['rent_roll_gap_count']},pending_approval={comms_stats.get('pending_gap_approval_count')}"
                        )
                    ),
                    (
                        None
                        if not comms_stats["rent_roll_stale_export_dates"] or comms_stats["gap_review_status"] == "ok"
                        else f"rent_roll_stale_export_dates={','.join(comms_stats['rent_roll_stale_export_dates'])},pending_approval={comms_stats.get('pending_stale_export_date_approval_count')}"
                    ),
                    (
                        None
                        if comms_stats.get("gap_review_approval_template_coverage_status") in {None, "ok"}
                        else f"rent_roll_gap_approval_template_coverage={comms_stats.get('gap_review_approval_template_coverage_status')}"
                    ),
                ]
                if blocker
            ],
        ),
        requirement(
            "monthly_review_and_guarded_apply",
            "Monthly UPDATES.md and FINANCIALS.md changes are reviewed and guard-applied",
            review_manifest_effectively_clean and owner_review_gate.get("status") == "ok" and guarded_apply.get("status") == "ok" and guarded_apply.get("apply") is True,
            {
                "review_manifest_status": review_manifest.get("status"),
                "review_manifest_effectively_clean": review_manifest_effectively_clean,
                "pending_update_review_count": owner_pending_update_count,
                "pending_financial_review_count": owner_pending_financial_count,
                "owner_review_gate_approved_update_count": owner_gate_summary.get("approved_update_count"),
                "owner_review_gate_approved_financial_count": owner_gate_summary.get("approved_financial_count"),
                "owner_review_gate_status": owner_review_gate.get("status"),
                "owner_review_gate_blocker_count": owner_review_gate.get("blocker_count"),
                "owner_review_gate_primary_blocker": owner_review_gate_primary_blocker or None,
                "owner_review_gate_idempotency_key": owner_review_gate.get("idempotency_key"),
                "owner_review_gate_property_checklist_digest": owner_review_gate.get("property_checklist_digest"),
                "owner_review_gate_property_checklist_count": owner_review_gate.get("property_checklist_count"),
                "owner_review_gate_property_skipped_count": owner_skipped_count,
                "owner_review_gate_property_external_excluded_count": owner_external_excluded_count,
                "owner_review_gate_property_excluded_total_count": owner_excluded_total_count,
                "owner_review_gate_guard_workflow_coverage_status": owner_guard_workflow.get("status"),
                "owner_review_gate_guard_workflow_digest": owner_guard_workflow.get("digest"),
                "owner_review_gate": str(reports / "baselane_monthly_owner_review_gate.md"),
                "guarded_apply_status": guarded_apply.get("status"),
                "guarded_apply_apply": guarded_apply.get("apply"),
                "guarded_apply_counts": guarded_apply_counts,
                "monthly_guarded_apply_held_by_daily_disk_preflight": monthly_guarded_apply_held_by_daily_disk_preflight,
                "guarded_apply_waiting_for_live_guards": guarded_apply_waiting_for_live_guards,
                "guarded_apply_waiting_for_current_rent_roll": guarded_apply_waiting_for_current_rent_roll,
                "owner_review_gate_waiting_for_live_guards": owner_review_gate_waiting_for_live_guards,
                "owner_review_gate_waiting_for_current_rent_roll": owner_review_gate_waiting_for_current_rent_roll,
                "rent_roll_blocks_live_apply": rent_roll_blocks_live_apply,
                "monthly_local_dry_run_ready": monthly_local_dry_run_ready,
                "live_guard_summary": live_guard_summary,
                "listing_cleanup_queue": listing_cleanup_summary,
            },
            monthly_guarded_apply_blockers,
        ),
        requirement(
            "lofty_pm_live_guard_workflow",
            "Lofty PM live captures and guarded workflow are ready",
            live_guard_workflow_ready
            and skipped_exclusion_counts_match
            and publish_excluded_no_payload_or_email,
            {
                "cdp_preflight_status": cdp_preflight.get("status"),
                "login_tab_count": cdp_preflight.get("login_tab_count"),
                "pm_tab_count": cdp_preflight.get("pm_tab_count"),
                "login_recovery_attempt_count": cdp_preflight.get("login_recovery_attempt_count")
                if "login_recovery_attempt_count" in cdp_preflight
                else len(cdp_preflight.get("login_recovery_attempts") or []),
                "login_recovery_opened_property_owners": cdp_preflight.get("login_recovery_opened_property_owners"),
                "manual_auth_required": cdp_preflight.get("manual_auth_required"),
                "manual_auth_reason": cdp_preflight.get("manual_auth_reason"),
                "live_update_capture_status": live_update_capture.get("status"),
                "live_update_capture_generated_at": live_update_capture.get("generated_at"),
                "live_update_capture_report_age_hours": iso_age_hours(live_update_capture.get("generated_at")),
                "live_update_capture_max_age_hours": LIVE_CAPTURE_MAX_AGE_HOURS,
                "live_update_capture_mutates_lofty_listing": live_update_capture.get("mutates_lofty_listing"),
                "live_update_capture_mutates_external_system": live_update_capture.get("mutates_external_system"),
                "live_update_capture_external_mutation_count": live_update_capture.get("external_mutation_count"),
                "live_update_capture_semantics": live_update_capture.get("capture_semantics"),
                "live_update_capture_contract": live_update_capture.get("capture_contract"),
                "live_update_target_count": live_update_capture.get("target_count"),
                "live_update_register_count": live_update_register_count,
                "live_update_capture_registered": live_update_capture_registered,
                "live_update_skipped_index_count": live_update_skipped_count,
                "live_update_excluded_property_count": live_update_excluded_count,
                "live_update_externally_excluded_count": live_update_external_count,
                "live_update_skipped_index_status_counts": live_update_capture.get("skipped_index_status_counts"),
                "live_update_skipped_index_digest": live_update_capture.get("skipped_index_digest"),
                "live_update_skipped_index_records": live_update_capture.get("skipped_index_records") or [],
                "live_update_check_ok_count": live_update_capture.get("check_ok_count"),
                "live_update_mismatch_count": live_update_mismatch_count,
                "live_financial_capture_status": live_financial_capture.get("status"),
                "live_financial_capture_generated_at": live_financial_capture.get("generated_at"),
                "live_financial_capture_report_age_hours": iso_age_hours(live_financial_capture.get("generated_at")),
                "live_financial_capture_max_age_hours": LIVE_CAPTURE_MAX_AGE_HOURS,
                "live_financial_capture_mutates_lofty_listing": live_financial_capture.get("mutates_lofty_listing"),
                "live_financial_capture_mutates_external_system": live_financial_capture.get("mutates_external_system"),
                "live_financial_capture_external_mutation_count": live_financial_capture.get("external_mutation_count"),
                "live_financial_capture_semantics": live_financial_capture.get("capture_semantics"),
                "live_financial_capture_contract": live_financial_capture.get("capture_contract"),
                "live_financial_target_count": live_financial_capture.get("target_count"),
                "live_financial_register_count": live_financial_register_count,
                "live_financial_capture_registered": live_financial_capture_registered,
                "live_financial_skipped_index_count": live_financial_skipped_count,
                "live_financial_excluded_property_count": live_financial_excluded_count,
                "live_financial_externally_excluded_count": live_financial_external_count,
                "live_financial_skipped_index_status_counts": live_financial_capture.get("skipped_index_status_counts"),
                "live_financial_skipped_index_digest": live_financial_capture.get("skipped_index_digest"),
                "live_financial_skipped_index_records": live_financial_capture.get("skipped_index_records") or [],
                "live_financial_check_ok_count": live_financial_capture.get("check_ok_count"),
                "live_financial_mismatch_count": live_financial_mismatch_count,
                "lofty_financial_patch_readiness_status": lofty_financial_patch_readiness.get("status"),
                "lofty_financial_patch_readiness_issue_count": lofty_financial_patch_readiness.get("issue_count"),
                "lofty_financial_patch_property_count": lofty_financial_patch_property_count,
                "lofty_financial_patch_active_count": lofty_financial_patch_active_count,
                "lofty_financial_patch_skipped_count": lofty_financial_patch_skipped_count,
                "lofty_financial_patch_ready_count": lofty_financial_patch_ready_count,
                "lofty_financial_patch_guard_reconcile_required_count": lofty_financial_patch_guard_reconcile_required_count,
                "lofty_financial_patch_guard_reconcile_required_field_count": lofty_financial_patch_readiness.get("guard_reconcile_required_field_count"),
                "lofty_financial_patch_blocked_count": lofty_financial_patch_blocked_count,
                "lofty_financial_patch_blocked_empty_patch_count": lofty_financial_patch_readiness.get("blocked_empty_patch_count"),
                "lofty_financial_patch_field_count_total": lofty_financial_patch_field_count_total,
                "lofty_financial_patch_readiness_digest": lofty_financial_patch_readiness.get("financial_patch_readiness_digest"),
                "lofty_financial_patch_guard_reconcile_csv": lofty_financial_patch_readiness.get("guard_reconcile_csv"),
                "lofty_financial_patch_blocked_empty_patch_csv": lofty_financial_patch_readiness.get("blocked_empty_patch_csv"),
                "lofty_financial_patch_record_status_counts": lofty_financial_patch_readiness.get("record_status_counts"),
                "lofty_financial_patch_mutates_lofty_listing": lofty_financial_patch_readiness.get("mutates_lofty_listing"),
                "lofty_financial_patch_sends_owner_email": lofty_financial_patch_readiness.get("sends_owner_email"),
                "lofty_financial_patch_ready": lofty_financial_patch_ready,
                "listing_cleanup_queue": listing_cleanup_summary,
                "owner_review_gate_property_skipped_count": owner_skipped_count,
                "owner_review_gate_property_external_excluded_count": owner_external_excluded_count,
                "owner_review_gate_property_excluded_total_count": owner_excluded_total_count,
                "publish_excluded_property_count": publish_excluded_property_count,
                "publish_excluded_property_count_present": publish_excluded_property_count_present,
                "publish_excluded_payload_file_count": publish_excluded_payload_file_count,
                "publish_excluded_owner_email_candidate_count": publish_excluded_owner_email_candidate_count,
                "publish_excluded_no_payload_or_email": publish_excluded_no_payload_or_email,
                "skipped_index_count_mode": skipped_index_count_mode,
                "split_skipped_index_counts_match": split_skipped_index_counts_match,
                "collapsed_skipped_index_counts_match": collapsed_skipped_index_counts_match,
                "skipped_index_counts_match": skipped_index_counts_match,
                "total_exclusion_counts_match": total_exclusion_counts_match,
                "skipped_exclusion_counts_match": skipped_exclusion_counts_match,
                "live_guard_blocker_collapsed_by_cdp_auth": not lofty_cdp_ready,
            },
            live_guard_workflow_blockers,
        ),
        requirement(
            "owner_email_idempotent_no_spam",
            "Owner email send is max-once/month and not allowed until readiness is clean",
            readiness.get("owner_email_allowed") is True
            and publish.get("status") == "ok"
            and owner_email_send_guard_ok
            and publish_send_interval_ok
            and publish_idempotency_configured
            and publish_send_decision_digest_configured
            and publish_readiness_snapshot_current
            and publish_evidence_issue_count == 0
            and publish_send_safely_recorded
            and publish_lock_safe
            and publish_existing_lock_digest_safe
            and skipped_exclusion_counts_match
            and publish_excluded_no_payload_or_email
            and owner_email_packet_no_leak
            and owner_email_packet_live_guard_ok
            and owner_email_packet_preview_safe
            and (not owner_email_packet_required_now or owner_email_packet_ready),
            {
                "owner_email_allowed": readiness.get("owner_email_allowed"),
                "publish_status": publish.get("status"),
                "owner_email_send_guard_status": owner_email_send_guard.get("status"),
                "owner_email_send_guard_ok": owner_email_send_guard.get("guard_ok"),
                "owner_email_send_guard_send_allowed": owner_email_send_guard.get("send_allowed"),
                "owner_email_send_guard_safe_block": owner_email_send_guard.get("safe_block"),
                "owner_email_send_guard_max_once_monthly_ok": owner_email_send_guard.get("max_once_monthly_ok"),
                "owner_email_send_guard_no_spam_guard_ok": owner_email_send_guard.get("no_spam_guard_ok"),
                "owner_email_send_guard_issue_count": owner_email_send_guard.get("issue_count"),
                "owner_email_send_guard_issues": owner_email_send_guard.get("issues"),
                "owner_email_send_guard_idempotency_proof": owner_email_send_guard.get("idempotency_proof"),
                "owner_email_packet_status": owner_email_packet.get("status"),
                "owner_email_packet_issue_count": owner_email_packet.get("issue_count"),
                "owner_email_packet_recipient_count": owner_email_packet.get("recipient_count"),
                "owner_email_packet_packet_count": owner_email_packet.get("packet_count"),
                "owner_email_packet_full_history_leak_count": owner_email_packet.get("full_history_leak_count"),
                "owner_email_packet_requires_live_update_guard": owner_email_packet.get("requires_live_update_guard"),
                "owner_email_packet_live_update_capture_report": owner_email_packet.get("live_update_capture_report"),
                "owner_email_packet_no_leak": owner_email_packet_no_leak,
                "owner_email_packet_live_guard_ok": owner_email_packet_live_guard_ok,
                "owner_email_packet_preview_file_write_allowed": owner_email_packet_preview_file_write_allowed,
                "owner_email_packet_preview_write_blocked_reason": owner_email_packet_preview_write_blocked_reason,
                "owner_email_packet_unsafe_preview_packet_count": owner_email_packet_unsafe_preview_packet_count,
                "owner_email_packet_stale_preview_file_removed_count": owner_email_packet_stale_preview_file_removed_count,
                "owner_email_packet_stale_preview_cleanup_error_count": owner_email_packet_stale_preview_cleanup_error_count,
                "owner_email_packet_preview_count": owner_email_packet_preview_count,
                "owner_email_packet_preview_telemetry_present": owner_email_packet_preview_telemetry_present,
                "owner_email_packet_preview_allowed_without_packets": owner_email_packet_preview_allowed_without_packets,
                "owner_email_packet_previews_written_when_blocked": owner_email_packet_previews_written_when_blocked,
                "owner_email_packet_preview_block_reason_current": owner_email_packet_preview_block_reason_current,
                "owner_email_packet_preview_block_reason_required_now": owner_email_packet_required_now,
                "owner_email_packet_preview_safe": owner_email_packet_preview_safe,
                "owner_email_packet_ready": owner_email_packet_ready,
                "owner_email_packet_required_now": owner_email_packet_required_now,
                "native_owner_email_ready": native_owner_email_ready,
                "native_owner_email_property_count": owner_email_send_guard.get("owner_email_packet_native_property_count"),
                "native_owner_email_property_coverage_ok": owner_email_send_guard.get("owner_email_packet_native_property_coverage_ok"),
                "native_owner_email_signal_only_ok": owner_email_send_guard.get("owner_email_packet_signal_only_ok"),
                "owner_email_packet_property_unavailable_count": owner_email_packet.get("property_unavailable_count"),
                "owner_email_packet_property_unavailable_reason_counts": owner_email_packet.get("property_unavailable_reason_counts"),
                "owner_email_packet_property_unavailable_candidate_update_source_count": owner_email_packet.get(
                    "property_unavailable_candidate_update_source_count"
                ),
                "owner_email_packet_property_unavailable_candidate_update_approval_target_count": owner_email_packet.get(
                    "property_unavailable_candidate_update_approval_target_count"
                ),
                "owner_email_packet_property_unavailable_candidate_financial_approval_target_count": owner_email_packet.get(
                    "property_unavailable_candidate_financial_approval_target_count"
                ),
                "owner_email_packet_safe_to_send_now": owner_email_packet.get("safe_to_send_now"),
                "owner_email_guild_test_handoff": owner_email_guild_handoff,
                "owner_email_guild_test_prepared": owner_email_guild_handoff.get("prepared"),
                "owner_email_guild_test_posted": owner_email_guild_handoff.get("posted"),
                "owner_email_guild_test_valid": owner_email_guild_handoff.get("valid"),
                "owner_email_guild_test_target": owner_email_guild_handoff.get("target"),
                "owner_email_guild_test_selected_property_name": owner_email_guild_handoff.get("selected_property_name"),
                "owner_email_guild_test_message_file": owner_email_guild_handoff.get("message_file"),
                "owner_email_guild_test_next_action": owner_email_guild_test_next_action(owner_email_guild_handoff),
                "send_lock_status": publish.get("send_lock_status"),
                "sent_state_write_status": publish.get("sent_state_write_status"),
                "sent_state_file": publish.get("sent_state_file"),
                "sent_state_month": publish.get("sent_state_month"),
                "send_decision_digest": publish_send_decision_digest,
                "owner_email_idempotency_send_decision_digest": publish_idempotency_send_decision_digest,
                "owner_email_send_decision_digest": publish_send_decision_nested_digest,
                "send_decision_digest_configured": publish_send_decision_digest_configured,
                "existing_send_lock_decision_digest": publish_existing_send_lock_decision_digest,
                "existing_send_lock_matches_send_decision": publish_existing_send_lock_matches_send_decision,
                "existing_send_lock_digest_safe": publish_existing_lock_digest_safe,
                "owner_email_idempotency_configured": publish_idempotency_configured,
                "owner_email_idempotency": publish_idempotency,
                "owner_email_send_decision": publish_send_decision,
                "excluded_property_count": publish_excluded_property_count,
                "excluded_payload_file_count": publish_excluded_payload_file_count,
                "excluded_owner_email_candidate_count": publish_excluded_owner_email_candidate_count,
                "publish_excluded_no_payload_or_email": publish_excluded_no_payload_or_email,
                "excluded_property_names": publish.get("excluded_property_names"),
                "owner_review_gate_property_skipped_count": owner_skipped_count,
                "owner_review_gate_property_external_excluded_count": owner_external_excluded_count,
                "owner_review_gate_property_excluded_total_count": owner_excluded_total_count,
                "live_update_skipped_index_count": live_update_skipped_count,
                "live_financial_skipped_index_count": live_financial_skipped_count,
                "live_update_excluded_property_count": live_update_excluded_count,
                "live_financial_excluded_property_count": live_financial_excluded_count,
                "skipped_index_count_mode": skipped_index_count_mode,
                "split_skipped_index_counts_match": split_skipped_index_counts_match,
                "collapsed_skipped_index_counts_match": collapsed_skipped_index_counts_match,
                "skipped_index_counts_match": skipped_index_counts_match,
                "total_exclusion_counts_match": total_exclusion_counts_match,
                "skipped_exclusion_counts_match": skipped_exclusion_counts_match,
                "monthly_readiness_snapshot": publish_readiness_snapshot,
                "monthly_readiness_snapshot_configured": publish_readiness_snapshot_configured,
                "monthly_readiness_snapshot_current": publish_readiness_snapshot_current,
                "monthly_readiness_daily_sync_blocker_stale": monthly_readiness_daily_sync_stale,
                "current_owner_email_blocked_reason": current_owner_email_blocked_reason,
                "send_blocked_reason": publish_send_blocked_reason,
                "owner_email_send_decision_blocked_reason": publish_send_decision_blocked_reason,
                "owner_email_idempotency_blocked_reason": publish_idempotency_blocked_reason,
                "send_blocked_reason_current": publish_blocked_reason_current,
                "nested_blocked_reasons_current": publish_nested_blocked_reasons_current,
                "send_interval_days": publish_send_interval_days,
                "effective_send_owner_emails": publish.get("effective_send_owner_emails"),
                "owner_email_will_send_count": publish.get("owner_email_will_send_count"),
                "owner_email_send_evidence_count": publish.get("owner_email_send_evidence_count"),
                "owner_email_send_evidence_issue_count": publish.get("owner_email_send_evidence_issue_count"),
                "owner_email_policy": publish.get("owner_email_policy"),
            },
            [
                blocker
                for blocker in [
                    None if readiness.get("owner_email_allowed") is True else f"owner_email_waiting_for_readiness={current_owner_email_blocked_reason}",
                    None if monthly_readiness_blocks_downstream or publish.get("status") == "ok" else f"lofty_pm_publish={publish.get('status')}",
                    None if owner_email_send_guard_ok else f"owner_email_send_guard={owner_email_send_guard.get('status')}:{owner_email_send_guard.get('issue_count')}",
                    None if owner_email_packet_no_leak else f"owner_email_packet_full_history_leak_count={owner_email_packet.get('full_history_leak_count')}",
                    None if owner_email_packet_live_guard_ok else "owner_email_packet_live_update_guard_missing",
                    None if owner_email_packet_preview_telemetry_present else "owner_email_packet_preview_telemetry_missing",
                    None if not owner_email_packet_preview_allowed_without_packets else "owner_email_packet_preview_allowed_without_packets",
                    None if not owner_email_packet_previews_written_when_blocked else "owner_email_packet_previews_written_when_blocked",
                    None if owner_email_packet_unsafe_preview_packet_count == 0 else f"owner_email_packet_unsafe_preview_packet_count={owner_email_packet_unsafe_preview_packet_count}",
                    None if owner_email_packet_stale_preview_cleanup_error_count == 0 else f"owner_email_packet_stale_preview_cleanup_error_count={owner_email_packet_stale_preview_cleanup_error_count}",
                    None
                    if (not owner_email_packet_required_now or owner_email_packet_preview_block_reason_current)
                    else "owner_email_packet_preview_block_reason_stale",
                    None if (not owner_email_packet_required_now or owner_email_packet_ready) else f"owner_email_packet_not_ready={owner_email_packet.get('status')}:{owner_email_packet.get('packet_count')}p/{owner_email_packet.get('recipient_count')}r",
                    None if publish_send_interval_ok else f"send_interval_days={publish_send_interval_days}",
                    None if publish_idempotency_configured else "owner_email_idempotency_not_configured",
                    None if publish_send_decision_digest_configured else "owner_email_send_decision_digest_missing_or_mismatch",
                    None if publish_readiness_snapshot_configured else "owner_email_readiness_snapshot_missing",
                    None if (not publish_readiness_snapshot_configured or publish_readiness_snapshot_current) else f"owner_email_readiness_snapshot_stale:{publish_readiness_snapshot.get('blocker_count')}!={readiness.get('blocker_count')}",
                    None if publish_blocked_reason_current else "owner_email_send_blocked_reason_stale",
                    None if publish_nested_blocked_reasons_current else "owner_email_nested_blocked_reason_stale",
                    None if publish_evidence_issue_count == 0 else f"owner_email_send_evidence_issue_count={publish_evidence_issue_count}",
                    None if publish_send_safely_recorded else f"sent_state_write_status={publish_sent_state_status},will_send={publish_will_send_count},evidence={publish_evidence_count}",
                    None if publish_lock_safe else f"send_lock_status={publish_send_lock_status}",
                    None if publish_existing_lock_digest_safe else "existing_send_lock_decision_digest_mismatch",
                    None if skipped_exclusion_counts_match else "skipped_closed_exclusion_count_mismatch",
                    None if publish_excluded_no_payload_or_email else "excluded_closed_property_has_publish_or_email_artifact",
                ]
                if blocker
            ],
        ),
    ]

    ok_count = sum(1 for item in requirements if item["status"] == "ok")
    review_count = len(requirements) - ok_count
    completion_percent = 100 if not requirements else round((ok_count / len(requirements)) * 100)

    def action_path(value: object) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        path = Path(raw)
        try:
            if path.is_absolute():
                return str(path.relative_to(root))
        except ValueError:
            pass
        try:
            if path.is_absolute():
                return str(path.relative_to(root.parent))
        except ValueError:
            pass
        return raw

    def requirement_action(item: dict[str, Any]) -> dict[str, Any]:
        if item.get("status") == "ok":
            return {"blocker": None, "artifact": None, "next_action": None, "hold": None}
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        blockers_for_item = item.get("blockers") if isinstance(item.get("blockers"), list) else []
        blocker = blockers_for_item[0] if blockers_for_item else "review"
        requirement_id = item.get("id")
        artifact: str | None = None
        next_action = "Resolve this review blocker, then rerun scripts/baselane_financials_goal_audit.py."
        hold: str | None = None

        def downstream_daily_disk_hold_action(downstream_hold: str) -> str:
            return (
                "Resolve the daily Baselane disk-space blocker first: "
                f"{daily_disk_preflight_next_action} "
                f"Keep {downstream_hold} disabled until the daily sync rerun is clean."
            )

        if requirement_id == "daily_deterministic_sync":
            daily_scheduler_primary = evidence.get("daily_scheduler_primary_blocker") if isinstance(evidence.get("daily_scheduler_primary_blocker"), dict) else {}
            artifact = action_path(daily_scheduler_primary.get("artifact") or "reports/baselane_daily_sync_report.json")
            next_action = str(
                daily_scheduler_primary.get("next_action")
                or "Inspect reports/baselane_daily_run_report.json and the cron wrapper; rerun the human-paced sync only if canonical data is not current."
            )
            hold = str(daily_scheduler_primary.get("hold") or "weekly/monthly document updates")
        elif requirement_id == "monthly_15th_scheduler":
            artifact = "reports/baselane_scheduler_audit_report.json"
            next_action = (
                "Run python3 scripts/baselane_scheduler_audit.py and restore the 15th-of-month "
                "baselane_financials_monthly_cron.sh schedule lines before monthly publish/email."
            )
            hold = "monthly publish and investor email"
        elif requirement_id == "local_model_validation":
            artifact = "reports/baselane_local_model_preflight_report.json"
            blocker_detail = evidence.get("blocker_detail") if isinstance(evidence.get("blocker_detail"), dict) else {}
            next_action = str(
                blocker_detail.get("action")
                or "Refresh the ollama-cyber/qwen3.5:35b-a3b preflight and deterministic finance contract, then rerun the audit."
            )
        elif requirement_id == "eod_telegram_visibility":
            latest_source = str(evidence.get("latest_report_source") or "baselane_eod_telegram_report.json")
            artifact = f"reports/{latest_source}"
            latest_delivery_status = str(evidence.get("latest_report_telegram_delivery_status") or "")
            latest_dry_run = evidence.get("latest_report_dry_run") is True
            latest_is_fresh = fresh_generated_at({"generated_at": evidence.get("latest_report_generated_at")}, EOD_REPORT_MAX_AGE_HOURS)
            if latest_dry_run and latest_delivery_status == "preview_only" and latest_is_fresh:
                next_action = (
                    f"Send the latest EOD preview from reports/{latest_source} via explicit non-dry-run/scheduled delivery, "
                    "or leave it held if external-send approval is not intended; do not rerun only to refresh already-current evidence."
                )
            else:
                next_action = "Rerun scripts/baselane_eod_telegram_report.py after the daily and monthly reports are refreshed; send only when external-send approval is intended."
        elif requirement_id == "weekly_idempotent_file_updates":
            cf_balance_sheet_month_blocked = any(str(value).startswith("cf_balance_sheet_audit_run_month_mismatch") for value in blockers_for_item)
            cf_balance_sheet_blocked = any(str(value).startswith("cf_balance_sheet_consistency=") for value in blockers_for_item)
            weekly_primary = evidence.get("weekly_primary_blocker") if isinstance(evidence.get("weekly_primary_blocker"), dict) else {}
            active_weekly_reason_parts = {
                str(part)
                for part in evidence.get("weekly_active_reason_parts") or []
                if str(part)
            }
            mortgage_tokenomics_blocked = (
                active_weekly_reason_parts
                and active_weekly_reason_parts.issubset(MORTGAGE_TOKENOMICS_WEEKLY_REASON_PARTS)
            ) or weekly_mortgage_tokenomics_only(item)
            weekly_failed_hook = evidence.get("weekly_status") == "failed" and (
                "hook_returned_nonzero" in active_weekly_reason_parts or count(evidence.get("weekly_return_code")) != 0
            )
            weekly_cf_failed = evidence.get("weekly_cf_status") == "failed" or count(evidence.get("weekly_cf_return_code")) != 0
            artifact = action_path(
                "reports/baselane_cf_balance_sheet_consistency_audit.json"
                if cf_balance_sheet_month_blocked or cf_balance_sheet_blocked
                else weekly_primary.get("artifact")
                if weekly_primary and weekly_primary.get("id") == "weekly_disk_space_preflight"
                else weekly_primary.get("artifact")
                if weekly_primary and weekly_primary.get("id") in {"weekly_hook_failed", "weekly_cf_hook_failed"}
                else evidence.get("weekly_cf_report")
                if weekly_failed_hook and weekly_cf_failed
                else "reports/baselane_weekly_file_updates_run_report.json"
                if weekly_failed_hook
                else "reports/baselane_monthly_mortgage_workflow_review_packet.md"
                if mortgage_tokenomics_blocked
                else evidence.get("ecogl_source_fix_approval")
                or evidence.get("weekly_cf_gate")
                or "reports/baselane_weekly_file_updates_run_report.json"
            )
            if cf_balance_sheet_month_blocked:
                next_action = "Rerun scripts/baselane_cf_balance_sheet_consistency_audit.py for the current closed month, then rerun bash scripts/baselane_weekly_file_updates_cron.sh."
            elif cf_balance_sheet_blocked:
                next_action = "Resolve the exact Cash Flow balance-sheet discrepancies in reports/baselane_cf_balance_sheet_consistency_audit.json; use the cash apply dry-run and obtain approval before any --apply, then rerun the audit and weekly file updates."
            elif weekly_primary and weekly_primary.get("id") == "weekly_disk_space_preflight":
                next_action = str(weekly_primary.get("next_action") or "Free local Dropbox/Windows disk space, then rerun bash scripts/baselane_weekly_file_updates_cron.sh.")
            elif weekly_primary and weekly_primary.get("id") in {"weekly_hook_failed", "weekly_cf_hook_failed"}:
                next_action = str(weekly_primary.get("next_action") or "Open the weekly run report/log, fix the failed hook, then rerun bash scripts/baselane_weekly_file_updates_cron.sh.")
            elif weekly_failed_hook and weekly_cf_failed:
                next_action = "Open reports/baselane_weekly_cf_statement_sync_report.json and the weekly cron log, fix the CF hook failure, then rerun bash scripts/baselane_weekly_file_updates_cron.sh."
            elif weekly_failed_hook:
                next_action = "Open reports/baselane_weekly_file_updates_run_report.json and the weekly cron log, fix the failed hook, then rerun bash scripts/baselane_weekly_file_updates_cron.sh."
            elif str(blocker).startswith("source_cash_balance_violation_count") or int(evidence.get("ecogl_source_fix_remaining_count") or 0) > 0:
                next_action = "Fix exact Baselane source categories from the approved correction artifacts, export again, and rerun bash scripts/baselane_weekly_file_updates_cron.sh."
            elif mortgage_tokenomics_blocked:
                next_action = "Resolve the mortgage/coownership tokenomics workflow gates in reports/baselane_monthly_mortgage_workflow_review_packet.md and reports/mortgage_workflow_capture_queue.md, then rerun bash scripts/baselane_weekly_file_updates_cron.sh."
            else:
                next_action = "Rerun bash scripts/baselane_weekly_file_updates_cron.sh after resolving the weekly CF gate/idempotency blocker."
            hold = str(weekly_primary.get("hold") or "Lofty PM publish and investor email")
        elif requirement_id == "monthly_bank_statement_capture":
            artifact = action_path(evidence.get("report") or "reports/baselane_monthly_statements_idempotent_report.json")
            if daily_disk_preflight_blocked:
                artifact = action_path(daily_disk_preflight_artifact)
                next_action = downstream_daily_disk_hold_action("monthly statement capture, monthly publish, and investor email")
                hold = "monthly statement capture, monthly publish, and investor email"
            elif evidence.get("reason") == "disk-space-preflight" or evidence.get("action") == "free-disk":
                next_action = "Free local Dropbox/Windows disk space, then rerun monthly statement capture and this audit."
                hold = "monthly statement capture, monthly publish, and investor email"
            elif evidence.get("download_error_class") == "auth-required":
                if evidence.get("auth_recovery_attempted") is True and evidence.get("auth_recovery_manual_auth_required") is True:
                    next_action = "Automation already hard-refreshed/reopened Baselane statements; finish login in the visible tab, then run BASELANE_MONTHLY_STATEMENTS_GATE_ONLY=1 bash scripts/baselane_monthly_statements_idempotent.sh and rerun the audit."
                elif evidence.get("auth_recovery_attempted") is True and evidence.get("auth_recovery_status") == "ok":
                    next_action = "Automation found an authenticated Baselane tab; run bash scripts/baselane_monthly_statements_idempotent.sh to capture statements and rerun the audit."
                else:
                    next_action = "Authenticate Baselane in the visible browser tab, then run BASELANE_MONTHLY_STATEMENTS_GATE_ONLY=1 bash scripts/baselane_monthly_statements_idempotent.sh and rerun the audit."
            elif evidence.get("download_error_class") == "no-statement-buttons":
                next_action = "Baselane has no target-month statement download buttons yet; retry full statement capture after Baselane posts statements, then rerun the audit."
            else:
                next_action = "Run BASELANE_MONTHLY_STATEMENTS_GATE_ONLY=1 bash scripts/baselane_monthly_statements_idempotent.sh, then rerun the audit."
        elif requirement_id == "monthly_transfer_reconciliation_telegram":
            artifact = "reports/baselane_lofty_transfer_requirements_telegram_send.json"
            next_action = (
                "Regenerate reports/baselane_lofty_transfer_requirements.json and its Telegram markdown, "
                "then run scripts/send_monthly_transfer_reconciliation_telegram.py with non-dry-run send approval and "
                "the expected stable transfer-report digest."
            )
            hold = "monthly transfer reconciliation DM proof"
        elif requirement_id == "monthly_canonical_docs":
            artifact = "reports/baselane_discord_public_financial_source_guard.json"
            next_action = "Refresh canonical UPDATES.md/FINANCIALS.md folder guards and remove any legacy Financials/GL Rows sources before rerunning the audit."
            hold = "Lofty PM publish and investor email"
        elif requirement_id == "monthly_comms_rent_roll_context":
            artifact = action_path(evidence.get("gap_review_csv") or evidence.get("gap_review") or evidence.get("rent_roll_csv"))
            source = evidence.get("rent_roll_source") if isinstance(evidence.get("rent_roll_source"), dict) else {}
            source_stale = (
                str(source.get("freshness_status") or evidence.get("rent_roll_freshness_status") or "").strip() in {"missing", "stale"}
                or source.get("source_current") is False
                or int(source.get("pending_stale_export_date_count") or evidence.get("pending_stale_export_date_count") or 0) > 0
            )
            if source_stale:
                next_action = rent_roll_source_next_action(
                    source,
                    evidence.get("hemlane_cdp_preflight") if isinstance(evidence.get("hemlane_cdp_preflight"), dict) else hemlane_cdp_preflight,
                    run_month,
                    comms_root,
                    evidence.get("hemlane_cdp_capture") if isinstance(evidence.get("hemlane_cdp_capture"), dict) else None,
                )
            else:
                next_action = (
                    evidence.get("gap_review_monthly_dry_run_command")
                    or "Capture/approve rent-roll gap context, then rerun monthly_lofty_updates.sh --dry-run and this audit."
                )
            hold = "Lofty PM publish and investor email"
        elif requirement_id == "monthly_review_and_guarded_apply":
            artifact = action_path(evidence.get("owner_review_gate") or "reports/baselane_monthly_owner_review_gate.md")
            owner_gate_primary = (
                evidence.get("owner_review_gate_primary_blocker")
                if isinstance(evidence.get("owner_review_gate_primary_blocker"), dict)
                else {}
            )
            owner_gate_primary_action = str(
                owner_gate_primary.get("next_action")
                or owner_gate_primary.get("action")
                or ""
            ).strip()
            owner_gate_primary_artifact = action_path(owner_gate_primary.get("artifact"))
            if daily_disk_preflight_blocked:
                artifact = action_path(daily_disk_preflight_artifact)
                next_action = downstream_daily_disk_hold_action("guarded monthly apply, Lofty PM publish, and investor email")
            elif (
                owner_gate_primary_action
                and evidence.get("owner_review_gate_status") != "ok"
                and count(evidence.get("pending_update_review_count")) == 0
                and count(evidence.get("pending_financial_review_count")) == 0
            ):
                artifact = owner_gate_primary_artifact or artifact
                next_action = owner_gate_primary_action
            elif listing_cleanup_action and count((evidence.get("listing_cleanup_queue") or {}).get("ready_count")) > 0:
                next_action = combined_listing_financial_patch_action or listing_cleanup_action
            elif evidence.get("guarded_apply_waiting_for_live_guards"):
                next_action = (
                    f"{lofty_pm_next_action(cdp_preflight)} "
                    "Then rerun the guarded monthly apply dry-run."
                )
            elif evidence.get("guarded_apply_waiting_for_current_rent_roll"):
                next_action = (
                    "Finish Hemlane login/CAPTCHA and run `bash scripts/baselane_financials_post_auth_resume.sh`; "
                    "local monthly review and publish dry-run evidence is clean, but live guarded apply is held until current rent roll is captured."
                )
            elif str(evidence.get("live_guard_summary") or "").startswith("live_guard_reconcile_required="):
                next_action = combined_listing_financial_patch_action or listing_cleanup_action or financial_patch_action or "Run the guarded monthly apply dry-run against the registered live guard snapshots; resolve UPDATES.md/FINANCIALS.md diffs before publish."
            else:
                next_action = "Review/approve the owner gate, rerun guarded monthly apply, and confirm guard audit is clean before publish."
            hold = "Lofty PM publish and investor email"
        elif requirement_id == "lofty_pm_live_guard_workflow":
            artifact = "reports/lofty_cdp_preflight_report.json"
            if daily_disk_preflight_blocked:
                artifact = action_path(daily_disk_preflight_artifact)
                next_action = downstream_daily_disk_hold_action("Lofty PM publish and live guarded updates")
            elif evidence.get("cdp_preflight_status") != "ok":
                next_action = lofty_pm_next_action(cdp_preflight)
            elif evidence.get("live_update_capture_registered") is True and evidence.get("live_financial_capture_registered") is True:
                next_action = combined_listing_financial_patch_action or listing_cleanup_action or financial_patch_action or "Live guard snapshots are registered; reconcile live/local UPDATES.md and FINANCIALS.md guard diffs, then rerun the guarded apply/audit."
            else:
                next_action = "Rerun live UPDATES.md and FINANCIALS.md guard captures for all non-sold targets, then rerun the audit."
            hold = "Lofty PM publish and investor email"
        elif requirement_id == "owner_email_idempotent_no_spam":
            artifact = "reports/baselane_monthly_owner_email_send_guard.json"
            guild_action = str(evidence.get("owner_email_guild_test_next_action") or "").strip()
            if daily_disk_preflight_blocked:
                artifact = action_path(daily_disk_preflight_artifact)
                next_action = downstream_daily_disk_hold_action("owner email")
            elif evidence.get("monthly_readiness_daily_sync_blocker_stale"):
                next_action = (
                    "Refresh the monthly safe dry-run so owner email readiness uses the clean canonical daily sync report; "
                    "keep owner email disabled."
                )
            elif evidence.get("owner_email_allowed") is not True:
                next_action = "Resolve monthly readiness first; keep owner email disabled and do not send while this requirement is review."
            else:
                next_action = "Refresh publish evidence and owner-email send guard; send only once the idempotency lock and evidence digest are clean."
            if guild_action and evidence.get("owner_email_guild_test_valid") is not True and not daily_disk_preflight_blocked:
                next_action = f"{next_action} {guild_action}"
            hold = "Owner email send"

        return {
            "blocker": blocker,
            "artifact": artifact,
            "next_action": next_action,
            "hold": hold,
        }

    for item in requirements:
        item.update(requirement_action(item))
        first_blocker = (item.get("blockers") or [None])[0]
        item["requirement"] = item.get("id")
        item["summary"] = first_blocker or "ok"

    blockers = [
        {
            "id": item["id"],
            "requirement": item["id"],
            "title": item["title"],
            "summary": item.get("summary"),
            "blockers": item["blockers"],
            "blocker": item.get("blocker"),
            "artifact": item.get("artifact"),
            "next_action": item.get("next_action"),
            "hold": item.get("hold"),
            "evidence": item.get("evidence") or {},
        }
        for item in requirements
        if item["status"] != "ok"
    ]

    if first_day_pm_fee_cleanup_action_count:
        primary_blocker = {
            "id": "daily_deterministic_sync",
            "requirement": "daily_deterministic_sync",
            "title": "1st-day AOPS PM fee source cleanup",
            "summary": f"1st-day AOPS PM fee rows ({first_day_pm_fee_cleanup_action_count})",
            "blocker": f"1st-day AOPS PM fee rows ({first_day_pm_fee_cleanup_action_count})",
            "artifact": "reports/baselane_first_day_pm_fee_source_cleanup_actions.csv",
            "worksheet": "reports/baselane_first_day_pm_fee_source_cleanup_actions.csv",
            "next_action": f"Run {first_day_pm_fee_cleanup_command} before Baselane source-category mutation; source-fix apply is blocked while this queue is non-empty.",
            "hold": "Lofty PM publish and investor email",
            "source_quality_blocked_after_cleanup": not source_fix_reports_effectively_clear,
        }
    elif source_fix_row_count and not source_fix_reports_effectively_clear:
        primary_blocker = {
            "id": "weekly_idempotent_file_updates",
            "requirement": "weekly_idempotent_file_updates",
            "title": "ECO GL source quality",
            "summary": f"ECO GL source quality ({source_fix_row_count} exceptions)",
            "blocker": f"ECO GL source quality ({source_fix_row_count} exceptions)",
            "artifact": "reports/baselane_ecogl_source_fix_approval.json",
            "evidence": "reports/baselane_ecogl_source_fix_evidence.md",
            "worksheet": "reports/baselane_ecogl_source_fix_approved_corrections.csv",
            "approval": "reports/baselane_ecogl_source_fix_approval.json",
            "validation": "reports/baselane_ecogl_source_fix_correction_validation.md",
            "apply_plan": "reports/baselane_ecogl_source_fix_apply_plan.md",
            "apply_plan_csv": "reports/baselane_ecogl_source_fix_apply_plan.csv",
            "next_action": (
                f"Verify the {future_journal_action_count} future-dated journal ID(s) live, then reverse/delete or redate them upstream as appropriate; export again and rerun bash scripts/baselane_weekly_file_updates_cron.sh."
                if future_journal_action_count == source_fix_row_count
                else f"Run BASELANE_SOURCE_FIX_APPLY=1 bash scripts/baselane_apply_source_fix_then_refresh.sh for {source_fix_apply_ready_count} current-ID approved correction(s); resolve remaining pending categories in reports/baselane_ecogl_source_fix_approval.json."
                if source_fix_ready_to_apply and source_fix_validation_needs_input
                else "Approve valid categories in the source-fix approval JSON, run validation, update Baselane source rows, export again, then rerun bash scripts/baselane_weekly_file_updates_cron.sh."
                if source_fix_validation_needs_input
                else "Use the approved corrections CSV to fix exact Baselane source categories, export again, then rerun bash scripts/baselane_weekly_file_updates_cron.sh."
            ),
            "hold": "Lofty PM publish and investor email",
            "historical_evidence_status_counts": source_fix_evidence_status_counts,
            "historical_evidence_automation_safe_count": source_fix_automation_safe_count,
            "verified_fixed_count": source_fix_verified_fixed_count,
            "remaining_count": source_fix_remaining_count,
            "verifier_status_counts": source_fix_verifier_status_counts,
            "action_type_counts": source_fix_action_type_counts,
            "approval_status": ecogl_source_fix_approval.get("status"),
            "approval_approved_count": source_fix_approval_approved_count,
            "approval_pending_count": source_fix_approval_pending_count,
            "approval_invalid_count": source_fix_approval_invalid_count,
            "validation_status": ecogl_source_fix_correction_validation.get("status"),
            "validation_ready_count": source_fix_validation_ready_count,
            "validation_pending_count": source_fix_validation_pending_count,
            "validation_invalid_count": source_fix_validation_invalid_count,
            "apply_plan_status": ecogl_source_fix_apply_plan.get("status"),
            "apply_plan_ready_current_source_index_count": source_fix_apply_ready_count,
            "apply_plan_needs_current_source_index_refresh_count": source_fix_apply_refresh_count,
            "apply_plan_blocked_count": source_fix_apply_blocked_count,
        }
    elif blockers:
        first = sorted(enumerate(blockers), key=lambda pair: (primary_blocker_priority(pair[1]), pair[0]))[0][1]
        first_evidence = first.get("evidence") if isinstance(first.get("evidence"), dict) else {}
        first_source = first_evidence.get("rent_roll_source") if isinstance(first_evidence.get("rent_roll_source"), dict) else {}
        primary_hold = "Lofty PM publish and investor email"
        primary_summary = first.get("summary") or (first.get("blockers") or ["review"])[0]
        primary_blocker_text = (first.get("blockers") or ["review"])[0]
        if first.get("id") == "monthly_comms_rent_roll_context":
            artifact = action_path(first_evidence.get("gap_review_csv") or first_evidence.get("gap_review") or first_evidence.get("rent_roll_csv"))
            evidence = action_path(first_evidence.get("rent_roll_source_report") or first_evidence.get("summary"))
            first_source_stale = (
                str(first_source.get("freshness_status") or "").strip() in {"missing", "stale"}
                or first_source.get("source_current") is False
                or int(first_source.get("pending_stale_export_date_count") or 0) > 0
            )
            if first_source_stale:
                next_action = rent_roll_source_next_action(
                    first_source,
                    first_evidence.get("hemlane_cdp_preflight") if isinstance(first_evidence.get("hemlane_cdp_preflight"), dict) else hemlane_cdp_preflight,
                    run_month,
                    comms_root,
                    first_evidence.get("hemlane_cdp_capture") if isinstance(first_evidence.get("hemlane_cdp_capture"), dict) else None,
                )
            else:
                next_action = (
                    first_source.get("next_action")
                    or first_evidence.get("gap_review_monthly_dry_run_command")
                    or "Capture a current Hemlane rent roll for the run month with monthly_hemlane_cdp.sh, rerun monthly_lofty_updates.sh --dry-run only after that source is current, then rerun the Baselane goal audit."
                )
        elif first.get("id") == "daily_deterministic_sync":
            daily_scheduler_primary = (
                first_evidence.get("daily_scheduler_primary_blocker")
                if isinstance(first_evidence.get("daily_scheduler_primary_blocker"), dict)
                else {}
            )
            artifact = action_path(first.get("artifact") or daily_scheduler_primary.get("artifact") or "reports/baselane_daily_sync_report.json")
            evidence = action_path(daily_scheduler_primary.get("source_report") or "reports/baselane_daily_sync_report.json")
            next_action = first.get("next_action") or daily_scheduler_primary.get("next_action") or "Inspect reports/baselane_daily_run_report.json and the cron wrapper; rerun the human-paced sync only if canonical data is not current."
            primary_hold = first.get("hold") or daily_scheduler_primary.get("hold") or "weekly/monthly document updates"
            primary_summary = daily_scheduler_primary.get("summary") or first.get("summary") or primary_summary
            primary_blocker_text = daily_scheduler_primary.get("blocker") or first.get("blocker") or primary_blocker_text
        elif first.get("id") == "monthly_15th_scheduler":
            artifact = action_path(first.get("artifact") or "reports/baselane_scheduler_audit_report.json")
            evidence = action_path(first_evidence.get("monthly_scheduler_report") or "reports/baselane_financials_monthly_run_report.json")
            next_action = (
                first.get("next_action")
                or "Run python3 scripts/baselane_scheduler_audit.py and restore the 15th-of-month baselane_financials_monthly_cron.sh schedule lines before monthly publish/email."
            )
            primary_hold = first.get("hold") or "monthly publish and investor email"
        elif first.get("id") == "local_model_validation":
            blocker_detail = first_evidence.get("blocker_detail") if isinstance(first_evidence.get("blocker_detail"), dict) else {}
            artifact = action_path(first.get("artifact") or "reports/baselane_local_model_preflight_report.json")
            evidence = artifact
            next_action = str(
                first.get("next_action")
                or blocker_detail.get("action")
                or "Refresh the ollama-cyber/qwen3.5:35b-a3b preflight and deterministic finance contract, then rerun the audit."
            )
            primary_summary = blocker_detail.get("summary") or first.get("summary") or primary_summary
            primary_blocker_text = blocker_detail.get("code") or first.get("blocker") or primary_blocker_text
        elif first.get("id") == "monthly_bank_statement_capture":
            artifact = action_path(first_evidence.get("report") or "reports/baselane_monthly_statements_idempotent_report.json")
            evidence = action_path(first_evidence.get("download_report") or "reports/baselane_statements_download_report.json")
            if first_evidence.get("download_error_class") == "auth-required":
                if first_evidence.get("auth_recovery_attempted") is True and first_evidence.get("auth_recovery_manual_auth_required") is True:
                    next_action = "Automation already hard-refreshed/reopened Baselane statements; finish login in the visible tab, then run BASELANE_MONTHLY_STATEMENTS_GATE_ONLY=1 bash scripts/baselane_monthly_statements_idempotent.sh and rerun the Baselane goal audit."
                elif first_evidence.get("auth_recovery_attempted") is True and first_evidence.get("auth_recovery_status") == "ok":
                    next_action = "Automation found an authenticated Baselane tab; run bash scripts/baselane_monthly_statements_idempotent.sh to capture statements and rerun the Baselane goal audit."
                else:
                    next_action = "Authenticate Baselane in the visible browser tab, then run BASELANE_MONTHLY_STATEMENTS_GATE_ONLY=1 bash scripts/baselane_monthly_statements_idempotent.sh and rerun the Baselane goal audit."
            elif first_evidence.get("download_error_class") == "no-statement-buttons":
                next_action = "Baselane has no target-month statement download buttons yet; retry full statement capture after Baselane posts statements, then rerun the Baselane goal audit."
            else:
                next_action = "Run BASELANE_MONTHLY_STATEMENTS_GATE_ONLY=1 bash scripts/baselane_monthly_statements_idempotent.sh to refresh verified current target-month bank statement evidence, then rerun the Baselane goal audit."
        elif first.get("id") == "monthly_transfer_reconciliation_telegram":
            artifact = action_path(first.get("artifact") or "reports/baselane_lofty_transfer_requirements_telegram_send.json")
            evidence = "reports/baselane_lofty_transfer_requirements_telegram_send_state.json"
            next_action = (
                first.get("next_action")
                or "Regenerate reports/baselane_lofty_transfer_requirements.json and its Telegram markdown, then run scripts/send_monthly_transfer_reconciliation_telegram.py with non-dry-run send approval and the expected stable transfer-report digest."
            )
            primary_hold = first.get("hold") or "monthly transfer reconciliation DM proof"
        elif first.get("id") == "eod_telegram_visibility":
            first_latest_source = str(first_evidence.get("latest_report_source") or "baselane_eod_telegram_report.json")
            artifact = f"reports/{first_latest_source}"
            evidence = "reports/baselane_eod_telegram_send_state.json"
            next_action = "Send one non-dry-run EOD Telegram report or verify tonight's scheduled delivery; dry-run preview is diagnostic only."
        elif first.get("id") == "weekly_idempotent_file_updates":
            artifact = first.get("artifact")
            evidence = action_path(first_evidence.get("yhome_operating_cash_apply_verify_report")) or first.get("evidence")
            next_action = first.get("next_action") or "Rerun bash scripts/baselane_weekly_file_updates_cron.sh after resolving the weekly CF gate/idempotency blocker."
        elif first.get("id") == "monthly_review_and_guarded_apply":
            artifact = action_path(first.get("artifact") or first_evidence.get("owner_review_gate") or "reports/baselane_monthly_owner_review_gate.md")
            listing_cleanup = first_evidence.get("listing_cleanup_queue") if isinstance(first_evidence.get("listing_cleanup_queue"), dict) else {}
            owner_gate_primary = (
                first_evidence.get("owner_review_gate_primary_blocker")
                if isinstance(first_evidence.get("owner_review_gate_primary_blocker"), dict)
                else {}
            )
            owner_gate_primary_action = str(
                owner_gate_primary.get("next_action")
                or owner_gate_primary.get("action")
                or ""
            ).strip()
            owner_gate_primary_artifact = action_path(owner_gate_primary.get("artifact"))
            evidence = action_path(listing_cleanup.get("ready_cleanup_csv")) or first.get("evidence")
            if owner_gate_primary_action and first_evidence.get("owner_review_gate_status") != "ok":
                artifact = owner_gate_primary_artifact or artifact
                evidence = owner_gate_primary.get("evidence") or evidence
                next_action = owner_gate_primary_action
                primary_summary = owner_gate_primary.get("summary") or first.get("summary") or primary_summary
                primary_blocker_text = owner_gate_primary.get("blocker") or owner_gate_primary.get("id") or first.get("blocker") or primary_blocker_text
            else:
                next_action = first.get("next_action") or "Review/approve the owner gate, rerun guarded monthly apply, and confirm guard audit is clean before publish."
        else:
            artifact = None
            evidence = None
            next_action = "Resolve the first review blocker, then rerun scripts/baselane_financials_goal_audit.py."
        primary_blocker = {
            "id": first.get("id"),
            "requirement": first.get("requirement") or first.get("id"),
            "title": first.get("title"),
            "summary": primary_summary,
            "blocker": primary_blocker_text,
            "artifact": artifact,
            "evidence": evidence,
            "next_action": next_action,
            "hold": primary_hold,
        }
    else:
        primary_blocker = {
            "id": None,
            "requirement": None,
            "title": None,
            "summary": None,
            "blocker": None,
            "artifact": None,
            "evidence": None,
            "next_action": "No remaining goal blocker.",
            "hold": None,
        }
    primary_blocker_id = primary_blocker.get("id")
    secondary_actionable_blockers = [
        actionable_blocker_summary(item)
        for _, item in sorted(enumerate(blockers), key=lambda pair: (primary_blocker_priority(pair[1]), pair[0]))
        if item.get("id") != primary_blocker_id
    ][:3]
    actionable_summary = {
        "primary_blocker": primary_blocker,
        "secondary_blockers": secondary_actionable_blockers,
        "secondary_blocker_count": len(secondary_actionable_blockers),
        "actionable_blocker_count": len(blockers),
        "review_requirement_count": review_count,
        "ok_requirement_count": ok_count,
        "requirement_count": len(requirements),
        "completion_percent": completion_percent,
        "source_quality_is_upstream_blocker": not source_fix_reports_effectively_clear,
        "noise_policy": "Use primary_blocker for the first action and secondary_blockers for concurrent actionable holds; requirements remain the full completion audit.",
    }
    report = {
        "generated_at": iso_z(),
        "run_month": run_month,
        "status": "ok" if review_count == 0 else "review",
        "achieved": review_count == 0,
        "requirement_count": len(requirements),
        "ok_count": ok_count,
        "review_count": review_count,
        "completion_percent": completion_percent,
        "primary_blocker": primary_blocker,
        "next_action": primary_blocker.get("next_action"),
        "hold": primary_blocker.get("hold"),
        "actionable_summary": actionable_summary,
        "blockers": blockers,
        "requirements": requirements,
    }
    return sanitize_operator_action_text(report)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Baselane / Lofty Goal Audit",
        "",
        f"- Status: `{report['status']}`",
        f"- Achieved: `{report['achieved']}`",
        f"- Requirements: `{report['ok_count']}/{report['requirement_count']}` ok",
        f"- Completion: `{report.get('completion_percent')}%`",
        "",
        "## Action Required",
        "",
    ]
    primary = ((report.get("actionable_summary") or {}).get("primary_blocker") or {})
    secondary = (report.get("actionable_summary") or {}).get("secondary_blockers") or []
    lines.extend(
        [
            f"- Primary blocker: `{primary.get('blocker') or 'none'}`",
            f"- Open: `{primary.get('artifact') or 'none'}`",
            f"- Evidence: `{primary.get('evidence') or 'none'}`",
            f"- Next action: {primary.get('next_action') or 'No action required.'}",
            f"- Hold: `{primary.get('hold') or 'none'}`",
            "",
        ]
    )
    if secondary:
        lines.extend(["## Concurrent Holds", ""])
        for item in secondary:
            lines.append(f"- `{item.get('id')}`: {item.get('next_action') or item.get('blocker') or 'review'}")
        lines.append("")
    lines.extend(
        [
            "## Requirements",
            "",
        ]
    )
    for item in report["requirements"]:
        lines.append(f"### {item['title']}")
        lines.append(f"- ID: `{item['id']}`")
        lines.append(f"- Status: `{item['status']}`")
        lines.append(f"- Open: `{item.get('artifact') or 'none'}`")
        lines.append(f"- Next action: {item.get('next_action') or 'No action required.'}")
        if item.get("hold"):
            lines.append(f"- Hold: `{item.get('hold')}`")
        if item["blockers"]:
            for blocker in item["blockers"]:
                lines.append(f"- Blocker: {blocker}")
        else:
            lines.append("- Blocker: none")
        lines.append("")
    return "\n".join(lines)


def compact_status(report: dict[str, Any]) -> dict[str, Any]:
    actionable = report.get("actionable_summary") if isinstance(report.get("actionable_summary"), dict) else {}
    primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
    secondary = actionable.get("secondary_blockers") if isinstance(actionable.get("secondary_blockers"), list) else []
    return {
        "status": report["status"],
        "achieved": report["achieved"],
        "ok_count": report["ok_count"],
        "review_count": report["review_count"],
        "requirement_count": report["requirement_count"],
        "completion_percent": report.get("completion_percent"),
        "primary_blocker": primary,
        "secondary_blocker_count": len(secondary),
        "secondary_blockers": secondary,
        "next_action": primary.get("next_action"),
        "hold": primary.get("hold"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit current evidence against the Baselane/Lofty financial reporting goal.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    report_path = args.report or root / "reports" / "baselane_financials_goal_audit.json"
    markdown_path = args.markdown or root / "reports" / "baselane_financials_goal_audit.md"
    report = build_report(root)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(compact_status(report), indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
