#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from discord_summary_routing_policy import (
    EARLCOIN_GUILD_ID,
    EARLCOIN_REVIEW_FORUM_ID,
    EARLCOIN_REVIEW_TARGET,
    LOFTY_GUILD_ID,
    REVIEW_DESTINATION_CLASS,
    REVIEW_DESTINATION_PURPOSE,
)
from lofty_monthly_exclusions import DEFAULT_MANUAL_EXCLUDED_PROPERTIES, load_yhome_transition_exclusions
from transfer_report_digest import stable_transfer_report_digest


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


PUBLISH_REPORT_MAX_AGE_HOURS = 30.0
OWNER_EMAIL_PACKET_MAX_AGE_HOURS = 6.0
REQUIRED_MANUAL_EXCLUDED_PROPERTIES = DEFAULT_MANUAL_EXCLUDED_PROPERTIES
DEFAULT_YHOME_TRANSITION_RECONCILIATION_CANDIDATES = (
    Path("reports/yhome_transition_reconciliation.csv"),
    Path("reports/yhome_transition_reconciliation.live.20260702.refreshed.csv"),
    Path("reports/yhome_transition_reconciliation.live.20260702.csv"),
)
REQUIRED_LOFTY_GUILD_ID = LOFTY_GUILD_ID
REQUIRED_EARLCOIN_REVIEW_GUILD_ID = EARLCOIN_GUILD_ID
REQUIRED_EARLCOIN_REVIEW_FORUM_ID = EARLCOIN_REVIEW_FORUM_ID
REQUIRED_PROPERTY_EMAIL_COOLDOWN_DAYS = 7
MAX_SIGNAL_ONLY_EMAIL_BODY_CHARS = 3500
MAX_SIGNAL_ONLY_EMAIL_BODY_LINES = 80
MAX_SIGNAL_ONLY_PROPERTY_UPDATE_CHARS = 1800
MAX_SIGNAL_ONLY_PROPERTY_UPDATE_LINES = 45
DISCORD_SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")


def count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def generic_monthly_run_next_action(action: str) -> bool:
    text = str(action or "").strip().lower()
    return not text or "daily sync" in text or "failed monthly run step" in text or "monthly run report is ok" in text


def transfer_reconciliation_next_action(report: dict[str, Any]) -> str | None:
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
            f"Resolve the Baselane monthly accrual live-plan blocker: apply/verify {update_count} guarded live accrual update(s){plan_ref}{digest_ref}, "
            "then run human-paced Baselane sync and rerun transfer reconciliation and monthly readiness. "
            "Keep Lofty publish, Discord, Telegram, and owner email disabled until the transfer report is final."
        )
    return (
        f"Resolve the Baselane monthly accrual amount mismatch{plan_ref}{digest_ref}, then rerun transfer reconciliation and monthly readiness. "
        "Keep Lofty publish, Discord, Telegram, and owner email disabled until the transfer report is final."
    )


def lofty_listing_financial_update_chain_from_publish(publish: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(publish, dict):
        return {}
    publish_status = str(publish.get("status") or "")
    publish_apply = publish.get("apply") is True
    publish_attempted = publish.get("publish_attempted") is True
    publish_issue_count = count(publish.get("issue_count"))
    publish_result_count = count(publish.get("publish_result_count"))
    publish_failed_count = count(publish.get("publish_failed_count"))
    financial_enabled = publish.get("financial_publish_enabled") is True
    financial_result_count = count(publish.get("financial_publish_result_count"))
    financial_failed_count = count(publish.get("financial_publish_failed_count"))
    listing_full_history_count = count(publish.get("listing_update_full_history_count"))
    listing_non_history_count = count(publish.get("listing_update_non_history_count"))
    live_financial_guard = publish.get("live_financial_guard_report") if isinstance(publish.get("live_financial_guard_report"), dict) else {}
    live_financial_guard_ok = (
        live_financial_guard.get("status") == "ok"
        and count(live_financial_guard.get("guard_ok_count")) >= financial_result_count
        and financial_result_count > 0
    )
    guarded_apply_publish_mode_ready = (
        publish.get("guarded_apply_status") == "ok"
        and publish.get("guarded_apply_publish_mode_ready") is True
        and count(publish.get("guarded_apply_blocker_count")) == 0
        and count(publish.get("guarded_apply_suppressed_issue_count")) == 0
    )
    live_verified_idempotent_publish = (
        not publish_apply
        and not publish_attempted
        and guarded_apply_publish_mode_ready
        and live_financial_guard_ok
    )
    ok = (
        publish_status == "ok"
        and publish_issue_count == 0
        and publish_result_count > 0
        and publish_failed_count == 0
        and financial_enabled
        and financial_result_count > 0
        and financial_failed_count == 0
        and listing_full_history_count > 0
        and listing_non_history_count == 0
        and ((publish_apply and publish_attempted) or live_verified_idempotent_publish)
    )
    return {
        "status": "ok" if ok else "review",
        "source": "publish_report",
        "publish_status": publish_status,
        "publish_apply": publish_apply,
        "publish_attempted": publish_attempted,
        "publish_issue_count": publish_issue_count,
        "publish_result_count": publish_result_count,
        "publish_failed_count": publish_failed_count,
        "financial_publish_enabled": financial_enabled,
        "financial_publish_result_count": financial_result_count,
        "financial_publish_failed_count": financial_failed_count,
        "listing_update_full_history_count": listing_full_history_count,
        "listing_update_non_history_count": listing_non_history_count,
        "guarded_apply_publish_mode_ready": guarded_apply_publish_mode_ready,
        "live_financial_guard_ok": live_financial_guard_ok,
        "live_verified_idempotent_publish": live_verified_idempotent_publish,
    }


def sha256ish(value: object) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "")))


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def sha256_file(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def file_mtime_z(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except OSError:
        return None


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


def iso_month_matches(value: object, run_month: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return not run_month or parsed.astimezone(timezone.utc).strftime("%Y-%m") == run_month


def close_cycle_month_matches(value: object, run_month: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        run_year, run_month_number = (int(part) for part in run_month.split("-", 1))
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    close_year = run_year + (1 if run_month_number == 12 else 0)
    close_month_number = 1 if run_month_number == 12 else run_month_number + 1
    return parsed.astimezone(timezone.utc).strftime("%Y-%m") in {
        f"{run_year:04d}-{run_month_number:02d}",
        f"{close_year:04d}-{close_month_number:02d}",
    }


def required_fresh_generated_at(report: dict[str, Any], max_age_hours: float = PUBLISH_REPORT_MAX_AGE_HOURS) -> bool:
    if not str(report.get("generated_at") or "").strip():
        return False
    age_hours = iso_age_hours(report.get("generated_at"))
    return age_hours is not None and -1 <= age_hours <= max_age_hours


def monthly_readiness_blocked_reason(readiness: dict[str, Any]) -> str:
    actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
    primary = actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}
    primary_text = str(primary.get("blocker") or primary.get("class") or "").strip()
    actionable_count = count(actionable.get("actionable_blocker_count"))
    if primary_text:
        return f"monthly readiness owner_email_allowed=false; primary={primary_text}; actionable={actionable_count}"
    return f"monthly readiness owner_email_allowed=false; actionable={actionable_count}"


def stale_monthly_readiness_blocked_reason(blocked_reason: str, readiness: dict[str, Any]) -> bool:
    return (
        readiness.get("status") == "ok"
        and readiness.get("owner_email_allowed") is True
        and str(blocked_reason or "").strip().startswith("monthly readiness owner_email_allowed=false")
    )


def monthly_readiness_primary_blocker(readiness: dict[str, Any]) -> dict[str, Any]:
    primary = readiness.get("primary_blocker") if isinstance(readiness.get("primary_blocker"), dict) else {}
    if primary:
        return primary
    actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
    return actionable.get("primary_blocker") if isinstance(actionable.get("primary_blocker"), dict) else {}


def actionable_primary_blocker(blocker: dict[str, Any]) -> bool:
    if not isinstance(blocker, dict) or not blocker:
        return False
    values = {
        str(blocker.get(key) or "").strip().lower()
        for key in ("id", "class", "blocker", "summary")
        if str(blocker.get(key) or "").strip()
    }
    return bool(values and not values.issubset({"ok", "none", "null", "false"}))


def owner_email_guard_self_blocker(blocker: dict[str, Any]) -> bool:
    values = {
        str(blocker.get(key) or "").strip().lower()
        for key in ("id", "class", "blocker")
        if str(blocker.get(key) or "").strip()
    }
    artifact = Path(str(blocker.get("artifact") or "")).name
    return "owner_email.send_guard.not_ok" in values or (
        bool(
            values
            & {
                "financial_review_blocked",
                "transfer_telegram_send_gate_blocked",
                "discord_review_proof_blocked",
            }
        )
        and artifact == "baselane_monthly_owner_email_send_guard.json"
    )


def disk_space_preflight_blocker(blocker: dict[str, Any]) -> bool:
    values = {
        str(blocker.get(key) or "").strip().lower()
        for key in ("id", "class", "blocker")
        if str(blocker.get(key) or "").strip()
    }
    return "operational.disk_space_preflight.not_ok" in values


def disk_space_preflight_ok(report: dict[str, Any]) -> bool:
    return str(report.get("status") or "").strip() == "ok" and count(report.get("issue_count")) == 0


def disk_space_primary_blocker(report: dict[str, Any]) -> dict[str, Any]:
    required_free_mib = report.get("required_free_mib")
    try:
        required_free_text = f"{float(required_free_mib):.1f} MiB"
    except (TypeError, ValueError):
        required_free_text = "the required free space"
    next_action = str(report.get("next_action") or "").strip() or (
        f"Free local Dropbox/Windows disk space: free at least {required_free_text} before Baselane downloads or ledger writes."
    )
    return {
        "id": "operational.disk_space_preflight.not_ok",
        "class": "operational.disk_space_preflight.not_ok",
        "blocker": "operational.disk_space_preflight.not_ok",
        "summary": "Low local disk space blocks monthly owner-email packet generation.",
        "artifact": str(report.get("path") or "reports/baselane_financials_monthly_disk_space_preflight_report.json"),
        "required_free_mib": required_free_mib,
        "issues": report.get("issues") if isinstance(report.get("issues"), list) else [],
        "next_action": next_action + " Then rerun scripts/baselane_cron_run.sh and monthly publish/email.",
        "hold": "Lofty PM publish and investor email",
        "source": "current_disk_space_preflight",
    }


def current_monthly_disk_space_preflight_report(root: Path, monthly_run: dict[str, Any]) -> dict[str, Any]:
    artifacts = monthly_run.get("artifacts") if isinstance(monthly_run.get("artifacts"), dict) else {}
    raw_path = str(artifacts.get("disk_space_preflight") or "").strip()
    path = Path(raw_path) if raw_path else root / "reports" / "baselane_financials_monthly_disk_space_preflight_report.json"
    if not path.is_absolute():
        path = root / path
    return read_json(path)


def sanitize_owner_email_packet_primary_blocker(
    owner_email_packet: dict[str, Any],
    disk_space_preflight: dict[str, Any],
) -> dict[str, Any]:
    primary = (
        owner_email_packet.get("primary_blocker")
        if isinstance(owner_email_packet.get("primary_blocker"), dict)
        else {}
    )
    if not disk_space_preflight_blocker(primary):
        return owner_email_packet
    if not disk_space_preflight_ok(disk_space_preflight):
        updated = dict(owner_email_packet)
        updated["primary_blocker"] = disk_space_primary_blocker(disk_space_preflight)
        updated["stale_primary_blocker_replaced"] = primary
        updated["stale_primary_blocker_replaced_reason"] = "current_monthly_disk_space_preflight_still_blocked"
        return updated
    sanitized = dict(owner_email_packet)
    sanitized.pop("primary_blocker", None)
    sanitized["stale_primary_blocker_ignored"] = primary
    sanitized["stale_primary_blocker_ignored_reason"] = "current_monthly_disk_space_preflight_ok"
    return sanitized


def owner_email_final_primary_blocker(
    *,
    send_allowed: bool,
    safe_block: bool,
    issues: list[str],
    blocked_reason: str,
    owner_email_packet: dict[str, Any],
    financial_review: dict[str, Any],
    discord_all_property_guard: dict[str, Any],
    transfer_telegram_gate: dict[str, Any],
    readiness_primary: dict[str, Any],
) -> dict[str, Any]:
    if send_allowed:
        return {
            "id": "owner_email_final_gate_open",
            "class": "ok",
            "blocker": None,
            "summary": "owner email final gate open",
            "next_action": "Send owner emails only through the guarded non-native owner email packet sender.",
        }

    packet_primary = (
        owner_email_packet.get("primary_blocker")
        if isinstance(owner_email_packet.get("primary_blocker"), dict)
        else {}
    )
    if actionable_primary_blocker(packet_primary):
        blocker = dict(packet_primary)
        blocker.setdefault("id", blocker.get("class") or "owner_email_packet_primary_blocker")
        blocker.setdefault("source", "owner_email_packet")
        blocker.setdefault("summary", blocker.get("blocker") or blocker.get("class") or "owner email packet blocked")
        blocker.setdefault("next_action", owner_email_packet.get("next_action") or "Resolve owner email packet blocker.")
        return blocker

    if financial_review.get("blocked") is True:
        blockers = financial_review.get("blockers") if isinstance(financial_review.get("blockers"), list) else []
        first = blockers[0] if blockers and isinstance(blockers[0], dict) else {}
        return {
            "id": "email_final_gate_financial_blocked",
            "class": "financial_review_blocked",
            "blocker": first.get("reason") or first.get("source") or "financial review blockers remain",
            "summary": "owner email blocked by financial review gate",
            "count": financial_review.get("blocker_count"),
            "artifact": first.get("artifact") or first.get("report"),
            "next_action": first.get("next_action")
            or "Resolve monthly financial review blockers, regenerate Discord review proof, then rerun owner email guard.",
            "source": "financial_review",
        }

    if transfer_telegram_gate.get("ok") is not True:
        issues_list = transfer_telegram_gate.get("issues") if isinstance(transfer_telegram_gate.get("issues"), list) else []
        return {
            "id": "email_final_gate_transfer_telegram_blocked",
            "class": "transfer_telegram_send_gate_blocked",
            "blocker": issues_list[0] if issues_list else "transfer Telegram reconciliation send gate is not ok",
            "summary": "owner email blocked by Telegram transfer reconciliation gate",
            "count": len(issues_list),
            "next_action": "Send or validate the Telegram monthly transfer reconciliation DM from the current transfer report, then rerun owner email guard.",
            "source": "transfer_telegram_send_gate",
        }

    if discord_all_property_guard.get("ok") is not True:
        issues_list = discord_all_property_guard.get("issues") if isinstance(discord_all_property_guard.get("issues"), list) else []
        return {
            "id": "email_final_gate_discord_review_blocked",
            "class": "discord_review_proof_blocked",
            "blocker": issues_list[0] if issues_list else "Discord all-property review proof is not ok",
            "summary": "owner email blocked by Discord review proof gate",
            "count": len(issues_list),
            "next_action": "Post or verify the all-property Discord review from the current plan, then rerun owner email guard.",
            "source": "discord_all_property_send_guard",
        }

    if issues:
        return {
            "id": "owner_email_send_guard_issue",
            "class": "owner_email_send_guard_issue",
            "blocker": issues[0],
            "summary": "owner email send guard has blocking issues",
            "count": len(issues),
            "next_action": "Resolve owner email send guard issues, then rerun the guard.",
            "source": "owner_email_send_guard",
        }

    if actionable_primary_blocker(readiness_primary):
        blocker = dict(readiness_primary)
        blocker.setdefault("id", blocker.get("class") or "monthly_readiness_primary_blocker")
        blocker.setdefault("source", "monthly_readiness")
        blocker.setdefault("summary", blocker.get("blocker") or blocker.get("class") or "monthly readiness blocked")
        blocker.setdefault("next_action", blocker.get("next_action") or "Resolve monthly readiness primary blocker.")
        return blocker

    if safe_block:
        return {
            "id": "owner_email_safe_block",
            "class": "safe_block",
            "blocker": blocked_reason or "owner email send not requested",
            "summary": "owner email safely blocked",
            "next_action": "No email will send until SEND_OWNER_EMAILS is enabled and every final gate is clean.",
            "source": "owner_email_send_guard",
        }

    return {
        "id": "owner_email_final_gate_blocked",
        "class": "blocked",
        "blocker": blocked_reason or "owner email final gate is not open",
        "summary": "owner email final gate blocked",
        "next_action": "Inspect owner email send guard inputs and rerun after resolving blockers.",
        "source": "owner_email_send_guard",
    }


def guild_test_post_snapshot_valid(snapshot: dict[str, Any], run_month: str) -> bool:
    status = str(snapshot.get("status") or "")
    posted = snapshot.get("posted") is True or snapshot.get("post_status") in {"ok", "sent", "posted"}
    month = str(snapshot.get("run_month") or "")
    month_ok = not run_month or not month or month == run_month
    message_id = str(snapshot.get("posted_message_id") or "")
    channel_id = str(snapshot.get("posted_channel_id") or "")
    target = str(snapshot.get("target") or "")
    target_channel_id = target.removeprefix("channel:") if target.startswith("channel:") else ""
    ids_ok = bool(DISCORD_SNOWFLAKE_RE.fullmatch(message_id)) and bool(DISCORD_SNOWFLAKE_RE.fullmatch(channel_id))
    target_ok = not target_channel_id or channel_id == target_channel_id
    posted_at_ok = close_cycle_month_matches(snapshot.get("posted_at"), run_month)
    digest_ok = sha256ish(snapshot.get("digest"))
    return (
        status in {"ok", "sent", "posted"}
        and snapshot.get("valid") is True
        and posted
        and month_ok
        and ids_ok
        and target_ok
        and guild_test_post_lofty_guild_ok(snapshot)
        and posted_at_ok
        and digest_ok
    )


def guild_test_post_lofty_guild_ok(snapshot: dict[str, Any]) -> bool:
    selected = snapshot.get("selected") if isinstance(snapshot.get("selected"), dict) else {}
    route_report = snapshot.get("route_report") if isinstance(snapshot.get("route_report"), dict) else {}
    route_result = route_report.get("result") if isinstance(route_report.get("result"), dict) else {}
    envelope = snapshot.get("envelope") if isinstance(snapshot.get("envelope"), dict) else {}
    candidates = (
        snapshot.get("guild_id"),
        snapshot.get("guildId"),
        selected.get("guild_id"),
        selected.get("guildId"),
        route_result.get("guild_id"),
        route_result.get("guildId"),
        envelope.get("guild_id"),
        envelope.get("guildId"),
    )
    return any(str(candidate or "") == REQUIRED_LOFTY_GUILD_ID for candidate in candidates)


def guild_test_post_route_ok(snapshot: dict[str, Any]) -> bool:
    target = str(snapshot.get("target") or "")
    if not target.startswith("channel:"):
        return False
    selected = snapshot.get("selected") if isinstance(snapshot.get("selected"), dict) else {}
    route_report = snapshot.get("route_report") if isinstance(snapshot.get("route_report"), dict) else {}
    route_result = route_report.get("result") if isinstance(route_report.get("result"), dict) else {}
    selected_target_ok = selected.get("route_matched") is True and str(selected.get("target") or "") == target
    result_target_ok = route_result.get("route_matched") is True and str(route_result.get("target") or "") == target
    property_name = str(selected.get("property_name") or snapshot.get("property_name") or "").strip()
    return bool(property_name and (selected_target_ok or result_target_ok) and guild_test_post_lofty_guild_ok(snapshot))


def guild_test_post_handoff(snapshot: dict[str, Any], run_month: str) -> dict[str, Any]:
    selected = snapshot.get("selected") if isinstance(snapshot.get("selected"), dict) else {}
    route_report = snapshot.get("route_report") if isinstance(snapshot.get("route_report"), dict) else {}
    route_result = route_report.get("result") if isinstance(route_report.get("result"), dict) else {}
    target = str(snapshot.get("target") or selected.get("target") or "").strip()
    property_name = str(
        selected.get("property_name")
        or snapshot.get("property_name")
        or route_result.get("property", "")
    ).strip()
    message_file = str(snapshot.get("message_file") or "").strip()
    envelope_file = str(snapshot.get("envelope_file") or "").strip()
    post_status = str(snapshot.get("post_status") or snapshot.get("status") or "").strip()
    posted = snapshot.get("posted") is True or post_status in {"ok", "sent", "posted"}
    prepared = bool(snapshot.get("prepared") is True or (message_file and envelope_file and target and property_name))
    valid = guild_test_post_snapshot_valid(snapshot, run_month)
    route_ok = guild_test_post_route_ok(snapshot)
    target_channel_id = target.removeprefix("channel:") if target.startswith("channel:") else ""
    next_action = str(snapshot.get("next_action") or "").strip()
    if not next_action:
        if valid:
            next_action = "Guild test post is proven; owner email may proceed only if every other send guard is clean."
        elif prepared and route_ok:
            next_action = "Post message_file to target after explicit approval, then rerun with posted message/channel IDs."
        elif snapshot:
            next_action = "Prepare a routed guild test post snapshot before allowing owner email."
        else:
            next_action = "Create a guild test post snapshot before allowing owner email."
    return {
        "status": str(snapshot.get("status") or "").strip(),
        "post_status": post_status,
        "prepared": prepared,
        "posted": posted,
        "valid": valid,
        "route_proof_ok": route_ok,
        "lofty_guild_ok": guild_test_post_lofty_guild_ok(snapshot),
        "required_lofty_guild_id": REQUIRED_LOFTY_GUILD_ID,
        "digest_ok": sha256ish(snapshot.get("digest")),
        "posted_at": snapshot.get("posted_at"),
        "posted_at_month_matches": close_cycle_month_matches(snapshot.get("posted_at"), run_month),
        "message_file": message_file,
        "envelope_file": envelope_file,
        "target": target,
        "target_channel_id": target_channel_id,
        "selected_property_name": property_name,
        "posted_channel_id": snapshot.get("posted_channel_id"),
        "posted_message_id": snapshot.get("posted_message_id"),
        "run_month": str(snapshot.get("run_month") or ""),
        "run_month_matches": (
            not run_month
            or not str(snapshot.get("run_month") or "")
            or str(snapshot.get("run_month")) == run_month
        ),
        "next_action": next_action,
    }


def discord_all_property_send_guard(
    discord_send: dict[str, Any],
    monthly_run: dict[str, Any],
    discord_plan_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    discord_plan_validation = discord_plan_validation or {}
    unloaded_statuses = {"", "missing", "unreadable", "not_checked"}
    send_status_raw = str(discord_send.get("status") or "")
    monthly_status_raw = str(monthly_run.get("status") or "")
    send_report_loaded = send_status_raw not in unloaded_statuses
    monthly_run_loaded = monthly_status_raw not in unloaded_statuses or any(
        key in monthly_run
        for key in (
            "discord_property_update_status",
            "discord_property_update_send_mode",
            "discord_all_property_send_proof_ok",
            "discord_all_property_send_record_count",
        )
    )
    use_send_report = send_report_loaded
    status = str(
        (discord_send.get("status") if use_send_report else monthly_run.get("discord_property_update_status")) or ""
    )
    mode = str((discord_send.get("mode") if use_send_report else monthly_run.get("discord_property_update_send_mode")) or "")
    dry_run = (
        (discord_send.get("dry_run") if use_send_report else monthly_run.get("discord_property_update_dry_run")) is True
        or status in {"ok_dry_run", "ok_partial_dry_run"}
    )
    all_property_proof_ok = bool(
        (discord_send.get("all_property_discord_review_proof_ok") if use_send_report else None) is True
        or monthly_run.get("discord_all_property_send_proof_ok") is True
    )
    eligible_property_proof_ok = bool(
        (discord_send.get("eligible_property_discord_review_proof_ok") if use_send_report else None) is True
        or all_property_proof_ok
    )
    eligible_live_post_ok = bool(
        (discord_send.get("discord_eligible_property_live_post_ok") if use_send_report else None) is True
        or
        (discord_send.get("discord_all_property_live_post_ok") if use_send_report else None) is True
        or monthly_run.get("discord_all_property_live_post_ok") is True
        or (
            status in {"ok", "ok_partial"}
            and not dry_run
            and eligible_property_proof_ok
        )
    )
    dry_run_verified = bool(
        (discord_send.get("discord_all_property_dry_run_verified") if use_send_report else None) is True
        or dry_run
        and eligible_property_proof_ok
    )
    record_count = count(
        (discord_send.get("record_count") if use_send_report else None)
        or monthly_run.get("discord_all_property_send_record_count")
    )
    verified_count = count(
        (discord_send.get("sent_or_verified_count") if use_send_report else None)
        or monthly_run.get("discord_all_property_send_verified_count")
    )
    failed_count = count(
        (discord_send.get("failed_count") if use_send_report else None)
        or monthly_run.get("discord_all_property_send_failed_count")
    )
    current_plan_record_count = count(discord_plan_validation.get("record_count"))
    eligible_record_count = count(
        (discord_send.get("eligible_record_count") if use_send_report else None)
        or discord_plan_validation.get("financial_review_ready_record_count")
        or record_count
    )
    held_record_count = count(
        (discord_send.get("held_financial_review_count") if use_send_report else None)
        or discord_plan_validation.get("financial_review_blocked_record_count")
    )
    record_count_matches_current_plan = bool(
        current_plan_record_count <= 0 or record_count == current_plan_record_count
    )
    issue_counts = (
        discord_send.get("issue_counts")
        if use_send_report and isinstance(discord_send.get("issue_counts"), dict)
        else {}
    )
    plan_digest = str((discord_send.get("plan_digest") if use_send_report else None) or monthly_run.get("discord_all_property_send_plan_digest") or "")
    plan_digest_ok = sha256ish(plan_digest)
    route_source = discord_send if use_send_report else monthly_run
    review_destination = (
        route_source.get("review_destination")
        if isinstance(route_source.get("review_destination"), dict)
        else {}
    )
    destination_class = str(
        route_source.get("destination_class") or review_destination.get("destination_class") or ""
    )
    destination_purpose = str(
        route_source.get("destination_purpose") or review_destination.get("destination_purpose") or ""
    )
    destination_guild_id = str(
        route_source.get("guild_id")
        or route_source.get("destination_guild_id")
        or review_destination.get("guild_id")
        or ""
    )
    destination_forum_id = str(
        route_source.get("forum_id")
        or route_source.get("destination_forum_id")
        or review_destination.get("forum_id")
        or ""
    )
    destination_target = str(
        route_source.get("target")
        or route_source.get("destination_target")
        or review_destination.get("target")
        or ""
    )
    earlcoin_review_route_ok = bool(
        destination_class == REVIEW_DESTINATION_CLASS
        and destination_purpose == REVIEW_DESTINATION_PURPOSE
        and destination_guild_id == REQUIRED_EARLCOIN_REVIEW_GUILD_ID
        and destination_forum_id == REQUIRED_EARLCOIN_REVIEW_FORUM_ID
        and destination_target == EARLCOIN_REVIEW_TARGET
    )
    if not issue_counts and isinstance(monthly_run.get("discord_all_property_send_issue_counts"), dict):
        issue_counts = monthly_run.get("discord_all_property_send_issue_counts")
    ok = bool(
        (send_report_loaded or monthly_run_loaded)
        and status in {"ok", "ok_partial"}
        and not dry_run
        and mode == "all_plan"
        and eligible_property_proof_ok
        and eligible_live_post_ok
        and record_count > 0
        and eligible_record_count > 0
        and verified_count == eligible_record_count
        and failed_count == 0
        and plan_digest_ok
        and record_count_matches_current_plan
        and earlcoin_review_route_ok
    )
    issues: list[str] = []
    if not send_report_loaded and not monthly_run_loaded:
        issues.append(f"discord_send_report_{discord_send.get('status')}")
    if status not in {"ok", "ok_partial"}:
        issues.append(f"discord_send_status={status or 'missing'}")
    if dry_run:
        issues.append("discord_all_property_send_dry_run_not_final_review")
    if mode != "all_plan":
        issues.append(f"discord_send_mode={mode or 'missing'}")
    if not eligible_property_proof_ok:
        issues.append("discord_all_property_send_proof_ok=false")
    if not eligible_live_post_ok:
        issues.append("discord_all_property_live_post_ok=false")
    if record_count <= 0:
        issues.append("discord_all_property_send_record_count=0")
    if verified_count != eligible_record_count:
        issues.append(f"discord_all_property_send_verified_count={verified_count}/{eligible_record_count}")
    if failed_count:
        issues.append(f"discord_all_property_send_failed_count={failed_count}")
    if not plan_digest_ok:
        issues.append("discord_all_property_send_plan_digest_missing_or_invalid")
    if not record_count_matches_current_plan:
        issues.append(
            f"discord_all_property_send_record_count_mismatch_current_plan={record_count}/{current_plan_record_count}"
        )
    if not earlcoin_review_route_ok:
        issues.append(
            "discord_review_destination_not_canonical_earlcoin_forum:"
            f"guild={destination_guild_id or 'missing'}:forum={destination_forum_id or 'missing'}:"
            f"purpose={destination_purpose or 'missing'}"
        )
    return {
        "ok": ok,
        "required": True,
        "status": status,
        "dry_run": dry_run,
        "mode": mode,
        "proof_ok": eligible_property_proof_ok,
        "all_property_proof_ok": all_property_proof_ok,
        "eligible_property_proof_ok": eligible_property_proof_ok,
        "live_post_ok": eligible_live_post_ok,
        "eligible_live_post_ok": eligible_live_post_ok,
        "dry_run_verified": dry_run_verified,
        "record_count": record_count,
        "eligible_record_count": eligible_record_count,
        "held_record_count": held_record_count,
        "current_plan_record_count": current_plan_record_count,
        "record_count_matches_current_plan": record_count_matches_current_plan,
        "verified_count": verified_count,
        "failed_count": failed_count,
        "issue_counts": issue_counts,
        "plan_digest": plan_digest,
        "plan_digest_ok": plan_digest_ok,
        "earlcoin_review_route_ok": earlcoin_review_route_ok,
        "destination_class": destination_class,
        "destination_purpose": destination_purpose,
        "destination_guild_id": destination_guild_id,
        "destination_forum_id": destination_forum_id,
        "destination_target": destination_target,
        "required_earlcoin_review_guild_id": REQUIRED_EARLCOIN_REVIEW_GUILD_ID,
        "required_earlcoin_review_forum_id": REQUIRED_EARLCOIN_REVIEW_FORUM_ID,
        "issues": issues,
        "send_report_status": discord_send.get("status"),
        "send_report_path": discord_send.get("path"),
    }


def readiness_snapshot(readiness: dict[str, Any], readiness_path: Path) -> dict[str, Any]:
    snapshot = {
        "path": str(readiness_path),
        "status": readiness.get("status"),
        "run_month": readiness.get("run_month"),
        "owner_email_allowed": readiness.get("owner_email_allowed"),
        "owner_email_blocked_reason": readiness.get("owner_email_blocked_reason"),
        "blocker_count": readiness.get("blocker_count"),
        "blocked_property_count": readiness.get("blocked_property_count"),
        "primary_blocker": monthly_readiness_primary_blocker(readiness),
        "counts": readiness.get("counts") or {},
        "actionable_summary": readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {},
    }
    snapshot["digest"] = stable_digest(snapshot)
    snapshot["source_generated_at"] = readiness.get("generated_at")
    snapshot["source_mtime"] = file_mtime_z(readiness_path)
    return snapshot


def list_of_dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def normalize_property_name(value: object) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\blfty\d+\b", " ", text)
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\bavenue\b", "ave", text)
    text = re.sub(r"\bstreet\b", "st", text)
    text = re.sub(r"\bpublic\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def collect_excluded_property_keys(*reports: dict[str, Any]) -> set[str]:
    excluded_keys: set[str] = set()
    list_keys = {
        "excluded_property_names",
        "excluded_total_property_names",
        "manual_excluded_property_names",
        "skipped_closed_property_names",
        "skipped_sold_property_names",
    }
    record_statuses = {
        "excluded_no_live_update_or_email",
        "skipped_sold",
        "skipped_closed",
        "skipped_delisted",
        "blocked_excluded_property",
    }

    def add(value: object) -> None:
        key = normalize_property_name(value)
        if key:
            excluded_keys.add(key)

    def walk(value: object, key_name: str = "") -> None:
        if isinstance(value, dict):
            raw_status_values = [
                str(value.get("status") or "").strip().lower(),
                str(value.get("update_status") or "").strip().lower(),
                str(value.get("financial_status") or "").strip().lower(),
            ]
            if any(status in record_statuses for status in raw_status_values):
                add(
                    value.get("property_name")
                    or value.get("property")
                    or value.get("name")
                    or Path(str(value.get("property_path") or value.get("input_property_path") or "")).name
                )
            for child_key, child_value in value.items():
                walk(child_value, str(child_key))
        elif isinstance(value, list):
            if key_name in list_keys:
                for item in value:
                    if isinstance(item, dict):
                        add(item.get("property_name") or item.get("property") or item.get("name"))
                    else:
                        add(item)
                return
            for item in value:
                walk(item, key_name)

    for report in reports:
        if isinstance(report, dict):
            walk(report)
    return excluded_keys


def monthly_summary_issue_excluded(record: dict[str, Any], excluded_keys: set[str]) -> bool:
    record_keys = [
        normalize_property_name(record.get("property_name")),
        normalize_property_name(record.get("property")),
        normalize_property_name(record.get("matched_exclusion_property")),
    ]
    record_keys = [key for key in record_keys if key]
    return any(
        excluded == key or excluded in key or key in excluded
        for key in record_keys
        for excluded in excluded_keys
    )


def financial_review_gate(
    readiness: dict[str, Any],
    transfer_reconciliation: dict[str, Any],
    discord_plan_validation: dict[str, Any],
    monthly_run: dict[str, Any] | None = None,
    publish: dict[str, Any] | None = None,
) -> dict[str, Any]:
    monthly_run = monthly_run if isinstance(monthly_run, dict) else {}
    publish = publish if isinstance(publish, dict) else {}
    monthly_run_status = str(monthly_run.get("status") or "")
    monthly_run_failed_step = str(
        monthly_run.get("effective_failed_step") or monthly_run.get("failed_step") or ""
    ).strip()
    monthly_run_next_action = str(monthly_run.get("next_action") or "").strip()
    readiness_primary = monthly_readiness_primary_blocker(readiness)
    readiness_financial_blockers = []
    if (
        readiness.get("owner_email_allowed") is False
        and actionable_primary_blocker(readiness_primary)
        and not owner_email_guard_self_blocker(readiness_primary)
    ):
        readiness_financial_blockers.append(readiness_primary)

    transfer_source_blockers = [
        str(item)
        for item in (transfer_reconciliation.get("source_blockers") or [])
        if str(item or "").strip()
    ]
    transfer_active_source_cash_actions = list_of_dicts(
        transfer_reconciliation.get("source_cash_reconciliation_active_monthly_candidate_actions")
    )
    transfer_property_cash_review_details = [
        item
        for item in list_of_dicts(transfer_reconciliation.get("property_cash_review_details"))
        if item.get("property_cash_review_required") is True
        or item.get("recommended_transfer_instruction")
        or count(item.get("property_cash_review_classification_review_count") or item.get("classification_review_count")) > 0
        or abs(
            float(
                item.get("property_cash_review_high_priority_unresolved_sum")
                or (item.get("net_cash_exposure_review") or {}).get("high_priority_unresolved_sum")
                or 0
            )
        )
        > 0
    ]
    transfer_property_cash_review_details.extend(
        item
        for item in list_of_dicts(transfer_reconciliation.get("rows"))
        if item.get("property_cash_review_required") is True
        or count(item.get("property_cash_review_classification_review_count")) > 0
        or abs(float(item.get("property_cash_review_high_priority_unresolved_sum") or 0)) > 0
    )
    transfer_status = str(transfer_reconciliation.get("status") or "not_checked")
    transfer_report_loaded = transfer_status not in {"not_checked", "missing", "unreadable"}
    transfer_reconciliation_final = transfer_reconciliation.get("recommended_send_to_lofty_total_is_final") is True
    transfer_next_action = transfer_reconciliation_next_action(transfer_reconciliation)
    readiness_next_action = str(readiness_primary.get("next_action") or "").strip()
    if generic_monthly_run_next_action(monthly_run_next_action):
        if transfer_next_action:
            monthly_run_next_action = transfer_next_action
        elif readiness_next_action and str(readiness_primary.get("class") or "").startswith("operational.monthly_run"):
            monthly_run_next_action = readiness_next_action
    discord_financial_review_issues = [
        str(item)
        for item in (discord_plan_validation.get("financial_review_issues") or [])
        if str(item or "").strip()
    ]
    discord_plan_status = str(discord_plan_validation.get("status") or "")
    discord_financial_issue_count = count(discord_plan_validation.get("financial_review_issue_count"))
    discord_global_financial_issue_count = count(
        discord_plan_validation.get("global_financial_review_issue_count")
        if discord_plan_validation.get("global_financial_review_issue_count") is not None
        else discord_financial_issue_count
    )
    discord_plan_validation_issue_count = count(discord_plan_validation.get("issue_count"))
    discord_financial_review_artifacts = (
        discord_plan_validation.get("financial_review_artifacts")
        if isinstance(discord_plan_validation.get("financial_review_artifacts"), list)
        else []
    )
    discord_plan_non_financial_issue_count = count(discord_plan_validation.get("non_financial_issue_count"))
    discord_review_ready = discord_plan_validation.get("discord_review_ready") is True
    discord_review_ready_but_financial_blocked = (
        discord_plan_validation.get("discord_review_ready_but_financial_blocked") is True
    )
    discord_non_financial_issues = [
        str(item)
        for item in (discord_plan_validation.get("issues") or [])
        if str(item or "").strip() and str(item) not in {f"{discord_financial_issue_count} financial review blocker(s) present"}
    ]
    lofty_listing_financial_update_chain = monthly_run.get("lofty_listing_financial_update_chain")
    lofty_listing_financial_update_chain = (
        lofty_listing_financial_update_chain
        if isinstance(lofty_listing_financial_update_chain, dict)
        else {}
    )
    publish_financial_chain = lofty_listing_financial_update_chain_from_publish(publish)
    if (
        lofty_listing_financial_update_chain.get("status") != "ok"
        and publish_financial_chain.get("status") == "ok"
    ):
        lofty_listing_financial_update_chain = publish_financial_chain
    lofty_listing_financial_update_chain_ok = lofty_listing_financial_update_chain.get("status") == "ok"
    lofty_monthly_summary_issue_records = []
    for key in (
        "lofty_financial_patch_candidate_packet_monthly_summary_issue_records",
        "lofty_financial_patch_runtime_monthly_summary_issue_records",
    ):
        lofty_monthly_summary_issue_records.extend(list_of_dicts(monthly_run.get(key)))
    excluded_property_keys = collect_excluded_property_keys(readiness, publish, monthly_run)
    excluded_lofty_monthly_summary_issue_records = [
        record
        for record in lofty_monthly_summary_issue_records
        if monthly_summary_issue_excluded(record, excluded_property_keys)
    ]
    lofty_monthly_summary_issue_records = [
        record
        for record in lofty_monthly_summary_issue_records
        if not monthly_summary_issue_excluded(record, excluded_property_keys)
    ]

    blockers: list[dict[str, Any]] = []
    if monthly_run_status == "failed":
        blockers.append(
            {
                "source": "monthly_run",
                "status": monthly_run_status,
                "failed_step": monthly_run_failed_step,
                "reason": monthly_run_failed_step or "monthly run failed",
                "next_action": monthly_run_next_action
                or "Resolve the failed monthly close step, then rerun owner email guard.",
            }
        )
    if readiness_financial_blockers:
        blockers.append({"source": "monthly_readiness", "items": readiness_financial_blockers})
    if not lofty_listing_financial_update_chain_ok:
        blockers.append(
            {
                "source": "lofty_listing_financial_update_chain",
                "reason": f"status={lofty_listing_financial_update_chain.get('status')}",
                "chain": lofty_listing_financial_update_chain,
                "monthly_summary_issue_records": lofty_monthly_summary_issue_records,
            }
        )
    if transfer_report_loaded and not transfer_reconciliation_final:
        blockers.append(
            {
                "source": "transfer_reconciliation",
                "reason": "recommended_send_to_lofty_total_is_final=false",
                "source_blockers": transfer_source_blockers,
                "active_source_cash_actions": transfer_active_source_cash_actions,
                "property_cash_review_details": transfer_property_cash_review_details,
                "next_action": transfer_next_action,
            }
        )
    if discord_global_financial_issue_count:
        blockers.append(
            {
                "source": "discord_plan_validation",
                "status": discord_plan_status,
                "financial_review_issue_count": discord_global_financial_issue_count,
                "financial_review_issues": discord_financial_review_issues,
                "financial_review_artifacts": discord_financial_review_artifacts,
            }
        )
    if discord_plan_status not in {"", "ok", "ok_partial"} and (discord_non_financial_issues or discord_plan_validation_issue_count > discord_financial_issue_count):
        blockers.append(
            {
                "source": "discord_plan_validation_non_financial",
                "status": discord_plan_status,
                "issue_count": discord_plan_validation_issue_count,
                "issues": discord_non_financial_issues,
                "message_digest_issue_count": count(discord_plan_validation.get("message_digest_issue_count")),
                "missing_financial_summary_count": count(discord_plan_validation.get("missing_financial_summary_count")),
                "unmapped_count": count(discord_plan_validation.get("unmapped_count")),
                "stale_route_count": count(discord_plan_validation.get("stale_route_count")),
            }
        )

    return {
        "blocked": bool(blockers),
        "blocker_count": len(blockers),
        "blockers": blockers,
        "lofty_listing_financial_update_chain": lofty_listing_financial_update_chain,
        "lofty_listing_financial_update_chain_ok": lofty_listing_financial_update_chain_ok,
        "lofty_monthly_summary_issue_records": lofty_monthly_summary_issue_records,
        "excluded_lofty_monthly_summary_issue_record_count": len(excluded_lofty_monthly_summary_issue_records),
        "excluded_lofty_monthly_summary_issue_records": excluded_lofty_monthly_summary_issue_records,
        "monthly_run_status": monthly_run_status,
        "monthly_run_failed_step": monthly_run_failed_step,
        "monthly_run_next_action": monthly_run_next_action,
        "transfer_reconciliation_next_action": transfer_next_action,
        "readiness_financial_blockers": readiness_financial_blockers,
        "transfer_reconciliation_status": transfer_reconciliation.get("status"),
        "transfer_reconciliation_report_loaded": transfer_report_loaded,
        "transfer_reconciliation_recommended_send_to_lofty_total_is_final": transfer_reconciliation_final,
        "transfer_reconciliation_source_blockers": transfer_source_blockers,
        "transfer_reconciliation_active_source_cash_actions": transfer_active_source_cash_actions,
        "transfer_reconciliation_property_cash_review_details": transfer_property_cash_review_details,
        "discord_plan_validation_status": discord_plan_status,
        "discord_financial_review_issue_count": discord_financial_issue_count,
        "discord_global_financial_review_issue_count": discord_global_financial_issue_count,
        "discord_financial_review_issues": discord_financial_review_issues,
        "discord_financial_review_artifact_area_count": count(
            discord_plan_validation.get("financial_review_artifact_area_count")
        ),
        "discord_financial_review_missing_artifact_count": count(
            discord_plan_validation.get("financial_review_missing_artifact_count")
        ),
        "discord_financial_review_artifacts": discord_financial_review_artifacts,
        "discord_plan_validation_issue_count": discord_plan_validation_issue_count,
        "discord_plan_validation_non_financial_issue_count": discord_plan_non_financial_issue_count,
        "discord_review_ready": discord_review_ready,
        "discord_review_ready_but_financial_blocked": discord_review_ready_but_financial_blocked,
        "discord_review_policy": discord_plan_validation.get("discord_review_policy"),
        "discord_plan_validation_non_financial_issues": discord_non_financial_issues,
        "discord_plan_validation_message_digest_issue_count": count(discord_plan_validation.get("message_digest_issue_count")),
    }


def transfer_telegram_send_gate(
    report: dict[str, Any],
    *,
    current_transfer_report_digest: str | None = None,
) -> dict[str, Any]:
    blockers = [
        str(item)
        for item in (report.get("send_blockers") or [])
        if str(item or "").strip()
    ]
    status = str(report.get("status") or "not_checked")
    dry_run = report.get("dry_run") is True
    recorded_transfer_digest = str(report.get("transfer_report_digest") or "").strip() or None
    transfer_report_status = str(report.get("transfer_report_status") or "")
    transfer_report_final = report.get("transfer_report_recommended_send_to_lofty_total_is_final") is True
    transfer_report_review_count_fields_present = all(
        key in report
        for key in (
            "transfer_report_source_blocker_count",
            "transfer_report_property_cash_review_blocker_count",
            "transfer_report_property_cash_review_detail_count",
        )
    )
    transfer_report_review_but_sender_safe = bool(
        transfer_report_status == "review"
        and report.get("send_safe") is True
        and transfer_report_final
        and transfer_report_review_count_fields_present
        and count(report.get("transfer_report_source_blocker_count")) == 0
        and count(report.get("transfer_report_property_cash_review_blocker_count")) == 0
        and count(report.get("transfer_report_property_cash_review_detail_count")) == 0
    )
    transfer_report_status_ok = transfer_report_status == "ok" or transfer_report_review_but_sender_safe
    transfer_report_digest_matches_current = (
        recorded_transfer_digest == current_transfer_report_digest
        if current_transfer_report_digest
        else True
    )
    ok = bool(
        status in {"ok", "ok_previous"}
        and not dry_run
        and report.get("send_safe") is True
        and report.get("telegram_send_ok") is True
        and report.get("message_quality_ok") is True
        and transfer_report_status_ok
        and transfer_report_final
        and transfer_report_digest_matches_current
        and not blockers
    )
    preview_validated = bool(
        dry_run
        and status == "ok_dry_run"
        and report.get("message_quality_ok") is True
        and transfer_report_status_ok
        and transfer_report_final
        and transfer_report_digest_matches_current
        and not blockers
    )
    issues: list[str] = []
    if status not in {"ok", "ok_previous"}:
        issues.append(f"transfer_telegram_send_status={status}")
    if dry_run:
        issues.append("transfer_telegram_send_dry_run=true")
    if report.get("send_safe") is not True:
        issues.append("transfer_telegram_send_safe=false")
    if report.get("telegram_send_ok") is not True:
        issues.append("telegram_send_ok=false")
    if report.get("message_quality_ok") is not True:
        issues.append("telegram_message_quality_not_ok")
    if not transfer_report_status_ok:
        issues.append(f"transfer_report_status={report.get('transfer_report_status')}")
    if not transfer_report_final:
        issues.append("transfer_report_recommended_send_to_lofty_total_is_final=false")
    if not transfer_report_digest_matches_current:
        issues.append("transfer_report_digest_mismatch_current")
    issues.extend(blockers)
    deduped_issues = list(dict.fromkeys(issues))
    return {
        "ok": ok,
        "status": status,
        "dry_run": dry_run,
        "send_safe": report.get("send_safe") is True,
        "telegram_send_ok": report.get("telegram_send_ok") is True,
        "message_quality_ok": report.get("message_quality_ok") is True,
        "transfer_report_status": report.get("transfer_report_status"),
        "transfer_report_status_ok": transfer_report_status_ok,
        "transfer_report_review_but_sender_safe": transfer_report_review_but_sender_safe,
        "transfer_report_review_count_fields_present": transfer_report_review_count_fields_present,
        "transfer_report_recommended_send_to_lofty_total_is_final": transfer_report_final,
        "transfer_report_digest": recorded_transfer_digest,
        "current_transfer_report_digest": current_transfer_report_digest,
        "transfer_report_digest_matches_current": transfer_report_digest_matches_current,
        "current_for_transfer": transfer_report_digest_matches_current,
        "preview_validated": preview_validated,
        "send_blockers": blockers,
        "issues": deduped_issues,
    }


def send_lock_safe(status: object) -> bool:
    return str(status or "") in {
        "not_requested",
        "written",
        "cleared_after_sent_state",
        "cleared_no_sends_needed",
        "blocked_existing_lock",
    }


def send_lock_safe_to_send(status: object) -> bool:
    return str(status or "") in {
        "not_requested",
        "written",
        "cleared_after_sent_state",
        "cleared_no_sends_needed",
    }


def read_state_file(path_value: str) -> tuple[bool, str, str]:
    if not path_value:
        return False, "", ""
    path = Path(path_value)
    if not path.is_file():
        return False, "", ""
    try:
        return True, path.read_text(encoding="utf-8").strip(), ""
    except Exception as exc:  # noqa: BLE001
        return True, "", str(exc)


def resolve_runtime_path(root: Path, path_value: str) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path
    return root / path


def default_yhome_transition_reconciliation_csv(root: Path) -> Path | None:
    for candidate in DEFAULT_YHOME_TRANSITION_RECONCILIATION_CANDIDATES:
        path = candidate if candidate.is_absolute() else root / candidate
        if path.is_file():
            return path
    return None


def yhome_transition_guard_from_csv(path: Path | None) -> dict[str, Any]:
    _excluded, guard = load_yhome_transition_exclusions(path)
    guard = dict(guard)
    if path is not None:
        guard.setdefault("path", str(path))
    guard["source"] = "yhome_transition_reconciliation_csv"
    return guard


def active_property_guard_proof(
    publish: dict[str, Any],
    yhome_transition_csv: Path | None = None,
) -> dict[str, Any]:
    policy = str(publish.get("active_property_only_policy") or "")
    raw_manual_excluded = [str(item or "") for item in publish.get("manual_excluded_property_names") or []]
    manual_excluded = [
        item
        for item in raw_manual_excluded
        if any(required.lower() in item.lower() for required in REQUIRED_MANUAL_EXCLUDED_PROPERTIES)
    ]
    yhome_guard = publish.get("yhome_transition_guard") if isinstance(publish.get("yhome_transition_guard"), dict) else {}
    if (
        yhome_guard.get("status") != "ok"
        or count(yhome_guard.get("excluded_count")) <= 0
        or yhome_guard.get("column_b_rule_ok") is not True
    ) and yhome_transition_csv is not None:
        yhome_guard = yhome_transition_guard_from_csv(yhome_transition_csv)
    missing_manual_exclusions = [
        name
        for name in REQUIRED_MANUAL_EXCLUDED_PROPERTIES
        if not any(name.lower() in item.lower() for item in manual_excluded)
    ]
    policy_lower = policy.lower()
    proof = {
        "policy": policy,
        "policy_mentions_yhome": "yhome" in policy_lower,
        "policy_mentions_manual_exclusions": "manual" in policy_lower and "exclusion" in policy_lower,
        "manual_excluded_property_names": manual_excluded,
        "manual_required_property_names": list(REQUIRED_MANUAL_EXCLUDED_PROPERTIES),
        "manual_missing_property_names": missing_manual_exclusions,
        "manual_exclusions_ok": not missing_manual_exclusions,
        "yhome_transition_guard_status": yhome_guard.get("status"),
        "yhome_transition_guard_excluded_count": count(yhome_guard.get("excluded_count")),
        "yhome_transition_guard_column_b_header": yhome_guard.get("column_b_header"),
        "yhome_transition_guard_column_b_rule": yhome_guard.get("column_b_rule"),
        "yhome_transition_guard_column_b_marker_count": count(yhome_guard.get("column_b_marker_count")),
        "yhome_transition_guard_column_b_rule_ok": yhome_guard.get("column_b_rule_ok") is True,
        "yhome_transition_guard_ok": (
            yhome_guard.get("status") == "ok"
            and count(yhome_guard.get("excluded_count")) > 0
            and yhome_guard.get("column_b_rule_ok") is True
        ),
    }
    proof["ok"] = (
        proof["policy_mentions_yhome"]
        and proof["policy_mentions_manual_exclusions"]
        and proof["manual_exclusions_ok"]
        and proof["yhome_transition_guard_ok"]
        and proof["yhome_transition_guard_column_b_rule_ok"]
    )
    return proof


def refresh_blocked_publish_readiness_snapshot(
    publish_path: Path,
    publish: dict[str, Any],
    readiness: dict[str, Any],
    readiness_path: Path,
) -> dict[str, Any]:
    if publish.get("status") in {"missing", "unreadable"}:
        return {"status": "skipped", "reason": f"publish_report_{publish.get('status')}"}
    if readiness.get("status") in {"missing", "unreadable"}:
        return {"status": "skipped", "reason": f"readiness_report_{readiness.get('status')}"}
    if (
        readiness.get("status") == "ok"
        and readiness.get("owner_email_allowed") is True
        and publish.get("effective_send_owner_emails") is not True
    ):
        current_idempotency = publish.get("owner_email_idempotency") if isinstance(publish.get("owner_email_idempotency"), dict) else {}
        current_decision = publish.get("owner_email_send_decision") if isinstance(publish.get("owner_email_send_decision"), dict) else {}
        stale_reason_values = (
            str(publish.get("send_blocked_reason") or ""),
            str(current_idempotency.get("send_blocked_reason") or ""),
            str(current_decision.get("blocked_reason") or ""),
        )
        if not any(stale_monthly_readiness_blocked_reason(reason, readiness) for reason in stale_reason_values):
            return {"status": "skipped", "reason": "readiness_not_blocked"}

        snapshot = readiness_snapshot(readiness, readiness_path)
        updated = dict(publish)
        updated["monthly_readiness_snapshot"] = snapshot
        updated["monthly_readiness_blocked_reason"] = ""
        updated["monthly_readiness_blocked_reason_matches"] = True
        updated["send_blocked_reason"] = ""
        updated["effective_send_owner_emails"] = False
        updated["readiness_snapshot_refreshed_by_guard"] = True
        updated["readiness_snapshot_refreshed_at"] = iso_z()

        send_decision_inputs = updated.get("send_decision_inputs")
        if isinstance(send_decision_inputs, dict):
            send_decision_inputs = dict(send_decision_inputs)
            send_decision_inputs["readiness_digest"] = snapshot["digest"]
            send_decision_inputs["readiness_owner_email_allowed"] = True
            send_decision_inputs["send_blocked_reason"] = ""
            updated["send_decision_inputs"] = send_decision_inputs
            digest = stable_digest(send_decision_inputs)
            updated["send_decision_digest"] = digest
        else:
            digest = str(updated.get("send_decision_digest") or "")

        idempotency = dict(current_idempotency)
        idempotency["send_blocked_reason"] = ""
        idempotency["safe_to_send_now"] = False
        if sha256ish(digest):
            idempotency["send_decision_digest"] = digest
        updated["owner_email_idempotency"] = idempotency

        decision = dict(current_decision)
        decision["blocked_reason"] = ""
        decision["effective"] = False
        decision["safe_to_send_now"] = False
        if sha256ish(digest):
            decision["send_decision_digest"] = digest
        updated["owner_email_send_decision"] = decision

        write_json(publish_path, updated)
        publish.clear()
        publish.update(updated)
        return {
            "status": "updated",
            "reason": "green_readiness_snapshot_refreshed",
            "blocker_count": snapshot.get("blocker_count"),
            "digest": snapshot.get("digest"),
        }
    if readiness.get("owner_email_allowed") is not False:
        return {"status": "skipped", "reason": "readiness_not_blocked"}
    if publish.get("effective_send_owner_emails") is True:
        return {"status": "skipped", "reason": "publish_report_effective_send_true"}

    snapshot = readiness_snapshot(readiness, readiness_path)
    blocked_reason = monthly_readiness_blocked_reason(readiness)
    current_snapshot = publish.get("monthly_readiness_snapshot") if isinstance(publish.get("monthly_readiness_snapshot"), dict) else {}
    current_reason = str(publish.get("send_blocked_reason") or "")
    current_idempotency = publish.get("owner_email_idempotency") if isinstance(publish.get("owner_email_idempotency"), dict) else {}
    current_decision = publish.get("owner_email_send_decision") if isinstance(publish.get("owner_email_send_decision"), dict) else {}
    needs_update = (
        current_snapshot.get("digest") != snapshot["digest"]
        or current_snapshot.get("source_generated_at") != snapshot.get("source_generated_at")
        or current_snapshot.get("source_mtime") != snapshot.get("source_mtime")
        or current_reason != blocked_reason
        or current_idempotency.get("send_blocked_reason") != blocked_reason
        or current_decision.get("blocked_reason") != blocked_reason
    )
    if not needs_update:
        return {"status": "unchanged", "reason": "publish_readiness_snapshot_current"}

    updated = dict(publish)
    updated["monthly_readiness_snapshot"] = snapshot
    updated["monthly_readiness_blocked_reason"] = blocked_reason
    updated["monthly_readiness_blocked_reason_matches"] = True
    updated["send_blocked_reason"] = blocked_reason
    updated["effective_send_owner_emails"] = False
    updated["readiness_snapshot_refreshed_by_guard"] = True
    updated["readiness_snapshot_refreshed_at"] = iso_z()

    send_decision_inputs = updated.get("send_decision_inputs")
    if isinstance(send_decision_inputs, dict):
        send_decision_inputs = dict(send_decision_inputs)
        send_decision_inputs["readiness_digest"] = snapshot["digest"]
        send_decision_inputs["readiness_owner_email_allowed"] = False
        updated["send_decision_inputs"] = send_decision_inputs
        digest = stable_digest(send_decision_inputs)
        updated["send_decision_digest"] = digest
    else:
        digest = str(updated.get("send_decision_digest") or "")

    idempotency = dict(current_idempotency)
    idempotency["send_blocked_reason"] = blocked_reason
    idempotency["safe_to_send_now"] = False
    if sha256ish(digest):
        idempotency["send_decision_digest"] = digest
    updated["owner_email_idempotency"] = idempotency

    decision = dict(current_decision)
    decision["blocked_reason"] = blocked_reason
    decision["effective"] = False
    decision["safe_to_send_now"] = False
    if sha256ish(digest):
        decision["send_decision_digest"] = digest
    updated["owner_email_send_decision"] = decision

    write_json(publish_path, updated)
    publish.clear()
    publish.update(updated)
    return {
        "status": "updated",
        "reason": "blocked_readiness_snapshot_refreshed",
        "blocker_count": snapshot.get("blocker_count"),
        "digest": snapshot.get("digest"),
    }


def build_report(
    root: Path,
    readiness_path: Path,
    publish_path: Path,
    monthly_run_path: Path,
    owner_email_packet_path: Path | None = None,
    discord_send_path: Path | None = None,
    transfer_reconciliation_path: Path | None = None,
    transfer_telegram_send_path: Path | None = None,
    discord_plan_validation_path: Path | None = None,
    guild_test_post_path: Path | None = None,
    yhome_transition_csv: Path | None = None,
) -> dict[str, Any]:
    if (
        discord_plan_validation_path is None
        and transfer_telegram_send_path is not None
        and str(transfer_telegram_send_path).endswith("discord-validation.json")
    ):
        discord_plan_validation_path = transfer_telegram_send_path
        transfer_telegram_send_path = None
    readiness = read_json(readiness_path)
    publish = read_json(publish_path)
    publish_readiness_snapshot_refresh = refresh_blocked_publish_readiness_snapshot(
        publish_path,
        publish,
        readiness,
        readiness_path,
    )
    monthly_run = read_json(monthly_run_path)
    owner_email_packet = read_json(owner_email_packet_path) if owner_email_packet_path else {"status": "not_checked"}
    disk_space_preflight = current_monthly_disk_space_preflight_report(root, monthly_run)
    owner_email_packet_for_primary_blocker = sanitize_owner_email_packet_primary_blocker(
        owner_email_packet,
        disk_space_preflight,
    )
    discord_send = read_json(discord_send_path) if discord_send_path else {"status": "not_checked"}
    transfer_reconciliation = read_json(transfer_reconciliation_path) if transfer_reconciliation_path else {"status": "not_checked"}
    transfer_telegram_send = read_json(transfer_telegram_send_path) if transfer_telegram_send_path else {"status": "not_checked"}
    discord_plan_validation = read_json(discord_plan_validation_path) if discord_plan_validation_path else {"status": "not_checked"}
    standalone_guild_test_post = read_json(guild_test_post_path) if guild_test_post_path else {"status": "not_checked"}
    posted_guild_test_post_path = None
    posted_guild_test_post = {"status": "not_checked"}
    if guild_test_post_path:
        candidate_posted_path = guild_test_post_path.with_name(f"{guild_test_post_path.stem}.posted.json")
        if candidate_posted_path.is_file():
            posted_guild_test_post_path = candidate_posted_path
            posted_guild_test_post = read_json(candidate_posted_path)
    financial_review = financial_review_gate(
        readiness,
        transfer_reconciliation,
        discord_plan_validation,
        monthly_run,
        publish,
    )
    transfer_telegram_gate = transfer_telegram_send_gate(
        transfer_telegram_send,
        current_transfer_report_digest=stable_transfer_report_digest(transfer_reconciliation_path),
    )
    idempotency = publish.get("owner_email_idempotency") if isinstance(publish.get("owner_email_idempotency"), dict) else {}
    decision = publish.get("owner_email_send_decision") if isinstance(publish.get("owner_email_send_decision"), dict) else {}
    send_decision_inputs = publish.get("send_decision_inputs") if isinstance(publish.get("send_decision_inputs"), dict) else {}
    run_month = str(publish.get("run_month") or monthly_run.get("run_month") or "")
    monthly_finance_truth_refresh_issue_summary = str(
        monthly_run.get("monthly_finance_truth_refresh_issue_summary") or ""
    ).strip()
    monthly_finance_truth_refresh_next_action = str(
        monthly_run.get("monthly_finance_truth_refresh_next_action")
        or monthly_run.get("next_action")
        or ""
    ).strip()
    baselane_login_wait_reason = str(monthly_run.get("baselane_login_wait_reason") or "").strip()
    baselane_login_wait_recaptcha_present = monthly_run.get("baselane_login_wait_recaptcha_present") is True
    baselane_login_wait_current_url = str(monthly_run.get("baselane_login_wait_current_url") or "").strip()
    publish_guild_test_post = (
        publish.get("guild_test_post_snapshot") if isinstance(publish.get("guild_test_post_snapshot"), dict) else {}
    )
    standalone_guild_test_post_valid = guild_test_post_snapshot_valid(standalone_guild_test_post, run_month)
    posted_guild_test_post_valid = guild_test_post_snapshot_valid(posted_guild_test_post, run_month)
    if posted_guild_test_post_valid:
        guild_test_post = posted_guild_test_post
        guild_test_post_source = "standalone_guild_test_post_posted_proof"
    elif standalone_guild_test_post_valid:
        guild_test_post = standalone_guild_test_post
        guild_test_post_source = "standalone_guild_test_post_report"
    else:
        guild_test_post = publish_guild_test_post
        guild_test_post_source = "publish_report_snapshot"
    active_property_proof = active_property_guard_proof(
        publish,
        yhome_transition_csv=yhome_transition_csv or default_yhome_transition_reconciliation_csv(root),
    )

    send_requested = publish.get("send_owner_emails") is True or decision.get("requested") is True
    effective_send = publish.get("effective_send_owner_emails") is True or decision.get("effective") is True
    will_send_count = count(publish.get("owner_email_will_send_count") or decision.get("will_send_count"))
    evidence_count = count(publish.get("owner_email_send_evidence_count") or decision.get("send_evidence_count"))
    evidence_issue_count = count(publish.get("owner_email_send_evidence_issue_count") or decision.get("send_evidence_issue_count"))
    excluded_property_count = count(publish.get("excluded_property_count"))
    excluded_payload_file_count = count(publish.get("excluded_payload_file_count"))
    excluded_owner_email_candidate_count = count(publish.get("excluded_owner_email_candidate_count"))
    send_interval_days = publish.get("send_interval_days")
    send_interval_ok = isinstance(send_interval_days, int) and send_interval_days >= REQUIRED_PROPERTY_EMAIL_COOLDOWN_DAYS
    sent_state_file = str(
        monthly_run.get("owner_email_sent_state_file")
        or publish.get("sent_state_file")
        or idempotency.get("sent_state_file")
        or ""
    )
    sent_state_path = resolve_runtime_path(root, sent_state_file)
    sent_state_file_exists, sent_state_file_month, sent_state_file_error = read_state_file(str(sent_state_path or ""))
    sent_state_month = str(
        monthly_run.get("owner_email_sent_state_month")
        or publish.get("sent_state_month")
        or idempotency.get("sent_state_month")
        or ""
    )
    reported_sent_state_matches_run_month = bool(run_month and sent_state_month == run_month)
    sent_state_file_matches_run_month = bool(run_month and sent_state_file_month == run_month)
    sent_state_matches_run_month = reported_sent_state_matches_run_month or sent_state_file_matches_run_month
    already_sent_this_month = sent_state_matches_run_month
    send_lock_status = str(
        monthly_run.get("owner_email_publish_send_lock_status")
        or publish.get("send_lock_status")
        or idempotency.get("send_lock_status")
        or decision.get("send_lock_status")
        or ""
    )
    send_lock_file = str(
        monthly_run.get("owner_email_publish_send_lock_file")
        or publish.get("send_lock_file")
        or idempotency.get("send_lock_file")
        or decision.get("send_lock_file")
        or ""
    )
    send_lock_path = resolve_runtime_path(root, send_lock_file)
    send_lock_file_payload = read_json(send_lock_path) if send_lock_path else {"status": "not_configured"}
    send_lock_file_status = str(send_lock_file_payload.get("status") or "")
    send_lock_file_unreadable = bool(send_lock_file and send_lock_file_status == "unreadable")
    send_lock_file_loaded = bool(send_lock_file and send_lock_file_status not in {"missing", "unreadable"})
    if send_lock_file_unreadable:
        send_lock_status = "blocked_unreadable_lock"
    send_lock_run_month = str(send_lock_file_payload.get("run_month") or "")
    send_lock_run_month_matches = bool(not send_lock_file_loaded or not run_month or send_lock_run_month == run_month)
    send_lock_manual_review_required = send_lock_file_payload.get("manual_review_required") is True
    send_lock_safe_retry_without_duplicate = send_lock_file_payload.get("safe_retry_without_duplicate_owner_email") is True
    send_lock_owner_email_send_attempted = send_lock_file_payload.get("owner_email_send_attempted") is True
    send_lock_owner_email_send_proven_complete = send_lock_file_payload.get("owner_email_send_proven_complete") is True
    send_lock_owner_email_send_intended_count = count(send_lock_file_payload.get("owner_email_send_intended_count"))
    send_lock_manual_review_reason = str(send_lock_file_payload.get("manual_review_reason") or "")
    existing_send_lock_decision_digest = str(
        publish.get("existing_send_lock_decision_digest")
        or idempotency.get("existing_send_lock_decision_digest")
        or decision.get("existing_send_lock_decision_digest")
        or ""
    )
    existing_send_lock_matches_send_decision = (
        publish.get("existing_send_lock_matches_send_decision") is True
        or idempotency.get("existing_send_lock_matches_send_decision") is True
        or decision.get("existing_send_lock_matches_send_decision") is True
    )
    blocked_existing_lock = send_lock_status == "blocked_existing_lock"
    existing_send_lock_digest_safe = bool(
        not blocked_existing_lock
        or (
            sha256ish(existing_send_lock_decision_digest)
            and existing_send_lock_matches_send_decision
        )
    )
    sent_state_write_status = str(publish.get("sent_state_write_status") or idempotency.get("sent_state_write_status") or decision.get("sent_state_write_status") or "")
    blocked_reason = str(publish.get("send_blocked_reason") or idempotency.get("send_blocked_reason") or decision.get("blocked_reason") or "")
    send_decision_digest = str(publish.get("send_decision_digest") or "")
    send_decision_inputs_present = bool(send_decision_inputs)
    send_decision_inputs_digest = stable_digest(send_decision_inputs) if send_decision_inputs_present else ""
    send_decision_digest_matches_inputs = bool(
        not send_decision_inputs_present
        or (
            sha256ish(send_decision_digest)
            and send_decision_digest == send_decision_inputs_digest
        )
    )
    guild_test_post_required = not (
        publish.get("guild_test_post_required_before_email") is False
        and send_decision_inputs.get("guild_test_post_required") is False
        and not send_requested
        and not effective_send
    )
    guild_test_post_valid = guild_test_post_snapshot_valid(guild_test_post, run_month)
    guild_test_post_digest = str(guild_test_post.get("digest") or "")
    guild_test_post_digest_ok = sha256ish(guild_test_post_digest)
    send_decision_guild_test_post_digest = str(send_decision_inputs.get("guild_test_post_digest") or "")
    guild_test_post_digest_matches_decision_inputs = bool(
        not send_decision_inputs_present
        or not guild_test_post_required
        or (sha256ish(send_decision_guild_test_post_digest) and send_decision_guild_test_post_digest == guild_test_post_digest)
    )
    guild_test_post_posted_at_month_matches = close_cycle_month_matches(guild_test_post.get("posted_at"), run_month)
    guild_test_post_lofty_guild_ok_value = guild_test_post_lofty_guild_ok(guild_test_post)
    guild_test_post_route_proof_ok = guild_test_post_route_ok(guild_test_post)
    guild_test_post_handoff_summary = guild_test_post_handoff(guild_test_post, run_month)
    discord_all_property_guard = discord_all_property_send_guard(discord_send, monthly_run, discord_plan_validation)
    digest_ok = (
        sha256ish(send_decision_digest)
        and send_decision_digest == str(idempotency.get("send_decision_digest") or "")
        and send_decision_digest == str(decision.get("send_decision_digest") or "")
        and send_decision_digest_matches_inputs
    )
    readiness_ok = readiness.get("status") == "ok" and readiness.get("owner_email_allowed") is True
    if stale_monthly_readiness_blocked_reason(blocked_reason, readiness):
        blocked_reason = ""
    publish_loaded = publish.get("status") not in {"missing", "unreadable"}
    publish_fresh = required_fresh_generated_at(publish)
    idempotency_configured = idempotency.get("configured") is True and bool(sent_state_file)
    owner_email_packet_required = owner_email_packet_path is not None
    owner_email_packet_loaded = owner_email_packet.get("status") not in {"missing", "unreadable"}
    owner_email_packet_fresh = (
        not owner_email_packet_required
        or required_fresh_generated_at(owner_email_packet, OWNER_EMAIL_PACKET_MAX_AGE_HOURS)
    )
    owner_email_packet_status = str(owner_email_packet.get("status") or "")
    owner_email_packet_issue_count = count(owner_email_packet.get("issue_count"))
    owner_email_packet_run_month = str(owner_email_packet.get("run_month") or "")
    owner_email_packet_run_month_matches = bool(
        not owner_email_packet_run_month or not run_month or owner_email_packet_run_month == run_month
    )
    owner_email_packet_full_history_leak_count = count(owner_email_packet.get("full_history_leak_count"))
    owner_email_packet_full_history_guard_issue_count = count(owner_email_packet.get("full_history_guard_issue_count"))
    owner_email_packet_body_guard_issue_count = count(owner_email_packet.get("body_guard_issue_count"))
    owner_email_packet_unsafe_preview_packet_count = count(owner_email_packet.get("unsafe_preview_packet_count"))
    owner_email_packet_safe_to_send_now = owner_email_packet.get("safe_to_send_now") is True
    owner_email_packet_property_count = count(owner_email_packet.get("property_count"))
    owner_email_packet_available_property_count = count(owner_email_packet.get("available_property_count"))
    owner_email_packet_property_unavailable_count = count(owner_email_packet.get("property_unavailable_count"))
    owner_email_packet_packet_count = count(owner_email_packet.get("packet_count"))
    owner_email_packet_native_property_count = count(owner_email_packet.get("native_lofty_owner_email_property_count"))
    owner_email_packet_native_eligible_property_count = count(
        owner_email_packet.get("native_lofty_owner_email_eligible_property_count")
    )
    if "native_lofty_owner_email_eligible_property_count" not in owner_email_packet:
        owner_email_packet_native_eligible_property_count = owner_email_packet_native_property_count
    owner_email_packet_native_cooldown_held_property_count = count(
        owner_email_packet.get("native_lofty_owner_email_cooldown_held_property_count")
    )
    owner_email_packet_native_idempotency_held_property_count = count(
        owner_email_packet.get("native_lofty_owner_email_idempotency_held_property_count")
    )
    if "native_lofty_owner_email_idempotency_held_property_count" not in owner_email_packet:
        owner_email_packet_native_idempotency_held_property_count = owner_email_packet_native_cooldown_held_property_count
    owner_email_packet_native_property_coverage_ok = owner_email_packet.get("native_lofty_owner_email_property_coverage_ok") is True
    owner_email_packet_native_allowed = owner_email_packet.get("native_lofty_owner_email_allowed") is True
    owner_email_packet_monthly_financial_summary_missing_count = count(
        owner_email_packet.get("monthly_financial_summary_missing_property_count")
    )
    owner_email_packet_monthly_financial_summary_present_count = count(
        owner_email_packet.get("monthly_financial_summary_present_total_property_count")
    )
    owner_email_packet_property_cooldown_days = count(owner_email_packet.get("property_email_cooldown_days"))
    owner_email_packet_property_cooldown_ok = owner_email_packet.get("property_email_cooldown_ok") is True
    owner_email_packet_property_cooldown_issue_count = count(owner_email_packet.get("property_email_cooldown_issue_count"))
    owner_email_packet_discord_plan_digest = str(owner_email_packet.get("discord_all_property_send_plan_digest") or "")
    owner_email_packet_discord_plan_digest_ok = sha256ish(owner_email_packet_discord_plan_digest)
    owner_email_packet_discord_plan_digest_matches_send = bool(
        not owner_email_packet_required
        or (
            owner_email_packet_discord_plan_digest_ok
            and owner_email_packet_discord_plan_digest == str(discord_all_property_guard["plan_digest"] or "")
        )
    )
    owner_email_packet_email_body_max_chars = count(owner_email_packet.get("email_body_max_chars"))
    owner_email_packet_email_body_max_lines = count(owner_email_packet.get("email_body_max_lines"))
    owner_email_packet_property_update_max_chars = count(owner_email_packet.get("property_update_max_chars"))
    owner_email_packet_property_update_max_lines = count(owner_email_packet.get("property_update_max_lines"))
    owner_email_packet_signal_only_ok = bool(
        owner_email_packet_email_body_max_chars
        and owner_email_packet_email_body_max_chars <= MAX_SIGNAL_ONLY_EMAIL_BODY_CHARS
        and owner_email_packet_email_body_max_lines
        and owner_email_packet_email_body_max_lines <= MAX_SIGNAL_ONLY_EMAIL_BODY_LINES
        and owner_email_packet_property_update_max_chars
        and owner_email_packet_property_update_max_chars <= MAX_SIGNAL_ONLY_PROPERTY_UPDATE_CHARS
        and owner_email_packet_property_update_max_lines
        and owner_email_packet_property_update_max_lines <= MAX_SIGNAL_ONLY_PROPERTY_UPDATE_LINES
    )
    owner_email_packet_legacy_packet_ok = bool(
        owner_email_packet_safe_to_send_now
        and owner_email_packet_packet_count > 0
    )
    owner_email_packet_mcp_native_packet_ok = bool(
        owner_email_packet_native_allowed
        and owner_email_packet_native_property_count > 0
        and owner_email_packet_native_eligible_property_count == owner_email_packet_native_property_count
        and owner_email_packet_native_property_count + owner_email_packet_native_idempotency_held_property_count
        == owner_email_packet_available_property_count
        and owner_email_packet_native_property_coverage_ok
    )
    owner_email_packet_property_cooldown_gate_ok = bool(
        (
            owner_email_packet_property_cooldown_ok
            and owner_email_packet_property_cooldown_issue_count == 0
        )
        or owner_email_packet_mcp_native_packet_ok
    )
    owner_email_packet_ok_for_send = bool(
        not owner_email_packet_required
        or (
            owner_email_packet_loaded
            and owner_email_packet_fresh
            and owner_email_packet_status == "ok"
            and owner_email_packet_issue_count == 0
            and owner_email_packet_property_count > 0
            and owner_email_packet_available_property_count == owner_email_packet_property_count
            and owner_email_packet_property_unavailable_count == 0
            and (owner_email_packet_legacy_packet_ok or owner_email_packet_mcp_native_packet_ok)
            and owner_email_packet_monthly_financial_summary_missing_count == 0
            and owner_email_packet_monthly_financial_summary_present_count == owner_email_packet_property_count
            and owner_email_packet_run_month_matches
            and owner_email_packet_full_history_leak_count == 0
            and owner_email_packet_full_history_guard_issue_count == 0
            and owner_email_packet_body_guard_issue_count == 0
            and owner_email_packet_unsafe_preview_packet_count == 0
            and owner_email_packet_property_cooldown_days == REQUIRED_PROPERTY_EMAIL_COOLDOWN_DAYS
            and owner_email_packet_property_cooldown_gate_ok
            and owner_email_packet_discord_plan_digest_matches_send
            and owner_email_packet_signal_only_ok
        )
    )
    owner_email_discord_review_chain = {
        "status": "ok"
        if (
            discord_all_property_guard["ok"]
            and owner_email_packet_discord_plan_digest_matches_send
            and owner_email_packet_ok_for_send
        )
        else "review",
        "policy": "Owner email is final and requires live Discord all-property posts/review proof with matching plan digest; dry-run only prepares human review and never unlocks email.",
        "discord_all_property_send_ok": discord_all_property_guard["ok"],
        "discord_all_property_send_dry_run": discord_all_property_guard["dry_run"],
        "discord_all_property_dry_run_verified": discord_all_property_guard["dry_run_verified"],
        "discord_all_property_live_post_ok": discord_all_property_guard["live_post_ok"],
        "discord_all_property_send_plan_digest": discord_all_property_guard["plan_digest"],
        "discord_all_property_send_plan_digest_ok": discord_all_property_guard["plan_digest_ok"],
        "owner_email_packet_discord_plan_digest": owner_email_packet_discord_plan_digest,
        "owner_email_packet_discord_plan_digest_ok": owner_email_packet_discord_plan_digest_ok,
        "owner_email_packet_discord_plan_digest_matches_send": owner_email_packet_discord_plan_digest_matches_send,
        "owner_email_packet_ok_for_send": owner_email_packet_ok_for_send,
        "owner_email_packet_legacy_packet_ok": owner_email_packet_legacy_packet_ok,
        "owner_email_packet_mcp_native_packet_ok": owner_email_packet_mcp_native_packet_ok,
    }

    issues: list[str] = []
    if not publish_loaded:
        issues.append(f"publish_report_{publish.get('status')}")
    if send_requested and not send_interval_ok:
        issues.append(f"send_interval_days={send_interval_days}")
    if send_requested and not idempotency_configured:
        issues.append("owner_email_idempotency_not_configured")
    if send_requested and not digest_ok:
        issues.append("send_decision_digest_missing_or_mismatch")
    if effective_send and not readiness_ok:
        issues.append("effective_send_without_clean_readiness")
    if effective_send and not publish_fresh:
        issues.append("publish_report_stale_or_missing_generated_at")
    if effective_send and owner_email_packet_required and not owner_email_packet_loaded:
        issues.append(f"owner_email_packet_report_{owner_email_packet.get('status')}")
    if effective_send and owner_email_packet_required and owner_email_packet_loaded and not owner_email_packet_fresh:
        issues.append("owner_email_packet_stale_or_missing_generated_at")
    if effective_send and owner_email_packet_required and owner_email_packet_loaded and owner_email_packet_status != "ok":
        issues.append(f"owner_email_packet_status={owner_email_packet_status}")
    if (
        effective_send
        and owner_email_packet_required
        and owner_email_packet_loaded
        and not owner_email_packet_safe_to_send_now
        and not owner_email_packet_mcp_native_packet_ok
    ):
        issues.append("owner_email_packet_safe_to_send_now=false")
    if effective_send and owner_email_packet_required and owner_email_packet_property_count <= 0:
        issues.append("owner_email_packet_property_count=0")
    if (
        effective_send
        and owner_email_packet_required
        and owner_email_packet_available_property_count != owner_email_packet_property_count
    ):
        issues.append(
            f"owner_email_packet_available_property_count={owner_email_packet_available_property_count}/{owner_email_packet_property_count}"
        )
    if effective_send and owner_email_packet_required and owner_email_packet_property_unavailable_count:
        issues.append(f"owner_email_packet_property_unavailable_count={owner_email_packet_property_unavailable_count}")
    if (
        effective_send
        and owner_email_packet_required
        and owner_email_packet_packet_count <= 0
        and not owner_email_packet_mcp_native_packet_ok
    ):
        issues.append("owner_email_packet_packet_count=0")
    if (
        effective_send
        and owner_email_packet_required
        and owner_email_packet_native_property_count != owner_email_packet_available_property_count
        and not owner_email_packet_mcp_native_packet_ok
    ):
        issues.append(
            f"owner_email_packet_native_property_count={owner_email_packet_native_property_count}/{owner_email_packet_available_property_count}"
        )
    if effective_send and owner_email_packet_required and not owner_email_packet_native_property_coverage_ok:
        issues.append("owner_email_packet_native_property_coverage_ok=false")
    if (
        effective_send
        and owner_email_packet_required
        and not owner_email_packet_legacy_packet_ok
        and not owner_email_packet_mcp_native_packet_ok
    ):
        issues.append("owner_email_packet_no_signal_only_send_path_ready")
    if effective_send and owner_email_packet_required and owner_email_packet_monthly_financial_summary_missing_count:
        issues.append(
            f"owner_email_packet_monthly_financial_summary_missing_property_count={owner_email_packet_monthly_financial_summary_missing_count}"
        )
    if (
        effective_send
        and owner_email_packet_required
        and owner_email_packet_monthly_financial_summary_present_count != owner_email_packet_property_count
    ):
        issues.append(
            "owner_email_packet_monthly_financial_summary_present_total_property_count="
            f"{owner_email_packet_monthly_financial_summary_present_count}/{owner_email_packet_property_count}"
        )
    if effective_send and owner_email_packet_required and not owner_email_packet_run_month_matches:
        issues.append(f"owner_email_packet_run_month_mismatch:{owner_email_packet_run_month or 'missing'}!={run_month}")
    if effective_send and owner_email_packet_required and owner_email_packet_issue_count:
        issues.append(f"owner_email_packet_issue_count={owner_email_packet_issue_count}")
    if effective_send and owner_email_packet_required and owner_email_packet_full_history_leak_count:
        issues.append(f"owner_email_packet_full_history_leak_count={owner_email_packet_full_history_leak_count}")
    if effective_send and owner_email_packet_required and owner_email_packet_full_history_guard_issue_count:
        issues.append(f"owner_email_packet_full_history_guard_issue_count={owner_email_packet_full_history_guard_issue_count}")
    if effective_send and owner_email_packet_required and owner_email_packet_body_guard_issue_count:
        issues.append(f"owner_email_packet_body_guard_issue_count={owner_email_packet_body_guard_issue_count}")
    if effective_send and owner_email_packet_required and owner_email_packet_unsafe_preview_packet_count:
        issues.append(f"owner_email_packet_unsafe_preview_packet_count={owner_email_packet_unsafe_preview_packet_count}")
    if effective_send and owner_email_packet_required and owner_email_packet_property_cooldown_days != REQUIRED_PROPERTY_EMAIL_COOLDOWN_DAYS:
        issues.append(f"owner_email_packet_property_cooldown_days={owner_email_packet_property_cooldown_days}")
    if effective_send and owner_email_packet_required and not owner_email_packet_property_cooldown_gate_ok:
        issues.append("owner_email_packet_property_cooldown_ok=false")
    if (
        effective_send
        and owner_email_packet_required
        and owner_email_packet_property_cooldown_issue_count
        and not owner_email_packet_mcp_native_packet_ok
    ):
        issues.append(f"owner_email_packet_property_cooldown_issue_count={owner_email_packet_property_cooldown_issue_count}")
    if effective_send and owner_email_packet_required and not owner_email_packet_discord_plan_digest_matches_send:
        issues.append("owner_email_packet_discord_plan_digest_missing_or_mismatch")
    if effective_send and owner_email_packet_required and not owner_email_packet_signal_only_ok:
        issues.append("owner_email_packet_signal_only_limits_not_ok")
    if effective_send and not active_property_proof["policy_mentions_yhome"]:
        issues.append("active_property_policy_missing_yhome")
    if effective_send and not active_property_proof["policy_mentions_manual_exclusions"]:
        issues.append("active_property_policy_missing_manual_exclusions")
    if effective_send and not active_property_proof["manual_exclusions_ok"]:
        issues.append(
            "manual_excluded_properties_missing="
            + ",".join(active_property_proof["manual_missing_property_names"])
        )
    if effective_send and not active_property_proof["yhome_transition_guard_ok"]:
        issues.append(f"yhome_transition_guard_not_ok={active_property_proof['yhome_transition_guard_status']}")
    if effective_send and not active_property_proof["yhome_transition_guard_column_b_rule_ok"]:
        issues.append("yhome_transition_guard_column_b_rule_not_ok")
    if send_requested and readiness_ok and guild_test_post_required and not guild_test_post_valid:
        issues.append("guild_test_post_required_before_email_not_valid")
    if send_requested and readiness_ok and guild_test_post_required and not guild_test_post_digest_ok:
        issues.append("guild_test_post_digest_missing_or_invalid")
    if send_requested and readiness_ok and guild_test_post_required and not guild_test_post_digest_matches_decision_inputs:
        issues.append("guild_test_post_digest_mismatch_send_decision_inputs")
    if send_requested and readiness_ok and guild_test_post_required and not guild_test_post_posted_at_month_matches:
        issues.append("guild_test_post_posted_at_missing_or_wrong_month")
    if send_requested and readiness_ok and guild_test_post_required and send_decision_inputs.get("guild_test_post_valid") is True and not guild_test_post_valid:
        issues.append("guild_test_post_decision_inputs_valid_without_snapshot_evidence")
    if send_requested and readiness_ok and guild_test_post_required and not guild_test_post_route_proof_ok:
        issues.append("guild_test_post_route_proof_missing_or_invalid")
    if send_requested and readiness_ok and guild_test_post_required and not guild_test_post_lofty_guild_ok_value:
        issues.append("guild_test_post_lofty_guild_missing_or_invalid")
    if send_requested and readiness_ok and not discord_all_property_guard["ok"]:
        issues.extend(f"discord_all_property_send:{issue}" for issue in discord_all_property_guard["issues"])
    if effective_send and evidence_issue_count:
        issues.append(f"owner_email_send_evidence_issue_count={evidence_issue_count}")
    if effective_send and will_send_count != evidence_count:
        issues.append(f"owner_email_send_evidence_mismatch:{evidence_count}/{will_send_count}")
    if excluded_payload_file_count:
        issues.append(f"excluded_payload_file_count={excluded_payload_file_count}")
    if excluded_owner_email_candidate_count:
        issues.append(f"excluded_owner_email_candidate_count={excluded_owner_email_candidate_count}")
    if effective_send and will_send_count > 0 and sent_state_write_status != "written":
        issues.append(f"sent_state_write_status={sent_state_write_status}")
    if effective_send and will_send_count > 0 and not sent_state_file_matches_run_month:
        issues.append("sent_state_file_missing_or_not_current_month")
    if effective_send and not send_lock_safe_to_send(send_lock_status):
        issues.append(f"send_lock_status={send_lock_status}")
    if send_requested and send_lock_file_unreadable:
        issues.append("send_lock_file_unreadable")
    if blocked_existing_lock and not existing_send_lock_digest_safe:
        issues.append("blocked_existing_lock_without_matching_send_decision_digest")
    if send_lock_file_loaded and not send_lock_run_month_matches:
        issues.append(f"send_lock_run_month_mismatch:{send_lock_run_month or 'missing'}!={run_month}")
    if not effective_send and will_send_count:
        issues.append(f"will_send_count_nonzero_while_effective_send_false:{will_send_count}")
    if not effective_send and evidence_issue_count:
        issues.append(f"owner_email_send_evidence_issue_count={evidence_issue_count}")
    if send_requested and not effective_send and not (blocked_reason or already_sent_this_month):
        issues.append("send_requested_without_blocked_reason")
    if send_requested and not effective_send and "already sent" in blocked_reason.lower() and not sent_state_file_matches_run_month:
        issues.append("already_sent_block_without_matching_sent_state")
    if reported_sent_state_matches_run_month and not sent_state_file_matches_run_month:
        issues.append("reported_sent_state_without_matching_file")
    if sent_state_file_error:
        issues.append(f"sent_state_file_unreadable:{sent_state_file_error}")

    current_month_sent_state_required = bool(effective_send and will_send_count > 0)
    send_evidence_matches_intent = bool(evidence_issue_count == 0 and will_send_count == evidence_count)
    send_state_write_ok = bool(
        not current_month_sent_state_required
        or (sent_state_write_status == "written" and sent_state_file_matches_run_month)
    )
    already_sent_state_ok = bool(
        "already sent" not in blocked_reason.lower()
        or sent_state_file_matches_run_month
    )
    reported_sent_state_ok = bool(
        not reported_sent_state_matches_run_month
        or sent_state_file_matches_run_month
    )
    max_once_monthly_ok = bool(
        run_month
        and (not send_requested or send_interval_ok)
        and (not send_requested or idempotency_configured)
        and send_state_write_ok
        and already_sent_state_ok
        and reported_sent_state_ok
        and not sent_state_file_error
    )
    send_lock_safe = globals()["send_lock_safe"](send_lock_status)
    send_lock_can_send = send_lock_safe_to_send(send_lock_status)
    send_lock_ok_for_guard = bool(
        not send_requested
        or (
            (send_lock_can_send if effective_send else send_lock_safe)
            and existing_send_lock_digest_safe
        )
    )

    send_allowed = bool(
        send_requested
        and effective_send
        and readiness_ok
        and publish.get("status") == "ok"
        and publish_fresh
        and owner_email_packet_ok_for_send
        and send_interval_ok
        and idempotency_configured
        and digest_ok
        and (not guild_test_post_required or guild_test_post_valid)
        and discord_all_property_guard["ok"]
        and transfer_telegram_gate["ok"]
        and not financial_review["blocked"]
        and not issues
        and evidence_issue_count == 0
        and will_send_count == evidence_count
        and (will_send_count == 0 or sent_state_write_status == "written")
        and (will_send_count == 0 or sent_state_file_matches_run_month)
        and send_lock_can_send
    )
    safe_block = bool(not effective_send and not issues and (blocked_reason or already_sent_this_month or not send_requested))
    guard_ok = send_allowed or safe_block
    final_gate_blocked = bool(
        (financial_review["blocked"] and financial_review.get("discord_review_ready") is True)
        or (transfer_telegram_gate["ok"] is False and transfer_telegram_gate.get("status") != "not_checked")
        or (send_requested and readiness_ok and not discord_all_property_guard["ok"])
    )
    no_spam_guard_ok = bool(
        guard_ok
        and max_once_monthly_ok
        and send_evidence_matches_intent
        and send_lock_ok_for_guard
        and (not effective_send or send_allowed)
    )
    publish_readiness_snapshot = publish.get("monthly_readiness_snapshot") if isinstance(publish.get("monthly_readiness_snapshot"), dict) else {}
    publish_readiness_snapshot_digest = publish_readiness_snapshot.get("digest")
    publish_readiness_snapshot_source_generated_at = publish_readiness_snapshot.get("source_generated_at")
    publish_readiness_snapshot_source_mtime = publish_readiness_snapshot.get("source_mtime")
    idempotency_proof = {
        "run_month": run_month,
        "max_once_monthly_ok": max_once_monthly_ok,
        "no_spam_guard_ok": no_spam_guard_ok,
        "send_requested": send_requested,
        "effective_send_owner_emails": effective_send,
        "send_blocked_reason": blocked_reason,
        "send_allowed": send_allowed,
        "safe_block": safe_block,
        "readiness_ok": readiness_ok,
        "publish_loaded": publish_loaded,
        "publish_fresh": publish_fresh,
        "publish_generated_at": publish.get("generated_at"),
        "publish_report_age_hours": iso_age_hours(publish.get("generated_at")),
        "publish_report_max_age_hours": PUBLISH_REPORT_MAX_AGE_HOURS,
        "publish_monthly_readiness_snapshot_digest": publish_readiness_snapshot_digest,
        "publish_monthly_readiness_snapshot_source_generated_at": publish_readiness_snapshot_source_generated_at,
        "publish_monthly_readiness_snapshot_source_mtime": publish_readiness_snapshot_source_mtime,
        "owner_email_packet_required": owner_email_packet_required,
        "owner_email_packet_fresh": owner_email_packet_fresh,
        "owner_email_packet_max_age_hours": OWNER_EMAIL_PACKET_MAX_AGE_HOURS,
        "owner_email_packet_generated_at": owner_email_packet.get("generated_at"),
        "owner_email_packet_status": owner_email_packet_status,
        "owner_email_packet_issue_count": owner_email_packet_issue_count,
        "owner_email_packet_safe_to_send_now": owner_email_packet_safe_to_send_now,
        "owner_email_packet_ok_for_send": owner_email_packet_ok_for_send,
        "owner_email_packet_legacy_packet_ok": owner_email_packet_legacy_packet_ok,
        "owner_email_packet_mcp_native_packet_ok": owner_email_packet_mcp_native_packet_ok,
        "owner_email_packet_property_count": owner_email_packet_property_count,
        "owner_email_packet_available_property_count": owner_email_packet_available_property_count,
        "owner_email_packet_property_unavailable_count": owner_email_packet_property_unavailable_count,
        "owner_email_packet_packet_count": owner_email_packet_packet_count,
        "owner_email_packet_native_property_count": owner_email_packet_native_property_count,
        "owner_email_packet_native_eligible_property_count": owner_email_packet_native_eligible_property_count,
        "owner_email_packet_native_cooldown_held_property_count": owner_email_packet_native_cooldown_held_property_count,
        "owner_email_packet_native_idempotency_held_property_count": owner_email_packet_native_idempotency_held_property_count,
        "owner_email_packet_native_property_coverage_ok": owner_email_packet_native_property_coverage_ok,
        "owner_email_packet_native_lofty_owner_email_allowed": owner_email_packet_native_allowed,
        "owner_email_packet_monthly_financial_summary_missing_property_count": owner_email_packet_monthly_financial_summary_missing_count,
        "owner_email_packet_monthly_financial_summary_present_total_property_count": owner_email_packet_monthly_financial_summary_present_count,
        "owner_email_packet_run_month": owner_email_packet_run_month,
        "owner_email_packet_run_month_matches": owner_email_packet_run_month_matches,
        "owner_email_packet_full_history_leak_count": owner_email_packet_full_history_leak_count,
        "owner_email_packet_full_history_guard_issue_count": owner_email_packet_full_history_guard_issue_count,
        "owner_email_packet_body_guard_issue_count": owner_email_packet_body_guard_issue_count,
        "owner_email_packet_unsafe_preview_packet_count": owner_email_packet_unsafe_preview_packet_count,
        "owner_email_packet_property_cooldown_days": owner_email_packet_property_cooldown_days,
        "owner_email_packet_property_cooldown_ok": owner_email_packet_property_cooldown_ok,
        "owner_email_packet_property_cooldown_gate_ok": owner_email_packet_property_cooldown_gate_ok,
        "owner_email_packet_property_cooldown_issue_count": owner_email_packet_property_cooldown_issue_count,
        "owner_email_packet_discord_all_property_send_plan_digest": owner_email_packet_discord_plan_digest,
        "owner_email_packet_discord_plan_digest_ok": owner_email_packet_discord_plan_digest_ok,
        "owner_email_packet_discord_plan_digest_matches_send": owner_email_packet_discord_plan_digest_matches_send,
        "owner_email_discord_review_chain": owner_email_discord_review_chain,
        "owner_email_packet_signal_only_ok": owner_email_packet_signal_only_ok,
        "active_property_guard_proof": active_property_proof,
        "active_property_policy_mentions_yhome": active_property_proof["policy_mentions_yhome"],
        "active_property_policy_mentions_manual_exclusions": active_property_proof["policy_mentions_manual_exclusions"],
        "manual_exclusions_ok": active_property_proof["manual_exclusions_ok"],
        "yhome_transition_guard_ok": active_property_proof["yhome_transition_guard_ok"],
        "yhome_transition_guard_column_b_rule_ok": active_property_proof["yhome_transition_guard_column_b_rule_ok"],
        "yhome_transition_guard_column_b_header": active_property_proof["yhome_transition_guard_column_b_header"],
        "yhome_transition_guard_column_b_marker_count": active_property_proof["yhome_transition_guard_column_b_marker_count"],
        "idempotency_configured": idempotency_configured,
        "send_interval_days": send_interval_days,
        "send_interval_ok": send_interval_ok,
        "send_decision_digest_ok": digest_ok,
        "send_decision_inputs_present": send_decision_inputs_present,
        "send_decision_inputs_digest": send_decision_inputs_digest,
        "send_decision_digest_matches_inputs": send_decision_digest_matches_inputs,
        "current_month_sent_state_required": current_month_sent_state_required,
        "current_month_sent_state_recorded": sent_state_file_matches_run_month,
        "sent_state_file": sent_state_file,
        "sent_state_file_resolved": str(sent_state_path or ""),
        "sent_state_write_status": sent_state_write_status,
        "send_state_write_ok": send_state_write_ok,
        "already_sent_this_month": already_sent_this_month,
        "already_sent_state_ok": already_sent_state_ok,
        "reported_sent_state_ok": reported_sent_state_ok,
        "send_lock_status": send_lock_status,
        "send_lock_file": send_lock_file,
        "send_lock_file_resolved": str(send_lock_path or ""),
        "send_lock_file_loaded": send_lock_file_loaded,
        "send_lock_file_status": send_lock_file_status,
        "send_lock_file_unreadable": send_lock_file_unreadable,
        "send_lock_run_month": send_lock_run_month,
        "send_lock_run_month_matches": send_lock_run_month_matches,
        "send_lock_manual_review_required": send_lock_manual_review_required,
        "send_lock_manual_review_reason": send_lock_manual_review_reason,
        "send_lock_safe_retry_without_duplicate_owner_email": send_lock_safe_retry_without_duplicate,
        "send_lock_owner_email_send_intended_count": send_lock_owner_email_send_intended_count,
        "send_lock_owner_email_send_attempted": send_lock_owner_email_send_attempted,
        "send_lock_owner_email_send_proven_complete": send_lock_owner_email_send_proven_complete,
        "existing_send_lock_decision_digest": existing_send_lock_decision_digest,
        "existing_send_lock_matches_send_decision": existing_send_lock_matches_send_decision,
        "existing_send_lock_digest_safe": existing_send_lock_digest_safe,
        "send_lock_safe": send_lock_safe,
        "send_lock_safe_to_send": send_lock_can_send,
        "send_lock_ok_for_guard": send_lock_ok_for_guard,
        "will_send_count": will_send_count,
        "send_evidence_count": evidence_count,
        "send_evidence_issue_count": evidence_issue_count,
        "send_evidence_matches_intent": send_evidence_matches_intent,
        "guild_test_post_required_before_email": guild_test_post_required,
        "guild_test_post_source": guild_test_post_source,
        "standalone_guild_test_post_status": standalone_guild_test_post.get("status"),
        "standalone_guild_test_post_valid": standalone_guild_test_post_valid,
        "posted_guild_test_post_path": str(posted_guild_test_post_path) if posted_guild_test_post_path else None,
        "posted_guild_test_post_status": posted_guild_test_post.get("status"),
        "posted_guild_test_post_valid": posted_guild_test_post_valid,
        "guild_test_post_valid": guild_test_post_valid,
        "guild_test_post_required_lofty_guild_id": REQUIRED_LOFTY_GUILD_ID,
        "guild_test_post_lofty_guild_ok": guild_test_post_lofty_guild_ok_value,
        "guild_test_post_digest_ok": guild_test_post_digest_ok,
        "guild_test_post_digest": guild_test_post_digest,
        "guild_test_post_digest_matches_decision_inputs": guild_test_post_digest_matches_decision_inputs,
        "send_decision_guild_test_post_digest": send_decision_guild_test_post_digest,
        "guild_test_post_digest": guild_test_post_digest,
        "guild_test_post_digest_matches_decision_inputs": guild_test_post_digest_matches_decision_inputs,
        "send_decision_guild_test_post_digest": send_decision_guild_test_post_digest,
        "guild_test_post_posted_at": guild_test_post.get("posted_at"),
        "guild_test_post_posted_at_month_matches": guild_test_post_posted_at_month_matches,
        "guild_test_post_route_proof_ok": guild_test_post_route_proof_ok,
        "guild_test_post_handoff": guild_test_post_handoff_summary,
        "discord_all_property_send_guard": discord_all_property_guard,
        "discord_all_property_send_required": discord_all_property_guard["required"],
        "discord_all_property_send_ok": discord_all_property_guard["ok"],
        "discord_all_property_send_status": discord_all_property_guard["status"],
        "discord_all_property_send_mode": discord_all_property_guard["mode"],
        "discord_all_property_send_record_count": discord_all_property_guard["record_count"],
        "discord_all_property_send_current_plan_record_count": discord_all_property_guard["current_plan_record_count"],
        "discord_all_property_send_record_count_matches_current_plan": discord_all_property_guard[
            "record_count_matches_current_plan"
        ],
        "discord_all_property_send_verified_count": discord_all_property_guard["verified_count"],
        "discord_all_property_send_failed_count": discord_all_property_guard["failed_count"],
        "discord_all_property_send_plan_digest": discord_all_property_guard["plan_digest"],
        "discord_all_property_send_plan_digest_ok": discord_all_property_guard["plan_digest_ok"],
        "discord_all_property_send_issues": discord_all_property_guard["issues"],
        "transfer_telegram_send_gate_ok": transfer_telegram_gate["ok"],
        "transfer_telegram_send_status": transfer_telegram_gate["status"],
        "transfer_telegram_send_dry_run": transfer_telegram_gate["dry_run"],
        "transfer_telegram_send_safe": transfer_telegram_gate["send_safe"],
        "transfer_telegram_send_ok": transfer_telegram_gate["telegram_send_ok"],
        "transfer_telegram_message_quality_ok": transfer_telegram_gate["message_quality_ok"],
        "transfer_telegram_current_for_transfer": transfer_telegram_gate["current_for_transfer"],
        "transfer_telegram_preview_validated": transfer_telegram_gate["preview_validated"],
        "transfer_telegram_send_issues": transfer_telegram_gate["issues"],
        "transfer_telegram_send_blockers": transfer_telegram_gate["send_blockers"],
        "transfer_telegram_transfer_report_digest": transfer_telegram_gate["transfer_report_digest"],
        "transfer_telegram_current_transfer_report_digest": transfer_telegram_gate["current_transfer_report_digest"],
        "transfer_telegram_transfer_report_digest_matches_current": transfer_telegram_gate[
            "transfer_report_digest_matches_current"
        ],
        "excluded_property_count": excluded_property_count,
        "excluded_payload_file_count": excluded_payload_file_count,
        "excluded_owner_email_candidate_count": excluded_owner_email_candidate_count,
        "issue_count": len(issues),
        "email_final_gate_financial_blocked": financial_review["blocked"],
        "financial_review_blocker_count": financial_review["blocker_count"],
        "financial_review_blockers": financial_review["blockers"],
        "lofty_listing_financial_update_chain": financial_review["lofty_listing_financial_update_chain"],
        "lofty_listing_financial_update_chain_ok": financial_review["lofty_listing_financial_update_chain_ok"],
        "lofty_monthly_summary_issue_records": financial_review["lofty_monthly_summary_issue_records"],
        "excluded_lofty_monthly_summary_issue_record_count": financial_review[
            "excluded_lofty_monthly_summary_issue_record_count"
        ],
        "excluded_lofty_monthly_summary_issue_records": financial_review[
            "excluded_lofty_monthly_summary_issue_records"
        ],
        "transfer_reconciliation_recommended_send_to_lofty_total_is_final": financial_review[
            "transfer_reconciliation_recommended_send_to_lofty_total_is_final"
        ],
        "transfer_reconciliation_report_loaded": financial_review["transfer_reconciliation_report_loaded"],
        "transfer_reconciliation_source_blockers": financial_review["transfer_reconciliation_source_blockers"],
        "transfer_reconciliation_active_source_cash_actions": financial_review[
            "transfer_reconciliation_active_source_cash_actions"
        ],
        "transfer_reconciliation_property_cash_review_details": financial_review[
            "transfer_reconciliation_property_cash_review_details"
        ],
        "discord_financial_review_issue_count": financial_review["discord_financial_review_issue_count"],
        "discord_financial_review_issues": financial_review["discord_financial_review_issues"],
        "discord_financial_review_artifact_area_count": financial_review[
            "discord_financial_review_artifact_area_count"
        ],
        "discord_financial_review_missing_artifact_count": financial_review[
            "discord_financial_review_missing_artifact_count"
        ],
        "discord_financial_review_artifacts": financial_review["discord_financial_review_artifacts"],
        "discord_plan_validation_issue_count": financial_review["discord_plan_validation_issue_count"],
        "discord_plan_validation_non_financial_issue_count": financial_review[
            "discord_plan_validation_non_financial_issue_count"
        ],
        "discord_review_ready": financial_review["discord_review_ready"],
        "discord_review_ready_but_financial_blocked": financial_review[
            "discord_review_ready_but_financial_blocked"
        ],
        "discord_review_policy": financial_review["discord_review_policy"],
        "discord_plan_validation_non_financial_issues": financial_review[
            "discord_plan_validation_non_financial_issues"
        ],
        "discord_plan_validation_message_digest_issue_count": financial_review[
            "discord_plan_validation_message_digest_issue_count"
        ],
    }
    readiness_primary = monthly_readiness_primary_blocker(readiness)
    primary_blocker = owner_email_final_primary_blocker(
        send_allowed=send_allowed,
        safe_block=safe_block,
        issues=issues,
        blocked_reason=blocked_reason,
        owner_email_packet=owner_email_packet_for_primary_blocker,
        financial_review=financial_review,
        discord_all_property_guard=discord_all_property_guard,
        transfer_telegram_gate=transfer_telegram_gate,
        readiness_primary=readiness_primary,
    )
    return {
        "generated_at": iso_z(),
        "status": "ok" if no_spam_guard_ok and not final_gate_blocked else "review",
        "guard_ok": no_spam_guard_ok,
        "transport_guard_ok": guard_ok,
        "final_gate_blocked": final_gate_blocked,
        "max_once_monthly_ok": max_once_monthly_ok,
        "no_spam_guard_ok": no_spam_guard_ok,
        "idempotency_proof": idempotency_proof,
        "send_allowed": send_allowed,
        "safe_block": safe_block,
        "primary_blocker": primary_blocker,
        "next_action": primary_blocker.get("next_action"),
        "run_month": run_month,
        "readiness_report": str(readiness_path),
        "publish_report": str(publish_path),
        "owner_email_packet_report": str(owner_email_packet_path) if owner_email_packet_path else None,
        "monthly_disk_space_preflight_status": disk_space_preflight.get("status"),
        "monthly_finance_truth_refresh_issue_summary": monthly_finance_truth_refresh_issue_summary or None,
        "monthly_finance_truth_refresh_next_action": monthly_finance_truth_refresh_next_action or None,
        "monthly_finance_truth_refresh_auth_blocked": monthly_run.get("monthly_finance_truth_refresh_auth_blocked") is True,
        "monthly_finance_truth_refresh_cdp_blocked": monthly_run.get("monthly_finance_truth_refresh_cdp_blocked") is True,
        "baselane_login_wait_reason": baselane_login_wait_reason or None,
        "baselane_login_wait_recaptcha_present": baselane_login_wait_recaptcha_present,
        "baselane_login_wait_current_url": baselane_login_wait_current_url or None,
        "owner_email_packet_stale_primary_blocker_ignored": owner_email_packet_for_primary_blocker.get(
            "stale_primary_blocker_ignored"
        ),
        "owner_email_packet_stale_primary_blocker_ignored_reason": owner_email_packet_for_primary_blocker.get(
            "stale_primary_blocker_ignored_reason"
        ),
        "owner_email_packet_stale_primary_blocker_replaced": owner_email_packet_for_primary_blocker.get(
            "stale_primary_blocker_replaced"
        ),
        "owner_email_packet_stale_primary_blocker_replaced_reason": owner_email_packet_for_primary_blocker.get(
            "stale_primary_blocker_replaced_reason"
        ),
        "transfer_reconciliation_report": str(transfer_reconciliation_path) if transfer_reconciliation_path else None,
        "transfer_telegram_send_report": str(transfer_telegram_send_path) if transfer_telegram_send_path else None,
        "discord_plan_validation_report": str(discord_plan_validation_path) if discord_plan_validation_path else None,
        "email_final_gate_discord_review_blocked": not discord_all_property_guard["ok"],
        "email_final_gate_transfer_telegram_blocked": not transfer_telegram_gate["ok"],
        "transfer_telegram_send_gate_ok": transfer_telegram_gate["ok"],
        "transfer_telegram_send_status": transfer_telegram_gate["status"],
        "transfer_telegram_send_dry_run": transfer_telegram_gate["dry_run"],
        "transfer_telegram_send_safe": transfer_telegram_gate["send_safe"],
        "transfer_telegram_send_ok": transfer_telegram_gate["telegram_send_ok"],
        "transfer_telegram_message_quality_ok": transfer_telegram_gate["message_quality_ok"],
        "transfer_telegram_current_for_transfer": transfer_telegram_gate["current_for_transfer"],
        "transfer_telegram_preview_validated": transfer_telegram_gate["preview_validated"],
        "transfer_telegram_transfer_report_status": transfer_telegram_gate["transfer_report_status"],
        "transfer_telegram_transfer_report_recommended_send_to_lofty_total_is_final": transfer_telegram_gate[
            "transfer_report_recommended_send_to_lofty_total_is_final"
        ],
        "transfer_telegram_send_issues": transfer_telegram_gate["issues"],
        "transfer_telegram_send_blockers": transfer_telegram_gate["send_blockers"],
        "transfer_telegram_transfer_report_digest": transfer_telegram_gate["transfer_report_digest"],
        "transfer_telegram_current_transfer_report_digest": transfer_telegram_gate["current_transfer_report_digest"],
        "transfer_telegram_transfer_report_digest_matches_current": transfer_telegram_gate[
            "transfer_report_digest_matches_current"
        ],
        "email_final_gate_financial_blocked": financial_review["blocked"],
        "financial_review_blocker_count": financial_review["blocker_count"],
        "financial_review_blockers": financial_review["blockers"],
        "lofty_listing_financial_update_chain": financial_review["lofty_listing_financial_update_chain"],
        "lofty_listing_financial_update_chain_ok": financial_review["lofty_listing_financial_update_chain_ok"],
        "lofty_monthly_summary_issue_records": financial_review["lofty_monthly_summary_issue_records"],
        "excluded_lofty_monthly_summary_issue_record_count": financial_review[
            "excluded_lofty_monthly_summary_issue_record_count"
        ],
        "excluded_lofty_monthly_summary_issue_records": financial_review[
            "excluded_lofty_monthly_summary_issue_records"
        ],
        "readiness_financial_blockers": financial_review["readiness_financial_blockers"],
        "transfer_reconciliation_status": financial_review["transfer_reconciliation_status"],
        "transfer_reconciliation_recommended_send_to_lofty_total_is_final": financial_review[
            "transfer_reconciliation_recommended_send_to_lofty_total_is_final"
        ],
        "transfer_reconciliation_report_loaded": financial_review["transfer_reconciliation_report_loaded"],
        "transfer_reconciliation_source_blockers": financial_review["transfer_reconciliation_source_blockers"],
        "transfer_reconciliation_active_source_cash_actions": financial_review[
            "transfer_reconciliation_active_source_cash_actions"
        ],
        "transfer_reconciliation_property_cash_review_details": financial_review[
            "transfer_reconciliation_property_cash_review_details"
        ],
        "discord_plan_validation_status": financial_review["discord_plan_validation_status"],
        "discord_financial_review_issue_count": financial_review["discord_financial_review_issue_count"],
        "discord_financial_review_issues": financial_review["discord_financial_review_issues"],
        "discord_financial_review_artifact_area_count": financial_review[
            "discord_financial_review_artifact_area_count"
        ],
        "discord_financial_review_missing_artifact_count": financial_review[
            "discord_financial_review_missing_artifact_count"
        ],
        "discord_financial_review_artifacts": financial_review["discord_financial_review_artifacts"],
        "discord_plan_validation_issue_count": financial_review["discord_plan_validation_issue_count"],
        "discord_plan_validation_non_financial_issue_count": financial_review[
            "discord_plan_validation_non_financial_issue_count"
        ],
        "discord_review_ready": financial_review["discord_review_ready"],
        "discord_review_ready_but_financial_blocked": financial_review[
            "discord_review_ready_but_financial_blocked"
        ],
        "discord_review_policy": financial_review["discord_review_policy"],
        "discord_plan_validation_non_financial_issues": financial_review[
            "discord_plan_validation_non_financial_issues"
        ],
        "discord_plan_validation_message_digest_issue_count": financial_review[
            "discord_plan_validation_message_digest_issue_count"
        ],
        "publish_readiness_snapshot_refresh": publish_readiness_snapshot_refresh,
        "monthly_run_report": str(monthly_run_path),
        "readiness_status": readiness.get("status"),
        "readiness_run_month": readiness.get("run_month"),
        "readiness_owner_email_allowed": readiness.get("owner_email_allowed"),
        "readiness_owner_email_blocked_reason": readiness.get("owner_email_blocked_reason") or (
            monthly_readiness_blocked_reason(readiness) if readiness.get("owner_email_allowed") is False else None
        ),
        "readiness_blocker_count": readiness.get("blocker_count"),
        "readiness_actionable_blocker_count": count(
            (readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}).get("actionable_blocker_count")
        ),
        "readiness_primary_blocker": readiness_primary.get("blocker") or readiness_primary.get("class"),
        "readiness_primary_blocker_detail": readiness_primary,
        "publish_status": publish.get("status"),
        "publish_generated_at": publish.get("generated_at"),
        "publish_report_age_hours": iso_age_hours(publish.get("generated_at")),
        "publish_report_max_age_hours": PUBLISH_REPORT_MAX_AGE_HOURS,
        "publish_fresh": publish_fresh,
        "publish_monthly_readiness_snapshot_digest": publish_readiness_snapshot_digest,
        "publish_monthly_readiness_snapshot_source_generated_at": publish_readiness_snapshot_source_generated_at,
        "publish_monthly_readiness_snapshot_source_mtime": publish_readiness_snapshot_source_mtime,
        "owner_email_packet_required": owner_email_packet_required,
        "owner_email_packet_loaded": owner_email_packet_loaded,
        "owner_email_packet_fresh": owner_email_packet_fresh,
        "owner_email_packet_max_age_hours": OWNER_EMAIL_PACKET_MAX_AGE_HOURS,
        "owner_email_packet_generated_at": owner_email_packet.get("generated_at"),
        "owner_email_packet_status": owner_email_packet_status,
        "owner_email_packet_issue_count": owner_email_packet_issue_count,
        "owner_email_packet_run_month": owner_email_packet_run_month,
        "owner_email_packet_run_month_matches": owner_email_packet_run_month_matches,
        "owner_email_packet_safe_to_send_now": owner_email_packet_safe_to_send_now,
        "owner_email_packet_ok_for_send": owner_email_packet_ok_for_send,
        "owner_email_packet_legacy_packet_ok": owner_email_packet_legacy_packet_ok,
        "owner_email_packet_mcp_native_packet_ok": owner_email_packet_mcp_native_packet_ok,
        "owner_email_packet_property_count": owner_email_packet_property_count,
        "owner_email_packet_available_property_count": owner_email_packet_available_property_count,
        "owner_email_packet_property_unavailable_count": owner_email_packet_property_unavailable_count,
        "owner_email_packet_packet_count": owner_email_packet_packet_count,
        "owner_email_packet_native_property_count": owner_email_packet_native_property_count,
        "owner_email_packet_native_eligible_property_count": owner_email_packet_native_eligible_property_count,
        "owner_email_packet_native_cooldown_held_property_count": owner_email_packet_native_cooldown_held_property_count,
        "owner_email_packet_native_idempotency_held_property_count": owner_email_packet_native_idempotency_held_property_count,
        "owner_email_packet_native_property_coverage_ok": owner_email_packet_native_property_coverage_ok,
        "owner_email_packet_native_lofty_owner_email_allowed": owner_email_packet_native_allowed,
        "owner_email_packet_monthly_financial_summary_missing_property_count": owner_email_packet_monthly_financial_summary_missing_count,
        "owner_email_packet_monthly_financial_summary_present_total_property_count": owner_email_packet_monthly_financial_summary_present_count,
        "owner_email_packet_full_history_leak_count": owner_email_packet_full_history_leak_count,
        "owner_email_packet_full_history_guard_issue_count": owner_email_packet_full_history_guard_issue_count,
        "owner_email_packet_body_guard_issue_count": owner_email_packet_body_guard_issue_count,
        "owner_email_packet_unsafe_preview_packet_count": owner_email_packet_unsafe_preview_packet_count,
        "owner_email_packet_property_cooldown_days": owner_email_packet_property_cooldown_days,
        "owner_email_packet_property_cooldown_ok": owner_email_packet_property_cooldown_ok,
        "owner_email_packet_property_cooldown_gate_ok": owner_email_packet_property_cooldown_gate_ok,
        "owner_email_packet_property_cooldown_issue_count": owner_email_packet_property_cooldown_issue_count,
        "owner_email_packet_discord_all_property_send_plan_digest": owner_email_packet_discord_plan_digest,
        "owner_email_packet_discord_plan_digest_ok": owner_email_packet_discord_plan_digest_ok,
        "owner_email_packet_discord_plan_digest_matches_send": owner_email_packet_discord_plan_digest_matches_send,
        "owner_email_discord_review_chain": owner_email_discord_review_chain,
        "owner_email_packet_signal_only_ok": owner_email_packet_signal_only_ok,
        "owner_email_packet_email_body_max_chars": owner_email_packet_email_body_max_chars,
        "owner_email_packet_email_body_max_lines": owner_email_packet_email_body_max_lines,
        "owner_email_packet_property_update_max_chars": owner_email_packet_property_update_max_chars,
        "owner_email_packet_property_update_max_lines": owner_email_packet_property_update_max_lines,
        "active_property_guard_proof": active_property_proof,
        "active_property_policy_mentions_yhome": active_property_proof["policy_mentions_yhome"],
        "active_property_policy_mentions_manual_exclusions": active_property_proof["policy_mentions_manual_exclusions"],
        "manual_exclusions_ok": active_property_proof["manual_exclusions_ok"],
        "yhome_transition_guard_ok": active_property_proof["yhome_transition_guard_ok"],
        "yhome_transition_guard_column_b_rule_ok": active_property_proof["yhome_transition_guard_column_b_rule_ok"],
        "yhome_transition_guard_column_b_header": active_property_proof["yhome_transition_guard_column_b_header"],
        "yhome_transition_guard_column_b_marker_count": active_property_proof["yhome_transition_guard_column_b_marker_count"],
        "send_requested": send_requested,
        "effective_send_owner_emails": effective_send,
        "send_blocked_reason": blocked_reason,
        "send_interval_days": send_interval_days,
        "send_interval_ok": send_interval_ok,
        "guild_test_post_required_before_email": guild_test_post_required,
        "guild_test_post_source": guild_test_post_source,
        "standalone_guild_test_post_status": standalone_guild_test_post.get("status"),
        "standalone_guild_test_post_valid": standalone_guild_test_post_valid,
        "posted_guild_test_post_path": str(posted_guild_test_post_path) if posted_guild_test_post_path else None,
        "posted_guild_test_post_status": posted_guild_test_post.get("status"),
        "posted_guild_test_post_valid": posted_guild_test_post_valid,
        "guild_test_post_required_lofty_guild_id": REQUIRED_LOFTY_GUILD_ID,
        "guild_test_post_lofty_guild_ok": guild_test_post_lofty_guild_ok_value,
        "guild_test_post_valid": guild_test_post_valid,
        "guild_test_post_digest_ok": guild_test_post_digest_ok,
        "guild_test_post_digest": guild_test_post_digest,
        "guild_test_post_digest_matches_decision_inputs": guild_test_post_digest_matches_decision_inputs,
        "send_decision_guild_test_post_digest": send_decision_guild_test_post_digest,
        "guild_test_post_posted_at": guild_test_post.get("posted_at"),
        "guild_test_post_posted_at_month_matches": guild_test_post_posted_at_month_matches,
        "guild_test_post_route_proof_ok": guild_test_post_route_proof_ok,
        "guild_test_post_status": guild_test_post_handoff_summary["status"],
        "guild_test_post_post_status": guild_test_post_handoff_summary["post_status"],
        "guild_test_post_prepared": guild_test_post_handoff_summary["prepared"],
        "guild_test_post_posted": guild_test_post_handoff_summary["posted"],
        "guild_test_post_message_file": guild_test_post_handoff_summary["message_file"],
        "guild_test_post_envelope_file": guild_test_post_handoff_summary["envelope_file"],
        "guild_test_post_target": guild_test_post_handoff_summary["target"],
        "guild_test_post_target_channel_id": guild_test_post_handoff_summary["target_channel_id"],
        "guild_test_post_selected_property_name": guild_test_post_handoff_summary["selected_property_name"],
        "guild_test_post_posted_channel_id": guild_test_post_handoff_summary["posted_channel_id"],
        "guild_test_post_posted_message_id": guild_test_post_handoff_summary["posted_message_id"],
        "guild_test_post_run_month": guild_test_post_handoff_summary["run_month"],
        "guild_test_post_run_month_matches": guild_test_post_handoff_summary["run_month_matches"],
        "guild_test_post_next_action": guild_test_post_handoff_summary["next_action"],
        "guild_test_post_handoff": guild_test_post_handoff_summary,
        "guild_test_post_snapshot": guild_test_post,
        "discord_all_property_send_guard": discord_all_property_guard,
        "discord_all_property_send_required": discord_all_property_guard["required"],
        "discord_all_property_send_ok": discord_all_property_guard["ok"],
        "discord_all_property_send_status": discord_all_property_guard["status"],
        "discord_all_property_send_mode": discord_all_property_guard["mode"],
        "discord_all_property_send_record_count": discord_all_property_guard["record_count"],
        "discord_all_property_send_current_plan_record_count": discord_all_property_guard["current_plan_record_count"],
        "discord_all_property_send_record_count_matches_current_plan": discord_all_property_guard[
            "record_count_matches_current_plan"
        ],
        "discord_all_property_send_verified_count": discord_all_property_guard["verified_count"],
        "discord_all_property_send_failed_count": discord_all_property_guard["failed_count"],
        "discord_all_property_send_plan_digest": discord_all_property_guard["plan_digest"],
        "discord_all_property_send_plan_digest_ok": discord_all_property_guard["plan_digest_ok"],
        "discord_all_property_send_issues": discord_all_property_guard["issues"],
        "sent_state_file": sent_state_file,
        "sent_state_file_resolved": str(sent_state_path or ""),
        "sent_state_file_exists": sent_state_file_exists,
        "sent_state_file_month": sent_state_file_month,
        "sent_state_file_matches_run_month": sent_state_file_matches_run_month,
        "reported_sent_state_matches_run_month": reported_sent_state_matches_run_month,
        "sent_state_matches_run_month": sent_state_matches_run_month,
        "sent_state_file_error": sent_state_file_error,
        "sent_state_month": sent_state_month,
        "already_sent_this_month": already_sent_this_month,
        "idempotency_configured": idempotency_configured,
        "send_decision_digest": send_decision_digest,
        "send_decision_digest_ok": digest_ok,
        "send_decision_inputs_present": send_decision_inputs_present,
        "send_decision_inputs_digest": send_decision_inputs_digest,
        "send_decision_digest_matches_inputs": send_decision_digest_matches_inputs,
        "current_month_sent_state_required": current_month_sent_state_required,
        "current_month_sent_state_recorded": sent_state_file_matches_run_month,
        "send_state_write_ok": send_state_write_ok,
        "already_sent_state_ok": already_sent_state_ok,
        "reported_sent_state_ok": reported_sent_state_ok,
        "send_lock_status": send_lock_status,
        "send_lock_file": send_lock_file,
        "send_lock_file_resolved": str(send_lock_path or ""),
        "send_lock_file_loaded": send_lock_file_loaded,
        "send_lock_file_status": send_lock_file_status,
        "send_lock_file_unreadable": send_lock_file_unreadable,
        "send_lock_run_month": send_lock_run_month,
        "send_lock_run_month_matches": send_lock_run_month_matches,
        "send_lock_manual_review_required": send_lock_manual_review_required,
        "send_lock_manual_review_reason": send_lock_manual_review_reason,
        "send_lock_safe_retry_without_duplicate_owner_email": send_lock_safe_retry_without_duplicate,
        "send_lock_owner_email_send_intended_count": send_lock_owner_email_send_intended_count,
        "send_lock_owner_email_send_attempted": send_lock_owner_email_send_attempted,
        "send_lock_owner_email_send_proven_complete": send_lock_owner_email_send_proven_complete,
        "existing_send_lock_decision_digest": existing_send_lock_decision_digest,
        "existing_send_lock_matches_send_decision": existing_send_lock_matches_send_decision,
        "existing_send_lock_digest_safe": existing_send_lock_digest_safe,
        "send_lock_safe": send_lock_safe,
        "send_lock_safe_to_send": send_lock_can_send,
        "send_lock_ok_for_guard": send_lock_ok_for_guard,
        "sent_state_write_status": sent_state_write_status,
        "send_evidence_matches_intent": send_evidence_matches_intent,
        "owner_email_will_send_count": will_send_count,
        "owner_email_send_evidence_count": evidence_count,
        "owner_email_send_evidence_issue_count": evidence_issue_count,
        "excluded_property_count": excluded_property_count,
        "excluded_payload_file_count": excluded_payload_file_count,
        "excluded_owner_email_candidate_count": excluded_owner_email_candidate_count,
        "issue_count": len(issues),
        "issues": issues,
        "policy": "Owner email is allowed only after clean monthly readiness, guarded publish success, matching send evidence, sent-state recording, and max-once-per-month idempotency.",
        "root": str(root),
    }


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit monthly owner-email no-spam send guard state.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--readiness-report", type=Path)
    parser.add_argument("--publish-report", type=Path)
    parser.add_argument("--monthly-run-report", type=Path)
    parser.add_argument("--owner-email-packet-report", type=Path)
    parser.add_argument("--discord-send-report", type=Path)
    parser.add_argument("--transfer-reconciliation-report", type=Path)
    parser.add_argument("--transfer-telegram-send-report", type=Path)
    parser.add_argument("--discord-plan-validation-report", type=Path)
    parser.add_argument("--guild-test-post-report", type=Path)
    parser.add_argument("--yhome-transition-csv", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.root
    report = build_report(
        root,
        args.readiness_report or root / "reports" / "baselane_financials_monthly_readiness.json",
        args.publish_report or root / "reports" / "baselane_financials_monthly_lofty_pm_publish.json",
        args.monthly_run_report or root / "reports" / "baselane_financials_monthly_run_report.json",
        args.owner_email_packet_report or root / "reports" / "baselane_monthly_owner_email_packet.json",
        args.discord_send_report or root / "reports" / "baselane_financials_monthly_discord_property_update_send.json",
        args.transfer_reconciliation_report or root / "reports" / "baselane_lofty_transfer_requirements.json",
        args.transfer_telegram_send_report or root / "reports" / "baselane_lofty_transfer_requirements_telegram_send.json",
        args.discord_plan_validation_report or root / "reports" / "baselane_financials_monthly_discord_all_send_plan_validation.json",
        args.guild_test_post_report or root / "reports" / "baselane_financials_monthly_guild_test_post.json",
        args.yhome_transition_csv,
    )
    report_path = args.report or root / "reports" / "baselane_monthly_owner_email_send_guard.json"
    write_json(report_path, report)
    print(json.dumps({key: report[key] for key in ("status", "send_allowed", "safe_block", "issue_count")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
