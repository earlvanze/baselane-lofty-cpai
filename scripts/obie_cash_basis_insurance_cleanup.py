#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from baselane_monthly_accruals_idempotent import (
    ACCRUAL_TEMPLATES,
    CASH_BASIS_INSURANCE_STATES,
    NO_FIXED_ACCRUAL_TEMPLATE_REQUIRED,
    PM_FEE_PROPERTIES,
    PROPERTY_ALIASES,
    canonical_accrual_property_name,
    is_cash_basis_insurance_accrual,
    iso_z,
    normalize_schedule_address,
    parse_marker,
    property_state,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GL = Path("/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv")
DEFAULT_REAL_ESTATE_ROOT = Path("/mnt/c/Users/digit/Dropbox/Real Estate")
DEFAULT_SOURCE_INDEX = ROOT / "reports" / "baselane_source_transaction_index.csv"
DEFAULT_OBIE_DIR = Path("/mnt/c/Users/digit/Dropbox/Real Estate/Lofty PM/Obie")
DEFAULT_REPORT_JSON = ROOT / "reports" / "obie_cash_basis_insurance_cleanup.json"
DEFAULT_REPORT_MD = DEFAULT_OBIE_DIR / "OH-IL-TN Cash-Basis Insurance Duplicate Audit.md"
DEFAULT_REPORT_CSV = DEFAULT_OBIE_DIR / "OH-IL-TN Cash-Basis Insurance Duplicate Audit.csv"
GRAPHQL_HELPER = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [{key: str(value or "") for key, value in row.items()} for row in reader]


def discover_property_split_gls(real_estate_root: Path, states: set[str]) -> list[Path]:
    paths: list[Path] = []
    if not real_estate_root.is_dir():
        return paths
    target_directory = "07 - P&L & Owner Statements"
    for state in sorted(states):
        state_root = real_estate_root / state
        if not state_root.is_dir():
            continue
        try:
            property_roots = [path for path in state_root.iterdir() if path.is_dir()]
        except OSError:
            continue
        for property_root in property_roots:
            # These are the only supported canonical layouts. Avoid recursive
            # walks so Dropbox-backed trees cannot pull archived artifacts or
            # consume unbounded resources during a scheduled cleanup.
            ledger_directories = (
                property_root / "Public" / target_directory,
                property_root / target_directory,
            )
            for ledger_directory in ledger_directories:
                if not ledger_directory.is_dir():
                    continue
                paths.extend(
                    path
                    for path in ledger_directory.glob("ECO Systems General Ledger*.csv")
                    if path.is_file()
                )
    return sorted(dict.fromkeys(paths), key=lambda path: str(path).lower())


def cleanup_targets(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] = []
    if args.gl_csv:
        paths.append(args.gl_csv)
    elif DEFAULT_GL.is_file():
        paths.append(DEFAULT_GL)
    if not args.skip_property_split_gls:
        paths.extend(discover_property_split_gls(args.real_estate_root, CASH_BASIS_INSURANCE_STATES))
    return sorted(dict.fromkeys(paths), key=lambda path: str(path).lower())


def file_contains_insurance_accrual(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if b"Insurance Accrual" in chunk:
                    return True
    except OSError:
        return False
    return False


def write_csv_atomic(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
    tmp.replace(path)


def amount(value: object) -> float:
    try:
        return round(float(str(value or "0").replace("$", "").replace(",", "").strip() or "0"), 2)
    except ValueError:
        return 0.0


def parse_date(value: str) -> dt.date | None:
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%B %d, %Y", "%B %d %Y"):
        try:
            return dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def month_key(row: dict[str, str]) -> str:
    row_date = parse_date(row.get("Date", ""))
    return f"{row_date.year:04d}-{row_date.month:02d}" if row_date else ""


def row_text(row: dict[str, str], fields: tuple[str, ...] = ("Merchant", "Description", "Property", "Notes")) -> str:
    return " ".join(str(row.get(field) or "") for field in fields)


def source_row_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        normalize_schedule_address(row.get("Date", "")),
        normalize_schedule_address(row.get("Merchant", "")),
        normalize_schedule_address(row.get("Description", "")),
        f"{amount(row.get('Amount')):.2f}",
        normalize_schedule_address(row.get("Property", "")),
    )


def known_property_candidates() -> dict[str, str]:
    candidates: dict[str, str] = {}

    def add(name: str) -> None:
        state = property_state(name)
        if state in CASH_BASIS_INSURANCE_STATES:
            candidates[normalize_schedule_address(name)] = name
            short_name = name.split(",", 1)[0].strip()
            if short_name:
                candidates[normalize_schedule_address(short_name)] = name

    for template in ACCRUAL_TEMPLATES:
        add(str(template.get("property") or ""))
    for name, _rate, _prefix in PM_FEE_PROPERTIES:
        add(str(name))
    for name in NO_FIXED_ACCRUAL_TEMPLATE_REQUIRED:
        add(str(name))
    for canonical, aliases in PROPERTY_ALIASES.items():
        add(canonical)
        for alias in aliases:
            if property_state(canonical) in CASH_BASIS_INSURANCE_STATES:
                candidates[normalize_schedule_address(alias)] = canonical
    return candidates


KNOWN_PROPERTIES = known_property_candidates()


def canonical_property(value: str) -> str:
    normalized = normalize_schedule_address(value)
    if not normalized:
        return ""
    if normalized in KNOWN_PROPERTIES:
        return KNOWN_PROPERTIES[normalized]
    matches = [
        canonical
        for key, canonical in KNOWN_PROPERTIES.items()
        if key and (key in normalized or normalized in key)
    ]
    return sorted(matches, key=len)[0] if matches else value.strip()


def scoped_property(row: dict[str, str]) -> tuple[str, str]:
    marker = parse_marker(str(row.get("Notes") or ""))
    if marker:
        canonical = canonical_accrual_property_name(marker["property"])
    else:
        canonical = canonical_property(str(row.get("Property") or ""))
    state = property_state(canonical)
    return canonical, state


def is_scoped_insurance_accrual(row: dict[str, str]) -> bool:
    marker = parse_marker(str(row.get("Notes") or ""))
    if marker:
        return is_cash_basis_insurance_accrual(canonical_accrual_property_name(marker["property"]), marker["kind"])
    canonical, state = scoped_property(row)
    text = row_text(row, ("Merchant", "Description", "Category", "Sub-category", "Notes")).lower()
    return state in CASH_BASIS_INSURANCE_STATES and "insurance accrual" in text and "rental dwelling" in text


def is_osc_risk_secure(row: dict[str, str]) -> bool:
    return bool(re.search(r"\bOSC\s*-?\s*RISK\s*SECURE\b", row_text(row, ("Merchant", "Description")), re.IGNORECASE))


def card_suffix(row: dict[str, str]) -> str:
    match = re.search(r"\*\*(\d{4})", row_text(row, ("Description", "Merchant")))
    return match.group(1) if match else ""


def payment_record(csv_row: int, row: dict[str, str]) -> dict[str, Any]:
    canonical, state = scoped_property(row)
    return {
        "csv_row": csv_row,
        "date": row.get("Date", ""),
        "month": month_key(row),
        "property": canonical,
        "state": state,
        "amount": amount(row.get("Amount")),
        "card_suffix": card_suffix(row),
        "merchant": row.get("Merchant", ""),
        "description": row.get("Description", ""),
        "notes": row.get("Notes", ""),
    }


def accrual_record(csv_row: int, row: dict[str, str], source_matches: list[dict[str, str]]) -> dict[str, Any]:
    canonical, state = scoped_property(row)
    marker = parse_marker(str(row.get("Notes") or ""))
    return {
        "csv_row": csv_row,
        "date": row.get("Date", ""),
        "month": marker["month"] if marker else month_key(row),
        "property": canonical,
        "state": state,
        "amount": amount(row.get("Amount")),
        "merchant": row.get("Merchant", ""),
        "notes": row.get("Notes", ""),
        "baselane_ids": [item.get("BaselaneId", "") for item in source_matches if item.get("BaselaneId", "")],
    }


def source_matches_for(row: dict[str, str], source_by_key: dict[tuple[str, str, str, str, str], list[dict[str, str]]]) -> list[dict[str, str]]:
    key = source_row_key(row)
    return source_by_key.get(key, [])


def marker_key_from_row(row: dict[str, str]) -> str:
    marker = parse_marker(str(row.get("Notes") or ""))
    if not marker:
        return ""
    return "|".join([marker["prefix"], marker["kind"], canonical_accrual_property_name(marker["property"]), marker["month"], marker["amount"]])


def source_matches_for_accrual(
    row: dict[str, str],
    source_by_key: dict[tuple[str, str, str, str, str], list[dict[str, str]]],
    source_by_marker: dict[str, list[dict[str, str]]],
) -> list[dict[str, str]]:
    marker_key = marker_key_from_row(row)
    if marker_key and marker_key in source_by_marker:
        return source_by_marker[marker_key]
    return source_matches_for(row, source_by_key)


def duplicate_flags(payments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    month_amount_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    month_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in payments:
        groups[(row["property"], row["date"], row["amount"], row["card_suffix"])].append(row)
        month_amount_groups[(row["property"], row["month"], row["amount"], row["card_suffix"])].append(row)
        month_groups[(row["property"], row["month"])].append(row)

    flags: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for key, rows in groups.items():
        if len(rows) > 1:
            seen.add(("exact", *key))
            flags.append({"severity": "high", "kind": "exact_duplicate", "property": key[0], "month": rows[0]["month"], "amount": key[2], "card_suffix": key[3], "rows": rows, "recommendation": "Do not assume the next month should be skipped until bank settlement/refund is confirmed; this exact duplicate needs review."})
    for key, rows in month_amount_groups.items():
        dates = sorted({row["date"] for row in rows})
        if len(rows) > 1 and len(dates) > 1 and ("exact", key[0], dates[0], key[2], key[3]) not in seen:
            flags.append({"severity": "medium", "kind": "repeated_amount_same_month", "property": key[0], "month": key[1], "amount": key[2], "card_suffix": key[3], "rows": rows, "recommendation": "Possible retry/duplicate billing in the same month; verify before skipping any future month."})
    for key, rows in month_groups.items():
        if len(rows) > 1:
            exact_already_flagged = any(flag["property"] == key[0] and flag["month"] == key[1] for flag in flags)
            if not exact_already_flagged:
                flags.append({"severity": "low", "kind": "multi_payment_month", "property": key[0], "month": key[1], "amount": round(sum(row["amount"] for row in rows), 2), "card_suffix": "", "rows": rows, "recommendation": "Multiple OSC Risk Secure charges found in one month; likely multi-policy/catch-up unless amounts/cards duplicate."})
    return sorted(flags, key=lambda item: (SEVERITY_RANK.get(str(item["severity"]), 9), item["property"], item["month"]))


def next_month(month: str) -> str:
    try:
        year, month_num = (int(part) for part in month.split("-", 1))
    except ValueError:
        return ""
    if month_num == 12:
        return f"{year + 1}-01"
    return f"{year:04d}-{month_num + 1:02d}"


def skip_review_candidates(flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for flag in flags:
        if flag.get("severity") not in {"high", "medium"}:
            continue
        candidate_month = next_month(str(flag.get("month") or ""))
        action = (
            "Review Obie/OSC settlement. If no refund/credit exists and the duplicate was a true prepayment, "
            f"skip or credit {candidate_month}; otherwise do not skip and request refund/credit."
            if candidate_month
            else "Review Obie/OSC settlement before skipping any month."
        )
        candidates.append(
            {
                "property": flag.get("property", ""),
                "duplicate_month": flag.get("month", ""),
                "candidate_skip_or_credit_month": candidate_month,
                "severity": flag.get("severity", ""),
                "kind": flag.get("kind", ""),
                "duplicate_amount": flag.get("amount", 0),
                "card_suffix": flag.get("card_suffix", ""),
                "row_count": len(flag.get("rows") or []),
                "action": action,
            }
        )
    return candidates


def write_duplicate_csv(path: Path, report: dict[str, Any]) -> None:
    fieldnames = [
        "severity",
        "kind",
        "property",
        "duplicate_month",
        "candidate_skip_or_credit_month",
        "duplicate_amount",
        "card_suffix",
        "row_count",
        "action",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in report.get("skip_review_candidates") or []:
            writer.writerow({field: item.get(field, "") for field in fieldnames})


def run_graphql(payload: dict[str, Any]) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        payload_path = Path(handle.name)
    try:
        proc = subprocess.run(
            ["node", str(GRAPHQL_HELPER), str(payload_path)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
            timeout=120,
        )
    finally:
        payload_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"GraphQL helper rc={proc.returncode}")
    result = json.loads(proc.stdout)
    if result.get("errors"):
        raise RuntimeError(json.dumps(result["errors"], indent=2))
    return result


def delete_live_transactions(baselane_ids: list[str]) -> dict[str, Any]:
    if not baselane_ids:
        return {"status": "skipped", "reason": "no_baselane_ids", "deleted_count": 0}
    payload = {
        "operationName": "UpdateTransactions",
        "variables": {"input": [{"id": row_id, "isDeleted": True, "isReviewedByUser": True} for row_id in baselane_ids]},
        "query": "mutation UpdateTransactions($input: [UpdateTransaction!]) { updateTransactions(input: $input) { id isDeleted } }",
    }
    rows = run_graphql(payload)["data"]["updateTransactions"]
    return {"status": "applied", "deleted_count": len(rows), "rows": rows}


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# OH/IL/TN Cash-Basis Insurance Audit",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Mode: `{report['mode']}`",
        f"- Local insurance accrual rows removed: `{report['removed_local_accrual_count']}`",
        f"- Live Baselane delete candidates: `{report['live_delete_candidate_count']}`",
        f"- Property-split GLs checked: `{report.get('property_split_gl_candidate_count', 0)}`",
        f"- Property-split GLs containing insurance accruals: `{report.get('property_split_gl_files_with_insurance_accrual_count', 0)}`",
        f"- OSC Risk Secure payment rows reviewed: `{report['osc_payment_count']}`",
        f"- Duplicate/review flags: `{report['duplicate_flag_count']}`",
        f"- Skip/credit review candidates: `{report['skip_review_candidate_count']}`",
        "",
        "## Removed Insurance Accruals",
    ]
    if report["removed_accruals"]:
        for item in report["removed_accruals"]:
            ids = ", ".join(item.get("baselane_ids") or []) or "no BaselaneId in source index"
            lines.append(f"- `{item['month']}` `{item['property']}` `{item['amount']:.2f}` row `{item['csv_row']}` IDs `{ids}`")
    else:
        lines.append("- None found.")
    lines.extend(["", "## Skip/Credit Review Queue"])
    if report["skip_review_candidates"]:
        lines.append("- Do not auto-skip. Only skip or credit after Obie/OSC confirms the duplicate was not refunded and should offset a future month.")
        for item in report["skip_review_candidates"]:
            lines.append(
                f"- `{item['severity']}` `{item['property']}` duplicate month `{item['duplicate_month']}` "
                f"candidate skip/credit month `{item['candidate_skip_or_credit_month'] or 'n/a'}` "
                f"amount `{item['duplicate_amount']}` card `{item.get('card_suffix') or 'n/a'}`"
            )
            lines.append(f"  - {item['action']}")
    else:
        lines.append("- No skip/credit candidates found.")
    lines.extend(["", "## OSC Risk Secure Duplicate Flags"])
    if report["duplicate_flags"]:
        for flag in report["duplicate_flags"]:
            lines.append(f"- `{flag['severity']}` `{flag['kind']}` `{flag['property']}` `{flag['month']}` amount `{flag['amount']}` card `{flag.get('card_suffix') or 'n/a'}`")
            lines.append(f"  - {flag['recommendation']}")
            for row in flag["rows"]:
                lines.append(f"  - row `{row['csv_row']}` `{row['date']}` `{row['amount']:.2f}` `{row['merchant']}`")
    else:
        lines.append("- No duplicate OSC Risk Secure payment flags found.")
    lines.extend(["", "## Rule", "- OH/IL/TN insurance is cash-basis only via OSC Risk Secure transactions; monthly Insurance Accrual rows should not be generated for these states."])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_report(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    fieldnames, rows = read_csv(args.gl_csv)
    resolve_source = getattr(args, "resolve_source", True)
    _source_fields, source_rows = read_csv(args.source_index) if resolve_source and args.source_index.is_file() else ([], [])
    source_by_key: dict[tuple[str, str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    source_by_marker: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in source_rows:
        source_by_key[source_row_key(row)].append(row)
        marker_key = marker_key_from_row(row)
        if marker_key:
            source_by_marker[marker_key].append(row)

    removed_accruals: list[dict[str, Any]] = []
    kept_rows: list[dict[str, str]] = []
    live_delete_ids: list[str] = []
    payments: list[dict[str, Any]] = []
    for csv_row, row in enumerate(rows, start=2):
        if getattr(args, "audit_duplicates", True) and is_osc_risk_secure(row):
            record = payment_record(csv_row, row)
            if record["state"] in CASH_BASIS_INSURANCE_STATES:
                payments.append(record)
        if is_scoped_insurance_accrual(row):
            matches = source_matches_for_accrual(row, source_by_key, source_by_marker)
            record = accrual_record(csv_row, row, matches)
            removed_accruals.append(record)
            live_delete_ids.extend(record["baselane_ids"])
            continue
        kept_rows.append(row)

    live_delete_ids = sorted(dict.fromkeys(live_delete_ids), key=lambda value: int(value) if value.isdigit() else value)
    duplicate_rows = duplicate_flags(payments)
    skip_candidates = skip_review_candidates(duplicate_rows)
    report = {
        "generated_at": iso_z(),
        "status": "ok",
        "mode": "apply" if args.apply_local or args.apply_live else "dry_run",
        "gl_csv": str(args.gl_csv),
        "source_index": str(args.source_index),
        "cash_basis_insurance_states": sorted(CASH_BASIS_INSURANCE_STATES),
        "removed_local_accrual_count": len(removed_accruals),
        "removed_accruals": removed_accruals,
        "local_row_count_before": len(rows),
        "local_row_count_after": len(kept_rows),
        "live_delete_candidate_count": len(live_delete_ids),
        "live_delete_candidate_ids": live_delete_ids,
        "osc_payment_count": len(payments),
        "duplicate_flag_count": len(duplicate_rows),
        "duplicate_flags": duplicate_rows,
        "skip_review_candidate_count": len(skip_candidates),
        "skip_review_candidates": skip_candidates,
        "digest": hashlib.sha256(json.dumps({"removed": removed_accruals, "duplicates": duplicate_rows}, sort_keys=True, default=str).encode()).hexdigest(),
    }
    return report, kept_rows, fieldnames


def aggregate_reports(subreports: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    removed_accruals = [
        {**item, "gl_csv": report["gl_csv"]}
        for report in subreports
        for item in report.get("removed_accruals", [])
    ]
    duplicate_flags: list[dict[str, Any]] = []
    seen_duplicate_keys: set[str] = set()
    for report in subreports:
        for flag in report.get("duplicate_flags") or []:
            if not isinstance(flag, dict):
                continue
            dedupe_key = json.dumps(
                {
                    "kind": flag.get("kind"),
                    "property": flag.get("property"),
                    "month": flag.get("month"),
                    "amount": flag.get("amount"),
                    "card_suffix": flag.get("card_suffix"),
                    "rows": [
                        {
                            "date": row.get("date"),
                            "amount": row.get("amount"),
                            "card_suffix": row.get("card_suffix"),
                        }
                        for row in flag.get("rows") or []
                        if isinstance(row, dict)
                    ],
                },
                sort_keys=True,
                default=str,
            )
            if dedupe_key in seen_duplicate_keys:
                continue
            seen_duplicate_keys.add(dedupe_key)
            duplicate_flags.append(flag)
    duplicate_flags.sort(
        key=lambda item: (
            SEVERITY_RANK.get(str(item.get("severity")), 9),
            str(item.get("property") or ""),
            str(item.get("month") or ""),
            str(item.get("kind") or ""),
        )
    )
    aggregated_skip_review_candidates = skip_review_candidates(duplicate_flags)
    live_delete_ids = sorted(
        {
            row_id
            for report in subreports
            for row_id in report.get("live_delete_candidate_ids", [])
            if row_id
        },
        key=lambda value: int(value) if str(value).isdigit() else str(value),
    )
    status = "failed" if any(report.get("status") == "failed" for report in subreports) else "ok"
    report = {
        "generated_at": iso_z(),
        "status": status,
        "mode": "apply" if args.apply_local or args.apply_live else "dry_run",
        "gl_csv": str(args.gl_csv) if args.gl_csv else "",
        "gl_csvs": [report.get("gl_csv") for report in subreports],
        "gl_csv_count": len(subreports),
        "property_split_gl_scan_enabled": not args.skip_property_split_gls,
        "real_estate_root": str(args.real_estate_root),
        "source_index": str(args.source_index),
        "markdown_report": str(args.report_md),
        "csv_report": str(args.report_csv),
        "cash_basis_insurance_states": sorted(CASH_BASIS_INSURANCE_STATES),
        "removed_local_accrual_count": len(removed_accruals),
        "removed_local_accrual_file_count": len({item.get("gl_csv") for item in removed_accruals}),
        "removed_accruals": removed_accruals,
        "local_row_count_before": sum(int(report.get("local_row_count_before") or 0) for report in subreports),
        "local_row_count_after": sum(int(report.get("local_row_count_after") or 0) for report in subreports),
        "live_delete_candidate_count": len(live_delete_ids),
        "live_delete_candidate_ids": live_delete_ids,
        "osc_payment_count": sum(int(report.get("osc_payment_count") or 0) for report in subreports),
        "duplicate_flag_count": len(duplicate_flags),
        "duplicate_flags": duplicate_flags,
        "skip_review_candidate_count": len(aggregated_skip_review_candidates),
        "skip_review_candidates": aggregated_skip_review_candidates,
        "digest": hashlib.sha256(
            json.dumps(
                {
                    "removed": removed_accruals,
                    "duplicates": duplicate_flags,
                    "gl_csvs": [report.get("gl_csv") for report in subreports],
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest(),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete OH/IL/TN insurance accruals and audit OSC Risk Secure cash-basis payments.")
    parser.add_argument("--gl-csv", type=Path, default=None)
    parser.add_argument("--real-estate-root", type=Path, default=DEFAULT_REAL_ESTATE_ROOT)
    parser.add_argument("--skip-property-split-gls", action="store_true")
    parser.add_argument("--source-index", type=Path, default=DEFAULT_SOURCE_INDEX)
    parser.add_argument("--report-json", type=Path, default=DEFAULT_REPORT_JSON)
    parser.add_argument("--report-md", type=Path, default=DEFAULT_REPORT_MD)
    parser.add_argument("--report-csv", type=Path, default=DEFAULT_REPORT_CSV)
    parser.add_argument("--apply-local", action="store_true")
    parser.add_argument("--apply-live", action="store_true")
    args = parser.parse_args()

    targets = cleanup_targets(args)
    property_split_targets = [] if args.skip_property_split_gls else discover_property_split_gls(
        args.real_estate_root, CASH_BASIS_INSURANCE_STATES
    )
    subresults: list[tuple[dict[str, Any], list[dict[str, str]], list[str]]] = []
    for path in targets:
        audit_duplicates = path == (args.gl_csv or DEFAULT_GL)
        if not audit_duplicates and not file_contains_insurance_accrual(path):
            continue
        subargs = argparse.Namespace(**vars(args))
        subargs.gl_csv = path
        subargs.audit_duplicates = audit_duplicates
        subargs.resolve_source = audit_duplicates
        subresults.append(build_report(subargs))
    report = aggregate_reports([item[0] for item in subresults], args)
    report["cleanup_target_count"] = len(targets)
    report["property_split_gl_candidate_count"] = len(property_split_targets)
    report["property_split_gl_files_with_insurance_accrual_count"] = sum(
        1
        for subreport, _kept_rows, _fieldnames in subresults
        if Path(str(subreport.get("gl_csv") or "")) in set(property_split_targets)
    )
    report["property_split_gl_scan_scope"] = (
        "state/{property}/Public/07 - P&L & Owner Statements, "
        "state/{property} Public/07 - P&L & Owner Statements, and legacy "
        "state/{property}/07 - P&L & Owner Statements; nested archive/recovery paths excluded"
    )
    live_result = {"status": "skipped", "reason": "dry_run"}
    if args.apply_live:
        try:
            live_result = delete_live_transactions(report["live_delete_candidate_ids"])
        except Exception as exc:
            live_result = {"status": "failed", "error": str(exc)}
            report["status"] = "failed"
    local_apply_allowed = True
    if args.apply_local and report["removed_local_accrual_count"]:
        local_apply_allowed = (
            live_result.get("status") == "applied"
            and int(live_result.get("deleted_count") or 0) >= int(report.get("live_delete_candidate_count") or 0)
            and all(item.get("baselane_ids") for item in report.get("removed_accruals") or [])
        )
    if args.apply_local and live_result.get("status") != "failed" and local_apply_allowed:
        applied_files = []
        for subreport, kept_rows, fieldnames in subresults:
            if int(subreport.get("removed_local_accrual_count") or 0) <= 0:
                continue
            gl_path = Path(str(subreport.get("gl_csv") or ""))
            write_csv_atomic(gl_path, fieldnames, kept_rows)
            applied_files.append(str(gl_path))
        report["local_apply_status"] = "applied"
        report["local_apply_file_count"] = len(applied_files)
        report["local_apply_files"] = applied_files
    elif args.apply_local:
        report["status"] = "failed"
        report["local_apply_status"] = "blocked_live_delete_unconfirmed"
        report["local_apply_block_reason"] = (
            "Refused local deletion because every removed accrual must have a BaselaneId and a confirmed live delete."
        )
    else:
        report["local_apply_status"] = "dry_run"
        report["local_apply_file_count"] = 0
        report["local_apply_files"] = []
    report["live_apply_status"] = live_result

    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    write_markdown(args.report_md, report)
    write_duplicate_csv(args.report_csv, report)
    print(json.dumps({key: report[key] for key in ("status", "mode", "removed_local_accrual_count", "live_delete_candidate_count", "osc_payment_count", "duplicate_flag_count", "skip_review_candidate_count", "digest")}, indent=2))
    print(str(args.report_md))
    print(str(args.report_csv))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
