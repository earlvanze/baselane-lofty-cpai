#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from discord_summary_routing_policy import (
    DRAFT_REVIEW_PREFIX,
    review_delivery_report_issues,
    review_destination,
    review_route_issues,
)
from send_monthly_discord_review_drafts import (
    finalize_report,
    financial_review_blockers_for,
    initial_report,
    iso_z,
    plan_digest,
    sha256_text,
)


SUCCESS_STATUSES = {"ok", "ok_previous"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MergeValidationError(ValueError):
    def __init__(self, issues: Iterable[str]):
        self.issues = sorted(set(str(issue) for issue in issues if str(issue).strip()))
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True)
class ReceiptCandidate:
    result: dict[str, Any]
    source_path: Path
    source_plan_digest: str


@dataclass(frozen=True)
class LiveVerification:
    property_name: str
    target: str
    report_path: Path
    generated_at: str


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON source must be an object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def named_records(records: object, scope: str, issues: list[str]) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        issues.append(f"{scope}_records_missing")
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            issues.append(f"{scope}_record_invalid:{index}")
            continue
        property_name = str(record.get("property_name") or "").strip()
        if not property_name:
            issues.append(f"{scope}_property_name_missing:{index}")
            continue
        if property_name in seen:
            issues.append(f"{scope}_property_name_duplicate:{property_name}")
            continue
        seen.add(property_name)
        result.append(record)
    return result


def validate_plan(
    plan: dict[str, Any],
    *,
    expected_physical_count: int,
    expected_reporting_count: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    issues: list[str] = []
    records = named_records(plan.get("records"), "plan", issues)
    if plan.get("status") not in {"ok", "ok_partial"}:
        issues.append(f"plan_status_not_ready:{plan.get('status')}")
    if int(plan.get("authoritative_active_property_count") or 0) != expected_physical_count:
        issues.append("plan_authoritative_active_property_count_mismatch")
    if int(plan.get("authoritative_reporting_target_count") or 0) != expected_reporting_count:
        issues.append("plan_authoritative_reporting_target_count_mismatch")
    if int(plan.get("record_count") or len(records)) != expected_reporting_count:
        issues.append("plan_record_count_mismatch")
    if len(records) != expected_reporting_count:
        issues.append("plan_records_length_mismatch")
    issues.extend(
        f"plan_route:{item.get('issue')}:{item.get('property_name') or item.get('scope')}"
        for item in review_route_issues(plan)
    )
    for record in records:
        property_name = str(record.get("property_name") or "").strip()
        message = str(record.get("message") or "")
        digest = str(record.get("message_sha256") or "").strip()
        if not message.strip():
            issues.append(f"plan_message_missing:{property_name}")
        elif not SHA256_RE.fullmatch(digest) or sha256_text(message) != digest:
            issues.append(f"plan_message_digest_mismatch:{property_name}")
    return records, issues


def validate_roster(
    roster: dict[str, Any],
    *,
    run_month: object,
    expected_physical_count: int,
    expected_reporting_count: int,
) -> list[str]:
    issues: list[str] = []
    if roster.get("status") != "ok" or int(roster.get("issue_count") or 0) != 0:
        issues.append(f"roster_not_ready:{roster.get('status')}")
    if str(roster.get("run_month") or "") != str(run_month or ""):
        issues.append("roster_run_month_mismatch")
    for field in (
        "physical_property_count",
        "authoritative_active_property_count",
        "expected_active_property_count",
    ):
        if int(roster.get(field) or 0) != expected_physical_count:
            issues.append(f"roster_{field}_mismatch")
    for field in (
        "reporting_target_count",
        "authoritative_reporting_target_count",
        "expected_reporting_target_count",
    ):
        if int(roster.get(field) or 0) != expected_reporting_count:
            issues.append(f"roster_{field}_mismatch")
    physical = roster.get("physical_properties")
    targets = roster.get("reporting_targets")
    if not isinstance(physical, list) or len(physical) != expected_physical_count:
        issues.append("roster_physical_properties_length_mismatch")
    if not isinstance(targets, list) or len(targets) != expected_reporting_count:
        issues.append("roster_reporting_targets_length_mismatch")
    elif sum(int(item.get("physical_property_count") or 0) for item in targets if isinstance(item, dict)) != expected_physical_count:
        issues.append("roster_reporting_target_physical_property_sum_mismatch")
    return issues


def validate_coverage(
    coverage: dict[str, Any],
    *,
    plan: dict[str, Any],
    plan_names: list[str],
    expected_physical_count: int,
    expected_reporting_count: int,
) -> list[str]:
    issues: list[str] = []
    if coverage.get("status") != "ok" or int(coverage.get("issue_count") or 0) != 0:
        issues.append(f"coverage_not_ready:{coverage.get('status')}")
    if str(coverage.get("run_month") or "") != str(plan.get("run_month") or ""):
        issues.append("coverage_run_month_mismatch")
    if int(coverage.get("authoritative_active_property_count") or 0) != expected_physical_count:
        issues.append("coverage_authoritative_active_property_count_mismatch")
    if int(coverage.get("authoritative_reporting_target_count") or 0) != expected_reporting_count:
        issues.append("coverage_authoritative_reporting_target_count_mismatch")
    if int(coverage.get("current_plan_record_count") or 0) != expected_reporting_count:
        issues.append("coverage_current_plan_record_count_mismatch")
    if int(coverage.get("covered_reporting_target_count") or 0) != expected_reporting_count:
        issues.append("coverage_covered_reporting_target_count_mismatch")
    planned = coverage.get("planned_reporting_targets")
    covered = coverage.get("covered_reporting_targets")
    if not isinstance(planned, list) or len(planned) != len(set(planned)) or set(planned) != set(plan_names):
        issues.append("coverage_planned_reporting_targets_mismatch")
    if not isinstance(covered, list) or len(covered) != len(set(covered)) or set(covered) != set(plan_names):
        issues.append("coverage_covered_reporting_targets_mismatch")
    destination = review_destination()
    for field in ("destination_class", "guild_id", "forum_id"):
        if str(coverage.get(field) or "") != str(destination[field]):
            issues.append(f"coverage_{field}_mismatch")
    plan_time = parse_timestamp(plan.get("generated_at"))
    coverage_time = parse_timestamp(coverage.get("generated_at"))
    if plan_time is None or coverage_time is None or coverage_time < plan_time:
        issues.append("coverage_predates_current_plan")
    return issues


def collect_live_verifications(
    paths: list[Path],
    *,
    plan: dict[str, Any],
    plan_names: set[str],
) -> tuple[dict[str, LiveVerification], list[dict[str, Any]], list[str]]:
    issues: list[str] = []
    verifications: dict[str, LiveVerification] = {}
    evidence: list[dict[str, Any]] = []
    plan_time = parse_timestamp(plan.get("generated_at"))
    for path in paths:
        report = read_json(path)
        generated_at = str(report.get("generated_at") or "")
        generated_time = parse_timestamp(generated_at)
        channels = report.get("channels") if isinstance(report.get("channels"), list) else []
        evidence.append(
            {
                "path": str(path),
                "status": report.get("status"),
                "checked_channel_count": report.get("checked_channel_count"),
                "issue_count": report.get("issue_count"),
                "generated_at": generated_at,
            }
        )
        if report.get("status") != "ok" or int(report.get("issue_count") or 0) != 0:
            issues.append(f"verification_report_not_ready:{path}")
        if plan_time is None or generated_time is None or generated_time < plan_time:
            issues.append(f"verification_report_predates_current_plan:{path}")
        if int(report.get("checked_channel_count") or 0) != len(channels):
            issues.append(f"verification_checked_channel_count_mismatch:{path}")
        for channel in channels:
            if not isinstance(channel, dict):
                issues.append(f"verification_channel_invalid:{path}")
                continue
            property_name = str(channel.get("property_name") or "").strip()
            target = str(channel.get("target") or "").strip()
            classifications = channel.get("classifications") or []
            channel_issues = channel.get("issues") or []
            if property_name not in plan_names:
                issues.append(f"verification_property_not_in_plan:{property_name or path}")
                continue
            if "body:current" not in classifications or channel_issues:
                issues.append(f"verification_body_not_current:{property_name}")
                continue
            if not re.fullmatch(r"channel:\d{17,25}", target):
                issues.append(f"verification_target_invalid:{property_name}")
                continue
            current = LiveVerification(property_name, target, path, generated_at)
            prior = verifications.get(property_name)
            if prior and prior.target != target:
                issues.append(f"verification_target_conflict:{property_name}")
            else:
                verifications[property_name] = current
    return verifications, evidence, issues


def collect_receipts(
    paths: list[Path],
) -> tuple[dict[str, ReceiptCandidate], list[dict[str, Any]], list[str]]:
    issues: list[str] = []
    receipts: dict[str, ReceiptCandidate] = {}
    evidence: list[dict[str, Any]] = []
    for path in paths:
        report = read_json(path)
        route_issues = review_delivery_report_issues(report)
        if route_issues:
            issues.extend(f"source_route_invalid:{path}:{item.get('issue')}" for item in route_issues)
        source_digest = str(report.get("plan_digest") or "").strip()
        results = report.get("results") if isinstance(report.get("results"), list) else []
        success_count = 0
        for result in results:
            if not isinstance(result, dict) or result.get("status") not in SUCCESS_STATUSES:
                continue
            success_count += 1
            property_name = str(result.get("property_name") or "").strip()
            thread_id = str(result.get("thread_id") or "").strip()
            target = str(result.get("target") or "").strip()
            if not property_name:
                issues.append(f"source_success_property_missing:{path}")
                continue
            if not re.fullmatch(r"\d{17,25}", thread_id) or target != f"channel:{thread_id}":
                issues.append(f"source_success_thread_invalid:{property_name}:{path}")
                continue
            if not isinstance(result.get("header_receipt"), dict) or not isinstance(result.get("body_receipt"), dict):
                issues.append(f"source_success_receipt_missing:{property_name}:{path}")
                continue
            candidate = ReceiptCandidate(result, path, source_digest)
            prior = receipts.get(property_name)
            if prior and str(prior.result.get("thread_id") or "") != thread_id:
                issues.append(f"source_success_thread_conflict:{property_name}")
                continue
            receipts[property_name] = candidate
        evidence.append(
            {
                "path": str(path),
                "status": report.get("status"),
                "record_count": report.get("record_count"),
                "successful_receipt_count": success_count,
                "plan_digest": source_digest or None,
            }
        )
    return receipts, evidence, issues


def build_merged_report(
    *,
    plan_path: Path,
    roster_path: Path,
    coverage_path: Path,
    source_paths: list[Path],
    verification_paths: list[Path],
    account: str,
    expected_physical_count: int,
    expected_reporting_count: int,
) -> dict[str, Any]:
    plan = read_json(plan_path)
    roster = read_json(roster_path)
    coverage = read_json(coverage_path)
    records, issues = validate_plan(
        plan,
        expected_physical_count=expected_physical_count,
        expected_reporting_count=expected_reporting_count,
    )
    issues.extend(
        validate_roster(
            roster,
            run_month=plan.get("run_month"),
            expected_physical_count=expected_physical_count,
            expected_reporting_count=expected_reporting_count,
        )
    )
    plan_names = [str(record.get("property_name") or "").strip() for record in records]
    issues.extend(
        validate_coverage(
            coverage,
            plan=plan,
            plan_names=plan_names,
            expected_physical_count=expected_physical_count,
            expected_reporting_count=expected_reporting_count,
        )
    )
    verifications, verification_evidence, verification_issues = collect_live_verifications(
        verification_paths,
        plan=plan,
        plan_names=set(plan_names),
    )
    receipts, source_evidence, receipt_issues = collect_receipts(source_paths)
    issues.extend(verification_issues)
    issues.extend(receipt_issues)
    receipt_names = set(receipts)
    expected_names = set(plan_names)
    for property_name in sorted(expected_names - receipt_names):
        issues.append(f"receipt_missing:{property_name}")
    for property_name in sorted(receipt_names - expected_names):
        issues.append(f"receipt_not_in_plan:{property_name}")

    current_plan_digest = plan_digest(plan)
    merged_results: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        property_name = str(record.get("property_name") or "").strip()
        candidate = receipts.get(property_name)
        if candidate is None:
            continue
        receipt = candidate.result
        expected_message_digest = str(record.get("message_sha256") or "").strip()
        receipt_message_digest = str(receipt.get("message_sha256") or "").strip()
        direct_plan_binding = candidate.source_plan_digest == current_plan_digest
        if direct_plan_binding and receipt_message_digest != expected_message_digest:
            issues.append(f"current_digest_source_message_mismatch:{property_name}")
            continue
        verification: dict[str, Any]
        if direct_plan_binding:
            verification = {
                "method": "source_plan_digest_and_message_sha256",
                "source_report": str(candidate.source_path),
                "plan_digest": current_plan_digest,
            }
        else:
            live = verifications.get(property_name)
            if live is None:
                issues.append(f"stale_receipt_live_verification_missing:{property_name}")
                continue
            if live.target != str(receipt.get("target") or ""):
                issues.append(f"stale_receipt_live_verification_target_mismatch:{property_name}")
                continue
            verification = {
                "method": "discord_live_body_read",
                "verification_report": str(live.report_path),
                "verified_at": live.generated_at,
                "classification": "body:current",
            }
        blockers = financial_review_blockers_for(plan, record)
        merged = dict(receipt)
        merged.update(
            {
                "index": index,
                "property_name": property_name,
                "thread_name": record.get("thread_name"),
                "thread_name_normalized": record.get("thread_name_normalized"),
                "financial_review_blocked": bool(blockers),
                "financial_review_blockers": blockers,
                "message_sha256": expected_message_digest,
                "body_bytes": len((DRAFT_REVIEW_PREFIX + str(record.get("message") or "")).encode("utf-8")),
                "status": "ok_previous",
                "stage": "complete",
                "issue": None,
                "receipt_source_report": str(candidate.source_path),
                "receipt_source_plan_digest": candidate.source_plan_digest or None,
                "receipt_original_message_sha256": receipt_message_digest or None,
                "current_plan_binding": verification,
            }
        )
        merged_results.append(merged)

    if issues or len(merged_results) != expected_reporting_count:
        if len(merged_results) != expected_reporting_count:
            issues.append("merged_result_count_mismatch")
        raise MergeValidationError(issues)

    sender_args = SimpleNamespace(plan=plan_path, account=account)
    report = initial_report(sender_args, plan, False)
    report["results"] = merged_results
    finalize_report(report, records, False)
    report.update(
        {
            "generated_at": iso_z(),
            "merge_mode": "deterministic_live_verified_receipt_union",
            "authoritative_active_property_count": expected_physical_count,
            "authoritative_reporting_target_count": expected_reporting_count,
            "physical_property_count": expected_physical_count,
            "reporting_target_count": expected_reporting_count,
            "roster": str(roster_path),
            "coverage_report": str(coverage_path),
            "coverage_status": coverage.get("status"),
            "covered_reporting_target_count": coverage.get("covered_reporting_target_count"),
            "coverage_approval_state": coverage.get("approval_state"),
            "source_reports": source_evidence,
            "live_verification_reports": verification_evidence,
            "human_approval_received": False,
            "lofty_publication_approved": False,
            "owner_email_approved": False,
            "discord_all_property_owner_email_review_complete": False,
            "owner_email_sent": False,
        }
    )
    delivery_issues = review_delivery_report_issues(report)
    if delivery_issues:
        raise MergeValidationError(
            f"merged_route_invalid:{item.get('issue')}:{item.get('property_name') or item.get('scope')}"
            for item in delivery_issues
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge monthly EARLCoin review receipts against the authoritative live-verified roster."
    )
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--roster", required=True, type=Path)
    parser.add_argument("--coverage-report", required=True, type=Path)
    parser.add_argument("--source-report", action="append", required=True, type=Path)
    parser.add_argument("--verification-report", action="append", default=[], type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--account", default="default")
    parser.add_argument("--expected-physical-property-count", type=int, default=32)
    parser.add_argument("--expected-reporting-target-count", type=int, default=30)
    args = parser.parse_args()

    try:
        report = build_merged_report(
            plan_path=args.plan,
            roster_path=args.roster,
            coverage_path=args.coverage_report,
            source_paths=args.source_report,
            verification_paths=args.verification_report,
            account=args.account,
            expected_physical_count=args.expected_physical_property_count,
            expected_reporting_count=args.expected_reporting_target_count,
        )
    except (MergeValidationError, OSError, ValueError, json.JSONDecodeError) as exc:
        issues = exc.issues if isinstance(exc, MergeValidationError) else [str(exc)]
        failure = {
            "generated_at": iso_z(),
            "status": "review",
            "issue": "discord_review_receipt_merge_validation_failed",
            "issue_count": len(issues),
            "issues": issues,
            "plan": str(args.plan),
            "roster": str(args.roster),
            "coverage_report": str(args.coverage_report),
            "source_reports": [str(path) for path in args.source_report],
            "live_verification_reports": [str(path) for path in args.verification_report],
            "human_approval_received": False,
            "lofty_publication_approved": False,
            "owner_email_approved": False,
            "owner_email_sent": False,
        }
        write_json(args.report, failure)
        print(f"status=review issues={len(issues)}", file=sys.stderr)
        return 2

    write_json(args.report, report)
    print(
        f"status={report['status']} physical={report['physical_property_count']} "
        f"targets={report['record_count']} verified={report['sent_or_verified_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
