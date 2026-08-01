#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OPENCLAW_SCRIPTS = Path(
    os.environ.get("OPENCLAW_WORKSPACE", Path(__file__).resolve().parents[3])
) / "scripts"
if OPENCLAW_SCRIPTS.is_dir():
    # Keep this repository's monthly modules authoritative; shared OpenClaw
    # scripts are fallback helpers (for example, the Discord route resolver).
    sys.path.append(str(OPENCLAW_SCRIPTS))

import post_property_update_discord as discord_route
from lofty_index_status import is_active_index_status
from lofty_monthly_publish_to_pm import DEFAULT_MANUAL_EXCLUDED_PROPERTIES, stable_digest
from lofty_monthly_owner_email_packet import (
    append_verified_financial_summary_if_missing,
    candidate_summary_for,
    load_candidate_financial_summaries,
)
from lofty_property_paths import resolve_index_property_path


DISCORD_MESSAGE_LIMIT_BYTES = 2000
DISCORD_SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DISALLOWED_DISCORD_SUMMARY_SNIPPETS = (
    "This month's update is limited to verified cash-position data from Lofty and ECO records.",
    "No tenant ledger rows are included.",
)
FINANCIALS_SUMMARY_MARKERS = (
    "Financial detail:",
    "Financial summary from FINANCIALS.md:",
    "Financial summary as of ",
)
SOLD_PROPERTY_NAME_MARKERS = (
    "9919 S Oglesby",
)


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


def parse_iso_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def posted_at_valid(value: object, run_month: str) -> bool:
    parsed = parse_iso_datetime(value)
    if parsed is None:
        return False
    if not run_month:
        return True
    try:
        run_year, run_month_number = (int(part) for part in run_month.split("-", 1))
    except (TypeError, ValueError):
        return False
    close_year = run_year + (1 if run_month_number == 12 else 0)
    close_month_number = 1 if run_month_number == 12 else run_month_number + 1
    return parsed.strftime("%Y-%m") in {
        f"{run_year:04d}-{run_month_number:02d}",
        f"{close_year:04d}-{close_month_number:02d}",
    }


def digest_valid(value: object) -> bool:
    return bool(SHA256_RE.fullmatch(str(value or "")))


def report_digest_valid(data: dict[str, Any]) -> bool:
    digest = str(data.get("digest") or "")
    if not digest_valid(digest):
        return False
    payload = {key: value for key, value in data.items() if key not in {"generated_at", "digest"}}
    return stable_digest(payload) == digest


def existing_valid_report(path: Path, run_month: str) -> bool:
    data = read_json(path)
    status = str(data.get("status") or "")
    posted = data.get("posted") is True or data.get("post_status") in {"ok", "sent", "posted"}
    run_month_ok = data.get("run_month") in {None, "", run_month}
    message_id = str(data.get("posted_message_id") or "")
    channel_id = str(data.get("posted_channel_id") or "")
    target = str(data.get("target") or "")
    target_channel_id = target.removeprefix("channel:") if target.startswith("channel:") else ""
    ids_valid = bool(DISCORD_SNOWFLAKE_RE.fullmatch(message_id)) and bool(DISCORD_SNOWFLAKE_RE.fullmatch(channel_id))
    target_valid = not target_channel_id or channel_id == target_channel_id
    return (
        status in {"ok", "sent", "posted"}
        and posted
        and run_month_ok
        and data.get("valid") is True
        and ids_valid
        and target_valid
        and posted_at_valid(data.get("posted_at"), run_month)
        and report_digest_valid(data)
    )


def property_name_from_path(path: Path) -> str:
    return path.name.strip()


def route_property_name(value: str) -> str:
    return value.split(",", 1)[0].strip() or value


def excluded_by_manual_policy(property_path: Path) -> str:
    lowered = str(property_path).lower()
    for name in DEFAULT_MANUAL_EXCLUDED_PROPERTIES:
        if name.lower() in lowered:
            return name
    for name in SOLD_PROPERTY_NAME_MARKERS:
        if name.lower() in lowered:
            return name
    return ""


def disallowed_summary_reason(text: str) -> str:
    for snippet in DISALLOWED_DISCORD_SUMMARY_SNIPPETS:
        if snippet in text:
            return f"disallowed_limited_financial_summary:{snippet}"
    return ""


def has_verified_financial_summary_text(text: str, run_month: str | None = None) -> bool:
    month = str(run_month or "").strip()
    month_heading_ok = bool(
        month
        and re.search(
            rf"(?m)^##\s+(?:Cash Flow Snapshot|Monthly Cash Position|Source Evidence)\s*\({re.escape(month)}\)\s*$",
            text,
        )
    )
    return (
        any(marker in text for marker in FINANCIALS_SUMMARY_MARKERS)
        and (bool(re.search(r"\bas of\b", text, flags=re.IGNORECASE)) or month_heading_ok)
        and not disallowed_summary_reason(text)
    )


def load_candidates(index_csv: Path, candidate_packet_report: Path | None = None, run_month: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    candidate_summaries, candidate_issues, candidate_summary_report = load_candidate_financial_summaries(
        candidate_packet_report,
        run_month,
    )
    with index_csv.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            property_path, _path_resolution = resolve_index_property_path(row)
            draft_path = Path(row.get("draft_path") or "").expanduser()
            if not draft_path.is_absolute():
                draft_path = index_csv.parent / draft_path
            status = str(row.get("status") or "")
            manual_exclusion = excluded_by_manual_policy(property_path)
            if not is_active_index_status(status):
                skipped.append({"property_path": str(property_path), "status": status, "reason": "inactive_index_status"})
                continue
            if manual_exclusion:
                skipped.append({"property_path": str(property_path), "status": status, "reason": f"manual_exclusion:{manual_exclusion}"})
                continue
            if not draft_path.is_file():
                skipped.append({"property_path": str(property_path), "status": status, "reason": f"draft_missing:{draft_path}"})
                continue
            draft_text = draft_path.read_text(encoding="utf-8", errors="replace").strip()
            property_name = property_name_from_path(property_path)
            route_name = route_property_name(property_name)
            candidate_summary = candidate_summary_for(
                candidate_summaries,
                lofty_property_id=str(row.get("lofty_property_id") or ""),
                property_name=property_name,
                updates_md=draft_path,
            )
            draft_text, financial_summary_enriched = append_verified_financial_summary_if_missing(
                draft_text,
                candidate_summary,
            )
            disallowed_reason = disallowed_summary_reason(draft_text)
            if disallowed_reason:
                skipped.append({"property_path": str(property_path), "status": status, "reason": disallowed_reason})
                continue
            has_financial_summary = has_verified_financial_summary_text(draft_text, run_month)
            display_name = re.sub(r"\s+Public\s*$", "", route_name, flags=re.IGNORECASE).strip()
            message = f"Property Update: {display_name}\n\n{draft_text}\n"
            route_report = discord_route.build_report(message)
            result = route_report.get("result") if isinstance(route_report.get("result"), dict) else {}
            envelope = discord_route.build_envelope(message)
            message_bytes = len(message.encode("utf-8"))
            candidates.append(
                {
                    "property_name": property_name,
                    "route_property_name": route_name,
                    "property_path": str(property_path),
                    "draft_path": str(draft_path),
                    "index_status": status,
                    "message": message,
                    "message_bytes": message_bytes,
                    "has_financial_summary": has_financial_summary,
                    "financial_summary_enriched": financial_summary_enriched,
                    "candidate_summary_source": candidate_summary_report.get("path") if financial_summary_enriched else None,
                    "route_matched": result.get("route_matched") is True,
                    "target": result.get("target"),
                    "guild_id": result.get("guild_id"),
                    "route_report": route_report,
                    "envelope": envelope,
                }
            )
    candidates.sort(
        key=lambda item: (
            1 if item["route_matched"] else 0,
            1 if item["message_bytes"] <= DISCORD_MESSAGE_LIMIT_BYTES else 0,
            item["property_name"],
        ),
        reverse=True,
    )
    if candidate_issues:
        skipped.append({"property_path": "", "status": "candidate_summary_review", "reason": "; ".join(candidate_issues)})
    candidate_summary_report = {**candidate_summary_report, "issues": candidate_issues}
    return candidates, skipped, candidate_summary_report


def write_text_if_changed(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8", errors="replace") == text:
        return
    path.write_text(text, encoding="utf-8")


def write_json_if_changed(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8", errors="replace") == text:
        return
    path.write_text(text, encoding="utf-8")


def posted_evidence_valid(args: argparse.Namespace, selected: dict[str, Any]) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not args.posted_message_id:
        issues.append("--posted-message-id missing")
    elif not DISCORD_SNOWFLAKE_RE.fullmatch(args.posted_message_id):
        issues.append("--posted-message-id invalid Discord snowflake")
    if not args.posted_channel_id:
        issues.append("--posted-channel-id missing")
    elif not DISCORD_SNOWFLAKE_RE.fullmatch(args.posted_channel_id):
        issues.append("--posted-channel-id invalid Discord snowflake")
    target = str(selected.get("target") or "")
    expected_channel_id = target.removeprefix("channel:")
    if args.posted_channel_id and expected_channel_id and args.posted_channel_id != expected_channel_id:
        issues.append(f"posted channel mismatch: {args.posted_channel_id} != {expected_channel_id}")
    if not args.posted_at:
        issues.append("--posted-at missing")
    elif not posted_at_valid(args.posted_at, args.run_month):
        issues.append(f"--posted-at invalid or not in run month {args.run_month}")
    return not issues, issues


def build_report(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    posted_proof_path = args.posted_proof_report or args.report.with_name(f"{args.report.stem}.posted.json")
    if posted_proof_path.is_file() and existing_valid_report(posted_proof_path, args.run_month):
        return read_json(posted_proof_path), 0
    if args.report.is_file() and not args.force and existing_valid_report(args.report, args.run_month):
        return read_json(args.report), 0
    if not args.index_csv.is_file():
        return {
            "generated_at": iso_z(),
            "status": "review",
            "valid": False,
            "posted": False,
            "run_month": args.run_month,
            "issue_count": 1,
            "issues": [f"monthly index missing: {args.index_csv}"],
            "path": str(args.report),
        }, 2

    candidates, skipped, candidate_summary_report = load_candidates(
        args.index_csv,
        args.review_candidate_packet_report,
        args.run_month,
    )
    if not candidates:
        bad_summary_skip_count = sum(
            1
            for item in skipped
            if str(item.get("reason") or "").startswith("disallowed_limited_financial_summary:")
        )
        return {
            "generated_at": iso_z(),
            "status": "review",
            "valid": False,
            "posted": False,
            "run_month": args.run_month,
            "issue_count": 1,
            "issues": ["no active monthly property update draft candidates"],
            "candidate_count": 0,
            "candidate_summary_report": candidate_summary_report,
            "disallowed_limited_financial_summary_skip_count": bad_summary_skip_count,
            "verified_financial_summary_required": True,
            "next_action": (
                "Regenerate non-empty monthly review candidate packet from FINANCIALS.md, rerun this report with --force, "
                "then post only a routed active-property Discord message."
            ),
            "skipped": skipped,
            "path": str(args.report),
        }, 2

    selected = candidates[0]
    missing_financial_summary = [
        {
            "property_name": item.get("property_name"),
            "property_path": item.get("property_path"),
            "draft_path": item.get("draft_path"),
        }
        for item in candidates
        if item.get("has_financial_summary") is not True
    ]
    message_file = args.message_file or args.report.with_suffix(".message.md")
    envelope_file = args.envelope_file or args.report.with_suffix(".envelope.json")
    write_text_if_changed(message_file, selected["message"])
    write_json_if_changed(envelope_file, selected["envelope"])

    evidence_ok, evidence_issues = posted_evidence_valid(args, selected)
    evidence_supplied = bool(args.posted_message_id or args.posted_channel_id or args.posted_at)
    posted = evidence_ok and bool(args.posted_message_id)
    status = "review" if evidence_supplied and evidence_issues else ("ok" if posted else "prepared_not_posted")
    valid = posted
    issues = list(evidence_issues if evidence_supplied else [])
    issues.extend(f"candidate_packet:{issue}" for issue in candidate_summary_report.get("issues", []) if issue)
    if selected["message_bytes"] > DISCORD_MESSAGE_LIMIT_BYTES:
        issues.append(f"message_bytes_exceeds_discord_limit:{selected['message_bytes']}")
    if not selected["route_matched"]:
        issues.append("selected property route falls back to Atlas; update route map or choose a routed candidate before posting")
    if selected.get("has_financial_summary") is not True:
        issues.append("selected property update missing verified FINANCIALS.md summary with as-of date")
    report = {
        "generated_at": iso_z(),
        "status": status if not issues or status == "review" else "review",
        "valid": valid and not issues,
        "posted": posted and not issues,
        "post_status": "posted" if posted and not issues else "not_posted",
        "run_month": args.run_month,
        "path": str(args.report),
        "message_file": str(message_file),
        "envelope_file": str(envelope_file),
        "candidate_count": len(candidates),
        "skipped_count": len(skipped),
        "missing_financial_summary_count": len(missing_financial_summary),
        "missing_financial_summary": missing_financial_summary[:50],
        "issue_count": len(issues),
        "issues": issues,
        "selected": {
            key: selected[key]
            for key in (
                "property_name",
                "route_property_name",
                "property_path",
                "draft_path",
                "index_status",
                "message_bytes",
                "has_financial_summary",
                "financial_summary_enriched",
                "candidate_summary_source",
                "route_matched",
                "target",
                "guild_id",
            )
        },
        "target": selected.get("target"),
        "guild_id": selected.get("guild_id"),
        "posted_message_id": args.posted_message_id or None,
        "posted_channel_id": args.posted_channel_id or None,
        "posted_url": args.posted_url or None,
        "posted_at": args.posted_at or None,
        "evidence_required": ["posted_message_id", "posted_channel_id", "posted_at", "digest", "run_month"],
        "next_action": (
            "After explicit approval, post message_file to the selected Lofty guild property channel, then rerun this script with --posted-message-id and --posted-channel-id."
            if not posted
            else "Guild property-channel test post evidence recorded; owner email gate may proceed if all other monthly guards are clean."
        ),
        "route_report": selected["route_report"],
        "envelope": selected["envelope"],
        "candidate_summary_report": candidate_summary_report,
        "skipped": skipped[:50],
    }
    report["digest"] = stable_digest({key: value for key, value in report.items() if key != "generated_at"})
    return report, 0 if report["status"] in {"ok", "prepared_not_posted"} else 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare or validate the monthly Lofty guild property-channel test post report without sending.")
    parser.add_argument("--index-csv", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--run-month", default=datetime.now(timezone.utc).strftime("%Y-%m"))
    parser.add_argument("--message-file", type=Path)
    parser.add_argument("--envelope-file", type=Path)
    parser.add_argument("--review-candidate-packet-report", type=Path)
    parser.add_argument("--posted-message-id", default="")
    parser.add_argument("--posted-channel-id", default="")
    parser.add_argument("--posted-url", default="")
    parser.add_argument("--posted-at", default="")
    parser.add_argument("--posted-proof-report", type=Path)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report, rc = build_report(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report.get("valid") is True and report.get("posted") is True:
        posted_proof_path = args.posted_proof_report or args.report.with_name(f"{args.report.stem}.posted.json")
        posted_proof_path.parent.mkdir(parents=True, exist_ok=True)
        posted_proof_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "valid": report.get("valid"),
                "posted": report.get("posted"),
                "property": (report.get("selected") or {}).get("property_name") if isinstance(report.get("selected"), dict) else None,
                "target": report.get("target"),
                "issue_count": report.get("issue_count"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
