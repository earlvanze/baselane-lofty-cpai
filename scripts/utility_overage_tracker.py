#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shlex
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, TextIO

DATE_FMT = "%B %d, %Y"
ISSUE_CLASS = "utility-overage"
SCRIPT_PATH = Path(__file__).resolve()
REPORT_FIELDS = ["property", "month", "utility_total", "baseline_median", "delta_$", "delta_%", "status"]
TOP_FIELDS = ["property", "month", "utility_total", "baseline_median", "delta_$", "delta_%"]
TREND_FIELDS = ["property", "month", "utility_total"]


def parse_date(value: str):
    return datetime.strptime(value.strip(), DATE_FMT)


def month_key(dt: datetime):
    return f"{dt.year:04d}-{dt.month:02d}"


def load_rows_with_stats(path: str | Path):
    rows = []
    stats = {
        "ledger_rows": 0,
        "utility_rows": 0,
        "skipped_date_count": 0,
        "skipped_amount_count": 0,
    }
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["ledger_rows"] += 1
            if (row.get("Category") or "").strip().lower() != "utilities":
                continue
            stats["utility_rows"] += 1
            try:
                dt = parse_date(row["Date"])
            except Exception:
                stats["skipped_date_count"] += 1
                continue
            try:
                amount = float(row["Amount"])
            except Exception:
                stats["skipped_amount_count"] += 1
                continue
            rows.append({
                "date": dt,
                "month": month_key(dt),
                "property": (row.get("Property") or "").strip() or "(unknown)",
                "amount": abs(amount),
                "merchant": row.get("Merchant") or "",
                "sub_category": row.get("Sub-category") or "",
            })
    stats["loaded_utility_row_count"] = len(rows)
    return rows, stats


def load_rows(path: str):
    rows, _stats = load_rows_with_stats(path)
    return rows


def compute_monthly(rows):
    monthly = defaultdict(float)
    for r in rows:
        key = (r["property"], r["month"])
        monthly[key] += r["amount"]
    return monthly


def compute_trend(monthly):
    """Return a list of rows: property, month, utility_total."""
    trend_rows = []
    for (prop, month), total in monthly.items():
        trend_rows.append({
            "property": prop,
            "month": month,
            "utility_total": round(total, 2),
        })
    trend_rows.sort(key=lambda r: (r["property"], r["month"]))
    return trend_rows


def compute_report(monthly, baseline_months=6, pct_threshold=25.0, abs_threshold=50.0):
    by_prop = defaultdict(list)
    for (prop, month), total in monthly.items():
        by_prop[prop].append((month, total))

    report_rows = []
    for prop, items in by_prop.items():
        items.sort(key=lambda x: x[0])
        totals_by_month = {m: t for m, t in items}
        months = [m for m, _ in items]
        for idx, month in enumerate(months):
            prior_months = [m for m in months[:idx] if m in totals_by_month]
            if not prior_months:
                continue
            prior_months = prior_months[-baseline_months:]
            baseline_values = [totals_by_month[m] for m in prior_months]
            if len(baseline_values) < 3:
                continue
            baseline = median(baseline_values)
            if baseline <= 0:
                continue
            current = totals_by_month[month]
            delta = current - baseline
            delta_pct = (delta / baseline) * 100.0
            status = "OVER" if (delta_pct >= pct_threshold and delta >= abs_threshold) else "OK"
            report_rows.append({
                "property": prop,
                "month": month,
                "utility_total": round(current, 2),
                "baseline_median": round(baseline, 2),
                "delta_$": round(delta, 2),
                "delta_%": round(delta_pct, 2),
                "status": status,
            })

    return report_rows


def diagnostic_command(input_path: Path, baseline_months: int, pct_threshold: float, abs_threshold: float) -> str:
    return " ".join(
        shlex.quote(part)
        for part in [
            "python3",
            str(SCRIPT_PATH),
            "--input",
            str(input_path),
            "--baseline-months",
            str(baseline_months),
            "--pct-threshold",
            str(pct_threshold),
            "--abs-threshold",
            str(abs_threshold),
            "--json",
        ]
    )


def review_command_validation(
    command: object | None = None,
    input_path: Path | None = None,
    baseline_months: int = 12,
    pct_threshold: float = 25.0,
    abs_threshold: float = 50.0,
) -> dict[str, Any]:
    input_path = input_path or Path("")
    command_text = str(
        command
        if command is not None
        else diagnostic_command(input_path, baseline_months, pct_threshold, abs_threshold)
    )
    try:
        parts = shlex.split(command_text)
    except ValueError as exc:
        parts = []
        parse_issue = str(exc)
    else:
        parse_issue = None

    expected_script = str(SCRIPT_PATH)
    expected_input = str(input_path)
    script_exists = SCRIPT_PATH.exists()
    script_is_file = SCRIPT_PATH.is_file()
    write_flags = sorted(set(parts).intersection({"--output", "--top-output", "--trend-output"}))
    issues: list[str] = []

    def value_after(flag: str) -> str | None:
        if flag not in parts:
            return None
        index = parts.index(flag)
        if index + 1 >= len(parts):
            return None
        return parts[index + 1]

    input_value = value_after("--input")
    baseline_value = value_after("--baseline-months")
    pct_value = value_after("--pct-threshold")
    abs_value = value_after("--abs-threshold")

    if parse_issue:
        issues.append(f"command parse failed: {parse_issue}")
    if not parts or parts[0] != "python3":
        issues.append("review command must start with python3")
    if expected_script not in parts:
        issues.append(f"review command must target {expected_script}")
    if "--json" not in parts:
        issues.append("review command must include --json")
    if input_value != expected_input:
        issues.append(f"review command input must be {expected_input}")
    if baseline_value != str(baseline_months):
        issues.append(f"review command baseline months must be {baseline_months}")
    if pct_value != str(pct_threshold):
        issues.append(f"review command pct threshold must be {pct_threshold}")
    if abs_value != str(abs_threshold):
        issues.append(f"review command abs threshold must be {abs_threshold}")
    if write_flags:
        issues.append(f"review command must not include write target flags: {', '.join(write_flags)}")
    if not script_exists:
        issues.append(f"review command script is missing: {expected_script}")
    elif not script_is_file:
        issues.append(f"review command path is not a file: {expected_script}")

    return {
        "command": command_text,
        "expected_script_path": expected_script,
        "script_exists": script_exists,
        "script_is_file": script_is_file,
        "path": expected_script,
        "path_exists": script_exists,
        "python3_present": bool(parts) and parts[0] == "python3",
        "script_path_present": expected_script in parts,
        "json_flag_present": "--json" in parts,
        "input_flag_present": "--input" in parts,
        "input_value": input_value,
        "input_matches": input_value == expected_input,
        "baseline_months_value": baseline_value,
        "baseline_months_matches": baseline_value == str(baseline_months),
        "pct_threshold_value": pct_value,
        "pct_threshold_matches": pct_value == str(pct_threshold),
        "abs_threshold_value": abs_value,
        "abs_threshold_matches": abs_value == str(abs_threshold),
        "write_flags_present": write_flags,
        "requires_executable": False,
        "valid": not issues,
        "issues": issues,
        "issue": issues[0] if issues else None,
    }


def remediation_fields(
    classification: str,
    input_path: Path,
    baseline_months: int,
    pct_threshold: float,
    abs_threshold: float,
) -> dict[str, Any]:
    has_issues = classification != "ok"
    command = diagnostic_command(input_path, baseline_months, pct_threshold, abs_threshold)
    return {
        "remediation_class": ISSUE_CLASS if has_issues else "no-remediation-needed",
        "requires_operator_approval": has_issues,
        "requires_interactive_sudo": False,
        "requires_interactive_oauth": False,
        "safe_to_run_automatically": not has_issues,
        "review_command": command,
        "review_command_safe_to_run_automatically": True,
        "cleanup_command_after_review": None,
        "restart_command_after_review": None,
        "oauth_command_after_review": None,
        "helper_command_after_review": None,
    }


def classified_issue_records(
    issues: list[str],
    classification: str,
    input_path: Path,
    baseline_months: int,
    pct_threshold: float,
    abs_threshold: float,
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    fields = remediation_fields(classification, input_path, baseline_months, pct_threshold, abs_threshold)
    validation = review_command_validation(
        fields["review_command"],
        input_path=input_path,
        baseline_months=baseline_months,
        pct_threshold=pct_threshold,
        abs_threshold=abs_threshold,
    )
    return [
        {
            "issue": issue,
            "issue_class": ISSUE_CLASS,
            "classification": classification,
            "area": "utility-overage-tracker",
            "input": str(input_path),
            "input_exists": evidence.get("input_exists"),
            "input_readable": evidence.get("input_readable"),
            "ledger_rows": evidence.get("ledger_rows"),
            "utility_rows": evidence.get("utility_rows"),
            "loaded_utility_row_count": evidence.get("loaded_utility_row_count"),
            "report_row_count": evidence.get("report_row_count"),
            "over_count": evidence.get("over_count"),
            "review_command_valid": validation["valid"],
            "review_command_validation": validation,
            **fields,
        }
        for issue in issues
    ]


def classified_issue_summary(report: dict[str, Any]) -> dict[str, Any]:
    classified = report.get("classified_issues") or []
    class_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    for issue in classified:
        issue_class = issue.get("issue_class")
        route = issue.get("classification", report.get("classification"))
        if issue_class:
            class_counts[issue_class] = class_counts.get(issue_class, 0) + 1
        if route:
            route_counts[route] = route_counts.get(route, 0) + 1
    return {
        "total": len(classified),
        "total_count": len(classified),
        "ok_count": int(report.get("ok_count") or 0),
        "issue_count": int(report.get("issue_count") or 0),
        "visible_ok_count": len(report.get("visible_ok") or []),
        "class_counts": class_counts,
        "issue_class_counts": class_counts,
        "route_classification": report.get("classification"),
        "route_classification_counts": route_counts,
        "approval_required_count": sum(1 for issue in classified if issue.get("requires_operator_approval")),
        "review_required_count": int(report.get("review_required_count") or 0),
        "interactive_sudo_count": sum(1 for issue in classified if issue.get("requires_interactive_sudo")),
        "interactive_oauth_count": sum(1 for issue in classified if issue.get("requires_interactive_oauth")),
        "safe_review_command_count": sum(1 for issue in classified if issue.get("review_command_safe_to_run_automatically")),
        "valid_review_command_count": sum(
            1
            for issue in classified
            if issue.get("review_command_safe_to_run_automatically") and issue.get("review_command_valid")
        ),
        "invalid_review_command_count": sum(
            1
            for issue in classified
            if issue.get("review_command_safe_to_run_automatically") and not issue.get("review_command_valid")
        ),
        "review_command_validation_issues": [
            validation_issue
            for issue in classified
            if issue.get("review_command_safe_to_run_automatically") and not issue.get("review_command_valid")
            for validation_issue in ((issue.get("review_command_validation") or {}).get("issues") or [])
        ],
        "safe_to_run_automatically": report.get("safe_to_run_automatically") is True,
        "input_exists": report.get("input_exists") is True,
        "input_readable": report.get("input_readable") is True,
        "write_attempted": report.get("write_attempted") is True,
        "ledger_rows": int(report.get("ledger_rows") or 0),
        "utility_rows": int(report.get("utility_rows") or 0),
        "loaded_utility_row_count": int(report.get("loaded_utility_row_count") or 0),
        "report_row_count": int(report.get("report_row_count") or 0),
        "over_count": int(report.get("over_count") or 0),
        "remediation_class": report.get("remediation_class"),
        "cleanup_command_available_after_review": bool(report.get("cleanup_command_after_review")),
        "restart_command_available_after_review": bool(report.get("restart_command_after_review")),
        "oauth_command_available_after_review": bool(report.get("oauth_command_after_review")),
        "helper_command_available_after_review": bool(report.get("helper_command_after_review")),
    }


def build_report(
    input_path: str | Path,
    output_path: str | Path | None = None,
    top_output: str | Path | None = None,
    trend_output: str | Path | None = None,
    baseline_months: int = 12,
    pct_threshold: float = 25.0,
    abs_threshold: float = 50.0,
    write_attempted: bool = False,
) -> dict[str, Any]:
    input_path = Path(input_path)
    output_path = Path(output_path) if output_path else None
    top_output = Path(top_output) if top_output else None
    trend_output = Path(trend_output) if trend_output else None
    issues: list[str] = []
    rows: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "ledger_rows": 0,
        "utility_rows": 0,
        "skipped_date_count": 0,
        "skipped_amount_count": 0,
        "loaded_utility_row_count": 0,
    }
    input_exists = input_path.exists()
    input_readable = input_path.is_file()

    if baseline_months < 1:
        issues.append(f"baseline-months must be >= 1, got {baseline_months}")
    if pct_threshold < 0:
        issues.append(f"pct-threshold must be >= 0, got {pct_threshold}")
    if abs_threshold < 0:
        issues.append(f"abs-threshold must be >= 0, got {abs_threshold}")

    if not input_exists:
        issues.append(f"Utility ledger input not found: {input_path}")
    elif not input_path.is_file():
        issues.append(f"Utility ledger input is not a file: {input_path}")
    else:
        try:
            rows, stats = load_rows_with_stats(input_path)
        except Exception as exc:  # noqa: BLE001
            input_readable = False
            issues.append(f"Utility ledger input could not be read: {exc}")

    monthly = compute_monthly(rows)
    report_rows = []
    if baseline_months >= 1 and pct_threshold >= 0 and abs_threshold >= 0:
        report_rows = compute_report(monthly, baseline_months, pct_threshold, abs_threshold)
    over_rows = [row for row in report_rows if row["status"] == "OVER"]
    over_rows.sort(key=lambda row: (row["delta_%"], row["delta_$"]), reverse=True)
    if over_rows:
        top = over_rows[0]
        issues.append(
            "Utility overage review required: "
            f"over_count={len(over_rows)} top_property={top['property']} top_month={top['month']} "
            f"delta_pct={top['delta_%']} delta_dollars={top['delta_$']}"
        )

    visible_ok = [
        "OK utility overage tracker input: "
        f"ledger_rows={stats['ledger_rows']} utility_rows={stats['utility_rows']} "
        f"report_rows={len(report_rows)} over_count={len(over_rows)}",
        "OK utility overage diagnostic boundary: "
        "no Baselane network call, CSV write, restart, sudo, OAuth, cleanup, or helper command",
    ]
    classification = "utility-overage-review" if issues else "ok"
    evidence = {
        "input_exists": input_exists,
        "input_readable": input_readable,
        "ledger_rows": stats["ledger_rows"],
        "utility_rows": stats["utility_rows"],
        "loaded_utility_row_count": stats["loaded_utility_row_count"],
        "report_row_count": len(report_rows),
        "over_count": len(over_rows),
    }
    classified_issues = classified_issue_records(
        issues,
        classification,
        input_path,
        baseline_months,
        pct_threshold,
        abs_threshold,
        evidence,
    )
    fields = remediation_fields(classification, input_path, baseline_months, pct_threshold, abs_threshold)
    report = {
        "generated_at": datetime.now().isoformat(),
        "status": "UTILITY_OVERAGE_REVIEW" if issues else "NO_REPLY",
        "classification": classification,
        "ok": visible_ok,
        "ok_state": not issues,
        "visible_ok": visible_ok,
        "ok_count": len(visible_ok),
        "issues": issues,
        "issue_count": len(issues),
        "issue_classes": [ISSUE_CLASS] if issues else [],
        "classified_issues": classified_issues,
        "advisory_count": 0,
        "review_required_count": len(classified_issues),
        "approval_required_count": sum(1 for issue in classified_issues if issue.get("requires_operator_approval")),
        "input": str(input_path),
        "input_exists": input_exists,
        "input_readable": input_readable,
        "output": str(output_path) if output_path else None,
        "top_output": str(top_output) if top_output else None,
        "trend_output": str(trend_output) if trend_output else None,
        "write_attempted": write_attempted,
        "report_write_attempted": write_attempted,
        "top_output_write_attempted": bool(write_attempted and top_output),
        "trend_output_write_attempted": bool(write_attempted and trend_output),
        "baseline_months": baseline_months,
        "pct_threshold": pct_threshold,
        "abs_threshold": abs_threshold,
        "ledger_rows": stats["ledger_rows"],
        "utility_rows": stats["utility_rows"],
        "loaded_utility_row_count": stats["loaded_utility_row_count"],
        "skipped_date_count": stats["skipped_date_count"],
        "skipped_amount_count": stats["skipped_amount_count"],
        "property_count": len({row["property"] for row in rows}),
        "monthly_bucket_count": len(monthly),
        "report_row_count": len(report_rows),
        "over_count": len(over_rows),
        "over_rows_bounded": over_rows[:10],
        "report_rows_bounded": report_rows[:25],
        "remediation": {"classification": fields["remediation_class"], **fields},
        **fields,
    }
    report["safe_review_command_count"] = sum(
        1 for issue in classified_issues if issue.get("review_command_safe_to_run_automatically")
    )
    report["valid_review_command_count"] = sum(
        1
        for issue in classified_issues
        if issue.get("review_command_safe_to_run_automatically") and issue.get("review_command_valid")
    )
    report["invalid_review_command_count"] = sum(
        1
        for issue in classified_issues
        if issue.get("review_command_safe_to_run_automatically") and not issue.get("review_command_valid")
    )
    report["review_command_validation_issues"] = [
        validation_issue
        for issue in classified_issues
        if issue.get("review_command_safe_to_run_automatically") and not issue.get("review_command_valid")
        for validation_issue in ((issue.get("review_command_validation") or {}).get("issues") or [])
    ]
    report["classified_issue_summary"] = classified_issue_summary(report)
    return report


def write_csv_outputs(report_rows, monthly, output: str | Path, top_output: str | Path | None = None, trend_output: str | Path | None = None) -> None:
    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for row in report_rows:
            writer.writerow(row)

    if top_output:
        over = [r for r in report_rows if r["status"] == "OVER"]
        over.sort(key=lambda r: (r["delta_%"], r["delta_$"]), reverse=True)
        with open(top_output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TOP_FIELDS)
            writer.writeheader()
            for row in over:
                writer.writerow({k: row[k] for k in TOP_FIELDS})

    if trend_output:
        trend = compute_trend(monthly)
        with open(trend_output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TREND_FIELDS)
            writer.writeheader()
            for row in trend:
                writer.writerow(row)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Utility overage tracker")
    parser.add_argument("--json", action="store_true", help="Emit a read-only diagnostic and do not write CSV reports")
    parser.add_argument("--input", required=True, help="Path to ECO Systems General Ledger.csv")
    parser.add_argument("--output", help="Output CSV report path")
    parser.add_argument("--top-output", help="Optional: path for severity-sorted OVER rows")
    parser.add_argument("--trend-output", help="Optional: path for per-property monthly totals")
    parser.add_argument("--baseline-months", type=int, default=12)
    parser.add_argument("--pct-threshold", type=float, default=25.0)
    parser.add_argument("--abs-threshold", type=float, default=50.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    output = stdout or sys.stdout

    if args.json:
        report = build_report(
            input_path=args.input,
            output_path=args.output,
            top_output=args.top_output,
            trend_output=args.trend_output,
            baseline_months=args.baseline_months,
            pct_threshold=args.pct_threshold,
            abs_threshold=args.abs_threshold,
            write_attempted=False,
        )
        print(json.dumps(report, indent=2, sort_keys=True), file=output)
        return 0 if report["status"] == "NO_REPLY" else 1

    if not args.output:
        raise SystemExit("--output is required unless --json is used")

    rows = load_rows(args.input)
    monthly = compute_monthly(rows)
    report = compute_report(monthly, args.baseline_months, args.pct_threshold, args.abs_threshold)
    write_csv_outputs(report, monthly, args.output, args.top_output, args.trend_output)

    print(f"wrote {args.output} ({len(report)} rows)", file=output)
    if args.top_output:
        print(f"wrote {args.top_output} (OVER only, sorted by severity)", file=output)
    if args.trend_output:
        print(f"wrote {args.trend_output} (per-property monthly totals)", file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
