#!/usr/bin/env python3
"""Build a compact readiness report for Aligned owner-statement imports.

This is read-only. It does not call Baselane, import rows, or update Cash Flow
Statement workbooks.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
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
DEFAULT_AUTH_REPORT = ROOT / "reports" / "aligned_parent_dryrun_latest" / "baselane_auth_recovery_report.json"
DEFAULT_PREFLIGHT_SUMMARY = (
    ROOT / "reports" / "aligned_parent_dryrun_latest" / "aligned-owner-statement-import-live-preflight" / "summary.json"
)
DEFAULT_DOWNSTREAM_REPORT = (
    ROOT / "reports" / "aligned_parent_dryrun_latest" / "aligned_owner_statement_downstream_validation.json"
)
DEFAULT_COMPLETION_REPORT = (
    ROOT / "reports" / "aligned_parent_dryrun_latest" / "aligned_owner_statement_completion_gate.json"
)
DEFAULT_SCOPE_REVIEW = (
    ROOT / "reports" / "aligned_parent_dryrun_latest" / "aligned_owner_statement_cleveland_hemlane_current_review.json"
)
DEFAULT_IMPORT_REPORT_DIR = ROOT / "reports" / "aligned_parent_dryrun_latest" / "aligned-owner-statement-import-backfill"
DEFAULT_LOCAL_LEDGER_DUPLICATE_SCAN = (
    ROOT / "reports" / "aligned_parent_dryrun_latest" / "aligned_owner_local_ledger_duplicate_key_scan.json"
)
DEFAULT_REPORT = ROOT / "reports" / "aligned_parent_dryrun_latest" / "aligned_owner_statement_import_readiness.json"
DEFAULT_MARKDOWN = ROOT / "reports" / "aligned_parent_dryrun_latest" / "aligned_owner_statement_import_readiness.md"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return payload if isinstance(payload, dict) else {"status": "unreadable", "path": str(path), "error": "not_object"}


def count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def looks_like_auth_query_error(value: Any) -> bool:
    text = str(value or "").lower()
    return bool(
        text
        and (
            "x-firebase-appcheck" in text
            or "auth_required" in text
            or "unauthorized_access" in text
            or "missing cookie" in text
            or "session-expired" in text
            or "session expired" in text
            or "login" in text
        )
    )


def abs_path(raw: Any, root: Path) -> Path:
    path = Path(str(raw))
    return path if path.is_absolute() else root / path


def find_property_config(config: dict[str, Any], property_id: str) -> dict[str, Any]:
    for item in config.get("properties") or []:
        if str(item.get("baselane_property_id") or "") == str(property_id):
            return item if isinstance(item, dict) else {}
    return {}


def canonical_search_roots(property_config: dict[str, Any]) -> list[Path]:
    roots = []
    for raw in property_config.get("search_roots") or []:
        path = Path(str(raw))
        if path.is_dir():
            roots.append(path.resolve())
    return roots


def is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def noncanonical_cash_flow_candidates(property_config: dict[str, Any], limit: int = 25) -> list[str]:
    roots = canonical_search_roots(property_config)
    if not roots:
        return []
    scan_roots = []
    for root in roots:
        public_root = root.parent
        if public_root.is_dir() and public_root not in scan_roots:
            scan_roots.append(public_root)
    candidates: list[str] = []
    for scan_root in scan_roots:
        for path in sorted(scan_root.rglob("Cash Flow Statement*.xlsx"), key=lambda item: str(item).lower()):
            if any(is_under(path, canonical_root) for canonical_root in roots):
                continue
            candidates.append(str(path))
            if len(candidates) >= limit:
                return candidates
    return candidates


def auth_summary(auth: dict[str, Any]) -> dict[str, Any]:
    verified = count(auth.get("verified_authenticated_tab_count"))
    authenticated = count(auth.get("authenticated_tab_count"))
    url_only = count(auth.get("url_authenticated_tab_count"))
    non_baselane = count(auth.get("non_baselane_page_tab_count"))
    ok = auth.get("status") == "ok" and verified > 0
    if not ok and verified > 0 and auth.get("manual_auth_required") is not True:
        ok = True
    return {
        "status": auth.get("status"),
        "ok": ok,
        "issue_summary": auth.get("issue_summary"),
        "next_action": auth.get("next_action"),
        "cdp_url": auth.get("cdp_url"),
        "preferred_cdp_attempt_failed": auth.get("preferred_cdp_attempt_failed") is True,
        "manual_auth_required": auth.get("manual_auth_required") is True,
        "manual_auth_reason": auth.get("manual_auth_reason"),
        "verified_authenticated_tab_count": verified,
        "authenticated_tab_count": authenticated,
        "url_authenticated_tab_count": url_only,
        "non_baselane_page_tab_count": non_baselane,
        "url_only_auth_evidence": url_only > 0 and verified == 0,
    }


def preflight_summary(preflight: dict[str, Any]) -> dict[str, Any]:
    query_error_months = [str(month) for month in preflight.get("query_error_months") or []]
    auth_error_months = [str(month) for month in preflight.get("auth_error_months") or []]
    pre_fallback_query_error_months = [
        str(month) for month in preflight.get("pre_fallback_query_error_months") or []
    ]
    pre_fallback_auth_error_months = [
        str(month) for month in preflight.get("pre_fallback_auth_error_months") or []
    ]
    missing_report_months = [str(month) for month in preflight.get("missing_report_months") or []]
    timed_out_months = [str(month) for month in preflight.get("timed_out_months") or []]
    review_reasons = [str(reason) for reason in preflight.get("review_reasons") or []]
    reports = preflight.get("reports") if isinstance(preflight.get("reports"), list) else []
    commands = preflight.get("commands") if isinstance(preflight.get("commands"), list) else []
    for command in commands:
        if not isinstance(command, dict):
            continue
        month = str(command.get("month") or "")
        fallback = command.get("staging_fallback") if isinstance(command.get("staging_fallback"), dict) else {}
        live_before_fallback = (
            fallback.get("live_report_before_fallback")
            if isinstance(fallback.get("live_report_before_fallback"), dict)
            else {}
        )
        query_error = live_before_fallback.get("query_error")
        if query_error:
            append_unique(pre_fallback_query_error_months, month)
            append_unique(query_error_months, month)
        if looks_like_auth_query_error(query_error):
            append_unique(pre_fallback_auth_error_months, month)
            append_unique(auth_error_months, month)
    staging_fallback_months = [
        str(item.get("month") or "")
        for item in reports
        if isinstance(item, dict) and item.get("used_staging_fallback") is True
    ]
    staging_fallback_months = [month for month in staging_fallback_months if month]
    live_duplicate_query_months = [
        str(item.get("month") or "")
        for item in reports
        if (
            isinstance(item, dict)
            and item.get("status") == "ok"
            and item.get("timed_out") is not True
            and item.get("used_staging_fallback") is not True
        )
    ]
    live_duplicate_query_months = [month for month in live_duplicate_query_months if month]
    planned = count(preflight.get("planned_count_total"))
    to_create = count(preflight.get("to_create_count_total"))
    created = count(preflight.get("created_count_total"))
    existing = count(preflight.get("existing_key_count_total"))
    skipped_existing = count(preflight.get("skipped_existing_count_total"))
    expected_remaining_or_existing = count(preflight.get("expected_remaining_or_existing_total"))
    if expected_remaining_or_existing == 0 and (to_create or skipped_existing):
        expected_remaining_or_existing = to_create + skipped_existing
    expected_plan_coverage_complete = planned > 0 and expected_remaining_or_existing == planned
    duplicate_or_existing = skipped_existing
    ok = (
        preflight.get("status") == "ok"
        and planned > 0
        and created == 0
        and expected_plan_coverage_complete
        and not review_reasons
        and not query_error_months
        and not missing_report_months
        and not timed_out_months
    )
    return {
        "status": preflight.get("status"),
        "ok": ok,
        "duplicate_check_complete": ok,
        "duplicate_check_trusted_zero": ok and existing == 0 and skipped_existing == 0,
        "expected_existing_keys_idempotent": ok and skipped_existing > 0,
        "used_staging_fallback_months": staging_fallback_months,
        "used_staging_fallback_month_count": len(staging_fallback_months),
        "live_duplicate_query_months": live_duplicate_query_months,
        "live_duplicate_query_month_count": len(live_duplicate_query_months),
        "staging_plan_complete": (
            bool(staging_fallback_months)
            and planned > 0
            and expected_plan_coverage_complete
            and created == 0
            and not missing_report_months
        ),
        "review_reasons": review_reasons,
        "planned_count_total": planned,
        "to_create_count_total": to_create,
        "created_count_total": created,
        "existing_key_count_total": existing,
        "skipped_existing_count_total": skipped_existing,
        "expected_remaining_or_existing_total": expected_remaining_or_existing,
        "expected_plan_coverage_complete": expected_plan_coverage_complete,
        "duplicate_or_existing_key_count": duplicate_or_existing,
        "query_error_months": query_error_months,
        "auth_error_months": auth_error_months,
        "pre_fallback_query_error_months": pre_fallback_query_error_months,
        "pre_fallback_auth_error_months": pre_fallback_auth_error_months,
        "missing_report_months": missing_report_months,
        "timed_out_months": timed_out_months,
        "query_error_month_count": len(query_error_months),
        "auth_error_month_count": len(auth_error_months),
        "pre_fallback_query_error_month_count": len(pre_fallback_query_error_months),
        "pre_fallback_auth_error_month_count": len(pre_fallback_auth_error_months),
        "missing_report_month_count": len(missing_report_months),
        "timed_out_month_count": len(timed_out_months),
    }


def cash_flow_summary(downstream: dict[str, Any], property_config: dict[str, Any]) -> dict[str, Any]:
    workbook = downstream.get("cash_flow_workbook") if isinstance(downstream.get("cash_flow_workbook"), dict) else {}
    schema_priority = workbook.get("selected_schema_priority") or []
    schema = schema_priority[1] if isinstance(schema_priority, list) and len(schema_priority) > 1 else None
    noncanonical = noncanonical_cash_flow_candidates(property_config)
    return {
        "status": downstream.get("status"),
        "selected": workbook.get("selected"),
        "selected_schema": schema,
        "candidate_count": count(workbook.get("candidate_count")),
        "mixed_template_candidate_count": count(workbook.get("mixed_template_candidate_count")),
        "schema_labels": workbook.get("schema_labels") or [],
        "canonical_ok": schema == "dao_eco_template" and count(workbook.get("mixed_template_candidate_count")) == 0,
        "noncanonical_candidate_count": len(noncanonical),
        "noncanonical_candidates": noncanonical,
        "noncanonical_candidates_block_live_import": False,
    }


def scope_review_summary(scope_review: dict[str, Any]) -> dict[str, Any]:
    import_coverage = (
        scope_review.get("import_coverage") if isinstance(scope_review.get("import_coverage"), dict) else {}
    )
    pre_live = (
        scope_review.get("pre_live_cron_readiness")
        if isinstance(scope_review.get("pre_live_cron_readiness"), dict)
        else {}
    )
    checks = pre_live.get("checks") if isinstance(pre_live.get("checks"), dict) else {}
    failed_checks = sorted(str(key) for key, value in checks.items() if not value)
    review_reasons = [str(reason) for reason in scope_review.get("review_reasons") or []]
    cash_flow_issues = scope_review.get("cash_flow_selection_issues") or []
    cash_flow_duplicate_warnings = scope_review.get("cash_flow_duplicate_template_warnings") or []
    unmatched_count = count(scope_review.get("unmatched_scope_candidate_count"))
    unqueued_count = count(import_coverage.get("unqueued_nonzero_property_count"))
    duplicate_warning_count = count(scope_review.get("cash_flow_duplicate_template_warning_count"))
    blocking_duplicate_warning_count = count(
        scope_review.get("cash_flow_blocking_duplicate_template_warning_count")
    )
    ready_for_cron = (
        pre_live.get("ready") is True
        and not review_reasons
        and unmatched_count == 0
        and unqueued_count == 0
        and not cash_flow_issues
        and blocking_duplicate_warning_count == 0
    )
    return {
        "status": scope_review.get("status"),
        "completion_state": scope_review.get("completion_state"),
        "ready_for_cron_owned_live_import": ready_for_cron,
        "pre_live_status": pre_live.get("status"),
        "pre_live_failed_checks": failed_checks,
        "review_reasons": review_reasons,
        "scope_candidate_count": count(scope_review.get("scope_candidate_count")),
        "configured_scope_match_count": count(scope_review.get("configured_scope_match_count")),
        "unmatched_scope_candidate_count": unmatched_count,
        "cash_flow_selection_issue_count": len(cash_flow_issues),
        "cash_flow_duplicate_template_warning_count": duplicate_warning_count,
        "cash_flow_blocking_duplicate_template_warning_count": blocking_duplicate_warning_count,
        "cash_flow_duplicate_template_warnings": cash_flow_duplicate_warnings,
        "zero_row_property_count": count(import_coverage.get("zero_row_property_count")),
        "nonzero_dry_run_property_count": count(import_coverage.get("nonzero_dry_run_property_count")),
        "queued_nonzero_property_count": count(import_coverage.get("queued_nonzero_property_count")),
        "unqueued_nonzero_property_count": unqueued_count,
    }


def completion_summary(completion: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": completion.get("status"),
        "primary_blocker": completion.get("primary_blocker"),
        "queue_status": completion.get("queue_status"),
        "failed_checks": completion.get("failed_checks") or completion.get("review_reasons") or [],
        "ledger_missing_key_count": count(completion.get("ledger_missing_key_count")),
        "manifest_matched_key_count": count(completion.get("manifest_matched_key_count")),
        "cash_flow_mixed_template_candidate_count": count(completion.get("cash_flow_mixed_template_candidate_count")),
    }


def local_duplicate_scan_summary(scan: dict[str, Any], scan_path: Path | None = None) -> dict[str, Any]:
    hit_key_count = count(scan.get("hit_key_count"))
    key_count = count(scan.get("key_count"))
    searched_file_count = count(scan.get("searched_file_count"))
    status = scan.get("status")
    if status is None and scan.get("job") == "aligned-owner-local-ledger-duplicate-key-scan":
        status = "ok"
    return {
        "status": status,
        "path": scan.get("path") or (str(scan_path) if scan_path else None),
        "note": scan.get("note"),
        "key_count": key_count,
        "searched_file_count": searched_file_count,
        "hit_count": count(scan.get("hit_count")),
        "hit_key_count": hit_key_count,
        "local_scan_no_hits": key_count > 0 and searched_file_count > 0 and hit_key_count == 0,
        "authoritative_for_live_import": False,
    }


def import_label_guard_summary(report_dir: Path, queue: dict[str, Any]) -> dict[str, Any]:
    months = [str(month) for month in queue.get("months") or []]
    report_count = 0
    missing_months: list[str] = []
    non_ok_months: list[str] = []
    label_guard_non_ok_months: list[str] = []
    expected_plan_non_ok_months: list[str] = []
    issue_months: list[str] = []
    disallowed_rich_category_count = 0
    expected_disallowed_rich_category_count = 0
    tag_mismatch_count = 0
    issue_count = 0
    report_paths: dict[str, str] = {}

    for month in months:
        report_path = report_dir / f"baselane_aligned_owner_statement_import_{month}.json"
        report_paths[month] = str(report_path)
        report = read_json(report_path)
        if report.get("status") == "missing":
            missing_months.append(month)
            continue
        report_count += 1
        if report.get("status") != "ok":
            non_ok_months.append(month)

        label_guard = report.get("label_guard") if isinstance(report.get("label_guard"), dict) else {}
        if label_guard.get("status") != "ok":
            label_guard_non_ok_months.append(month)
        disallowed_rich_category_count += count(label_guard.get("disallowed_rich_category_count"))

        expected_plan_check = (
            report.get("expected_plan_check") if isinstance(report.get("expected_plan_check"), dict) else {}
        )
        if expected_plan_check.get("status") != "ok":
            expected_plan_non_ok_months.append(month)
        expected_disallowed_rich_category_count += count(
            expected_plan_check.get("expected_disallowed_rich_category_count")
        )
        tag_mismatch_count += count(expected_plan_check.get("tag_mismatch_count"))

        issues = report.get("issues") if isinstance(report.get("issues"), list) else []
        if issues:
            issue_months.append(month)
            issue_count += len(issues)

    ok = (
        bool(months)
        and not missing_months
        and not non_ok_months
        and not label_guard_non_ok_months
        and not expected_plan_non_ok_months
        and disallowed_rich_category_count == 0
        and expected_disallowed_rich_category_count == 0
        and tag_mismatch_count == 0
        and issue_count == 0
    )
    return {
        "status": "ok" if ok else "review",
        "ok": ok,
        "report_dir": str(report_dir),
        "expected_month_count": len(months),
        "report_count": report_count,
        "months": months,
        "report_paths": report_paths,
        "missing_months": missing_months,
        "non_ok_months": non_ok_months,
        "label_guard_non_ok_months": label_guard_non_ok_months,
        "expected_plan_non_ok_months": expected_plan_non_ok_months,
        "issue_months": issue_months,
        "disallowed_rich_category_count": disallowed_rich_category_count,
        "expected_disallowed_rich_category_count": expected_disallowed_rich_category_count,
        "tag_mismatch_count": tag_mismatch_count,
        "issue_count": issue_count,
    }


def primary_blocker(parts: dict[str, dict[str, Any]], queue: dict[str, Any]) -> str | None:
    if not parts["auth"]["ok"]:
        return "baselane_auth_not_verified"
    if parts["cash_flow"]["mixed_template_candidate_count"]:
        return "cash_flow_mixed_template_duplicates"
    if not parts["cash_flow"]["canonical_ok"]:
        return "cash_flow_workbook_not_canonical_dao_eco"
    if not parts["scope_review"]["ready_for_cron_owned_live_import"] and queue.get("status") != "completed":
        return "scope_review_not_ready_for_cron_live_import"
    if not parts["import_label_guard"]["ok"] and queue.get("status") != "completed":
        return "aligned_import_label_guard_not_ok"
    if parts["preflight"].get("auth_error_months"):
        return "baselane_auth_not_verified"
    if parts["preflight"]["query_error_months"]:
        return "baselane_duplicate_query_failed"
    if "duplicate_keys_present" in set(parts["preflight"].get("review_reasons") or []):
        return "aligned_rows_already_exist_or_duplicate"
    if not parts["preflight"]["ok"]:
        return "aligned_live_preflight_not_ok"
    if parts["preflight"]["to_create_count_total"] == 0 and parts["preflight"].get("expected_plan_coverage_complete"):
        if parts["completion"]["status"] == "complete":
            return None
        return "pending_downstream_validation_after_live_import"
    if queue.get("status") == "completed" and parts["completion"]["status"] != "complete":
        return "completion_gate_not_clean"
    if parts["completion"]["status"] == "complete":
        return None
    return "pending_cron_owned_live_import"


def build_report(
    root: Path,
    queue_path: Path,
    config_path: Path,
    auth_report_path: Path,
    preflight_summary_path: Path,
    downstream_report_path: Path,
    scope_review_path: Path,
    completion_report_path: Path,
    import_report_dir: Path,
    local_duplicate_scan_path: Path | None = None,
) -> dict[str, Any]:
    queue = read_json(queue_path)
    config = read_json(config_path)
    expected = queue.get("expected") if isinstance(queue.get("expected"), dict) else {}
    property_id = str(expected.get("baselane_property_id") or "")
    property_config = find_property_config(config, property_id)
    parts = {
        "auth": auth_summary(read_json(auth_report_path)),
        "preflight": preflight_summary(read_json(preflight_summary_path)),
        "cash_flow": cash_flow_summary(read_json(downstream_report_path), property_config),
        "scope_review": scope_review_summary(read_json(scope_review_path)),
        "completion": completion_summary(read_json(completion_report_path)),
        "import_label_guard": import_label_guard_summary(import_report_dir, queue),
        "local_ledger_duplicate_scan": local_duplicate_scan_summary(
            read_json(local_duplicate_scan_path or DEFAULT_LOCAL_LEDGER_DUPLICATE_SCAN),
            local_duplicate_scan_path or DEFAULT_LOCAL_LEDGER_DUPLICATE_SCAN,
        ),
    }
    blocker = primary_blocker(parts, queue)
    ready_for_cron_live_import = (
        blocker == "pending_cron_owned_live_import"
        and queue.get("status") == "queued"
        and count(expected.get("to_create_count")) > 0
    )
    complete = parts["completion"]["status"] == "complete" and queue.get("status") == "completed"
    status = "complete" if complete else ("ready_for_cron_live_import" if ready_for_cron_live_import else "review")
    return {
        "job": "aligned-owner-statement-import-readiness",
        "generated_at": iso_z(),
        "status": status,
        "primary_blocker": blocker,
        "ready_for_cron_live_import": ready_for_cron_live_import,
        "live_write_policy": "Do not run manual live import; live writes must go through the cron-owned monthly Baselane path.",
        "queue": {
            "path": str(queue_path),
            "status": queue.get("status"),
            "queue_id": queue.get("queue_id"),
            "months": queue.get("months") or [],
            "expected": expected,
        },
        "property": {
            "baselane_property_id": property_id,
            "property_full": property_config.get("property_full"),
            "property_short": property_config.get("property_short"),
            "search_roots": property_config.get("search_roots") or [],
        },
        "artifacts": {
            "auth_report": str(auth_report_path),
            "preflight_summary": str(preflight_summary_path),
            "downstream_report": str(downstream_report_path),
            "scope_review": str(scope_review_path),
            "completion_report": str(completion_report_path),
            "import_report_dir": str(import_report_dir),
            "local_ledger_duplicate_scan": str(local_duplicate_scan_path or DEFAULT_LOCAL_LEDGER_DUPLICATE_SCAN),
        },
        **parts,
    }


def markdown(report: dict[str, Any]) -> str:
    auth = report.get("auth") or {}
    preflight = report.get("preflight") or {}
    cash_flow = report.get("cash_flow") or {}
    scope_review = report.get("scope_review") or {}
    completion = report.get("completion") or {}
    import_label_guard = report.get("import_label_guard") or {}
    local_duplicate_scan = report.get("local_ledger_duplicate_scan") or {}
    lines = [
        "# Aligned Owner Statement Import Readiness",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Primary blocker: `{report.get('primary_blocker')}`",
        f"- Ready for cron live import: `{report.get('ready_for_cron_live_import')}`",
        f"- Queue status: `{(report.get('queue') or {}).get('status')}`",
        f"- Expected rows: `{((report.get('queue') or {}).get('expected') or {}).get('to_create_count')}`",
        "",
        "## Baselane Auth",
        f"- Status: `{auth.get('status')}`",
        f"- Issue: `{auth.get('issue_summary')}`",
        f"- Next action: `{auth.get('next_action')}`",
        f"- CDP URL: `{auth.get('cdp_url')}`",
        f"- Preferred CDP attempt failed: `{auth.get('preferred_cdp_attempt_failed')}`",
        f"- Verified authenticated tabs: `{auth.get('verified_authenticated_tab_count')}`",
        f"- Non-Baselane page tabs on reachable CDP: `{auth.get('non_baselane_page_tab_count')}`",
        f"- URL-only auth evidence: `{auth.get('url_only_auth_evidence')}`",
        f"- Manual auth required: `{auth.get('manual_auth_required')}`",
        f"- Manual auth reason: `{auth.get('manual_auth_reason')}`",
        "",
        "## Duplicate Preflight",
        f"- Status: `{preflight.get('status')}`",
        f"- Complete/trusted-zero: `{preflight.get('duplicate_check_complete')}` / `{preflight.get('duplicate_check_trusted_zero')}`",
        f"- Expected existing rows are idempotent: `{preflight.get('expected_existing_keys_idempotent')}`",
        f"- Planned/to-create/created: `{preflight.get('planned_count_total')}` / `{preflight.get('to_create_count_total')}` / `{preflight.get('created_count_total')}`",
        f"- Existing observed/skipped expected keys: `{preflight.get('existing_key_count_total')}` / `{preflight.get('skipped_existing_count_total')}`",
        f"- Expected remaining-or-existing coverage: `{preflight.get('expected_remaining_or_existing_total')}`",
        f"- Live duplicate-query months: `{preflight.get('live_duplicate_query_month_count')}`",
        f"- Staging fallback months: `{preflight.get('used_staging_fallback_month_count')}`",
        f"- Staging plan complete: `{preflight.get('staging_plan_complete')}`",
        f"- Query-error months: `{len(preflight.get('query_error_months') or [])}`",
        f"- Auth-error months: `{len(preflight.get('auth_error_months') or [])}`",
        f"- Timeout/missing-report months: `{len(preflight.get('timed_out_months') or [])}` / `{len(preflight.get('missing_report_months') or [])}`",
        "",
        "## Local Ledger Duplicate Scan",
        f"- Status: `{local_duplicate_scan.get('status')}`",
        f"- Non-authoritative: `{not local_duplicate_scan.get('authoritative_for_live_import')}`",
        f"- Keys/files searched: `{local_duplicate_scan.get('key_count')}` / `{local_duplicate_scan.get('searched_file_count')}`",
        f"- Hit keys/hits: `{local_duplicate_scan.get('hit_key_count')}` / `{local_duplicate_scan.get('hit_count')}`",
        f"- Local scan no hits: `{local_duplicate_scan.get('local_scan_no_hits')}`",
        f"- Note: `{local_duplicate_scan.get('note')}`",
        "",
        "## Cash Flow Workbook",
        f"- Selected schema: `{cash_flow.get('selected_schema')}`",
        f"- Canonical candidates: `{cash_flow.get('candidate_count')}`",
        f"- Mixed-template canonical candidates: `{cash_flow.get('mixed_template_candidate_count')}`",
        f"- Noncanonical/archived candidates: `{cash_flow.get('noncanonical_candidate_count')}`",
        f"- Selected workbook: `{cash_flow.get('selected')}`",
        "",
        "## Scope Review",
        f"- Status: `{scope_review.get('status')}`",
        f"- Ready for cron live import: `{scope_review.get('ready_for_cron_owned_live_import')}`",
        f"- Scope/configured/unmatched: `{scope_review.get('scope_candidate_count')}` / `{scope_review.get('configured_scope_match_count')}` / `{scope_review.get('unmatched_scope_candidate_count')}`",
        f"- Nonzero queued/unqueued: `{scope_review.get('queued_nonzero_property_count')}` / `{scope_review.get('unqueued_nonzero_property_count')}`",
        f"- Zero-row reviewed count: `{scope_review.get('zero_row_property_count')}`",
        f"- Active mixed-template duplicate warnings/blocking: `{scope_review.get('cash_flow_duplicate_template_warning_count')}` / `{scope_review.get('cash_flow_blocking_duplicate_template_warning_count')}`",
        "",
        "## Import Label Guard",
        f"- Status: `{import_label_guard.get('status')}`",
        f"- Reports/months: `{import_label_guard.get('report_count')}` / `{import_label_guard.get('expected_month_count')}`",
        f"- Missing months: `{len(import_label_guard.get('missing_months') or [])}`",
        f"- Label-guard non-ok months: `{len(import_label_guard.get('label_guard_non_ok_months') or [])}`",
        f"- Disallowed generated/reviewed labels: `{import_label_guard.get('disallowed_rich_category_count')}` / `{import_label_guard.get('expected_disallowed_rich_category_count')}`",
        f"- Expected tag mismatches: `{import_label_guard.get('tag_mismatch_count')}`",
        "",
        "## Completion Gate",
        f"- Status: `{completion.get('status')}`",
        f"- Queue status: `{completion.get('queue_status')}`",
        f"- Ledger missing keys: `{completion.get('ledger_missing_key_count')}`",
        f"- Manifest matched keys: `{completion.get('manifest_matched_key_count')}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--auth-report", type=Path, default=DEFAULT_AUTH_REPORT)
    parser.add_argument("--preflight-summary", type=Path, default=DEFAULT_PREFLIGHT_SUMMARY)
    parser.add_argument("--downstream-report", type=Path, default=DEFAULT_DOWNSTREAM_REPORT)
    parser.add_argument("--scope-review", type=Path, default=DEFAULT_SCOPE_REVIEW)
    parser.add_argument("--completion-report", type=Path, default=DEFAULT_COMPLETION_REPORT)
    parser.add_argument("--import-report-dir", type=Path, default=DEFAULT_IMPORT_REPORT_DIR)
    parser.add_argument("--local-ledger-duplicate-scan", type=Path, default=DEFAULT_LOCAL_LEDGER_DUPLICATE_SCAN)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()

    report = build_report(
        ROOT,
        abs_path(args.queue, ROOT),
        abs_path(args.config, ROOT),
        abs_path(args.auth_report, ROOT),
        abs_path(args.preflight_summary, ROOT),
        abs_path(args.downstream_report, ROOT),
        abs_path(args.scope_review, ROOT),
        abs_path(args.completion_report, ROOT),
        abs_path(args.import_report_dir, ROOT),
        abs_path(args.local_ledger_duplicate_scan, ROOT),
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] in {"complete", "ready_for_cron_live_import"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
