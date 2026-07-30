#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shlex
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO


ISSUE_CLASS = "baselane-weekly-unprocessed"
SCRIPT_PATH = Path(__file__).absolute()
DEFAULT_WORKSPACE = SCRIPT_PATH.parents[1]
CSV_FIELDS = [
    "scope",
    "key",
    "ledger_key",
    "Date",
    "Amount",
    "Merchant",
    "Description",
    "Account",
    "Property",
    "Type",
    "Category",
    "Notes",
    "unprocessed_hint",
]
DUPLICATE_REVIEW_FIELDS = [
    "fingerprint",
    "review_status",
    "key",
    "row_count",
    "extra_row_count",
    "candidate_scope_count",
    "candidate_scopes",
    "candidate_observed_scopes",
    "review_severity",
    "duplicate_shape",
    "all_rows_identical",
    "duplicate_difference_fields",
    "suggested_review_action",
    "Date",
    "Amount",
    "Merchant",
    "Description",
    "Account",
    "Property",
    "Type",
    "Category",
    "Notes",
    "row_numbers",
    "allowlist_reason",
    "allowlist_reviewed_by",
    "allowlist_reviewed_at",
]
ACCEPTED_DUPLICATE_DECISIONS = {"accepted_duplicate", "accepted_source_duplicate", "accepted_intentional_duplicate"}


def norm(value: str) -> str:
    return (value or "").strip().lower()


def contains_any(haystack: str, needles) -> bool:
    return any(n in haystack for n in needles)


def row_key(row: dict) -> str:
    parts = [
        row.get("Date", "").strip(),
        row.get("Amount", "").strip(),
        row.get("Merchant", "").strip(),
        row.get("Description", "").strip(),
        row.get("Account", "").strip(),
        row.get("Property", "").strip(),
    ]
    return "|".join(parts)


def candidate_row_key(row: dict) -> str:
    return "|".join([row_key(row), row.get("Notes", "").strip()])


def is_unprocessed_hint(row: dict) -> bool:
    cat = norm(row.get("Category", ""))
    ttype = norm(row.get("Type", ""))
    notes = norm(row.get("Notes", ""))

    if "[processed-weekly-pass]" in notes:
        return False

    if not cat:
        return True
    if cat in {"uncategorized", "unknown", "other", "other expenses", "general"}:
        return True
    if not ttype:
        return True
    return False


def scopes_for_row(row: dict):
    merchant = norm(row.get("Merchant", ""))
    description = norm(row.get("Description", ""))
    notes = norm(row.get("Notes", ""))
    account = norm(row.get("Account", ""))
    prop = norm(row.get("Property", ""))
    text = " ".join([merchant, description, notes])

    scopes = []

    if contains_any(text, ["holly hill", "mortgage", "pmi", "escrow"]):
        scopes.append("mortgage_split")

    if "morgan linen" in text:
        scopes.append("morgan_linen_split")

    if contains_any(text, ["amazon", "walmart"]) and contains_any(
        " ".join([account, prop]), ["madison"]
    ):
        scopes.append("madison_consumables_split")

    if "stripe" in text and (is_unprocessed_hint(row) or not prop):
        scopes.append("stripe_unmatched_payout")

    if contains_any(text, ["county waste", "netflix", "hulu", "spectrum"]) and contains_any(
        " ".join([account, prop]), ["madison"]
    ):
        scopes.append("shared_service_4way")

    return scopes


def stable_hash_for_candidates(candidates):
    material = "\n".join(
        sorted(f"{c['scope']}|{c['key']}" for c in candidates)
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def duplicate_record_fingerprint(record: dict[str, Any]) -> str:
    material = {
        "key": record.get("key") or "",
        "row_count": int(record.get("row_count") or 0),
        "candidate_scopes": record.get("candidate_scopes") or "",
        "row_numbers": record.get("row_numbers") or "",
        "review_severity": record.get("review_severity") or "",
    }
    payload = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


DIFF_REVIEW_FIELDS = ["Date", "Amount", "Merchant", "Description", "Account", "Property", "Type", "Category", "Notes"]


def canonical_duplicate_row(row: dict[str, Any]) -> str:
    material = {field: str(row.get(field) or "").strip() for field in CSV_FIELDS if field != "scope"}
    return json.dumps(material, sort_keys=True, separators=(",", ":"))


def exact_ledger_row_material(row: dict[str, Any], fieldnames: list[str]) -> str:
    material = {field: str(row.get(field) or "").strip() for field in fieldnames}
    return json.dumps(material, sort_keys=True, separators=(",", ":"))


def exact_deduped_ledger_rows(
    rows: list[dict[str, Any]],
    fieldnames: list[str],
) -> tuple[list[dict[str, Any]], int, int]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    duplicate_materials: set[str] = set()
    extra_row_count = 0
    for row in rows:
        material = exact_ledger_row_material(row, fieldnames)
        if material in seen:
            duplicate_materials.add(material)
            extra_row_count += 1
            continue
        seen.add(material)
        deduped.append(row)
    return deduped, len(duplicate_materials), extra_row_count


def duplicate_row_differences(grouped_rows: list[tuple[int, dict[str, Any]]]) -> dict[str, list[str]]:
    differences: dict[str, list[str]] = {}
    for field in DIFF_REVIEW_FIELDS:
        values: list[str] = []
        for _index, row in grouped_rows:
            value = str(row.get(field) or "").strip()
            if value not in values:
                values.append(value)
        if len(values) > 1:
            differences[field] = values
    return differences


def classify_duplicate_rows(grouped_rows: list[tuple[int, dict[str, Any]]], scopes: list[str]) -> tuple[str, bool, str, dict[str, list[str]]]:
    row_materials = [canonical_duplicate_row(row) for _index, row in grouped_rows]
    all_identical = len(set(row_materials)) <= 1
    candidate_scoped = bool(scopes)
    differences = duplicate_row_differences(grouped_rows)
    difference_fields = set(differences)
    if all_identical and candidate_scoped:
        return "exact_candidate_duplicate", True, "review_exact_source_duplicate_before_allowlist", differences
    if all_identical:
        return "exact_ledger_duplicate", True, "source_ledger_cleanup_or_allowlist", differences
    if difference_fields == {"Notes"} and candidate_scoped:
        return "notes_only_candidate_duplicate", False, "inspect_notes_difference_before_weekly_updates", differences
    if difference_fields == {"Notes"}:
        return "notes_only_ledger_duplicate", False, "inspect_notes_difference", differences
    if candidate_scoped:
        return "nonidentical_candidate_duplicate", False, "inspect_row_differences_before_weekly_updates", differences
    return "nonidentical_ledger_duplicate", False, "inspect_row_differences", differences


def load_duplicate_allowlist(path: Path) -> tuple[dict[str, dict[str, Any]], str | None]:
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {}, str(exc)
    raw_entries = data.get("entries") if isinstance(data, dict) else data
    entries: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_entries, list):
        return {}, "allowlist must be a JSON object with entries[] or a list"
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        fingerprint = str(entry.get("fingerprint") or "").strip()
        if fingerprint:
            entries[fingerprint] = entry
    return entries, None


def apply_duplicate_allowlist(
    records: list[dict[str, Any]],
    allowlist_entries: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    reviewed: list[dict[str, Any]] = []
    for record in records:
        fingerprint = duplicate_record_fingerprint(record)
        entry = allowlist_entries.get(fingerprint) or {}
        decision = str(entry.get("decision") or "").strip()
        accepted = decision in ACCEPTED_DUPLICATE_DECISIONS
        reviewed.append(
            {
                **record,
                "fingerprint": fingerprint,
                "review_status": "accepted" if accepted else "pending",
                "allowlist_decision": decision or None,
                "allowlist_reason": entry.get("reason") or "",
                "allowlist_reviewed_by": entry.get("reviewed_by") or "",
                "allowlist_reviewed_at": entry.get("reviewed_at") or "",
            }
        )
    return reviewed


def duplicate_allowlist_template(report: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for record in report.get("_duplicate_review_records") or report.get("duplicate_review_bounded") or []:
        if record.get("review_status") == "accepted":
            continue
        entries.append(
            {
                "fingerprint": record.get("fingerprint"),
                "key": record.get("key"),
                "review_severity": record.get("review_severity"),
                "duplicate_shape": record.get("duplicate_shape"),
                "all_rows_identical": record.get("all_rows_identical"),
                "duplicate_difference_fields": record.get("duplicate_difference_fields"),
                "duplicate_row_differences": record.get("duplicate_row_differences"),
                "suggested_review_action": record.get("suggested_review_action"),
                "candidate_scopes": record.get("candidate_scopes"),
                "candidate_observed_scopes": record.get("candidate_observed_scopes"),
                "row_count": record.get("row_count"),
                "row_numbers": record.get("row_numbers"),
                "decision": "pending",
                "reason": "",
                "reviewed_by": "",
                "reviewed_at": "",
            }
        )
    return {
        "version": 1,
        "generated_at": report.get("generated_at"),
        "instructions": "Copy reviewed entries into the allowlist file and set decision to accepted_duplicate only after confirming the duplicate transaction rows are intentional or harmless for weekly automation.",
        "accepted_decisions": sorted(ACCEPTED_DUPLICATE_DECISIONS),
        "source_duplicate_review_csv": report.get("duplicate_review_csv"),
        "entries": entries,
    }


def duplicate_review_markdown(report: dict[str, Any]) -> str:
    records = report.get("_duplicate_review_records") or report.get("duplicate_review_bounded") or []
    pending = [record for record in records if record.get("review_status") != "accepted"]
    candidate_pending = [record for record in pending if record.get("review_severity") == "candidate_duplicate"]
    ledger_pending = [record for record in pending if record.get("review_severity") == "ledger_duplicate"]
    lines = [
        f"# Baselane Weekly Duplicate Review — {report.get('iso_week')}",
        "",
        "Purpose: review duplicate ledger keys before destructive weekly automation.",
        "",
        "## Summary",
        f"- Pending duplicate keys: {report.get('duplicate_review_pending_count')}",
        f"- Candidate-impacting pending keys: {report.get('candidate_duplicate_pending_count')}",
        f"- Accepted duplicate keys: {report.get('duplicate_review_allowed_count')}",
        f"- Allowlist file: `{report.get('duplicate_allowlist_file')}`",
        f"- Allowlist template: `{report.get('duplicate_allowlist_template')}`",
        "",
        "## Review Rules",
        "- Review candidate-impacting duplicates first; they affect weekly automation scopes.",
        "- Only set a template entry decision to `accepted_duplicate` after confirming the duplicate rows are intentional or harmless.",
        "- Do not edit the source ledger from this packet; use it as a deterministic review ledger.",
        "",
        "## Candidate-Impacting Pending Duplicates",
    ]
    if candidate_pending:
        for index, record in enumerate(candidate_pending, start=1):
            lines.extend(
                [
                    f"- {index}. `{record.get('Date')}` `{record.get('Amount')}` — {record.get('Property') or 'Unknown property'}",
                    f"  - Scope: `{record.get('candidate_scopes') or 'unknown'}`",
                    f"  - Merchant: {record.get('Merchant') or ''}",
                    f"  - Description: {record.get('Description') or ''}",
                    f"  - Category/Type: {record.get('Category') or ''} / {record.get('Type') or ''}",
                    f"  - Row numbers: `{record.get('row_numbers')}`; row count: `{record.get('row_count')}`",
                    f"  - Duplicate shape: `{record.get('duplicate_shape')}`; all rows identical: `{record.get('all_rows_identical')}`; differing fields: `{record.get('duplicate_difference_fields') or ''}`",
                    f"  - Row differences: `{record.get('duplicate_row_differences') or {}}`",
                    f"  - Suggested review action: `{record.get('suggested_review_action')}`",
                    f"  - Fingerprint: `{record.get('fingerprint')}`",
                ]
            )
    else:
        lines.append("- None.")
    lines.extend(["", "## Ledger-Only Pending Duplicate Count", f"- {len(ledger_pending)}"])
    if ledger_pending:
        lines.extend(["", "## First Ledger-Only Pending Duplicates"])
        for index, record in enumerate(ledger_pending[:15], start=1):
            lines.extend(
                [
                    f"- {index}. `{record.get('Date')}` `{record.get('Amount')}` — {record.get('Property') or 'Unknown property'}",
                    f"  - Merchant: {record.get('Merchant') or ''}",
                    f"  - Observed scopes: `{record.get('candidate_observed_scopes') or ''}`",
                    f"  - Row numbers: `{record.get('row_numbers')}`; fingerprint: `{record.get('fingerprint')}`",
                    f"  - Duplicate shape: `{record.get('duplicate_shape')}`; differing fields: `{record.get('duplicate_difference_fields') or ''}`; suggested action: `{record.get('suggested_review_action')}`",
                ]
            )
    lines.append("")
    return "\n".join(lines)


def duplicate_review_records(
    rows: list[dict[str, Any]],
    duplicates: dict[str, int],
    candidates: list[dict[str, Any]],
    candidate_duplicate_scopes_by_ledger_key: dict[str, set[str]],
) -> list[dict[str, Any]]:
    rows_by_key: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, row in enumerate(rows, start=2):
        key = row_key(row)
        if key in duplicates:
            rows_by_key[key].append((index, row))

    observed_scopes_by_key: dict[str, set[str]] = defaultdict(set)
    for candidate in candidates:
        ledger_key = str(candidate.get("ledger_key") or candidate.get("key") or "")
        if ledger_key in duplicates:
            observed_scopes_by_key[ledger_key].add(str(candidate.get("scope") or ""))

    records: list[dict[str, Any]] = []
    for key in sorted(duplicates):
        grouped_rows = rows_by_key.get(key) or []
        first_row = grouped_rows[0][1] if grouped_rows else {}
        scopes = sorted(scope for scope in candidate_duplicate_scopes_by_ledger_key.get(key, set()) if scope)
        observed_scopes = sorted(scope for scope in observed_scopes_by_key.get(key, set()) if scope)
        duplicate_shape, all_rows_identical, suggested_review_action, row_differences = classify_duplicate_rows(grouped_rows, scopes)
        record = {
            "key": key,
            "row_count": len(grouped_rows),
            "extra_row_count": max(0, len(grouped_rows) - 1),
            "candidate_scope_count": len(scopes),
            "candidate_scopes": ",".join(scopes),
            "candidate_observed_scopes": ",".join(observed_scopes),
            "review_severity": "candidate_duplicate" if scopes else "ledger_duplicate",
            "duplicate_shape": duplicate_shape,
            "all_rows_identical": all_rows_identical,
            "duplicate_difference_fields": ",".join(sorted(row_differences)),
            "duplicate_row_differences": row_differences,
            "suggested_review_action": suggested_review_action,
            "Date": first_row.get("Date", ""),
            "Amount": first_row.get("Amount", ""),
            "Merchant": first_row.get("Merchant", ""),
            "Description": first_row.get("Description", ""),
            "Account": first_row.get("Account", ""),
            "Property": first_row.get("Property", ""),
            "Type": first_row.get("Type", ""),
            "Category": first_row.get("Category", ""),
            "Notes": first_row.get("Notes", ""),
            "row_numbers": ",".join(str(index) for index, _row in grouped_rows),
        }
        record["fingerprint"] = duplicate_record_fingerprint(record)
        record["review_status"] = "pending"
        records.append(record)
    return records


def diagnostic_command(ledger_path: Path, sample_limit: int) -> str:
    return " ".join(
        shlex.quote(part)
        for part in [
            "python3",
            str(SCRIPT_PATH),
            "--ledger",
            str(ledger_path),
            "--sample-limit",
            str(sample_limit),
            "--json",
        ]
    )


def review_command_validation(
    command: object | None = None,
    ledger_path: Path | None = None,
    sample_limit: int | None = None,
) -> dict[str, Any]:
    ledger_path = ledger_path or Path("")
    sample_limit = 50 if sample_limit is None else sample_limit
    command_text = str(command if command is not None else diagnostic_command(ledger_path, sample_limit))
    try:
        parts = shlex.split(command_text)
    except ValueError as exc:
        parts = []
        parse_issue = str(exc)
    else:
        parse_issue = None

    expected_script = str(SCRIPT_PATH)
    expected_ledger = str(ledger_path)
    script_exists = SCRIPT_PATH.exists()
    script_is_file = SCRIPT_PATH.is_file()
    issues: list[str] = []

    def value_after(flag: str) -> str | None:
        if flag not in parts:
            return None
        index = parts.index(flag)
        if index + 1 >= len(parts):
            return None
        return parts[index + 1]

    ledger_value = value_after("--ledger")
    sample_limit_value = value_after("--sample-limit")
    write_flags = sorted(set(parts).intersection({"--out-json", "--out-csv", "--state-file"}))

    if parse_issue:
        issues.append(f"command parse failed: {parse_issue}")
    if not parts or parts[0] != "python3":
        issues.append("review command must start with python3")
    if expected_script not in parts:
        issues.append(f"review command must target {expected_script}")
    if "--json" not in parts:
        issues.append("review command must include --json")
    if ledger_value != expected_ledger:
        issues.append(f"review command ledger must be {expected_ledger}")
    if sample_limit_value != str(sample_limit):
        issues.append(f"review command sample limit must be {sample_limit}")
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
        "json_flag_present": "--json" in parts,
        "python3_present": bool(parts) and parts[0] == "python3",
        "script_path_present": expected_script in parts,
        "path": expected_script,
        "path_exists": script_exists,
        "requires_executable": False,
        "ledger_flag_present": "--ledger" in parts,
        "ledger_value": ledger_value,
        "ledger_matches": ledger_value == expected_ledger,
        "sample_limit_flag_present": "--sample-limit" in parts,
        "sample_limit_value": sample_limit_value,
        "sample_limit_matches": sample_limit_value == str(sample_limit),
        "write_flags_present": write_flags,
        "valid": not issues,
        "issues": issues,
        "issue": issues[0] if issues else None,
    }


def remediation_fields(classification: str, ledger_path: Path, sample_limit: int) -> dict[str, Any]:
    has_issues = classification != "ok"
    command = diagnostic_command(ledger_path, sample_limit)
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
    ledger_path: Path,
    sample_limit: int,
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    fields = remediation_fields(classification, ledger_path, sample_limit)
    validation = review_command_validation(
        fields["review_command"],
        ledger_path=ledger_path,
        sample_limit=sample_limit,
    )
    return [
        {
            "issue": issue,
            "issue_class": ISSUE_CLASS,
            "classification": classification,
            "area": "baselane-weekly-unprocessed-report",
            "ledger": str(ledger_path),
            "ledger_exists": evidence.get("ledger_exists"),
            "ledger_readable": evidence.get("ledger_readable"),
            "state_file_exists": evidence.get("state_file_exists"),
            "state_file_readable": evidence.get("state_file_readable"),
            "ledger_rows": evidence.get("ledger_rows"),
        "candidate_count": evidence.get("candidate_count"),
        "candidate_duplicate_suppressed_count": evidence.get("candidate_duplicate_suppressed_count"),
        "duplicate_key_count": evidence.get("duplicate_key_count"),
            "duplicate_rows_total": evidence.get("duplicate_rows_total"),
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
        "ledger_exists": report.get("ledger_exists") is True,
        "ledger_readable": report.get("ledger_readable") is True,
        "state_file_exists": report.get("state_file_exists") is True,
        "state_file_readable": report.get("state_file_readable") is True,
        "write_attempted": report.get("write_attempted") is True,
        "state_write_attempted": report.get("state_write_attempted") is True,
        "ledger_rows": int(report.get("ledger_rows") or 0),
        "candidate_count": int(report.get("candidate_count") or 0),
        "candidate_duplicate_suppressed_count": int(report.get("candidate_duplicate_suppressed_count") or 0),
        "duplicate_key_count": int(report.get("duplicate_key_count") or 0),
        "duplicate_rows_total": int(report.get("duplicate_rows_total") or 0),
        "remediation_class": report.get("remediation_class"),
        "cleanup_command_available_after_review": bool(report.get("cleanup_command_after_review")),
        "restart_command_available_after_review": bool(report.get("restart_command_after_review")),
        "oauth_command_available_after_review": bool(report.get("oauth_command_after_review")),
        "helper_command_available_after_review": bool(report.get("helper_command_after_review")),
    }


def build_report_with_candidates(
    ledger_path: Path,
    out_json: Path,
    out_csv: Path,
    state_file: Path,
    duplicate_allowlist: Path | None = None,
    deduped_ledger_out: Path | None = None,
    sample_limit: int = 50,
    write_attempted: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ledger_path = Path(ledger_path)
    out_json = Path(out_json)
    out_csv = Path(out_csv)
    state_file = Path(state_file)
    duplicate_allowlist = Path(duplicate_allowlist) if duplicate_allowlist else state_file.with_name(".baselane_weekly_duplicate_allowlist.json")
    sample_limit = max(0, int(sample_limit))

    generated_at = datetime.now().isoformat()
    iso_week = datetime.now().strftime("%G-%V")
    issues: list[str] = []
    visible_ok: list[str] = []
    rows: list[dict[str, Any]] = []
    ledger_fieldnames: list[str] = []
    state_read_error = None
    previous: dict[str, Any] = {}
    allowlist_entries: dict[str, dict[str, Any]] = {}
    allowlist_error = None

    ledger_exists = ledger_path.exists()
    ledger_readable = ledger_path.is_file()
    if not ledger_exists:
        issues.append(f"Baselane weekly ledger not found: {ledger_path}")
    elif not ledger_path.is_file():
        issues.append(f"Baselane weekly ledger path is not a file: {ledger_path}")
    else:
        try:
            with ledger_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                ledger_fieldnames = list(reader.fieldnames or [])
                rows = list(reader)
        except Exception as exc:  # noqa: BLE001
            ledger_readable = False
            issues.append(f"Baselane weekly ledger could not be read: {exc}")

    state_file_exists = state_file.exists()
    state_file_readable = True
    if state_file_exists:
        try:
            previous = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            state_file_readable = False
            state_read_error = str(exc)
            issues.append(f"Baselane weekly state file could not be read: {exc}")

    allowlist_entries, allowlist_error = load_duplicate_allowlist(duplicate_allowlist)
    if allowlist_error:
        issues.append(f"Baselane weekly duplicate allowlist could not be read: {allowlist_error}")

    key_counter = Counter(row_key(r) for r in rows)
    duplicates = {k: c for k, c in key_counter.items() if c > 1}
    duplicate_rows_total = int(sum(duplicates.values()) - len(duplicates))
    deduped_rows, exact_duplicate_key_count, exact_duplicate_extra_row_count = exact_deduped_ledger_rows(
        rows,
        ledger_fieldnames,
    )

    candidates = []
    per_scope: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_candidate_keys: set[tuple[str, str]] = set()
    suppressed_candidate_duplicates: list[dict[str, Any]] = []
    for r in rows:
        ledger_key = row_key(r)
        key = candidate_row_key(r)
        for scope in scopes_for_row(r):
            scope_key = (scope, key)
            rec = {
                "scope": scope,
                "key": key,
                "ledger_key": ledger_key,
                "Date": r.get("Date", ""),
                "Amount": r.get("Amount", ""),
                "Merchant": r.get("Merchant", ""),
                "Description": r.get("Description", ""),
                "Account": r.get("Account", ""),
                "Property": r.get("Property", ""),
                "Type": r.get("Type", ""),
                "Category": r.get("Category", ""),
                "Notes": r.get("Notes", ""),
                "unprocessed_hint": is_unprocessed_hint(r),
            }
            if scope_key in seen_candidate_keys:
                suppressed_candidate_duplicates.append(rec)
                continue
            seen_candidate_keys.add(scope_key)
            candidates.append(rec)
            if len(per_scope[scope]) < sample_limit:
                per_scope[scope].append(rec)

    signature = stable_hash_for_candidates(candidates)
    same_week = previous.get("last_week") == iso_week
    same_signature = previous.get("last_signature") == signature
    scope_counts = Counter(c["scope"] for c in candidates)
    candidate_duplicate_scopes_by_ledger_key: dict[str, set[str]] = defaultdict(set)
    for record in suppressed_candidate_duplicates:
        ledger_key = str(record.get("ledger_key") or "")
        scope = str(record.get("scope") or "")
        if ledger_key and scope:
            candidate_duplicate_scopes_by_ledger_key[ledger_key].add(scope)
    duplicate_review = apply_duplicate_allowlist(
        duplicate_review_records(rows, duplicates, candidates, candidate_duplicate_scopes_by_ledger_key),
        allowlist_entries,
    )
    pending_duplicate_review = [record for record in duplicate_review if record.get("review_status") != "accepted"]
    allowed_duplicate_review = [record for record in duplicate_review if record.get("review_status") == "accepted"]
    blocking_duplicate_review = [
        record
        for record in pending_duplicate_review
        if record.get("review_severity") == "candidate_duplicate"
        or record.get("duplicate_shape") == "exact_ledger_duplicate"
    ]
    candidate_duplicate_count = sum(
        1 for record in duplicate_review if record.get("review_severity") == "candidate_duplicate"
    )
    pending_candidate_duplicate_count = sum(
        1 for record in pending_duplicate_review if record.get("review_severity") == "candidate_duplicate"
    )
    duplicate_review_severity_counts = Counter(record.get("review_severity") or "unknown" for record in duplicate_review)
    duplicate_review_pending_severity_counts = Counter(record.get("review_severity") or "unknown" for record in pending_duplicate_review)
    duplicate_review_allowed_severity_counts = Counter(record.get("review_severity") or "unknown" for record in allowed_duplicate_review)
    duplicate_shape_counts = Counter(record.get("duplicate_shape") or "unknown" for record in duplicate_review)
    duplicate_pending_shape_counts = Counter(record.get("duplicate_shape") or "unknown" for record in pending_duplicate_review)
    duplicate_suggested_action_counts = Counter(record.get("suggested_review_action") or "unknown" for record in pending_duplicate_review)
    exact_candidate_duplicate_pending_count = sum(
        1 for record in pending_duplicate_review
        if record.get("duplicate_shape") == "exact_candidate_duplicate"
    )
    notes_only_candidate_duplicate_pending_count = sum(
        1 for record in pending_duplicate_review
        if record.get("duplicate_shape") == "notes_only_candidate_duplicate"
    )
    candidate_duplicate_scopes = Counter(
        scope
        for record in duplicate_review
        if record.get("review_severity") == "candidate_duplicate"
        for scope in str(record.get("candidate_scopes") or "").split(",")
        if scope
    )
    if blocking_duplicate_review:
        issues.append(
            "Baselane weekly duplicate transaction keys require review: "
            f"blocking_duplicate_key_count={len(blocking_duplicate_review)} "
            f"pending_duplicate_key_count={len(pending_duplicate_review)} "
            f"candidate_duplicate_pending_count={pending_candidate_duplicate_count}"
        )
    duplicate_review_csv = out_json.with_name("baselane_weekly_duplicate_key_review.csv")
    duplicate_allowlist_template_path = out_json.with_name("baselane_weekly_duplicate_allowlist_template.json")
    duplicate_review_packet_path = out_json.with_name("baselane_weekly_duplicate_review_packet.md")
    duplicate_review_next_action = (
        "Review pending candidate_duplicate rows first; these duplicate ledger keys affect weekly automation scopes and must be resolved or explicitly marked in the duplicate allowlist before destructive weekly updates."
        if pending_candidate_duplicate_count
        else "Use the exact-row-deduped reporting ledger or clean/allowlist exact duplicate source rows before weekly/monthly financial outputs."
        if blocking_duplicate_review
        else "Review advisory duplicate-like ledger rows when convenient; they are not blocking because they are non-exact ledger-only rows."
        if pending_duplicate_review
        else "All duplicate ledger keys are either absent or explicitly reviewed in the duplicate allowlist."
        if duplicate_review
        else "No duplicate ledger keys detected."
    )

    visible_ok.append(
        "OK Baselane weekly unprocessed diagnostic: "
        f"ledger_rows={len(rows)} candidates={len(candidates)} duplicate_keys={len(duplicates)}"
    )
    visible_ok.append(
        "OK Baselane weekly unprocessed diagnostic boundary: "
        "no Baselane network call, CSV write, report write, state write, restart, sudo, OAuth, cleanup, or helper command"
    )

    classification = "baselane-weekly-unprocessed-review" if issues else "ok"
    evidence = {
        "ledger_exists": ledger_exists,
        "ledger_readable": ledger_readable,
        "state_file_exists": state_file_exists,
        "state_file_readable": state_file_readable,
        "ledger_rows": len(rows),
        "candidate_count": len(candidates),
        "candidate_duplicate_suppressed_count": len(suppressed_candidate_duplicates),
        "duplicate_key_count": len(duplicates),
        "duplicate_rows_total": duplicate_rows_total,
    }
    classified_issues = classified_issue_records(
        issues,
        classification,
        ledger_path,
        sample_limit,
        evidence,
    )
    fields = remediation_fields(classification, ledger_path, sample_limit)
    report = {
        "generated_at": generated_at,
        "iso_week": iso_week,
        "status": "BASELANE_WEEKLY_UNPROCESSED_REVIEW" if issues else "NO_REPLY",
        "classification": classification,
        "ok": visible_ok if not issues else visible_ok,
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
        "ledger": str(ledger_path),
        "ledger_exists": ledger_exists,
        "ledger_readable": ledger_readable,
        "ledger_rows": len(rows),
        "out_json": str(out_json),
        "out_csv": str(out_csv),
        "state_file": str(state_file),
        "duplicate_allowlist_file": str(duplicate_allowlist),
        "duplicate_allowlist_exists": duplicate_allowlist.exists(),
        "duplicate_allowlist_error": allowlist_error,
        "duplicate_allowlist_entry_count": len(allowlist_entries),
        "state_file_exists": state_file_exists,
        "state_file_readable": state_file_readable,
        "state_read_error": state_read_error,
        "write_attempted": write_attempted,
        "report_write_attempted": write_attempted,
        "csv_write_attempted": write_attempted,
        "state_write_attempted": write_attempted,
        "duplicate_key_count": len(duplicates),
        "duplicate_rows_total": duplicate_rows_total,
        "exact_duplicate_key_count": exact_duplicate_key_count,
        "exact_duplicate_extra_row_count": exact_duplicate_extra_row_count,
        "deduped_reporting_ledger": str(deduped_ledger_out) if deduped_ledger_out else None,
        "deduped_reporting_ledger_would_write": bool(deduped_ledger_out),
        "deduped_reporting_ledger_row_count": len(deduped_rows),
        "deduped_reporting_ledger_removed_row_count": exact_duplicate_extra_row_count,
        "candidate_duplicate_key_count": candidate_duplicate_count,
        "duplicate_review_pending_count": len(pending_duplicate_review),
        "duplicate_review_blocking_count": len(blocking_duplicate_review),
        "duplicate_review_allowed_count": len(allowed_duplicate_review),
        "candidate_duplicate_pending_count": pending_candidate_duplicate_count,
        "duplicate_review_severity_counts": dict(sorted(duplicate_review_severity_counts.items())),
        "duplicate_review_pending_severity_counts": dict(sorted(duplicate_review_pending_severity_counts.items())),
        "duplicate_review_allowed_severity_counts": dict(sorted(duplicate_review_allowed_severity_counts.items())),
        "duplicate_shape_counts": dict(sorted(duplicate_shape_counts.items())),
        "duplicate_pending_shape_counts": dict(sorted(duplicate_pending_shape_counts.items())),
        "duplicate_suggested_action_counts": dict(sorted(duplicate_suggested_action_counts.items())),
        "exact_candidate_duplicate_pending_count": exact_candidate_duplicate_pending_count,
        "notes_only_candidate_duplicate_pending_count": notes_only_candidate_duplicate_pending_count,
        "candidate_duplicate_scope_counts": dict(sorted(candidate_duplicate_scopes.items())),
        "duplicate_review_next_action": duplicate_review_next_action,
        "duplicate_keys_bounded": sorted(duplicates)[:10],
        "duplicate_review_csv": str(duplicate_review_csv),
        "duplicate_allowlist_template": str(duplicate_allowlist_template_path),
        "duplicate_review_packet": str(duplicate_review_packet_path),
        "duplicate_review_count": len(duplicate_review),
        "duplicate_review_bounded": duplicate_review[:sample_limit],
        "_duplicate_review_records": duplicate_review,
        "_deduped_rows": deduped_rows,
        "_ledger_fieldnames": ledger_fieldnames,
        "_deduped_ledger_out": str(deduped_ledger_out) if deduped_ledger_out else "",
        "candidate_duplicate_suppressed_count": len(suppressed_candidate_duplicates),
        "candidate_duplicate_suppressed_bounded": suppressed_candidate_duplicates[:sample_limit],
        "candidate_count": len(candidates),
        "candidate_signature_sha256": signature,
        "sample_limit": sample_limit,
        "idempotency": {
            "same_week_as_last_run": same_week,
            "same_signature_as_last_run": same_signature,
            "idempotent": bool(same_week and same_signature),
        },
        "scope_counts": dict(sorted(scope_counts.items())),
        "scope_samples": {k: v for k, v in sorted(per_scope.items())},
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
    return report, candidates


def build_report(
    ledger_path: Path,
    out_json: Path,
    out_csv: Path,
    state_file: Path,
    duplicate_allowlist: Path | None = None,
    deduped_ledger_out: Path | None = None,
    sample_limit: int = 50,
    write_attempted: bool = False,
) -> dict[str, Any]:
    report, _candidates = build_report_with_candidates(
        ledger_path=ledger_path,
        out_json=out_json,
        out_csv=out_csv,
        state_file=state_file,
        duplicate_allowlist=duplicate_allowlist,
        deduped_ledger_out=deduped_ledger_out,
        sample_limit=sample_limit,
        write_attempted=write_attempted,
    )
    return report


def write_report_outputs(
    report: dict[str, Any],
    candidates: list[dict[str, Any]],
    out_json: Path,
    out_csv: Path,
    state_file: Path,
) -> None:
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    state_file.parent.mkdir(parents=True, exist_ok=True)

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(candidate)

    duplicate_review_csv = Path(report["duplicate_review_csv"])
    duplicate_review_csv.parent.mkdir(parents=True, exist_ok=True)
    with duplicate_review_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DUPLICATE_REVIEW_FIELDS)
        writer.writeheader()
        for record in report.get("_duplicate_review_records") or report.get("duplicate_review_bounded") or []:
            writer.writerow({field: record.get(field, "") for field in DUPLICATE_REVIEW_FIELDS})
    duplicate_allowlist_template_path = Path(report["duplicate_allowlist_template"])
    duplicate_allowlist_template_path.write_text(
        json.dumps(duplicate_allowlist_template(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    duplicate_review_packet_path = Path(report["duplicate_review_packet"])
    duplicate_review_packet_path.write_text(duplicate_review_markdown(report), encoding="utf-8")
    deduped_ledger_out = Path(str(report.get("_deduped_ledger_out") or "")) if report.get("_deduped_ledger_out") else None
    if deduped_ledger_out:
        deduped_ledger_out.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = list(report.get("_ledger_fieldnames") or [])
        with deduped_ledger_out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in report.get("_deduped_rows") or []:
                writer.writerow({field: row.get(field, "") for field in fieldnames})
    report.pop("_duplicate_review_records", None)
    report.pop("_deduped_rows", None)
    report.pop("_ledger_fieldnames", None)
    report.pop("_deduped_ledger_out", None)

    with out_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    new_state = {
        "last_run_at": datetime.now().isoformat(),
        "last_week": report["iso_week"],
        "last_signature": report["candidate_signature_sha256"],
        "last_candidate_count": report["candidate_count"],
        "last_duplicate_key_count": report["duplicate_key_count"],
    }
    state_file.write_text(json.dumps(new_state, indent=2), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a read-only dashboard diagnostic and do not write report/csv/state files",
    )
    parser.add_argument(
        "--ledger",
        default=str(DEFAULT_WORKSPACE / "Dropbox" / "Projects" / "transaction_tracker" / "ECO Systems General Ledger.csv"),
    )
    parser.add_argument(
        "--out-json",
        default=str(DEFAULT_WORKSPACE / "reports" / "baselane_weekly_unprocessed_report.json"),
    )
    parser.add_argument(
        "--out-csv",
        default=str(DEFAULT_WORKSPACE / "reports" / "baselane_weekly_unprocessed_candidates.csv"),
    )
    parser.add_argument(
        "--state-file",
        default=str(DEFAULT_WORKSPACE / "scripts" / ".baselane_weekly_unprocessed_state.json"),
    )
    parser.add_argument(
        "--duplicate-allowlist",
        default=str(DEFAULT_WORKSPACE / "scripts" / ".baselane_weekly_duplicate_allowlist.json"),
    )
    parser.add_argument(
        "--deduped-ledger-out",
        default=None,
        help="write an exact-row-deduped reporting ledger CSV without modifying the raw Baselane export",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=50,
        help="max rows per scope in JSON sample",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)

    ledger_path = Path(args.ledger)
    out_json = Path(args.out_json)
    out_csv = Path(args.out_csv)
    state_file = Path(args.state_file)
    duplicate_allowlist = Path(args.duplicate_allowlist)
    deduped_ledger_out = Path(args.deduped_ledger_out) if args.deduped_ledger_out else None

    if args.json:
        report, _candidates = build_report_with_candidates(
            ledger_path=ledger_path,
            out_json=out_json,
            out_csv=out_csv,
            state_file=state_file,
            duplicate_allowlist=duplicate_allowlist,
            deduped_ledger_out=deduped_ledger_out,
            sample_limit=args.sample_limit,
            write_attempted=False,
        )
        print(json.dumps(report, indent=2, sort_keys=True), file=stdout or sys.stdout)
        return 0 if report["status"] == "NO_REPLY" else 1

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    report, candidates = build_report_with_candidates(
        ledger_path=ledger_path,
        out_json=out_json,
        out_csv=out_csv,
        state_file=state_file,
        duplicate_allowlist=duplicate_allowlist,
        deduped_ledger_out=deduped_ledger_out,
        sample_limit=args.sample_limit,
        write_attempted=True,
    )
    if not report["ledger_exists"]:
        raise SystemExit(f"Ledger not found: {ledger_path}")
    if not report["ledger_readable"]:
        raise SystemExit(f"Ledger not readable: {ledger_path}")

    write_report_outputs(report, candidates, out_json, out_csv, state_file)

    output = stdout or sys.stdout
    print(f"weekly_report={out_json}", file=output)
    print(f"candidates_csv={out_csv}", file=output)
    print(f"ledger_rows={report['ledger_rows']}", file=output)
    print(f"candidate_count={report['candidate_count']}", file=output)
    print(f"duplicate_key_count={report['duplicate_key_count']}", file=output)
    print(f"idempotent_same_week_and_signature={report['idempotency']['idempotent']}", file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
