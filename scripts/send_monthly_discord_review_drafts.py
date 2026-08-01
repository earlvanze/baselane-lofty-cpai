#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
            "target": record.get("target"),
            "message_sha256": record.get("message_sha256"),
        }
        for record in records
        if isinstance(record, dict)
    ]
    return sha256_text(
        json.dumps(
            {"run_month": plan.get("run_month"), "records": canonical},
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def prior_successes(report_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    if not report_path.is_file():
        return {}
    try:
        report = read_json(report_path)
    except Exception:
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in report.get("results") or []:
        if not isinstance(item, dict) or item.get("status") not in {"ok", "ok_previous"}:
            continue
        key = (str(item.get("target") or ""), str(item.get("review_digest") or ""))
        if all(key):
            result[key] = item
    return result


def review_plan_allowed(plan: dict[str, Any], validation: dict[str, Any]) -> bool:
    if plan.get("status") == "ok":
        return True
    return bool(
        plan.get("status") == "review"
        and validation.get("discord_review_ready") is True
        and int(validation.get("unmapped_count") or 0) == 0
        and int(validation.get("stale_route_count") or 0) == 0
        and int(validation.get("missing_financial_summary_count") or 0) == 0
    )


def send_message(openclaw_bin: str, target: str, message: str, dry_run: bool, account: str = "") -> tuple[int, Any, str]:
    if dry_run:
        return 0, {"dry_run": True, "target": target}, ""
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
    if account:
        command.extend(["--account", account])
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    stdout = completed.stdout.strip()
    try:
        payload: Any = json.loads(stdout) if stdout else None
    except json.JSONDecodeError:
        payload = {"stdout": stdout} if stdout else None
    return completed.returncode, payload, completed.stderr.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Post monthly property drafts to Discord for operator review.")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--account", default="")
    parser.add_argument("--plan-validation", type=Path)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume-report", type=Path)
    args = parser.parse_args()
    dry_run = bool(args.dry_run or not args.send)

    plan = read_json(args.plan)
    validation = read_json(args.plan_validation) if args.plan_validation else {}
    records = plan.get("records") if isinstance(plan.get("records"), list) else []
    previous = prior_successes(args.resume_report or args.report)
    report: dict[str, Any] = {
        "generated_at": iso_z(),
        "status": "running",
        "mode": "discord_public_review_drafts",
        "dry_run": dry_run,
        "plan": str(args.plan),
        "plan_status": plan.get("status"),
        "plan_digest": plan_digest(plan),
        "run_month": plan.get("run_month"),
        "record_count": len(records),
        "results": [],
    }
    if not review_plan_allowed(plan, validation):
        report.update(
            {
                "status": "review",
                "issue": f"plan_status_not_ok:{plan.get('status')}",
                "posted_or_verified_property_count": 0,
                "failed_count": 0,
                "all_property_review_drafts_posted": False,
                "owner_email_sent": False,
            }
        )
        write_json(args.report, report)
        return 2

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        property_name = str(record.get("property_name") or "").strip()
        target = str(record.get("target") or "").strip()
        message = str(record.get("message") or "")
        expected_digest = str(record.get("message_sha256") or "").strip()
        if not property_name or not target.startswith("channel:") or not message.strip():
            report["results"].append(
                {"index": index, "property_name": property_name, "target": target, "status": "failed", "issue": "invalid_plan_record"}
            )
            break
        if expected_digest and sha256_text(message) != expected_digest:
            report["results"].append(
                {"index": index, "property_name": property_name, "target": target, "status": "failed", "issue": "message_digest_mismatch"}
            )
            break

        header = (
            f"July 31 close draft for {property_name} is ready for review. "
            "Reply with edits, or approve the corresponding DAO for owner email. No owner email has been sent."
        )
        body = "[DRAFT FOR REVIEW - NOT EMAILED]\n\n" + message
        review_digest = sha256_text(header + "\n\n" + body)
        prior = previous.get((target, review_digest))
        if prior and not dry_run:
            report["results"].append({**prior, "index": index, "status": "ok_previous"})
            continue

        header_rc, header_receipt, header_error = send_message(args.openclaw_bin, target, header, dry_run, args.account)
        if header_rc != 0:
            report["results"].append(
                {
                    "index": index,
                    "property_name": property_name,
                    "target": target,
                    "status": "failed",
                    "stage": "header",
                    "review_digest": review_digest,
                    "header_error": header_error,
                }
            )
            break
        body_rc, body_receipt, body_error = send_message(args.openclaw_bin, target, body, dry_run, args.account)
        status = "ok_dry_run" if dry_run and body_rc == 0 else ("ok" if body_rc == 0 else "failed")
        report["results"].append(
            {
                "index": index,
                "property_name": property_name,
                "target": target,
                "status": status,
                "stage": "complete" if body_rc == 0 else "body",
                "review_digest": review_digest,
                "message_sha256": sha256_text(message),
                "header_receipt": header_receipt,
                "body_receipt": body_receipt,
                "body_error": body_error or None,
            }
        )
        report["generated_at"] = iso_z()
        write_json(args.report, report)
        if body_rc != 0:
            break

    ok_statuses = {"ok", "ok_previous", "ok_dry_run"}
    ok_count = sum(1 for item in report["results"] if item.get("status") in ok_statuses)
    failed_count = sum(1 for item in report["results"] if item.get("status") not in ok_statuses)
    report.update(
        {
            "generated_at": iso_z(),
            "status": ("ok_dry_run" if dry_run else "ok") if records and ok_count == len(records) and failed_count == 0 else "review",
            "posted_or_verified_property_count": ok_count,
            "failed_count": failed_count,
            "all_property_review_drafts_posted": bool(not dry_run and records and ok_count == len(records) and failed_count == 0),
            "owner_email_sent": False,
        }
    )
    write_json(args.report, report)
    print(json.dumps({k: report[k] for k in ("status", "record_count", "posted_or_verified_property_count", "failed_count")}, indent=2))
    return 0 if report["status"] in {"ok", "ok_dry_run"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
