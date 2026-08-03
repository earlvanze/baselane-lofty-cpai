#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import itertools
import re
from pathlib import Path
from typing import Any

from cleanup_monthly_discord_review_drafts import read_json, read_messages, write_json
from dedupe_monthly_discord_review_drafts import iso_z
from discord_summary_routing_policy import (
    DRAFT_REVIEW_PREFIX,
    EARLCOIN_GUILD_ID,
    EARLCOIN_REVIEW_FORUM_ID,
    EARLCOIN_REVIEW_DISCORD_ACCOUNT,
    active_portfolio_summary_population_issues,
    normalize_thread_name,
    review_destination,
    review_route_issues,
)
from reconcile_monthly_discord_review_drafts import edit_message


EXECUTION_OWNER = "agent:discord-public"


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def semantic_tokens(value: str) -> list[str]:
    """Ignore only whitespace introduced or removed at transport boundaries."""
    return re.findall(r"\S+", value)


def transport_equivalent(parts: list[str], expected: str) -> bool:
    """Allow OpenClaw's per-fragment trim and either hard or soft boundaries."""
    if not parts:
        return False
    expected_tokens = semantic_tokens(expected)
    for separators in itertools.product(("", "\n", "\n\n"), repeat=len(parts) - 1):
        value = parts[0]
        for separator, part in zip(separators, parts[1:]):
            value += separator + part
        if semantic_tokens(value) == expected_tokens:
            return True
    return False


def safe_fragment_split(text: str, count: int) -> list[str]:
    """Split at single newlines whose following line is not indented or blank."""
    text = text.rstrip()
    if count < 1:
        raise ValueError("fragment count must be positive")
    if count == 1:
        return [text]

    candidates = [
        index
        for index, character in enumerate(text)
        if character == "\n"
        and index > 0
        and index + 1 < len(text)
        and text[index - 1] != "\n"
        and not text[index + 1].isspace()
    ]
    if len(candidates) < count - 1:
        raise ValueError(
            f"only {len(candidates)} safe boundaries for {count} fragments"
        )

    targets = [len(text) * part / count for part in range(1, count)]
    best_boundaries: tuple[int, ...] | None = None
    best_score: tuple[float, int] | None = None
    for boundaries in itertools.combinations(candidates, count - 1):
        positions = (0, *[boundary + 1 for boundary in boundaries], len(text))
        lengths = [positions[index + 1] - positions[index] for index in range(count)]
        score = (
            sum((boundary - target) ** 2 for boundary, target in zip(boundaries, targets)),
            max(lengths),
        )
        if best_score is None or score < best_score:
            best_score = score
            best_boundaries = boundaries

    if best_boundaries is None:
        raise ValueError("no safe fragment split found")
    parts: list[str] = []
    start = 0
    for boundary in best_boundaries:
        parts.append(text[start:boundary])
        start = boundary + 1
    parts.append(text[start:])
    if "\n".join(parts) != text:
        raise ValueError("safe fragment split did not reconstruct source text")
    if any(part != part.strip() or part[:1].isspace() for part in parts):
        raise ValueError("safe fragment split produced transport-sensitive whitespace")
    return parts


def body_message_ids(record: dict[str, Any]) -> list[str]:
    receipt = record.get("body_receipt")
    if not isinstance(receipt, dict):
        return []
    raw = receipt.get("raw") if isinstance(receipt.get("raw"), dict) else {}
    result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
    nested = result.get("receipt") if isinstance(result.get("receipt"), dict) else {}
    values = nested.get("platformMessageIds")
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if str(value).isdigit()]


def gateway_delivery_report_issues(
    report: dict[str, Any],
    inventory: dict[str, Any],
    authoritative_property_count: object,
    expected_count: object,
) -> list[str]:
    issues: list[str] = []
    expected_destination = review_destination()
    for field, expected in expected_destination.items():
        if report.get(field) != expected:
            issues.append(f"send_report.{field}_mismatch")
        if report.get("review_destination", {}).get(field) != expected:
            issues.append(f"send_report.review_destination.{field}_mismatch")
    if report.get("status") != "ok":
        issues.append(f"send_report.status_not_ok:{report.get('status')}")
    if inventory.get("status") != "ok":
        issues.append(f"thread_inventory.status_not_ok:{inventory.get('status')}")

    targets = {
        normalize_thread_name(record.get("property_name")): str(record.get("target") or "")
        for record in inventory.get("channels") or []
        if isinstance(record, dict)
    }
    records = report.get("results") if isinstance(report.get("results"), list) else []
    issues.extend(
        active_portfolio_summary_population_issues(
            authoritative_property_count,
            expected_count,
            len(records),
        )
    )
    if len(records) != len(targets):
        issues.append(f"send_report_inventory_count_mismatch:{len(records)}:{len(targets)}")
    for record in records:
        if not isinstance(record, dict):
            issues.append("send_report.result_invalid")
            continue
        property_name = str(record.get("property_name") or "").strip()
        expected_target = targets.get(normalize_thread_name(property_name))
        if record.get("guild_id") != EARLCOIN_GUILD_ID:
            issues.append(f"{property_name}:guild_id_mismatch")
        if record.get("forum_id") != EARLCOIN_REVIEW_FORUM_ID:
            issues.append(f"{property_name}:forum_id_mismatch")
        if record.get("discord_account_id") != EARLCOIN_REVIEW_DISCORD_ACCOUNT:
            issues.append(f"{property_name}:discord_account_id_mismatch")
        if not expected_target or record.get("target") != expected_target:
            issues.append(f"{property_name}:thread_target_mismatch")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repair whitespace-only Discord review-draft fragment boundaries."
    )
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--send-report", required=True, type=Path)
    parser.add_argument("--thread-inventory", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--property", action="append", dest="properties", required=True)
    parser.add_argument("--account", default=EARLCOIN_REVIEW_DISCORD_ACCOUNT)
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--execution-owner", required=True)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    plan = read_json(args.plan)
    send_report = read_json(args.send_report)
    inventory = read_json(args.thread_inventory)
    authoritative_property_count = plan.get("authoritative_active_property_count")
    expected_count = plan.get("authoritative_reporting_target_count")
    issues = [
        *review_route_issues(plan),
        *gateway_delivery_report_issues(
            send_report,
            inventory,
            authoritative_property_count,
            expected_count,
        ),
    ]
    expected_plan_sha256 = str(send_report.get("plan_file_sha256") or "")
    actual_plan_sha256 = file_sha256(args.plan)
    if not expected_plan_sha256 or actual_plan_sha256 != expected_plan_sha256:
        issues.append(
            f"source_plan_digest_mismatch:{actual_plan_sha256}:{expected_plan_sha256}"
        )
    if args.execution_owner != EXECUTION_OWNER:
        issues.append(f"execution_owner_mismatch:{args.execution_owner}")
    if send_report.get("execution_owner") != EXECUTION_OWNER:
        issues.append(
            f"source_execution_owner_mismatch:{send_report.get('execution_owner')}"
        )
    if args.account != EARLCOIN_REVIEW_DISCORD_ACCOUNT:
        issues.append(f"discord_account_mismatch:{args.account}")

    wanted = {value.strip() for value in args.properties if value.strip()}
    plan_by_property = {
        str(record.get("property_name") or "").strip(): record
        for record in plan.get("records") or []
        if isinstance(record, dict)
    }
    sent_by_property = {
        str(record.get("property_name") or "").strip(): record
        for record in send_report.get("results") or []
        if isinstance(record, dict)
    }
    missing = sorted(wanted - plan_by_property.keys())
    missing.extend(sorted(wanted - sent_by_property.keys()))
    issues.extend(f"property_missing:{value}" for value in missing)

    results: list[dict[str, Any]] = []
    if not issues:
        for property_name in sorted(wanted):
            plan_record = plan_by_property[property_name]
            sent_record = sent_by_property[property_name]
            target = str(sent_record.get("target") or "")
            message_ids = body_message_ids(sent_record)
            row_issues: list[str] = []
            if not re.fullmatch(r"channel:\d{17,25}", target):
                row_issues.append(f"target_invalid:{target}")
            if not message_ids:
                row_issues.append("body_receipt_message_ids_missing")

            rows, read_issues = (
                read_messages(
                    args.openclaw_bin,
                    args.account,
                    target,
                    message_ids,
                    args.retries,
                )
                if not row_issues
                else ([], [])
            )
            row_issues.extend(read_issues)
            rows_by_id = {str(row.get("id") or ""): row for row in rows}
            if any(message_id not in rows_by_id for message_id in message_ids):
                row_issues.append("live_body_fragment_missing")

            expected = (
                DRAFT_REVIEW_PREFIX + str(plan_record.get("message") or "")
            ).rstrip()
            current_parts = [
                str(rows_by_id[message_id].get("content") or "").rstrip()
                for message_id in message_ids
                if message_id in rows_by_id
            ]
            if current_parts and not transport_equivalent(current_parts, expected):
                row_issues.append("live_content_differs_beyond_boundary_whitespace")
            try:
                replacement_parts = safe_fragment_split(expected, len(message_ids))
            except ValueError as exc:
                replacement_parts = []
                row_issues.append(f"safe_split_failed:{exc}")

            edited_ids: list[str] = []
            if args.apply and not row_issues:
                for message_id, current, replacement in zip(
                    message_ids, current_parts, replacement_parts
                ):
                    if current == replacement:
                        continue
                    ok, error = edit_message(
                        args.openclaw_bin,
                        args.account,
                        target,
                        message_id,
                        replacement,
                        args.retries,
                    )
                    if not ok:
                        row_issues.append(f"edit_failed:{message_id}:{error}")
                        break
                    edited_ids.append(message_id)

            verification_parts = current_parts
            if args.apply and edited_ids and not row_issues:
                verification_rows, verification_issues = read_messages(
                    args.openclaw_bin,
                    args.account,
                    target,
                    message_ids,
                    args.retries,
                )
                row_issues.extend(verification_issues)
                verification_by_id = {
                    str(row.get("id") or ""): row for row in verification_rows
                }
                verification_parts = [
                    str(verification_by_id.get(message_id, {}).get("content") or "").rstrip()
                    for message_id in message_ids
                ]
                if verification_parts != replacement_parts:
                    row_issues.append("post_edit_fragment_verification_failed")
                if "\n".join(verification_parts) != expected:
                    row_issues.append("post_edit_body_reconstruction_failed")

            results.append(
                {
                    "property_name": property_name,
                    "target": target,
                    "message_ids": message_ids,
                    "current_fragment_sha256": [sha256(value) for value in current_parts],
                    "replacement_fragment_sha256": [
                        sha256(value) for value in replacement_parts
                    ],
                    "verified_fragment_sha256": [
                        sha256(value) for value in verification_parts
                    ],
                    "edited_ids": edited_ids,
                    "status": "ok" if not row_issues else "review",
                    "issues": row_issues,
                }
            )

    all_issues = [*issues]
    all_issues.extend(
        f"{record['property_name']}:{issue}"
        for record in results
        for issue in record.get("issues") or []
    )
    report = {
        "generated_at": iso_z(),
        "status": "ok" if len(results) == len(wanted) and not all_issues else "review",
        "apply": args.apply,
        "execution_owner": args.execution_owner,
        "plan": str(args.plan.resolve()),
        "plan_file_sha256": actual_plan_sha256,
        "send_report": str(args.send_report.resolve()),
        "thread_inventory": str(args.thread_inventory.resolve()),
        "property_count": len(results),
        "edited_fragment_count": sum(len(row["edited_ids"]) for row in results),
        "issue_count": len(all_issues),
        "issues": all_issues,
        "results": results,
        "owner_email_sent": False,
        "lofty_guild_summary_sent": False,
    }
    write_json(args.report, report)
    print(
        f"status={report['status']} properties={len(results)} "
        f"edited={report['edited_fragment_count']} issues={len(all_issues)}"
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
