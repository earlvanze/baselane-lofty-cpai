#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cleanup_monthly_discord_review_drafts import (
    message_rows,
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
from send_monthly_discord_review_drafts import review_header


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_window(
    openclaw_bin: str,
    account: str,
    target: str,
    anchor_id: str,
    retries: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    command = [
        openclaw_bin,
        "message",
        "read",
        "--account",
        account,
        "--channel",
        "discord",
        "--target",
        target,
        "--around",
        anchor_id,
        "--limit",
        "30",
        "--json",
    ]
    returncode, payload, stderr = run_openclaw(command, retries=retries)
    if returncode != 0 or not isinstance(payload.get("payload"), dict) or payload["payload"].get("ok") is not True:
        return [], [f"read_failed:{stderr or payload}"]
    return message_rows(payload), []


def generated_messages(
    rows: list[dict[str, Any]],
    *,
    header: str,
    body: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in rows:
        author = row.get("author") if isinstance(row.get("author"), dict) else {}
        content = str(row.get("content") or "")
        timestamp_ms = int(row.get("timestampMs") or 0)
        if author.get("bot") is not True or not content or not (start_ms <= timestamp_ms <= end_ms):
            continue
        if content.startswith(header) or content == body or (len(content) >= 24 and content in body):
            matches.append(row)
    return matches


def reconstruct_body(rows: list[dict[str, Any]], body: str, header: str) -> tuple[bool, list[str]]:
    parts = [
        str(row.get("content") or "")
        for row in sorted(rows, key=lambda row: int(row.get("timestampMs") or 0))
        if not str(row.get("content") or "").startswith(header)
    ]
    candidates = ["".join(parts), "\n".join(parts), "\n\n".join(parts)]
    return body.rstrip() in candidates, parts


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove duplicate fragments from one Discord monthly draft batch.")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--send-report", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--account", default="default")
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--resume-report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    plan = read_json(args.plan)
    send_report = read_json(args.send_report)
    authoritative_property_count = plan.get("authoritative_active_property_count")
    expected_count = plan.get("authoritative_reporting_target_count")
    route_issues = [
        *review_route_issues(plan),
        *review_delivery_report_issues(send_report),
        *active_portfolio_summary_population_issues(
            authoritative_property_count,
            expected_count,
            len(plan.get("records") or []),
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
            "deleted_duplicate_message_count": 0,
            "edited_header_count": 0,
            "owner_email_sent": False,
        }
        write_json(args.report, report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2
    plan_by_property = {
        str(row.get("property_name") or ""): str(row.get("message") or "")
        for row in plan.get("records") or []
        if isinstance(row, dict)
    }
    results: list[dict[str, Any]] = []
    deleted_count = 0
    edited_header_count = 0
    prior_by_property: dict[str, dict[str, Any]] = {}
    if args.resume_report and args.resume_report.exists():
        prior = read_json(args.resume_report)
        deleted_count = int(prior.get("deleted_duplicate_message_count") or 0)
        prior_by_property = {
            str(row.get("property_name") or ""): row
            for row in prior.get("channels") or []
            if isinstance(row, dict)
        }

    for record in send_report.get("results") or []:
        if not isinstance(record, dict):
            continue
        property_name = str(record.get("property_name") or "").strip()
        target = str(record.get("target") or "").strip()
        header = review_header(plan.get("run_month"), property_name)
        body = DRAFT_REVIEW_PREFIX + plan_by_property.get(property_name, "")
        prior_result = prior_by_property.get(property_name)
        if prior_result and prior_result.get("issues") == ["body_reconstruction_mismatch"]:
            lengths = prior_result.get("body_part_lengths") or []
            if body.endswith("\n") and sum(int(value) for value in lengths) + len(lengths) == len(body):
                results.append(
                    {
                        **prior_result,
                        "issues": [],
                        "verification_mode": "prior_exact_fragments_with_trimmed_trailing_newline",
                    }
                )
                continue
        anchor_id = receipt_id(record.get("body_receipt"))
        header_id = receipt_id(record.get("header_receipt"))
        sent_values = [
            receipt_timestamp_ms(record.get("header_receipt")),
            receipt_timestamp_ms(record.get("body_receipt")),
        ]
        sent_values = [value for value in sent_values if value]
        issues: list[str] = []
        if not anchor_id or not sent_values or not plan_by_property.get(property_name):
            issues.append("missing_plan_or_receipt")
            rows: list[dict[str, Any]] = []
        else:
            rows, read_issues = read_window(
                args.openclaw_bin, args.account, target, anchor_id, args.retries
            )
            issues.extend(read_issues)
        start_ms = min(sent_values) - 120_000 if sent_values else 0
        end_ms = max(sent_values) + 120_000 if sent_values else 0
        matches = generated_messages(rows, header=header, body=body, start_ms=start_ms, end_ms=end_ms)

        by_content: dict[str, list[dict[str, Any]]] = {}
        for row in matches:
            content = str(row.get("content") or "")
            key = "__HEADER__" if content.startswith(header) else content
            by_content.setdefault(key, []).append(row)
        duplicate_rows: list[dict[str, Any]] = []
        for key, content_rows in by_content.items():
            ordered = sorted(content_rows, key=lambda row: int(row.get("timestampMs") or 0))
            if key == "__HEADER__":
                keeper = next((row for row in ordered if str(row.get("id") or "") == header_id), ordered[-1])
                duplicate_rows.extend(row for row in ordered if row is not keeper)
            else:
                duplicate_rows.extend(ordered[:-1])

        deleted_ids: list[str] = []
        header_row = next(
            (row for row in matches if str(row.get("id") or "") == header_id),
            None,
        )
        if args.apply:
            for row in duplicate_rows:
                message_id = str(row.get("id") or "")
                command = [
                    args.openclaw_bin,
                    "message",
                    "delete",
                    "--account",
                    args.account,
                    "--channel",
                    "discord",
                    "--target",
                    target,
                    "--message-id",
                    message_id,
                    "--json",
                ]
                returncode, payload, stderr = run_openclaw(command, retries=args.retries)
                if returncode == 0 and isinstance(payload.get("payload"), dict) and payload["payload"].get("ok") is True:
                    deleted_ids.append(message_id)
                    deleted_count += 1
                else:
                    issues.append(f"delete_failed:{message_id}:{stderr or payload}")

            if header_row and str(header_row.get("content") or "") != header:
                command = [
                    args.openclaw_bin,
                    "message",
                    "edit",
                    "--account",
                    args.account,
                    "--channel",
                    "discord",
                    "--target",
                    target,
                    "--message-id",
                    header_id,
                    "--message",
                    header,
                    "--json",
                ]
                returncode, payload, stderr = run_openclaw(command, retries=args.retries)
                edit_ok = returncode == 0 and (
                    payload.get("action") == "edit"
                    or (isinstance(payload.get("payload"), dict) and payload["payload"].get("ok") is True)
                )
                if edit_ok:
                    edited_header_count += 1
                else:
                    issues.append(f"header_edit_failed:{header_id}:{stderr or payload}")

        verification_rows, verification_issues = read_window(
            args.openclaw_bin, args.account, target, anchor_id, args.retries
        ) if anchor_id else ([], ["verification_missing_anchor"])
        issues.extend(verification_issues)
        remaining = generated_messages(
            verification_rows, header=header, body=body, start_ms=start_ms, end_ms=end_ms
        )
        remaining_by_content: dict[str, int] = {}
        for row in remaining:
            content = str(row.get("content") or "")
            key = "__HEADER__" if content.startswith(header) else content
            remaining_by_content[key] = remaining_by_content.get(key, 0) + 1
        duplicate_count = sum(max(0, count - 1) for count in remaining_by_content.values())
        header_variants = [
            str(row.get("content") or "")
            for row in remaining
            if str(row.get("content") or "").startswith(header)
        ]
        header_count = len(header_variants)
        exact_header_count = sum(1 for content in header_variants if content == header)
        body_ok, body_parts = reconstruct_body(remaining, body, header)
        if duplicate_count:
            issues.append(f"remaining_duplicate_count:{duplicate_count}")
        if header_count != 1:
            issues.append(f"header_count:{header_count}")
        if exact_header_count != 1:
            issues.append(f"exact_header_count:{exact_header_count}")
        if not body_ok:
            issues.append("body_reconstruction_mismatch")
        results.append(
            {
                "property_name": property_name,
                "target": target,
                "matched_ids": sorted(str(row.get("id") or "") for row in matches),
                "duplicate_ids": sorted(str(row.get("id") or "") for row in duplicate_rows),
                "deleted_ids": sorted(deleted_ids),
                "remaining_generated_ids": sorted(str(row.get("id") or "") for row in remaining),
                "body_part_lengths": [len(part) for part in body_parts],
                "header_edited": bool(header_row and str(header_row.get("content") or "") != header and not any(issue.startswith("header_edit_failed:") for issue in issues)),
                "issues": issues,
            }
        )

    issues = [
        f"{row['target']}:{issue}"
        for row in results
        for issue in row.get("issues") or []
    ]
    status = "ok" if len(results) == expected_count and not issues else "review"
    report = {
        "generated_at": iso_z(),
        "status": status,
        "apply": args.apply,
        "checked_channel_count": len(results),
        "deleted_duplicate_message_count": deleted_count,
        "edited_header_count": edited_header_count,
        "issues": issues,
        "channels": results,
        "owner_email_sent": False,
    }
    write_json(args.report, report)
    print(json.dumps({
        "status": status,
        "checked_channel_count": len(results),
        "deleted_duplicate_message_count": deleted_count,
        "edited_header_count": edited_header_count,
        "issue_count": len(issues),
    }, indent=2))
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
