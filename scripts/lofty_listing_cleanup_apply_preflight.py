#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).absolute().parents[1]
DEFAULT_MONTHLY_READINESS_REPORT = ROOT / "reports" / "baselane_financials_monthly_readiness.json"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not object"}


def nested_count(payload: dict[str, Any], *path: str) -> int:
    value: Any = payload
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_report(
    queue_path: Path,
    verify_path: Path,
    expected_digest: str,
    monthly_readiness_path: Path,
    allow_monthly_readiness_hold_for_repair: bool = False,
) -> dict[str, Any]:
    queue = read_json(queue_path)
    verify = read_json(verify_path)
    monthly_readiness = read_json(monthly_readiness_path)
    queue_digest = str(queue.get("ready_cleanup_idempotency_digest") or "")
    verify_digest = str(verify.get("ready_cleanup_idempotency_digest") or "")
    ready_count = int(queue.get("ready_listing_cleanup_count") or 0)
    no_op = ready_count <= 0
    readiness_status = str(monthly_readiness.get("status") or "")
    readiness_blocker_count = int(monthly_readiness.get("blocker_count") or 0)
    readiness_owner_email_allowed = monthly_readiness.get("owner_email_allowed") is True
    primary_blocker = monthly_readiness.get("primary_blocker") if isinstance(monthly_readiness.get("primary_blocker"), dict) else {}
    primary_blocker_hold = str(primary_blocker.get("hold") or "").strip()
    readiness_missing_or_unreadable = monthly_readiness.get("status") in {"missing", "unreadable"}
    monthly_readiness_clean = readiness_status == "ok" and readiness_blocker_count == 0 and readiness_owner_email_allowed
    monthly_readiness_hold_allowed_for_repair = (
        allow_monthly_readiness_hold_for_repair
        and not readiness_missing_or_unreadable
        and readiness_status == "review"
        and readiness_blocker_count > 0
        and not readiness_owner_email_allowed
        and bool(primary_blocker_hold)
    )
    issues: list[str] = []
    if queue.get("status") in {"missing", "unreadable"}:
        issues.append(f"queue_report_{queue.get('status')}")
    if verify.get("status") in {"missing", "unreadable"}:
        issues.append(f"dry_run_verify_report_{verify.get('status')}")
    if readiness_missing_or_unreadable:
        issues.append(f"monthly_readiness_report_{monthly_readiness.get('status')}")
    if not no_op and not monthly_readiness_clean and not monthly_readiness_hold_allowed_for_repair:
        issues.append("monthly_readiness_not_clean")
    if not no_op and primary_blocker_hold and not monthly_readiness_hold_allowed_for_repair:
        issues.append("monthly_readiness_has_publish_email_hold")
    if not no_op and allow_monthly_readiness_hold_for_repair and not monthly_readiness_clean and not monthly_readiness_hold_allowed_for_repair:
        issues.append("monthly_readiness_repair_hold_not_eligible")
    if not expected_digest:
        issues.append("expected_digest_missing")
    if queue_digest != expected_digest:
        issues.append("queue_digest_mismatch")
    if verify_digest != expected_digest:
        issues.append("dry_run_verify_digest_mismatch")
    if verify.get("status") != "ok" or int(verify.get("issue_count") or 0) != 0:
        issues.append("dry_run_verify_not_ok")
    if int(verify.get("ready_listing_cleanup_count") or 0) != ready_count:
        issues.append("dry_run_verify_ready_count_mismatch")
    if int(verify.get("verified_record_count") or 0) != ready_count:
        issues.append("dry_run_verify_record_count_mismatch")
    if verify.get("dry_run_only") is not True:
        issues.append("dry_run_verify_not_dry_run_only")
    if verify.get("sends_owner_email") is not False:
        issues.append("dry_run_verify_send_risk")
    if verify.get("mutates_lofty_listing") is not False:
        issues.append("dry_run_verify_mutation_risk")
    if verify.get("listing_update_scope") != "full_history":
        issues.append("dry_run_verify_scope_not_supported")
    if nested_count(verify, "dry_run_command_file", "bad_command_count") != 0:
        issues.append("dry_run_command_file_bad_commands")
    if nested_count(verify, "live_apply_command_file", "bad_command_count") != 0:
        issues.append("live_apply_command_file_bad_commands")
    return {
        "generated_at": iso_z(),
        "status": "ok" if not issues else "review",
        "issue_count": len(issues),
        "issues": issues,
        "queue_report": str(queue_path),
        "dry_run_verify_report": str(verify_path),
        "monthly_readiness_report": str(monthly_readiness_path),
        "monthly_readiness_status": readiness_status,
        "monthly_readiness_blocker_count": readiness_blocker_count,
        "monthly_readiness_owner_email_allowed": readiness_owner_email_allowed,
        "monthly_readiness_primary_blocker": primary_blocker,
        "allow_monthly_readiness_hold_for_repair": allow_monthly_readiness_hold_for_repair,
        "monthly_readiness_hold_allowed_for_repair": monthly_readiness_hold_allowed_for_repair,
        "expected_digest": expected_digest,
        "queue_digest": queue_digest,
        "dry_run_verify_digest": verify_digest,
        "ready_listing_cleanup_count": ready_count,
        "no_op": no_op,
        "verified_record_count": int(verify.get("verified_record_count") or 0),
        "dry_run_only": verify.get("dry_run_only") is True,
        "sends_owner_email": verify.get("sends_owner_email") is True,
        "mutates_lofty_listing": verify.get("mutates_lofty_listing") is True,
        "listing_update_scope": verify.get("listing_update_scope"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preflight gated live Lofty listing cleanup against exact dry-run evidence.")
    parser.add_argument("--queue-report", required=True, type=Path)
    parser.add_argument("--verify-report", required=True, type=Path)
    parser.add_argument("--monthly-readiness-report", type=Path, default=DEFAULT_MONTHLY_READINESS_REPORT)
    parser.add_argument(
        "--allow-monthly-readiness-hold-for-repair",
        action="store_true",
        help="Allow explicitly approved cleaned-history listing-field repair while monthly publish/email readiness is held.",
    )
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)
    report = build_report(
        args.queue_report,
        args.verify_report,
        args.expected_digest,
        args.monthly_readiness_report,
        allow_monthly_readiness_hold_for_repair=args.allow_monthly_readiness_hold_for_repair,
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "issue_count", "ready_listing_cleanup_count", "verified_record_count")}, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
