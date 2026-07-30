#!/usr/bin/env python3
"""Verify completion of the queued Aligned owner-statement Baselane import.

This is a read-only completion gate. It does not call Baselane and does not
write Cash Flow workbooks. Use it after the cron-owned monthly close runs.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def workspace_root() -> Path:
    for candidate in (
        os.environ.get("WORKSPACE_ROOT"),
        "/home/digit/.openclaw/workspace",
        "/home/umbrel/.openclaw/workspace",
        str(Path(__file__).resolve().parents[1]),
    ):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return Path(__file__).resolve().parents[1]


ROOT = workspace_root()
DEFAULT_QUEUE = ROOT / "config" / "aligned_owner_statement_backfill_queue.json"
DEFAULT_CONFIG = ROOT / "config" / "aligned_owner_statement_imports.json"
DEFAULT_MANIFEST_DIR = ROOT / "reports" / "aligned-owner-statement-import-manifests"
DEFAULT_CF_SYNC_REPORT = ROOT / "reports" / "aligned_owner_statement_cf_sync_report.json"
DEFAULT_DOWNSTREAM_REPORT = ROOT / "reports" / "aligned_owner_statement_downstream_validation.json"
DEFAULT_REPORT = ROOT / "reports" / "aligned_owner_statement_completion_gate.json"
DOWNSTREAM_VALIDATOR = ROOT / "scripts" / "validate_aligned_owner_statement_downstream.py"


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def default_scope_review() -> Path:
    current = ROOT / "reports" / "aligned_owner_statement_cleveland_hemlane_current_review.json"
    if current.is_file():
        return current
    candidates = sorted(
        (ROOT / "reports").glob("aligned_owner_statement_cleveland_hemlane_current_review*.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    return candidates[0] if candidates else current


def decimal_value(value: Any) -> Decimal:
    text = str(value if value is not None else "0").replace("$", "").replace(",", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return Decimal(text or "0").quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def refresh_downstream(args: argparse.Namespace) -> dict[str, Any]:
    command = [
        sys.executable,
        str(DOWNSTREAM_VALIDATOR),
        "--queue",
        str(args.queue),
        "--config",
        str(args.config),
        "--manifest-dir",
        str(args.manifest_dir),
        "--cf-sync-report",
        str(args.cf_sync_report),
        "--report",
        str(args.downstream_report),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return {
        "command": command,
        "return_code": result.returncode,
        "stdout_tail": result.stdout[-2000:],
        "stderr_tail": result.stderr[-2000:],
    }


def evaluate(queue: dict[str, Any], downstream: dict[str, Any], scope_review: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    reasons: list[str] = []
    expected = queue.get("expected") or {}
    expected_count = expected.get("to_create_count")
    expected_amount = expected.get("amount_total")
    expected_rows = downstream.get("expected_rows") or {}
    ledger_presence = downstream.get("ledger_presence") or {}
    ledger_label_presence = downstream.get("ledger_label_presence") or {}
    manifest_presence = downstream.get("created_manifest_presence") or {}
    cf_sync = downstream.get("cash_flow_sync_evidence") or {}
    cf_workbook = downstream.get("cash_flow_workbook") or {}
    cf_schema_priority = cf_workbook.get("selected_schema_priority") or []
    cf_schema = cf_schema_priority[1] if len(cf_schema_priority) > 1 else None
    cf_mixed_template_candidate_count = int(cf_workbook.get("mixed_template_candidate_count") or 0)
    import_coverage = scope_review.get("import_coverage") if isinstance(scope_review.get("import_coverage"), dict) else {}
    zero_row_properties = [
        item
        for item in import_coverage.get("zero_row_properties") or []
        if isinstance(item, dict)
    ]

    expected_count_int = int(expected_count or 0)
    checks = {
        "queue_completed": queue.get("status") == "completed",
        "downstream_ok": downstream.get("status") == "ok",
        "expected_count_matches_queue": expected_count is None
        or expected_count_int == int(expected_rows.get("count") or 0),
        "expected_amount_matches_queue": expected_amount is None
        or decimal_value(expected_amount) == decimal_value(expected_rows.get("amount_total")),
        "ledger_found_all_expected_keys": int(ledger_presence.get("found_key_count") or 0) == expected_count_int,
        "ledger_missing_zero": int(ledger_presence.get("missing_key_count") or 0) == 0,
        "ledger_expected_labels_present": int(ledger_label_presence.get("expected_tag_count") or 0) == expected_count_int,
        "ledger_checked_all_expected_labels": int(ledger_label_presence.get("checked_key_count") or 0) == expected_count_int,
        "ledger_label_mismatch_zero": int(ledger_label_presence.get("mismatch_count") or 0) == 0,
        "manifest_matched_all_expected_keys": int(manifest_presence.get("matched_key_count") or 0) == expected_count_int,
        "cash_flow_schema_is_dao_eco_template": cf_schema == "dao_eco_template",
        "cash_flow_has_no_mixed_template_duplicates": cf_mixed_template_candidate_count == 0,
        "cash_flow_sync_ok": cf_sync.get("status") == "ok",
        "cash_flow_sync_all_months_covered": not (cf_sync.get("missing_months") or []),
        "cash_flow_sync_no_failed_months": not (cf_sync.get("failed_months") or []),
        "cash_flow_sync_property_audits_present": not (cf_sync.get("property_audit_missing_months") or []),
        "scope_review_complete": scope_review.get("completion_state") == "complete_evidence_present",
        "scope_review_has_no_review_reasons": not (scope_review.get("review_reasons") or []),
        "scope_review_has_no_unmatched_scope": int(scope_review.get("unmatched_scope_candidate_count") or 0) == 0,
        "scope_review_has_no_cash_flow_selection_issues": not (scope_review.get("cash_flow_selection_issues") or []),
        "scope_review_has_no_unqueued_nonzero_properties": int(import_coverage.get("unqueued_nonzero_property_count") or 0) == 0,
        "scope_review_zero_row_properties_reviewed": all(
            item.get("status") == "reviewed_no_aligned_rows_to_import_after_transition"
            for item in zero_row_properties
        ),
    }
    for name, ok in checks.items():
        if not ok:
            reasons.append(name)
    satisfied_checks = [name for name, ok in checks.items() if ok]

    primary_blocker = None
    if reasons:
        if queue.get("status") != "completed" and downstream.get("status") in {"pending_import", "review"}:
            primary_blocker = "pending_import"
        elif cf_mixed_template_candidate_count:
            primary_blocker = "cash_flow_mixed_template_duplicates"
        elif int(import_coverage.get("unqueued_nonzero_property_count") or 0):
            primary_blocker = "unqueued_nonzero_scope_property"
        elif int(ledger_label_presence.get("mismatch_count") or 0):
            primary_blocker = "ledger_label_mismatch"
        else:
            primary_blocker = "completion_checks_failed"

    summary = {
        "expected_to_create_count": expected_count,
        "expected_amount_total": expected_amount,
        "queue_status": queue.get("status"),
        "downstream_status": downstream.get("status"),
        "ledger_found_key_count": ledger_presence.get("found_key_count"),
        "ledger_missing_key_count": ledger_presence.get("missing_key_count"),
        "ledger_expected_tag_count": ledger_label_presence.get("expected_tag_count"),
        "ledger_checked_label_key_count": ledger_label_presence.get("checked_key_count"),
        "ledger_label_mismatch_count": ledger_label_presence.get("mismatch_count"),
        "manifest_matched_key_count": manifest_presence.get("matched_key_count"),
        "cash_flow_sync_status": cf_sync.get("status"),
        "cash_flow_sync_report_path": cf_sync.get("report_path"),
        "cash_flow_selected_schema": cf_schema,
        "cash_flow_mixed_template_candidate_count": cf_mixed_template_candidate_count,
        "scope_review_completion_state": scope_review.get("completion_state"),
        "scope_review_import_coverage": {
            "zero_row_property_count": import_coverage.get("zero_row_property_count"),
            "nonzero_dry_run_property_count": import_coverage.get("nonzero_dry_run_property_count"),
            "queued_nonzero_property_count": import_coverage.get("queued_nonzero_property_count"),
            "unqueued_nonzero_property_count": import_coverage.get("unqueued_nonzero_property_count"),
        },
        "checks": checks,
        "failed_checks": reasons,
        "satisfied_checks": satisfied_checks,
        "primary_blocker": primary_blocker,
    }
    return ("complete" if not reasons else "not_complete"), reasons, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument("--cf-sync-report", type=Path, default=DEFAULT_CF_SYNC_REPORT)
    parser.add_argument("--downstream-report", type=Path, default=DEFAULT_DOWNSTREAM_REPORT)
    parser.add_argument("--scope-review", type=Path, default=default_scope_review())
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-refresh-downstream", action="store_true")
    args = parser.parse_args()

    refresh_result = None
    if not args.no_refresh_downstream:
        refresh_result = refresh_downstream(args)

    queue = read_json(args.queue)
    downstream = read_json(args.downstream_report)
    scope_review = read_json(args.scope_review)
    status, reasons, summary = evaluate(queue, downstream, scope_review)
    verified_cf_sync_report = summary.get("cash_flow_sync_report_path") or str(args.cf_sync_report)
    report = {
        "job": "aligned-owner-statement-completion-gate",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": status,
        "review_reasons": reasons,
        "queue": str(args.queue),
        "downstream_report": str(args.downstream_report),
        "scope_review": str(args.scope_review),
        "manifest_dir": str(args.manifest_dir),
        "cf_sync_report": verified_cf_sync_report,
        "cf_sync_report_argument": str(args.cf_sync_report),
        "refresh_downstream": refresh_result,
        **summary,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if status == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
