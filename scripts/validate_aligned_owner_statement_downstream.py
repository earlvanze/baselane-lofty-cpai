#!/usr/bin/env python3
"""Validate queued Aligned owner-statement rows downstream of Baselane import.

This is intentionally report-only. It does not call Baselane and does not write
Cash Flow Statement workbooks.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
from collections import Counter, defaultdict
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
DEFAULT_REPORT = ROOT / "reports" / "aligned_owner_statement_downstream_validation.json"
DEFAULT_MANIFEST_DIR = ROOT / "reports" / "aligned-owner-statement-import-manifests"
DEFAULT_CF_SYNC_REPORT = ROOT / "reports" / "aligned_owner_statement_cf_sync_report.json"
CF_SCRIPT = ROOT / "skills" / "baselane-financials" / "scripts" / "update_cf_statements.py"


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def queue_path(raw_path: Any) -> Path:
    path = Path(str(raw_path))
    if path.is_absolute():
        return path
    return ROOT / path


def decimal_value(value: Any) -> Decimal:
    text = str(value if value is not None else "0").replace("$", "").replace(",", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return Decimal(text or "0").quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def load_cf_module():
    spec = importlib.util.spec_from_file_location("cf_update", CF_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load CF updater module from {CF_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_report_paths(queue: dict[str, Any]) -> list[Path]:
    """Prefer the queue's current expected-plan reports over legacy dry-run paths."""
    paths: list[Path] = []
    expected_plan = queue.get("expected_plan") if isinstance(queue.get("expected_plan"), dict) else {}
    source = expected_plan.get("source") if isinstance(expected_plan, dict) else None
    if source:
        source_path = queue_path(source)
        if source_path.is_dir():
            paths = sorted(path for path in source_path.glob("*.json") if path.is_file())
        elif source_path.is_file():
            paths = [source_path]
    if not paths:
        paths = [queue_path(path) for path in queue.get("dry_run_reports") or []]
    return paths


def expected_rows_from_reports(queue: dict[str, Any]) -> list[dict[str, Any]]:
    expected = queue.get("expected") or {}
    property_id = str(expected.get("baselane_property_id") or "")
    months = set(queue.get("months") or [])
    rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for path in expected_report_paths(queue):
        report = read_json(path)
        report_month = str(report.get("month") or "")
        if months and report_month and report_month not in months:
            continue
        for row in report.get("planned_rows") or []:
            if property_id and str(row.get("propertyId") or "") != property_id:
                continue
            key = str(row.get("idempotency_key") or "")
            if not key or key in seen_keys:
                continue
            enriched = dict(row)
            enriched["_source_report"] = str(path)
            enriched["_report_month"] = report_month
            rows.append(enriched)
            seen_keys.add(key)
    return rows


def expected_tags_by_key(queue: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    tags: dict[str, dict[str, str]] = {}
    for row in rows:
        key = str(row.get("idempotency_key") or "")
        if not key:
            continue
        tag_id = str(row.get("tagId") or "")
        rich_category = str(row.get("richCategory") or "")
        if tag_id or rich_category:
            tags[key] = {"tagId": tag_id, "richCategory": rich_category}

    expected_plan = queue.get("expected_plan") if isinstance(queue.get("expected_plan"), dict) else {}
    months = expected_plan.get("months") if isinstance(expected_plan, dict) else {}
    month_items: list[dict[str, Any]] = []
    if isinstance(months, dict):
        month_items = [value for value in months.values() if isinstance(value, dict)]
    elif isinstance(months, list):
        month_items = [value for value in months if isinstance(value, dict)]
    for month in month_items:
        for key, expected_tag in (month.get("tags_by_key") or {}).items():
            if not isinstance(expected_tag, dict):
                continue
            tags[str(key)] = {
                "tagId": str(expected_tag.get("tagId") or ""),
                "richCategory": str(expected_tag.get("richCategory") or ""),
            }
    return tags


def ledger_label_presence(
    expected_tags: dict[str, dict[str, str]], matches: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "expected_tag_count": len(expected_tags),
        "checked_key_count": 0,
        "matched_key_count": 0,
        "mismatch_count": 0,
        "mismatches": [],
    }
    for key in sorted(expected_tags):
        expected_rich_category = str(expected_tags.get(key, {}).get("richCategory") or "")
        if not expected_rich_category or key not in matches:
            continue
        record["checked_key_count"] += 1
        observed_categories = sorted(
            {
                str(match.get("category") or "")
                for match in matches.get(key, [])
                if str(match.get("category") or "").strip()
            }
        )
        if expected_rich_category in observed_categories:
            record["matched_key_count"] += 1
            continue
        record["mismatches"].append(
            {
                "idempotency_key": key,
                "expected_rich_category": expected_rich_category,
                "observed_categories": observed_categories,
                "matches": matches.get(key, []),
            }
        )
    record["mismatch_count"] = len(record["mismatches"])
    return record


def ledger_sources(defaults: bool, extra_paths: list[Path]) -> list[Path]:
    paths: list[Path] = []
    if defaults:
        paths.extend(
            [
                ROOT / "reports" / "baselane_source_transaction_index.csv",
                Path("/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"),
                Path("/home/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"),
            ]
        )
    paths.extend(extra_paths)
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved not in seen:
            deduped.append(path)
            seen.add(resolved)
    return deduped


def scan_ledgers(keys: set[str], paths: list[Path]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sources: list[dict[str, Any]] = []
    for path in paths:
        source_record = {"path": str(path), "exists": path.is_file(), "row_count": 0, "matched_key_count": 0}
        if not path.is_file():
            sources.append(source_record)
            continue
        matched_here: set[str] = set()
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for row_number, row in enumerate(reader, start=2):
                source_record["row_count"] += 1
                text = " | ".join(str(value or "") for value in row.values())
                for key in keys:
                    if key in text:
                        matched_here.add(key)
                        matches[key].append(
                            {
                                "path": str(path),
                                "row_number": row_number,
                                "date": row.get("ISODate") or row.get("Date"),
                                "amount": row.get("Amount"),
                                "category": row.get("Category"),
                                "subcategory": row.get("Sub-category"),
                                "property": row.get("Property"),
                                "notes": row.get("Notes"),
                            }
                        )
        source_record["matched_key_count"] = len(matched_here)
        sources.append(source_record)
    return matches, sources


def scan_manifests(keys: set[str], manifest_dir: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": str(manifest_dir),
        "exists": manifest_dir.is_dir(),
        "manifest_count": 0,
        "matched_key_count": 0,
        "matched_keys": [],
    }
    if not manifest_dir.is_dir():
        return record
    matched: set[str] = set()
    for path in sorted(manifest_dir.glob("*.json")):
        record["manifest_count"] += 1
        payload = read_json(path)
        manifest_rows = payload.get("rows")
        if not isinstance(manifest_rows, list):
            manifest_rows = payload.get("created")
        if not isinstance(manifest_rows, list):
            manifest_rows = []
        for row in manifest_rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("idempotency_key") or "")
            if key in keys:
                matched.add(key)
    record["matched_keys"] = sorted(matched)
    record["matched_key_count"] = len(matched)
    return record


def find_property_config(config: dict[str, Any], property_id: str) -> dict[str, Any]:
    for item in config.get("properties") or []:
        if str(item.get("baselane_property_id") or "") == str(property_id):
            return item
    return {}


def cf_candidates(property_config: dict[str, Any], cf_module: Any) -> dict[str, Any]:
    roots = [Path(str(path)) for path in property_config.get("search_roots") or []]
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.extend(
                path
                for path in root.glob("Cash Flow Statement*.xlsx")
                if "conflict" not in path.name.lower() and "conflicted copy" not in path.name.lower()
            )
    priorities = {str(path): cf_module.cf_workbook_schema_priority(path) for path in candidates}
    selected = None
    if candidates:
        selected = sorted(
            candidates,
            key=lambda path: (
                priorities[str(path)][0],
                cf_module.cf_candidate_priority_for_property(path, str(property_config.get("property_full") or "")),
            ),
        )[0]
    schema_labels = sorted(
        {
            priority[1]
            for priority in priorities.values()
            if isinstance(priority, (list, tuple)) and len(priority) > 1 and priority[1]
        }
    )
    selected_schema_priority = list(priorities.get(str(selected), ())) if selected else None
    mixed_template_candidates = len(schema_labels) > 1
    return {
        "candidate_count": len(candidates),
        "selected": str(selected) if selected else None,
        "selected_schema_priority": selected_schema_priority,
        "schema_labels": schema_labels,
        "mixed_template_candidate_count": len(candidates) if mixed_template_candidates else 0,
        "mixed_template_candidates": mixed_template_candidates,
        "candidates": [
            {
                "path": str(path),
                "schema_priority": list(priorities[str(path)]),
                "selected": bool(selected and path == selected),
            }
            for path in sorted(candidates, key=lambda item: str(item).lower())
        ],
    }


def normalized_matches(cf_module: Any, candidate: Any, reference: str) -> bool:
    if not candidate or not reference:
        return False
    return bool(
        cf_module.normalized_property_is_match(str(candidate), reference)
        or cf_module.normalized_property_is_match(reference, str(candidate))
    )


def audit_row_matches_property(row: dict[str, Any], property_config: dict[str, Any], property_scope: str, cf_module: Any) -> bool:
    summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
    labels = [
        row.get("property"),
        row.get("file"),
        summary.get("property"),
        summary.get("matched_gl_property"),
        summary.get("file"),
    ]
    references = [
        property_scope,
        str(property_config.get("property_short") or ""),
        str(property_config.get("property_full") or ""),
    ]
    for reference in references:
        if reference and any(normalized_matches(cf_module, label, reference) for label in labels):
            return True

    paths = [str(value or "") for value in (row.get("file"), summary.get("file")) if value]
    roots = [str(value or "") for value in property_config.get("search_roots") or [] if value]
    for path in paths:
        if any(root and path.startswith(root.rstrip("/") + "/") for root in roots):
            return True
    return False


def find_property_audit_row(audit_path: Path, property_config: dict[str, Any], property_scope: str, cf_module: Any) -> dict[str, Any]:
    try:
        rows = json.loads(audit_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if isinstance(row, dict) and audit_row_matches_property(row, property_config, property_scope, cf_module):
            return row
    return {}


def discovery_duplicate_evidence(discovery_path: Path, property_config: dict[str, Any], property_scope: str, cf_module: Any) -> list[dict[str, Any]]:
    try:
        payload = json.loads(discovery_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    duplicates = payload.get("duplicate_candidates") if isinstance(payload, dict) else {}
    if not isinstance(duplicates, dict):
        return []
    references = [
        property_scope,
        str(property_config.get("property_short") or ""),
        str(property_config.get("property_full") or ""),
    ]
    matched = []
    for key, value in duplicates.items():
        if any(reference and normalized_matches(cf_module, key, reference) for reference in references):
            item = dict(value) if isinstance(value, dict) else {"value": value}
            item["property_key"] = key
            schema_priorities = item.get("schema_priorities") or {}
            schema_labels = sorted(
                {
                    priority[1]
                    for priority in schema_priorities.values()
                    if isinstance(priority, (list, tuple)) and len(priority) > 1 and priority[1]
                }
            )
            item["schema_labels"] = schema_labels
            item["mixed_template_candidates"] = len(schema_labels) > 1
            matched.append(item)
    return matched


def cash_flow_sync_evidence(
    queue: dict[str, Any],
    property_config: dict[str, Any],
    cf_module: Any,
    cf_sync_report_path: Path,
) -> dict[str, Any]:
    required_months = [str(month) for month in queue.get("months") or [] if str(month).strip()]
    record: dict[str, Any] = {
        "report_path": str(cf_sync_report_path),
        "exists": cf_sync_report_path.is_file(),
        "required_months": required_months,
        "covered_months": [],
        "missing_months": required_months,
        "failed_months": [],
        "audit_missing_months": [],
        "discovery_missing_months": [],
        "property_audit_missing_months": [],
        "months": [],
    }
    if not required_months:
        record["status"] = "skipped_no_required_months"
        record["missing_month_count"] = 0
        return record
    if not cf_sync_report_path.is_file():
        record["status"] = "missing_report"
        record["missing_month_count"] = len(required_months)
        return record

    payload = read_json(cf_sync_report_path)
    property_scope = str(
        payload.get("property_scope")
        or property_config.get("property_short")
        or property_config.get("property_full")
        or (queue.get("expected") or {}).get("property")
        or ""
    )
    record.update(
        {
            "job_status": payload.get("status"),
            "return_code": payload.get("return_code"),
            "property_scope": property_scope,
            "ledger": payload.get("ledger"),
            "month_count": payload.get("month_count"),
        }
    )

    covered: set[str] = set()
    required = set(required_months)
    for raw_month in payload.get("months") or []:
        if not isinstance(raw_month, dict):
            continue
        month = str(raw_month.get("month") or "")
        if month not in required:
            continue
        audit_path = Path(str(raw_month.get("audit_report") or ""))
        discovery_path = Path(str(raw_month.get("discovery_report") or ""))
        audit_exists = audit_path.is_file()
        discovery_exists = discovery_path.is_file()
        audit_row = find_property_audit_row(audit_path, property_config, property_scope, cf_module) if audit_exists else {}
        summary = audit_row.get("summary") if isinstance(audit_row.get("summary"), dict) else {}
        month_record = {
            "month": month,
            "return_code": int(raw_month.get("return_code") or 0),
            "mode": raw_month.get("mode"),
            "audit_report": str(audit_path) if raw_month.get("audit_report") else None,
            "audit_report_exists": audit_exists,
            "discovery_report": str(discovery_path) if raw_month.get("discovery_report") else None,
            "discovery_report_exists": discovery_exists,
            "property_audit_found": bool(audit_row),
            "workbook": summary.get("file") or audit_row.get("file"),
            "matched_gl_property": summary.get("matched_gl_property"),
            "conflict_count": int(summary.get("conflicts") or 0),
            "cf_statement_update_count": int(summary.get("cf_statement_update_count") or 0),
            "source_cash_balance_update_count": int(summary.get("source_cash_balance_update_count") or 0),
            "duplicate_candidates": discovery_duplicate_evidence(discovery_path, property_config, property_scope, cf_module)
            if discovery_exists
            else [],
        }
        record["months"].append(month_record)
        if month_record["return_code"] != 0:
            record["failed_months"].append(month)
        if not audit_exists:
            record["audit_missing_months"].append(month)
        if not discovery_exists:
            record["discovery_missing_months"].append(month)
        if not audit_row:
            record["property_audit_missing_months"].append(month)
        if month_record["return_code"] == 0 and audit_exists and discovery_exists and audit_row:
            covered.add(month)

    record["covered_months"] = sorted(covered)
    record["missing_months"] = sorted(required - covered)
    record["missing_month_count"] = len(record["missing_months"])
    if payload.get("status") != "ok" or record["missing_months"] or record["failed_months"]:
        record["status"] = "review"
    else:
        record["status"] = "ok"
    return record


def summarize_expected(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_month: dict[str, dict[str, Any]] = {}
    by_category: dict[str, dict[str, Any]] = {}
    for row in rows:
        month = str(row.get("date") or "")[:7] or str(row.get("_report_month") or "")
        category = str(row.get("richCategory") or row.get("source_category") or "(unknown)")
        amount = decimal_value(row.get("amount"))
        for bucket, key in ((by_month, month), (by_category, category)):
            item = bucket.setdefault(key, {"count": 0, "amount": Decimal("0.00")})
            item["count"] += 1
            item["amount"] += amount
    return {
        "count": len(rows),
        "amount_total": f"{sum((decimal_value(row.get('amount')) for row in rows), Decimal('0.00')):.2f}",
        "by_month": {key: {"count": value["count"], "amount": f"{value['amount']:.2f}"} for key, value in sorted(by_month.items())},
        "by_rich_category": {
            key: {"count": value["count"], "amount": f"{value['amount']:.2f}"}
            for key, value in sorted(by_category.items())
        },
        "duplicate_key_count": sum(1 for count in Counter(str(row.get("idempotency_key") or "") for row in rows).values() if count > 1),
    }


def determine_status(report: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    expected = report.get("queue", {}).get("expected") or {}
    expected_count = expected.get("to_create_count")
    expected_amount = expected.get("amount_total")
    summary = report.get("expected_rows") or {}
    if expected_count is not None and int(expected_count) != int(summary.get("count") or 0):
        reasons.append("expected_count_mismatch")
    if expected_amount is not None and decimal_value(expected_amount) != decimal_value(summary.get("amount_total")):
        reasons.append("expected_amount_mismatch")
    cf_schema = ((report.get("cash_flow_workbook") or {}).get("selected_schema_priority") or [None, None])[1]
    if cf_schema != "dao_eco_template":
        reasons.append("cash_flow_workbook_not_dao_eco_template")
    if int((report.get("cash_flow_workbook") or {}).get("mixed_template_candidate_count") or 0) > 0:
        reasons.append("cash_flow_mixed_template_duplicate_candidates")
    if reasons:
        return "review", reasons

    ledger_missing = int(report.get("ledger_presence", {}).get("missing_key_count") or 0) > 0
    manifest_presence = report.get("created_manifest_presence") or {}
    expected_count_int = int(expected_count or summary.get("count") or 0)
    cf_sync = report.get("cash_flow_sync_evidence") or {}
    if not ledger_missing:
        ledger_labels = report.get("ledger_label_presence") or {}
        if int(ledger_labels.get("mismatch_count") or 0) > 0:
            reasons.append("ledger_category_mismatch")
        if int(manifest_presence.get("matched_key_count") or 0) != expected_count_int:
            reasons.append("created_manifest_missing_expected_keys")
        cf_sync_status = cf_sync.get("status")
        if cf_sync_status != "ok":
            if cf_sync_status == "missing_report":
                reasons.append("cash_flow_sync_report_missing")
            else:
                reasons.append("cash_flow_sync_not_ok")
        if cf_sync.get("missing_month_count"):
            reasons.append("cash_flow_sync_missing_months")
        if cf_sync.get("failed_months"):
            reasons.append("cash_flow_sync_failed_months")
        if cf_sync.get("property_audit_missing_months"):
            reasons.append("cash_flow_sync_property_audit_missing")
        if reasons:
            return "review", reasons

    if ledger_missing:
        reasons.append("pending_import_missing_ledger_keys")
        return "pending_import", reasons
    return "ok", reasons


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Aligned owner-statement downstream coverage")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument("--cf-sync-report", type=Path, default=DEFAULT_CF_SYNC_REPORT)
    parser.add_argument("--ledger-csv", type=Path, action="append", default=[])
    parser.add_argument("--no-default-ledgers", action="store_true")
    args = parser.parse_args()

    queue = read_json(args.queue)
    config = read_json(args.config)
    cf_module = load_cf_module()
    expected_property_id = str((queue.get("expected") or {}).get("baselane_property_id") or "")
    property_config = find_property_config(config, expected_property_id)
    property_gl = None
    for raw_root in property_config.get("search_roots") or []:
        root = Path(str(raw_root))
        short_name = str(property_config.get("property_short") or "")
        candidate = root / f"ECO Systems General Ledger - {short_name}.csv"
        if candidate.is_file():
            property_gl = candidate
            break

    extra_ledgers = list(args.ledger_csv)
    if property_gl:
        extra_ledgers.append(property_gl)
    rows = expected_rows_from_reports(queue)
    expected_tags = expected_tags_by_key(queue, rows)
    keys = {str(row.get("idempotency_key") or "") for row in rows if row.get("idempotency_key")}
    matches, sources = scan_ledgers(keys, ledger_sources(not args.no_default_ledgers, extra_ledgers))
    manifest_record = scan_manifests(keys, args.manifest_dir)
    found_keys = set(matches)
    missing_keys = sorted(keys - found_keys)
    excluded_names = cf_module.load_excluded_property_names()
    property_key = cf_module.normalize_property_name(str(property_config.get("property_full") or queue.get("expected", {}).get("property") or ""))
    standard_cf_excluded = cf_module.is_excluded_property_key(property_key, excluded_names)

    report: dict[str, Any] = {
        "job": "aligned-owner-statement-downstream-validation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "queue_path": str(args.queue),
        "config_path": str(args.config),
        "queue": {
            "queue_id": queue.get("queue_id"),
            "status": queue.get("status"),
            "months": queue.get("months") or [],
            "expected": queue.get("expected") or {},
        },
        "property_config": {
            "property_full": property_config.get("property_full"),
            "property_short": property_config.get("property_short"),
            "baselane_property_id": property_config.get("baselane_property_id"),
            "search_roots": property_config.get("search_roots") or [],
        },
        "expected_row_sources": [str(path) for path in expected_report_paths(queue)],
        "expected_rows": summarize_expected(rows),
        "expected_keys": sorted(keys),
        "expected_tags_by_key": expected_tags,
        "ledger_presence": {
            "ledger_sources": sources,
            "found_key_count": len(found_keys),
            "missing_key_count": len(missing_keys),
            "missing_keys": missing_keys,
            "matches_by_key": {key: matches[key] for key in sorted(matches)},
        },
        "ledger_label_presence": ledger_label_presence(expected_tags, matches),
        "created_manifest_presence": manifest_record,
        "cash_flow_workbook": cf_candidates(property_config, cf_module),
        "cash_flow_sync_scope": {
            "standard_cf_sync_excluded": bool(standard_cf_excluded),
            "explicit_property_scope_can_override_exclusion": bool(
                any(cf_module.property_scope_matches_exclusion(property_key, name) for name in excluded_names)
            ),
            "excluded_name_matches": sorted(
                name for name in excluded_names if cf_module.property_scope_matches_exclusion(property_key, name)
            ),
        },
        "cash_flow_sync_evidence": cash_flow_sync_evidence(queue, property_config, cf_module, args.cf_sync_report),
    }
    status, reasons = determine_status(report)
    report["status"] = status
    report["review_reasons"] = reasons
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if status in {"ok", "pending_import"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
