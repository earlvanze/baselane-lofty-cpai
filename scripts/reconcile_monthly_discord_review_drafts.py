#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import re
from typing import Any

from cleanup_monthly_discord_review_drafts import (
    read_json,
    receipt_id,
    receipt_timestamp_ms,
    run_openclaw,
    write_json,
)
from discord_summary_routing_policy import (
    DRAFT_REVIEW_PREFIX,
    active_portfolio_summary_population_issues,
    review_delivery_report_issues,
    review_route_issues,
)
from dedupe_monthly_discord_review_drafts import iso_z, read_window
from send_monthly_discord_review_drafts import review_header


DRAFT_PREFIX = DRAFT_REVIEW_PREFIX
CASH_HEADING = "## Monthly Cash Position"


def split_body(message: str) -> tuple[str, str]:
    body = DRAFT_PREFIX + message
    marker = "\n\n" + CASH_HEADING
    if marker not in body:
        raise ValueError("monthly cash position heading missing")
    first, remainder = body.split(marker, 1)
    return first.rstrip(), (CASH_HEADING + remainder).rstrip()


def split_for_fragment_count(text: str, count: int) -> list[str]:
    """Split text into exactly ``count`` newline-preserving Discord edits.

    OpenClaw may split a long send into more than the two semantic sections
    used by older batches.  Edits must retain the existing message count, so
    rebalance the replacement at line boundaries while preserving the exact
    body when the fragments are joined with a newline.
    """
    text = text.rstrip()
    if count < 1:
        return []
    if count == 1:
        return [text]
    lines = text.split("\n")
    if len(lines) < count:
        raise ValueError(f"cannot split {len(lines)} lines into {count} fragments")

    parts: list[str] = []
    start = 0
    for part_index in range(count - 1):
        groups_left = count - part_index
        max_end = len(lines) - (groups_left - 1)
        remaining_text = "\n".join(lines[start:])
        target = len(remaining_text) / groups_left
        best_end = start + 1
        best_distance = float("inf")
        for end in range(start + 1, max_end + 1):
            candidate = "\n".join(lines[start:end])
            distance = abs(len(candidate) - target)
            if distance < best_distance:
                best_end = end
                best_distance = distance
            elif len(candidate) > target:
                break
        parts.append("\n".join(lines[start:best_end]))
        start = best_end
    parts.append("\n".join(lines[start:]))
    if "\n".join(parts) != text:
        raise ValueError("fragment split does not reconstruct source text")
    return parts


def edit_message(
    openclaw_bin: str,
    account: str,
    target: str,
    message_id: str,
    content: str,
    retries: int,
) -> tuple[bool, str]:
    command = [
        openclaw_bin,
        "message",
        "edit",
        "--account",
        account,
        "--channel",
        "discord",
        "--target",
        target,
        "--message-id",
        message_id,
        "--message",
        content,
        "--json",
    ]
    returncode, payload, stderr = run_openclaw(command, retries=retries)
    ok = returncode == 0 and (
        payload.get("action") == "edit"
        or (
            isinstance(payload.get("payload"), dict)
            and payload["payload"].get("ok") is True
        )
    )
    return ok, "" if ok else str(stderr or payload)


def batch_rows(
    rows: list[dict[str, Any]],
    *,
    property_name: str,
    old_body: str,
    new_body: str,
    start_ms: int,
    end_ms: int,
    expected_header_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expected_header_ids = expected_header_ids or set()
    header_prefixes = (
        f"July 2026 close draft for {property_name} is ready for review.",
        f"July 31 close draft for {property_name} is ready for review.",
    )
    headers: list[dict[str, Any]] = []
    body: list[dict[str, Any]] = []
    for row in rows:
        author = row.get("author") if isinstance(row.get("author"), dict) else {}
        content = str(row.get("content") or "")
        message_id = str(row.get("id") or "")
        timestamp_ms = int(row.get("timestampMs") or 0)
        if author.get("bot") is not True or not (start_ms <= timestamp_ms <= end_ms):
            continue
        if message_id in expected_header_ids or content.startswith(header_prefixes):
            headers.append(row)
        elif content.startswith(DRAFT_PREFIX.rstrip()) or (
            len(content) >= 24 and (content in old_body or content in new_body)
        ):
            body.append(row)
    return (
        sorted(headers, key=lambda row: int(row.get("timestampMs") or 0)),
        sorted(body, key=lambda row: int(row.get("timestampMs") or 0)),
    )


def reconstructs(parts: list[str], expected: str) -> bool:
    candidates = ("".join(parts), "\n".join(parts), "\n\n".join(parts))
    return expected.rstrip() in candidates


def fragments_match(parts: list[str], expected_parts: list[str]) -> bool:
    """Compare fragments after Discord strips boundary newlines/whitespace."""
    def normalize_boundary(value: str) -> str:
        return value.strip("\r\n").rstrip()

    return len(parts) == len(expected_parts) and all(
        normalize_boundary(part) == normalize_boundary(expected)
        for part, expected in zip(parts, expected_parts)
    )


def transport_equivalent(parts: list[str], expected: str) -> bool:
    """Accept only whitespace changes at or within Discord fragment boundaries."""
    if not parts:
        return False
    live_tokens = [token for part in parts for token in re.findall(r"\S+", part)]
    return live_tokens == re.findall(r"\S+", expected)


def reconciliation_status(
    send_report_results: list[object],
    checked_results: list[dict[str, Any]],
    issues: list[str],
) -> str:
    """Treat a verified maintenance subset as complete for its own receipts."""
    expected_checked_count = sum(
        1 for record in send_report_results if isinstance(record, dict)
    )
    if expected_checked_count < 1:
        return "review"
    return (
        "ok"
        if len(checked_results) == expected_checked_count and not issues
        else "review"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile an existing Discord monthly draft batch to a newer exact plan."
    )
    parser.add_argument("--previous-plan", required=True, type=Path)
    parser.add_argument("--current-plan", required=True, type=Path)
    parser.add_argument("--send-report", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--account", default="default")
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--property",
        action="append",
        default=[],
        help=(
            "Reconcile only the exact named property from an otherwise fully "
            "validated portfolio plan; repeat as needed."
        ),
    )
    args = parser.parse_args()

    previous_plan = read_json(args.previous_plan)
    current_plan = read_json(args.current_plan)
    send_report = read_json(args.send_report)
    authoritative_property_count = current_plan.get("authoritative_active_property_count")
    expected_count = current_plan.get("authoritative_reporting_target_count")
    route_issues = [
        *review_route_issues(previous_plan),
        *review_route_issues(current_plan),
        *review_delivery_report_issues(send_report),
        *active_portfolio_summary_population_issues(
            authoritative_property_count,
            expected_count,
            len(current_plan.get("records") or []),
        ),
    ]
    if route_issues:
        report = {
            "generated_at": iso_z(),
            "status": "review",
            "apply": args.apply,
            "issue": "earlcoin_review_route_invalid",
            "route_issue_count": len(route_issues),
            "route_issues": route_issues,
            "checked_channel_count": 0,
            "edited_fragment_count": 0,
            "owner_email_sent": False,
        }
        write_json(args.report, report)
        print(f"status=review checked=0 edited=0 issues={len(route_issues)}")
        return 2

    previous = {
        str(row.get("property_name") or ""): str(row.get("message") or "")
        for row in previous_plan.get("records") or []
        if isinstance(row, dict)
    }
    current = {
        str(row.get("property_name") or ""): str(row.get("message") or "")
        for row in current_plan.get("records") or []
        if isinstance(row, dict)
    }
    results: list[dict[str, Any]] = []
    edit_count = 0

    all_send_report_results = [
        record
        for record in send_report.get("results") or []
        if isinstance(record, dict)
    ]
    requested_properties = {
        str(value or "").strip().casefold()
        for value in args.property
        if str(value or "").strip()
    }
    if requested_properties:
        send_report_results = [
            record
            for record in all_send_report_results
            if str(record.get("property_name") or "").strip().casefold()
            in requested_properties
        ]
        matched_properties = {
            str(record.get("property_name") or "").strip().casefold()
            for record in send_report_results
        }
        missing_properties = sorted(requested_properties - matched_properties)
        if missing_properties:
            report = {
                "generated_at": iso_z(),
                "status": "review",
                "apply": args.apply,
                "issue": "requested_property_missing_from_send_report",
                "requested_properties": sorted(args.property),
                "missing_requested_properties": missing_properties,
                "portfolio_send_report_record_count": len(all_send_report_results),
                "checked_channel_count": 0,
                "edited_fragment_count": 0,
                "owner_email_sent": False,
            }
            write_json(args.report, report)
            print(
                "status=review checked=0 edited=0 "
                f"issues={len(missing_properties)}"
            )
            return 2
    else:
        send_report_results = all_send_report_results

    for record in send_report_results:
        property_name = str(record.get("property_name") or "").strip()
        target = str(record.get("target") or "").strip()
        issues: list[str] = []
        edited_ids: list[str] = []
        anchor_id = receipt_id(record.get("body_receipt"))
        header_receipt_id = receipt_id(record.get("header_receipt"))
        sent_values = [
            receipt_timestamp_ms(record.get("header_receipt")),
            receipt_timestamp_ms(record.get("body_receipt")),
        ]
        sent_values = [value for value in sent_values if value]
        if property_name not in previous or property_name not in current:
            issues.append("missing_plan_record")
        if not anchor_id or not sent_values:
            issues.append("missing_send_receipt")
        start_ms = min(sent_values) - 120_000 if sent_values else 0
        end_ms = max(sent_values) + 120_000 if sent_values else 0
        rows, read_issues = (
            read_window(args.openclaw_bin, args.account, target, anchor_id, args.retries)
            if anchor_id
            else ([], ["read_missing_anchor"])
        )
        issues.extend(read_issues)
        old_body = DRAFT_PREFIX + previous.get(property_name, "")
        new_body = DRAFT_PREFIX + current.get(property_name, "")
        header = review_header(current_plan.get("run_month"), property_name)
        header_rows, body_rows = batch_rows(
            rows,
            property_name=property_name,
            old_body=old_body,
            new_body=new_body,
            start_ms=start_ms,
            end_ms=end_ms,
            expected_header_ids={header_receipt_id} if header_receipt_id else set(),
        )
        if len(header_rows) != 1:
            issues.append(f"header_count:{len(header_rows)}")
        if not body_rows:
            issues.append(f"body_fragment_count:{len(body_rows)}")

        classifications: list[str] = []
        if len(header_rows) == 1:
            # The header is dispatch metadata, not part of the financial summary.
            # Preserve it byte-for-byte once its original receipt has been found.
            classifications.append("header:receipt")

        body_safe_to_replace = False
        new_parts: list[str] = []
        old_parts: list[str] = []
        if body_rows:
            contents = [str(row.get("content") or "").rstrip() for row in body_rows]
            try:
                old_parts = split_for_fragment_count(old_body, len(body_rows))
                new_parts = split_for_fragment_count(new_body, len(body_rows))
            except ValueError as exc:
                issues.append(f"body_split_failed:{exc}")
            if (
                fragments_match(contents, new_parts)
                or reconstructs(contents, new_body)
                or transport_equivalent(contents, new_body)
            ):
                classifications.append("body:current")
            elif (
                fragments_match(contents, old_parts)
                or reconstructs(contents, old_body)
                or transport_equivalent(contents, old_body)
            ):
                classifications.append("body:previous")
                body_safe_to_replace = True
            elif len(contents) == len(new_parts) == len(old_parts) and all(
                fragments_match([content], [old_part])
                or fragments_match([content], [new_part])
                for content, old_part, new_part in zip(contents, old_parts, new_parts)
            ):
                classifications.append("body:mixed_previous_current")
                body_safe_to_replace = True
            else:
                classifications.append("body:concurrent")
                issues.append("concurrent_body_fragments")
        if args.apply and body_safe_to_replace:
            for fragment_index, (row, new_text) in enumerate(
                zip(body_rows, new_parts), start=1
            ):
                message_id = str(row.get("id") or "")
                ok, error = edit_message(
                    args.openclaw_bin,
                    args.account,
                    target,
                    message_id,
                    new_text,
                    args.retries,
                )
                if ok:
                    edited_ids.append(message_id)
                    edit_count += 1
                else:
                    issues.append(
                        f"edit_failed:fragment_{fragment_index}:{message_id}:{error}"
                    )

        verification_rows, verification_issues = (
            (
                read_window(args.openclaw_bin, args.account, target, anchor_id, args.retries)
                if anchor_id
                else ([], ["verification_missing_anchor"])
            )
            if args.apply and edited_ids
            else (rows, [])
        )
        issues.extend(verification_issues)
        verification_headers, verification_body = batch_rows(
            verification_rows,
            property_name=property_name,
            old_body=old_body,
            new_body=new_body,
            start_ms=start_ms,
            end_ms=end_ms,
            expected_header_ids={header_receipt_id} if header_receipt_id else set(),
        )
        if len(verification_headers) != 1:
            issues.append("header_verification_failed")
        verification_contents = [
            str(row.get("content") or "").rstrip() for row in verification_body
        ]
        if args.apply and not (
            fragments_match(verification_contents, new_parts)
            or transport_equivalent(verification_contents, new_body)
        ):
            issues.append("body_fragment_verification_failed")
        results.append(
            {
                "property_name": property_name,
                "target": target,
                "classifications": classifications,
                "edited_ids": edited_ids,
                "issues": sorted(set(issues)),
            }
        )

    issues = [
        f"{row['property_name']}:{issue}"
        for row in results
        for issue in row.get("issues") or []
    ]
    report = {
        "generated_at": iso_z(),
        "status": reconciliation_status(send_report_results, results, issues),
        "apply": args.apply,
        "authoritative_reporting_target_count": expected_count,
        "portfolio_send_report_record_count": len(all_send_report_results),
        "requested_properties": sorted(args.property),
        "expected_checked_channel_count": len(send_report_results),
        "checked_channel_count": len(results),
        "edited_fragment_count": edit_count,
        "issue_count": len(issues),
        "issues": issues,
        "channels": results,
    }
    write_json(args.report, report)
    print(
        f"status={report['status']} checked={len(results)} "
        f"edited={edit_count} issues={len(issues)}"
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
