#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

import requests

# Usage:
# APP_CHECK=<x-firebase-appcheck> BSESSION=<__Host-BSESSION> python3 baselane_export_ledger.py

URL = "https://orchestration.baselane.com/graphql"
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", str(Path.home() / ".openclaw" / "workspace")))


def first_existing_dir(candidates: list[Path], fallback: Path) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return fallback


DROPBOX_ROOT = Path(os.environ["DROPBOX_ROOT"]) if os.environ.get("DROPBOX_ROOT") else first_existing_dir(
    [
        Path("/mnt/c/Users/digit/Dropbox"),
        Path("/data/Dropbox"),
        Path.home() / "Dropbox",
        Path("/home/digit/Dropbox"),
    ],
    Path("/mnt/c/Users/digit/Dropbox"),
)
TRACKER_DIR = Path(os.environ.get("TRACKER_DIR", str(DROPBOX_ROOT / "Projects/assetrail")))
OUT_PATH = TRACKER_DIR / "ECO Systems General Ledger.csv"
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", str(WORKSPACE_ROOT / "reports")))
GUARD_REPORT_PATH = REPORTS_DIR / "baselane_export_guard_last.json"
ALERT_PATH = REPORTS_DIR / "baselane_weekly_alerts.txt"
TIMEOUT = int(os.environ.get("BASELANE_HTTP_TIMEOUT_SECONDS", "60"))
EXPECTED_SELECTED = int(os.environ.get("BASELANE_EXPECTED_SELECTED", "0"))
MIN_ROWS = int(os.environ.get("BASELANE_MIN_ROWS", "6000"))
MAX_ROWS = int(os.environ.get("BASELANE_MAX_ROWS", "25000"))
PAGE_LIMIT = int(os.environ.get("BASELANE_PAGE_LIMIT", "500"))
ISSUE_CLASS = "baselane-export-ledger"
SCRIPT_PATH = Path(__file__).resolve()


def diagnostic_command() -> str:
    return f"python3 {SCRIPT_PATH} --json"


DIAGNOSTIC_COMMAND = diagnostic_command()

EXCLUDE_RAW = {
    "1 Coolwood Dr",
    "3880 Dover St.",
    "3880 Dover St",
    "Crypto Investments",
    "Dome",
    "EVCO Holdings",
    "Mining, Sales, Consulting, and PM",
    "Mining, Sales, Consulting, & PM",
    "NARWALL Holdings",
    "Personal",
    "Vehicles",
}

FIELDS = [
    "Account",
    "Date",
    "Merchant",
    "Description",
    "Amount",
    "Type",
    "Category",
    "Sub-category",
    "Property",
    "Unit",
    "Notes",
]


def normalize_name(value: str) -> str:
    text = (value or "").strip().lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


EXCLUDE_NORM = {normalize_name(item) for item in EXCLUDE_RAW}
ECO_SOURCE_PROPERTY_NORM = normalize_name("Mining, Sales, Consulting, and PM")
ECO_ACCRUAL_NOTE = re.compile(
    r"^AOPS-(?:(?:MONTHLY|OHIL|PAU|PNL)-ACCRUAL|PM-FEE)"
    r"\|(dao_eco|pm_eco)\|([^|]+)\|\d{4}-\d{2}"
    r"\|(-?\d+(?:\.\d{1,2})?)(?:\s|\||$)"
)
EXCLUDE_TOKEN_RULES = {
    normalize_name("1 Coolwood Dr"): ["1", "coolwood"],
    normalize_name("3880 Dover St."): ["3880", "dover"],
    normalize_name("Crypto Investments"): ["crypto", "investments"],
    normalize_name("Dome"): ["dome"],
    normalize_name("EVCO Holdings"): ["evco", "holdings"],
    normalize_name("Mining, Sales, Consulting, and PM"): ["mining", "sales", "consulting", "pm"],
    normalize_name("NARWALL Holdings"): ["narwall", "holdings"],
    normalize_name("Personal"): ["personal"],
    normalize_name("Vehicles"): ["vehicles"],
}


def eco_accrual_target_property(row: dict[str, Any]) -> str:
    """Return the target for an exact, balanced ECO-side accrual row."""
    marker = ECO_ACCRUAL_NOTE.match(str(row.get("Notes") or "").strip())
    if not marker:
        return ""
    kind, target, marker_amount = marker.groups()
    try:
        amount_matches = abs(float(row.get("Amount")) - float(marker_amount)) <= 0.001
    except (TypeError, ValueError):
        return ""
    expected_prefix = (
        "ECO Systems LLC DAO Registration Fee Revenue | "
        if kind == "dao_eco"
        else "ECO Systems LLC PM Fee Revenue | "
    )
    description = str(row.get("Description") or "").strip()
    if (
        not amount_matches
        or str(row.get("Type") or "").strip() != "Revenue"
        or str(row.get("Category") or "").strip() != "Fees & Other Revenue"
        or not str(row.get("Merchant") or "").strip().startswith(expected_prefix)
        or (description and not description.startswith(expected_prefix))
    ):
        return ""
    return target.strip()


def remediation_fields(classification: str) -> dict[str, Any]:
    has_issues = classification != "ok"
    return {
        "remediation_class": "operator-reviewed-baselane-export-ledger" if has_issues else "no-remediation-needed",
        "requires_operator_approval": has_issues,
        "requires_interactive_sudo": False,
        "requires_interactive_oauth": False,
        "safe_to_run_automatically": not has_issues,
        "review_command": diagnostic_command(),
        "review_command_safe_to_run_automatically": True,
        "cleanup_command_after_review": None,
        "restart_command_after_review": None,
        "oauth_command_after_review": None,
        "helper_command_after_review": None,
    }


def review_command_validation(command: object | None = None) -> dict[str, Any]:
    command_text = str(command if command is not None else diagnostic_command())
    try:
        parts = shlex.split(command_text)
    except ValueError as exc:
        parts = []
        parse_issue = str(exc)
    else:
        parse_issue = None
    expected_path = str(SCRIPT_PATH)
    script_exists = SCRIPT_PATH.exists()
    script_is_file = SCRIPT_PATH.is_file()
    issues: list[str] = []
    if parse_issue:
        issues.append(f"command parse failed: {parse_issue}")
    if not parts or parts[0] != "python3":
        issues.append("review command must start with python3")
    if expected_path not in parts:
        issues.append(f"review command must target {expected_path}")
    if "--json" not in parts:
        issues.append("review command must include --json")
    if not script_exists:
        issues.append(f"review command script is missing: {expected_path}")
    elif not script_is_file:
        issues.append(f"review command path is not a file: {expected_path}")
    return {
        "command": command_text,
        "expected_script_path": expected_path,
        "script_exists": script_exists,
        "script_is_file": script_is_file,
        "path": expected_path,
        "path_exists": script_exists,
        "python3_present": bool(parts and parts[0] == "python3"),
        "script_path_present": expected_path in parts,
        "json_flag_present": "--json" in parts,
        "requires_executable": False,
        "valid": not issues,
        "issues": issues,
        "issue": issues[0] if issues else None,
    }


def classified_issue_records(issues: list[str], evidence: dict[str, Any], classification: str) -> list[dict[str, Any]]:
    fields = remediation_fields(classification)
    review_validation = review_command_validation(fields.get("review_command"))
    return [
        {
            "issue": issue,
            "issue_class": ISSUE_CLASS,
            "classification": classification,
            "area": "baselane-export-ledger",
            "app_check_present": evidence.get("app_check_present"),
            "bsession_present": evidence.get("bsession_present"),
            "tracker_parent_exists": evidence.get("tracker_parent_exists"),
            "tracker_parent_writable": evidence.get("tracker_parent_writable"),
            "reports_parent_exists": evidence.get("reports_parent_exists"),
            "reports_parent_writable": evidence.get("reports_parent_writable"),
            "min_rows": evidence.get("min_rows"),
            "max_rows": evidence.get("max_rows"),
            "review_command_valid": review_validation["valid"],
            "review_command_validation": review_validation,
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
    review_validation_issues = [
        issue.get("review_command_validation")
        for issue in classified
        if issue.get("review_command_safe_to_run_automatically")
        and not issue.get("review_command_valid")
    ]
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
        "valid_review_command_count": sum(1 for issue in classified if issue.get("review_command_valid")),
        "invalid_review_command_count": sum(
            1
            for issue in classified
            if issue.get("review_command_safe_to_run_automatically")
            and not issue.get("review_command_valid")
        ),
        "review_command_validation_issues": review_validation_issues,
        "safe_to_run_automatically": report.get("safe_to_run_automatically") is True,
        "app_check_present": report.get("app_check_present") is True,
        "bsession_present": report.get("bsession_present") is True,
        "tracker_dir_exists": report.get("tracker_dir_exists") is True,
        "tracker_parent_exists": report.get("tracker_parent_exists") is True,
        "tracker_parent_writable": report.get("tracker_parent_writable") is True,
        "reports_dir_exists": report.get("reports_dir_exists") is True,
        "reports_parent_exists": report.get("reports_parent_exists") is True,
        "reports_parent_writable": report.get("reports_parent_writable") is True,
        "canonical_exists": report.get("canonical_exists") is True,
        "network_attempted": report.get("network_attempted") is True,
        "csv_write_attempted": report.get("csv_write_attempted") is True,
        "guard_write_attempted": report.get("guard_write_attempted") is True,
        "alert_write_attempted": report.get("alert_write_attempted") is True,
        "remediation_class": report.get("remediation_class"),
        "cleanup_command_available_after_review": bool(report.get("cleanup_command_after_review")),
        "restart_command_available_after_review": bool(report.get("restart_command_after_review")),
        "oauth_command_available_after_review": bool(report.get("oauth_command_after_review")),
        "helper_command_available_after_review": bool(report.get("helper_command_after_review")),
    }


def build_report(
    env: dict[str, str] | None = None,
    tracker_dir: Path = TRACKER_DIR,
    reports_dir: Path = REPORTS_DIR,
    out_path: Path | None = None,
    expected_selected: int = EXPECTED_SELECTED,
    min_rows: int = MIN_ROWS,
    max_rows: int = MAX_ROWS,
) -> dict[str, Any]:
    env = env if env is not None else os.environ
    out_path = out_path or tracker_dir / "ECO Systems General Ledger.csv"
    issues: list[str] = []
    visible_ok: list[str] = []

    app_check_present = bool((env.get("APP_CHECK") or "").strip())
    bsession_present = bool((env.get("BSESSION") or "").strip())
    tracker_parent = out_path.parent
    tracker_parent_exists = tracker_parent.exists()
    tracker_parent_writable = os.access(tracker_parent, os.W_OK) if tracker_parent_exists else False
    reports_parent = reports_dir.parent
    reports_parent_exists = reports_parent.exists()
    reports_parent_writable = os.access(reports_parent, os.W_OK) if reports_parent_exists else False

    if not app_check_present:
        issues.append("APP_CHECK is not present in runtime env")
    if not bsession_present:
        issues.append("BSESSION is not present in runtime env")
    if not tracker_parent_exists:
        issues.append(f"ledger output parent does not exist: {tracker_parent}")
    elif not tracker_parent_writable:
        issues.append(f"ledger output parent is not writable: {tracker_parent}")
    if not reports_parent_exists:
        issues.append(f"reports parent does not exist: {reports_parent}")
    elif not reports_parent_writable:
        issues.append(f"reports parent is not writable: {reports_parent}")
    if min_rows < 0:
        issues.append(f"BASELANE_MIN_ROWS must be non-negative: {min_rows}")
    if max_rows < min_rows:
        issues.append(f"BASELANE_MAX_ROWS must be >= BASELANE_MIN_ROWS: {max_rows} < {min_rows}")
    if expected_selected < 0:
        issues.append(f"BASELANE_EXPECTED_SELECTED must be non-negative: {expected_selected}")

    if not issues:
        visible_ok.append(
            "OK Baselane ledger export config: "
            f"canonical={out_path} min_rows={min_rows} max_rows={max_rows}"
        )
        visible_ok.append(
            "OK Baselane ledger export diagnostic: "
            "no Baselane network call, CSV write, guard write, alert write, restart, sudo, OAuth, cleanup, or helper command"
        )

    classification = "baselane-export-ledger-review" if issues else "ok"
    evidence = {
        "app_check_present": app_check_present,
        "bsession_present": bsession_present,
        "tracker_parent_exists": tracker_parent_exists,
        "tracker_parent_writable": tracker_parent_writable,
        "reports_parent_exists": reports_parent_exists,
        "reports_parent_writable": reports_parent_writable,
        "min_rows": min_rows,
        "max_rows": max_rows,
    }
    classified_issues = classified_issue_records(issues, evidence, classification)
    fields = remediation_fields(classification)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "BASELANE_EXPORT_LEDGER_REVIEW" if issues else "NO_REPLY",
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
        "url_host": "orchestration.baselane.com",
        "app_check_present": app_check_present,
        "bsession_present": bsession_present,
        "workspace_root": str(WORKSPACE_ROOT),
        "tracker_dir": str(tracker_dir),
        "tracker_dir_exists": tracker_dir.exists(),
        "tracker_parent_exists": tracker_parent_exists,
        "tracker_parent_writable": tracker_parent_writable,
        "canonical_path": str(out_path),
        "canonical_exists": out_path.exists(),
        "reports_dir": str(reports_dir),
        "reports_dir_exists": reports_dir.exists(),
        "reports_parent_exists": reports_parent_exists,
        "reports_parent_writable": reports_parent_writable,
        "guard_report_path": str(reports_dir / "baselane_export_guard_last.json"),
        "alert_path": str(reports_dir / "baselane_weekly_alerts.txt"),
        "expected_selected": expected_selected if expected_selected > 0 else None,
        "min_rows": min_rows,
        "max_rows": max_rows,
        "excluded_property_name_count": len(EXCLUDE_RAW),
        "network_attempted": False,
        "csv_write_attempted": False,
        "guard_write_attempted": False,
        "alert_write_attempted": False,
        "remediation": {"classification": fields["remediation_class"], **fields},
        **fields,
    }
    report["classified_issue_summary"] = classified_issue_summary(report)
    summary = report["classified_issue_summary"]
    report["safe_review_command_count"] = summary["safe_review_command_count"]
    report["valid_review_command_count"] = summary["valid_review_command_count"]
    report["invalid_review_command_count"] = summary["invalid_review_command_count"]
    report["review_command_validation_issues"] = summary["review_command_validation_issues"]
    return report


def append_alert(line: str, alert_path: Path = ALERT_PATH) -> None:
    with alert_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now().isoformat()}] {line}\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        # Exclude Account column from outputs (privacy)
        filtered_fields = [f for f in FIELDS if f != "Account"]
        filtered_rows = [{k: v for k, v in row.items() if k != "Account"} for row in rows]
        writer = csv.DictWriter(handle, fieldnames=filtered_fields)
        writer.writeheader()
        writer.writerows(filtered_rows)


def gql(
    operation_name: str,
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    app_check: str,
    bsession: str,
    url: str = URL,
    timeout: int = TIMEOUT,
) -> dict[str, Any]:
    payload = {"operationName": operation_name, "variables": variables or {}, "query": query}
    response = requests.post(
        url,
        headers={
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://app.baselane.com",
            "referer": "https://app.baselane.com/",
            "x-firebase-appcheck": app_check,
        },
        cookies={"__Host-BSESSION": bsession},
        data=json.dumps(payload),
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL {operation_name} errors: {body['errors']}")
    return body.get("data", {})


def selected_property_context(props: list[dict[str, Any]], expected_selected: int) -> dict[str, Any]:
    excluded_ids = {
        str(prop["id"])
        for prop in props
        if normalize_name(prop.get("name") or "") in EXCLUDE_NORM
    }
    selected_count = len(props) - len(excluded_ids)
    autocorrect_applied = False
    autocorrect_added: list[dict[str, Any]] = []

    if expected_selected > 0 and selected_count != expected_selected:
        for prop in props:
            prop_id = str(prop["id"])
            if prop_id in excluded_ids:
                continue
            prop_norm = normalize_name(prop.get("name") or "")
            for target_norm, tokens in EXCLUDE_TOKEN_RULES.items():
                if all(token in prop_norm for token in tokens):
                    excluded_ids.add(prop_id)
                    autocorrect_applied = True
                    autocorrect_added.append(
                        {
                            "property_id": prop_id,
                            "property_name": prop.get("name") or "",
                            "matched_rule": target_norm,
                            "tokens": tokens,
                        }
                    )
                    break
        selected_count = len(props) - len(excluded_ids)

    return {
        "excluded_ids": excluded_ids,
        "selected_count": selected_count,
        "selected_property_ids": {str(prop["id"]) for prop in props if str(prop["id"]) not in excluded_ids},
        "autocorrect_applied": autocorrect_applied,
        "autocorrect_added": autocorrect_added,
    }


QUERY = """query Transactions($input: SortsAndFilters) {
  transactions(input: $input) {
    total
    data {
      description
      bankAccountId
      amount
      merchantName
      name
      pending
      time
      hidden
      isDeleted
      isExternal
      isManual
      isSplit
      parentId
      isReviewedByUser
      tagIdSource
      propertyTagIdSource
      tagRuleId
      propertyRuleId
      originalTransaction
      isDocumentUploaded
      linkedAssetId
      linkedLoanId
      id
      tagId
      date
      propertyId
      unitId
      note
    }
  }
}
"""


def run_export(
    *,
    app_check: str | None = None,
    bsession: str | None = None,
    tracker_dir: Path = TRACKER_DIR,
    reports_dir: Path = REPORTS_DIR,
    out_path: Path | None = None,
    guard_report_path: Path | None = None,
    expected_selected: int = EXPECTED_SELECTED,
    min_rows: int = MIN_ROWS,
    max_rows: int = MAX_ROWS,
    gql_func: Callable[[str, str, dict[str, Any] | None], dict[str, Any]] | None = None,
    stdout: TextIO | None = None,
) -> int:
    app_check = app_check if app_check is not None else os.environ.get("APP_CHECK", "")
    bsession = bsession if bsession is not None else os.environ.get("BSESSION", "")
    if not app_check or not bsession:
        raise RuntimeError("Set APP_CHECK and BSESSION env vars")

    out = stdout or sys.stdout
    out_path = out_path or tracker_dir / "ECO Systems General Ledger.csv"
    guard_report_path = guard_report_path or reports_dir / "baselane_export_guard_last.json"
    reports_dir.mkdir(parents=True, exist_ok=True)

    def call_gql(operation_name: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if gql_func is not None:
            return gql_func(operation_name, query, variables)
        return gql(operation_name, query, variables, app_check=app_check or "", bsession=bsession or "")

    props = call_gql("PropertyList", "query PropertyList { property { id name address } }").get("property", [])
    prop_map = {str(prop["id"]): prop.get("name", "") for prop in props}
    selection = selected_property_context(props, expected_selected)
    excluded_ids = selection["excluded_ids"]
    selected_property_ids = selection["selected_property_ids"]

    tags = call_gql("TagList", "query TagList { tag { type subType { id name } } }").get("tag", [])
    tag_map = {}
    for tag in tags:
        for subtype in tag.get("subType", []):
            tag_map[str(subtype["id"])] = (tag.get("type", ""), subtype.get("name", ""))

    all_rows: list[dict[str, Any]] = []
    filtered_rows: list[dict[str, Any]] = []
    fetched_transaction_ids: set[str] = set()
    duplicate_transaction_ids: set[str] = set()
    page = 1
    limit = PAGE_LIMIT
    fetched_total = 0
    dropped_excluded_property_rows = 0
    dropped_no_property_rows = 0
    dropped_non_selected_rows = 0
    dropped_unknown_property_rows = 0
    unknown_property_name_rows = 0
    included_eco_accrual_counterpart_rows = 0
    dropped_excluded_eco_accrual_target_rows = 0

    while True:
        variables = {
            "input": {
                # Date alone is not a stable pagination key when a daily sync
                # creates more than one page of same-day manual transactions.
                # Baselane accepts id ordering, which prevents page drift and
                # silently omitted/duplicated 200-row windows.
                "sort": {"field": "id", "direction": "DESC"},
                "filter": {
                    "isHidden": False,
                    "search": "",
                    "isCategorized": None,
                    "tagId": None,
                    "bankAccountId": None,
                    "propertyId": None,
                    "unitId": None,
                    "isDeleted": False,
                    "isDocumentUploaded": None,
                },
                "page": page,
                "pageLimit": limit,
            }
        }

        data = call_gql("Transactions", QUERY, variables).get("transactions", {})
        txs = data.get("data", [])
        if not txs:
            break

        fetched_total += len(txs)
        for tx in txs:
            transaction_id = str(tx.get("id") or "")
            if transaction_id in fetched_transaction_ids:
                duplicate_transaction_ids.add(transaction_id)
            fetched_transaction_ids.add(transaction_id)
            property_id = str(tx.get("propertyId")) if tx.get("propertyId") is not None else None
            prop_name = prop_map.get(property_id, "") if property_id else ""
            tag_id = str(tx.get("tagId")) if tx.get("tagId") is not None else None
            trans_type, trans_subtype = tag_map.get(tag_id, ("", ""))
            date_str = ""
            if tx.get("date"):
                date_str = datetime.strptime(tx["date"], "%Y-%m-%d").strftime("%B %d, %Y")

            merchant = tx.get("merchantName") or ""
            desc = tx.get("description") or tx.get("name") or merchant
            notes = ""
            if isinstance(tx.get("note"), dict):
                notes = tx["note"].get("text", "")
            elif tx.get("note"):
                notes = str(tx.get("note"))

            row = {
                # Account details are intentionally excluded by write_csv().
                # Avoid one redundant BankAccount GraphQL request per account;
                # those lookups add latency and can stall an otherwise complete
                # transaction export without contributing to either output.
                "Account": "",
                "Date": date_str,
                "Merchant": merchant,
                "Description": desc,
                "Amount": tx.get("amount"),
                "Type": trans_type,
                "Category": trans_subtype,
                "Sub-category": "",
                "Property": prop_name or "",
                "Unit": "",
                "Notes": notes,
            }
            all_rows.append(row)

            eco_target = ""
            if normalize_name(prop_name) == ECO_SOURCE_PROPERTY_NORM:
                eco_target = eco_accrual_target_property(row)
            if eco_target:
                if normalize_name(eco_target) in EXCLUDE_NORM:
                    dropped_excluded_eco_accrual_target_rows += 1
                    continue
                out_row = dict(row)
                out_row["Property"] = eco_target
                filtered_rows.append(out_row)
                included_eco_accrual_counterpart_rows += 1
                continue

            if property_id is None:
                dropped_no_property_rows += 1
                continue
            if property_id in excluded_ids or normalize_name(prop_name) in EXCLUDE_NORM:
                dropped_excluded_property_rows += 1
                continue
            if property_id not in selected_property_ids:
                dropped_non_selected_rows += 1
                continue
            if not prop_name:
                dropped_unknown_property_rows += 1
                prop_name = f"UNKNOWN_PROPERTY_{property_id}"
                unknown_property_name_rows += 1

            out_row = dict(row)
            out_row["Property"] = prop_name
            filtered_rows.append(out_row)

        total = data.get("total")
        if isinstance(total, int) and fetched_total >= total:
            break
        page += 1

    blank_property_rows = sum(1 for row in filtered_rows if not (row.get("Property") or "").strip())
    excluded_property_rows_in_output = sum(
        1 for row in filtered_rows if normalize_name(row.get("Property") or "") in EXCLUDE_NORM
    )
    unique_props = len({(row.get("Property") or "").strip() for row in filtered_rows if (row.get("Property") or "").strip()})
    now = datetime.now()
    ts = now.strftime("%Y%m%d-%H%M%S")
    tmp_path = out_path.with_name(f".{out_path.name}.tmp.{ts}.csv")
    backup_path = out_path.with_name(f"ECO Systems General Ledger.{ts}.bak.csv")
    snapshot_path = out_path.with_name(f"ECO Systems General Ledger.filtered.{ts}.csv")
    all_snapshot_path = reports_dir / f"baselane_export_all_transactions.{ts}.csv"
    filtered_preview_path = reports_dir / f"baselane_export_filtered_preview.{ts}.csv"

    write_csv(all_snapshot_path, all_rows)
    write_csv(filtered_preview_path, filtered_rows)

    violations = []
    if len(filtered_rows) < min_rows:
        violations.append(f"row_count_below_min:{len(filtered_rows)}<{min_rows}")
    if len(filtered_rows) > max_rows:
        violations.append(f"row_count_above_max:{len(filtered_rows)}>{max_rows}")
    if blank_property_rows > 0:
        violations.append(f"blank_property_rows:{blank_property_rows}")
    if excluded_property_rows_in_output > 0:
        violations.append(f"excluded_property_rows_in_output:{excluded_property_rows_in_output}")
    if unknown_property_name_rows > 0:
        violations.append(f"unknown_property_name_rows:{unknown_property_name_rows}")
    if duplicate_transaction_ids:
        violations.append(
            f"duplicate_transaction_ids:{len(duplicate_transaction_ids)}"
        )
    if fetched_total != len(fetched_transaction_ids):
        violations.append(
            "fetched_transaction_id_count_mismatch:"
            f"{fetched_total}!={len(fetched_transaction_ids)}"
        )

    guard_payload = {
        "ok": len(violations) == 0,
        "timestamp": now.isoformat(),
        "expected_selected": expected_selected if expected_selected > 0 else None,
        "actual_selected": selection["selected_count"],
        "property_count_warning": (
            f"selected_property_count_mismatch expected={expected_selected} actual={selection['selected_count']}"
            if expected_selected > 0 and selection["selected_count"] != expected_selected
            else None
        ),
        "selected_property_ids_count": len(selected_property_ids),
        "total_properties": len(props),
        "excluded_properties_matched": len(excluded_ids),
        "fetched_total_rows": fetched_total,
        "unique_fetched_transaction_ids": len(fetched_transaction_ids),
        "duplicate_transaction_id_count": len(duplicate_transaction_ids),
        "all_rows_exported_local": len(all_rows),
        "dropped_excluded_property_rows": dropped_excluded_property_rows,
        "dropped_no_property_rows": dropped_no_property_rows,
        "dropped_non_selected_rows": dropped_non_selected_rows,
        "dropped_unknown_property_rows": dropped_unknown_property_rows,
        "included_eco_accrual_counterpart_rows": included_eco_accrual_counterpart_rows,
        "dropped_excluded_eco_accrual_target_rows": dropped_excluded_eco_accrual_target_rows,
        "output_rows": len(filtered_rows),
        "unique_output_properties": unique_props,
        "blank_property_rows": blank_property_rows,
        "excluded_property_rows_in_output": excluded_property_rows_in_output,
        "unknown_property_name_rows": unknown_property_name_rows,
        "autocorrect_applied": selection["autocorrect_applied"],
        "autocorrect_added": selection["autocorrect_added"],
        "all_export_snapshot": str(all_snapshot_path),
        "filtered_preview_snapshot": str(filtered_preview_path),
        "filtered_snapshot": str(snapshot_path),
        "canonical_path": str(out_path),
        "violations": violations,
    }
    guard_report_path.write_text(json.dumps(guard_payload, indent=2), encoding="utf-8")

    if violations:
        raise RuntimeError("Guard failed: " + "; ".join(violations))

    try:
        shutil.copy2(filtered_preview_path, tmp_path)
        shutil.copy2(tmp_path, snapshot_path)
        if out_path.exists():
            shutil.copy2(out_path, backup_path)
        os.replace(tmp_path, out_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    print(f"WROTE {out_path} rows={len(filtered_rows)} unique_props={unique_props}", file=out)
    print(f"ALL_EXPORT_SNAPSHOT {all_snapshot_path} rows={len(all_rows)}", file=out)
    print(f"FILTERED_PREVIEW {filtered_preview_path} rows={len(filtered_rows)}", file=out)
    print(f"FILTERED_SNAPSHOT {snapshot_path}", file=out)
    if backup_path.exists():
        print(f"BACKUP {backup_path}", file=out)
    print(f"GUARD_REPORT {guard_report_path}", file=out)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run or inspect the Baselane ledger export")
    parser.add_argument("--json", action="store_true", help="Emit a read-only diagnostic report and do not call Baselane or write files")
    parser.add_argument("--tracker-dir", default=str(TRACKER_DIR), help="Ledger output directory")
    parser.add_argument("--reports-dir", default=str(REPORTS_DIR), help="Report output directory")
    parser.add_argument("--out-path", default=None, help="Canonical ledger CSV output path")
    parser.add_argument("--expected-selected", type=int, default=EXPECTED_SELECTED)
    parser.add_argument("--min-rows", type=int, default=MIN_ROWS)
    parser.add_argument("--max-rows", type=int, default=MAX_ROWS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    tracker_dir = Path(args.tracker_dir)
    reports_dir = Path(args.reports_dir)
    out_path = Path(args.out_path) if args.out_path else tracker_dir / "ECO Systems General Ledger.csv"
    if args.json:
        report = build_report(
            tracker_dir=tracker_dir,
            reports_dir=reports_dir,
            out_path=out_path,
            expected_selected=args.expected_selected,
            min_rows=args.min_rows,
            max_rows=args.max_rows,
        )
        print(json.dumps(report, indent=2, sort_keys=True), file=stdout or sys.stdout)
        return 0 if report["status"] == "NO_REPLY" else 1

    return run_export(
        tracker_dir=tracker_dir,
        reports_dir=reports_dir,
        out_path=out_path,
        expected_selected=args.expected_selected,
        min_rows=args.min_rows,
        max_rows=args.max_rows,
        stdout=stdout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
