#!/usr/bin/env python3
"""Build a read-only Cleveland/Hemlane Aligned owner-statement review report.

This script does not call Baselane and does not write Cash Flow workbooks. It
ties the Yhome Transition Reconciliation scope to local dry-run import reports,
the queued Baselane backfill, downstream ledger validation, and CF workbook
selection evidence.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
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
REPORTS = ROOT / "reports"
DEFAULT_CONFIG = ROOT / "config" / "aligned_owner_statement_imports.json"
DEFAULT_QUEUE = ROOT / "config" / "aligned_owner_statement_backfill_queue.json"
CF_SCRIPT = ROOT / "skills" / "baselane-financials" / "scripts" / "update_cf_statements.py"


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def load_cf_module() -> Any:
    spec = importlib.util.spec_from_file_location("cf_update", CF_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load CF updater module from {CF_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decimal_value(value: Any) -> Decimal:
    text = str(value if value is not None else "0").replace("$", "").replace(",", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return Decimal(text or "0").quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def latest_file(patterns: list[str]) -> Path | None:
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(path for path in REPORTS.glob(pattern) if path.is_file())
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def default_yhome_csv() -> Path:
    latest = latest_file(["yhome_transition_reconciliation.live.*.csv", "yhome_transition_reconciliation.csv"])
    return latest or (REPORTS / "yhome_transition_reconciliation.csv")


def default_monthly_report() -> Path:
    latest = latest_file(["aligned_monthly_*/baselane_monthly_statements_idempotent_report.json"])
    return latest or (REPORTS / "baselane_monthly_statements_idempotent_report.json")


def default_downstream_report(monthly_report: Path) -> Path:
    monthly = read_json(monthly_report)
    path = monthly.get("aligned_owner_downstream_validation_report")
    if path:
        candidate = Path(str(path))
        if candidate.is_file():
            return candidate
    latest = latest_file(["aligned_monthly_*/aligned_owner_statement_downstream_validation.json"])
    return latest or (REPORTS / "aligned_owner_statement_downstream_validation.json")


def reconciliation_scope(yhome_csv: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scoped: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    with yhome_csv.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            prop = str(row.get("Property") or "").strip()
            pm = str(row.get("New PM") or "").strip()
            sub_pm = str(row.get("New Sub-PM") or "").strip()
            if "Cleveland" not in prop or "Hemlane" not in sub_pm:
                continue
            item = {
                "property": prop,
                "new_pm": pm,
                "new_sub_pm": sub_pm,
                "non_sold_or_selling": "Sold" not in pm,
                "status_description": row.get("Status Description"),
            }
            if item["non_sold_or_selling"]:
                scoped.append(item)
            else:
                excluded.append(item)
    return scoped, excluded


def property_matches(cf_module: Any, candidate: str, reference: str) -> bool:
    if not candidate or not reference:
        return False
    return bool(
        cf_module.normalized_property_is_match(candidate, reference)
        or cf_module.normalized_property_is_match(reference, candidate)
    )


def config_for_scope(config: dict[str, Any], scope_rows: list[dict[str, Any]], cf_module: Any) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    for scope_row in scope_rows:
        scope_property = str(scope_row.get("property") or "")
        for item in config.get("properties") or []:
            references = [
                str(item.get("property_full") or ""),
                str(item.get("property_short") or ""),
                str(item.get("baselane_property") or ""),
                *[str(alias or "") for alias in item.get("property_aliases") or []],
            ]
            if any(property_matches(cf_module, scope_property, reference) for reference in references):
                property_id = str(item.get("baselane_property_id") or "")
                if property_id and property_id not in seen:
                    copied = dict(item)
                    copied["_reconciliation_property"] = scope_property
                    matched.append(copied)
                    seen.add(property_id)
                break
    return matched


def blank_month_record(report_path: Path, month: str) -> dict[str, Any]:
    return {
        "report": str(report_path),
        "month": month,
        "planned_count": 0,
        "skipped_existing_count": 0,
        "to_create_count": 0,
        "to_create_amount": "0.00",
        "sample_to_create_rows": [],
    }


def import_dry_run_summary(
    configs: list[dict[str, Any]],
    report_paths: list[Path],
) -> dict[str, dict[str, Any]]:
    properties: dict[str, dict[str, Any]] = {}
    by_id = {str(item.get("baselane_property_id") or ""): item for item in configs}
    for item in configs:
        short = str(item.get("property_short") or item.get("property_full") or item.get("baselane_property_id"))
        properties[short] = {
            "property_full": item.get("property_full"),
            "property_short": item.get("property_short"),
            "reconciliation_property": item.get("_reconciliation_property"),
            "baselane_property_id": str(item.get("baselane_property_id") or ""),
            "transition_date": item.get("transition_date"),
            "configured_search_roots": item.get("search_roots") or [],
            "dry_run_by_month": {},
            "dry_run_totals": {
                "planned_count": 0,
                "skipped_existing_count": 0,
                "to_create_count": 0,
                "to_create_amount": "0.00",
            },
            "status": "not_evaluated",
        }

    for report_path in report_paths:
        report = read_json(report_path)
        month = str(report.get("month") or report_path.stem.rsplit("_", 1)[-1])
        skipped = set(str(key) for key in report.get("skipped_existing_keys") or [])
        month_records = {
            str(item.get("baselane_property_id") or ""): blank_month_record(report_path, month)
            for item in configs
        }
        for row in report.get("planned_rows") or []:
            property_id = str(row.get("propertyId") or "")
            if property_id not in by_id:
                continue
            record = month_records[property_id]
            record["planned_count"] += 1
            key = str(row.get("idempotency_key") or "")
            if key in skipped:
                record["skipped_existing_count"] += 1
                continue
            record["to_create_count"] += 1
            record["to_create_amount"] = f"{decimal_value(record['to_create_amount']) + decimal_value(row.get('amount')):.2f}"
            if len(record["sample_to_create_rows"]) < 5:
                record["sample_to_create_rows"].append(
                    {
                        "date": row.get("date"),
                        "amount": row.get("amount"),
                        "richCategory": row.get("richCategory"),
                        "idempotency_key": key,
                    }
                )
        for property_id, record in month_records.items():
            item = by_id[property_id]
            short = str(item.get("property_short") or item.get("property_full") or property_id)
            properties[short]["dry_run_by_month"][month] = record
            totals = properties[short]["dry_run_totals"]
            totals["planned_count"] += record["planned_count"]
            totals["skipped_existing_count"] += record["skipped_existing_count"]
            totals["to_create_count"] += record["to_create_count"]
            totals["to_create_amount"] = (
                f"{decimal_value(totals['to_create_amount']) + decimal_value(record['to_create_amount']):.2f}"
            )

    for item in properties.values():
        if int(item["dry_run_totals"]["to_create_count"]) == 0:
            item["status"] = "reviewed_no_aligned_rows_to_import_after_transition"
        else:
            item["status"] = "queued_for_cron_owned_baselane_manual_clearing_import"
    return properties


def add_cf_discovery(properties: dict[str, dict[str, Any]], cf_module: Any) -> None:
    for short, item in properties.items():
        files, metadata = cf_module.discover_cf_files(include_metadata=True, property_scope=short)
        duplicates = metadata.get("duplicate_candidates") or {}
        selected_paths = {str(path) for path in files.values()}
        selected_items = []
        for key, selected_path in files.items():
            selected_items.append(
                {
                    "property_key": key,
                    "selected": str(selected_path),
                    "selected_schema_priority": list(cf_module.cf_workbook_schema_priority(selected_path)),
                    "duplicate_candidate": duplicates.get(key) or None,
                }
            )
        noncanonical_outputs = active_noncanonical_cash_flow_outputs(item, selected_paths)
        item["cash_flow_workbook_discovery"] = {
            "property_scope": short,
            "selected_items": selected_items,
            "duplicate_candidate_count": len(duplicates),
            "duplicate_template_mismatch_property_count": metadata.get("duplicate_template_mismatch_property_count") or 0,
            "duplicate_template_mismatch_candidates": metadata.get("duplicate_template_mismatch_candidates") or [],
            "active_noncanonical_output_count": len(noncanonical_outputs),
            "active_noncanonical_outputs": noncanonical_outputs,
            "selection_policy": "Prefer DAO/ECO template over generic Owner Contributions/Distributions template.",
        }


def active_noncanonical_cash_flow_outputs(item: dict[str, Any], selected_paths: set[str]) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    selected = set()
    for selected_path in selected_paths:
        if not selected_path:
            continue
        selected_candidate = Path(selected_path)
        selected.add(str(selected_candidate))
        try:
            selected.add(str(selected_candidate.resolve()))
        except OSError:
            pass
    allowed_suffixes = {".csv", ".pdf", ".xlsx"}
    for root_text in item.get("configured_search_roots") or []:
        root = Path(str(root_text))
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir(), key=lambda candidate: str(candidate).lower()):
            if not path.is_file():
                continue
            lowered = path.name.lower()
            looks_like_cash_flow_output = (
                lowered.startswith("cash flow statement")
                or lowered.startswith("cash_flow")
                or lowered.startswith("cash-flow")
            )
            try:
                resolved_path = str(path.resolve())
            except OSError:
                resolved_path = str(path)
            if (
                not looks_like_cash_flow_output
                or "conflict" in lowered
                or "conflicted copy" in lowered
                or lowered.startswith("~$")
                or str(path) in selected
                or resolved_path in selected
                or path.suffix.lower() not in allowed_suffixes
            ):
                continue
            try:
                size_bytes = path.stat().st_size
            except OSError:
                size_bytes = None
            outputs.append(
                {
                    "path": str(path),
                    "file": path.name,
                    "suffix": path.suffix.lower(),
                    "size_bytes": size_bytes,
                    "reason": "active_cash_flow_statement_output_not_selected_canonical_workbook",
                }
            )
    return outputs


def cash_flow_selection_issues(properties: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for short, item in properties.items():
        discovery = item.get("cash_flow_workbook_discovery") or {}
        selected_items = discovery.get("selected_items") or []
        if not selected_items:
            issues.append(
                {
                    "property": short,
                    "reason": "cash_flow_workbook_missing",
                }
            )
            continue
        for selected in selected_items:
            schema_priority = selected.get("selected_schema_priority") or []
            schema = schema_priority[1] if len(schema_priority) > 1 else None
            if schema != "dao_eco_template":
                issues.append(
                    {
                        "property": short,
                        "reason": "cash_flow_workbook_not_dao_eco_template",
                        "selected": selected.get("selected"),
                        "selected_schema_priority": schema_priority,
                    }
                )
    return issues


def cash_flow_duplicate_template_warnings(properties: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for short, item in properties.items():
        discovery = item.get("cash_flow_workbook_discovery") or {}
        candidates = discovery.get("duplicate_template_mismatch_candidates") or []
        to_create_count = int((item.get("dry_run_totals") or {}).get("to_create_count") or 0)
        if candidates:
            warnings.append(
                {
                    "property": short,
                    "reason": "cash_flow_active_mixed_template_duplicate",
                    "blocks_live_import": to_create_count > 0,
                    "to_create_count": to_create_count,
                    "duplicate_template_mismatch_property_count": (
                        discovery.get("duplicate_template_mismatch_property_count") or 0
                    ),
                    "duplicate_template_mismatch_candidates": candidates,
                }
            )
        noncanonical_outputs = discovery.get("active_noncanonical_outputs") or []
        if noncanonical_outputs:
            warnings.append(
                {
                    "property": short,
                    "reason": "cash_flow_active_noncanonical_output_duplicate",
                    "blocks_live_import": to_create_count > 0,
                    "to_create_count": to_create_count,
                    "active_noncanonical_output_count": len(noncanonical_outputs),
                    "active_noncanonical_outputs": noncanonical_outputs,
                }
            )
    return warnings


def queue_expected_property_ids(queue: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    expected = queue.get("expected") if isinstance(queue.get("expected"), dict) else {}
    for key in ("baselane_property_id", "property_id"):
        value = str(expected.get(key) or "").strip()
        if value:
            ids.add(value)
    for item in queue.get("expected_properties") or []:
        if not isinstance(item, dict):
            continue
        value = str(item.get("baselane_property_id") or item.get("property_id") or "").strip()
        if value:
            ids.add(value)
    return ids


def import_coverage_evidence(properties: dict[str, dict[str, Any]], queue: dict[str, Any]) -> dict[str, Any]:
    queued_property_ids = queue_expected_property_ids(queue)
    zero_row_properties: list[dict[str, Any]] = []
    nonzero_properties: list[dict[str, Any]] = []
    queued_nonzero_properties: list[dict[str, Any]] = []
    unqueued_nonzero_properties: list[dict[str, Any]] = []

    for short, item in properties.items():
        property_id = str(item.get("baselane_property_id") or "").strip()
        totals = item.get("dry_run_totals") or {}
        to_create_count = int(totals.get("to_create_count") or 0)
        evidence = {
            "property": short,
            "property_full": item.get("property_full"),
            "baselane_property_id": property_id,
            "to_create_count": to_create_count,
            "to_create_amount": totals.get("to_create_amount"),
            "status": item.get("status"),
        }
        if to_create_count == 0:
            zero_row_properties.append(evidence)
            continue
        nonzero_properties.append(evidence)
        if property_id in queued_property_ids:
            queued_nonzero_properties.append(evidence)
        else:
            unqueued_nonzero_properties.append(evidence)

    return {
        "queued_property_ids": sorted(queued_property_ids),
        "zero_row_properties": zero_row_properties,
        "zero_row_property_count": len(zero_row_properties),
        "nonzero_dry_run_properties": nonzero_properties,
        "nonzero_dry_run_property_count": len(nonzero_properties),
        "queued_nonzero_properties": queued_nonzero_properties,
        "queued_nonzero_property_count": len(queued_nonzero_properties),
        "unqueued_nonzero_properties": unqueued_nonzero_properties,
        "unqueued_nonzero_property_count": len(unqueued_nonzero_properties),
    }


def pre_live_cron_readiness(
    *,
    scope_rows: list[dict[str, Any]],
    scoped_configs: list[dict[str, Any]],
    unmatched_scope_candidates: list[dict[str, Any]],
    cf_issues: list[dict[str, Any]],
    import_coverage: dict[str, Any],
    queue: dict[str, Any],
    monthly: dict[str, Any],
    downstream: dict[str, Any],
    cf_duplicate_warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    def expected_plan_tag_coverage(queue_payload: dict[str, Any]) -> dict[str, Any]:
        plan = queue_payload.get("expected_plan") if isinstance(queue_payload.get("expected_plan"), dict) else {}
        months = plan.get("months") if isinstance(plan.get("months"), dict) else {}
        month_tag_counts: dict[str, int] = {}
        tag_keys: set[str] = set()
        expected_keys: set[str] = set()
        for month, month_plan in months.items():
            if not isinstance(month_plan, dict):
                continue
            tags = month_plan.get("tags_by_key") if isinstance(month_plan.get("tags_by_key"), dict) else {}
            month_tag_counts[str(month)] = len(tags)
            tag_keys.update(str(key) for key in tags if str(key))
            expected_keys.update(str(key) for key in month_plan.get("idempotency_keys") or [] if str(key))
        return {
            "expected_key_count": len(expected_keys),
            "tag_key_count": len(tag_keys),
            "missing_tag_key_count": len(expected_keys - tag_keys),
            "month_tag_counts": dict(sorted(month_tag_counts.items())),
        }

    expected = queue.get("expected") or {}
    expected_rows = downstream.get("expected_rows") or {}
    ledger_presence = downstream.get("ledger_presence") or {}
    ledger_label_presence = downstream.get("ledger_label_presence") or {}
    cash_flow_workbook = downstream.get("cash_flow_workbook") or {}
    cf_schema_priority = cash_flow_workbook.get("selected_schema_priority") or []
    cf_schema = cf_schema_priority[1] if len(cf_schema_priority) > 1 else None
    expected_count = expected.get("to_create_count")
    expected_amount = expected.get("amount_total")
    expected_rows_count = expected_rows.get("count")
    expected_rows_amount = expected_rows.get("amount_total")
    ledger_found_count = int(ledger_presence.get("found_key_count") or 0)
    ledger_missing_count = int(ledger_presence.get("missing_key_count") or 0)
    monthly_to_create = monthly.get("aligned_owner_import_backfill_to_create_count")
    if monthly_to_create is None:
        monthly_to_create = monthly.get("to_create_count_total")
    monthly_created = monthly.get("aligned_owner_import_backfill_created_count")
    if monthly_created is None:
        monthly_created = monthly.get("created_count_total")
    monthly_skipped_existing = monthly.get("skipped_existing_count_total")
    monthly_existing_key_count = monthly.get("existing_key_count_total")
    monthly_planned_count = monthly.get("planned_count_total")
    monthly_remaining_or_existing = monthly.get("expected_remaining_or_existing_total")
    coverage_to_create = sum(
        int(item.get("to_create_count") or 0)
        for item in import_coverage.get("nonzero_dry_run_properties") or []
        if isinstance(item, dict)
    )
    effective_to_create = monthly_to_create
    if int(effective_to_create or 0) == 0 and coverage_to_create and monthly.get("to_create_count_total") is None:
        effective_to_create = coverage_to_create
    if monthly_remaining_or_existing is None:
        if monthly_planned_count is not None:
            monthly_remaining_or_existing = monthly_planned_count
        elif monthly_to_create is not None:
            monthly_remaining_or_existing = int(monthly_to_create or 0) + int(monthly_skipped_existing or 0)
    queue_tag_coverage = expected_plan_tag_coverage(queue)
    blocking_duplicate_warnings = [
        warning for warning in (cf_duplicate_warnings or []) if warning.get("blocks_live_import")
    ]

    checks = {
        "scope_has_candidates": bool(scope_rows),
        "all_scope_candidates_configured": len(scope_rows) == len(scoped_configs) and not unmatched_scope_candidates,
        "all_zero_row_properties_reviewed": all(
            item.get("status") == "reviewed_no_aligned_rows_to_import_after_transition"
            for item in import_coverage.get("zero_row_properties") or []
        ),
        "all_nonzero_dry_run_properties_queued": int(import_coverage.get("unqueued_nonzero_property_count") or 0) == 0,
        "no_cash_flow_selection_issues": not cf_issues,
        "no_blocking_cash_flow_duplicate_template_warnings": not blocking_duplicate_warnings,
        "queue_is_queued": queue.get("status") == "queued",
        "downstream_pending_import": downstream.get("status") == "pending_import",
        "expected_count_matches_queue": (
            expected_count is None or int(expected_count) == int(expected_rows_count or 0)
        ),
        "expected_amount_matches_queue": (
            expected_amount is None or decimal_value(expected_amount) == decimal_value(expected_rows_amount)
        ),
        "ledger_has_no_expected_keys_yet": ledger_found_count == 0,
        "ledger_missing_count_matches_expected": (
            expected_count is None or ledger_missing_count == int(expected_count)
        ),
        "expected_tags_match_queue": (
            expected_count is None
            or int(ledger_label_presence.get("expected_tag_count") or 0) == int(expected_count)
        ),
        "expected_plan_tags_cover_expected": (
            expected_count is None
            or (
                queue_tag_coverage["expected_key_count"] == int(expected_count)
                and queue_tag_coverage["tag_key_count"] == int(expected_count)
                and queue_tag_coverage["missing_tag_key_count"] == 0
            )
        ),
        "ledger_label_mismatch_zero": int(ledger_label_presence.get("mismatch_count") or 0) == 0,
        "dry_run_created_no_rows": int(monthly_created or 0) == 0,
        "dry_run_to_create_matches_expected": (
            expected_count is None or int(monthly_remaining_or_existing or effective_to_create or 0) == int(expected_count)
        ),
        "cash_flow_schema_is_dao_eco_template": cf_schema == "dao_eco_template",
    }
    ready = all(checks.values())
    return {
        "status": "ready_for_cron_owned_live_import" if ready else "not_ready",
        "ready": ready,
        "checks": checks,
        "expected_to_create_count": expected_count,
        "expected_amount_total": expected_amount,
        "monthly_to_create_count": monthly_to_create,
        "monthly_skipped_existing_count": monthly_skipped_existing,
        "monthly_existing_key_count": monthly_existing_key_count,
        "monthly_planned_count": monthly_planned_count,
        "monthly_remaining_or_existing_count": monthly_remaining_or_existing,
        "coverage_to_create_count": coverage_to_create,
        "effective_to_create_count": effective_to_create,
        "ledger_found_key_count": ledger_found_count,
        "ledger_missing_key_count": ledger_missing_count,
        "ledger_expected_tag_count": ledger_label_presence.get("expected_tag_count"),
        "ledger_checked_label_key_count": ledger_label_presence.get("checked_key_count"),
        "ledger_label_mismatch_count": ledger_label_presence.get("mismatch_count"),
        "queue_expected_plan_key_count": queue_tag_coverage["expected_key_count"],
        "queue_expected_plan_tag_count": queue_tag_coverage["tag_key_count"],
        "queue_expected_plan_missing_tag_count": queue_tag_coverage["missing_tag_key_count"],
        "queue_expected_plan_month_tag_counts": queue_tag_coverage["month_tag_counts"],
        "cash_flow_selected_schema": cf_schema,
        "cash_flow_duplicate_template_warning_count": len(cf_duplicate_warnings or []),
        "cash_flow_blocking_duplicate_template_warning_count": len(blocking_duplicate_warnings),
        "cash_flow_duplicate_template_warnings": cf_duplicate_warnings or [],
        "import_coverage": import_coverage,
        "manual_live_import_allowed": False,
        "next_step": (
            "Wait for the mid-month cron-owned Baselane monthly close to run the live import, "
            "then require downstream validation status ok before marking complete."
        ),
    }


def report_paths_from_args(args: argparse.Namespace) -> list[Path]:
    if args.dry_run_report:
        return [Path(path) for path in args.dry_run_report]
    paths = sorted(REPORTS.glob(args.dry_run_report_glob))
    return [path for path in paths if path.is_file()]


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    cf_module = load_cf_module()
    yhome_csv = args.yhome_csv or default_yhome_csv()
    config_path = args.config
    queue_path = args.queue
    monthly_report_path = args.monthly_report or default_monthly_report()
    downstream_report_path = args.downstream_report or default_downstream_report(monthly_report_path)

    config = read_json(config_path)
    queue = read_json(queue_path)
    monthly = read_json(monthly_report_path)
    downstream = read_json(downstream_report_path)
    scope_rows, excluded_rows = reconciliation_scope(yhome_csv)
    scoped_configs = config_for_scope(config, scope_rows, cf_module)
    properties = import_dry_run_summary(scoped_configs, report_paths_from_args(args))
    add_cf_discovery(properties, cf_module)
    matched_scope_properties = {
        str(item.get("_reconciliation_property") or "").strip()
        for item in scoped_configs
        if str(item.get("_reconciliation_property") or "").strip()
    }
    unmatched_scope_candidates = [
        row
        for row in scope_rows
        if str(row.get("property") or "").strip() not in matched_scope_properties
    ]
    cf_issues = cash_flow_selection_issues(properties)
    cf_duplicate_warnings = cash_flow_duplicate_template_warnings(properties)
    import_coverage = import_coverage_evidence(properties, queue)

    ledger_presence = downstream.get("ledger_presence") or {}
    cf_sync = downstream.get("cash_flow_sync_evidence") or {}
    queue_status = queue.get("status")
    downstream_status = downstream.get("status")
    readiness = pre_live_cron_readiness(
        scope_rows=scope_rows,
        scoped_configs=scoped_configs,
        unmatched_scope_candidates=unmatched_scope_candidates,
        cf_issues=cf_issues,
        import_coverage=import_coverage,
        queue=queue,
        monthly=monthly,
        downstream=downstream,
        cf_duplicate_warnings=cf_duplicate_warnings,
    )
    all_zero_or_validated = all(
        int(item.get("dry_run_totals", {}).get("to_create_count") or 0) == 0
        for item in properties.values()
    ) or downstream_status == "ok"
    review_reasons: list[str] = []
    if unmatched_scope_candidates:
        review_reasons.append("in_scope_property_missing_aligned_import_config")
    if import_coverage["unqueued_nonzero_properties"]:
        review_reasons.append("in_scope_property_with_aligned_rows_missing_from_queue")
    if cf_issues:
        review_reasons.append("cash_flow_workbook_selection_issue")
    blocking_duplicate_reasons = sorted(
        {
            str(warning.get("reason") or "cash_flow_active_duplicate_output")
            for warning in cf_duplicate_warnings
            if warning.get("blocks_live_import")
        }
    )
    review_reasons.extend(blocking_duplicate_reasons)
    completion_state = (
        "complete_evidence_present"
        if (
            queue_status == "completed"
            and downstream_status == "ok"
            and all_zero_or_validated
            and not unmatched_scope_candidates
            and not import_coverage["unqueued_nonzero_properties"]
            and not cf_issues
            and not any(warning.get("blocks_live_import") for warning in cf_duplicate_warnings)
        )
        else "not_complete_until_queued_import_and_downstream_validation_ok"
    )
    status = (
        "review"
        if review_reasons
        else ("ok" if completion_state == "complete_evidence_present" else "pending_import")
    )

    return {
        "job": "aligned-owner-statement-cleveland-hemlane-current-review",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": status,
        "review_reasons": review_reasons,
        "reconciliation_source": str(yhome_csv),
        "scope": (
            "Cleveland rows in Yhome Transition Reconciliation where New Sub-PM is "
            "Hemlane and New PM is not ECO (Sold). ECO (Selling) remains in scope."
        ),
        "scope_candidates": scope_rows,
        "scope_candidate_count": len(scope_rows),
        "configured_scope_match_count": len(scoped_configs),
        "unmatched_scope_candidates": unmatched_scope_candidates,
        "unmatched_scope_candidate_count": len(unmatched_scope_candidates),
        "excluded_cleveland_hemlane_sold_rows": excluded_rows,
        "import_config": str(config_path),
        "cash_flow_selection_issues": cf_issues,
        "cash_flow_duplicate_template_warnings": cf_duplicate_warnings,
        "cash_flow_duplicate_template_warning_count": len(cf_duplicate_warnings),
        "cash_flow_blocking_duplicate_template_warning_count": sum(
            1 for warning in cf_duplicate_warnings if warning.get("blocks_live_import")
        ),
        "import_coverage": import_coverage,
        "pre_live_cron_readiness": readiness,
        "queue": {
            "path": str(queue_path),
            "queue_id": queue.get("queue_id"),
            "status": queue_status,
            "months": queue.get("months") or [],
            "expected": queue.get("expected") or {},
        },
        "properties": properties,
        "latest_offline_monthly_gate": {
            "report": str(monthly_report_path),
            "status": monthly.get("status"),
            "dry_run": monthly.get("dry_run"),
            "backfill_queue_status": monthly.get("aligned_owner_import_backfill_queue_status"),
            "backfill_to_create_count": (
                monthly.get("aligned_owner_import_backfill_to_create_count")
                if monthly.get("aligned_owner_import_backfill_to_create_count") is not None
                else monthly.get("to_create_count_total")
            ),
            "backfill_created_count": (
                monthly.get("aligned_owner_import_backfill_created_count")
                if monthly.get("aligned_owner_import_backfill_created_count") is not None
                else monthly.get("created_count_total")
            ),
            "backfill_planned_count": monthly.get("planned_count_total"),
            "backfill_skipped_existing_count": monthly.get("skipped_existing_count_total"),
            "backfill_existing_key_count": monthly.get("existing_key_count_total"),
            "backfill_expected_remaining_or_existing_count": monthly.get("expected_remaining_or_existing_total"),
            "downstream_validation_status": monthly.get("aligned_owner_downstream_validation_status"),
            "downstream_missing_key_count": monthly.get("aligned_owner_downstream_validation_missing_key_count"),
            "cf_selected_workbook": monthly.get("aligned_owner_cf_selected_workbook"),
            "cf_selected_schema": monthly.get("aligned_owner_cf_selected_schema"),
            "cf_duplicate_candidate_count": monthly.get("aligned_owner_cf_duplicate_candidate_count"),
            "cf_mixed_template_candidate_count": monthly.get("aligned_owner_cf_mixed_template_candidate_count"),
        },
        "latest_downstream_validation": {
            "report": str(downstream_report_path),
            "status": downstream_status,
            "review_reasons": downstream.get("review_reasons") or [],
            "expected_rows": downstream.get("expected_rows") or {},
            "ledger_presence": {
                "found_key_count": ledger_presence.get("found_key_count"),
                "missing_key_count": ledger_presence.get("missing_key_count"),
                "ledger_sources": ledger_presence.get("ledger_sources") or [],
            },
            "ledger_label_presence": downstream.get("ledger_label_presence") or {},
            "cash_flow_workbook": downstream.get("cash_flow_workbook") or {},
            "cash_flow_sync_evidence": {
                "status": cf_sync.get("status"),
                "covered_months": cf_sync.get("covered_months") or [],
                "missing_months": cf_sync.get("missing_months") or [],
                "failed_months": cf_sync.get("failed_months") or [],
                "property_audit_missing_months": cf_sync.get("property_audit_missing_months") or [],
            },
        },
        "live_apply_executed_here": False,
        "live_apply_constraint": (
            "No live Baselane upstream import is performed by this review. Queue remains "
            "for the mid-month cron-owned monthly gate using the existing Baselane auth/session path."
        ),
        "completion_state": completion_state,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yhome-csv", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument(
        "--dry-run-report-glob",
        default="baselane_aligned_owner_statement_import_dryrun_cleveland_*.json",
    )
    parser.add_argument("--dry-run-report", action="append", default=[])
    parser.add_argument("--monthly-report", type=Path, default=None)
    parser.add_argument("--downstream-report", type=Path, default=None)
    parser.add_argument(
        "--report",
        type=Path,
        default=REPORTS / f"aligned_owner_statement_cleveland_hemlane_current_review_{datetime.now():%Y%m%d}.json",
    )
    args = parser.parse_args()

    report = build_report(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
