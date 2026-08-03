#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from discord_summary_routing_policy import active_portfolio_summary_population_issues


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


def receipt_id(receipt: object) -> str:
    if not isinstance(receipt, dict):
        return ""
    direct = str(receipt.get("messageId") or "").strip()
    if direct:
        return direct
    for envelope_name in ("payload", "raw"):
        envelope = receipt.get(envelope_name)
        if not isinstance(envelope, dict):
            continue
        result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
        direct_result = str(result.get("messageId") or "").strip()
        if direct_result:
            return direct_result
        nested = result.get("receipt") if isinstance(result.get("receipt"), dict) else {}
        platform_ids = nested.get("platformMessageIds")
        if isinstance(platform_ids, list):
            ids = [str(value).strip() for value in platform_ids if str(value).strip()]
            if ids:
                # The final continuation fragment is the safest bounded-read anchor.
                return ids[-1]
    return ""


def receipt_timestamp_ms(receipt: object) -> int:
    if not isinstance(receipt, dict):
        return 0
    for envelope_name in ("payload", "raw"):
        envelope = receipt.get(envelope_name)
        if not isinstance(envelope, dict):
            continue
        result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
        nested = result.get("receipt") if isinstance(result.get("receipt"), dict) else {}
        sent_at = nested.get("sentAt")
        if sent_at:
            return int(sent_at)
    return 0


def parse_cli_json(stdout: str) -> dict[str, Any]:
    start = stdout.find("{")
    if start < 0:
        raise ValueError("OpenClaw command returned no JSON object")
    payload = json.loads(stdout[start:])
    if not isinstance(payload, dict):
        raise ValueError("OpenClaw command returned a non-object JSON value")
    return payload


def run_openclaw(command: list[str], retries: int = 1) -> tuple[int, dict[str, Any], str]:
    last_result: tuple[int, dict[str, Any], str] = (1, {}, "not_run")
    for attempt in range(retries):
        completed = subprocess.run(command, check=False, text=True, capture_output=True)
        try:
            payload = parse_cli_json(completed.stdout)
        except Exception as exc:  # noqa: BLE001
            payload = {"parse_error": str(exc), "stdout": completed.stdout[-1000:]}
        last_result = completed.returncode, payload, completed.stderr.strip()
        if completed.returncode == 0:
            return last_result
        if attempt + 1 < retries:
            time.sleep(1)
    return last_result


def message_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    outer = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    rows = outer.get("messages") if isinstance(outer.get("messages"), list) else []
    return [row for row in rows if isinstance(row, dict)]


def read_messages(
    openclaw_bin: str,
    account: str,
    target: str,
    around_ids: list[str],
    retries: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    rows_by_id: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    # Discord can resolve an `around` window from a deleted snowflake's timestamp.
    # The body receipt is adjacent to any automatic continuation fragments, so one
    # bounded read covers the entire generated pair without scanning channel history.
    anchor_id = around_ids[-1] if around_ids else ""
    requests: list[list[str]] = (
        [["--around", anchor_id, "--limit", "10"]] if anchor_id else [["--limit", "10"]]
    )
    for read_args in requests:
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
            *read_args,
            "--json",
        ]
        returncode, payload, stderr = run_openclaw(command, retries=retries)
        if returncode != 0 or payload.get("payload", {}).get("ok") is not True:
            issues.append(f"read_failed:{' '.join(read_args)}:{stderr or payload}")
            continue
        for row in message_rows(payload):
            message_id = str(row.get("id") or "")
            if message_id:
                rows_by_id[message_id] = row
    return list(rows_by_id.values()), issues


def is_old_batch_message(
    row: dict[str, Any],
    *,
    property_name: str,
    expected_ids: set[str],
    expected_texts: list[str],
    sent_at_ms: int,
) -> bool:
    author = row.get("author") if isinstance(row.get("author"), dict) else {}
    if author.get("bot") is not True:
        return False
    content = str(row.get("content") or "")
    if not content:
        return False
    message_id = str(row.get("id") or "")
    timestamp_ms = int(row.get("timestampMs") or 0)
    near_receipt = bool(sent_at_ms and timestamp_ms and abs(timestamp_ms - sent_at_ms) <= 120_000)
    exact_prefix = near_receipt and (
        content.startswith(f"July 31 close draft for {property_name}")
        or content.startswith("[DRAFT FOR REVIEW - NOT EMAILED]")
    )
    text_fragment = near_receipt and any(len(content) >= 24 and content in expected for expected in expected_texts)
    return bool((message_id in expected_ids and near_receipt) or exact_prefix or text_fragment)


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete and verify one invalid monthly Discord draft batch.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--account", default="default")
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--resume-report", type=Path)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    manifest = read_json(args.manifest)
    plan = read_json(args.plan)
    authoritative_property_count = plan.get("authoritative_active_property_count")
    expected_count = plan.get("authoritative_reporting_target_count")
    population_issues = active_portfolio_summary_population_issues(
        authoritative_property_count,
        expected_count,
        len(plan.get("records") or []),
    )
    plan_by_property = {
        str(record.get("property_name") or ""): str(record.get("message") or "")
        for record in plan.get("records") or []
        if isinstance(record, dict)
    }
    results: list[dict[str, Any]] = []
    global_issues: list[str] = []
    deleted_count = 0
    prior_by_property: dict[str, dict[str, Any]] = {}
    if args.resume_report and args.resume_report.exists():
        prior = read_json(args.resume_report)
        deleted_count = int(prior.get("deleted_message_count") or 0)
        prior_by_property = {
            str(row.get("property_name") or ""): row
            for row in prior.get("channels") or []
            if isinstance(row, dict)
        }

    for record in manifest.get("results") or []:
        if not isinstance(record, dict):
            continue
        property_name = str(record.get("property_name") or "").strip()
        prior_result = prior_by_property.get(property_name)
        if prior_result and not prior_result.get("issues") and not prior_result.get("remaining_ids"):
            results.append(prior_result)
            continue
        target = str(record.get("target") or "").strip()
        header = (
            f"July 31 close draft for {property_name} is ready for review. "
            "Reply with edits, or approve the corresponding DAO for owner email. No owner email has been sent."
        )
        body = "[DRAFT FOR REVIEW - NOT EMAILED]\n\n" + plan_by_property.get(property_name, "")
        header_id = receipt_id(record.get("header_receipt"))
        body_id = receipt_id(record.get("body_receipt"))
        expected_ids = {value for value in (header_id, body_id) if value}
        sent_at_ms = max(
            receipt_timestamp_ms(record.get("header_receipt")),
            receipt_timestamp_ms(record.get("body_receipt")),
        )
        rows, issues = read_messages(
            args.openclaw_bin, args.account, target, sorted(expected_ids), args.retries
        )
        matches = [
            row
            for row in rows
            if is_old_batch_message(
                row,
                property_name=property_name,
                expected_ids=expected_ids,
                expected_texts=[header, body],
                sent_at_ms=sent_at_ms,
            )
        ]
        deleted_ids: list[str] = []
        if args.apply:
            for row in matches:
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
                if returncode == 0 and payload.get("payload", {}).get("ok") is True:
                    deleted_ids.append(message_id)
                    deleted_count += 1
                else:
                    issues.append(f"delete_failed:{message_id}:{stderr or payload}")

        if deleted_ids:
            verification_rows, verification_issues = read_messages(
                args.openclaw_bin, args.account, target, sorted(expected_ids), args.retries
            )
            issues.extend(verification_issues)
        else:
            verification_rows = rows
        remaining_ids = [
            str(row.get("id") or "")
            for row in verification_rows
            if is_old_batch_message(
                row,
                property_name=property_name,
                expected_ids=expected_ids,
                expected_texts=[header, body],
                sent_at_ms=sent_at_ms,
            )
        ]
        if issues:
            global_issues.extend(f"{target}:{issue}" for issue in issues)
        results.append(
            {
                "property_name": property_name,
                "target": target,
                "expected_receipt_ids": sorted(expected_ids),
                "matched_ids": sorted(str(row.get("id") or "") for row in matches),
                "deleted_ids": sorted(deleted_ids),
                "remaining_ids": sorted(remaining_ids),
                "issues": issues,
            }
        )

    remaining_count = sum(len(result["remaining_ids"]) for result in results)
    global_issues.extend(population_issues)
    status = (
        "ok"
        if len(results) == expected_count and not global_issues and remaining_count == 0
        else "review"
    )
    report = {
        "generated_at": iso_z(),
        "status": status,
        "apply": args.apply,
        "account": args.account,
        "checked_channel_count": len(results),
        "deleted_message_count": deleted_count,
        "remaining_matching_message_count": remaining_count,
        "issues": global_issues,
        "channels": results,
    }
    write_json(args.report, report)
    print(
        json.dumps(
            {
                "status": status,
                "checked_channel_count": len(results),
                "deleted_message_count": deleted_count,
                "remaining_matching_message_count": remaining_count,
                "issue_count": len(global_issues),
            },
            indent=2,
        )
    )
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
