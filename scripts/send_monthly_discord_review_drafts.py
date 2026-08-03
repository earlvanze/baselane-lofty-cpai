#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from discord_summary_routing_policy import (
    DRAFT_REVIEW_PREFIX,
    EARLCOIN_GUILD_ID,
    EARLCOIN_GUILD_NAME,
    EARLCOIN_REVIEW_DISCORD_ACCOUNT,
    EARLCOIN_REVIEW_FORUM_ID,
    EARLCOIN_REVIEW_FORUM_NAME,
    EARLCOIN_REVIEW_TARGET,
    REVIEW_DESTINATION_CLASS,
    REVIEW_DESTINATION_PURPOSE,
    REVIEW_PUBLICATION_STATE,
    normalize_thread_name,
    review_destination,
    review_route_issues,
)


SNOWFLAKE_RE = re.compile(r"^\d{17,25}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUCCESS_STATUSES = {"ok", "ok_previous", "ok_dry_run"}


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def plan_digest(plan: dict[str, Any]) -> str:
    records = plan.get("records") if isinstance(plan.get("records"), list) else []
    canonical = [
        {
            "property_name": record.get("property_name"),
            "guild_id": record.get("guild_id"),
            "forum_id": record.get("forum_id"),
            "target": record.get("target"),
            "thread_name_normalized": record.get("thread_name_normalized"),
            "message_sha256": record.get("message_sha256"),
            "financial_review_blocked": record.get("financial_review_blocked") is True,
            "financial_review_blockers": record.get("financial_review_blockers") or [],
        }
        for record in records
        if isinstance(record, dict)
    ]
    return sha256_text(
        json.dumps(
            {
                "run_month": plan.get("run_month"),
                "review_destination": plan.get("review_destination"),
                "records": canonical,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def prior_successes(report_path: Path) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    if not report_path.is_file():
        return {}
    try:
        report = read_json(report_path)
    except Exception:
        return {}
    result: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for item in report.get("results") or []:
        if not isinstance(item, dict) or item.get("status") not in {"ok", "ok_previous"}:
            continue
        key = (
            str(item.get("guild_id") or ""),
            str(item.get("forum_id") or ""),
            str(item.get("thread_name_normalized") or ""),
            str(item.get("review_digest") or ""),
        )
        if all(key):
            result[key] = item
    return result


def prior_live_verified_messages(
    report_path: Path,
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """Index corrected in-place drafts without pretending their old header changed."""
    if not report_path.is_file():
        return {}
    try:
        report = read_json(report_path)
    except Exception:
        return {}
    result: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for item in report.get("results") or []:
        if not isinstance(item, dict) or item.get("status") not in {"ok", "ok_previous"}:
            continue
        binding = item.get("current_plan_binding")
        if not isinstance(binding, dict) or binding.get("method") not in {
            "discord_live_body_read",
            "source_plan_digest_and_message_sha256",
        }:
            continue
        message_digest = str(item.get("message_sha256") or "").strip()
        thread_name = str(item.get("thread_name_normalized") or "").strip()
        thread_id = str(item.get("thread_id") or "").strip()
        if (
            str(item.get("guild_id") or "") != EARLCOIN_GUILD_ID
            or str(item.get("forum_id") or "") != EARLCOIN_REVIEW_FORUM_ID
            or str(item.get("forum_target") or "") != EARLCOIN_REVIEW_TARGET
            or str(item.get("discord_account_id") or "") != EARLCOIN_REVIEW_DISCORD_ACCOUNT
            or not SHA256_RE.fullmatch(message_digest)
            or not thread_name
            or not SNOWFLAKE_RE.fullmatch(thread_id)
            or str(item.get("target") or "") != f"channel:{thread_id}"
        ):
            continue
        result[(EARLCOIN_GUILD_ID, EARLCOIN_REVIEW_FORUM_ID, thread_name, message_digest)] = item
    return result


def review_plan_allowed(plan: dict[str, Any], validation: dict[str, Any]) -> bool:
    if plan.get("status") in {"ok", "ok_partial"}:
        return True
    return bool(
        plan.get("status") == "review"
        and validation.get("discord_review_ready") is True
        and validation.get("earlcoin_review_route_ok") is True
        and int(validation.get("unmapped_count") or 0) == 0
        and int(validation.get("stale_route_count") or 0) == 0
        and int(validation.get("missing_financial_summary_count") or 0) == 0
    )


def parse_payload(stdout: str) -> Any:
    try:
        return json.loads(stdout) if stdout else None
    except json.JSONDecodeError:
        return {"stdout": stdout} if stdout else None


def run_openclaw(command: list[str]) -> tuple[int, Any, str]:
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    return completed.returncode, parse_payload(completed.stdout.strip()), completed.stderr.strip()


def with_account(command: list[str], account: str) -> list[str]:
    if account:
        command.extend(["--account", account])
    return command


def iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from iter_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_dicts(nested)


def snowflake(value: object) -> str:
    text = str(value or "").strip()
    return text if SNOWFLAKE_RE.fullmatch(text) else ""


def extract_threads(payload: Any) -> list[dict[str, Any]]:
    threads: dict[str, dict[str, Any]] = {}
    for item in iter_dicts(payload):
        name = str(item.get("name") or item.get("threadName") or item.get("thread_name") or "").strip()
        thread_id = snowflake(
            item.get("threadId")
            or item.get("thread_id")
            or item.get("id")
            or item.get("channelId")
            or item.get("channel_id")
        )
        if not name or not thread_id or thread_id in {EARLCOIN_GUILD_ID, EARLCOIN_REVIEW_FORUM_ID}:
            continue
        metadata = item.get("threadMetadata") if isinstance(item.get("threadMetadata"), dict) else {}
        if not metadata and isinstance(item.get("thread_metadata"), dict):
            metadata = item["thread_metadata"]
        threads[thread_id] = {
            "id": thread_id,
            "name": name,
            "name_normalized": normalize_thread_name(name),
            "archived": bool(item.get("archived") or metadata.get("archived")),
            "locked": bool(item.get("locked") or metadata.get("locked")),
        }
    return list(threads.values())


def list_forum_threads(openclaw_bin: str, account: str) -> tuple[int, list[dict[str, Any]], list[Any], str]:
    payloads: list[Any] = []
    errors: list[str] = []
    threads: dict[str, dict[str, Any]] = {}
    for include_archived in (False, True):
        command = [
            openclaw_bin,
            "message",
            "thread",
            "list",
            "--channel",
            "discord",
            "--guild-id",
            EARLCOIN_GUILD_ID,
            "--channel-id",
            EARLCOIN_REVIEW_FORUM_ID,
            "--limit",
            "100",
            "--json",
        ]
        if include_archived:
            command.append("--include-archived")
        rc, payload, error = run_openclaw(with_account(command, account))
        payloads.append(payload)
        if rc != 0:
            errors.append(error or f"thread_list_returncode={rc}")
            continue
        for thread in extract_threads(payload):
            threads[thread["id"]] = thread
    return (0 if not errors else 1), list(threads.values()), payloads, "; ".join(errors)


def matching_thread(threads: list[dict[str, Any]], thread_name: str) -> dict[str, Any] | None:
    expected = normalize_thread_name(thread_name)
    if not expected:
        return None
    exact = [item for item in threads if item.get("name_normalized") == expected]
    if exact:
        return sorted(exact, key=lambda item: (item.get("locked") is True, item.get("archived") is True))[0]
    partial = [
        item
        for item in threads
        if item.get("name_normalized")
        and (expected in str(item["name_normalized"]) or str(item["name_normalized"]) in expected)
    ]
    return sorted(partial, key=lambda item: (item.get("locked") is True, item.get("archived") is True))[0] if partial else None


def extract_created_thread_id(payload: Any) -> str:
    for item in iter_dicts(payload):
        for key in ("threadId", "thread_id", "channelId", "channel_id", "id"):
            thread_id = snowflake(item.get(key))
            if thread_id and thread_id not in {EARLCOIN_GUILD_ID, EARLCOIN_REVIEW_FORUM_ID}:
                return thread_id
    return ""


def create_thread(
    openclaw_bin: str,
    account: str,
    thread_name: str,
    initial_message: str,
) -> tuple[int, str, Any, str]:
    command = [
        openclaw_bin,
        "message",
        "thread",
        "create",
        "--channel",
        "discord",
        "--target",
        EARLCOIN_REVIEW_TARGET,
        "--thread-name",
        thread_name,
        "--message",
        initial_message,
        "--auto-archive-min",
        "10080",
        "--json",
    ]
    rc, payload, error = run_openclaw(with_account(command, account))
    return rc, extract_created_thread_id(payload), payload, error


def send_message(openclaw_bin: str, account: str, target: str, message: str) -> tuple[int, Any, str]:
    command = [
        openclaw_bin,
        "message",
        "send",
        "--json",
        "--channel",
        "discord",
        "--target",
        target,
        "--message",
        message,
    ]
    return run_openclaw(with_account(command, account))


def month_label(run_month: object) -> str:
    text = str(run_month or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return text or "Monthly"


def financial_review_blockers_for(
    plan: dict[str, Any],
    record: dict[str, Any],
) -> list[str]:
    values: list[object] = []
    if int(plan.get("global_financial_review_issue_count") or 0) > 0:
        values.extend(plan.get("financial_review_issues") or [])
    if record.get("financial_review_blocked") is True:
        values.extend(record.get("financial_review_blockers") or [])
    blockers: list[str] = []
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if text and text not in blockers:
            blockers.append(text)
    return blockers


def review_header(
    run_month: object,
    property_name: object,
    financial_review_blockers: list[str] | None = None,
) -> str:
    blockers = financial_review_blockers or []
    blocker_note = ""
    if blockers:
        preview = "; ".join(blocker[:180] for blocker in blockers[:3])
        if len(blockers) > 3:
            preview += f"; +{len(blockers) - 3} more in the dispatch receipt"
        blocker_note = (
            f" Financial readiness remains on hold ({len(blockers)} blocker(s)): {preview}. "
            "The draft is being posted so the operator can review it; this is not publication approval."
        )
    return (
        f"{month_label(run_month)} close draft for {str(property_name or '').strip()} is ready for review."
        f"{blocker_note} Reply with edits. Owner email and Lofty guild financial-summary publication each "
        "require separate human approval. No owner email or Lofty guild summary has been sent."
    )


def initial_report(args: argparse.Namespace, plan: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    records = plan.get("records") if isinstance(plan.get("records"), list) else []
    destination = review_destination()
    physical_property_count = int(plan.get("authoritative_active_property_count") or 0)
    reporting_target_count = int(plan.get("authoritative_reporting_target_count") or len(records))
    return {
        "generated_at": iso_z(),
        "status": "running",
        "mode": "all_plan",
        "delivery_mode": "earlcoin_operator_review_drafts",
        "dry_run": dry_run,
        "account": args.account or None,
        "plan": str(args.plan),
        "plan_status": plan.get("status"),
        "plan_digest": plan_digest(plan),
        "run_month": plan.get("run_month"),
        "record_count": len(records),
        "authoritative_active_property_count": physical_property_count,
        "authoritative_reporting_target_count": reporting_target_count,
        "physical_property_count": physical_property_count,
        "reporting_target_count": reporting_target_count,
        "review_destination": destination,
        **destination,
        "results": [],
        "owner_email_sent": False,
    }


def finalize_report(report: dict[str, Any], records: list[Any], dry_run: bool) -> None:
    results = report["results"]
    ok_count = sum(1 for item in results if item.get("status") in SUCCESS_STATUSES)
    held_count = sum(1 for item in results if item.get("status") == "held_financial_review")
    held_global_count = sum(1 for item in results if item.get("status") == "held_global_financial_review")
    posted_with_financial_review_blocker_count = sum(
        1
        for item in results
        if item.get("status") in SUCCESS_STATUSES and item.get("financial_review_blocked") is True
    )
    failed_count = sum(
        1
        for item in results
        if item.get("status") not in {*SUCCESS_STATUSES, "held_financial_review", "held_global_financial_review"}
    )
    eligible_count = len(records) - held_count - held_global_count
    all_eligible_ok = bool(eligible_count and ok_count == eligible_count and failed_count == 0)
    all_records_ok = bool(records and ok_count == len(records) and failed_count == 0)
    if all_records_ok:
        status = "ok_dry_run" if dry_run else "ok"
    elif all_eligible_ok and (held_count or held_global_count):
        status = "ok_partial_dry_run" if dry_run else "ok_partial"
    else:
        status = "review"
    issue_counts: dict[str, int] = {}
    for item in results:
        issue = str(item.get("issue") or "").strip()
        if issue:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
    report.update(
        {
            "generated_at": iso_z(),
            "status": status,
            "eligible_record_count": eligible_count,
            "held_financial_review_count": held_count,
            "held_global_financial_review_count": held_global_count,
            "posted_with_financial_review_blocker_count": posted_with_financial_review_blocker_count,
            "sent_or_verified_count": ok_count,
            "posted_or_verified_property_count": ok_count,
            "failed_count": failed_count,
            "issue_counts": dict(sorted(issue_counts.items())),
            "all_property_discord_review_proof_ok": all_records_ok,
            "eligible_property_discord_review_proof_ok": all_eligible_ok,
            "discord_all_property_dry_run_verified": bool(dry_run and all_records_ok),
            "discord_all_property_live_post_ok": bool(not dry_run and all_records_ok),
            "discord_eligible_property_live_post_ok": bool(not dry_run and all_eligible_ok),
            "discord_all_property_owner_email_review_complete": False,
            "operator_review_requested": bool(not dry_run and all_records_ok),
            "human_approval_received": False,
            "lofty_publication_approved": False,
            "owner_email_approved": False,
            "all_property_review_drafts_posted": bool(not dry_run and all_records_ok),
            "owner_email_sent": False,
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Post monthly property drafts to EARLCoin for operator review.")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--account", default=EARLCOIN_REVIEW_DISCORD_ACCOUNT)
    parser.add_argument("--plan-validation", type=Path)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume-report", type=Path)
    parser.add_argument(
        "--property",
        action="append",
        default=[],
        help="Send only the exact named property from an otherwise fully validated portfolio plan; repeat as needed.",
    )
    args = parser.parse_args()
    dry_run = bool(args.dry_run or not args.send)

    plan = read_json(args.plan)
    validation = read_json(args.plan_validation) if args.plan_validation else {}
    records = plan.get("records") if isinstance(plan.get("records"), list) else []
    report = initial_report(args, plan, dry_run)
    requested_properties = {str(value or "").strip().casefold() for value in args.property if str(value or "").strip()}
    if requested_properties:
        selected_records = [
            record
            for record in records
            if isinstance(record, dict)
            and str(record.get("property_name") or "").strip().casefold() in requested_properties
        ]
        matched_properties = {
            str(record.get("property_name") or "").strip().casefold()
            for record in selected_records
        }
        missing_properties = sorted(requested_properties - matched_properties)
        if missing_properties:
            report.update(
                {
                    "status": "review",
                    "issue": "requested_property_missing_from_validated_plan",
                    "requested_properties": sorted(args.property),
                    "missing_requested_properties": missing_properties,
                    "posted_or_verified_property_count": 0,
                    "failed_count": 0,
                    "all_property_review_drafts_posted": False,
                }
            )
            write_json(args.report, report)
            return 2
        records = selected_records
        report.update(
            {
                "mode": "selected_validated_plan",
                "portfolio_plan_record_count": len(plan.get("records") or []),
                "record_count": len(records),
                "requested_properties": [str(record.get("property_name") or "") for record in records],
            }
        )
    if args.account != EARLCOIN_REVIEW_DISCORD_ACCOUNT:
        report.update(
            {
                "status": "review",
                "issue": "earlcoin_review_discord_account_invalid",
                "expected_account": EARLCOIN_REVIEW_DISCORD_ACCOUNT,
                "posted_or_verified_property_count": 0,
                "failed_count": 0,
                "all_property_review_drafts_posted": False,
            }
        )
        write_json(args.report, report)
        return 2
    destination_issues = review_route_issues(plan)
    if destination_issues:
        report.update(
            {
                "status": "review",
                "issue": "earlcoin_review_route_invalid",
                "destination_issue_count": len(destination_issues),
                "destination_issues": destination_issues,
                "posted_or_verified_property_count": 0,
                "failed_count": 0,
                "all_property_review_drafts_posted": False,
            }
        )
        write_json(args.report, report)
        return 2
    if not review_plan_allowed(plan, validation):
        report.update(
            {
                "status": "review",
                "issue": f"plan_status_not_ok:{plan.get('status')}",
                "posted_or_verified_property_count": 0,
                "failed_count": 0,
                "all_property_review_drafts_posted": False,
            }
        )
        write_json(args.report, report)
        return 2

    resume_report = args.resume_report or args.report
    previous = prior_successes(resume_report)
    previous_live_verified = prior_live_verified_messages(resume_report)
    threads: list[dict[str, Any]] = []
    thread_list_loaded = False
    thread_list_error = ""
    thread_list_receipts: list[Any] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            report["results"].append(
                {"index": index, "status": "failed", "issue": "invalid_plan_record"}
            )
            continue
        property_name = str(record.get("property_name") or "").strip()
        thread_name = str(record.get("thread_name") or "").strip()
        thread_name_normalized = str(record.get("thread_name_normalized") or "").strip()
        message = str(record.get("message") or "")
        expected_digest = str(record.get("message_sha256") or "").strip()
        financial_review_blockers = financial_review_blockers_for(plan, record)
        base_result = {
            "index": index,
            "property_name": property_name,
            "guild_id": EARLCOIN_GUILD_ID,
            "forum_id": EARLCOIN_REVIEW_FORUM_ID,
            "forum_target": EARLCOIN_REVIEW_TARGET,
            "discord_account_id": args.account,
            "thread_name": thread_name,
            "thread_name_normalized": thread_name_normalized,
            "financial_review_blocked": bool(financial_review_blockers),
            "financial_review_blockers": financial_review_blockers,
        }
        if not property_name or not thread_name_normalized or not message.strip():
            report["results"].append(
                {**base_result, "status": "failed", "issue": "invalid_plan_record"}
            )
            continue
        if not expected_digest or sha256_text(message) != expected_digest:
            report["results"].append(
                {**base_result, "status": "failed", "issue": "message_digest_mismatch"}
            )
            continue

        header = review_header(plan.get("run_month"), property_name, financial_review_blockers)
        body = DRAFT_REVIEW_PREFIX + message
        if len(body.encode("utf-8")) > 2000:
            report["results"].append(
                {**base_result, "status": "failed", "issue": "review_body_exceeds_discord_limit"}
            )
            continue
        review_digest = sha256_text(
            "\n".join(
                (
                    EARLCOIN_GUILD_ID,
                    EARLCOIN_REVIEW_FORUM_ID,
                    thread_name_normalized,
                    header,
                    body,
                )
            )
        )
        prior_key = (EARLCOIN_GUILD_ID, EARLCOIN_REVIEW_FORUM_ID, thread_name_normalized, review_digest)
        prior = previous.get(prior_key)
        resume_match = "review_digest"
        if prior is None:
            prior = previous_live_verified.get(
                (
                    EARLCOIN_GUILD_ID,
                    EARLCOIN_REVIEW_FORUM_ID,
                    thread_name_normalized,
                    expected_digest,
                )
            )
            resume_match = "live_verified_current_message"
        if prior and not dry_run:
            report["results"].append(
                {
                    **prior,
                    "index": index,
                    "status": "ok_previous",
                    "resume_match": resume_match,
                }
            )
            continue
        if dry_run:
            report["results"].append(
                {
                    **base_result,
                    "status": "ok_dry_run",
                    "stage": "would_resolve_or_create_thread",
                    "review_digest": review_digest,
                    "message_sha256": expected_digest,
                    "target": None,
                    "body_bytes": len(body.encode("utf-8")),
                }
            )
            continue

        if not thread_list_loaded:
            thread_list_loaded = True
            list_rc, threads, thread_list_receipts, thread_list_error = list_forum_threads(
                args.openclaw_bin,
                args.account,
            )
            report["thread_list_receipts"] = thread_list_receipts
            if list_rc != 0:
                report["thread_list_error"] = thread_list_error
        if thread_list_error:
            report["results"].append(
                {
                    **base_result,
                    "status": "failed",
                    "issue": "earlcoin_forum_thread_list_failed",
                    "thread_list_error": thread_list_error,
                }
            )
            continue

        thread = matching_thread(threads, thread_name)
        locked_replacement = bool(thread is not None and thread.get("locked") is True)
        create_required = thread is None or locked_replacement
        header_receipt: Any = None
        if create_required:
            create_rc, thread_id, header_receipt, create_error = create_thread(
                args.openclaw_bin,
                args.account,
                thread_name,
                header,
            )
            if create_rc != 0 or not thread_id:
                report["results"].append(
                    {
                        **base_result,
                        "status": "failed",
                        "stage": "thread_create",
                        "issue": "earlcoin_forum_thread_create_failed",
                        "review_digest": review_digest,
                        "thread_create_error": create_error or ("created_thread_id_missing" if not thread_id else None),
                        "thread_create_receipt": header_receipt,
                    }
                )
                continue
            thread = {
                "id": thread_id,
                "name": thread_name,
                "name_normalized": thread_name_normalized,
                "archived": False,
                "locked": False,
            }
            threads.append(thread)
            thread_resolution = "created_replacement" if locked_replacement else "created"
        else:
            thread_resolution = "existing_archived" if thread.get("archived") else "existing_active"
            header_rc, header_receipt, header_error = send_message(
                args.openclaw_bin,
                args.account,
                f"channel:{thread['id']}",
                header,
            )
            if header_rc != 0:
                report["results"].append(
                    {
                        **base_result,
                        "target": f"channel:{thread['id']}",
                        "status": "failed",
                        "stage": "header",
                        "issue": "earlcoin_review_header_send_failed",
                        "review_digest": review_digest,
                        "header_error": header_error,
                        "header_receipt": header_receipt,
                    }
                )
                continue

        target = f"channel:{thread['id']}"
        body_rc, body_receipt, body_error = send_message(
            args.openclaw_bin,
            args.account,
            target,
            body,
        )
        report["results"].append(
            {
                **base_result,
                "target": target,
                "thread_id": thread["id"],
                "thread_resolution": thread_resolution,
                "status": "ok" if body_rc == 0 else "failed",
                "stage": "complete" if body_rc == 0 else "body",
                "issue": None if body_rc == 0 else "earlcoin_review_body_send_failed",
                "review_digest": review_digest,
                "message_sha256": expected_digest,
                "body_bytes": len(body.encode("utf-8")),
                "header_receipt": header_receipt,
                "body_receipt": body_receipt,
                "body_error": body_error or None,
            }
        )
        report["generated_at"] = iso_z()
        write_json(args.report, report)

    finalize_report(report, records, dry_run)
    write_json(args.report, report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("status", "record_count", "posted_or_verified_property_count", "failed_count")
            },
            indent=2,
        )
    )
    return 0 if report["status"] in {"ok", "ok_dry_run", "ok_partial", "ok_partial_dry_run"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
