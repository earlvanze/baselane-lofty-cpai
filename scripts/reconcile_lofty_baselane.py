#!/usr/bin/env python3
"""
Lofty ↔ Baselane Reconciliation

Compares Lofty property financial data with Baselane actual transactions
to identify discrepancies in rent collection, expenses, and reserves.

Usage:
  python3 reconcile_lofty_baselane.py --property "49 Bannbury Ln" --month 2026-03
"""

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Any, TextIO

WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE_ROOT", Path(__file__).absolute().parents[1]))
DROPBOX = WORKSPACE / "Dropbox"
SCRIPT_PATH = Path(__file__).resolve()
ISSUE_CLASS = "lofty-baselane-reconciliation"
FINANCIALS_RELATIVE_PATH = Path("Public") / "00 - README & Property Snapshot" / "FINANCIALS.md"

def load_lofty_financials(property_path):
    """Load Lofty financial data from the canonical public financials path."""
    financials_file = property_path / FINANCIALS_RELATIVE_PATH
    if not financials_file.exists():
        return None

    content = financials_file.read_text()
    data = {"raw": content}

    # Parse key metrics (simple extraction)
    import re
    patterns = {
        "sale_price": r"Sale Price: \$([\d,]+)",
        "closing_costs": r"Closing Costs: \$([\d,]+)",
        "total_investment": r"Total Investment: \$([\d,]+)",
        "cap_rate": r"Cap Rate: ([\d.]+)%",
        "irr": r"IRR: ([\d.]+)%",
        "maintenance_reserve": r"Current Maintenance Reserve: \$([\d,]+)",
        "monthly_rent": r"Monthly Rent: \$([\d,]+)",
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, content)
        if match:
            value = match.group(1).replace(",", "")
            data[key] = float(value) if "." in value else int(float(value))

    return data

def load_baselane_transactions(property_name, month):
    """Load Baselane transactions for a property from split ledger files"""
    # Find property's Baselane split folder
    splits_dir = DROPBOX / "Real Estate" / "_SPLITS" / property_name.replace(",", "")

    transactions = []

    # Look for monthly CSV files
    month_pattern = f"*{month.replace('-', '')}*.csv"
    for csv_file in splits_dir.glob(month_pattern):
        import csv
        with open(csv_file) as f:
            reader = csv.DictReader(f)
            for row in reader:
                transactions.append({
                    "date": row.get("Date", ""),
                    "description": row.get("Description", ""),
                    "amount": float(row.get("Amount", 0) or 0),
                    "category": row.get("Category", ""),
                    "type": row.get("Type", ""),  # Income/Expense
                    "file": str(csv_file),
                })

    return transactions


def find_property_path(property_name: str) -> Path | None:
    """Find a property folder by matching the existing FINANCIALS.md layout."""
    for root in [DROPBOX / "Real Estate"]:
        if not root.exists():
            continue
        pattern = f"**/{property_name.replace(' ', '*')}*/{FINANCIALS_RELATIVE_PATH.as_posix()}"
        for match in root.glob(pattern):
            return match.parent.parent.parent
    return None

def reconcile(lofty_data, baselane_transactions, property_name, month):
    """Compare Lofty expectations vs Baselane actuals"""
    report = {
        "property": property_name,
        "month": month,
        "discrepancies": [],
        "summary": {},
    }

    # Calculate Baselane totals by category
    baselane_totals = defaultdict(float)
    for tx in baselane_transactions:
        baselane_totals[tx["category"]] += tx["amount"]

    # Compare rent income
    lofty_rent = lofty_data.get("monthly_rent", 0)
    baselane_rent = baselane_totals.get("Rent", 0) + baselane_totals.get("Rental Income", 0)

    if abs(lofty_rent - baselane_rent) > 1:  # $1 tolerance
        report["discrepancies"].append({
            "type": "rent_variance",
            "lofty_expected": lofty_rent,
            "baselane_actual": baselane_rent,
            "difference": baselane_rent - lofty_rent,
            "severity": "high" if abs(baselane_rent - lofty_rent) > 100 else "medium",
        })

    # Compare maintenance reserve contributions
    lofty_reserve = lofty_data.get("maintenance_reserve", 0)
    baselane_reserve = baselane_totals.get("Maintenance Reserve", 0)

    if abs(lofty_reserve - baselane_reserve) > 1:
        report["discrepancies"].append({
            "type": "reserve_variance",
            "lofty_expected": lofty_reserve,
            "baselane_actual": baselane_reserve,
            "difference": baselane_reserve - lofty_reserve,
            "severity": "medium",
        })

    # Summary
    report["summary"] = {
        "lofty_monthly_rent": lofty_rent,
        "baselane_rent_collected": baselane_rent,
        "baselane_total_expenses": sum(t["amount"] for t in baselane_transactions if t["type"] == "Expense"),
        "transaction_count": len(baselane_transactions),
        "discrepancy_count": len(report["discrepancies"]),
    }
    report["discrepancy_count"] = len(report["discrepancies"])

    return report


def diagnostic_command(property_name: str, month: str) -> str:
    return " ".join(
        shlex.quote(part)
        for part in [
            "python3",
            str(SCRIPT_PATH),
            "--property",
            property_name,
            "--month",
            month,
            "--json",
        ]
    )


def review_command_validation(command_text: str, property_name: str, month: str) -> dict[str, Any]:
    validation: dict[str, Any] = {
        "command": command_text,
        "valid": False,
        "issues": [],
        "parts": [],
        "script_path": str(SCRIPT_PATH),
        "script_exists": SCRIPT_PATH.exists(),
        "script_is_file": SCRIPT_PATH.is_file(),
        "path": str(SCRIPT_PATH),
        "path_exists": SCRIPT_PATH.exists(),
    }
    try:
        parts = shlex.split(command_text)
    except ValueError as exc:
        validation["issues"].append(f"parse-error:{exc}")
        return validation

    validation["parts"] = parts
    if len(parts) != 7:
        validation["issues"].append("unexpected-argument-count")
    if not parts or parts[0] != "python3":
        validation["issues"].append("missing-python3")
    if len(parts) < 2 or Path(parts[1]).resolve() != SCRIPT_PATH:
        validation["issues"].append("unexpected-script-path")
    if "--property" not in parts:
        validation["issues"].append("missing-property-flag")
    else:
        idx = parts.index("--property")
        if idx + 1 >= len(parts) or parts[idx + 1] != property_name:
            validation["issues"].append("unexpected-property")
    if "--month" not in parts:
        validation["issues"].append("missing-month-flag")
    else:
        idx = parts.index("--month")
        if idx + 1 >= len(parts) or parts[idx + 1] != month:
            validation["issues"].append("unexpected-month")
    if "--json" not in parts:
        validation["issues"].append("missing-json-flag")
    if "--output" in parts or "-o" in parts:
        validation["issues"].append("unexpected-output-write-flag")
    if not SCRIPT_PATH.exists():
        validation["issues"].append("script-missing")
    elif not SCRIPT_PATH.is_file():
        validation["issues"].append("script-not-file")

    validation["valid"] = not validation["issues"]
    return validation


def remediation_fields(ok_state: bool, property_name: str, month: str) -> dict[str, Any]:
    command = diagnostic_command(property_name, month)
    validation = review_command_validation(command, property_name, month)
    return {
        "remediation_class": "no-remediation-needed" if ok_state else "operator-reviewed-lofty-baselane-reconciliation",
        "requires_operator_approval": not ok_state,
        "requires_interactive_sudo": False,
        "requires_interactive_oauth": False,
        "safe_to_run_automatically": ok_state,
        "review_command": None if ok_state else command,
        "review_command_safe_to_run_automatically": not ok_state,
        "review_command_valid": None if ok_state else validation["valid"],
        "review_command_validation": None if ok_state else validation,
        "cleanup_command_after_review": None,
        "restart_command_after_review": None,
        "oauth_command_after_review": None,
        "helper_command_after_review": None,
        "remediation": {
            "command": None,
            "review_command": None if ok_state else command,
            "review_command_validation": None if ok_state else validation,
        },
    }


def classified_issue_records(report: dict[str, Any]) -> list[dict[str, Any]]:
    fields = remediation_fields(False, str(report["property"]), str(report["month"]))
    records: list[dict[str, Any]] = []

    for issue in report.get("input_issues", []):
        records.append(
            {
                "class": ISSUE_CLASS,
                "issue_class": ISSUE_CLASS,
                "classification": report["classification"],
                "route_classification": report["classification"],
                "type": issue.get("type"),
                "severity": issue.get("severity", "high"),
                "message": issue.get("message"),
                "property": report["property"],
                "month": report["month"],
                **fields,
            }
        )

    for discrepancy in report.get("discrepancies", []):
        records.append(
            {
                "class": ISSUE_CLASS,
                "issue_class": ISSUE_CLASS,
                "classification": report["classification"],
                "route_classification": report["classification"],
                "type": discrepancy.get("type"),
                "severity": discrepancy.get("severity"),
                "property": report["property"],
                "month": report["month"],
                "lofty_expected": discrepancy.get("lofty_expected"),
                "baselane_actual": discrepancy.get("baselane_actual"),
                "difference": discrepancy.get("difference"),
                **fields,
            }
        )

    return records


def classified_issue_summary(report: dict[str, Any]) -> dict[str, Any]:
    issues = report.get("classified_issues", [])
    class_counts: dict[str, int] = defaultdict(int)
    severity_counts: dict[str, int] = defaultdict(int)
    valid_count = 0
    invalid_count = 0
    validation_issues: list[str] = []

    for issue in issues:
        cls = str(issue.get("issue_class") or issue.get("class") or ISSUE_CLASS)
        class_counts[cls] += 1
        severity = issue.get("severity")
        if severity:
            severity_counts[str(severity)] += 1
        if issue.get("review_command_safe_to_run_automatically"):
            if issue.get("review_command_valid"):
                valid_count += 1
            else:
                invalid_count += 1
                validation = issue.get("review_command_validation") or {}
                validation_issues.extend(str(item) for item in validation.get("issues", []))

    return {
        "total": len(issues),
        "total_count": len(issues),
        "issue_count": len(issues),
        "classification": report["classification"],
        "classes": sorted(class_counts),
        "class_counts": dict(sorted(class_counts.items())),
        "issue_class_counts": dict(sorted(class_counts.items())),
        "severity_counts": dict(sorted(severity_counts.items())),
        "discrepancy_count": int(report.get("discrepancy_count", 0)),
        "transaction_count": int(report.get("summary", {}).get("transaction_count", 0)),
        "input_issue_count": len(report.get("input_issues", [])),
        "review_required_count": len(issues),
        "requires_operator_approval_count": len(issues),
        "requires_interactive_sudo_count": 0,
        "requires_interactive_oauth_count": 0,
        "safe_review_command_count": valid_count + invalid_count,
        "valid_review_command_count": valid_count,
        "invalid_review_command_count": invalid_count,
        "review_command_validation_issues": sorted(set(validation_issues)),
        "write_attempted": bool(report.get("write_attempted")),
        "output_path": report.get("output_path"),
    }


def build_report(property_name: str, month: str, output: str | None = None, write_attempted: bool = False) -> dict[str, Any]:
    input_issues: list[dict[str, Any]] = []
    property_path = find_property_path(property_name)
    lofty_data: dict[str, Any] | None = None
    transactions: list[dict[str, Any]] = []
    base_report: dict[str, Any]

    if not property_path:
        input_issues.append(
            {
                "type": "property-not-found",
                "severity": "high",
                "message": f"Property '{property_name}' not found",
            }
        )
        base_report = {
            "property": property_name,
            "month": month,
            "discrepancies": [],
            "summary": {"transaction_count": 0, "discrepancy_count": 0},
        }
    else:
        lofty_data = load_lofty_financials(property_path)
        if not lofty_data:
            input_issues.append(
                {
                    "type": "financials-not-found",
                    "severity": "high",
                    "message": f"No FINANCIALS.md found for {property_name}",
                }
            )
            base_report = {
                "property": property_name,
                "month": month,
                "discrepancies": [],
                "summary": {"transaction_count": 0, "discrepancy_count": 0},
            }
        else:
            transactions = load_baselane_transactions(property_name, month)
            base_report = reconcile(lofty_data, transactions, property_name, month)

    issue_count = len(input_issues) + len(base_report.get("discrepancies", []))
    ok_state = issue_count == 0
    classification = "ok" if ok_state else "lofty-baselane-reconciliation-review"
    report = {
        **base_report,
        "status": "NO_REPLY" if ok_state else "LOFTY_BASELANE_RECONCILIATION_REVIEW",
        "classification": classification,
        "ok_state": ok_state,
        "ok": [f"reconciliation ok property={property_name} month={month}"] if ok_state else [],
        "visible_ok": [f"reconciliation ok property={property_name} month={month}"] if ok_state else [],
        "ok_count": 1 if ok_state else 0,
        "input_issues": input_issues,
        "issue_count": issue_count,
        "advisory_count": 0,
        "review_required_count": issue_count,
        "approval_required_count": issue_count,
        "property_path": str(property_path) if property_path else None,
        "property_path_exists": bool(property_path and property_path.exists()),
        "financials_loaded": lofty_data is not None,
        "transaction_count": len(transactions),
        "output_path": output,
        "write_attempted": write_attempted,
        "discrepancy_count": len(base_report.get("discrepancies", [])),
    }
    report.update(remediation_fields(ok_state, property_name, month))
    report["issue_classes"] = [] if ok_state else [ISSUE_CLASS]
    report["classified_issues"] = [] if ok_state else classified_issue_records(report)
    report["classified_issue_summary"] = classified_issue_summary(report)
    report["safe_review_command_count"] = int(report["classified_issue_summary"]["safe_review_command_count"])
    report["valid_review_command_count"] = int(report["classified_issue_summary"]["valid_review_command_count"])
    report["invalid_review_command_count"] = int(report["classified_issue_summary"]["invalid_review_command_count"])
    report["review_command_validation_issues"] = list(
        report["classified_issue_summary"]["review_command_validation_issues"]
    )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Reconcile Lofty vs Baselane')
    parser.add_argument('--property', '-p', required=True, help='Property name/address')
    parser.add_argument('--month', '-m', default=datetime.now().strftime("%Y-%m"), help='Month (YYYY-MM)')
    parser.add_argument('--output', '-o', help='Output JSON path')
    parser.add_argument('--json', action='store_true', help='Emit read-only dashboard JSON; do not write a report file')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    stdout = stdout or sys.stdout

    if args.json:
        report = build_report(args.property, args.month, output=args.output, write_attempted=False)
        json.dump(report, stdout, indent=2, sort_keys=True)
        stdout.write("\n")
        return 0 if report["ok_state"] else 1

    property_path = find_property_path(args.property)
    if not property_path:
        print(f"ERROR: Property '{args.property}' not found", file=stdout)
        return 1

    lofty_data = load_lofty_financials(property_path)
    if not lofty_data:
        print(f"ERROR: No FINANCIALS.md found for {args.property}", file=stdout)
        return 1

    transactions = load_baselane_transactions(args.property, args.month)
    report = reconcile(lofty_data, transactions, args.property, args.month)

    output = args.output or f"/tmp/reconciliation_{args.property.replace(' ', '_')}_{args.month}.json"
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with open(output, 'w') as f:
        json.dump(report, f, indent=2)

    print(json.dumps(report, indent=2), file=stdout)
    print(f"\nReport saved: {output}", file=stdout)
    return 0 if report["discrepancy_count"] == 0 else 1

if __name__ == '__main__':
    raise SystemExit(main())
