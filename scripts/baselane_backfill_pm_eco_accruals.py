#!/usr/bin/env python3
"""Safely complete reviewed legacy PM DAO accruals with ECO revenue rows.

Eligibility is based on the actual rent transaction source, never geography.
Literal Hemlane deposits are net rent and do not create a manual PM accrual;
direct gross receipts do. The tool also refuses to pair an ECO revenue row to
an incorrectly calculated legacy DAO expense.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import baselane_monthly_accruals_idempotent as accruals


DEFAULT_GL = Path("/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv")
# The $37,500 April deposit at 9 Country Club is deferred until August 2026;
# its $7,500 PM component must not be recognized in this historical backfill.
DEFERRED_LEGACY_KEYS = {("9 Country Club Ln N", "2026-04")}
PM_SETTLEMENT_CASH_RE = re.compile(r"pm_settlement_cash\|(?P<property>[^|]+)\|\d{4}-\d{2}\|(?P<amount>\d+(?:\.\d+)?)", re.I)


def marker_for(notes: str) -> dict[str, str] | None:
    return accruals.parse_marker(notes) or accruals.parse_pm_fee_marker(notes)


def legacy_pm_rows(rows: list[dict[str, str]]) -> list[tuple[int, dict[str, str], dict[str, str]]]:
    results = []
    for csv_row, row in enumerate(rows, start=2):
        marker = marker_for(str(row.get("Notes") or ""))
        if not marker or marker["kind"] != "pm":
            continue
        try:
            amount = float(str(row.get("Amount") or "0").replace(",", ""))
        except ValueError:
            continue
        if amount >= 0:
            continue
        results.append((csv_row, row, marker))
    return results


def existing_eco_keys(rows: list[dict[str, str]]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in rows:
        marker = marker_for(str(row.get("Notes") or ""))
        if marker and marker["kind"] == accruals.PM_ECO_KIND:
            keys.add((marker["property"], marker["month"]))
    return keys


def cash_settled_legacy_amounts(rows: list[dict[str, str]]) -> set[tuple[str, float]]:
    """Return DAO PM amounts already neutralized by a dedicated settlement row.

    Those legacy rows are not safe to pair with ECO revenue until their
    historical cash workaround has itself been reclassified as a transfer.
    """
    settled: set[tuple[str, float]] = set()
    for row in rows:
        raw_notes = str(row.get("Notes") or "")
        marker = accruals.parse_marker(raw_notes)
        cash_match = PM_SETTLEMENT_CASH_RE.search(raw_notes)
        if marker and marker["kind"] == "pm_settlement":
            property_name = marker["property"]
        elif cash_match:
            property_name = cash_match.group("property").strip()
        else:
            continue
        try:
            amount = round(abs(float(str(row.get("Amount") or "0").replace(",", ""))), 2)
        except ValueError:
            amount = 0.0
        if not amount and cash_match:
            amount = round(float(cash_match.group("amount")), 2)
        if amount:
            settled.add((property_name, amount))
    return settled


def build_backfill(rows: list[dict[str, str]], fieldnames: list[str]) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    eco_keys = existing_eco_keys(rows)
    settled_amounts = cash_settled_legacy_amounts(rows)
    months = sorted({marker["month"] for _, _, marker in legacy_pm_rows(rows)})
    expected_by_month = {
        month: accruals.compute_pm_fees(rows, month)
        for month in months
    }
    new_rows: list[dict[str, str]] = []
    report: list[dict[str, object]] = []
    for csv_row, source, marker in legacy_pm_rows(rows):
        property_name, month = marker["property"], marker["month"]
        state = accruals.property_state(property_name)
        entry: dict[str, object] = {"csv_row": csv_row, "property": property_name, "month": month, "amount": abs(float(str(source["Amount"]).replace(",", ""))), "state": state}
        expected_amount = expected_by_month.get(month, {}).get(property_name)
        hemlane_net_basis = accruals.hemlane_net_rent_amount(rows, property_name, month)
        entry["expected_amount"] = expected_amount
        entry["hemlane_net_rent_basis"] = hemlane_net_basis
        if (property_name, month) in DEFERRED_LEGACY_KEYS:
            entry["status"] = "deferred_explicitly"
        elif expected_amount is None:
            entry["status"] = (
                "hemlane_net_no_manual_accrual"
                if hemlane_net_basis > 0
                else "no_direct_gross_rent_basis"
            )
        elif round(float(entry["amount"]), 2) != round(float(expected_amount), 2):
            entry["status"] = "dao_amount_mismatch_requires_correction"
        elif (property_name, float(entry["amount"])) in settled_amounts:
            entry["status"] = "cash_settlement_reclass_required"
        elif (property_name, month) in eco_keys:
            entry["status"] = "already_completed"
        else:
            amount = abs(float(str(source["Amount"]).replace(",", "")))
            prefix = marker["prefix"]
            accrual_id = f"{prefix}|pm|{property_name}|{month}|{amount:.2f}"
            month_label = accruals.month_label(month)
            rate = next((item[1] for item in accruals.PM_FEE_PROPERTIES if item[0] == property_name), 0.0)
            components = accruals.PM_FEE_COMPONENTS.get(property_name) or []
            label = f"ECO Systems LLC PM Fee Revenue | {property_name} | {month_label}"
            row = {field: "" for field in fieldnames}
            row.update({
                "Account": "ECO Systems, LLC-ECO Systems Accrual Overlay",
                "Date": accruals.month_end_date(month),
                "Merchant": label,
                "Description": label,
                "Amount": f"{amount:.2f}",
                "Type": "Revenue",
                "Category": "Fees & Other Revenue",
                "Sub-category": "Property Management",
                "Property": accruals.reporting_property_name(property_name),
                "Unit": "",
                "Notes": f"{prefix}|{accruals.PM_ECO_KIND}|{property_name}|{month}|{amount:.2f} | Accrual ID {accrual_id}. ECO Systems LLC PM fee revenue for {month_label} ({accruals.pm_fee_rule_summary(property_name, month, rate, components)}); matched to pre-existing DAO-side PM expense. Accounting/manual accrual only, no bank transfer.",
            })
            new_rows.append(row)
            entry["status"] = "ready"
        report.append(entry)
    return new_rows, report


def normalize(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def marker_in_scope(
    marker: dict[str, str],
    property_filters: list[str] | None,
    start_month: str | None,
    end_month: str | None,
) -> bool:
    if property_filters:
        property_key = normalize(marker["property"])
        filter_keys = [normalize(value) for value in property_filters]
        if not any(key in property_key or property_key in key for key in filter_keys):
            return False
    if start_month and marker["month"] < start_month:
        return False
    if end_month and marker["month"] > end_month:
        return False
    return True


def scoped_legacy_settlement_rows(
    rows: list[dict[str, str]],
    property_filters: list[str] | None,
    start_month: str | None,
    end_month: str | None,
) -> list[tuple[int, dict[str, str], dict[str, str]]]:
    results = []
    for csv_row, row in enumerate(rows, start=2):
        marker = marker_for(str(row.get("Notes") or ""))
        if (
            marker
            and marker["kind"] == "pm_settlement"
            and marker_in_scope(marker, property_filters, start_month, end_month)
        ):
            results.append((csv_row, row, marker))
    return results


def action_digest(actions: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(actions, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gl-csv", type=Path, default=DEFAULT_GL)
    parser.add_argument("--property", dest="property_filters", action="append", default=None)
    parser.add_argument("--start-month")
    parser.add_argument("--end-month")
    parser.add_argument(
        "--remove-legacy-settlements",
        action="store_true",
        help="Remove scoped obsolete positive PM-settlement rows before completing ECO-side accruals.",
    )
    parser.add_argument("--require-action-digest")
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed scoped migration to the local GL.")
    args = parser.parse_args()
    if bool(args.start_month) != bool(args.end_month):
        parser.error("--start-month and --end-month must be provided together")
    fieldnames, rows = accruals.read_gl(args.gl_csv)
    settlement_rows = (
        scoped_legacy_settlement_rows(
            rows, args.property_filters, args.start_month, args.end_month
        )
        if args.remove_legacy_settlements
        else []
    )
    removed_row_numbers = {item[0] for item in settlement_rows}
    working_rows = [
        row for csv_row, row in enumerate(rows, start=2)
        if csv_row not in removed_row_numbers
    ]
    all_new_rows, all_report = build_backfill(working_rows, fieldnames)
    report = [
        item for item in all_report
        if marker_in_scope(
            {"property": str(item["property"]), "month": str(item["month"])},
            args.property_filters,
            args.start_month,
            args.end_month,
        )
    ]
    ready_keys = {
        (str(item["property"]), str(item["month"]))
        for item in report if item["status"] == "ready"
    }
    new_rows = [
        row for row in all_new_rows
        if (
            (marker := marker_for(str(row.get("Notes") or "")))
            and (marker["property"], marker["month"]) in ready_keys
        )
    ]
    summary = defaultdict(int)
    for item in report:
        summary[str(item["status"])] += 1
    actions = {
        "gl_csv": str(args.gl_csv),
        "remove_legacy_settlements": [
            {
                "csv_row": csv_row,
                "property": marker["property"],
                "month": marker["month"],
                "amount": row.get("Amount"),
                "notes": row.get("Notes"),
            }
            for csv_row, row, marker in settlement_rows
        ],
        "append_eco_rows": [
            {
                "property": marker["property"],
                "month": marker["month"],
                "amount": row.get("Amount"),
                "notes": row.get("Notes"),
            }
            for row in new_rows
            if (marker := marker_for(str(row.get("Notes") or "")))
        ],
    }
    digest = action_digest(actions)
    output = {
        "summary": dict(sorted(summary.items())),
        "action_digest": digest,
        "legacy_settlement_remove_count": len(settlement_rows),
        "eco_row_append_count": len(new_rows),
        "legacy_settlement_rows": actions["remove_legacy_settlements"],
        "ready_rows": [item for item in report if item["status"] == "ready"],
        "review_rows": [item for item in report if item["status"] != "ready"],
    }
    if args.apply:
        if args.require_action_digest != digest:
            parser.error(
                "--apply requires --require-action-digest matching the current dry-run"
            )
        if settlement_rows:
            accruals.write_gl(args.gl_csv, working_rows + new_rows, fieldnames)
        elif new_rows:
            accruals.append_rows_to_gl(args.gl_csv, new_rows, fieldnames)
        output["applied"] = True
    else:
        output["applied"] = False
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
