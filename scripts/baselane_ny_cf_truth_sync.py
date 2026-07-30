#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from baselane_ecogl_data_quality_autonomy import raw_no_dao_mortgage_violation_reason


WORKSPACE_ROOT = Path(__file__).absolute().parents[1]
CF_SCRIPT = WORKSPACE_ROOT / "skills" / "baselane-financials" / "scripts" / "update_cf_statements.py"
DEFAULT_GL = Path("/home/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv")
DEFAULT_REPORTING_LEDGER = WORKSPACE_ROOT / "reports" / "baselane_weekly_accrual_overlay_reporting_ledger.csv"
DEFAULT_REAL_ESTATE = Path("/home/digit/Dropbox/Real Estate")
DEFAULT_REPORT = WORKSPACE_ROOT / "reports" / "cf_statement_sync" / "ny_2026_truth_sync.json"
DEFAULT_MISMATCH_CSV = WORKSPACE_ROOT / "reports" / "cf_statement_sync" / "ny_2026_truth_sync_mismatches.csv"
DEFAULT_MD = WORKSPACE_ROOT / "reports" / "cf_statement_sync" / "ny_2026_truth_sync.md"
DEFAULT_SOURCE_CLEANUP_QUEUE = WORKSPACE_ROOT / "reports" / "baselane_source_cleanup_queue.json"
DEFAULT_SOURCE_CASH_REPORT = WORKSPACE_ROOT / "reports" / "baselane_daily_source_cash_balance_report.json"
SOURCE_CASH_REPORT_MAX_AGE_HOURS = 36


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_cf_module():
    spec = importlib.util.spec_from_file_location("update_cf_statements", CF_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load CF script: {CF_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def direct_ny_cf_files(cf: Any, real_estate_root: Path) -> tuple[list[tuple[str, Path]], dict[str, Any]]:
    ny_base = real_estate_root / "NY"
    items: list[tuple[str, Path]] = []
    duplicate_candidates: dict[str, Any] = {}
    owner_statement_dirs: dict[str, str] = {}
    skipped: dict[str, list[dict[str, str]]] = {}
    property_dir_names = {path.name for path in ny_base.iterdir() if path.is_dir()}
    for prop_dir in sorted(ny_base.iterdir()):
        if not prop_dir.is_dir() or prop_dir.name.startswith("_") or prop_dir.name in {"Public", "reports"}:
            continue
        is_public_dir = prop_dir.name.lower().endswith(" public")
        public_sibling_key = cf.normalize_property_name(prop_dir.name[:-7].rstrip()) if is_public_dir else ""
        private_has_public_sibling = not is_public_dir and any(
            candidate.lower().endswith(" public")
            and cf.normalize_property_name(prop_dir.name).startswith(cf.normalize_property_name(candidate[:-7].rstrip()))
            for candidate in property_dir_names
        )
        if private_has_public_sibling:
            skipped.setdefault(cf.normalize_property_name(prop_dir.name), []).append(
                {"path": str(prop_dir), "reason": "public_sibling_scanned_through_private_root"}
            )
            continue
        key = cf.normalize_property_name(prop_dir.name)
        candidates: list[Path] = []
        for owner_dir in (prop_dir / cf.OWNER_STATEMENTS_DIR, prop_dir / "Public" / cf.OWNER_STATEMENTS_DIR):
            if not owner_dir.is_dir():
                continue
            owner_statement_dirs[key] = str(owner_dir)
            for path in sorted(owner_dir.rglob("Cash Flow Statement*.xlsx")):
                filename = path.name.lower()
                if "conflicted copy" in filename or "conflict" in filename:
                    continue
                if cf.is_legacy_public_finance_path(path):
                    skipped.setdefault(key, []).append({"path": str(path), "reason": "legacy_public_finance_dir_ignored"})
                    continue
                candidates.append(path)
        if not candidates:
            continue
        schema_priorities = {path: cf.cf_workbook_schema_priority(path) for path in candidates} if len(candidates) > 1 else {}
        ranked = sorted(
            candidates,
            key=lambda path: (
                schema_priorities.get(path, (0, "not_needed"))[0],
                cf.cf_candidate_priority_for_property(path, prop_dir.name),
            ),
        )
        selected = ranked[0]
        items.append((key, selected))
        if len(ranked) > 1:
            duplicate_candidates[key] = {
                "selected": str(selected),
                "ignored": [str(path) for path in ranked[1:]],
                "candidate_count": len(ranked),
                "schema_priorities": {str(path): schema_priorities.get(path) for path in ranked},
            }
    return items, {
        "owner_statement_dirs": owner_statement_dirs,
        "duplicate_candidates": duplicate_candidates,
        "skipped": skipped,
    }


def parse_month(value: str) -> tuple[int, int]:
    year, month = value.split("-", 1)
    return int(year), int(month)


def month_range(start_month: str, end_month: str) -> list[tuple[int, int]]:
    start_year, start = parse_month(start_month)
    end_year, end = parse_month(end_month)
    months = []
    year, month = start_year, start
    while (year, month) <= (end_year, end):
        months.append((year, month))
        month += 1
        if month == 13:
            year += 1
            month = 1
    return months


def source_cash_mode_for_month(year: int, month: int, today: date | None = None) -> str:
    current_date = today or date.today()
    return "as_of_month_end" if (year, month) < (current_date.year, current_date.month) else "full_column_e"


def resolve_report_source_path(value: str) -> Path:
    """Resolve report paths without silently substituting a central ledger."""
    path = Path(value)
    if path.is_file():
        return path
    dropbox_prefix = "/home/digit/Dropbox/"
    if value.startswith(dropbox_prefix) and os.path.ismount("/mnt/c"):
        mounted = Path("/mnt/c/Users/digit/Dropbox") / value[len(dropbox_prefix):]
        if mounted.is_file():
            return mounted
    return path


def load_canonical_source_cash_manifest(path: Path) -> dict[str, Any]:
    """Load the fresh Public property-split ECO GL manifest used for CF cash."""
    if not path.is_file():
        return {"status": "missing", "usable": False, "reason": "source_cash_report_missing", "entries": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "unreadable", "usable": False, "reason": "source_cash_report_unreadable", "error": str(exc), "entries": {}}
    generated_at = str(payload.get("generated_at") or "")
    try:
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - generated).total_seconds() / 3600
    except ValueError:
        age_hours = None
    quality_fields = (
        "violation_count",
        "missing_row_count",
        "missing_month_column_count",
        "unreadable_count",
        "noncanonical_source_count",
        "blocking_no_match_count",
        "split_scope_missing_property_count",
    )
    quality_blockers = {field: int(payload.get(field) or 0) for field in quality_fields}
    entries: dict[str, dict[str, Any]] = {}
    rejected: dict[str, str] = {}
    for item in payload.get("checked_workbooks_bounded") or []:
        if not isinstance(item, dict):
            continue
        property_name = str(item.get("property") or "").strip()
        source_mode = str(item.get("source_cash_source_mode") or "")
        source_value = str(item.get("source_cash_source") or "")
        source = resolve_report_source_path(source_value)
        if not property_name:
            continue
        if source_mode != "canonical_property_split_gl":
            rejected[property_name] = "noncanonical_source_mode"
            continue
        if not source.is_file():
            rejected[property_name] = "canonical_source_missing"
            continue
        entries[property_key(property_name)] = {
            **item,
            "source_cash_source": str(source),
        }
    usable = (
        payload.get("status") in {"ok", "review"}
        and age_hours is not None
        and age_hours <= SOURCE_CASH_REPORT_MAX_AGE_HOURS
        and not any(quality_blockers.values())
    )
    reason = "fresh_canonical_property_split_sources" if usable else "source_cash_report_not_usable"
    if age_hours is None:
        reason = "source_cash_report_timestamp_invalid"
    elif age_hours > SOURCE_CASH_REPORT_MAX_AGE_HOURS:
        reason = "source_cash_report_stale"
    return {
        "status": payload.get("status", "missing"),
        "usable": usable,
        "reason": reason,
        "generated_at": generated_at,
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "quality_blockers": quality_blockers,
        "entry_count": len(entries),
        "rejected": rejected,
        "entries": entries,
    }


def canonical_source_entry(property_name: str, manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Match a property only to its verified canonical property-split ledger."""
    entries = manifest.get("entries") or {}
    exact = entries.get(property_key(property_name))
    if exact:
        return exact
    property_tokens = set(property_key(property_name).split())
    property_number = next((token for token in property_tokens if token.isdigit()), None)
    matches: list[tuple[int, str, dict[str, Any]]] = []
    for entry_key, entry in entries.items():
        entry_tokens = set(entry_key.split())
        entry_number = next((token for token in entry_tokens if token.isdigit()), None)
        if property_number and entry_number and property_number != entry_number:
            continue
        shared = len(property_tokens & entry_tokens)
        if shared >= min(3, len(property_tokens), len(entry_tokens)):
            matches.append((shared, entry_key, entry))
    if len(matches) == 1:
        return matches[0][2]
    return None


def property_key(value: str) -> str:
    return " ".join(value.strip().lower().split())


def row_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(field) or "") for field in ("Merchant", "Description", "Type", "Category", "Sub-category", "Notes"))


def is_overlay_accrual(row: dict[str, Any]) -> bool:
    return "ecogl_accrual_overlay_id=" in str(row.get("Notes") or "") or str(row.get("Type") or "").strip() == "Accrual Overlay"


def is_pm_fee(row: dict[str, Any]) -> bool:
    text = row_text(row).lower()
    category = str(row.get("_cf_category") or row.get("Category") or "").lower()
    return "management fee" in text or "property management" in text or "pm fee" in text or category in {"management fees", "property management"}


def is_dao_llc_fee(row: dict[str, Any]) -> bool:
    category = str(row.get("_cf_category") or row.get("Category") or "").lower()
    export_category = str(row.get("Category") or "").lower()
    notes = " ".join(str(row.get(field) or "") for field in ("Notes", "Category", "Sub-category")).lower()
    has_fee_language = "dao llc fee" in notes or "dao llc fees" in notes or "annual dao" in notes or "llc fee" in notes
    is_operating_fee_category = (
        category in {"accounting & tax fees", "legal & other professional fees"}
        or export_category.startswith("operating expenses")
    )
    return has_fee_language and is_operating_fee_category


def sum_rows(rows: list[dict[str, Any]], predicate) -> float:
    return round(sum(float(row.get("_amount") or 0.0) for row in rows if predicate(row)), 2)


def build_cash_truth_rows(
    cf: Any,
    reporting_transactions: list[dict[str, Any]],
    raw_transactions: list[dict[str, Any]],
    properties: list[tuple[str, str]],
    year: int,
    month: int,
    canonical_source_rows: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for property_key, gl_property in properties:
        reporting_property_rows = cf.filter_by_property(reporting_transactions, gl_property)
        raw_property_rows = cf.filter_by_property(raw_transactions, gl_property)
        reporting_balance = cf.source_cash_balance(reporting_property_rows, year, month)
        raw_balance = cf.source_cash_balance(raw_property_rows, year, month)
        split_rows = (canonical_source_rows or {}).get(property_key)
        split_balance = cf.source_cash_balance(split_rows, year, month) if split_rows is not None else None
        reporting_through_month = [
            row
            for row in reporting_property_rows
            if row.get("_date") and (row["_date"].year, row["_date"].month) <= (year, month)
        ]
        raw_through_month = [
            row
            for row in raw_property_rows
            if row.get("_date") and (row["_date"].year, row["_date"].month) <= (year, month)
        ]
        rows.append(
            {
                "property_key": property_key,
                "matched_gl_property": gl_property,
                "as_of_month": f"{year:04d}-{month:02d}",
                "eco_operating_cash": split_balance["expected"] if split_balance else None,
                "eco_operating_cash_source": "canonical_property_split_gl_full_column_e_all_rows" if split_balance else None,
                "eco_operating_cash_property_split": split_balance["expected"] if split_balance else None,
                "eco_operating_cash_reporting_ledger": reporting_balance["expected"],
                "eco_operating_cash_canonical": reporting_balance["expected"],
                "eco_operating_cash_raw": raw_balance["expected"],
                "property_split_minus_reporting": (
                    round(split_balance["expected"] - reporting_balance["expected"], 2)
                    if split_balance
                    else None
                ),
                "canonical_minus_raw": round(reporting_balance["expected"] - raw_balance["expected"], 2),
                "pm_fees_ytd": sum_rows(reporting_through_month, is_pm_fee),
                "dao_llc_fees_ytd": sum_rows(reporting_through_month, is_dao_llc_fee),
                "accrual_overlay_ytd": sum_rows(reporting_through_month, is_overlay_accrual),
                "raw_transaction_count_full": raw_balance["included_count"],
                "canonical_transaction_count_full": reporting_balance["included_count"],
                "raw_transaction_count_ytd": len(raw_through_month),
                "canonical_transaction_count_ytd": len(reporting_through_month),
                "excluded_earldao_interest_total": reporting_balance["excluded_earldao_interest_total"],
            }
        )
    return rows


def transaction_fingerprint(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(row.get(field) or "")
        for field in (
            "Date",
            "Amount",
            "Account",
            "Merchant",
            "Description",
            "Property",
            "Type",
            "Category",
            "Sub-category",
            "Notes",
        )
    )


def rows_through_cash_month(cf: Any, rows: list[dict[str, Any]], property_name: str, year: int, month: int) -> list[dict[str, Any]]:
    scoped = cf.filter_by_property(rows, property_name)
    result = []
    for row in scoped:
        row_date = row.get("_date")
        if not row_date or (row_date.year, row_date.month) > (year, month):
            continue
        if cf.is_earldao_interest_transaction(row):
            continue
        result.append(row)
    return result


def bounded_row(row: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "date": row.get("Date") or "",
        "amount": row.get("Amount") or "",
        "property": row.get("Property") or "",
        "merchant": row.get("Merchant") or "",
        "description": row.get("Description") or "",
        "type": row.get("Type") or "",
        "category": row.get("Category") or "",
        "sub_category": row.get("Sub-category") or "",
        "notes": row.get("Notes") or "",
        "reason": reason,
    }


def build_cash_reconciliation_rows(
    cf: Any,
    reporting_transactions: list[dict[str, Any]],
    raw_transactions: list[dict[str, Any]],
    properties: list[tuple[str, str]],
    year: int,
    month: int,
    limit: int = 12,
) -> list[dict[str, Any]]:
    rows = []
    for property_key, gl_property in properties:
        raw_rows = rows_through_cash_month(cf, raw_transactions, gl_property, year, month)
        reporting_rows = rows_through_cash_month(cf, reporting_transactions, gl_property, year, month)
        reporting_counter = Counter(transaction_fingerprint(row) for row in reporting_rows)
        raw_missing: list[dict[str, Any]] = []
        for row in raw_rows:
            fingerprint = transaction_fingerprint(row)
            if reporting_counter[fingerprint] > 0:
                reporting_counter[fingerprint] -= 1
            else:
                raw_missing.append(row)
        raw_counter = Counter(transaction_fingerprint(row) for row in raw_rows)
        reporting_added: list[dict[str, Any]] = []
        for row in reporting_rows:
            fingerprint = transaction_fingerprint(row)
            if raw_counter[fingerprint] > 0:
                raw_counter[fingerprint] -= 1
            else:
                reporting_added.append(row)
        missing_total = round(sum(float(row.get("_amount") or 0.0) for row in raw_missing), 2)
        added_total = round(sum(float(row.get("_amount") or 0.0) for row in reporting_added), 2)
        no_dao_mortgage_rows = [
            row for row in raw_missing if raw_no_dao_mortgage_violation_reason(row)
        ]
        duplicate_or_reporting_clean_rows = [
            row for row in raw_missing if not raw_no_dao_mortgage_violation_reason(row)
        ]
        deterministic_reporting_cleanup = bool(raw_missing) and not reporting_added
        source_clean_for_transfer = not no_dao_mortgage_rows and not reporting_added
        reporting_ready_for_cf = not reporting_added
        reasons = []
        if no_dao_mortgage_rows:
            reasons.append("raw_no_dao_mortgage_rows_present")
        if duplicate_or_reporting_clean_rows:
            reasons.append("raw_rows_removed_by_reporting_cleanup_nonblocking_for_full_column_e_cash")
        if reporting_added:
            reasons.append("reporting_rows_not_in_raw_export")
        rows.append(
            {
                "property_key": property_key,
                "matched_gl_property": gl_property,
                "source_clean_for_transfer": source_clean_for_transfer,
                "reporting_ready_for_cf": reporting_ready_for_cf,
                "deterministic_reporting_cleanup": deterministic_reporting_cleanup,
                "reason": "ok" if source_clean_for_transfer else ";".join(reasons),
                "raw_missing_from_reporting_count": len(raw_missing),
                "raw_missing_from_reporting_total": missing_total,
                "reporting_added_count": len(reporting_added),
                "reporting_added_total": added_total,
                "no_dao_mortgage_missing_count": len(no_dao_mortgage_rows),
                "raw_cleanup_missing_count": len(duplicate_or_reporting_clean_rows),
                "nonblocking_reporting_difference_count": len(duplicate_or_reporting_clean_rows),
                "raw_missing_bounded": [
                    bounded_row(
                        row,
                        raw_no_dao_mortgage_violation_reason(row) or "raw row removed by canonical reporting cleanup",
                    )
                    for row in raw_missing[:limit]
                ],
                "reporting_added_bounded": [
                    bounded_row(row, "canonical reporting row not present in raw export")
                    for row in reporting_added[:limit]
                ],
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# NY CF Truth Sync",
        "",
        f"- Status: `{report['status']}`",
        f"- Mode: `{report['mode']}`",
        f"- Reporting ledger: `{report['reporting_ledger']}`",
        f"- Raw GL: `{report['raw_gl_csv']}`",
        f"- Month range: `{report['start_month']}` to `{report['end_month']}`",
        f"- ECO cash as of: `{report['cash_as_of_month']}`",
        f"- NY workbooks: `{report['ny_cf_workbook_count']}`",
        f"- Month checks: `{report['month_check_count']}`",
        f"- Workbook changes: `{report['change_count']}`",
        f"- Formula overwrites: `{report['overwrite_formula_count']}`",
        f"- Post-sync mismatches: `{report['post_sync_mismatch_count']}`",
        f"- Cash transfer ready: `{str(report['cash_transfer_ready']).lower()}`",
        f"- CF reporting ready: `{str(report['cash_reporting_ready']).lower()}`",
        f"- Cash source issue count: `{report['cash_source_issue_count']}`",
        f"- Source cleanup queue actions: `{report.get('source_cleanup_queue_action_count', 0)}`",
        "",
        "## ECO Operating Cash",
    ]
    for row in report.get("cash_truth_rows", []):
        eco_cash = row.get("eco_operating_cash")
        eco_cash_text = "UNAVAILABLE" if eco_cash is None else f"${eco_cash:,.2f}"
        lines.append(
            f"- `{row['matched_gl_property']}`: ECO Operating Cash `{eco_cash_text}` from the full Column E ledger; "
            f"reporting-ledger comparison `${row['eco_operating_cash_canonical']:,.2f}`; "
            f"split/reporting delta `{row.get('property_split_minus_reporting')}`; "
            f"PM/DAO/accrual YTD `${row['pm_fees_ytd']:,.2f}` / `${row['dao_llc_fees_ytd']:,.2f}` / `${row['accrual_overlay_ytd']:,.2f}`"
        )
    if report.get("cash_source_issue_count"):
        lines.extend(["", "## Cash Source Issues"])
        for row in report.get("cash_reconciliation_rows", []):
            if row.get("source_clean_for_transfer"):
                continue
            lines.append(
                f"- `{row['matched_gl_property']}`: `{row['reason']}`; "
                f"CF reporting ready `{str(row.get('reporting_ready_for_cf')).lower()}`; "
                f"raw missing `{row['raw_missing_from_reporting_count']}` / `${row['raw_missing_from_reporting_total']:,.2f}`; "
                f"reporting added `{row['reporting_added_count']}` / `${row['reporting_added_total']:,.2f}`"
            )
    if report.get("post_sync_mismatch_count"):
        lines.extend(["", "## Remaining Mismatch Types"])
        for key, count in sorted((report.get("post_sync_mismatch_count_by_type") or {}).items()):
            lines.append(f"- `{key}`: {count}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync NY 2026 CF statements from canonical reporting ledger and report ECO operating cash truth.")
    parser.add_argument("--reporting-ledger", type=Path, default=DEFAULT_REPORTING_LEDGER)
    parser.add_argument("--raw-gl-csv", type=Path, default=DEFAULT_GL)
    parser.add_argument("--real-estate-root", type=Path, default=DEFAULT_REAL_ESTATE)
    parser.add_argument("--start-month", default="2026-01")
    parser.add_argument("--end-month", default=f"{date.today().year:04d}-{date.today().month:02d}")
    parser.add_argument("--cash-as-of-month", default=None, help="Report ECO operating cash through this month; defaults to --end-month.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--mismatches-csv", type=Path, default=DEFAULT_MISMATCH_CSV)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MD)
    parser.add_argument("--source-cleanup-queue", type=Path, default=DEFAULT_SOURCE_CLEANUP_QUEUE)
    parser.add_argument("--source-cash-report", type=Path, default=DEFAULT_SOURCE_CASH_REPORT)
    parser.add_argument("--cash-only", action="store_true", help="Skip workbook discovery/update/audit and only emit ECO cash reconciliation.")
    parser.add_argument("--cash-property", action="append", default=None, help="Restrict --cash-only reconciliation to one or more GL property names.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cf = load_cf_module()
    cf.REAL_ESTATE_BASE = args.real_estate_root
    reporting_ledger = args.reporting_ledger if args.reporting_ledger.is_file() else args.raw_gl_csv
    reporting_transactions = cf.load_gl_data(reporting_ledger)
    raw_transactions = cf.load_gl_data(args.raw_gl_csv)
    source_cash_manifest = load_canonical_source_cash_manifest(args.source_cash_report)
    gl_properties = {row["_property"] for row in reporting_transactions if row.get("_property")}
    if args.cash_only:
        wanted = {property_key(item) for item in (args.cash_property or [])}
        matched_properties = [
            (property_key(prop), prop)
            for prop in sorted(gl_properties)
            if not wanted or property_key(prop) in wanted
        ]
        ny_items: list[tuple[str, Path]] = []
        discovery = {"cash_only": True, "owner_statement_dirs": {}, "duplicate_candidates": {}, "skipped": {}}
    else:
        ny_items, discovery = direct_ny_cf_files(cf, args.real_estate_root)
        matched_properties = []
    months = month_range(args.start_month, args.end_month)

    results: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    mismatch_by_type: Counter[str] = Counter()
    update_properties: set[str] = set()
    source_cash_mode_counts: Counter[str] = Counter()
    canonical_source_rows: dict[str, list[dict[str, Any]]] = {}
    canonical_source_missing: list[str] = []

    for workbook_property_key, workbook_path in ny_items:
        matched_gl = cf.match_gl_property(workbook_property_key, gl_properties) or cf.match_gl_property(cf.property_name_from_cf_file(workbook_path), gl_properties)
        if not matched_gl:
            mismatches.append(
                {
                    "property_key": workbook_property_key,
                    "matched_gl_property": "",
                    "year_month": "",
                    "file": str(workbook_path),
                    "row": "",
                    "label": "",
                    "cf_value": "",
                    "gl_total": "",
                    "diff": "",
                    "type": "no_gl_property_match",
                    "action": "fix_property_matching_or_source_gl",
                }
            )
            mismatch_by_type["no_gl_property_match"] += 1
            continue
        matched_properties.append((workbook_property_key, matched_gl))
        property_rows = cf.filter_by_property(reporting_transactions, matched_gl)
        source_entry = canonical_source_entry(workbook_property_key, source_cash_manifest) if source_cash_manifest.get("usable") else None
        if source_entry is None:
            canonical_source_missing.append(workbook_property_key)
            mismatch_by_type["canonical_property_split_source_missing"] += 1
            mismatches.append(
                {
                    "property_key": workbook_property_key,
                    "matched_gl_property": matched_gl,
                    "year_month": "",
                    "file": str(workbook_path),
                    "row": "",
                    "label": "ECO Operating Cash",
                    "cf_value": "",
                    "gl_total": "",
                    "diff": "",
                    "type": "canonical_property_split_source_missing",
                    "action": "do_not_fallback_to_raw_or_central_ledger",
                }
            )
            continue
        source_rows = cf.load_gl_data(Path(str(source_entry["source_cash_source"])))
        canonical_source_rows[workbook_property_key] = source_rows
        for year, month in months:
            month_rows = cf.filter_by_month(property_rows, year, month)
            source_cash_mode = source_cash_mode_for_month(year, month)
            source_cash_mode_counts[source_cash_mode] += 1
            before = cf.update_xlsx(
                workbook_path,
                matched_gl,
                month_rows,
                year,
                month,
                dry_run=args.dry_run,
                source_cash_data=source_rows,
                source_cash_mode=source_cash_mode,
            )
            for change in before:
                record = {
                    "property_key": workbook_property_key,
                    "matched_gl_property": matched_gl,
                    "year_month": f"{year:04d}-{month:02d}",
                    "file": str(workbook_path),
                    **change,
                }
                changes.append(record)
                if "error" not in change:
                    update_properties.add(workbook_property_key)
            audit = cf.audit_xlsx(
                workbook_path,
                matched_gl,
                month_rows,
                year,
                month,
                source_cash_data=source_rows,
                source_cash_mode=source_cash_mode,
            )
            results.append(
                {
                    "property_key": workbook_property_key,
                    "matched_gl_property": matched_gl,
                    "year_month": f"{year:04d}-{month:02d}",
                    "file": str(workbook_path),
                    "gl_transaction_count": len(month_rows),
                    "summary": audit.get("summary") or {},
                    "error": audit.get("error"),
                    "conflict_count": len(audit.get("conflicts") or []),
                }
            )
            if audit.get("error"):
                mismatch_by_type["audit_error"] += 1
                mismatches.append(
                    {
                        "property_key": workbook_property_key,
                        "matched_gl_property": matched_gl,
                        "year_month": f"{year:04d}-{month:02d}",
                        "file": str(workbook_path),
                        "row": "",
                        "label": "",
                        "cf_value": "",
                        "gl_total": "",
                        "diff": "",
                        "type": "audit_error",
                        "action": audit.get("error"),
                    }
                )
            for conflict in audit.get("conflicts") or []:
                conflict_type = conflict.get("type") or "unknown"
                mismatch_by_type[conflict_type] += 1
                mismatches.append(
                    {
                        "property_key": workbook_property_key,
                        "matched_gl_property": matched_gl,
                        "year_month": f"{year:04d}-{month:02d}",
                        "file": str(workbook_path),
                        "row": conflict.get("row", ""),
                        "label": conflict.get("label", ""),
                        "cf_value": conflict.get("cf_value", ""),
                        "gl_total": conflict.get("gl_total", ""),
                        "diff": conflict.get("diff", ""),
                        "type": conflict_type,
                        "action": conflict.get("action", ""),
                    }
                )

    cash_as_of_month = args.cash_as_of_month or args.end_month
    cash_year, cash_month_number = parse_month(cash_as_of_month)
    cash_reconciliation_rows = build_cash_reconciliation_rows(
        cf,
        reporting_transactions,
        raw_transactions,
        matched_properties,
        cash_year,
        cash_month_number,
    )
    canonical_source_missing_set = set(canonical_source_missing)
    for property_key_value, gl_property in matched_properties:
        if property_key_value in canonical_source_rows:
            continue
        source_entry = canonical_source_entry(property_key_value, source_cash_manifest) if source_cash_manifest.get("usable") else None
        if source_entry is not None:
            canonical_source_rows[property_key_value] = cf.load_gl_data(Path(str(source_entry["source_cash_source"])))
            continue
        canonical_source_missing_set.add(property_key_value)
    canonical_source_missing = sorted(canonical_source_missing_set)
    cash_truth_rows = build_cash_truth_rows(
        cf,
        reporting_transactions,
        raw_transactions,
        matched_properties,
        cash_year,
        cash_month_number,
        canonical_source_rows,
    )
    cash_source_issue_count = sum(1 for row in cash_reconciliation_rows if not row["source_clean_for_transfer"])
    canonical_source_issue_count = len(canonical_source_missing) + (0 if source_cash_manifest.get("usable") else 1)
    cash_transfer_ready = cash_source_issue_count == 0 and canonical_source_issue_count == 0
    cash_reporting_ready = not mismatches and all(row.get("reporting_ready_for_cf") for row in cash_reconciliation_rows)
    source_cleanup_queue: dict[str, Any] = {"status": "missing", "action_count": 0, "missing_id_count": 0}
    if args.source_cleanup_queue.is_file():
        try:
            queue_data = json.loads(args.source_cleanup_queue.read_text(encoding="utf-8"))
            if isinstance(queue_data, dict):
                source_cleanup_queue = queue_data
        except Exception as exc:  # noqa: BLE001
            source_cleanup_queue = {"status": "unreadable", "error": str(exc), "action_count": 0, "missing_id_count": 0}
    status = "ok"
    if mismatches:
        status = "review"
    if not cash_transfer_ready:
        status = "blocked_source_not_clean"
    report = {
        "generated_at": iso_z(),
        "status": status,
        "mode": "dry_run" if args.dry_run else "apply",
        "reporting_ledger": str(reporting_ledger),
        "raw_gl_csv": str(args.raw_gl_csv),
        "real_estate_root": str(args.real_estate_root),
        "source_cash_report": str(args.source_cash_report),
        "source_cash_manifest": {key: value for key, value in source_cash_manifest.items() if key != "entries"},
        "canonical_source_missing_properties": sorted(canonical_source_missing),
        "start_month": args.start_month,
        "end_month": args.end_month,
        "cash_as_of_month": cash_as_of_month,
        "source_cash_balance_policy": {
            "closed_month_mode": "as_of_month_end",
            "active_month_mode": "full_column_e",
            "mode_counts": dict(sorted(source_cash_mode_counts.items())),
        },
        "ny_cf_workbook_count": len(ny_items),
        "ny_cf_workbooks": [{"property_key": key, "file": str(path)} for key, path in ny_items],
        "month_check_count": len(results),
        "change_count": len([change for change in changes if "error" not in change]),
        "overwrite_formula_count": sum(1 for change in changes if change.get("action") == "overwrite_formula"),
        "source_cash_update_count": sum(1 for change in changes if change.get("action") == "set_source_cash_balance"),
        "updated_property_count": len(update_properties),
        "updated_properties": sorted(update_properties),
        "post_sync_mismatch_count": len(mismatches),
        "post_sync_mismatch_count_by_type": dict(sorted(mismatch_by_type.items())),
        "cash_transfer_ready": cash_transfer_ready,
        "cash_reporting_ready": cash_reporting_ready,
        "cash_source_issue_count": cash_source_issue_count,
        "canonical_source_issue_count": canonical_source_issue_count,
        "source_cleanup_queue": {
            "path": str(args.source_cleanup_queue),
            "status": source_cleanup_queue.get("status"),
            "action_count": source_cleanup_queue.get("action_count", 0),
            "missing_id_count": source_cleanup_queue.get("missing_id_count", 0),
            "action_counts": source_cleanup_queue.get("action_counts", {}),
        },
        "source_cleanup_queue_action_count": source_cleanup_queue.get("action_count", 0),
        "source_cleanup_queue_missing_id_count": source_cleanup_queue.get("missing_id_count", 0),
        "cash_truth_rows": cash_truth_rows,
        "cash_reconciliation_rows": cash_reconciliation_rows,
        "changes": changes,
        "results": results,
        "mismatches": mismatches,
        "discovery": discovery,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(
        args.mismatches_csv,
        mismatches,
        ["property_key", "matched_gl_property", "year_month", "file", "row", "label", "cf_value", "gl_total", "diff", "type", "action"],
    )
    write_markdown(args.markdown, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "mode": report["mode"],
                "ny_cf_workbook_count": report["ny_cf_workbook_count"],
                "month_check_count": report["month_check_count"],
                "change_count": report["change_count"],
                "overwrite_formula_count": report["overwrite_formula_count"],
                "post_sync_mismatch_count": report["post_sync_mismatch_count"],
                "report": str(args.report),
                "markdown": str(args.markdown),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
