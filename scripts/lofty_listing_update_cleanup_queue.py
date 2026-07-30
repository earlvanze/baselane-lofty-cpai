#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).absolute().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from lofty_monthly_exclusions import DEFAULT_MANUAL_EXCLUDED_PROPERTIES
except ImportError:  # pragma: no cover - keeps the standalone script usable if copied without helpers.
    DEFAULT_MANUAL_EXCLUDED_PROPERTIES = ()

MAX_LISTING_UPDATE_CHARS = 3500
MAX_LISTING_UPDATE_LINES = 80
PROPERTY_UPDATE_MARKER_RE = re.compile(r"(?mi)^\s*-\s+\*{0,2}\s*Property Update\s*\(")
PROPERTY_UPDATE_ENTRY_RE = re.compile(r"(?ms)^\s*-\s+\*{0,2}\s*Property Update\s*\((\d{2}/\d{2}/\d{4})\):\*{0,2}\s*(.*)$")
DATED_UPDATE_HEADING_RE = re.compile(r"(?mi)^\s*##\s+\d{4}-\d{2}-\d{2}\s*$")
PROPERTY_UPDATES_HEADER_RE = re.compile(r"(?mi)^\s*#\s+Property Updates\s*$")
INTERNAL_OWNER_UPDATE_CONTENT_RE = re.compile(
    r"(?mi)^\s*(?:##\s*)?(?:Monthly send checklist|Monthly review checklist|Internal context\b|Internal operations context\b|Recent Public(?:/Sale)? file activity:)"
    r"|^\s*-\s+\[\s*\]\s+"
    r"|remove before (?:sending|owner-facing publication)"
    r"|do not use native Lofty owner email"
    r"|send-property-updates"
)
LOFTY_RESERVE_SUMMARY_RE = re.compile(r"(?mi)^\s*-\s*Lofty-held current maintenance reserve:\s*-?\$[\d,]+\.\d{2}\s*$")
ECO_GL_SUMMARY_RE = re.compile(r"(?mi)^\s*-\s*ECO GL Column E sum:\s*-?\$[\d,]+\.\d{2}(?:\s+\(\d+\s+rows\))?\s*$")


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


def parse_dated_entries(md_text: str) -> list[dict[str, str]]:
    parts = re.split(r"(?m)^##\s+(\d{4}-\d{2}-\d{2})\s*$", md_text)
    entries: list[dict[str, str]] = []
    if len(parts) < 3:
        return entries
    for index in range(1, len(parts), 2):
        if index + 1 >= len(parts):
            break
        body = parts[index + 1].strip()
        if body:
            entries.append({"date": parts[index], "body": body})
    return entries


def trim_embedded_update_history(text: str, *, current_marker_included: bool = True) -> str:
    text = (text or "").strip()
    markers = list(PROPERTY_UPDATE_MARKER_RE.finditer(text))
    if markers:
        trim_marker = markers[1] if current_marker_included and len(markers) > 1 else markers[0] if not current_marker_included else None
        if trim_marker is not None:
            text = text[: trim_marker.start()].rstrip()
    dated_headings = list(DATED_UPDATE_HEADING_RE.finditer(text))
    if dated_headings:
        text = text[: dated_headings[0].start()].rstrip()
    updates_headers = list(PROPERTY_UPDATES_HEADER_RE.finditer(text))
    if updates_headers:
        text = text[: updates_headers[0].start()].rstrip()
    return text.strip()


def format_money(value: object) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "Not available"
    prefix = "-$" if amount < 0 else "$"
    return f"{prefix}{abs(amount):,.2f}"


def verified_candidate_summary(summary: dict[str, Any] | None) -> bool:
    return (
        isinstance(summary, dict)
        and summary.get("lofty_curr_maintenance_reserve") is not None
        and summary.get("eco_gl_column_e_status") == "ok"
        and summary.get("eco_gl_column_e_sum") is not None
    )


def monthly_financial_summary_guard_issues(text: str, *, required_summary_count: int) -> list[str]:
    if required_summary_count <= 0:
        return []
    issues: list[str] = []
    reserve_count = len(LOFTY_RESERVE_SUMMARY_RE.findall(text or ""))
    eco_count = len(ECO_GL_SUMMARY_RE.findall(text or ""))
    if reserve_count < required_summary_count:
        issues.append(f"lofty_reserve_summary_count={reserve_count}<required={required_summary_count}")
    if eco_count < required_summary_count:
        issues.append(f"eco_gl_column_e_summary_count={eco_count}<required={required_summary_count}")
    return issues


def render_monthly_financial_summary(summary: dict[str, Any]) -> str:
    lines = [
        "Financial summary:",
        f"- Lofty-held current maintenance reserve: {format_money(summary.get('lofty_curr_maintenance_reserve'))}",
        f"- ECO GL Column E sum: {format_money(summary.get('eco_gl_column_e_sum'))}",
    ]
    if summary.get("eco_gl_column_e_row_count") is not None:
        lines[-1] += f" ({int(summary.get('eco_gl_column_e_row_count') or 0)} rows)"
    return "\n".join(lines)


def append_verified_financial_summary_if_missing(text: str, summary: dict[str, Any] | None) -> tuple[str, bool]:
    if not summary or not verified_candidate_summary(summary):
        return text, False
    if not monthly_financial_summary_guard_issues(text, required_summary_count=1):
        return text, True
    return text.rstrip() + "\n\n" + render_monthly_financial_summary(summary) + "\n", True


def load_candidate_packet_records(candidate_packet_report: Path | None, run_month: str | None = None) -> list[dict[str, Any]]:
    if candidate_packet_report is None:
        return []
    data = read_json(candidate_packet_report)
    if data.get("status") in {"missing", "unreadable"}:
        return []
    candidate_run_month = str(data.get("run_month") or "").strip()
    expected_run_month = str(run_month or "").strip()
    if candidate_run_month and expected_run_month and candidate_run_month != expected_run_month:
        return []
    records = data.get("records") if isinstance(data.get("records"), list) else []
    return [record for record in records if isinstance(record, dict)]


def candidate_packet_run_month(candidate_packet_report: Path | None) -> str:
    if candidate_packet_report is None:
        return ""
    data = read_json(candidate_packet_report)
    if data.get("status") in {"missing", "unreadable"}:
        return ""
    return str(data.get("run_month") or "").strip()


def candidate_packet_run_month_matches(candidate_packet_report: Path | None, run_month: str | None) -> bool:
    candidate_run_month = candidate_packet_run_month(candidate_packet_report)
    expected_run_month = str(run_month or "").strip()
    return not candidate_run_month or not expected_run_month or candidate_run_month == expected_run_month


def load_candidate_financial_summaries(
    candidate_packet_report: Path | None,
    run_month: str | None = None,
) -> dict[str, dict[str, Any]]:
    records = load_candidate_packet_records(candidate_packet_report, run_month)
    summaries: dict[str, dict[str, Any]] = {}
    for record in records:
        summary = record.get("monthly_financial_summary") if isinstance(record.get("monthly_financial_summary"), dict) else {}
        if not verified_candidate_summary(summary):
            continue
        summary = {**summary, "candidate_packet_record_property_name": record.get("property_name")}
        keys = set()
        for source_key in (
            "lofty_property_id",
            "property_name",
            "input_property_name",
            "property_path",
            "input_property_path",
            "updates_md",
            "update_approval_target",
            "update_candidate",
            "financials_md",
            "financial_approval_target",
            "financial_candidate",
        ):
            raw_key = str(record.get(source_key) or "").strip()
            if raw_key:
                keys.add(raw_key)
                keys.add(raw_key.lower())
                keys.add(normalize_property_key(raw_key))
                keys |= path_label_keys(raw_key)
        approval_target = Path(str(record.get("update_approval_target") or ""))
        if approval_target.name:
            updates_path = approval_target.with_name("UPDATES.md")
            keys.add(str(updates_path))
            keys.add(normalize_property_key(updates_path))
            keys |= path_label_keys(updates_path)
        for key in keys:
            if key:
                summaries[key] = summary
    return summaries


def candidate_record_metadata(record: dict[str, Any]) -> dict[str, Any]:
    update_candidate = str(record.get("update_candidate") or "").strip()
    update_approval_target = str(record.get("update_approval_target") or "").strip()
    update_candidate_path = Path(update_candidate) if update_candidate else None
    update_approval_target_path = Path(update_approval_target) if update_approval_target else None
    metadata: dict[str, Any] = {
        "candidate_packet_record_property_name": record.get("property_name"),
        "candidate_update_candidate": update_candidate,
        "candidate_update_candidate_exists": bool(update_candidate_path and update_candidate_path.is_file()),
        "candidate_update_approval_target": update_approval_target,
        "candidate_update_approval_target_exists": bool(update_approval_target_path and update_approval_target_path.is_file()),
        "candidate_update_source_mode": record.get("update_source_mode"),
        "candidate_financial_candidate": record.get("financial_candidate"),
        "candidate_financial_candidate_exists": Path(str(record.get("financial_candidate") or "")).is_file()
        if str(record.get("financial_candidate") or "").strip()
        else False,
        "candidate_financial_gate_issues": record.get("financial_candidate_gate_issues")
        if isinstance(record.get("financial_candidate_gate_issues"), list)
        else [],
    }
    candidate_text = ""
    if update_candidate_path and update_candidate_path.is_file():
        candidate_text = update_candidate_path.read_text(encoding="utf-8", errors="replace")
        metadata.update(
            {
                "candidate_update_candidate_char_count": len(candidate_text),
                "candidate_update_candidate_line_count": len(candidate_text.splitlines()),
                "candidate_update_candidate_sha256": sha256_text(candidate_text),
            }
        )
    else:
        metadata.update(
            {
                "candidate_update_candidate_char_count": 0,
                "candidate_update_candidate_line_count": 0,
                "candidate_update_candidate_sha256": "",
            }
        )
    metadata["candidate_financial_gate_issue_count"] = len(metadata["candidate_financial_gate_issues"])
    return metadata


def candidate_record_keys(record: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for raw_key in (
        str(record.get("lofty_property_id") or "").strip(),
        str(record.get("property_name") or "").strip().lower(),
        str(record.get("updates_md") or "").strip(),
        str(record.get("update_approval_target") or "").strip(),
        str(record.get("update_candidate") or "").strip(),
    ):
        if raw_key:
            keys.add(raw_key)
            keys.add(normalize_property_key(raw_key))
    for source_key in ("property_name", "updates_md", "update_approval_target", "update_candidate"):
        keys |= path_label_keys(record.get(source_key))
    return {key for key in keys if key}


def load_candidate_update_metadata(
    candidate_packet_report: Path | None,
    run_month: str | None = None,
) -> dict[str, dict[str, Any]]:
    records = load_candidate_packet_records(candidate_packet_report, run_month)
    metadata_by_key: dict[str, dict[str, Any]] = {}
    for record in records:
        metadata = candidate_record_metadata(record)
        for key in candidate_record_keys(record):
            metadata_by_key[key] = metadata
    return metadata_by_key


def load_candidate_update_sources(candidate_packet_report: Path | None, run_month: str | None = None) -> dict[str, Path]:
    records = load_candidate_packet_records(candidate_packet_report, run_month)
    sources: dict[str, Path] = {}
    for record in records:
        target = str(record.get("update_approval_target") or "").strip()
        if not target:
            continue
        target_path = Path(target)
        if not target_path.is_file():
            continue
        for key in (
            str(record.get("lofty_property_id") or "").strip(),
            str(record.get("property_name") or "").strip().lower(),
            str(record.get("updates_md") or "").strip(),
        ):
            if key:
                sources[key] = target_path
    return sources


def candidate_summary_for(summaries: dict[str, dict[str, Any]], prop: dict[str, Any], updates_md: Path) -> dict[str, Any] | None:
    for key in property_candidate_keys(prop, updates_md):
        summary = summaries.get(key)
        if summary:
            return summary
    return None


def candidate_update_source_for(sources: dict[str, Path], prop: dict[str, Any], updates_md: Path) -> Path | None:
    return (
        sources.get(str(prop.get("lofty_property_id") or "").strip())
        or sources.get(str(prop.get("property_name") or "").strip().lower())
        or sources.get(str(updates_md))
    )


def property_candidate_keys(prop: dict[str, Any], updates_md: Path) -> list[str]:
    raw_keys = [
        str(prop.get("lofty_property_id") or "").strip(),
        str(prop.get("property_name") or "").strip().lower(),
        str(updates_md),
    ]
    keys: list[str] = []
    for raw_key in raw_keys:
        if raw_key:
            keys.append(raw_key)
            keys.append(normalize_property_key(raw_key))
    for source_key in ("property_name", "property_path", "updates_md"):
        for key in path_label_keys(prop.get(source_key)):
            keys.append(key)
    for key in path_label_keys(str(updates_md)):
        keys.append(key)
    seen: set[str] = set()
    return [key for key in keys if key and not (key in seen or seen.add(key))]


def candidate_update_metadata_for(metadata_by_key: dict[str, dict[str, Any]], prop: dict[str, Any], updates_md: Path) -> dict[str, Any] | None:
    for key in property_candidate_keys(prop, updates_md):
        metadata = metadata_by_key.get(key)
        if metadata:
            return metadata
    return None


def entry_listing_text(entry: dict[str, str], financial_summary: dict[str, Any] | None = None) -> tuple[str, bool]:
    body = trim_embedded_update_history(entry.get("body") or "")
    match = PROPERTY_UPDATE_ENTRY_RE.search(body)
    if match:
        trimmed_body = trim_embedded_update_history(match.group(2), current_marker_included=False)
        text = f"- Property Update ({match.group(1)}):\n{trimmed_body}".strip()
    else:
        text = body.strip()
    text, enriched = append_verified_financial_summary_if_missing(text, financial_summary)
    return text.strip(), enriched


def latest_listing_text(updates_md: Path, financial_summary: dict[str, Any] | None = None) -> tuple[str, str | None, bool]:
    text = updates_md.read_text(encoding="utf-8", errors="replace")
    entries = parse_dated_entries(text)
    if not entries:
        return "", None, False
    latest_text, enriched = entry_listing_text(entries[0], financial_summary=financial_summary)
    return latest_text, entries[0]["date"], enriched


def listing_text_issues(text: str, *, require_monthly_financial_summary: bool = False) -> list[str]:
    text = (text or "").strip()
    issues: list[str] = []
    if not text:
        issues.append("latest_update_empty")
    markers = PROPERTY_UPDATE_MARKER_RE.findall(text)
    if len(markers) < 1:
        issues.append("missing_property_update_entry")
    if len(markers) > 1:
        issues.append(f"multiple_property_update_entries={len(markers)}")
    if DATED_UPDATE_HEADING_RE.search(text):
        issues.append("dated_update_heading")
    if PROPERTY_UPDATES_HEADER_RE.search(text):
        issues.append("property_updates_header")
    if len(text) > MAX_LISTING_UPDATE_CHARS:
        issues.append(f"oversized_listing_update_chars={len(text)}>{MAX_LISTING_UPDATE_CHARS}")
    line_count = len(text.splitlines())
    if line_count > MAX_LISTING_UPDATE_LINES:
        issues.append(f"oversized_listing_update_lines={line_count}>{MAX_LISTING_UPDATE_LINES}")
    if INTERNAL_OWNER_UPDATE_CONTENT_RE.search(text):
        issues.append("internal_owner_update_content_detected")
    issues.extend(
        monthly_financial_summary_guard_issues(
            text,
            required_summary_count=1 if require_monthly_financial_summary else 0,
        )
    )
    return issues


def shell_command(parts: list[object]) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts if str(part or "").strip())


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def stable_digest(payload: Any) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def normalize_property_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def path_label_keys(value: object) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()
    keys = {normalize_property_key(text)}
    try:
        path = Path(text)
        if path.name in {"UPDATES.md", "FINANCIALS.md"}:
            keys.add(normalize_property_key(path.parents[1].name if len(path.parents) > 1 else ""))
        else:
            keys.add(normalize_property_key(path.name))
    except Exception:  # noqa: BLE001
        pass
    return {key for key in keys if key}


def exclusion_index(live_capture: dict[str, Any]) -> dict[str, Any]:
    excluded_names = [str(name) for name in live_capture.get("excluded_property_names") or [] if str(name or "").strip()]
    manual_names = [
        str(name)
        for name in [*(live_capture.get("manual_excluded_property_names") or []), *DEFAULT_MANUAL_EXCLUDED_PROPERTIES]
        if str(name or "").strip()
    ]
    manual_names = list(dict.fromkeys(manual_names))
    skipped_records = live_capture.get("skipped_index_records") if isinstance(live_capture.get("skipped_index_records"), list) else []
    keys: dict[str, str] = {}
    for name in excluded_names:
        keys[normalize_property_key(name)] = "yhome_or_index_excluded"
    for name in manual_names:
        keys[normalize_property_key(name)] = "manual_excluded"
    for record in skipped_records:
        if not isinstance(record, dict):
            continue
        reason = str(record.get("status") or record.get("raw_status") or "skipped_index")
        for source_key in ("property_path", "input_property_path", "property_name", "name"):
            for key in path_label_keys(record.get(source_key)):
                keys[key] = reason
    return {
        "keys": keys,
        "excluded_property_count": len(excluded_names),
        "excluded_property_names": excluded_names,
        "manual_excluded_property_names": manual_names,
        "manual_excluded_property_count": len(manual_names),
        "skipped_index_count": len(skipped_records),
        "skipped_index_status_counts": live_capture.get("skipped_index_status_counts") or {},
        "yhome_transition_guard_status": (live_capture.get("yhome_transition_guard") or {}).get("status"),
        "yhome_transition_column_b_rule_ok": (live_capture.get("yhome_transition_guard") or {}).get("column_b_rule_ok") is True,
    }


def property_exclusion_reason(prop: dict[str, Any], exclusions: dict[str, Any]) -> str:
    keys = exclusions.get("keys") if isinstance(exclusions.get("keys"), dict) else {}
    candidates = {
        normalize_property_key(prop.get("property_name")),
        normalize_property_key(prop.get("property_path")),
        normalize_property_key(prop.get("updates_md")),
    }
    candidates |= path_label_keys(prop.get("property_path"))
    candidates |= path_label_keys(prop.get("updates_md"))
    for key in candidates:
        if key and key in keys:
            return str(keys[key])
    prop_key = normalize_property_key(prop.get("property_name"))
    for manual_name in exclusions.get("manual_excluded_property_names") or []:
        manual_key = normalize_property_key(manual_name)
        if manual_key and (manual_key in prop_key or prop_key in manual_key):
            return "manual_excluded"
    return ""


def live_record_key(record: dict[str, Any]) -> str:
    return str(record.get("lofty_property_id") or record.get("updates_md") or record.get("property_name") or "")


def live_records_by_key(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for record in records:
        for key in {
            str(record.get("lofty_property_id") or ""),
            str(record.get("updates_md") or ""),
            str(record.get("property_name") or ""),
        }:
            if key:
                by_key[key] = record
    return by_key


def build_record(prop: dict[str, Any], live_record: dict[str, Any] | None, args: argparse.Namespace, exclusions: dict[str, Any] | None = None) -> dict[str, Any]:
    updates_md = Path(str(prop.get("updates_md") or ""))
    exclusions = exclusions or {}
    excluded_reason = property_exclusion_reason(prop, exclusions)
    candidate_update_metadata = candidate_update_metadata_for(args.candidate_update_metadata, prop, updates_md)
    record: dict[str, Any] = {
        "property_name": prop.get("property_name"),
        "lofty_property_id": prop.get("lofty_property_id"),
        "updates_md": str(updates_md),
        "excluded_from_live_cleanup": bool(excluded_reason),
        "exclude_reason": excluded_reason,
        "live_capture_status": (live_record or {}).get("status"),
        "live_snapshot_path": (live_record or {}).get("snapshot_path"),
        "live_snapshot_listing_issues": (live_record or {}).get("live_snapshot_listing_issues") or [],
        "live_snapshot_listing_issue_count": len((live_record or {}).get("live_snapshot_listing_issues") or []),
        "mutates_lofty_listing": False,
        "sends_owner_email": False,
    }
    if candidate_update_metadata:
        record.update(candidate_update_metadata)
    else:
        record.update(
            {
                "candidate_packet_record_property_name": None,
                "candidate_update_candidate": "",
                "candidate_update_candidate_exists": False,
                "candidate_update_candidate_char_count": 0,
                "candidate_update_candidate_line_count": 0,
                "candidate_update_candidate_sha256": "",
                "candidate_update_approval_target": "",
                "candidate_update_approval_target_exists": False,
                "candidate_update_source_mode": None,
                "candidate_financial_candidate": None,
                "candidate_financial_candidate_exists": False,
                "candidate_financial_gate_issues": [],
                "candidate_financial_gate_issue_count": 0,
            }
        )
    if excluded_reason:
        record.update({"status": "blocked_excluded_property", "reason": f"excluded from live listing cleanup: {excluded_reason}"})
        return record
    if not live_record:
        record.update({"status": "blocked_missing_live_capture_record", "reason": "no live update capture record for active property"})
        return record
    if not updates_md.is_file():
        record.update({"status": "blocked_missing_updates_md", "reason": "canonical UPDATES.md missing"})
        return record
    candidate_summary = candidate_summary_for(args.candidate_summaries, prop, updates_md)
    approved_update_source = candidate_update_source_for(args.candidate_update_sources, prop, updates_md)
    candidate_listing_issues: list[str] = []
    candidate_listing_date = None
    candidate_financial_summary_enriched = False
    candidate_path_text = str(record.get("candidate_update_candidate") or "").strip()
    if candidate_path_text and Path(candidate_path_text).is_file():
        candidate_listing_text, candidate_listing_date, candidate_financial_summary_enriched = latest_listing_text(
            Path(candidate_path_text),
            financial_summary=candidate_summary,
        )
        candidate_listing_issues = listing_text_issues(
            candidate_listing_text,
            require_monthly_financial_summary=args.require_monthly_financial_summary,
        )
    record.update(
        {
            "candidate_update_candidate_latest_date": candidate_listing_date,
            "candidate_update_quality_issues": candidate_listing_issues,
            "candidate_update_quality_issue_count": len(candidate_listing_issues),
            "candidate_update_financial_summary_enriched": candidate_financial_summary_enriched,
        }
    )
    updates_source = approved_update_source or updates_md
    latest_text, latest_date, financial_summary_enriched = latest_listing_text(updates_source, financial_summary=candidate_summary)
    issues = listing_text_issues(latest_text, require_monthly_financial_summary=args.require_monthly_financial_summary)
    record.update(
        {
            "latest_update_date": latest_date,
            "latest_update_source": str(updates_source),
            "latest_update_source_type": "approved_update_artifact" if approved_update_source else "updates_md",
            "latest_listing_update_char_count": len(latest_text),
            "latest_listing_update_line_count": len(latest_text.splitlines()),
            "latest_listing_update_sha256": sha256_text(latest_text),
            "latest_listing_update_issues": issues,
            "financial_summary_enriched": financial_summary_enriched,
            "requires_monthly_financial_summary": args.require_monthly_financial_summary,
        }
    )
    if issues:
        record.update({"status": "blocked_unsafe_latest_update", "reason": ",".join(issues)})
        return record
    if not record["live_snapshot_listing_issues"]:
        record.update({"status": "blocked_not_history_leak_cleanup", "reason": "live mismatch is not a detected full-history/oversized listing-field leak"})
        return record

    dry_run_command = [
        args.python_bin,
        args.publish_script,
        "--property",
        updates_md,
        "--map-file",
        args.runtime_map,
        "--send-interval-days",
        args.send_interval_days,
        "--skip-send",
        "--dry-run",
    ]
    if args.review_candidate_packet_report:
        dry_run_command.extend(["--review-candidate-packet-report", args.review_candidate_packet_report])
    if args.run_month:
        dry_run_command.extend(["--run-month", args.run_month])
    if approved_update_source:
        dry_run_command.extend(["--approved-update-source", approved_update_source])
    if args.require_monthly_financial_summary:
        dry_run_command.append("--require-monthly-financial-summary")
    live_command = [
        args.python_bin,
        args.publish_script,
        "--property",
        updates_md,
        "--map-file",
        args.runtime_map,
        "--send-interval-days",
        args.send_interval_days,
        "--skip-send",
    ]
    if args.review_candidate_packet_report:
        live_command.extend(["--review-candidate-packet-report", args.review_candidate_packet_report])
    if args.run_month:
        live_command.extend(["--run-month", args.run_month])
    if approved_update_source:
        live_command.extend(["--approved-update-source", approved_update_source])
    if args.require_monthly_financial_summary:
        live_command.append("--require-monthly-financial-summary")
    record.update(
        {
            "status": "ready_listing_cleanup",
            "reason": "live Lofty listing field needs guarded full-history repair; owner email remains disabled",
            "listing_update_scope": "full_history",
            "dry_run_command": shell_command(dry_run_command),
            "live_apply_command_requires_explicit_approval": shell_command(live_command),
        }
    )
    return record


def count_statuses(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        status = str(record.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def write_ready_cleanup_artifacts(report_path: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    ready_records = [record for record in records if record.get("status") == "ready_listing_cleanup"]
    candidate_approval_records = [
        record
        for record in records
        if record.get("status") == "blocked_unsafe_latest_update"
        and record.get("candidate_update_candidate_exists") is True
        and int(record.get("candidate_update_quality_issue_count") or 0) == 0
        and int(record.get("candidate_financial_gate_issue_count") or 0) == 0
        and not record.get("candidate_update_approval_target_exists")
        and str(record.get("candidate_update_candidate") or "").strip()
        and str(record.get("candidate_update_approval_target") or "").strip()
    ]
    artifact_dir = report_path.parent
    artifact_dir.mkdir(parents=True, exist_ok=True)
    base_name = report_path.stem
    csv_path = artifact_dir / f"{base_name}.ready.csv"
    candidate_approval_csv_path = artifact_dir / f"{base_name}.candidate-approval.csv"
    dry_run_commands_path = artifact_dir / f"{base_name}.dry-run-commands.sh"
    live_apply_commands_path = artifact_dir / f"{base_name}.live-apply-commands.requires-explicit-approval.sh"
    candidate_approval_commands_path = artifact_dir / f"{base_name}.candidate-approval-copy.requires-current-rent-roll-and-explicit-approval.sh"
    dry_run_verify_report_path = artifact_dir / "lofty_listing_cleanup_dry_run_verify.json"
    apply_preflight_report_path = artifact_dir / f"{base_name}.live-apply-preflight.json"
    apply_preflight_script = Path(__file__).absolute().with_name("lofty_listing_cleanup_apply_preflight.py")
    monthly_readiness_report_path = artifact_dir / "baselane_financials_monthly_readiness.json"

    fieldnames = [
        "property_name",
        "lofty_property_id",
        "latest_update_date",
        "latest_listing_update_char_count",
        "latest_listing_update_line_count",
        "latest_listing_update_sha256",
        "latest_update_source",
        "latest_update_source_type",
        "financial_summary_enriched",
        "live_snapshot_path",
        "reason",
        "dry_run_command",
        "live_apply_command_requires_explicit_approval",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in ready_records:
            writer.writerow({key: record.get(key, "") for key in fieldnames})
    candidate_approval_fieldnames = [
        "property_name",
        "lofty_property_id",
        "candidate_update_candidate",
        "candidate_update_candidate_sha256",
        "candidate_update_approval_target",
        "candidate_update_quality_issue_count",
        "candidate_financial_gate_issue_count",
        "candidate_update_approval_copy_command_requires_current_rent_roll_and_explicit_approval",
    ]
    for record in candidate_approval_records:
        candidate = Path(str(record.get("candidate_update_candidate") or ""))
        target = Path(str(record.get("candidate_update_approval_target") or ""))
        record["candidate_update_approval_copy_command_requires_current_rent_roll_and_explicit_approval"] = shell_command(
            [
                "bash",
                "-lc",
                f"test -d {shlex.quote(str(target.parent))} && test ! -e {shlex.quote(str(target))} && cp -- {shlex.quote(str(candidate))} {shlex.quote(str(target))}",
            ]
        )
    with candidate_approval_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=candidate_approval_fieldnames)
        writer.writeheader()
        for record in candidate_approval_records:
            writer.writerow({key: record.get(key, "") for key in candidate_approval_fieldnames})

    dry_run_lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "# Generated review-only commands. These do not send owner email and include --dry-run.",
    ]
    live_apply_lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "# DO NOT RUN WITHOUT EXPLICIT OPERATOR APPROVAL.",
        "# Each command repairs the listing field with the full canonical UPDATES.md history and keeps owner email disabled with --skip-send.",
        "# Canonical UPDATES.md history remains untouched; owner emails are handled separately by signal-only send guards.",
        "# Refuses to run unless the exact dry-run cleanup verification report passed for this queue digest.",
        "# If monthly readiness is held, repair-only cleanup also requires LOFTY_LISTING_CLEANUP_REPAIR_ONLY_APPROVED=1.",
        '[[ "${LOFTY_LISTING_CLEANUP_APPLY_APPROVED:-}" == "1" ]] || { echo "Refusing live Lofty listing cleanup: set LOFTY_LISTING_CLEANUP_APPLY_APPROVED=1 after explicit approval." >&2; exit 64; }',
    ]
    ready_manifest = [
        {
            "property_name": record.get("property_name"),
            "lofty_property_id": record.get("lofty_property_id"),
            "updates_md": record.get("updates_md"),
            "latest_update_date": record.get("latest_update_date"),
            "latest_update_source": record.get("latest_update_source"),
            "latest_update_source_type": record.get("latest_update_source_type"),
            "latest_listing_update_sha256": record.get("latest_listing_update_sha256"),
            "latest_listing_update_char_count": record.get("latest_listing_update_char_count"),
            "latest_listing_update_line_count": record.get("latest_listing_update_line_count"),
            "financial_summary_enriched": record.get("financial_summary_enriched") is True,
        }
        for record in ready_records
    ]
    ready_cleanup_idempotency_digest = stable_digest(ready_manifest)
    candidate_approval_manifest = [
        {
            "property_name": record.get("property_name"),
            "lofty_property_id": record.get("lofty_property_id"),
            "updates_md": record.get("updates_md"),
            "candidate_update_candidate": record.get("candidate_update_candidate"),
            "candidate_update_candidate_sha256": record.get("candidate_update_candidate_sha256"),
            "candidate_update_approval_target": record.get("candidate_update_approval_target"),
        }
        for record in candidate_approval_records
    ]
    candidate_approval_idempotency_digest = stable_digest(candidate_approval_manifest)
    dry_run_lines.append(f"# ready_cleanup_idempotency_digest={ready_cleanup_idempotency_digest}")
    live_apply_lines.append(f"# ready_cleanup_idempotency_digest={ready_cleanup_idempotency_digest}")
    live_apply_lines.append(
        '[[ "${LOFTY_LISTING_CLEANUP_APPLY_DIGEST:-}" == "'
        + ready_cleanup_idempotency_digest
        + '" ]] || { echo "Refusing live Lofty listing cleanup: set LOFTY_LISTING_CLEANUP_APPLY_DIGEST='
        + ready_cleanup_idempotency_digest
        + ' after reviewing this exact queue." >&2; exit 65; }'
    )
    preflight_command = (
        'repair_only_flag=(); '
        'if [[ "${LOFTY_LISTING_CLEANUP_REPAIR_ONLY_APPROVED:-}" == "1" ]]; then repair_only_flag+=(--allow-monthly-readiness-hold-for-repair); fi; '
        f"python3 {shlex.quote(str(apply_preflight_script))} "
        f"--queue-report {shlex.quote(str(report_path))} "
        f'--verify-report "${{LOFTY_LISTING_CLEANUP_DRY_RUN_VERIFY_REPORT:-{dry_run_verify_report_path}}}" '
        f'--monthly-readiness-report "${{LOFTY_LISTING_CLEANUP_MONTHLY_READINESS_REPORT:-{monthly_readiness_report_path}}}" '
        '--expected-digest "${LOFTY_LISTING_CLEANUP_APPLY_DIGEST}" '
        f'--report "${{LOFTY_LISTING_CLEANUP_APPLY_PREFLIGHT_REPORT:-{apply_preflight_report_path}}}" '
        '"${repair_only_flag[@]}"'
    )
    live_apply_lines.append(preflight_command)
    for record in ready_records:
        label = str(record.get("property_name") or record.get("lofty_property_id") or "property")
        dry_run_lines.append("")
        dry_run_lines.append(f"# {label}")
        dry_run_lines.append(str(record.get("dry_run_command") or ""))
        live_apply_lines.append("")
        live_apply_lines.append(f"# {label}")
        live_apply_lines.append(str(record.get("live_apply_command_requires_explicit_approval") or ""))

    candidate_approval_lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "# DO NOT RUN UNTIL RENT ROLL IS CURRENT AND OPERATOR APPROVES THESE UPDATE CANDIDATES.",
        "# Copies reviewed current update candidates to monthly approved update artifacts.",
        "# Does not modify UPDATES.md, publish Lofty listing updates, or send owner email.",
        f"# candidate_update_approval_idempotency_digest={candidate_approval_idempotency_digest}",
        '[[ "${LOFTY_LISTING_UPDATE_APPROVAL_COPY_APPROVED:-}" == "1" ]] || { echo "Refusing update approval copy: set LOFTY_LISTING_UPDATE_APPROVAL_COPY_APPROVED=1 after explicit approval." >&2; exit 64; }',
        '[[ "${LOFTY_LISTING_UPDATE_APPROVAL_CURRENT_RENT_ROLL_CONFIRMED:-}" == "1" ]] || { echo "Refusing update approval copy: current rent roll must be confirmed." >&2; exit 65; }',
        '[[ "${LOFTY_LISTING_UPDATE_APPROVAL_COPY_DIGEST:-}" == "'
        + candidate_approval_idempotency_digest
        + '" ]] || { echo "Refusing update approval copy: set LOFTY_LISTING_UPDATE_APPROVAL_COPY_DIGEST='
        + candidate_approval_idempotency_digest
        + ' after reviewing this exact queue." >&2; exit 66; }',
    ]
    for record in candidate_approval_records:
        command = str(record.get("candidate_update_approval_copy_command_requires_current_rent_roll_and_explicit_approval") or "")
        label = str(record.get("property_name") or record.get("lofty_property_id") or "property")
        candidate_approval_lines.append("")
        candidate_approval_lines.append(f"# {label}")
        candidate_approval_lines.append(command)

    dry_run_commands_path.write_text("\n".join(dry_run_lines).rstrip() + "\n", encoding="utf-8")
    live_apply_commands_path.write_text("\n".join(live_apply_lines).rstrip() + "\n", encoding="utf-8")
    candidate_approval_commands_path.write_text("\n".join(candidate_approval_lines).rstrip() + "\n", encoding="utf-8")
    return {
        "ready_cleanup_csv": str(csv_path),
        "candidate_update_approval_csv": str(candidate_approval_csv_path),
        "dry_run_commands_file": str(dry_run_commands_path),
        "live_apply_commands_requires_explicit_approval_file": str(live_apply_commands_path),
        "candidate_update_approval_copy_commands_requires_current_rent_roll_and_explicit_approval_file": str(candidate_approval_commands_path),
        "candidate_update_approval_copy_requires_current_rent_roll": True,
        "candidate_update_approval_copy_requires_explicit_approval": True,
        "candidate_update_approval_copy_approval_env_var": "LOFTY_LISTING_UPDATE_APPROVAL_COPY_APPROVED",
        "candidate_update_approval_copy_approval_required_value": "1",
        "candidate_update_approval_copy_current_rent_roll_env_var": "LOFTY_LISTING_UPDATE_APPROVAL_CURRENT_RENT_ROLL_CONFIRMED",
        "candidate_update_approval_copy_current_rent_roll_required_value": "1",
        "candidate_update_approval_copy_digest_env_var": "LOFTY_LISTING_UPDATE_APPROVAL_COPY_DIGEST",
        "candidate_update_approval_copy_digest_required_value": candidate_approval_idempotency_digest,
        "live_apply_approval_env_var": "LOFTY_LISTING_CLEANUP_APPLY_APPROVED",
        "live_apply_approval_env_required_value": "1",
        "live_apply_approval_digest_env_var": "LOFTY_LISTING_CLEANUP_APPLY_DIGEST",
        "live_apply_approval_digest_required_value": ready_cleanup_idempotency_digest,
        "live_apply_repair_only_approval_env_var": "LOFTY_LISTING_CLEANUP_REPAIR_ONLY_APPROVED",
        "live_apply_repair_only_approval_required_value": "1",
        "live_apply_preflight_required": True,
        "live_apply_preflight_script": str(apply_preflight_script),
        "live_apply_preflight_command": preflight_command,
        "live_apply_preflight_report": str(apply_preflight_report_path),
        "live_apply_monthly_readiness_report": str(monthly_readiness_report_path),
        "live_apply_dry_run_verify_report": str(dry_run_verify_report_path),
        "post_apply_next_action": (
            "Rerun Lofty live update capture, listing cleanup queue, owner email packet dry-run, and EOD preview after approved live cleanup."
        ),
        "dry_run_command_count": len(ready_records),
        "live_apply_command_requires_explicit_approval_count": len(ready_records),
        "candidate_update_approval_copy_command_requires_current_rent_roll_count": len(candidate_approval_records),
        "ready_cleanup_idempotency_digest": ready_cleanup_idempotency_digest,
        "ready_cleanup_manifest": ready_manifest,
        "candidate_update_approval_idempotency_digest": candidate_approval_idempotency_digest,
        "candidate_update_approval_manifest": candidate_approval_manifest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a local-only cleanup queue for Lofty listing UPDATES fields that contain historical UPDATES.md text.")
    parser.add_argument("--live-update-capture-report", required=True, type=Path)
    parser.add_argument("--runtime-map", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--publish-script", default=Path("skills/lofty-pm/scripts/publish_latest_update_to_lofty.py"), type=Path)
    parser.add_argument("--python-bin", default="python3")
    parser.add_argument("--send-interval-days", default=31, type=int)
    parser.add_argument("--review-candidate-packet-report", type=Path, default=None)
    parser.add_argument("--require-monthly-financial-summary", action="store_true")
    parser.add_argument("--run-month", default="")
    args = parser.parse_args(argv)
    args.candidate_summaries = load_candidate_financial_summaries(args.review_candidate_packet_report, args.run_month)
    args.candidate_update_sources = load_candidate_update_sources(args.review_candidate_packet_report, args.run_month)
    args.candidate_update_metadata = load_candidate_update_metadata(args.review_candidate_packet_report, args.run_month)

    live_capture = read_json(args.live_update_capture_report)
    runtime_map = read_json(args.runtime_map)
    issues: list[str] = []
    if live_capture.get("status") in {"missing", "unreadable"}:
        issues.append(f"live update capture report {live_capture.get('status')}: {args.live_update_capture_report}")
    exclusions = exclusion_index(live_capture)
    if live_capture.get("status") not in {"missing", "unreadable"} and not exclusions.get("yhome_transition_column_b_rule_ok"):
        issues.append("live update capture missing ok Yhome column-B exclusion proof")
    if runtime_map.get("status") in {"missing", "unreadable"}:
        issues.append(f"runtime map {runtime_map.get('status')}: {args.runtime_map}")
    if args.require_monthly_financial_summary and not args.review_candidate_packet_report:
        issues.append("monthly financial summary required but review candidate packet report was not provided")
    if args.review_candidate_packet_report and not candidate_packet_run_month_matches(args.review_candidate_packet_report, args.run_month):
        issues.append(
            "review candidate packet run_month mismatch: "
            f"{candidate_packet_run_month(args.review_candidate_packet_report) or 'missing'}!={args.run_month or 'missing'}"
        )
    if args.review_candidate_packet_report and not args.candidate_summaries:
        issues.append(f"no verified monthly financial summaries in {args.review_candidate_packet_report}")
    if not args.publish_script.is_file():
        issues.append(f"publish script missing: {args.publish_script}")

    live_by_key = live_records_by_key(live_capture.get("records") if isinstance(live_capture.get("records"), list) else [])
    properties = runtime_map.get("properties") if isinstance(runtime_map.get("properties"), list) else []
    records = [
        build_record(
            prop,
            live_by_key.get(str(prop.get("lofty_property_id") or ""))
            or live_by_key.get(str(prop.get("updates_md") or ""))
            or live_by_key.get(str(prop.get("property_name") or "")),
            args,
            exclusions,
        )
        for prop in properties
    ]
    status_counts = count_statuses(records)
    queue_idempotency_digest = stable_digest(
        [
            {
                "property_name": record.get("property_name"),
                "lofty_property_id": record.get("lofty_property_id"),
                "status": record.get("status"),
                "latest_update_date": record.get("latest_update_date"),
                "latest_listing_update_sha256": record.get("latest_listing_update_sha256"),
                "latest_update_source": record.get("latest_update_source"),
                "latest_update_source_type": record.get("latest_update_source_type"),
                "reason": record.get("reason"),
                "exclude_reason": record.get("exclude_reason"),
            }
            for record in records
        ]
    )
    ready_count = status_counts.get("ready_listing_cleanup", 0)
    blocked_count = len(records) - ready_count
    excluded_ready_count = sum(1 for record in records if record.get("status") == "ready_listing_cleanup" and record.get("excluded_from_live_cleanup"))
    blocked_unsafe_records = [record for record in records if record.get("status") == "blocked_unsafe_latest_update"]
    blocked_unsafe_candidate_source_count = sum(1 for record in blocked_unsafe_records if record.get("candidate_update_candidate_exists"))
    blocked_unsafe_candidate_quality_issue_count = sum(int(record.get("candidate_update_quality_issue_count") or 0) for record in blocked_unsafe_records)
    blocked_unsafe_candidate_financial_gate_issue_count = sum(int(record.get("candidate_financial_gate_issue_count") or 0) for record in blocked_unsafe_records)
    blocked_unsafe_candidate_financial_gate_hold_count = sum(
        1
        for record in blocked_unsafe_records
        if record.get("candidate_update_candidate_exists")
        and int(record.get("candidate_update_quality_issue_count") or 0) == 0
        and int(record.get("candidate_financial_gate_issue_count") or 0) > 0
        and not record.get("candidate_update_approval_target_exists")
    )
    blocked_unsafe_candidate_approval_target_exists_count = sum(
        1 for record in blocked_unsafe_records if record.get("candidate_update_approval_target_exists")
    )
    if excluded_ready_count:
        issues.append(f"excluded_property_ready_cleanup_count={excluded_ready_count}")
    artifact_summary = write_ready_cleanup_artifacts(args.report, records)
    report = {
        "generated_at": iso_z(),
        "status": "failed" if issues else "review" if ready_count or blocked_count else "ok",
        "issues": issues,
        "issue_count": len(issues),
        "mutates_lofty_listing": False,
        "sends_owner_email": False,
        "live_apply_requires_explicit_approval": True,
        "requires_monthly_financial_summary": args.require_monthly_financial_summary,
        "run_month": args.run_month or None,
        "review_candidate_packet_report": str(args.review_candidate_packet_report) if args.review_candidate_packet_report else None,
        "review_candidate_packet_run_month": candidate_packet_run_month(args.review_candidate_packet_report) or None,
        "review_candidate_packet_run_month_matches": candidate_packet_run_month_matches(
            args.review_candidate_packet_report,
            args.run_month,
        ),
        "verified_candidate_summary_count": len(args.candidate_summaries),
        "approved_update_source_count": len(args.candidate_update_sources),
        "candidate_update_metadata_count": len(args.candidate_update_metadata),
        "active_property_only_policy": "Yhome sold/selling/closed/delisted rows, skipped index rows, and manual exclusions are blocked from live listing cleanup commands.",
        "excluded_property_count": exclusions.get("excluded_property_count", 0),
        "manual_excluded_property_count": exclusions.get("manual_excluded_property_count", 0),
        "manual_excluded_property_names": exclusions.get("manual_excluded_property_names", []),
        "skipped_index_count": exclusions.get("skipped_index_count", 0),
        "skipped_index_status_counts": exclusions.get("skipped_index_status_counts", {}),
        "yhome_transition_guard_status": exclusions.get("yhome_transition_guard_status"),
        "yhome_transition_column_b_rule_ok": exclusions.get("yhome_transition_column_b_rule_ok") is True,
        "excluded_ready_cleanup_count": excluded_ready_count,
        "property_count": len(properties),
        "record_count": len(records),
        "ready_listing_cleanup_count": ready_count,
        "blocked_count": blocked_count,
        "blocked_unsafe_latest_update_count": len(blocked_unsafe_records),
        "blocked_unsafe_update_candidate_source_count": blocked_unsafe_candidate_source_count,
        "blocked_unsafe_update_candidate_quality_issue_count": blocked_unsafe_candidate_quality_issue_count,
        "blocked_unsafe_update_candidate_financial_gate_issue_count": blocked_unsafe_candidate_financial_gate_issue_count,
        "blocked_unsafe_update_candidate_financial_gate_hold_count": blocked_unsafe_candidate_financial_gate_hold_count,
        "blocked_unsafe_update_approval_target_exists_count": blocked_unsafe_candidate_approval_target_exists_count,
        "record_status_counts": status_counts,
        "queue_idempotency_digest": queue_idempotency_digest,
        "live_snapshot_listing_issue_property_count": live_capture.get("live_snapshot_listing_issue_property_count"),
        "live_snapshot_listing_issue_counts": live_capture.get("live_snapshot_listing_issue_counts") or {},
        "live_update_capture_report": str(args.live_update_capture_report),
        "runtime_map": str(args.runtime_map),
        "publish_script": str(args.publish_script),
        "next_action": (
            "Open ready_cleanup_csv, run dry_run_commands_file to confirm current-update listing repair payloads, "
            "then run individual live apply commands only after explicit approval."
            if ready_count
            else "No safe listing cleanup commands are ready; resolve blocked records first."
        ),
        **artifact_summary,
        "records": records,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "issue_count", "property_count", "ready_listing_cleanup_count", "blocked_count")}, indent=2, sort_keys=True))
    return 1 if issues else 2 if report["status"] == "review" else 0


if __name__ == "__main__":
    raise SystemExit(main())
