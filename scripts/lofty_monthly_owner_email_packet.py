#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from lofty_monthly_exclusions import (
    DEFAULT_LISTING_UPDATE_POLICY_PATH,
    normalize as normalize_exclusion_property,
    policy_name_matches,
    sold_policy_exclusion_records,
)
from lofty_monthly_publish_to_pm import DEFAULT_MANUAL_EXCLUDED_PROPERTIES


def default_root() -> Path:
    env_root = os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("WORKSPACE_ROOT")
    if env_root:
        return Path(env_root)
    cwd = Path.cwd()
    if (cwd / "scripts").is_dir():
        return cwd
    return Path(__file__).absolute().parents[1]


ROOT = default_root()
SCRIPT_PATH = ROOT / "scripts" / "lofty_monthly_owner_email_packet.py"
REPORTS = ROOT / "reports"
DEFAULT_REPORT = REPORTS / "baselane_monthly_owner_email_packet.json"
DEFAULT_SENT_STATE = REPORTS / "baselane_monthly_owner_email_non_native_sent_state.json"
DEFAULT_OUT_DIR = REPORTS / "monthly_owner_email_packet"
DEFAULT_LISTING_CLEANUP_QUEUE_REPORT = REPORTS / "lofty_listing_update_cleanup_queue.json"
DEFAULT_REVIEW_CANDIDATE_PACKET = REPORTS / "baselane_financials_monthly_review_candidate_packet.json"
DEFAULT_DISCORD_SEND_REPORT = REPORTS / "baselane_financials_monthly_discord_property_update_send.json"
DEFAULT_DISCORD_PLAN_VALIDATION_REPORT = REPORTS / "baselane_financials_monthly_discord_all_send_plan_validation.json"
DEFAULT_PROPERTY_GAP_CSV_NAME = "lofty_owner_email_property_gaps.csv"
CANONICAL_RUNTIME_MAP_NAME = "baselane_financials_monthly_lofty_pm_runtime_map.json"
SEND_ENV = "LOFTY_NON_NATIVE_OWNER_EMAIL_SEND"
DEFAULT_RECIPIENTS_CSV = REPORTS / "lofty_owner_email_recipients.csv"
ISSUE_CLASS = "lofty-monthly-owner-email-packet"
RECIPIENT_CSV_FIELDS = ("name", "email", "property_name", "lofty_property_id")
PROPERTY_GAP_CSV_FIELDS = (
    "property_name",
    "lofty_property_id",
    "reason",
    "updates_md",
    "latest_update_source",
    "latest_update_source_type",
    "issue",
    "financial_summary_enriched",
    "monthly_financial_summary_present",
    "candidate_summary_source",
    "candidate_update_source",
    "candidate_update_approval_target",
    "candidate_financial_approval_target",
    "listing_cleanup_queue_status",
    "listing_cleanup_ready_but_local_update_blocked",
    "listing_cleanup_live_snapshot_listing_issues",
    "listing_cleanup_dry_run_command",
    "listing_cleanup_live_apply_command_requires_explicit_approval",
)
PROPERTY_UPDATE_MARKER_RE = re.compile(r"(?mi)^\s*-\s+\*{0,2}\s*Property Update\s*\(")
PROPERTY_UPDATE_ENTRY_RE = re.compile(r"(?ms)^\s*-\s+(?:\*\*)?\s*Property Update\s*\((\d{2}/\d{2}/\d{4})\):(?:\*\*)?\s*(.*)$")
PROPERTY_UPDATES_HEADER_RE = re.compile(r"(?mi)^\s*#\s+Property Updates\s*$")
DATED_UPDATE_HEADING_RE = re.compile(r"(?mi)^\s*##\s+\d{4}-\d{2}-\d{2}\s*$")
DATED_UPDATE_HEADING_DATE_RE = re.compile(r"(?mi)^\s*##\s+(\d{4}-\d{2}-\d{2})\s*$")
LOFTY_RESERVE_SUMMARY_RE = re.compile(
    r"(?mi)^\s*(?:-\s*Lofty maintenance reserve balance:|\|\s*Lofty maintenance reserve balance\s*\|)\s*-?\$[\d,]+\.\d{2}"
)
ECO_GL_SUMMARY_RE = re.compile(
    r"(?mi)^\s*(?:-\s*ECO GL Column E sum:|\|\s*ECO Operating Cash\s*\||ECO Operating Cash:)\s*-?\$[\d,]+\.\d{2}(?:\s+\(\d+\s+rows\)|\s+\([^)\n]*rows[^)\n]*\))?"
)
FINANCIALS_MD_SUMMARY_RE = re.compile(r"(?m)^(?:Financial detail:|Financial summary from FINANCIALS\.md:)\s*$")
SPENDABLE_ECO_SUMMARY_RE = re.compile(
    r"(?mi)^\s*(?:-\s*ECO Net DAO Funds \(spendable cash held by ECO\):"
    r"|\|\s*ECO Net DAO Funds \(spendable cash held by ECO\)\s*\|)\s*\$[\d,]+\.\d{2}"
)
DISALLOWED_LIMITED_FINANCIAL_SUMMARY_SNIPPETS = (
    "This month's update is limited to verified cash-position data from Lofty and ECO records.",
    "No tenant ledger rows are included.",
)
INTERNAL_OWNER_UPDATE_CONTENT_RE = re.compile(
    r"(?mi)^\s*(?:##\s*)?(?:Monthly send checklist|Internal context\b|Recent Public(?:/Sale)? file activity:)"
    r"|^\s*-\s+\[\s*\]\s+"
    r"|remove before (?:sending|owner-facing publication)"
    r"|do not use native Lofty owner email"
    r"|send-property-updates"
)
OPERATIONAL_REPORT_CONTENT_RE = re.compile(
    r"(?mi)^\s*(?:EOD|BLOCKER|OPEN|DO|SKIP|HOLD|Sync|Primary blocker|Next action):\s+"
    r"|\b(?:actionable_summary|primary_blocker|issue_count|generated_at|owner_email_allowed)\b"
    r"|\breports/[^\s]+"
)
MAX_EMAIL_BODY_CHARS = 3500
MAX_EMAIL_BODY_LINES = 80
MAX_PROPERTY_UPDATE_CHARS = 1800
MAX_PROPERTY_UPDATE_LINES = 45
PROPERTY_EMAIL_COOLDOWN_DAYS = 7
MIN_FULL_SOURCE_LEAK_CHARS = 200
SIGNAL_ONLY_OWNER_EMAIL_POLICY = (
    "one property per packet; latest approved Property Update snippet only; "
    "verified Lofty-held and ECO-held spendable-cash summary required; no UPDATES.md history"
)
DISCORD_SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def current_run_month() -> str:
    return os.environ.get("RUN_MONTH") or datetime.now(timezone.utc).strftime("%Y-%m")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not object"}


def normalize_property_name(value: object) -> str:
    normalized = normalize_exclusion_property(str(value or ""))
    return " ".join(token for token in normalized.split() if token != "public")


def financially_held_property_names(validation_report: dict[str, Any]) -> list[str]:
    records = validation_report.get("financial_review_blocked_properties")
    if not isinstance(records, list):
        return []
    names = [
        str(record.get("property_name") or "").strip()
        for record in records
        if isinstance(record, dict) and str(record.get("property_name") or "").strip()
    ]
    return list(dict.fromkeys(names))


def property_is_financially_held(property_name: object, held_names: list[str]) -> bool:
    normalized = normalize_property_name(property_name)
    return any(
        normalized
        and held_normalized
        and (normalized in held_normalized or held_normalized in normalized)
        for held_normalized in (normalize_property_name(name) for name in held_names)
    )


def excluded_by_manual_policy(property_name: object, property_path: object = None) -> str:
    haystack = f"{property_name or ''}\n{property_path or ''}".lower()
    for excluded in DEFAULT_MANUAL_EXCLUDED_PROPERTIES:
        if excluded.lower() in haystack:
            return excluded
    targets = [
        normalize_property_name(property_name),
        normalize_property_name(property_path),
    ]
    for record in sold_policy_exclusion_records(DEFAULT_LISTING_UPDATE_POLICY_PATH):
        excluded = str(record.get("full_address") or record.get("property_name") or "").strip()
        excluded_key = str(record.get("normalized_property") or "").strip()
        if excluded_key and any(policy_name_matches(target, excluded_key) for target in targets if target):
            return excluded
    return ""


def live_capture_excluded_property_names(path: Path | None) -> set[str]:
    if path is None:
        return set()
    data = read_json(path)
    records = data.get("skipped_index_records") if isinstance(data.get("skipped_index_records"), list) else []
    excluded: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        status = str(record.get("raw_status") or record.get("status") or "").strip().lower()
        if status not in {"skipped_sold", "skipped_closed"}:
            continue
        property_name = str(record.get("property_name") or "").strip()
        if not property_name:
            property_path = Path(str(record.get("property_path") or record.get("input_property_path") or "").strip())
            property_name = property_path.name
            if property_name.lower() == "public":
                property_name = property_path.parent.name
        normalized = normalize_property_name(property_name)
        if normalized and normalized != "public":
            excluded.add(normalized)
    return excluded


def discord_review_provenance(discord_send_report: Path | None, discord_plan_validation_report: Path | None) -> dict[str, Any]:
    send_report = read_json(discord_send_report) if discord_send_report else {}
    validation_report = read_json(discord_plan_validation_report) if discord_plan_validation_report else {}
    digest = str(send_report.get("plan_digest") or validation_report.get("plan_digest") or "")
    return {
        "discord_send_report": str(discord_send_report) if discord_send_report else None,
        "discord_plan_validation_report": str(discord_plan_validation_report) if discord_plan_validation_report else None,
        "discord_all_property_send_plan_digest": digest,
        "discord_all_property_send_plan_digest_ok": bool(SHA256_RE.fullmatch(digest)),
        "discord_all_property_send_status": send_report.get("status"),
        "discord_all_property_send_record_count": send_report.get("record_count"),
        "discord_plan_validation_status": validation_report.get("status"),
        "discord_financial_review_issue_count": validation_report.get("financial_review_issue_count"),
        "discord_financial_review_issues": validation_report.get("financial_review_issues") or [],
        "discord_financial_review_blocked_properties": validation_report.get("financial_review_blocked_properties") or [],
    }


def property_send_key(prop: dict[str, Any]) -> str:
    prop_id = str(prop.get("lofty_property_id") or "").strip()
    if prop_id:
        return f"id:{prop_id}"
    name = re.sub(r"\s+", " ", str(prop.get("property_name") or "").strip().lower())
    return f"name:{name}" if name else ""


def packet_property_send_keys(packet: dict[str, Any]) -> list[str]:
    properties = packet.get("source_properties") if isinstance(packet.get("source_properties"), list) else []
    keys = [property_send_key(prop) for prop in properties if isinstance(prop, dict)]
    if not keys:
        keys = [f"id:{str(prop_id).strip()}" for prop_id in packet.get("property_ids") or [] if str(prop_id or "").strip()]
    deduped: list[str] = []
    for key in keys:
        if key and key not in deduped:
            deduped.append(key)
    return deduped


def sent_state_property_map(state: dict[str, Any]) -> dict[str, str]:
    raw = state.get("property_sent_at")
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items() if str(key or "").strip() and str(value or "").strip()}
    return {}


def sent_state_property_month_map(state: dict[str, Any]) -> dict[str, str]:
    raw = state.get("property_sent_month")
    if isinstance(raw, dict):
        return {
            str(key): str(value)
            for key, value in raw.items()
            if str(key or "").strip() and re.fullmatch(r"\d{4}-\d{2}", str(value or "").strip())
        }
    return {}


def publish_state_run_month(state: dict[str, Any]) -> str:
    for field in ("last_sent_run_month", "last_sent_month"):
        value = str(state.get(field) or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}", value):
            return value
    text = str(state.get("last_sent_text") or "")
    matches = re.findall(r"(?:Cash Flow Snapshot|Monthly Cash Position|for)\s*\(?([12]\d{3}-\d{2})\)?", text, re.I)
    if not matches:
        matches = re.findall(r"\b([12]\d{3}-\d{2})\b", text)
    if matches:
        return matches[-1]
    sent_at = parse_iso_datetime(state.get("last_sent_at"))
    if sent_at and sent_at.day <= 15:
        prior_month_end = sent_at.replace(day=1) - timedelta(days=1)
        return prior_month_end.strftime("%Y-%m")
    return ""


def publish_state_has_owner_email_send_evidence(state: dict[str, Any]) -> bool:
    if str(state.get("last_sent_text") or "").strip():
        return True
    try:
        if int(state.get("last_sent_entry_count") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return state.get("owner_email_sent") is True or state.get("native_owner_email_sent") is True


def reconstruct_property_sent_state(runtime_map: Path, state: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    merged = dict(state) if isinstance(state, dict) else {}
    sent_at_by_property = sent_state_property_map(merged)
    sent_month_by_property = sent_state_property_month_map(merged)
    runtime = read_json(runtime_map)
    properties = runtime.get("properties") if isinstance(runtime.get("properties"), list) else []
    evidence: list[dict[str, str]] = []
    for prop in properties:
        if not isinstance(prop, dict):
            continue
        key = property_send_key(prop)
        updates_md = Path(str(prop.get("updates_md") or ""))
        publish_state_file = updates_md.parent / ".lofty_publish_state.json" if str(updates_md) else None
        if not key or publish_state_file is None or not publish_state_file.is_file():
            continue
        publish_state = read_json(publish_state_file)
        if not publish_state_has_owner_email_send_evidence(publish_state):
            continue
        sent_at = str(publish_state.get("last_sent_at") or "").strip()
        sent_month = publish_state_run_month(publish_state)
        if not sent_at or not sent_month:
            continue
        existing_sent_at = parse_iso_datetime(sent_at_by_property.get(key))
        reconstructed_sent_at = parse_iso_datetime(sent_at)
        if existing_sent_at is None or (reconstructed_sent_at and reconstructed_sent_at > existing_sent_at):
            sent_at_by_property[key] = sent_at
            sent_month_by_property[key] = sent_month
        elif key not in sent_month_by_property:
            sent_month_by_property[key] = sent_month
        evidence.append(
            {
                "property_send_key": key,
                "run_month": sent_month,
                "sent_at": sent_at,
                "publish_state_file": str(publish_state_file),
            }
        )
    merged["property_sent_at"] = sent_at_by_property
    merged["property_sent_month"] = sent_month_by_property
    return merged, evidence


def packets_sent_for_run_month(packets: list[dict[str, Any]], state: dict[str, Any], run_month: str) -> tuple[bool, list[str]]:
    target_keys = sorted({key for packet in packets for key in packet_property_send_keys(packet)})
    if not target_keys:
        return False, []
    if str(state.get("run_month") or "") == run_month:
        return True, target_keys
    sent_month_by_property = sent_state_property_month_map(state)
    sent_keys = [key for key in target_keys if sent_month_by_property.get(key) == run_month]
    return len(sent_keys) == len(target_keys), sent_keys


def property_cooldown_issues(
    packets: list[dict[str, Any]],
    state: dict[str, Any],
    now: datetime | None = None,
) -> list[str]:
    now = now or datetime.now(timezone.utc)
    sent_at_by_property = sent_state_property_map(state)
    issues: list[str] = []
    for packet in packets:
        keys = packet_property_send_keys(packet)
        if len(keys) != 1:
            issues.append(f"owner_email_packet_must_target_one_property:{len(keys)}")
            continue
        sent_at = parse_iso_datetime(sent_at_by_property.get(keys[0]))
        if sent_at and now - sent_at < timedelta(days=PROPERTY_EMAIL_COOLDOWN_DAYS):
            issues.append(f"property_email_cooldown_active:{keys[0]}")
    return issues


def property_cooldown_hold_keys(
    packets: list[dict[str, Any]],
    state: dict[str, Any],
    now: datetime | None = None,
) -> tuple[list[str], set[str]]:
    issues = property_cooldown_issues(packets, state, now)
    hold_keys = {
        issue.split("property_email_cooldown_active:", 1)[1]
        for issue in issues
        if issue.startswith("property_email_cooldown_active:")
    }
    return issues, hold_keys


def parse_iso_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z") and re.search(r"[+-]\d{2}:\d{2}Z$", raw):
        raw = raw[:-1]
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_month_matches(value: object, run_month: str) -> bool:
    parsed = parse_iso_datetime(value)
    return parsed is not None and parsed.strftime("%Y-%m") == run_month


def guild_test_post_guard(guild_test_post_report: Path | None, run_month: str, *, required: bool) -> dict[str, Any]:
    report = read_json(guild_test_post_report) if guild_test_post_report else {"status": "missing", "path": None}
    target = str(report.get("target") or "")
    expected_channel_id = target.removeprefix("channel:") if target.startswith("channel:") else ""
    posted_message_id = str(report.get("posted_message_id") or "")
    posted_channel_id = str(report.get("posted_channel_id") or "")
    issues: list[str] = []
    if required:
        if report.get("status") != "ok":
            issues.append(f"guild_test_post_status={report.get('status') or 'missing'}")
        if report.get("valid") is not True:
            issues.append("guild_test_post_valid=false")
        if report.get("posted") is not True:
            issues.append("guild_test_post_posted=false")
        if report.get("run_month") != run_month:
            issues.append(f"guild_test_post_run_month={report.get('run_month') or 'missing'}")
        if not DISCORD_SNOWFLAKE_RE.fullmatch(posted_message_id):
            issues.append("guild_test_post_message_id_missing_or_invalid")
        if not DISCORD_SNOWFLAKE_RE.fullmatch(posted_channel_id):
            issues.append("guild_test_post_channel_id_missing_or_invalid")
        if expected_channel_id and posted_channel_id != expected_channel_id:
            issues.append("guild_test_post_channel_mismatch")
        if not iso_month_matches(report.get("posted_at"), run_month):
            issues.append("guild_test_post_posted_at_missing_or_wrong_month")
        if not SHA256_RE.fullmatch(str(report.get("digest") or "")):
            issues.append("guild_test_post_digest_missing_or_invalid")
    return {
        "required": required,
        "path": str(guild_test_post_report) if guild_test_post_report else None,
        "status": report.get("status"),
        "valid": report.get("valid") is True,
        "posted": report.get("posted") is True,
        "run_month": report.get("run_month"),
        "posted_at": report.get("posted_at"),
        "posted_message_id": posted_message_id or None,
        "posted_channel_id": posted_channel_id or None,
        "target": target or None,
        "expected_channel_id": expected_channel_id or None,
        "digest": report.get("digest"),
        "issues": issues,
        "ok": not issues,
    }


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def email_digest(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def recipient_template_path(recipients_csv: Path) -> Path:
    suffix = recipients_csv.suffix or ".csv"
    return recipients_csv.with_name(f"{recipients_csv.stem}.example{suffix}")


def write_recipient_template(recipients_csv: Path) -> tuple[Path, bool]:
    template = recipient_template_path(recipients_csv)
    if template.exists():
        return template, False
    template.parent.mkdir(parents=True, exist_ok=True)
    with template.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RECIPIENT_CSV_FIELDS)
        writer.writeheader()
        writer.writerow(
            {
                "name": "Investor Name",
                "email": "",
                "property_name": "Exact active property name, or leave blank when using lofty_property_id",
                "lofty_property_id": "",
            }
        )
    return template, True


def reason_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        reason = str(record.get("reason") or "unknown").strip() or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def compact_count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def write_property_gap_csv(path: Path, records: list[dict[str, Any]]) -> tuple[Path, bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PROPERTY_GAP_CSV_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in PROPERTY_GAP_CSV_FIELDS})
    return path, True


def cleanup_artifact_path(raw_path: object, listing_cleanup_queue_report: Path | None) -> str:
    text = str(raw_path or "").strip()
    if not text:
        return ""
    if not listing_cleanup_queue_report:
        return text
    sibling = listing_cleanup_queue_report.parent / Path(text).name
    if sibling.is_file() and str(sibling) != text:
        return str(sibling)
    return text


def normalize_blocker(blocker: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(blocker)
    blocker_class = str(normalized.get("class") or normalized.get("id") or "unknown").strip() or "unknown"
    blocker_text = str(normalized.get("blocker") or blocker_class).strip() or blocker_class
    normalized.setdefault("class", blocker_class)
    normalized.setdefault("id", blocker_class)
    normalized.setdefault("summary", blocker_text)
    return normalized


def owner_packet_actionable_summary(
    *,
    issues: list[str],
    packets: list[dict[str, Any]],
    property_unavailable: list[dict[str, Any]],
    property_gap_csv: Path,
    recipients_csv: Path,
    recipient_template: Path,
    listing_cleanup_queue_report: Path | None,
    live_update_capture_report: Path | None,
    run_month: str,
    eligible_financial_summary_missing_count: int | None = None,
    financially_held_property_count: int = 0,
) -> dict[str, Any]:
    counts = reason_counts(property_unavailable)
    primary: dict[str, Any]
    cleanup_count = counts.get("listing_history_cleanup_required", 0)
    empty_count = counts.get("updates_md_empty", 0)
    live_guard_count = counts.get("live_update_guard_not_reconciled", 0)
    body_guard_count = counts.get("latest_update_body_guard", 0)
    unavailable_financial_summary_missing_count = sum(
        1 for record in property_unavailable if record.get("monthly_financial_summary_present") is False
    )
    financial_summary_missing_count = (
        unavailable_financial_summary_missing_count
        if eligible_financial_summary_missing_count is None
        else max(0, int(eligible_financial_summary_missing_count))
    )
    candidate_update_source_count = sum(
        1 for record in property_unavailable if str(record.get("candidate_update_source") or "").strip()
    )
    candidate_update_approval_target_count = sum(
        1 for record in property_unavailable if str(record.get("candidate_update_approval_target") or "").strip()
    )
    listing_cleanup_report = read_json(listing_cleanup_queue_report) if listing_cleanup_queue_report else {}
    live_capture_report = read_json(live_update_capture_report) if live_update_capture_report else {}
    ready_cleanup_csv = cleanup_artifact_path(listing_cleanup_report.get("ready_cleanup_csv"), listing_cleanup_queue_report)
    ready_cleanup_commands = cleanup_artifact_path(listing_cleanup_report.get("dry_run_commands_file"), listing_cleanup_queue_report)
    live_apply_commands = cleanup_artifact_path(
        listing_cleanup_report.get("live_apply_commands_requires_explicit_approval_file"),
        listing_cleanup_queue_report,
    )
    ready_cleanup_count = compact_count(listing_cleanup_report.get("ready_listing_cleanup_count"))
    ready_cleanup_unavailable = [
        record
        for record in property_unavailable
        if record.get("listing_cleanup_queue_status") == "ready_listing_cleanup"
    ]
    ready_cleanup_other_blocker_count = sum(
        1 for record in ready_cleanup_unavailable if record.get("reason") != "listing_history_cleanup_required"
    )
    ready_cleanup_other_blocker_reason_counts = reason_counts(
        [record for record in ready_cleanup_unavailable if record.get("reason") != "listing_history_cleanup_required"]
    )
    if not issues:
        primary = normalize_blocker({
            "class": "ok",
            "blocker": "none",
            "count": 0,
            "artifact": None,
            "next_action": "No packet action required.",
            "hold": "none",
        })
    elif cleanup_count:
        cleanup_blocker = f"{cleanup_count} Lofty listing update fields contain copied UPDATES.md history"
        if ready_cleanup_count and ready_cleanup_count != cleanup_count:
            cleanup_blocker = (
                f"{ready_cleanup_count} listing cleanup commands are ready; "
                f"{cleanup_count} direct listing-history blockers"
            )
            if ready_cleanup_other_blocker_count:
                cleanup_blocker += f"; {ready_cleanup_other_blocker_count} also blocked by local update readiness"
        primary = normalize_blocker({
            "class": "listing_history_cleanup_required",
            "blocker": cleanup_blocker,
            "count": cleanup_count,
            "artifact": ready_cleanup_csv or str(property_gap_csv),
            "command": ready_cleanup_commands or None,
            "approval_required_artifact": live_apply_commands or None,
            "next_action": (
                "Review cleaned-history repair CSV and dry-run commands; apply live listing cleanup only after explicit approval, "
                "then rerun owner email packet dry-run. Investor email remains blocked until live listing guards reconcile."
            ),
            "hold": "Lofty PM publish and investor email",
        })
    elif empty_count:
        primary = normalize_blocker({
            "class": "updates_md_empty",
            "blocker": f"{empty_count} active properties have empty UPDATES.md",
            "count": empty_count,
            "artifact": str(property_gap_csv),
            "next_action": (
                f"Review candidate updates for {run_month}; copy approved update artifacts only after current rent-roll/readiness approval, "
                "then rerun owner email packet dry-run."
            ),
            "hold": "Lofty PM publish and investor email",
        })
    elif live_guard_count:
        live_review_blockers = [
            str(item)
            for item in live_capture_report.get("review_blockers") or []
            if str(item or "").strip()
        ]
        live_capture_planned = any(
            blocker.startswith("live_update_capture_not_applied")
            or blocker.startswith("live_update_planned_count=")
            or blocker.startswith("live_update_unverified_count=")
            for blocker in live_review_blockers
        )
        if live_capture_planned:
            planned_count = compact_count(live_capture_report.get("planned_count"))
            unverified_count = compact_count(live_capture_report.get("unverified_count"))
            live_capture_target_count = compact_count(live_capture_report.get("target_count"))
            target_count = live_guard_count or live_capture_target_count or planned_count or unverified_count
            next_action = live_capture_report.get("next_action") if isinstance(live_capture_report.get("next_action"), dict) else {}
            primary = normalize_blocker({
                "class": "live_update_capture_required",
                "blocker": f"{live_guard_count}/{target_count} owner-email live UPDATES.md guards need authenticated capture",
                "count": live_guard_count,
                "live_capture_target_count": live_capture_target_count,
                "live_capture_planned_count": planned_count,
                "live_capture_unverified_count": unverified_count,
                "artifact": str(live_update_capture_report) if live_update_capture_report else str(property_gap_csv),
                "command": live_capture_report.get("rerun_command") or next_action.get("rerun_command"),
                "next_action": (
                    str(next_action.get("summary") or "").strip()
                    or "Capture authenticated live Lofty PM UPDATES.md guards, then rerun owner email packet dry-run."
                ),
                "hold": "Lofty PM publish and investor email",
            })
        else:
            primary = normalize_blocker({
                "class": "live_update_guard_not_reconciled",
                "blocker": f"{live_guard_count} live update guards are not reconciled",
                "count": live_guard_count,
                "artifact": str(property_gap_csv),
                "next_action": "Reconcile live/local UPDATES.md guard diffs, then rerun owner email packet dry-run.",
                "hold": "Lofty PM publish and investor email",
            })
    elif financial_summary_missing_count:
        primary = normalize_blocker({
            "class": "monthly_financial_summary_missing",
            "blocker": (
                f"{financial_summary_missing_count} active properties lack verified Lofty reserve and ECO GL cash summaries"
            ),
            "count": financial_summary_missing_count,
            "artifact": str(property_gap_csv),
            "next_action": (
                f"Regenerate the {run_month} review candidate packet from current Lofty/runtime-map and ECO GL data; "
                "only latest approved snippets with verified cash summaries may proceed to listing or email."
            ),
            "hold": "Lofty PM publish and investor email",
        })
    elif financially_held_property_count:
        primary = normalize_blocker({
            "class": "financial_review_hold",
            "blocker": f"{financially_held_property_count} active properties are held for financial review",
            "count": financially_held_property_count,
            "artifact": str(property_gap_csv),
            "next_action": (
                "Resolve the current financial-review blockers and rerun the owner email packet dry-run; "
                "no property owner email may send before the finance gate is clean."
            ),
            "hold": "Lofty PM publish and investor email",
        })
    elif body_guard_count:
        primary = normalize_blocker({
            "class": "latest_update_body_guard",
            "blocker": f"{body_guard_count} active properties need approved latest owner update copy",
            "count": body_guard_count,
            "artifact": str(property_gap_csv),
            "next_action": (
                f"Review candidate updates for {run_month}; copy approved latest owner-update artifacts after current "
                "rent-roll/readiness approval, then rerun owner email packet dry-run."
            ),
            "hold": "Lofty PM publish and investor email",
        })
    elif not packets:
        primary = normalize_blocker({
            "class": "no_packets_generated",
            "blocker": "no recipient email packets generated",
            "count": 1,
            "artifact": str(property_gap_csv),
            "next_action": "Resolve packet inputs and rerun owner email packet dry-run.",
            "hold": "investor email",
        })
    else:
        primary = normalize_blocker({
            "class": "review",
            "blocker": "owner email packet review",
            "count": len(issues),
            "artifact": str(property_gap_csv),
            "next_action": "Resolve owner email packet issues and rerun dry-run.",
            "hold": "investor email",
        })
    secondary_blockers = []
    return {
        "actionable_blocker_count": (0 if primary.get("class") == "ok" else 1) + len(secondary_blockers),
        "audit_issue_count": len(issues),
        "property_unavailable_reason_counts": counts,
        "primary_blocker": primary,
        "secondary_blockers": secondary_blockers,
        "recipient_readiness": {
            "recipient_csv_exists": recipients_csv.is_file(),
            "recipient_csv": str(recipients_csv),
            "recipient_template_csv": str(recipient_template),
            "recipient_blocker_class": None,
            "native_recipient_source": "lofty_pm_mcp",
            "non_native_recipient_csv_exists": recipients_csv.is_file(),
        },
        "candidate_update_source_count": candidate_update_source_count,
        "candidate_update_approval_target_count": candidate_update_approval_target_count,
        "listing_cleanup_ready_count_from_queue": ready_cleanup_count,
        "listing_cleanup_ready_direct_blocker_count": cleanup_count,
        "listing_cleanup_ready_other_blocker_count": ready_cleanup_other_blocker_count,
        "listing_cleanup_ready_other_blocker_reason_counts": ready_cleanup_other_blocker_reason_counts,
        "packet_count": len(packets),
        "noise_policy": "Use primary_blocker for action; full per-property evidence remains in property_gap_csv and property_unavailable_bounded.",
        "external_mutation_policy": "This packet report is non-mutating and cannot send email or update Lofty listings by itself.",
    }


def parse_latest_entry(md_text: str) -> dict[str, str] | None:
    stripped = md_text.strip()
    first_marker = PROPERTY_UPDATE_ENTRY_RE.search(stripped)
    first_dated_heading = DATED_UPDATE_HEADING_RE.search(stripped)
    if first_marker and (not first_dated_heading or first_marker.start() < first_dated_heading.start()):
        match = PROPERTY_UPDATE_ENTRY_RE.search(stripped[first_marker.start() :])
        if match:
            body = trim_after_first_property_update(match.group(2).strip(), current_marker_included=False)
            if body:
                return {"date": match.group(1), "body": f"- Property Update ({match.group(1)}):\n{body}".strip()}
    parts = re.split(r"(?m)^##\s+(\d{4}-\d{2}-\d{2})\s*$", md_text)
    if len(parts) >= 3:
        date = parts[1].strip()
        body = trim_after_first_property_update(parts[2].strip())
        if body:
            return {"date": date, "body": body}
    match = PROPERTY_UPDATE_ENTRY_RE.search(stripped)
    if match:
        body = trim_after_first_property_update(match.group(2).strip(), current_marker_included=False)
        if body:
            return {"date": match.group(1), "body": f"- Property Update ({match.group(1)}):\n{body}".strip()}
    return None


def trim_after_first_property_update(text: str, *, current_marker_included: bool = True) -> str:
    matches = list(PROPERTY_UPDATE_MARKER_RE.finditer(text))
    if matches:
        trim_match = matches[1] if current_marker_included and len(matches) > 1 else matches[0] if not current_marker_included else None
        if trim_match is not None:
            text = text[: trim_match.start()].rstrip()
    dated_headings = list(DATED_UPDATE_HEADING_RE.finditer(text))
    if dated_headings:
        text = text[: dated_headings[0].start()].rstrip()
    updates_headers = list(PROPERTY_UPDATES_HEADER_RE.finditer(text))
    if updates_headers:
        text = text[: updates_headers[0].start()].rstrip()
    return text.strip()


def one_update_only(text: str) -> bool:
    return len(PROPERTY_UPDATE_MARKER_RE.findall(text)) <= 1


def full_history_guard_issues(text: str, *, allowed_property_update_markers: int) -> list[str]:
    text = text or ""
    issues: list[str] = []
    marker_count = len(PROPERTY_UPDATE_MARKER_RE.findall(text))
    dated_heading_count = len(DATED_UPDATE_HEADING_RE.findall(text))
    updates_header_count = len(PROPERTY_UPDATES_HEADER_RE.findall(text))
    if marker_count > allowed_property_update_markers:
        issues.append(f"property_update_marker_count={marker_count}>allowed={allowed_property_update_markers}")
    if dated_heading_count:
        issues.append(f"dated_update_heading_count={dated_heading_count}")
    if updates_header_count:
        issues.append(f"full_updates_header_count={updates_header_count}")
    return issues


def canonical_property_update_guard_issues(text: str, *, required_property_update_markers: int) -> list[str]:
    marker_count = len(PROPERTY_UPDATE_MARKER_RE.findall(text or ""))
    if marker_count == required_property_update_markers:
        return []
    return [f"property_update_marker_count={marker_count}!=required={required_property_update_markers}"]


def normalized_leak_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "")
    return text.strip().lower()


def date_variants(value: str) -> set[str]:
    raw = str(value or "").strip()
    if not raw:
        return set()
    variants = {raw}
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed = datetime.strptime(raw, "%Y-%m-%d")
            variants.add(parsed.strftime("%m/%d/%Y"))
        elif re.fullmatch(r"\d{2}/\d{2}/\d{4}", raw):
            parsed = datetime.strptime(raw, "%m/%d/%Y")
            variants.add(parsed.strftime("%Y-%m-%d"))
    except ValueError:
        pass
    return variants


def source_history_dates(md_text: str, latest_date: str) -> list[str]:
    latest_variants = date_variants(latest_date)
    dates: set[str] = set()
    dates.update(DATED_UPDATE_HEADING_DATE_RE.findall(md_text or ""))
    dates.update(match.group(1) for match in PROPERTY_UPDATE_ENTRY_RE.finditer(md_text or ""))
    history = []
    for date in sorted(dates):
        if date not in latest_variants:
            history.append(date)
    return history


def source_updates_metadata(md_text: str, latest_date: str) -> dict[str, Any]:
    marker_count = len(PROPERTY_UPDATE_MARKER_RE.findall(md_text or ""))
    dated_heading_count = len(DATED_UPDATE_HEADING_RE.findall(md_text or ""))
    header_count = len(PROPERTY_UPDATES_HEADER_RE.findall(md_text or ""))
    history_dates = source_history_dates(md_text, latest_date)
    return {
        "source_updates_md_char_count": len(md_text or ""),
        "source_updates_md_line_count": len((md_text or "").splitlines()),
        "source_updates_md_property_update_marker_count": marker_count,
        "source_updates_md_dated_heading_count": dated_heading_count,
        "source_updates_md_header_count": header_count,
        "source_updates_md_history_dates": history_dates,
        "source_updates_md_has_history": bool(history_dates or marker_count > 1 or dated_heading_count > 1 or header_count),
    }


def source_full_history_leak_guard_issues(text: str, properties: list[dict[str, Any]] | None) -> list[str]:
    issues: list[str] = []
    if not properties:
        return issues
    normalized_body = normalized_leak_text(text)
    for prop in properties:
        if not isinstance(prop, dict) or not prop.get("source_updates_md_has_history"):
            continue
        label = str(prop.get("lofty_property_id") or prop.get("property_name") or "unknown").replace(" ", "_")
        source_text = str(prop.get("_source_updates_md_text") or "")
        normalized_source = normalized_leak_text(source_text)
        if (
            len(normalized_source) >= MIN_FULL_SOURCE_LEAK_CHARS
            and normalized_source in normalized_body
        ):
            issues.append(f"full_source_updates_md_embedded:{label}")
        for date in prop.get("source_updates_md_history_dates") or []:
            if any(variant in text for variant in date_variants(str(date))):
                issues.append(f"historical_update_date_leaked:{label}:{date}")
    return issues


def latest_source_scope_guard_issues(text: str, properties: list[dict[str, Any]] | None) -> list[str]:
    issues: list[str] = []
    if not properties:
        return issues
    normalized_body = normalized_leak_text(text)
    for prop in properties:
        if not isinstance(prop, dict):
            continue
        label = str(prop.get("lofty_property_id") or prop.get("property_name") or "unknown").replace(" ", "_")
        latest_body = str(prop.get("latest_update_body") or "")
        normalized_latest = normalized_leak_text(latest_body)
        normalized_source = normalized_leak_text(str(prop.get("_source_updates_md_text") or ""))
        if not normalized_latest:
            issues.append(f"latest_update_body_missing_from_source:{label}")
            continue
        if normalized_latest not in normalized_body:
            issues.append(f"latest_update_body_missing_from_email:{label}")
        if prop.get("source_updates_md_has_history") and normalized_source:
            if normalized_latest == normalized_source or normalized_source in normalized_latest:
                issues.append(f"latest_update_body_contains_full_source_updates_md:{label}")
    return issues


def monthly_financial_summary_guard_issues(
    text: str,
    *,
    required_summary_count: int,
    require_financials_md_summary: bool = False,
    run_month: str | None = None,
) -> list[str]:
    if required_summary_count <= 0:
        return []
    issues: list[str] = []
    financials_md_count = len(FINANCIALS_MD_SUMMARY_RE.findall(text or ""))
    eco_count = len(SPENDABLE_ECO_SUMMARY_RE.findall(text or ""))
    if require_financials_md_summary and financials_md_count < required_summary_count:
        issues.append(f"financials_md_summary_count={financials_md_count}<required={required_summary_count}")
    if financials_md_count and not financials_md_text_matches_run_month(text, run_month):
        issues.append(f"financials_md_summary_run_month_mismatch_or_missing:{run_month or 'unknown'}")
    if eco_count < required_summary_count:
        issues.append(f"eco_gl_column_e_summary_count={eco_count}<required={required_summary_count}")
    for snippet in DISALLOWED_LIMITED_FINANCIAL_SUMMARY_SNIPPETS:
        if snippet in (text or ""):
            issues.append(f"disallowed_limited_financial_summary:{snippet}")
    return issues


def monthly_financial_summary_present(text: str, run_month: str | None = None) -> bool:
    return not monthly_financial_summary_guard_issues(text, required_summary_count=1, run_month=run_month)


def internal_owner_update_guard_issues(text: str) -> list[str]:
    if INTERNAL_OWNER_UPDATE_CONTENT_RE.search(text or ""):
        return ["internal_owner_update_content_detected"]
    return []


def operational_report_guard_issues(text: str) -> list[str]:
    if OPERATIONAL_REPORT_CONTENT_RE.search(text or ""):
        return ["operational_report_content_detected"]
    return []


def format_money(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "Not available"
    prefix = "-$" if amount < 0 else "$"
    return f"{prefix}{abs(amount):,.2f}"


def verified_candidate_summary(summary: dict[str, Any]) -> bool:
    return (
        isinstance(summary, dict)
        and summary.get("eco_gl_column_e_status") == "ok"
        and summary.get("eco_gl_column_e_sum") is not None
        and summary.get("eco_held_unrestricted_cash_status") == "ok"
        and summary.get("eco_held_unrestricted_cash") is not None
    )


def candidate_gap_fields(candidate_summary: dict[str, Any] | None, candidate_summary_report: dict[str, Any]) -> dict[str, Any]:
    if not candidate_summary:
        return {}
    return {
        "candidate_summary_source": candidate_summary_report.get("path"),
        "candidate_update_source": candidate_summary.get("candidate_update_source"),
        "candidate_update_approval_target": candidate_summary.get("candidate_update_approval_target"),
        "candidate_financial_approval_target": candidate_summary.get("candidate_financial_approval_target"),
    }


def candidate_lookup_key(value: object) -> str:
    return str(value or "").strip().lower()


def monthly_artifact_property_folder(path_text: object) -> str:
    path = Path(str(path_text or ""))
    if not str(path).strip():
        return ""
    property_folder = path.parent.parent
    if property_folder.name.lower() == "public":
        property_folder = property_folder.parent
    return property_folder.name


def markdown_section(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ms)^##\s+{re.escape(heading)}(?:\s+\([^)]+\))?\s*\n.*?(?=^##\s+|\Z)"
    )
    match = pattern.search(text or "")
    return match.group(0).strip() if match else ""


def plain_text_financials_section(section: str) -> str:
    lines: list[str] = []
    skip_table_rule = False
    for raw_line in str(section or "").splitlines():
        line = raw_line.strip()
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        if re.match(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$", line):
            skip_table_rule = True
            continue
        if line.startswith("|") and line.endswith("|"):
            cells = [cell.strip().replace("`", "") for cell in line.strip("|").split("|")]
            if not cells:
                continue
            lowered = [cell.lower() for cell in cells]
            if lowered in (["metric", "amount", "source"], ["metric", "amount"], ["field", "value"]):
                continue
            if len(cells) >= 3:
                lines.append(f"{cells[0]}: {cells[1]} ({cells[2]})")
            elif len(cells) == 2:
                lines.append(f"{cells[0]}: {cells[1]}")
            continue
        if skip_table_rule and line.startswith("|"):
            continue
        lines.append(line.replace("`", ""))
    return "\n".join(lines).strip()


def render_financials_md_summary(summary: dict[str, Any]) -> str:
    text = str(summary.get("financials_md_text") or "").strip()
    if not text:
        return ""
    sections = [
        section
        for section in (
            markdown_section(text, "Cash Flow Snapshot"),
            markdown_section(text, "Monthly Cash Position"),
            markdown_section(text, "Source Evidence"),
        )
        if section
    ]
    if not sections:
        return ""
    return "Financial detail:\n\n" + "\n\n".join(
        plain_text_financials_section(section) for section in sections
    )


def financials_md_text_matches_run_month(text: str, run_month: str | None) -> bool:
    expected = str(run_month or "").strip()
    if not expected:
        return True
    return bool(re.search(rf"(?m)^##\s+(?:Cash Flow Snapshot|Monthly Cash Position|Source Evidence)\s*\({re.escape(expected)}\)\s*$", text or ""))


def render_monthly_financial_summary(summary: dict[str, Any], run_month: str | None = None) -> str:
    as_of_month = summary.get("as_of_month") or run_month
    as_of = f" as of {as_of_month}" if as_of_month else ""
    lines = [
        f"Financial summary{as_of}:",
    ]
    if summary.get("lofty_curr_maintenance_reserve") is not None:
        lines.append(
            f"- Lofty maintenance reserve balance: {format_money(summary.get('lofty_curr_maintenance_reserve'))}"
        )
    lines.append(
        "- ECO Net DAO Funds (spendable cash held by ECO): "
        f"{format_money(summary.get('eco_held_unrestricted_cash'))}"
    )
    if summary.get("open_accrued_obligations_status") == "ok":
        lines.append(
            "- Recorded unpaid obligations (not spendable cash): "
            f"{format_money(summary.get('open_accrued_obligations'))}"
        )
    return "\n".join(lines)


SYNTHETIC_FINANCIAL_SUMMARY_BLOCK_RE = re.compile(
    r"(?ms)\n*Financial summary(?:\s+as\s+of\s+[^:\n]+)?:\s*\n\s*-\s*Lofty-held current maintenance reserve:.*?\n\s*-\s*ECO GL Column E sum:.*?(?=\n\s*##|\Z)"
)
LIMITED_CASH_POSITION_SENTENCE_RE = re.compile(
    r"(?s)\n*This month's update is limited to verified cash-position data from Lofty and ECO records\."
    r"\s*No tenant ledger rows are included\.\s*"
)


def append_verified_financial_summary_if_missing(
    text: str,
    summary: dict[str, Any] | None,
    run_month: str | None = None,
) -> tuple[str, bool]:
    if not summary or not verified_candidate_summary(summary):
        return text, False
    rendered_summary = render_monthly_financial_summary(summary, run_month)
    if not rendered_summary:
        return text, False
    if summary.get("financials_md_text"):
        text = SYNTHETIC_FINANCIAL_SUMMARY_BLOCK_RE.sub("", text).rstrip()
        text = LIMITED_CASH_POSITION_SENTENCE_RE.sub("\n", text).rstrip()
        if any(marker in text for marker in ("Financial detail:", "Financial summary from FINANCIALS.md:")):
            return text, False
        return text.rstrip() + "\n\n" + rendered_summary + "\n", True
    if not monthly_financial_summary_guard_issues(text, required_summary_count=1, run_month=run_month):
        return text, False
    return text.rstrip() + "\n\n" + rendered_summary + "\n", True


def load_candidate_financial_summaries(
    candidate_packet_report: Path | None,
    run_month: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, Any]]:
    if candidate_packet_report is None:
        return {}, [], {"path": None, "status": "not_configured", "record_count": 0, "verified_summary_count": 0}
    data = read_json(candidate_packet_report)
    if data.get("status") in {"missing", "unreadable"}:
        return {}, [f"review candidate packet {data.get('status')}: {candidate_packet_report}"], {
            "path": str(candidate_packet_report),
            "status": data.get("status"),
            "record_count": 0,
            "verified_summary_count": 0,
        }
    candidate_run_month = str(data.get("run_month") or "").strip()
    expected_run_month = str(run_month or "").strip()
    if candidate_run_month and expected_run_month and candidate_run_month != expected_run_month:
        return {}, [f"review candidate packet run_month mismatch: {candidate_run_month}!={expected_run_month}"], {
            "path": str(candidate_packet_report),
            "status": data.get("status"),
            "run_month": candidate_run_month,
            "run_month_matches": False,
            "record_count": 0,
            "verified_summary_count": 0,
        }
    records = data.get("records") if isinstance(data.get("records"), list) else []
    load_issues: list[str] = []
    manifest_source_issues = data.get("review_manifest_source_issues")
    if isinstance(manifest_source_issues, list):
        load_issues.extend(f"review_manifest_source_issue={issue}" for issue in manifest_source_issues if issue)
    manifest_record_count = int(data.get("manifest_record_count") or 0)
    skipped_excluded_record_count = int(data.get("skipped_excluded_record_count") or 0)
    expected_record_count = max(0, manifest_record_count - skipped_excluded_record_count) if manifest_record_count else 0
    if expected_record_count and len(records) < expected_record_count:
        load_issues.append(f"candidate_packet_records_partial={len(records)}/{expected_record_count}")
    summaries: dict[str, dict[str, Any]] = {}
    verified_count = 0
    financials_md_rejected_count = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        summary = record.get("monthly_financial_summary") if isinstance(record.get("monthly_financial_summary"), dict) else {}
        if not verified_candidate_summary(summary):
            continue
        verified_count += 1
        summary = {
            **summary,
            "candidate_packet_record_property_name": record.get("property_name"),
            "candidate_update_source": record.get("update_candidate"),
            "candidate_update_approval_target": record.get("update_approval_target"),
            "candidate_financial_approval_target": record.get("financial_approval_target"),
        }
        financial_approval_target = Path(str(record.get("financial_approval_target") or ""))
        if financial_approval_target.is_file():
            financials_md_text = financial_approval_target.read_text(encoding="utf-8", errors="replace")
            if financials_md_text_matches_run_month(financials_md_text, expected_run_month or candidate_run_month):
                summary["financials_md_text"] = financials_md_text
                summary["financials_md_source"] = str(financial_approval_target)
            else:
                financials_md_rejected_count += 1
                summary["financials_md_source_rejected"] = str(financial_approval_target)
                summary["financials_md_source_rejected_reason"] = "financials_md_run_month_mismatch_or_missing_as_of_heading"
        keys = [
            str(record.get("lofty_property_id") or "").strip(),
            str(record.get("property_name") or "").strip(),
            str(record.get("update_approval_target") or "").strip(),
            monthly_artifact_property_folder(record.get("update_approval_target")),
            monthly_artifact_property_folder(record.get("financial_approval_target")),
        ]
        for key in keys:
            key = candidate_lookup_key(key)
            if key:
                summaries[key] = summary
    return summaries, load_issues, {
        "path": str(candidate_packet_report),
        "status": data.get("status"),
        "run_month": candidate_run_month or None,
        "run_month_matches": not (candidate_run_month and expected_run_month) or candidate_run_month == expected_run_month,
        "record_count": len(records),
        "manifest_record_count": manifest_record_count,
        "manifest_source_issue_count": int(data.get("manifest_source_issue_count") or len(manifest_source_issues or [])),
        "review_manifest_source_issues": manifest_source_issues if isinstance(manifest_source_issues, list) else [],
        "verified_summary_count": verified_count,
        "financials_md_source_rejected_count": financials_md_rejected_count,
    }


def candidate_summary_for(
    summaries: dict[str, dict[str, Any]],
    *,
    lofty_property_id: str,
    property_name: str,
    updates_md: Path,
) -> dict[str, Any] | None:
    return (
        summaries.get(candidate_lookup_key(lofty_property_id))
        or summaries.get(candidate_lookup_key(property_name))
        or summaries.get(candidate_lookup_key(str(updates_md)))
        or summaries.get(candidate_lookup_key(monthly_artifact_property_folder(updates_md)))
    )


def approved_update_text(candidate_summary: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not candidate_summary:
        return None, None
    approval_target = str(candidate_summary.get("candidate_update_approval_target") or "").strip()
    if not approval_target:
        return None, None
    path = Path(approval_target)
    if not path.is_file():
        return None, None
    return path.read_text(encoding="utf-8", errors="replace"), str(path)


def load_live_update_guard_statuses(live_update_capture_report: Path | None) -> tuple[dict[str, dict[str, Any]], list[str], bool]:
    if live_update_capture_report is None:
        return {}, [], False
    data = read_json(live_update_capture_report)
    if data.get("status") in {"missing", "unreadable"}:
        return {}, [f"live update capture report {data.get('status')}: {live_update_capture_report}"], True
    records = data.get("records") if isinstance(data.get("records"), list) else []
    if not records:
        return {}, [f"live update capture report has no records: {live_update_capture_report}"], True
    statuses: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        keys = [
            str(record.get("lofty_property_id") or "").strip(),
            str(record.get("updates_md") or "").strip(),
        ]
        check = record.get("check") if isinstance(record.get("check"), dict) else {}
        current_only_verify = (
            record.get("current_only_listing_verify")
            if isinstance(record.get("current_only_listing_verify"), dict)
            else {}
        )
        record_status = str(record.get("status") or "").strip()
        guard_ok = (
            (record_status == "guard_ok" and check.get("ok") is True)
            or record_status in {"guard_ok_live_distribution", "guard_ok_no_distribution_target"}
            or (
                record_status == "guard_ok_current_only"
                and (
                    current_only_verify.get("ok") is True
                    or (
                        record.get("match_status") == "matched"
                        and current_only_verify.get("listing_update_scope") == "current_update_only"
                    )
                )
            )
        )
        status = {
            "guard_ok": guard_ok,
            "status": record.get("status"),
            "check_return_code": check.get("return_code"),
        }
        for key in keys:
            if key:
                statuses[key] = status
    return statuses, [], True


def load_listing_cleanup_statuses(listing_cleanup_queue_report: Path | None) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if listing_cleanup_queue_report is None:
        return {}, []
    data = read_json(listing_cleanup_queue_report)
    if data.get("status") in {"missing", "unreadable"}:
        return {}, [f"listing cleanup queue report {data.get('status')}: {listing_cleanup_queue_report}"]
    records = data.get("records") if isinstance(data.get("records"), list) else []
    statuses: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        keys = [
            str(record.get("lofty_property_id") or "").strip(),
            str(record.get("updates_md") or "").strip(),
            str(record.get("property_name") or "").strip().lower(),
        ]
        for key in keys:
            if key:
                statuses[key] = record
    return statuses, []


def live_update_guard_status(
    statuses: dict[str, dict[str, Any]],
    *,
    lofty_property_id: str,
    updates_md: Path,
) -> dict[str, Any] | None:
    return statuses.get(lofty_property_id) or statuses.get(str(updates_md))


def listing_cleanup_status(
    statuses: dict[str, dict[str, Any]],
    *,
    lofty_property_id: str,
    property_name: str,
    updates_md: Path,
) -> dict[str, Any] | None:
    return statuses.get(lofty_property_id) or statuses.get(str(updates_md)) or statuses.get(property_name.lower())


def listing_cleanup_gap_fields(cleanup_status: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    if not cleanup_status:
        return {}
    cleanup_ready = cleanup_status.get("status") == "ready_listing_cleanup"
    return {
        "listing_cleanup_queue_status": cleanup_status.get("status"),
        "listing_cleanup_ready_but_local_update_blocked": bool(
            cleanup_ready and reason != "listing_history_cleanup_required"
        ),
        "listing_cleanup_dry_run_command": cleanup_status.get("dry_run_command"),
        "listing_cleanup_live_apply_command_requires_explicit_approval": cleanup_status.get(
            "live_apply_command_requires_explicit_approval"
        ),
        "listing_cleanup_live_snapshot_listing_issues": cleanup_status.get("live_snapshot_listing_issues") or [],
    }


def load_properties(
    runtime_map: Path,
    live_update_capture_report: Path | None = None,
    listing_cleanup_queue_report: Path | None = None,
    candidate_packet_report: Path | None = None,
    run_month: str | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], int]:
    data = read_json(runtime_map)
    if data.get("status") in {"missing", "unreadable"}:
        return [], [f"runtime map {data.get('status')}: {runtime_map}"], [], 0
    live_update_statuses, live_update_issues, require_live_update_guard = load_live_update_guard_statuses(live_update_capture_report)
    listing_cleanup_statuses, listing_cleanup_issues = load_listing_cleanup_statuses(listing_cleanup_queue_report)
    candidate_summaries, candidate_issues, candidate_summary_report = load_candidate_financial_summaries(
        candidate_packet_report,
        run_month,
    )
    live_capture_excluded_names = live_capture_excluded_property_names(live_update_capture_report)
    props = data.get("properties") if isinstance(data.get("properties"), list) else []
    issues: list[str] = [*live_update_issues, *listing_cleanup_issues, *candidate_issues]
    unavailable: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    source_property_count = 0
    for prop in props:
        if not isinstance(prop, dict):
            continue
        property_name = str(prop.get("property_name") or prop.get("full_address") or "").strip()
        if excluded_by_manual_policy(property_name, prop.get("property_path") or prop.get("updates_md")):
            continue
        if normalize_property_name(property_name) in live_capture_excluded_names:
            continue
        source_property_count += 1
        lofty_property_id = str(prop.get("lofty_property_id") or "").strip()
        updates_md = Path(str(prop.get("updates_md") or ""))
        cleanup_status = listing_cleanup_status(
            listing_cleanup_statuses,
            lofty_property_id=lofty_property_id,
            property_name=property_name,
            updates_md=updates_md,
        )
        if not updates_md.is_file():
            issue = f"updates_md missing for {property_name or lofty_property_id}: {updates_md}"
            issues.append(issue)
            reason = "updates_md_missing"
            unavailable_record = {
                "property_name": property_name,
                "lofty_property_id": lofty_property_id,
                "updates_md": str(updates_md),
                "reason": reason,
                "issue": issue,
            }
            unavailable_record.update(listing_cleanup_gap_fields(cleanup_status, reason))
            unavailable.append(unavailable_record)
            continue
        updates_text = updates_md.read_text(encoding="utf-8", errors="replace")
        candidate_summary = candidate_summary_for(
            candidate_summaries,
            lofty_property_id=lofty_property_id,
            property_name=property_name,
            updates_md=updates_md,
        )
        approved_text, approved_path = approved_update_text(candidate_summary)
        if not updates_text.strip():
            issue = f"updates_md empty for {property_name or lofty_property_id}: {updates_md}"
            issues.append(issue)
            reason = "updates_md_empty"
            unavailable_record = {
                "property_name": property_name,
                "lofty_property_id": lofty_property_id,
                "updates_md": str(updates_md),
                "reason": reason,
                "issue": issue,
            }
            unavailable_record.update(candidate_gap_fields(candidate_summary, candidate_summary_report))
            unavailable_record.update(listing_cleanup_gap_fields(cleanup_status, reason))
            unavailable.append(unavailable_record)
            continue
        latest_source_text = approved_text if approved_text and approved_text.strip() else updates_text
        latest_source_path = approved_path or str(updates_md)
        latest_source_type = "approved_update_artifact" if approved_path else "updates_md"
        entry = parse_latest_entry(latest_source_text)
        if not entry:
            issue = f"no latest update entry in {latest_source_path}"
            issues.append(issue)
            reason = "no_latest_update_entry"
            unavailable_record = {
                "property_name": property_name,
                "lofty_property_id": lofty_property_id,
                "updates_md": str(updates_md),
                "latest_update_source": latest_source_path,
                "latest_update_source_type": latest_source_type,
                "reason": reason,
                "issue": issue,
            }
            unavailable_record.update(candidate_gap_fields(candidate_summary, candidate_summary_report))
            unavailable_record.update(listing_cleanup_gap_fields(cleanup_status, reason))
            unavailable.append(unavailable_record)
            continue
        latest_update_body, financial_summary_enriched = append_verified_financial_summary_if_missing(
            entry["body"],
            candidate_summary,
            run_month,
        )
        financial_summary_present = monthly_financial_summary_present(latest_update_body, run_month)
        property_guard_issues = property_update_guard_issues(latest_update_body)
        if property_guard_issues:
            issue = f"latest update body failed safety guard for {latest_source_path}: {'; '.join(property_guard_issues)}"
            issues.append(issue)
            reason = "latest_update_body_guard"
            unavailable_record = {
                "property_name": property_name,
                "lofty_property_id": lofty_property_id,
                "updates_md": str(updates_md),
                "latest_update_source": latest_source_path,
                "latest_update_source_type": latest_source_type,
                "reason": reason,
                "issue": issue,
                "candidate_summary_available": bool(candidate_summary),
                "candidate_summary_report": candidate_summary_report,
                "financial_summary_enriched": financial_summary_enriched,
                "monthly_financial_summary_present": financial_summary_present,
            }
            unavailable_record.update(candidate_gap_fields(candidate_summary, candidate_summary_report))
            unavailable_record.update(listing_cleanup_gap_fields(cleanup_status, reason))
            unavailable.append(unavailable_record)
            continue
        if not one_update_only(latest_update_body):
            issue = f"latest update includes multiple Property Update entries: {updates_md}"
            issues.append(issue)
            reason = "latest_update_multiple_entries"
            unavailable_record = {
                "property_name": property_name,
                "lofty_property_id": lofty_property_id,
                "updates_md": str(updates_md),
                "latest_update_source": latest_source_path,
                "latest_update_source_type": latest_source_type,
                "reason": reason,
                "issue": issue,
            }
            unavailable_record.update(listing_cleanup_gap_fields(cleanup_status, reason))
            unavailable.append(unavailable_record)
            continue
        guard_status = live_update_guard_status(
            live_update_statuses,
            lofty_property_id=lofty_property_id,
            updates_md=updates_md,
        )
        if require_live_update_guard and not (guard_status and guard_status.get("guard_ok") is True):
            status_label = str((guard_status or {}).get("status") or "missing_guard_record")
            cleanup_ready = cleanup_status and cleanup_status.get("status") == "ready_listing_cleanup"
            reason = "listing_history_cleanup_required" if cleanup_ready else "live_update_guard_not_reconciled"
            issue = f"live update guard not reconciled for {property_name or lofty_property_id}: {updates_md} status={status_label}"
            unavailable_record = {
                "property_name": property_name,
                "lofty_property_id": lofty_property_id,
                "updates_md": str(updates_md),
                "latest_update_source": latest_source_path,
                "latest_update_source_type": latest_source_type,
                "reason": reason,
                "issue": issue,
            }
            if cleanup_ready:
                issue = (
                    f"Lofty listing field contains copied UPDATES.md history for {property_name or lofty_property_id}; "
                    f"cleanup queue is ready and email remains blocked until listing is reconciled"
                )
                unavailable_record.update(candidate_gap_fields(candidate_summary, candidate_summary_report))
                unavailable_record.update(
                    {
                        "issue": issue,
                        "financial_summary_enriched": financial_summary_enriched,
                        "monthly_financial_summary_present": financial_summary_present,
                    }
                )
                unavailable_record.update(listing_cleanup_gap_fields(cleanup_status, reason))
            else:
                unavailable_record.update(candidate_gap_fields(candidate_summary, candidate_summary_report))
                unavailable_record.update(
                    {
                        "financial_summary_enriched": financial_summary_enriched,
                        "monthly_financial_summary_present": financial_summary_present,
                    }
                )
                unavailable_record.update(listing_cleanup_gap_fields(cleanup_status, reason))
            if status_label != "planned":
                issues.append(issue)
            unavailable.append(unavailable_record)
            continue
        records.append(
            {
                "property_name": property_name,
                "lofty_property_id": lofty_property_id,
                "slug": str(prop.get("slug") or "").strip(),
                "updates_md": str(updates_md),
                "latest_update_source": latest_source_path,
                "latest_update_source_type": latest_source_type,
                "latest_update_date": entry["date"],
                "latest_update_body": latest_update_body.strip(),
                "latest_update_body_sha256": hashlib.sha256(latest_update_body.strip().encode("utf-8")).hexdigest(),
                "latest_update_digest": stable_digest({"date": entry["date"], "body": latest_update_body.strip()}),
                "latest_update_scope": "latest_only",
                "financial_summary_enriched": financial_summary_enriched,
                "monthly_financial_summary_present": financial_summary_present,
                "candidate_summary_source": candidate_summary_report.get("path") if financial_summary_enriched else None,
                "_source_updates_md_text": updates_text,
                **source_updates_metadata(updates_text, entry["date"]),
            }
        )
    return records, issues, unavailable, source_property_count


def load_recipients(recipients_csv: Path, properties: list[dict[str, Any]], allow_portfolio_recipients: bool) -> tuple[list[dict[str, Any]], list[str]]:
    if not recipients_csv.is_file():
        return [], [f"recipients CSV missing: {recipients_csv}; create from template {recipient_template_path(recipients_csv)}"]
    by_name = {str(prop.get("property_name") or "").strip().lower(): prop for prop in properties if prop.get("property_name")}
    by_id = {str(prop.get("lofty_property_id") or "").strip(): prop for prop in properties if prop.get("lofty_property_id")}
    recipients: list[dict[str, Any]] = []
    issues: list[str] = []
    try:
        with recipients_csv.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for index, row in enumerate(reader, start=2):
                email = str(row.get("email") or row.get("Email") or "").strip()
                if "@" not in email:
                    issues.append(f"invalid recipient email at row {index}")
                    continue
                name = str(row.get("name") or row.get("Name") or "").strip()
                prop_id = str(row.get("lofty_property_id") or row.get("property_id") or "").strip()
                prop_name = str(row.get("property_name") or row.get("Property") or row.get("property") or "").strip()
                matched: list[dict[str, Any]]
                if prop_id:
                    prop = by_id.get(prop_id)
                    matched = [prop] if prop else []
                elif prop_name:
                    prop = by_name.get(prop_name.lower())
                    matched = [prop] if prop else []
                elif allow_portfolio_recipients:
                    matched = properties
                else:
                    issues.append(f"recipient row {index} has no property_name or lofty_property_id")
                    continue
                if not matched:
                    issues.append(f"recipient row {index} references unknown or excluded property")
                    continue
                recipients.append(
                    {
                        "email": email,
                        "email_digest": email_digest(email),
                        "name": name,
                        "property_count": len(matched),
                        "property_ids": [prop.get("lofty_property_id") for prop in matched],
                        "properties": matched,
                    }
                )
    except Exception as exc:  # noqa: BLE001
        issues.append(f"recipients CSV unreadable: {exc}")
    return recipients, issues


def body_text(recipient: dict[str, Any], run_month: str) -> str:
    greeting = f"Hi {recipient['name']}," if recipient.get("name") else "Hi,"
    blocks = [
        greeting,
        "",
        f"Signal-only Lofty owner update for {run_month}.",
        "",
    ]
    for prop in recipient["properties"]:
        blocks.extend(
            [
                f"Property: {prop['property_name']}",
                prop["latest_update_body"],
                "",
            ]
        )
    blocks.extend(
        [
            "Scope: latest approved property signal plus current financial summary only.",
            "Supporting documents remain in the public property folder under 00 - README & Property Snapshot and 07 - P&L & Owner Statements.",
        ]
    )
    return "\n".join(blocks).strip() + "\n"


def native_body_text(prop: dict[str, Any], run_month: str) -> str:
    blocks = [
        f"Signal-only Lofty owner update for {run_month}.",
        "",
        f"Property: {prop['property_name']}",
        prop["latest_update_body"],
        "",
        "Scope: latest approved property signal plus current financial summary only.",
        "Supporting documents remain in the public property folder under 00 - README & Property Snapshot and 07 - P&L & Owner Statements.",
    ]
    return "\n".join(blocks).strip() + "\n"


def html_body(text: str) -> str:
    return "<html><body><pre style=\"font-family:Arial,sans-serif;white-space:pre-wrap\">" + html.escape(text) + "</pre></body></html>"


def email_body_guard_issues(text: str, property_count: int, properties: list[dict[str, Any]] | None = None) -> list[str]:
    issues: list[str] = []
    char_count = len(text)
    line_count = len(text.splitlines())
    issues.extend(canonical_property_update_guard_issues(text, required_property_update_markers=property_count))
    issues.extend(full_history_guard_issues(text, allowed_property_update_markers=property_count))
    issues.extend(source_full_history_leak_guard_issues(text, properties))
    issues.extend(latest_source_scope_guard_issues(text, properties))
    issues.extend(
        monthly_financial_summary_guard_issues(
            text,
            required_summary_count=property_count,
        )
    )
    issues.extend(internal_owner_update_guard_issues(text))
    issues.extend(operational_report_guard_issues(text))
    if char_count > MAX_EMAIL_BODY_CHARS:
        issues.append(f"body_char_count={char_count}>max={MAX_EMAIL_BODY_CHARS}")
    if line_count > MAX_EMAIL_BODY_LINES:
        issues.append(f"body_line_count={line_count}>max={MAX_EMAIL_BODY_LINES}")
    return issues


def full_history_leak_issue(issue_text: str) -> bool:
    return issue_text.startswith(
        (
            "property_update_marker_count=",
            "dated_update_heading_count=",
            "full_updates_header_count=",
            "full_source_updates_md_embedded:",
            "historical_update_date_leaked:",
            "latest_update_body_contains_full_source_updates_md:",
            "latest_update_body_missing_from_email:",
        )
    )


def packet_full_history_leak(packet: dict[str, Any]) -> bool:
    if packet["property_update_marker_count"] > packet["property_count"] or packet["dated_update_heading_count"] > 0:
        return True
    return any(full_history_leak_issue(str(issue)) for issue in packet.get("body_guard_issues") or [])


def normalize_packet_body_guards(packets: list[dict[str, Any]]) -> None:
    for packet in packets:
        body = str(packet.get("body_text") or "")
        try:
            property_count = int(packet.get("property_count") or 0)
        except (TypeError, ValueError):
            property_count = 0
        source_properties = packet.get("source_properties") if isinstance(packet.get("source_properties"), list) else None
        recomputed_issues = email_body_guard_issues(body, property_count, source_properties)
        merged_issues: list[str] = []
        for issue_text in [*(packet.get("body_guard_issues") or []), *recomputed_issues]:
            issue = str(issue_text)
            if issue and issue not in merged_issues:
                merged_issues.append(issue)
        packet["property_count"] = property_count
        packet["property_update_marker_count"] = len(PROPERTY_UPDATE_MARKER_RE.findall(body))
        packet["dated_update_heading_count"] = len(DATED_UPDATE_HEADING_RE.findall(body))
        packet["body_char_count"] = len(body)
        packet["body_line_count"] = len(body.splitlines())
        if packet.get("email"):
            packet["body_digest"] = stable_digest({"email": str(packet["email"]).lower(), "body": body})
        packet["body_guard_issues"] = merged_issues


def property_update_guard_issues(text: str) -> list[str]:
    issues: list[str] = []
    char_count = len(text)
    line_count = len(text.splitlines())
    issues.extend(canonical_property_update_guard_issues(text, required_property_update_markers=1))
    issues.extend(full_history_guard_issues(text, allowed_property_update_markers=1))
    issues.extend(internal_owner_update_guard_issues(text))
    issues.extend(operational_report_guard_issues(text))
    if PROPERTY_UPDATE_MARKER_RE.search(text):
        issues.extend(monthly_financial_summary_guard_issues(text, required_summary_count=1))
    if char_count > MAX_PROPERTY_UPDATE_CHARS:
        issues.append(f"property_update_char_count={char_count}>max={MAX_PROPERTY_UPDATE_CHARS}")
    if line_count > MAX_PROPERTY_UPDATE_LINES:
        issues.append(f"property_update_line_count={line_count}>max={MAX_PROPERTY_UPDATE_LINES}")
    return issues


def send_email_gws(gws_binary: str, to: str, subject: str, body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [gws_binary, "gmail", "+send", "--to", to, "--subject", subject, "--body", html_body(body), "--html"],
        capture_output=True,
        text=True,
        timeout=120,
    )


def write_previews(out_dir: Path, packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    previews: list[dict[str, Any]] = []
    for packet in packets:
        path = out_dir / f"{packet['email_digest']}.txt"
        path.write_text(packet["body_text"], encoding="utf-8")
        previews.append({"email_digest": packet["email_digest"], "path": str(path), "body_digest": packet["body_digest"]})
    return previews


def safe_preview_stem(prop: dict[str, Any]) -> str:
    raw = str(prop.get("lofty_property_id") or prop.get("slug") or prop.get("property_name") or "property")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    return stem[:96] or "property"


def collect_native_packets(properties: list[dict[str, Any]], run_month: str) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    for prop in properties:
        text = native_body_text(prop, run_month)
        body_guard_issues = email_body_guard_issues(text, 1, [prop])
        packets.append(
            {
                "property_name": prop.get("property_name"),
                "lofty_property_id": prop.get("lofty_property_id"),
                "property_count": 1,
                "property_ids": [prop.get("lofty_property_id")],
                "source_properties": [prop],
                "body_text": text,
                "body_digest": stable_digest(
                    {
                        "lofty_property_id": str(prop.get("lofty_property_id") or ""),
                        "body": text,
                    }
                ),
                "property_update_marker_count": len(PROPERTY_UPDATE_MARKER_RE.findall(text)),
                "dated_update_heading_count": len(DATED_UPDATE_HEADING_RE.findall(text)),
                "body_char_count": len(text),
                "body_line_count": len(text.splitlines()),
                "body_guard_issues": body_guard_issues,
                "preview_stem": safe_preview_stem(prop),
            }
        )
    return packets


def write_native_previews(out_dir: Path, packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    previews: list[dict[str, Any]] = []
    used: set[str] = set()
    for packet in packets:
        stem = str(packet.get("preview_stem") or "property")
        candidate = f"native-{stem}"
        unique = candidate
        counter = 2
        while unique in used:
            unique = f"{candidate}-{counter}"
            counter += 1
        used.add(unique)
        path = out_dir / f"{unique}.txt"
        path.write_text(str(packet["body_text"]), encoding="utf-8")
        previews.append(
            {
                "property_name": packet.get("property_name"),
                "lofty_property_id": packet.get("lofty_property_id"),
                "property_send_keys": packet_property_send_keys(packet),
                "path": str(path),
                "body_digest": packet["body_digest"],
                "body_guard_issue_count": len(packet.get("body_guard_issues") or []),
            }
        )
    return previews


def clear_stale_previews(out_dir: Path) -> tuple[int, list[str]]:
    if not out_dir.exists():
        return 0, []
    removed = 0
    errors: list[str] = []
    for path in sorted(out_dir.glob("*.txt")):
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
                removed += 1
        except OSError as exc:
            errors.append(f"stale preview cleanup failed for {path}: {exc}")
    return removed, errors


def collect_packets(
    runtime_map: Path,
    recipients_csv: Path,
    run_month: str,
    allow_portfolio_recipients: bool,
    live_update_capture_report: Path | None = None,
    listing_cleanup_queue_report: Path | None = None,
    candidate_packet_report: Path | None = None,
) -> tuple[list[dict[str, Any]], list[str], int, int, int, int, list[dict[str, Any]], int, int]:
    properties, property_issues, property_unavailable, source_property_count = load_properties(
        runtime_map=runtime_map,
        live_update_capture_report=live_update_capture_report,
        listing_cleanup_queue_report=listing_cleanup_queue_report,
        candidate_packet_report=candidate_packet_report,
        run_month=run_month,
    )
    property_financial_summary_enriched_count = sum(
        1 for prop in properties if prop.get("financial_summary_enriched") is True
    )
    property_financial_summary_present_count = sum(
        1 for prop in properties if prop.get("monthly_financial_summary_present") is True
    )
    recipients, recipient_issues = load_recipients(recipients_csv, properties, allow_portfolio_recipients)
    property_blocking_issues = property_issues if allow_portfolio_recipients or not properties else []
    packets: list[dict[str, Any]] = []
    for recipient in recipients:
        text = body_text(recipient, run_month)
        body_guard_issues = email_body_guard_issues(text, recipient["property_count"], recipient.get("properties"))
        packets.append(
            {
                "email": recipient["email"],
                "email_digest": recipient["email_digest"],
                "property_count": recipient["property_count"],
                "property_ids": recipient["property_ids"],
                "properties_bounded": [
                    {
                        "property_name": prop.get("property_name"),
                        "lofty_property_id": prop.get("lofty_property_id"),
                        "latest_update_scope": prop.get("latest_update_scope"),
                        "latest_update_source": prop.get("latest_update_source"),
                        "latest_update_source_type": prop.get("latest_update_source_type"),
                        "latest_update_body_sha256": prop.get("latest_update_body_sha256"),
                        "source_updates_md_has_history": prop.get("source_updates_md_has_history") is True,
                        "source_updates_md_history_date_count": len(prop.get("source_updates_md_history_dates") or []),
                        "financial_summary_enriched": prop.get("financial_summary_enriched") is True,
                        "monthly_financial_summary_present": prop.get("monthly_financial_summary_present") is True,
                        "candidate_summary_source": prop.get("candidate_summary_source"),
                    }
                    for prop in recipient.get("properties", [])[:25]
                ],
                "source_properties": recipient.get("properties", []),
                "body_text": text,
                "body_digest": stable_digest({"email": recipient["email"].lower(), "body": text}),
                "property_update_marker_count": len(PROPERTY_UPDATE_MARKER_RE.findall(text)),
                "dated_update_heading_count": len(DATED_UPDATE_HEADING_RE.findall(text)),
                "body_char_count": len(text),
                "body_line_count": len(text.splitlines()),
                "body_guard_issues": body_guard_issues,
            }
        )
    return (
        packets,
        [*property_blocking_issues, *recipient_issues],
        source_property_count,
        len(properties),
        len(recipients),
        len(property_unavailable),
        property_unavailable,
        property_financial_summary_enriched_count,
        property_financial_summary_present_count,
    )


def build_report(
    *,
    runtime_map: Path,
    recipients_csv: Path,
    run_month: str,
    sent_state_file: Path,
    out_dir: Path,
    subject: str,
    dry_run: bool,
    send: bool,
    allow_portfolio_recipients: bool,
    live_update_capture_report: Path | None = None,
    listing_cleanup_queue_report: Path | None = None,
    candidate_packet_report: Path | None = None,
    discord_send_report: Path | None = None,
    discord_plan_validation_report: Path | None = None,
    guild_test_post_report: Path | None = None,
    require_guild_test_post_before_email: bool = True,
    gws_binary: str | None = None,
    write_preview_files: bool = True,
    prepared_packets: list[dict[str, Any]] | None = None,
    prepared_issues: list[str] | None = None,
    prepared_property_count: int | None = None,
    prepared_available_property_count: int | None = None,
    prepared_recipient_count: int | None = None,
    prepared_property_unavailable_count: int | None = None,
    prepared_property_unavailable: list[dict[str, Any]] | None = None,
    prepared_property_financial_summary_enriched_count: int | None = None,
    prepared_property_financial_summary_present_count: int | None = None,
    native_only: bool = False,
) -> dict[str, Any]:
    discord_validation = read_json(discord_plan_validation_report) if discord_plan_validation_report else {}
    held_property_names = financially_held_property_names(discord_validation)
    live_capture_excluded_names = live_capture_excluded_property_names(live_update_capture_report)
    native_properties, native_property_issues, native_property_unavailable, native_source_property_count = load_properties(
        runtime_map=runtime_map,
        candidate_packet_report=candidate_packet_report,
        run_month=run_month,
    )
    native_properties = [
        prop
        for prop in native_properties
        if normalize_property_name(prop.get("property_name")) not in live_capture_excluded_names
        and not property_is_financially_held(prop.get("property_name"), held_property_names)
    ]
    native_property_unavailable = [
        record
        for record in native_property_unavailable
        if normalize_property_name(record.get("property_name")) not in live_capture_excluded_names
        and not property_is_financially_held(record.get("property_name"), held_property_names)
    ]
    native_source_property_count = sum(
        1
        for prop in read_json(runtime_map).get("properties") or []
        if isinstance(prop, dict)
        and not excluded_by_manual_policy(
            prop.get("property_name") or prop.get("full_address"),
            prop.get("property_path") or prop.get("updates_md"),
        )
        and normalize_property_name(prop.get("property_name") or prop.get("full_address")) not in live_capture_excluded_names
    )
    native_packets_all = collect_native_packets(native_properties, run_month)
    normalize_packet_body_guards(native_packets_all)
    if native_only:
        packets = []
        input_issues = []
        property_count = native_source_property_count
        available_property_count = len(native_properties)
        recipient_count = 0
        property_unavailable_count = len(native_property_unavailable)
        property_unavailable = list(native_property_unavailable)
        property_financial_summary_enriched_count = sum(
            1 for prop in native_properties if prop.get("monthly_financial_summary_enriched") is True
        )
        property_financial_summary_present_count = sum(
            1 for prop in native_properties if prop.get("monthly_financial_summary_present") is True
        )
    elif prepared_packets is None:
        (
            packets,
            input_issues,
            property_count,
            available_property_count,
            recipient_count,
            property_unavailable_count,
            property_unavailable,
            property_financial_summary_enriched_count,
            property_financial_summary_present_count,
        ) = collect_packets(
            runtime_map,
            recipients_csv,
            run_month,
            allow_portfolio_recipients,
            live_update_capture_report,
            listing_cleanup_queue_report,
            candidate_packet_report,
        )
    else:
        packets = prepared_packets
        input_issues = list(prepared_issues or [])
        property_count = int(prepared_property_count or 0)
        available_property_count = int(prepared_available_property_count or 0)
        recipient_count = int(prepared_recipient_count or 0)
        property_unavailable_count = int(prepared_property_unavailable_count or 0)
        property_unavailable = list(prepared_property_unavailable or [])
        property_financial_summary_enriched_count = int(prepared_property_financial_summary_enriched_count or 0)
        property_financial_summary_present_count = int(prepared_property_financial_summary_present_count or 0)
    _, candidate_load_issues, candidate_summary_report = load_candidate_financial_summaries(candidate_packet_report, run_month)
    discord_provenance = discord_review_provenance(discord_send_report, discord_plan_validation_report)
    normalize_packet_body_guards(packets)
    if native_only:
        recipient_template, recipient_template_written = recipient_template_path(recipients_csv), False
    elif not recipients_csv.is_file():
        recipient_template, recipient_template_written = write_recipient_template(recipients_csv)
    else:
        recipient_template, recipient_template_written = recipient_template_path(recipients_csv), False
    property_gap_csv = out_dir.parent / DEFAULT_PROPERTY_GAP_CSV_NAME
    property_gap_csv, property_gap_csv_written = write_property_gap_csv(property_gap_csv, property_unavailable)
    packet_digest = stable_digest(
        {
            "run_month": run_month,
            "subject": subject,
            "packets": [
                {
                    "email_digest": packet["email_digest"],
                    "body_digest": packet["body_digest"],
                    "property_ids": packet["property_ids"],
                }
                for packet in packets
            ],
        }
    )
    raw_state = read_json(sent_state_file)
    state, reconstructed_sent_evidence = reconstruct_property_sent_state(runtime_map, raw_state)
    sent_state_month = str(state.get("run_month") or "") if state.get("status") not in {"missing", "unreadable"} else ""
    cooldown_packets = packets if packets else native_packets_all
    already_sent, sent_property_keys = packets_sent_for_run_month(cooldown_packets, state, run_month)
    property_cooldown_issue_list, property_cooldown_hold_keys_set = property_cooldown_hold_keys(cooldown_packets, state)
    native_sent_hold_keys = set(sent_property_keys)
    native_email_idempotency_hold_keys = property_cooldown_hold_keys_set | native_sent_hold_keys
    native_email_held_packets = [
        packet
        for packet in native_packets_all
        if any(key in native_email_idempotency_hold_keys for key in packet_property_send_keys(packet))
    ]
    native_packets = [
        packet
        for packet in native_packets_all
        if not any(key in native_email_idempotency_hold_keys for key in packet_property_send_keys(packet))
    ]
    resolved_gws = gws_binary if gws_binary is not None else (shutil.which("gws") or "")
    guild_guard = guild_test_post_guard(
        guild_test_post_report,
        run_month,
        required=send and require_guild_test_post_before_email,
    )
    missing_recipient_csv_issue = (
        f"recipients CSV missing: {recipients_csv}; create from template {recipient_template_path(recipients_csv)}"
    )
    non_native_input_issues = [
        issue for issue in input_issues if str(issue) == missing_recipient_csv_issue
    ]
    issues = [
        issue for issue in input_issues if str(issue) != missing_recipient_csv_issue
    ]
    if not dry_run and not send:
        issues.append("select --dry-run or --send")
    if send and already_sent:
        issues.append(f"owner update email already sent for {run_month}")
    if send and property_cooldown_issue_list:
        issues.extend(property_cooldown_issue_list)
    if send and os.environ.get(SEND_ENV) != "1":
        issues.append(f"{SEND_ENV}=1 is required for non-native owner email send")
    if send and not resolved_gws:
        issues.append("gws CLI is not available for send mode")
    if send and live_update_capture_report is None:
        issues.append("live update guard report is required for send mode")
    if send and listing_cleanup_queue_report is None:
        issues.append("listing cleanup queue report is required for send mode")
    if send and candidate_packet_report is None:
        issues.append("review candidate packet report is required for send mode")
    for candidate_issue in candidate_load_issues:
        if candidate_issue not in issues:
            issues.append(candidate_issue)
    if send and require_guild_test_post_before_email and not guild_guard["ok"]:
        issues.append("valid guild property-channel test post is required before owner email send")
        issues.extend(f"guild_test_post:{issue}" for issue in guild_guard["issues"])
    if send and not packets:
        issues.append("no recipient email packets generated")
    if send:
        issues.extend(non_native_input_issues)
    full_history_leak_count = sum(1 for packet in packets if packet_full_history_leak(packet))
    full_history_guard_issue_count = sum(
        1
        for packet in packets
        for issue_text in packet.get("body_guard_issues") or []
        if full_history_leak_issue(str(issue_text))
    )
    body_guard_issue_count = sum(1 for packet in packets if packet.get("body_guard_issues"))
    native_body_guard_issue_count = sum(1 for packet in native_packets if packet.get("body_guard_issues"))
    native_total_property_count = sum(int(packet.get("property_count") or 0) for packet in native_packets_all)
    native_packet_property_count = sum(int(packet.get("property_count") or 0) for packet in native_packets)
    native_email_idempotency_held_property_count = sum(int(packet.get("property_count") or 0) for packet in native_email_held_packets)
    native_email_cooldown_held_property_count = sum(
        int(packet.get("property_count") or 0)
        for packet in native_packets_all
        if any(key in property_cooldown_hold_keys_set for key in packet_property_send_keys(packet))
    )
    native_email_current_month_held_property_count = sum(
        int(packet.get("property_count") or 0)
        for packet in native_packets_all
        if any(key in native_sent_hold_keys for key in packet_property_send_keys(packet))
    )
    native_email_eligible_property_count = native_packet_property_count
    native_owner_email_property_coverage_ok = (
        native_email_eligible_property_count > 0
        and native_packet_property_count + native_email_idempotency_held_property_count == native_total_property_count
        and native_body_guard_issue_count == 0
        and not native_property_issues
    )
    live_capture_report_for_noise = read_json(live_update_capture_report) if live_update_capture_report else {}
    live_capture_review_blockers_for_noise = [
        str(item)
        for item in live_capture_report_for_noise.get("review_blockers") or []
        if str(item or "").strip()
    ]
    live_capture_planned_for_noise = any(
        blocker.startswith("live_update_capture_not_applied")
        or blocker.startswith("live_update_planned_count=")
        or blocker.startswith("live_update_unverified_count=")
        for blocker in live_capture_review_blockers_for_noise
    )
    if full_history_leak_count:
        issues.append(f"possible full UPDATES.md leak in {full_history_leak_count} recipient packets")
    if body_guard_issue_count:
        issues.append(f"owner email body guard blocked {body_guard_issue_count} recipient packets")
    if native_body_guard_issue_count and not live_capture_planned_for_noise:
        issues.append(f"native Lofty owner email body guard blocked {native_body_guard_issue_count} property packets")
    financial_summary_enriched_count = property_financial_summary_enriched_count
    financial_summary_present_count = property_financial_summary_present_count
    unavailable_financial_summary_enriched_count = sum(
        1 for record in property_unavailable if record.get("financial_summary_enriched") is True
    )
    unavailable_financial_summary_present_count = sum(
        1 for record in property_unavailable if record.get("monthly_financial_summary_present") is True
    )
    total_financial_summary_present_count = financial_summary_present_count + unavailable_financial_summary_present_count
    financial_summary_required_property_count = property_count
    if native_only and held_property_names:
        financial_summary_required_property_count = max(0, property_count - len(held_property_names))
    missing_financial_summary_present_count = max(
        0,
        financial_summary_required_property_count - total_financial_summary_present_count,
    )
    if missing_financial_summary_present_count and not packets:
        candidate_status = candidate_summary_report.get("status") or "unknown"
        verified_count = int(candidate_summary_report.get("verified_summary_count") or 0)
        record_count = int(candidate_summary_report.get("record_count") or 0)
        issues.append(
            "monthly financial summary missing for "
            f"{missing_financial_summary_present_count}/{property_count} active properties; "
            f"candidate_packet_status={candidate_status}; "
            f"verified_candidate_summaries={verified_count}/{record_count}"
        )
    if native_only and held_property_names and not native_packets:
        issues.append(f"native Lofty owner email held by financial review for {len(held_property_names)} active properties")
    unavailable_reason_counts = reason_counts(property_unavailable)
    if unavailable_reason_counts.get("live_update_guard_not_reconciled") and not issues:
        issues.append(
            "live update guard capture required for "
            f"{unavailable_reason_counts['live_update_guard_not_reconciled']}/{property_count} active properties"
        )
    unavailable_candidate_update_source_count = sum(
        1 for record in property_unavailable if str(record.get("candidate_update_source") or "").strip()
    )
    unavailable_candidate_update_approval_target_count = sum(
        1 for record in property_unavailable if str(record.get("candidate_update_approval_target") or "").strip()
    )
    unavailable_candidate_financial_approval_target_count = sum(
        1 for record in property_unavailable if str(record.get("candidate_financial_approval_target") or "").strip()
    )
    unsafe_preview_packet_count = sum(
        1 for packet in packets if packet.get("body_guard_issues") or packet_full_history_leak(packet)
    )
    preview_file_write_allowed = write_preview_files and bool(packets) and not unsafe_preview_packet_count
    if unsafe_preview_packet_count:
        preview_write_blocked_reason = "owner email body guard blocked preview artifact writes"
    elif not packets:
        preview_write_blocked_reason = "no recipient email packets generated"
    else:
        preview_write_blocked_reason = None
    stale_preview_file_removed_count = 0
    stale_preview_cleanup_errors: list[str] = []
    if write_preview_files and not preview_file_write_allowed:
        stale_preview_file_removed_count, stale_preview_cleanup_errors = clear_stale_previews(out_dir)
        issues.extend(stale_preview_cleanup_errors)
    previews = write_previews(out_dir, packets) if preview_file_write_allowed else []
    native_preview_file_write_allowed = bool(native_packets) and native_body_guard_issue_count == 0
    native_previews = write_native_previews(out_dir, native_packets) if native_preview_file_write_allowed else []
    native_lofty_owner_email_allowed = (
        not issues
        and native_email_eligible_property_count > 0
        and bool(native_packets)
        and full_history_leak_count == 0
        and full_history_guard_issue_count == 0
        and body_guard_issue_count == 0
        and native_body_guard_issue_count == 0
        and not stale_preview_cleanup_errors
        and guild_guard["ok"]
    )
    non_native_signal_only_owner_email_allowed = (
        bool(packets)
        and not issues
        and not already_sent
        and not property_cooldown_issue_list
        and full_history_leak_count == 0
        and full_history_guard_issue_count == 0
        and body_guard_issue_count == 0
        and not stale_preview_cleanup_errors
    )
    safe_to_send = send and not issues and not already_sent and bool(packets)
    actionable_summary = owner_packet_actionable_summary(
        issues=issues,
        packets=packets,
        property_unavailable=property_unavailable,
        property_gap_csv=property_gap_csv,
        recipients_csv=recipients_csv,
        recipient_template=recipient_template,
        listing_cleanup_queue_report=listing_cleanup_queue_report,
        live_update_capture_report=live_update_capture_report,
        run_month=run_month,
        eligible_financial_summary_missing_count=missing_financial_summary_present_count,
        financially_held_property_count=len(held_property_names),
    )
    return {
        "generated_at": iso_z(),
        "status": "ok" if not issues else "review",
        "classification": "ok" if not issues else "lofty-monthly-owner-email-packet-review",
        "issue_count": len(issues),
        "issues": issues,
        "issue_classes": [ISSUE_CLASS] if issues else [],
        "actionable_summary": actionable_summary,
        "primary_blocker": actionable_summary["primary_blocker"],
        "next_action": actionable_summary["primary_blocker"]["next_action"],
        "hold": actionable_summary["primary_blocker"]["hold"],
        "run_month": run_month,
        "runtime_map": str(runtime_map),
        "runtime_map_exists": runtime_map.is_file(),
        "runtime_map_canonical_name_ok": runtime_map.name == CANONICAL_RUNTIME_MAP_NAME,
        "runtime_map_expected_name": CANONICAL_RUNTIME_MAP_NAME,
        "live_update_capture_report": str(live_update_capture_report) if live_update_capture_report else None,
        "listing_cleanup_queue_report": str(listing_cleanup_queue_report) if listing_cleanup_queue_report else None,
        "review_candidate_packet_report": str(candidate_packet_report) if candidate_packet_report else None,
        "review_candidate_packet_status": candidate_summary_report.get("status"),
        "review_candidate_packet_run_month": candidate_summary_report.get("run_month"),
        "review_candidate_packet_run_month_matches": candidate_summary_report.get("run_month_matches"),
        "review_candidate_packet_record_count": candidate_summary_report.get("record_count"),
        "review_candidate_packet_manifest_record_count": candidate_summary_report.get("manifest_record_count"),
        "review_candidate_packet_manifest_source_issue_count": candidate_summary_report.get("manifest_source_issue_count"),
        "review_candidate_packet_manifest_source_issues": candidate_summary_report.get("review_manifest_source_issues"),
        "review_candidate_packet_verified_summary_count": candidate_summary_report.get("verified_summary_count"),
        "review_candidate_packet_summary_source": candidate_summary_report.get("path"),
        **discord_provenance,
        "guild_test_post_report": str(guild_test_post_report) if guild_test_post_report else None,
        "require_guild_test_post_before_email": require_guild_test_post_before_email,
        "guild_test_post_guard": guild_guard,
        "guild_test_post_required": guild_guard["required"],
        "guild_test_post_ok": guild_guard["ok"],
        "guild_test_post_valid": guild_guard["valid"],
        "guild_test_post_posted": guild_guard["posted"],
        "guild_test_post_status": guild_guard["status"],
        "guild_test_post_issues": guild_guard["issues"],
        "requires_live_update_guard": live_update_capture_report is not None,
        "live_update_capture_excluded_property_count": len(live_capture_excluded_names),
        "native_only": native_only,
        "recipients_csv": str(recipients_csv),
        "recipient_template_csv": str(recipient_template),
        "recipient_template_written": recipient_template_written,
        "recipient_csv_required_fields": list(RECIPIENT_CSV_FIELDS),
        "recipient_csv_policy": (
            "legacy non-native email only; native Lofty owner email recipients are resolved by lofty-pm-mcp/send_emails"
            if native_only
            else "one row per recipient-property; portfolio-wide recipients require --allow-portfolio-recipients and all active properties must have a latest approved update"
        ),
        "owner_email_send_policy": SIGNAL_ONLY_OWNER_EMAIL_POLICY,
        "native_lofty_owner_email_allowed": native_lofty_owner_email_allowed,
        "native_lofty_owner_email_recipient_source": "lofty_pm_mcp",
        "native_lofty_owner_email_content_source": "per-property latest approved update plus verified financial summary",
        "native_lofty_owner_email_preview_count": len(native_previews),
        "native_lofty_owner_email_preview_file_write_allowed": native_preview_file_write_allowed,
        "native_lofty_owner_email_body_guard_issue_count": native_body_guard_issue_count,
        "native_lofty_owner_email_property_count": native_packet_property_count,
        "native_lofty_owner_email_total_property_count": native_total_property_count,
        "native_lofty_owner_email_eligible_property_count": native_email_eligible_property_count,
        "native_lofty_owner_email_cooldown_held_property_count": native_email_cooldown_held_property_count,
        "native_lofty_owner_email_cooldown_held_property_keys": sorted(property_cooldown_hold_keys_set)[:100],
        "native_lofty_owner_email_current_month_held_property_count": native_email_current_month_held_property_count,
        "native_lofty_owner_email_current_month_held_property_keys": sorted(native_sent_hold_keys)[:100],
        "native_lofty_owner_email_idempotency_held_property_count": native_email_idempotency_held_property_count,
        "native_lofty_owner_email_idempotency_held_property_keys": sorted(native_email_idempotency_hold_keys)[:100],
        "native_lofty_owner_email_idempotency_held_properties": [
            str(packet.get("property_name") or "")
            for packet in native_email_held_packets
            if str(packet.get("property_name") or "").strip()
        ][:100],
        "native_lofty_owner_email_financially_held_property_count": len(held_property_names),
        "native_lofty_owner_email_financially_held_properties": held_property_names,
        "native_lofty_owner_email_property_coverage_ok": native_owner_email_property_coverage_ok,
        "native_lofty_owner_email_property_coverage_policy": "Every available active runtime-map property not under an explicit property-scoped financial hold or per-property cooldown hold must either produce exactly one signal-only native Lofty owner email packet or be reported as held; held properties remain unsent.",
        "native_lofty_owner_email_previews": native_previews[:50],
        "non_native_signal_only_owner_email_allowed": non_native_signal_only_owner_email_allowed,
        "non_native_recipient_csv_required": not native_only,
        "non_native_recipient_csv_exists": recipients_csv.is_file(),
        "non_native_recipient_issue_count": len(non_native_input_issues),
        "non_native_recipient_issues": non_native_input_issues,
        "sent_state_file": str(sent_state_file),
        "sent_state_status": state.get("status", "ok") if isinstance(state, dict) else "unknown",
        "sent_state_month": sent_state_month,
        "already_sent_for_run_month": already_sent,
        "sent_property_count_for_run_month": len(sent_property_keys),
        "sent_property_keys_for_run_month": sent_property_keys[:100],
        "reconstructed_publish_state_count": len(reconstructed_sent_evidence),
        "reconstructed_publish_state_evidence": reconstructed_sent_evidence[:100],
        "max_once_monthly_ok": not already_sent,
        "property_email_cooldown_days": PROPERTY_EMAIL_COOLDOWN_DAYS,
        "property_email_cooldown_ok": not property_cooldown_issue_list,
        "property_email_cooldown_issue_count": len(property_cooldown_issue_list),
        "property_email_cooldown_issues": property_cooldown_issue_list[:25],
        "subject": subject,
        "dry_run": dry_run,
        "send_requested": send,
        "send_env": SEND_ENV,
        "send_env_enabled": os.environ.get(SEND_ENV) == "1",
        "gws_available": bool(resolved_gws),
        "gws_binary": resolved_gws or None,
        "property_count": property_count,
        "available_property_count": available_property_count,
        "property_unavailable_count": property_unavailable_count,
        "property_unavailable_bounded": property_unavailable[:25],
        "property_unavailable_reason_counts": reason_counts(property_unavailable),
        "property_gap_csv": str(property_gap_csv),
        "property_gap_csv_written": property_gap_csv_written,
        "property_gap_csv_fields": list(PROPERTY_GAP_CSV_FIELDS),
        "recipient_count": recipient_count,
        "packet_count": len(packets),
        "full_history_leak_count": full_history_leak_count,
        "full_history_guard_issue_count": full_history_guard_issue_count,
        "body_guard_issue_count": body_guard_issue_count,
        "native_property_load_issue_count": len(native_property_issues),
        "native_property_load_issues": native_property_issues[:10],
        "email_body_max_chars": MAX_EMAIL_BODY_CHARS,
        "email_body_max_lines": MAX_EMAIL_BODY_LINES,
        "property_update_max_chars": MAX_PROPERTY_UPDATE_CHARS,
        "property_update_max_lines": MAX_PROPERTY_UPDATE_LINES,
        "financial_summary_enriched_property_count": financial_summary_enriched_count,
        "monthly_financial_summary_present_property_count": financial_summary_present_count,
        "property_unavailable_financial_summary_enriched_count": unavailable_financial_summary_enriched_count,
        "property_unavailable_monthly_financial_summary_present_count": unavailable_financial_summary_present_count,
        "monthly_financial_summary_present_total_property_count": total_financial_summary_present_count,
        "monthly_financial_summary_required_property_count": financial_summary_required_property_count,
        "monthly_financial_summary_missing_property_count": missing_financial_summary_present_count,
        "property_unavailable_candidate_update_source_count": unavailable_candidate_update_source_count,
        "property_unavailable_candidate_update_approval_target_count": unavailable_candidate_update_approval_target_count,
        "property_unavailable_candidate_financial_approval_target_count": unavailable_candidate_financial_approval_target_count,
        "packet_digest": packet_digest,
        "safe_to_send_now": safe_to_send,
        "preview_dir": str(out_dir),
        "preview_file_write_allowed": preview_file_write_allowed,
        "preview_write_blocked_reason": preview_write_blocked_reason,
        "unsafe_preview_packet_count": unsafe_preview_packet_count,
        "stale_preview_file_removed_count": stale_preview_file_removed_count,
        "stale_preview_cleanup_error_count": len(stale_preview_cleanup_errors),
        "stale_preview_cleanup_errors": stale_preview_cleanup_errors[:10],
        "previews": previews,
        "packets_bounded": [
            {
                "email_digest": packet["email_digest"],
                "property_count": packet["property_count"],
                "property_ids": packet["property_ids"],
                "body_digest": packet["body_digest"],
                "property_update_marker_count": packet["property_update_marker_count"],
                "dated_update_heading_count": packet["dated_update_heading_count"],
                "body_char_count": packet["body_char_count"],
                "body_line_count": packet["body_line_count"],
                "property_send_keys": packet_property_send_keys(packet),
                "body_guard_issue_count": len(packet.get("body_guard_issues") or []),
                "full_history_guard_issue_count": sum(
                    1 for issue_text in packet.get("body_guard_issues") or [] if full_history_leak_issue(str(issue_text))
                ),
                "body_guard_issues": list(packet.get("body_guard_issues") or [])[:10],
                "properties_bounded": list(packet.get("properties_bounded") or [])[:25],
            }
            for packet in packets[:25]
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build signal-only Lofty monthly owner update email evidence")
    parser.add_argument("--runtime-map", required=True, type=Path)
    parser.add_argument("--recipients-csv", type=Path, default=DEFAULT_RECIPIENTS_CSV)
    parser.add_argument("--run-month", default=current_run_month())
    parser.add_argument("--sent-state-file", type=Path, default=DEFAULT_SENT_STATE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--subject", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--allow-portfolio-recipients", action="store_true")
    parser.add_argument(
        "--native-only",
        action="store_true",
        help="Build only native Lofty owner-email readiness/previews; do not require or template a non-native recipients CSV.",
    )
    parser.add_argument("--live-update-capture-report", type=Path, default=None)
    parser.add_argument("--listing-cleanup-queue-report", type=Path, default=None)
    parser.add_argument("--review-candidate-packet-report", type=Path, default=DEFAULT_REVIEW_CANDIDATE_PACKET)
    parser.add_argument("--discord-send-report", type=Path, default=DEFAULT_DISCORD_SEND_REPORT)
    parser.add_argument("--discord-plan-validation-report", type=Path, default=DEFAULT_DISCORD_PLAN_VALIDATION_REPORT)
    parser.add_argument("--guild-test-post-report", type=Path, default=None)
    parser.add_argument("--no-require-guild-test-post-before-email", action="store_true")
    parser.add_argument("--gws-binary", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.native_only and args.send:
        raise SystemExit("Refusing --send with --native-only; native Lofty sends must go through the guarded lofty-pm-mcp send_emails path")
    subject = args.subject.strip() or f"Lofty monthly owner update - {args.run_month}"
    if args.native_only:
        packets = []
        input_issues = []
        property_count = 0
        available_property_count = 0
        recipient_count = 0
        property_unavailable_count = 0
        property_unavailable = []
        property_financial_summary_enriched_count = 0
        property_financial_summary_present_count = 0
    else:
        (
            packets,
            input_issues,
            property_count,
            available_property_count,
            recipient_count,
            property_unavailable_count,
            property_unavailable,
            property_financial_summary_enriched_count,
            property_financial_summary_present_count,
        ) = collect_packets(
            args.runtime_map,
            args.recipients_csv,
            args.run_month,
            args.allow_portfolio_recipients,
            args.live_update_capture_report,
            args.listing_cleanup_queue_report,
            args.review_candidate_packet_report,
        )
    report = build_report(
        runtime_map=args.runtime_map,
        recipients_csv=args.recipients_csv,
        run_month=args.run_month,
        sent_state_file=args.sent_state_file,
        out_dir=args.out_dir,
        subject=subject,
        dry_run=args.dry_run,
        send=args.send,
        allow_portfolio_recipients=args.allow_portfolio_recipients,
        live_update_capture_report=args.live_update_capture_report,
        listing_cleanup_queue_report=args.listing_cleanup_queue_report,
        candidate_packet_report=args.review_candidate_packet_report,
        discord_send_report=args.discord_send_report,
        discord_plan_validation_report=args.discord_plan_validation_report,
        guild_test_post_report=args.guild_test_post_report,
        require_guild_test_post_before_email=not args.no_require_guild_test_post_before_email,
        gws_binary=args.gws_binary,
        prepared_packets=packets,
        prepared_issues=input_issues,
        prepared_property_count=property_count,
        prepared_available_property_count=available_property_count,
        prepared_recipient_count=recipient_count,
        prepared_property_unavailable_count=property_unavailable_count,
        prepared_property_unavailable=property_unavailable,
        prepared_property_financial_summary_enriched_count=property_financial_summary_enriched_count,
        prepared_property_financial_summary_present_count=property_financial_summary_present_count,
        native_only=args.native_only,
    )
    send_results: list[dict[str, Any]] = []
    if report["safe_to_send_now"]:
        packet_by_digest = {packet["email_digest"]: packet for packet in packets}
        for preview in report["previews"]:
            packet = next(packet for packet in report["packets_bounded"] if packet["email_digest"] == preview["email_digest"])
            send_packet = packet_by_digest[preview["email_digest"]]
            preview_body = Path(preview["path"]).read_text(encoding="utf-8")
            body = send_packet["body_text"]
            body_guard_issues = []
            if preview_body != body:
                body_guard_issues.append("preview_body_mismatch")
                body_guard_issues.extend(
                    f"preview:{item}"
                    for item in email_body_guard_issues(
                        preview_body,
                        int(packet["property_count"]),
                        send_packet.get("source_properties"),
                    )
                )
            body_guard_issues.extend(email_body_guard_issues(body, int(packet["property_count"]), send_packet.get("source_properties")))
            if body_guard_issues:
                send_results.append(
                    {
                        "email_digest": preview["email_digest"],
                        "body_digest": send_packet["body_digest"],
                        "preview_body_digest": preview.get("body_digest"),
                        "packet_digest": report["packet_digest"],
                        "return_code": 90,
                        "stdout_tail": "",
                        "stderr_tail": "owner email body guard blocked send: " + "; ".join(body_guard_issues),
                        "property_ids": packet["property_ids"],
                        "body_guard_issues": body_guard_issues,
                    }
                )
                continue
            result = send_email_gws(report["gws_binary"], send_packet["email"], subject, body)
            send_results.append(
                {
                    "email_digest": preview["email_digest"],
                    "body_digest": send_packet["body_digest"],
                    "preview_body_digest": preview.get("body_digest"),
                    "packet_digest": report["packet_digest"],
                    "return_code": result.returncode,
                    "stdout_tail": result.stdout[-500:],
                    "stderr_tail": result.stderr[-500:],
                    "property_ids": packet["property_ids"],
                }
            )
    send_failed_count = sum(1 for result in send_results if result["return_code"] != 0)
    if report["safe_to_send_now"] and not send_results:
        report["issues"].append("owner email send produced no send results")
    if send_failed_count:
        report["issues"].append(f"owner email send failed or was blocked for {send_failed_count} packet(s)")
    if report["issues"]:
        report["status"] = "review"
        report["classification"] = "lofty-monthly-owner-email-packet-review"
        report["issue_classes"] = [ISSUE_CLASS]
        report["issue_count"] = len(report["issues"])
        report["safe_to_send_now"] = False
    if report["safe_to_send_now"] and send_results and send_failed_count == 0:
        sent_at = iso_z()
        property_sent_at = sent_state_property_map(read_json(args.sent_state_file))
        property_sent_month = sent_state_property_month_map(read_json(args.sent_state_file))
        for packet in packets:
            for key in packet_property_send_keys(packet):
                property_sent_at[key] = sent_at
                property_sent_month[key] = args.run_month
        args.sent_state_file.parent.mkdir(parents=True, exist_ok=True)
        args.sent_state_file.write_text(
            json.dumps(
                {
                    "run_month": args.run_month,
                    "sent_at": sent_at,
                    "packet_digest": report["packet_digest"],
                    "body_digests": sorted(
                        str(result.get("body_digest") or "")
                        for result in send_results
                        if str(result.get("body_digest") or "")
                    ),
                    "recipient_count": report["recipient_count"],
                    "packet_count": report["packet_count"],
                    "property_email_cooldown_days": PROPERTY_EMAIL_COOLDOWN_DAYS,
                    "property_sent_at": property_sent_at,
                    "property_sent_month": property_sent_month,
                    "sender": "gws",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        report["sent_state_written"] = True
        report["safe_to_send_now"] = False
    else:
        report["sent_state_written"] = False
    report["send_result_count"] = len(send_results)
    report["send_failed_count"] = send_failed_count
    report["send_results_bounded"] = send_results[:25]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ["status", "issue_count", "recipient_count", "packet_count", "safe_to_send_now", "send_result_count", "send_failed_count"]}, indent=2))
    return 0 if report["status"] == "ok" and send_failed_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
