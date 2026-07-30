#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).absolute().parents[1]
DEFAULT_GL = Path(
    "/mnt/c/Users/digit/Dropbox/Real Estate/CO/804 S Quitman St Public/07 - P&L & Owner Statements/"
    "ECO Systems General Ledger - 804 S Quitman St - Recovered 2e2f7a56 - 804 S Quitman St, Denver, CO 80219.csv"
)
DEFAULT_CANDIDATE_PACKET = ROOT / "reports/baselane_financials_monthly_review_candidate_packet.json"
DEFAULT_TRANSFER_REPORT = ROOT / "reports/baselane_lofty_transfer_requirements.json"
DEFAULT_REPORT = ROOT / "reports/baselane_804_quitman_deficit_audit.json"
DEFAULT_DECISIONS = ROOT / "config/baselane_804_quitman_cash_alignment_decisions.json"
DEFAULT_REVIEW_QUEUE_CSV = ROOT / "reports/baselane_804_quitman_cash_alignment_review_queue.csv"
DEFAULT_GROUP_REVIEW_QUEUE_CSV = ROOT / "reports/baselane_804_quitman_cash_alignment_group_review_queue.csv"
DEFAULT_CANDIDATE_DECISIONS = ROOT / "reports/baselane_804_quitman_cash_alignment_candidate_decisions.json"
DEFAULT_DECISION_VALIDATION = ROOT / "reports/baselane_804_quitman_cash_alignment_decision_validation.json"
DEFAULT_DECISION_TEMPLATE = ROOT / "reports/baselane_804_quitman_cash_alignment_decision_template.json"
DEFAULT_REVIEWED_TEMPLATE = ROOT / "config/baselane_804_quitman_cash_alignment_reviewed_template.json"
DEFAULT_PROPOSED_DECISIONS = ROOT / "reports/baselane_804_quitman_cash_alignment_proposed_decisions.json"
DEFAULT_UPSTREAM_RETAG_QUEUE_CSV = ROOT / "reports/baselane_804_quitman_upstream_retag_queue.csv"
DEFAULT_REVIEW_MARKDOWN = ROOT / "reports/baselane_804_quitman_cash_alignment_review.md"
DEFAULT_IMPORT_COMMANDS = ROOT / "reports/baselane_804_quitman_cash_alignment_import_group_review.requires-explicit-approval.sh"
PROPERTY_MATCH_RE = re.compile(r"\b804\s+s\s+quitman\b", re.I)
TRANSFER_RE = re.compile(r"\b(transfer|internal_transfer|zelle|ach)\b", re.I)
OWNER_RE = re.compile(r"\b(owner contribution|owner distribution|contribution|distribution)\b", re.I)
DAO_ACCOUNT_RE = re.compile(r"\b(804\s+s\s+quitman|lion\s+lfty0435)\b", re.I)
VALID_DECISIONS = {
    "true_804_cash_movement",
    "dao_capital_or_reimbursement",
    "upstream_retag_required",
    "exclude_from_transfer_basis",
}


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def group_review_evidence_payload(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "group_id": group.get("group_id"),
        "suggested_decision": group.get("suggested_decision"),
        "transfer_basis_effect_if_approved": group.get("transfer_basis_effect_if_approved"),
        "row_count": int(group.get("row_count") or 0),
        "signed_amount_sum": round(float(group.get("signed_amount_sum") or 0.0), 2),
        "absolute_amount_sum": round(float(group.get("absolute_amount_sum") or 0.0), 2),
        "account": group.get("account") or "",
        "category": group.get("category") or "",
        "cash_alignment_review_bucket_names": sorted(
            str(bucket) for bucket in (group.get("cash_alignment_review_bucket_names") or [])
        ),
    }


def group_review_evidence_digest(group: dict[str, Any]) -> str:
    payload = group_review_evidence_payload(group)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_money(value: object) -> float:
    text = str(value or "").strip().replace("$", "").replace(",", "")
    if not text:
        return 0.0
    negative = text.startswith("(") and text.endswith(")")
    text = text.replace("(", "").replace(")", "")
    try:
        amount = float(text)
    except ValueError:
        return 0.0
    return round(-amount if negative else amount, 2)


def normalize_month(value: object) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return text
    for pattern in (r"^(\d{4})-(\d{2})-\d{2}", r"^(\d{1,2})/(\d{1,2})/(\d{4})$"):
        match = re.match(pattern, text)
        if match and pattern.startswith("^(\\d{4})"):
            return f"{match.group(1)}-{match.group(2)}"
        if match:
            return f"{match.group(3)}-{int(match.group(1)):02d}"
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m")
        except ValueError:
            pass
    return "unknown"


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": f"{type(exc).__name__}: {exc}"}
    return payload if isinstance(payload, dict) else {"status": "unreadable", "path": str(path), "error": "root is not object"}


def normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def row_fingerprint(row: dict[str, Any]) -> str:
    parts = [
        normalize_text(row.get("Date")),
        normalize_text(row.get("Account")),
        f"{float(row.get('amount') or 0.0):.2f}",
        normalize_text(row.get("Category")),
        normalize_text(row.get("Merchant")),
        normalize_text(row.get("Description")),
        normalize_text(row.get("Notes")),
    ]
    return "|".join(parts)


def load_decisions(path: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(path)
    decisions: dict[str, dict[str, Any]] = {}
    for item in payload.get("decisions") or []:
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision") or "")
        if not item.get("reviewed") or decision not in VALID_DECISIONS:
            continue
        fingerprint = str(item.get("fingerprint") or "")
        if not fingerprint:
            continue
        decisions[fingerprint] = {
            "decision": decision,
            "reviewed": True,
            "reviewed_at": item.get("reviewed_at"),
            "note": item.get("note"),
        }
    return decisions


def raw_decision_items(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = read_json(path)
    return payload, [item for item in payload.get("decisions") or [] if isinstance(item, dict)]


def validate_decision_items(items: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    row_fingerprints = {str(row.get("fingerprint") or "") for row in rows}
    seen: set[str] = set()
    issues: list[dict[str, Any]] = []
    reviewed_count = 0
    valid_count = 0
    for index, item in enumerate(items):
        fingerprint = str(item.get("fingerprint") or "")
        decision = str(item.get("decision") or "")
        reviewed = bool(item.get("reviewed"))
        if reviewed:
            reviewed_count += 1
        if not fingerprint:
            issues.append({"index": index, "code": "missing_fingerprint"})
            continue
        if fingerprint in seen:
            issues.append({"index": index, "code": "duplicate_fingerprint", "fingerprint": fingerprint})
        seen.add(fingerprint)
        if fingerprint not in row_fingerprints:
            issues.append({"index": index, "code": "fingerprint_not_in_current_804_gl", "fingerprint": fingerprint})
        if decision not in VALID_DECISIONS:
            issues.append({"index": index, "code": "invalid_decision", "fingerprint": fingerprint, "decision": decision})
        if not reviewed:
            issues.append({"index": index, "code": "not_reviewed", "fingerprint": fingerprint})
        if reviewed and decision in VALID_DECISIONS and fingerprint in row_fingerprints:
            valid_count += 1
    return {
        "status": "ok" if not issues else "review",
        "decision_count": len(items),
        "reviewed_decision_count": reviewed_count,
        "valid_decision_count": valid_count,
        "issue_count": len(issues),
        "issues": issues[:200],
    }


def reviewed_template_import(
    reviewed_template_path: Path,
    proposed_decisions_path: Path,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not reviewed_template_path.is_file():
        return {
            "status": "missing",
            "reviewed_template_path": str(reviewed_template_path),
            "proposed_decisions_path": str(proposed_decisions_path),
            "imported_decision_count": 0,
            "issue_count": 0,
            "issues": [],
        }
    payload, items = raw_decision_items(reviewed_template_path)
    row_fingerprints = {str(row.get("fingerprint") or "") for row in rows}
    reviewed_groups = [
        item
        for item in payload.get("group_summaries") or []
        if isinstance(item, dict) and item.get("reviewed") is True
    ]
    group_fingerprints: dict[str, list[str]] = defaultdict(list)
    for item in items:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("group_id") or "")
        fingerprint = str(item.get("fingerprint") or "")
        if group_id and fingerprint:
            group_fingerprints[group_id].append(fingerprint)
    reviewed_items = [item for item in items if item.get("reviewed") is True]
    expanded_by_fingerprint = {str(item.get("fingerprint") or ""): dict(item) for item in reviewed_items if item.get("fingerprint")}
    group_issues: list[dict[str, Any]] = []
    expanded_group_count = 0
    for group in reviewed_groups:
        group_id = str(group.get("group_id") or "")
        decision = str(group.get("decision") or group.get("suggested_decision") or "")
        if not group_id:
            group_issues.append({"code": "reviewed_group_missing_group_id"})
            continue
        if decision not in VALID_DECISIONS:
            group_issues.append({"code": "reviewed_group_invalid_decision", "group_id": group_id, "decision": decision})
            continue
        fingerprints = group_fingerprints.get(group_id) or []
        if not fingerprints:
            group_issues.append({"code": "reviewed_group_has_no_current_fingerprints", "group_id": group_id})
            continue
        stale_fingerprints = sorted(fingerprint for fingerprint in fingerprints if fingerprint not in row_fingerprints)
        if stale_fingerprints:
            group_issues.append(
                {
                    "code": "reviewed_group_contains_stale_fingerprints",
                    "group_id": group_id,
                    "stale_fingerprint_count": len(stale_fingerprints),
                    "stale_fingerprints": stale_fingerprints[:20],
                }
            )
            continue
        expanded_group_count += 1
        for fingerprint in fingerprints:
            expanded_by_fingerprint[fingerprint] = {
                "fingerprint": fingerprint,
                "group_id": group_id,
                "decision": decision,
                "reviewed": True,
                "reviewed_at": group.get("reviewed_at"),
                "note": group.get("note") or f"group-level approval: {group_id}",
            }
    reviewed_items = list(expanded_by_fingerprint.values())
    validation = validate_decision_items(reviewed_items, rows)
    validation["issues"] = (validation.get("issues") or []) + group_issues
    validation["issue_count"] = int(validation.get("issue_count") or 0) + len(group_issues)
    validation["status"] = "ok" if int(validation["issue_count"]) == 0 else "review"
    reviewed_ratio = (len(reviewed_items) / len(items)) if items else 0.0
    group_review_ratio = (expanded_group_count / len(payload.get("group_summaries") or [])) if payload.get("group_summaries") else 0.0
    output = {
        "status": "ok" if validation["status"] == "ok" else "review",
        "generated_at": iso_z(),
        "source_template": str(reviewed_template_path),
        "policy": (
            "Proposed config only. Inspect this file, then copy to "
            "config/baselane_804_quitman_cash_alignment_decisions.json when approved."
        ),
        "source_template_status": payload.get("status"),
        "decision_count": len(reviewed_items),
        "source_template_decision_count": len(items),
        "source_template_unreviewed_count": max(len(items) - len(reviewed_items), 0),
        "reviewed_group_count": expanded_group_count,
        "source_template_group_count": len(payload.get("group_summaries") or []),
        "reviewed_template_group_coverage_ratio": round(group_review_ratio, 6),
        "reviewed_template_coverage_ratio": round(reviewed_ratio, 6),
        "decisions": reviewed_items,
        "validation": validation,
    }
    proposed_decisions_path.parent.mkdir(parents=True, exist_ok=True)
    proposed_decisions_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": output["status"],
        "reviewed_template_path": str(reviewed_template_path),
        "proposed_decisions_path": str(proposed_decisions_path),
        "imported_decision_count": len(reviewed_items),
        "source_template_decision_count": len(items),
        "source_template_unreviewed_count": max(len(items) - len(reviewed_items), 0),
        "reviewed_group_count": expanded_group_count,
        "source_template_group_count": len(payload.get("group_summaries") or []),
        "reviewed_template_group_coverage_ratio": round(group_review_ratio, 6),
        "reviewed_template_coverage_ratio": round(reviewed_ratio, 6),
        "validation_status": validation["status"],
        "issue_count": validation["issue_count"],
        "issues": validation["issues"],
    }


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "reviewed", "approved"}


def import_group_review_csv_to_template(
    csv_path: Path,
    template_path: Path,
    *,
    min_note_length: int = 20,
) -> dict[str, Any]:
    if not csv_path.is_file():
        return {"status": "missing_csv", "csv": str(csv_path), "template": str(template_path), "imported_group_count": 0}
    if not template_path.is_file():
        return {"status": "missing_template", "csv": str(csv_path), "template": str(template_path), "imported_group_count": 0}
    template = read_json(template_path)
    groups = template.get("group_summaries") if isinstance(template.get("group_summaries"), list) else []
    by_group_id = {str(group.get("group_id") or ""): group for group in groups if isinstance(group, dict)}
    issues: list[dict[str, Any]] = []
    imported_count = 0
    skipped_count = 0
    with csv_path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            group_id = str(row.get("group_id") or "").strip()
            if not group_id:
                issues.append({"row": row_number, "code": "missing_group_id"})
                continue
            group = by_group_id.get(group_id)
            if not group:
                issues.append({"row": row_number, "group_id": group_id, "code": "group_id_not_in_template"})
                continue
            if not truthy(row.get("reviewed")):
                skipped_count += 1
                continue
            decision = str(row.get("approved_decision") or row.get("suggested_decision") or "").strip()
            note = str(row.get("review_note") or "").strip()
            reviewed_at = str(row.get("reviewed_at") or "").strip()
            evidence_digest = str(row.get("evidence_digest") or "").strip()
            if decision not in VALID_DECISIONS:
                issues.append({"row": row_number, "group_id": group_id, "code": "invalid_approved_decision", "decision": decision})
                continue
            expected_digest = group_review_evidence_digest(group)
            if evidence_digest != expected_digest:
                issues.append(
                    {
                        "row": row_number,
                        "group_id": group_id,
                        "code": "evidence_digest_mismatch",
                        "expected_digest": expected_digest,
                        "provided_digest": evidence_digest,
                    }
                )
                continue
            if len(note) < min_note_length:
                issues.append({"row": row_number, "group_id": group_id, "code": "review_note_too_short", "min_note_length": min_note_length})
                continue
            if not reviewed_at:
                issues.append({"row": row_number, "group_id": group_id, "code": "missing_reviewed_at"})
                continue
            group["reviewed"] = True
            group["decision"] = decision
            group["reviewed_at"] = reviewed_at
            group["note"] = note
            group["evidence_digest"] = evidence_digest
            imported_count += 1
    template["status"] = "review_in_progress" if imported_count else template.get("status") or "review_required"
    template["group_review_csv_import"] = {
        "status": "ok" if not issues else "review",
        "csv": str(csv_path),
        "imported_group_count": imported_count,
        "skipped_unreviewed_group_count": skipped_count,
        "issue_count": len(issues),
        "issues": issues[:200],
        "policy": (
            "Only rows with reviewed=true, valid approved_decision, reviewed_at, and a substantive review_note "
            "current evidence_digest are imported into group_summaries."
        ),
    }
    template_path.write_text(json.dumps(template, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "ok" if not issues else "review",
        "csv": str(csv_path),
        "template": str(template_path),
        "imported_group_count": imported_count,
        "skipped_unreviewed_group_count": skipped_count,
        "issue_count": len(issues),
        "issues": issues[:200],
    }


def write_group_review_import_commands(path: Path) -> None:
    script = "scripts/audit_804_quitman_cash_alignment.py"
    decision_validation = "reports/baselane_804_quitman_cash_alignment_decision_validation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "# Requires explicit human approval after completing the 804 group review CSV.",
                "# Imports reviewed=true group rows into the reviewed template, reruns audit, and refuses dirty validation.",
                'echo "[804-cash-alignment] importing reviewed group CSV and rerunning audit"',
                f"python3 {script} --import-group-review-csv",
                'VALIDATION_DETAIL="$(python3 - <<\'PY\'',
                "import json",
                f"payload=json.load(open({decision_validation!r}, encoding='utf-8'))",
                "progress=payload.get('review_progress') or {}",
                "effective_status=payload.get('effective_status') or ''",
                "unreviewed_group_count=int(progress.get('unreviewed_group_count') or 0)",
                "high_priority_unreviewed_group_count=int(progress.get('high_priority_unreviewed_group_count') or 0)",
                "print('effective_status=' + effective_status)",
                "print('unreviewed_group_count=' + str(unreviewed_group_count))",
                "print('high_priority_unreviewed_group_count=' + str(high_priority_unreviewed_group_count))",
                "print('reviewed_group_count=' + str(progress.get('reviewed_group_count') or 0))",
                "print('source_group_count=' + str(progress.get('source_group_count') or 0))",
                "print('review_rows_remaining=' + str(payload.get('review_rows_remaining') or 0))",
                "for issue in payload.get('effective_issues') or []:",
                "    print('effective_issue=' + str(issue))",
                "PY",
                ')"',
                'echo "$VALIDATION_DETAIL"',
                'EFFECTIVE_STATUS="$(printf "%s\\n" "$VALIDATION_DETAIL" | awk -F= \'/^effective_status=/{print $2; exit}\' )"',
                'UNREVIEWED_GROUP_COUNT="$(printf "%s\\n" "$VALIDATION_DETAIL" | awk -F= \'/^unreviewed_group_count=/{print $2; exit}\' )"',
                'HIGH_PRIORITY_UNREVIEWED_GROUP_COUNT="$(printf "%s\\n" "$VALIDATION_DETAIL" | awk -F= \'/^high_priority_unreviewed_group_count=/{print $2; exit}\' )"',
                'if [ "$UNREVIEWED_GROUP_COUNT" != "0" ]; then',
                '  echo "[804-cash-alignment] unreviewed group count is $UNREVIEWED_GROUP_COUNT; complete every group before transfer reconciliation" >&2',
                "  exit 1",
                "fi",
                'if [ "$HIGH_PRIORITY_UNREVIEWED_GROUP_COUNT" != "0" ]; then',
                '  echo "[804-cash-alignment] high-priority unreviewed group count is $HIGH_PRIORITY_UNREVIEWED_GROUP_COUNT; refusing transfer basis" >&2',
                "  exit 1",
                "fi",
                'if [ "$EFFECTIVE_STATUS" != "ok" ]; then',
                '  echo "[804-cash-alignment] effective validation is $EFFECTIVE_STATUS; refusing to treat 804 transfer basis as final" >&2',
                "  exit 1",
                "fi",
                'echo "[804-cash-alignment] effective validation ok; review proposed decisions before copying to live config"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def row_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(key) or "") for key in ("Account", "Category", "Type", "Merchant", "Description", "Notes"))


def risk_tags(row: dict[str, Any]) -> list[str]:
    text = row_text(row)
    category = str(row.get("Category") or "")
    tags: list[str] = []
    if TRANSFER_RE.search(text) or "transfer" in category.lower():
        tags.append("transfer_between_accounts")
    if OWNER_RE.search(text) or "owner" in category.lower():
        tags.append("owner_contribution_distribution")
    account = str(row.get("Account") or "")
    if account and "804 s quitman" not in account.lower() and "lion lfty0435" not in account.lower():
        tags.append("non_804_account")
    return tags


def disjoint_review_bucket(row: dict[str, Any]) -> str:
    tags = set(row.get("risk_tags") or [])
    category = str(row.get("Category") or "").lower()
    if "owner_contribution_distribution" in tags:
        if "non_804_account" in tags:
            return "owner_capital_or_distribution_external_account"
        return "owner_capital_or_distribution_dao_account"
    if "transfer_between_accounts" in tags:
        if "non_804_account" in tags:
            return "external_account_transfer"
        return "unbalanced_internal_804_transfer"
    if "non_804_account" in tags:
        if any(token in category for token in ("rent", "revenue")):
            return "external_account_revenue_or_receipt"
        return "external_account_operating_or_capex"
    return "other_review"


def candidate_decision_for_row(row: dict[str, Any]) -> dict[str, Any]:
    tags = set(row.get("risk_tags") or [])
    category = normalize_text(row.get("Category"))
    account = normalize_text(row.get("Account"))
    amount = float(row.get("amount") or 0.0)
    if "non_804_account" in tags:
        return {
            "decision": "upstream_retag_required",
            "confidence": "review_required",
            "reason": "Row is attributed to 804 but account is not an 804/Lion DAO account.",
        }
    if "owner_contribution_distribution" in tags:
        return {
            "decision": "dao_capital_or_reimbursement",
            "confidence": "review_required",
            "reason": "Owner contribution/distribution rows affect DAO capital but are not bank-transfer instructions until classified.",
        }
    if "transfer_between_accounts" in tags:
        return {
            "decision": "true_804_cash_movement",
            "confidence": "review_required",
            "reason": "804-only transfer row remains unbalanced after automatic balanced-transfer pairing.",
        }
    if "mortgage" in category and ("804 s quitman" in account or "lion lfty0435" in account):
        return {
            "decision": "true_804_cash_movement",
            "confidence": "review_required",
            "reason": "804 is configured as a DAO P&I property; verify mortgage row classification before approving.",
        }
    return {
        "decision": "exclude_from_transfer_basis" if amount == 0 else "true_804_cash_movement",
        "confidence": "review_required",
        "reason": "No automatic approval rule exists; human review required.",
    }


def load_804_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if not PROPERTY_MATCH_RE.search(str(row.get("Property") or "")):
                continue
            payload = dict(row)
            payload["amount"] = parse_money(row.get("Amount"))
            payload["month"] = normalize_month(row.get("Date"))
            payload["risk_tags"] = risk_tags(payload)
            payload["_row_index"] = len(rows)
            payload["fingerprint"] = row_fingerprint(payload)
            rows.append(payload)
    return rows


def candidate_financial_summary(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    for record in payload.get("records") or []:
        if not isinstance(record, dict):
            continue
        if PROPERTY_MATCH_RE.search(str(record.get("property_name") or record.get("input_property_name") or "")):
            summary = record.get("monthly_financial_summary")
            return summary if isinstance(summary, dict) else {}
    return {}


def transfer_record(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    for row in payload.get("rows") or []:
        if isinstance(row, dict) and PROPERTY_MATCH_RE.search(str(row.get("property") or "")):
            return row
    return {}


def sum_by(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        totals[str(row.get(key) or "")] += float(row.get("amount") or 0.0)
    return {name: round(value, 2) for name, value in sorted(totals.items())}


def sum_by_risk_tag(rows: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        amount = float(row.get("amount") or 0.0)
        for tag in row.get("risk_tags", []):
            totals[str(tag)] += amount
    return {tag: round(total, 2) for tag, total in sorted(totals.items())}


def top_sums(totals: dict[str, float], limit: int = 10) -> list[dict[str, Any]]:
    return [
        {"name": name, "signed_amount_sum": amount}
        for name, amount in sorted(totals.items(), key=lambda item: abs(item[1]), reverse=True)[:limit]
    ]


def compact_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "date": row.get("Date"),
        "month": row.get("month"),
        "property": row.get("Property"),
        "account": row.get("Account"),
        "amount": row.get("amount"),
        "merchant": row.get("Merchant"),
        "description": row.get("Description"),
        "category": row.get("Category"),
        "type": row.get("Type"),
        "notes": row.get("Notes"),
        "risk_tags": row.get("risk_tags"),
        "fingerprint": row.get("fingerprint"),
    }
    if row.get("review_decision"):
        payload["review_decision"] = row.get("review_decision")
        payload["reviewed_at"] = row.get("reviewed_at")
        payload["review_note"] = row.get("review_note")
    return payload


def review_samples_by_risk_tag(rows: list[dict[str, Any]], limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda item: abs(float(item.get("amount") or 0.0)), reverse=True):
        for tag in row.get("risk_tags", []):
            if len(samples[tag]) < limit:
                samples[tag].append(compact_row(row))
    return dict(sorted(samples.items()))


def review_action_buckets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = [
        (
            "transfer_between_accounts",
            "Classify as real DAO cash movement only when both sides are attributed to 804; otherwise retag/remove the non-804 side before transfer reconciliation.",
        ),
        (
            "owner_contribution_distribution",
            "Confirm whether this is DAO capital/reimbursement or owner cash movement; do not treat as bank transfer requirement until classified.",
        ),
        (
            "non_804_account",
            "Retag upstream account/property attribution unless the non-804 account is intentionally holding 804 DAO cash.",
        ),
    ]
    totals = sum_by_risk_tag(rows)
    counts = Counter(tag for row in rows for tag in row.get("risk_tags", []))
    return [
        {
            "risk_tag": tag,
            "row_count": int(counts.get(tag, 0)),
            "signed_amount_sum": totals.get(tag, 0.0),
            "review_action": action,
        }
        for tag, action in buckets
        if counts.get(tag, 0)
    ]


def review_priority_buckets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    definitions = [
        (
            "external_or_non_804_account",
            lambda row: "non_804_account" in set(row.get("risk_tags") or []),
            "Highest priority: verify whether non-804 accounts intentionally held 804 DAO cash; otherwise retag/remove upstream.",
        ),
        (
            "owner_capital_or_distribution",
            lambda row: "owner_contribution_distribution" in set(row.get("risk_tags") or []),
            "Classify as capital contribution, reimbursement, or owner distribution before treating as transferable cash.",
        ),
        (
            "unbalanced_internal_804_transfer",
            lambda row: "transfer_between_accounts" in set(row.get("risk_tags") or [])
            and "non_804_account" not in set(row.get("risk_tags") or [])
            and "owner_contribution_distribution" not in set(row.get("risk_tags") or []),
            "Review unmatched 804-only transfer rows; balanced pairs are already separated from the blocker set.",
        ),
    ]
    buckets = []
    for name, predicate, action in definitions:
        matching = [row for row in rows if predicate(row)]
        if not matching:
            continue
        buckets.append(
            {
                "bucket": name,
                "row_count": len(matching),
                "signed_amount_sum": round(sum(float(row.get("amount") or 0.0) for row in matching), 2),
                "top_accounts": top_sums(sum_by(matching, "Account"), limit=5),
                "top_categories": top_sums(sum_by(matching, "Category"), limit=5),
                "top_months": top_sums(sum_by(matching, "month"), limit=5),
                "review_action": action,
            }
        )
    return buckets


def net_cash_exposure_by_priority(rows: list[dict[str, Any]]) -> dict[str, Any]:
    exposure_by_bucket: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        bucket = disjoint_review_bucket(row)
        exposure_by_bucket[bucket] += float(row.get("amount") or 0.0)
        counts[bucket] += 1
    exposure_by_bucket = {
        bucket: round(amount, 2)
        for bucket, amount in sorted(exposure_by_bucket.items())
    }
    high_priority_names = [
        bucket
        for bucket in (
            "external_account_operating_or_capex",
            "external_account_revenue_or_receipt",
            "external_account_transfer",
            "owner_capital_or_distribution_external_account",
            "owner_capital_or_distribution_dao_account",
        )
        if bucket in exposure_by_bucket
    ]
    high_priority = round(
        sum(
            amount
            for bucket, amount in exposure_by_bucket.items()
            if bucket in set(high_priority_names)
        ),
        2,
    )
    return {
        "policy": (
            "Disjoint signed sums are not transfer instructions. They isolate unresolved 804-attributed cash exposure "
            "so human review can decide which amounts are true DAO capital, reimbursements, revenue, expenses, or "
            "upstream account/property tagging errors before bank-to-bank transfers are moved."
        ),
        "by_bucket": exposure_by_bucket,
        "row_count_by_bucket": dict(sorted(counts.items())),
        "high_priority_unresolved_sum": high_priority,
        "high_priority_bucket_names": high_priority_names,
    }


def balanced_internal_transfer_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        tags = set(row.get("risk_tags") or [])
        if "transfer_between_accounts" not in tags:
            continue
        if "non_804_account" in tags or "owner_contribution_distribution" in tags:
            continue
        key = (
            str(row.get("Date") or ""),
            str(row.get("Merchant") or row.get("Description") or ""),
            str(row.get("Notes") or ""),
        )
        groups[key].append(row)
    balanced = []
    for (date, merchant, notes), grouped_rows in groups.items():
        if len(grouped_rows) < 2:
            continue
        amount_sum = round(sum(float(row.get("amount") or 0.0) for row in grouped_rows), 2)
        if abs(amount_sum) > 0.01:
            continue
        balanced.append(
            {
                "date": date,
                "merchant": merchant,
                "notes": notes,
                "row_count": len(grouped_rows),
                "amount_sum": amount_sum,
                "row_indexes": [int(row.get("_row_index") or -1) for row in grouped_rows],
                "accounts": sorted({str(row.get("Account") or "") for row in grouped_rows}),
                "rows": [compact_row(row) for row in grouped_rows[:10]],
            }
        )
    return sorted(balanced, key=lambda item: (item["date"], item["merchant"]))


def apply_review_decisions(rows: list[dict[str, Any]], decisions: dict[str, dict[str, Any]]) -> int:
    matched = 0
    for row in rows:
        decision = decisions.get(str(row.get("fingerprint") or ""))
        if not decision:
            continue
        matched += 1
        row["review_decision"] = decision["decision"]
        row["reviewed_at"] = decision.get("reviewed_at")
        row["review_note"] = decision.get("note")
    return matched


def build_report(
    gl_path: Path,
    candidate_packet: Path,
    transfer_report: Path,
    decisions_path: Path = DEFAULT_DECISIONS,
    reviewed_template_path: Path = DEFAULT_REVIEWED_TEMPLATE,
    proposed_decisions_path: Path = DEFAULT_PROPOSED_DECISIONS,
) -> dict[str, Any]:
    rows = load_804_rows(gl_path) if gl_path.is_file() else []
    reviewed_template_import_status = reviewed_template_import(reviewed_template_path, proposed_decisions_path, rows)
    raw_decision_payload, raw_decisions = raw_decision_items(decisions_path)
    decision_validation = validate_decision_items(raw_decisions, rows)
    decisions = load_decisions(decisions_path)
    decision_match_count = apply_review_decisions(rows, decisions)
    balanced_transfer_groups = balanced_internal_transfer_groups(rows)
    balanced_transfer_row_indexes = {
        int(row_index)
        for group in balanced_transfer_groups
        for row_index in group.get("row_indexes", [])
        if row_index is not None
    }

    reviewed_rows = [row for row in rows if row.get("review_decision")]
    reviewed_upstream_retag_required_rows = [
        row for row in reviewed_rows if row.get("review_decision") == "upstream_retag_required"
    ]
    upstream_retag_required_remaining_count = len(reviewed_upstream_retag_required_rows)
    upstream_retag_required_remaining_sum = round(
        sum(float(row.get("amount") or 0.0) for row in reviewed_upstream_retag_required_rows),
        2,
    )
    review_rows = [
        row
        for row in rows
        if row.get("risk_tags")
        and int(row.get("_row_index") or -1) not in balanced_transfer_row_indexes
        and not row.get("review_decision")
    ]
    review_sum = round(sum(float(row.get("amount") or 0.0) for row in review_rows), 2)
    full_gl_sum = round(sum(float(row.get("amount") or 0.0) for row in rows), 2)
    financial_summary = candidate_financial_summary(candidate_packet)
    transfer_requirement = transfer_record(transfer_report)
    eco_operating_cash = parse_money(financial_summary.get("eco_gl_column_e_sum"))
    eco_operating_cash_rows = int(financial_summary.get("eco_gl_column_e_row_count") or 0)
    full_gl_cash_difference = round(full_gl_sum - eco_operating_cash, 2)
    risk_tag_counts = Counter(tag for row in review_rows for tag in row.get("risk_tags", []))
    priority_buckets = review_priority_buckets(review_rows)
    net_cash_exposure = net_cash_exposure_by_priority(review_rows)
    by_account = sum_by(rows, "Account")
    by_category = sum_by(rows, "Category")
    by_type = sum_by(rows, "Type")
    by_month = sum_by(rows, "month")
    review_by_account = sum_by(review_rows, "Account")
    review_by_category = sum_by(review_rows, "Category")
    review_by_month = sum_by(review_rows, "month")
    review_required = bool(review_rows)
    reviewed_decision_full_clearance = (
        not review_required
        and upstream_retag_required_remaining_count == 0
        and decision_validation["status"] == "ok"
        and decision_match_count == len(decisions)
        and len(decisions) > 0
    )
    findings = []
    if review_required:
        findings.append(
            "Transfer/owner/non-804-account rows exist; do not use full cumulative GL as final transfer basis without human review."
        )
    if upstream_retag_required_remaining_count:
        findings.append(
            "Reviewed upstream_retag_required rows still exist in the 804 property-split GL; retag/remove them upstream before treating 804 as source-clean."
        )
    if abs(full_gl_cash_difference) > 0.01:
        findings.append(
            f"Full property GL differs from the FINANCIALS ECO Operating Cash balance by ${full_gl_cash_difference:,.2f}; reconcile the source-ledger scope before calculating any separate distribution instruction."
        )
    if transfer_requirement and transfer_requirement.get("bank_transfer_action") != "review_before_transfer":
        findings.append("Transfer report does not hold 804 for bank-vs-GL review.")
    source_clean_gate = {
        "status": (
            "ok"
            if (
                not review_required
                and upstream_retag_required_remaining_count == 0
                and decision_validation["status"] == "ok"
            )
            else "review"
        ),
        "review_rows_remaining": len(review_rows),
        "review_rows_total": len(review_rows) + len(reviewed_rows),
        "review_rows_completed": len(reviewed_rows),
        "upstream_retag_required_remaining_count": upstream_retag_required_remaining_count,
        "upstream_retag_required_remaining_sum": upstream_retag_required_remaining_sum,
        "review_rows_completion_ratio": round(
            (len(reviewed_rows) / (len(review_rows) + len(reviewed_rows)))
            if (len(review_rows) + len(reviewed_rows))
            else 1.0,
            6,
        ),
        "reviewed_decision_full_clearance": reviewed_decision_full_clearance,
        "decision_validation_status": decision_validation["status"],
        "decision_match_count": decision_match_count,
        "live_decision_count": len(decisions),
        "policy": (
            "804 is source-clean only when no transfer/owner/non-804 rows remain unreviewed, no reviewed "
            "upstream_retag_required rows still exist in the 804 GL, all live decisions validate against the "
            "current GL, and every live decision matches a current row."
        ),
    }
    return {
        "job": "audit-804-quitman-cash-alignment",
        "generated_at": iso_z(),
        "status": "review" if findings else "ok",
        "property": "804 S Quitman St Public",
        "gl_path": str(gl_path),
        "candidate_packet": str(candidate_packet),
        "transfer_report": str(transfer_report),
        "decisions_path": str(decisions_path),
        "reviewed_template_import": reviewed_template_import_status,
        "decision_validation": decision_validation,
        "decision_validation_status": decision_validation["status"],
        "decision_config_status": raw_decision_payload.get("status"),
        "review_decision_count": len(decisions),
        "review_decision_match_count": decision_match_count,
        "source_clean_gate": source_clean_gate,
        "source_clean_status": source_clean_gate["status"],
        "reviewed_decision_full_clearance": reviewed_decision_full_clearance,
        "reviewed_row_count": len(reviewed_rows),
        "upstream_retag_required_remaining_count": upstream_retag_required_remaining_count,
        "upstream_retag_required_remaining_sum": upstream_retag_required_remaining_sum,
        "upstream_retag_required_remaining_rows_bounded": [
            compact_row(row) for row in reviewed_upstream_retag_required_rows[:500]
        ],
        "reviewed_rows_bounded": [compact_row(row) for row in reviewed_rows[:500]],
        "full_gl": {"row_count": len(rows), "sum": full_gl_sum},
        "financial_summary": financial_summary,
        "eco_operating_cash_balance": eco_operating_cash,
        "eco_operating_cash_row_count": eco_operating_cash_rows,
        "full_gl_minus_eco_operating_cash": full_gl_cash_difference,
        "classification_review_count": len(review_rows),
        "classification_review_sum": review_sum,
        "balanced_internal_transfer_group_count": len(balanced_transfer_groups),
        "balanced_internal_transfer_row_count": sum(int(group.get("row_count") or 0) for group in balanced_transfer_groups),
        "balanced_internal_transfer_groups": [
            {key: value for key, value in group.items() if key != "source_rows"}
            for group in balanced_transfer_groups[:50]
        ],
        "risk_tag_counts": dict(sorted(risk_tag_counts.items())),
        "risk_tag_amount_sums": sum_by_risk_tag(review_rows),
        "review_action_buckets": review_action_buckets(review_rows),
        "review_priority_buckets": priority_buckets,
        "net_cash_exposure_review": net_cash_exposure,
        "top_review_accounts": top_sums(review_by_account),
        "top_review_categories": top_sums(review_by_category),
        "top_review_months": top_sums(review_by_month),
        "review_samples_by_risk_tag": review_samples_by_risk_tag(review_rows),
        "by_account": by_account,
        "by_category": by_category,
        "by_type": by_type,
        "by_month": by_month,
        "top_accounts": top_sums(by_account),
        "top_categories": top_sums(by_category),
        "top_months": top_sums(by_month),
        "transfer_requirement_record": transfer_requirement or None,
        "preliminary_findings": findings,
        "recommended_transfer_instruction": (
            "Hold 804 transfers. Classify transfer-between-account, owner contribution/distribution, and non-804-account rows; "
            "then rerun transfer reconciliation before moving cash."
            if findings
            else "No 804 bank-vs-GL blocker detected."
        ),
        "classification_review_rows": [compact_row(row) for row in review_rows[:500]],
    }



def write_review_queue_csv(path: Path, report: dict[str, Any]) -> None:
    fieldnames = [
        "fingerprint",
        "date",
        "month",
        "account",
        "amount",
        "category",
        "type",
        "merchant",
        "description",
        "notes",
        "risk_tags",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in report.get("classification_review_rows") or []:
            if not isinstance(row, dict):
                continue
            payload = {key: row.get(key) for key in fieldnames}
            payload["risk_tags"] = ";".join(row.get("risk_tags") or [])
            writer.writerow(payload)


def upstream_retag_queue_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in report.get("classification_review_rows") or []:
        if not isinstance(row, dict):
            continue
        suggestion = candidate_decision_for_row(
            {
                "risk_tags": row.get("risk_tags") or [],
                "Category": row.get("category"),
                "Account": row.get("account"),
                "amount": row.get("amount"),
            }
        )
        if suggestion.get("decision") != "upstream_retag_required":
            continue
        rows.append(
            {
                "fingerprint": row.get("fingerprint"),
                "date": row.get("date"),
                "month": row.get("month"),
                "account": row.get("account"),
                "amount": row.get("amount"),
                "category": row.get("category"),
                "type": row.get("type"),
                "merchant": row.get("merchant"),
                "description": row.get("description"),
                "notes": row.get("notes"),
                "risk_tags": ";".join(row.get("risk_tags") or []),
                "proposed_action": "retag_or_remove_from_804_property_split_upstream",
                "proposed_decision": suggestion.get("decision"),
                "reason": suggestion.get("reason"),
            }
        )
    return rows


def write_upstream_retag_queue_csv(path: Path, report: dict[str, Any]) -> None:
    fieldnames = [
        "fingerprint",
        "date",
        "month",
        "account",
        "amount",
        "category",
        "type",
        "merchant",
        "description",
        "notes",
        "risk_tags",
        "proposed_action",
        "proposed_decision",
        "reason",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in upstream_retag_queue_rows(report):
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def build_candidate_decisions(report: dict[str, Any]) -> dict[str, Any]:
    candidates = []
    for row in report.get("classification_review_rows") or []:
        if not isinstance(row, dict):
            continue
        suggestion = candidate_decision_for_row(
            {
                "risk_tags": row.get("risk_tags") or [],
                "Category": row.get("category"),
                "Account": row.get("account"),
                "amount": row.get("amount"),
            }
        )
        candidates.append(
            {
                "fingerprint": row.get("fingerprint"),
                "property": row.get("property"),
                "date": row.get("date"),
                "account": row.get("account"),
                "amount": row.get("amount"),
                "category": row.get("category"),
                "type": row.get("type"),
                "merchant": row.get("merchant"),
                "description": row.get("description"),
                "notes": row.get("notes"),
                "risk_tags": row.get("risk_tags") or [],
                "suggested_decision": suggestion["decision"],
                "confidence": suggestion["confidence"],
                "reason": suggestion["reason"],
                "reviewed": False,
                "reviewed_at": None,
                "note": "",
            }
        )
    counts = Counter(item["suggested_decision"] for item in candidates)
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        key = (
            str(candidate.get("suggested_decision") or ""),
            str(candidate.get("account") or ""),
            str(candidate.get("category") or ""),
            str(candidate.get("reason") or ""),
        )
        grouped[key].append(candidate)
    decision_groups = []
    for (suggested_decision, account, category, reason), rows in grouped.items():
        signed_amount_sum = round(sum(float(row.get("amount") or 0.0) for row in rows), 2)
        bucket_counts = Counter(
            disjoint_review_bucket({"risk_tags": row.get("risk_tags") or [], "Category": row.get("category"), "amount": row.get("amount")})
            for row in rows
        )
        high_priority = any(
            bucket in {
                "external_account_operating_or_capex",
                "external_account_revenue_or_receipt",
                "external_account_transfer",
                "owner_capital_or_distribution_external_account",
                "owner_capital_or_distribution_dao_account",
            }
            for bucket in bucket_counts
        )
        decision_groups.append(
            {
                "suggested_decision": suggested_decision,
                "account": account,
                "category": category,
                "reason": reason,
                "row_count": len(rows),
                "signed_amount_sum": signed_amount_sum,
                "absolute_amount_sum": round(sum(abs(float(row.get("amount") or 0.0)) for row in rows), 2),
                "cash_alignment_review_bucket_counts": dict(sorted(bucket_counts.items())),
                "cash_alignment_review_bucket_names": sorted(bucket_counts),
                "high_priority_review": high_priority,
                "transfer_basis_effect_if_approved": (
                    "retag_or_remove_from_804_before_transfer_reconciliation"
                    if suggested_decision == "upstream_retag_required"
                    else "include_as_dao_capital_or_reimbursement_not_bank_transfer_instruction"
                    if suggested_decision == "dao_capital_or_reimbursement"
                    else "include_as_true_804_cash_movement_after_pairing_review"
                    if suggested_decision == "true_804_cash_movement"
                    else "exclude_from_transfer_basis"
                ),
                "months": sorted({normalize_month(row.get("date")) for row in rows if normalize_month(row.get("date")) != "unknown"}),
                "fingerprints": [str(row.get("fingerprint") or "") for row in rows],
                "sample_rows": rows[:5],
                "reviewed": False,
                "reviewed_at": None,
                "note": "",
            }
        )
    decision_groups.sort(key=lambda item: (-abs(float(item["signed_amount_sum"])), item["suggested_decision"], item["account"]))
    return {
        "job": "audit-804-quitman-cash-alignment-candidate-decisions",
        "generated_at": iso_z(),
        "status": "review" if candidates else "ok",
        "source_report": report.get("gl_path"),
        "candidate_count": len(candidates),
        "suggested_decision_counts": dict(sorted(counts.items())),
        "decision_group_count": len(decision_groups),
        "decision_groups": decision_groups,
        "policy": "Candidate decisions are not applied. Copy reviewed rows into config/baselane_804_quitman_cash_alignment_decisions.json only after human review.",
        "decisions": candidates,
    }


def write_candidate_decisions(path: Path, report: dict[str, Any]) -> None:
    payload = build_candidate_decisions(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_decision_template(candidate_payload: dict[str, Any]) -> dict[str, Any]:
    decisions = []
    decision_groups = candidate_payload.get("decision_groups") or []
    group_summaries = []
    for group_index, group in enumerate(decision_groups, start=1):
        if not isinstance(group, dict):
            continue
        group_id = f"group-{group_index:03d}"
        group_summaries.append(
            {
                "group_id": group_id,
                "suggested_decision": group.get("suggested_decision"),
                "account": group.get("account"),
                "category": group.get("category"),
                "row_count": group.get("row_count"),
                "signed_amount_sum": group.get("signed_amount_sum"),
                "months": group.get("months") or [],
                "reason": group.get("reason"),
                "absolute_amount_sum": group.get("absolute_amount_sum"),
                "cash_alignment_review_bucket_counts": group.get("cash_alignment_review_bucket_counts") or {},
                "cash_alignment_review_bucket_names": group.get("cash_alignment_review_bucket_names") or [],
                "high_priority_review": group.get("high_priority_review") is True,
                "transfer_basis_effect_if_approved": group.get("transfer_basis_effect_if_approved"),
                "reviewed": False,
                "reviewed_at": None,
                "note": "",
            }
        )
        for fingerprint in group.get("fingerprints") or []:
            if not fingerprint:
                continue
            decisions.append(
                {
                    "fingerprint": fingerprint,
                    "group_id": group_id,
                    "decision": group.get("suggested_decision"),
                    "reviewed": False,
                    "reviewed_at": None,
                    "note": (
                        f"Review group: {group.get('row_count')} rows, "
                        f"{group.get('account')}, {group.get('category')}; {group.get('reason')}"
                    ),
                }
            )
    return {
        "status": "review_required",
        "generated_at": iso_z(),
        "policy": (
            "Template only. Prefer reviewing group_summaries: set reviewed=true, reviewed_at, decision, and note on a whole group "
            "only when every row in that group has the same approved treatment. Row-level decisions remain available for exceptions."
        ),
        "candidate_count": candidate_payload.get("candidate_count"),
        "decision_group_count": candidate_payload.get("decision_group_count"),
        "suggested_decision_counts": candidate_payload.get("suggested_decision_counts") or {},
        "group_summaries": group_summaries,
        "decision_count": len(decisions),
        "decisions": decisions,
    }


def build_review_progress(template_payload: dict[str, Any], candidate_payload: dict[str, Any]) -> dict[str, Any]:
    groups = template_payload.get("group_summaries") if isinstance(template_payload.get("group_summaries"), list) else []
    if not groups:
        groups = build_decision_template(candidate_payload).get("group_summaries") or []
    total_count = len(groups)
    reviewed_groups = [group for group in groups if isinstance(group, dict) and group.get("reviewed") is True]
    unreviewed_groups = [group for group in groups if isinstance(group, dict) and group.get("reviewed") is not True]
    high_priority_groups = [group for group in groups if isinstance(group, dict) and group.get("high_priority_review") is True]
    high_priority_unreviewed = [group for group in unreviewed_groups if group.get("high_priority_review") is True]

    def amount_sum(source: list[dict[str, Any]], key: str) -> float:
        return round(sum(float(group.get(key) or 0.0) for group in source), 2)

    bucket_remaining: dict[str, float] = defaultdict(float)
    bucket_reviewed: dict[str, float] = defaultdict(float)
    remaining_by_effect: dict[str, float] = defaultdict(float)
    reviewed_by_effect: dict[str, float] = defaultdict(float)
    remaining_count_by_effect: dict[str, int] = defaultdict(int)
    reviewed_count_by_effect: dict[str, int] = defaultdict(int)
    for group in groups:
        if not isinstance(group, dict):
            continue
        bucket_names = group.get("cash_alignment_review_bucket_names") or []
        reviewed = group.get("reviewed") is True
        target = bucket_reviewed if reviewed else bucket_remaining
        for bucket in bucket_names:
            target[str(bucket)] += float(group.get("signed_amount_sum") or 0.0)
        effect = str(group.get("transfer_basis_effect_if_approved") or "unknown")
        if reviewed:
            reviewed_by_effect[effect] += float(group.get("signed_amount_sum") or 0.0)
            reviewed_count_by_effect[effect] += int(group.get("row_count") or 0)
        else:
            remaining_by_effect[effect] += float(group.get("signed_amount_sum") or 0.0)
            remaining_count_by_effect[effect] += int(group.get("row_count") or 0)
    sorted_groups = sorted(
        groups,
        key=lambda item: (
            item.get("reviewed") is True,
            item.get("high_priority_review") is not True,
            -abs(float(item.get("signed_amount_sum") or 0.0)),
            str(item.get("group_id") or ""),
        ),
    )

    def compact_group(group: dict[str, Any]) -> dict[str, Any]:
        return {
            "group_id": group.get("group_id"),
            "suggested_decision": group.get("suggested_decision"),
            "account": group.get("account"),
            "category": group.get("category"),
            "row_count": group.get("row_count"),
            "signed_amount_sum": group.get("signed_amount_sum"),
            "absolute_amount_sum": group.get("absolute_amount_sum"),
            "high_priority_review": group.get("high_priority_review") is True,
            "cash_alignment_review_bucket_names": group.get("cash_alignment_review_bucket_names") or [],
            "transfer_basis_effect_if_approved": group.get("transfer_basis_effect_if_approved"),
            "reviewed": group.get("reviewed") is True,
            "reviewed_at": group.get("reviewed_at"),
            "note": group.get("note"),
        }

    return {
        "status": "ok" if not unreviewed_groups else "review",
        "group_count": total_count,
        "reviewed_group_count": len(reviewed_groups),
        "unreviewed_group_count": len(unreviewed_groups),
        "group_completion_ratio": round((len(reviewed_groups) / total_count) if total_count else 1.0, 6),
        "high_priority_group_count": len(high_priority_groups),
        "high_priority_unreviewed_group_count": len(high_priority_unreviewed),
        "reviewed_signed_amount_sum": amount_sum(reviewed_groups, "signed_amount_sum"),
        "unreviewed_signed_amount_sum": amount_sum(unreviewed_groups, "signed_amount_sum"),
        "reviewed_absolute_amount_sum": amount_sum(reviewed_groups, "absolute_amount_sum"),
        "unreviewed_absolute_amount_sum": amount_sum(unreviewed_groups, "absolute_amount_sum"),
        "remaining_signed_amount_by_bucket": {key: round(value, 2) for key, value in sorted(bucket_remaining.items())},
        "reviewed_signed_amount_by_bucket": {key: round(value, 2) for key, value in sorted(bucket_reviewed.items())},
        "remaining_signed_amount_by_transfer_basis_effect": {
            key: round(value, 2) for key, value in sorted(remaining_by_effect.items())
        },
        "reviewed_signed_amount_by_transfer_basis_effect": {
            key: round(value, 2) for key, value in sorted(reviewed_by_effect.items())
        },
        "remaining_row_count_by_transfer_basis_effect": dict(sorted(remaining_count_by_effect.items())),
        "reviewed_row_count_by_transfer_basis_effect": dict(sorted(reviewed_count_by_effect.items())),
        "all_review_groups": [compact_group(group) for group in sorted_groups],
        "next_review_groups": [
            compact_group(group)
            for group in sorted_groups
            if group.get("reviewed") is not True
        ][:10],
        "policy": (
            "Review progress is informational and does not approve transfer movement. "
            "804 source-clean requires every current review group to be reviewed and copied into the live decisions config."
        ),
    }


def write_decision_template(path: Path, report: dict[str, Any]) -> None:
    candidate_payload = build_candidate_decisions(report)
    payload = build_decision_template(candidate_payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_group_review_queue_csv(path: Path, review_progress: dict[str, Any]) -> None:
    groups = review_progress.get("all_review_groups") or review_progress.get("next_review_groups")
    groups = groups if isinstance(groups, list) else []
    fieldnames = [
        "group_id",
        "high_priority_review",
        "suggested_decision",
        "transfer_basis_effect_if_approved",
        "row_count",
        "signed_amount_sum",
        "absolute_amount_sum",
        "account",
        "category",
        "cash_alignment_review_bucket_names",
        "evidence_digest",
        "reviewed",
        "reviewed_at",
        "approved_decision",
        "review_note",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for group in groups:
            if not isinstance(group, dict):
                continue
            writer.writerow(
                {
                    "group_id": group.get("group_id"),
                    "high_priority_review": group.get("high_priority_review") is True,
                    "suggested_decision": group.get("suggested_decision"),
                    "transfer_basis_effect_if_approved": group.get("transfer_basis_effect_if_approved"),
                    "row_count": group.get("row_count"),
                    "signed_amount_sum": group.get("signed_amount_sum"),
                    "absolute_amount_sum": group.get("absolute_amount_sum"),
                    "account": group.get("account"),
                    "category": group.get("category"),
                    "cash_alignment_review_bucket_names": ";".join(
                        str(bucket) for bucket in (group.get("cash_alignment_review_bucket_names") or [])
                    ),
                    "evidence_digest": group_review_evidence_digest(group),
                    "reviewed": "",
                    "reviewed_at": "",
                    "approved_decision": group.get("suggested_decision"),
                    "review_note": "",
                }
            )


def write_reviewed_template_scaffold(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    candidate_payload = build_candidate_decisions(report)
    payload = build_decision_template(candidate_payload)
    payload["review_progress"] = build_review_progress(payload, candidate_payload)
    payload["status"] = "review_required"
    payload["policy"] = (
        "Human review workspace. Set reviewed=true, reviewed_at, decision, and note for rows you approve. "
        "Rerun audit to produce reports/baselane_804_quitman_cash_alignment_proposed_decisions.json. "
        "This file is not a live decision config until reviewed rows are copied to "
        "config/baselane_804_quitman_cash_alignment_decisions.json."
    )
    if path.exists():
        existing = read_json(path)
        existing_decisions = existing.get("decisions") if isinstance(existing.get("decisions"), list) else []
        existing_groups = existing.get("group_summaries") if isinstance(existing.get("group_summaries"), list) else []
        existing_shape_valid = isinstance(existing.get("decisions"), list) or isinstance(existing.get("group_summaries"), list)
        reviewed_count = sum(1 for item in existing_decisions if isinstance(item, dict) and item.get("reviewed") is True)
        reviewed_group_count = sum(1 for item in existing_groups if isinstance(item, dict) and item.get("reviewed") is True)
        if reviewed_count == 0 and reviewed_group_count == 0:
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return {
                "status": "recovered_placeholder" if not existing_shape_valid else "refreshed_unreviewed",
                "path": str(path),
                "written": True,
                "decision_count": payload.get("decision_count"),
                "decision_group_count": payload.get("decision_group_count"),
                "existing_shape_valid": existing_shape_valid,
                "policy": (
                    "Existing placeholder/invalid reviewed template recovered with current group metadata."
                    if not existing_shape_valid
                    else "Existing unreviewed scaffold refreshed with current group metadata."
                ),
            }
        return {
            "status": "exists",
            "path": str(path),
            "written": False,
            "reviewed_decision_count": reviewed_count,
            "reviewed_group_count": reviewed_group_count,
            "policy": "Existing reviewed template with human-reviewed rows or groups preserved; no scaffold overwrite.",
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "created",
        "path": str(path),
        "written": True,
        "decision_count": payload.get("decision_count"),
        "decision_group_count": payload.get("decision_group_count"),
        "policy": "Scaffold only; no decisions are reviewed or live.",
    }


def markdown_money(value: object) -> str:
    amount = float(value or 0.0)
    return f"${amount:,.2f}"


def build_review_markdown(report: dict[str, Any], candidate_payload: dict[str, Any]) -> str:
    progress = report.get("review_progress") if isinstance(report.get("review_progress"), dict) else {}
    lines = [
        "# 804 S Quitman Cash Alignment Review",
        "",
        f"Generated at: {iso_z()}",
        "",
        "## Current Gate",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Rows needing review: `{report.get('classification_review_count')}`",
        f"- Reviewed live decisions: `{report.get('review_decision_count')}`",
        f"- Candidate groups: `{candidate_payload.get('decision_group_count')}`",
        f"- Reviewed groups: `{progress.get('reviewed_group_count', 0)}/{progress.get('group_count', candidate_payload.get('decision_group_count') or 0)}`",
        f"- High-priority groups remaining: `{progress.get('high_priority_unreviewed_group_count', progress.get('high_priority_group_count', 0))}`",
        f"- Unreviewed absolute exposure: `{markdown_money(progress.get('unreviewed_absolute_amount_sum') or 0)}`",
        f"- Source clean status: `{(report.get('source_clean_gate') or {}).get('status') or 'review'}`",
        f"- Reviewed decision full clearance: `{str(report.get('reviewed_decision_full_clearance') is True).lower()}`",
        f"- Suggested decision counts: `{json.dumps(candidate_payload.get('suggested_decision_counts') or {}, sort_keys=True)}`",
        "",
        "## Review Policy",
        "",
        "- These grouped suggestions are not applied automatically.",
        "- Confirm each group against Baselane/source records before marking rows reviewed.",
        "- Save reviewed rows to `config/baselane_804_quitman_cash_alignment_reviewed_template.json`; rerun audit to produce proposed live decisions.",
        "- Or complete `reports/baselane_804_quitman_cash_alignment_group_review_queue.csv`, then rerun with `--import-group-review-csv` to update the reviewed template.",
        "- Import command: `bash reports/baselane_804_quitman_cash_alignment_import_group_review.requires-explicit-approval.sh`.",
        f"- Accepted `approved_decision` values: `{', '.join(sorted(VALID_DECISIONS))}`.",
        "- Reviewed rows must retain the current `evidence_digest`, include `reviewed_at`, and include a substantive `review_note`.",
        "- Do not move 804 cash until transfer reconciliation is final and Telegram proof is live, not dry-run.",
        "",
        "## Net Cash Exposure Buckets",
        "",
    ]
    exposure = report.get("net_cash_exposure_review") if isinstance(report.get("net_cash_exposure_review"), dict) else {}
    by_bucket = exposure.get("by_bucket") if isinstance(exposure.get("by_bucket"), dict) else {}
    row_counts = exposure.get("row_count_by_bucket") if isinstance(exposure.get("row_count_by_bucket"), dict) else {}
    high_priority_names = set(exposure.get("high_priority_bucket_names") or [])
    if by_bucket:
        lines.extend(["| Bucket | Rows | Signed Sum | Priority |", "| --- | ---: | ---: | --- |"])
        for bucket, amount in sorted(by_bucket.items(), key=lambda item: abs(float(item[1] or 0.0)), reverse=True):
            lines.append(
                f"| `{bucket}` | `{row_counts.get(bucket, 0)}` | {markdown_money(amount)} | "
                f"{'high' if bucket in high_priority_names else 'normal'} |"
            )
        lines.append("")
    remaining_by_bucket = progress.get("remaining_signed_amount_by_bucket") if isinstance(progress.get("remaining_signed_amount_by_bucket"), dict) else {}
    if remaining_by_bucket:
        lines.extend([
            "## Remaining Review Exposure",
            "",
            "| Bucket | Unreviewed Signed Sum |",
            "| --- | ---: |",
        ])
        for bucket, amount in sorted(remaining_by_bucket.items(), key=lambda item: abs(float(item[1] or 0.0)), reverse=True):
            lines.append(f"| `{bucket}` | {markdown_money(amount)} |")
        lines.append("")
    remaining_by_effect = (
        progress.get("remaining_signed_amount_by_transfer_basis_effect")
        if isinstance(progress.get("remaining_signed_amount_by_transfer_basis_effect"), dict)
        else {}
    )
    remaining_count_by_effect = (
        progress.get("remaining_row_count_by_transfer_basis_effect")
        if isinstance(progress.get("remaining_row_count_by_transfer_basis_effect"), dict)
        else {}
    )
    if remaining_by_effect:
        lines.extend([
            "## Remaining Transfer-Basis Effect",
            "",
            "| Effect If Approved | Rows | Unreviewed Signed Sum |",
            "| --- | ---: | ---: |",
        ])
        for effect, amount in sorted(remaining_by_effect.items(), key=lambda item: abs(float(item[1] or 0.0)), reverse=True):
            lines.append(f"| `{effect}` | `{remaining_count_by_effect.get(effect, 0)}` | {markdown_money(amount)} |")
        lines.append("")
    next_groups = progress.get("next_review_groups") if isinstance(progress.get("next_review_groups"), list) else []
    if next_groups:
        lines.extend([
            "## Next Groups To Review",
            "",
            "| Group | Suggested Decision | Rows | Signed Sum | High Priority | Effect |",
            "| --- | --- | ---: | ---: | --- | --- |",
        ])
        for group in next_groups:
            if not isinstance(group, dict):
                continue
            lines.append(
                f"| `{group.get('group_id')}` | `{group.get('suggested_decision')}` | "
                f"`{group.get('row_count')}` | {markdown_money(group.get('signed_amount_sum'))} | "
                f"`{str(group.get('high_priority_review') is True).lower()}` | "
                f"`{group.get('transfer_basis_effect_if_approved')}` |"
            )
        lines.append("")
    lines.extend([
        "## Top Review Groups",
        "",
    ])
    for index, group in enumerate(candidate_payload.get("decision_groups") or [], start=1):
        if not isinstance(group, dict):
            continue
        lines.extend(
            [
                f"### {index}. {group.get('suggested_decision')} — {markdown_money(group.get('signed_amount_sum'))}",
                "",
                f"- Rows: `{group.get('row_count')}`",
                f"- Account: `{group.get('account')}`",
                f"- Category: `{group.get('category')}`",
                f"- Months: `{', '.join(group.get('months') or [])}`",
                f"- Reason: {group.get('reason')}",
                f"- Review buckets: `{json.dumps(group.get('cash_alignment_review_bucket_counts') or {}, sort_keys=True)}`",
                f"- Transfer-basis effect if approved: `{group.get('transfer_basis_effect_if_approved')}`",
                f"- High priority: `{str(group.get('high_priority_review') is True).lower()}`",
                "",
                "| Date | Amount | Merchant | Notes | Fingerprint |",
                "| --- | ---: | --- | --- | --- |",
            ]
        )
        for row in group.get("sample_rows") or []:
            if not isinstance(row, dict):
                continue
            fingerprint = str(row.get("fingerprint") or "")
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(row.get("date") or ""),
                        markdown_money(row.get("amount")),
                        str(row.get("merchant") or "").replace("|", "/"),
                        str(row.get("notes") or row.get("note") or "").replace("|", "/"),
                        f"`{fingerprint[:96]}`",
                    ]
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_review_markdown(path: Path, report: dict[str, Any]) -> None:
    candidate_payload = build_candidate_decisions(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_review_markdown(report, candidate_payload), encoding="utf-8")


def write_decision_validation(path: Path, report: dict[str, Any]) -> None:
    source_clean_gate = report.get("source_clean_gate") if isinstance(report.get("source_clean_gate"), dict) else {}
    decision_validation = report.get("decision_validation") if isinstance(report.get("decision_validation"), dict) else {}
    review_rows_remaining = int(source_clean_gate.get("review_rows_remaining") or report.get("classification_review_count") or 0)
    live_decision_count = int(source_clean_gate.get("live_decision_count") or report.get("review_decision_count") or 0)
    effective_issues = list(decision_validation.get("issues") or [])
    if review_rows_remaining > 0:
        effective_issues.append(
            {
                "code": "source_clean_review_rows_remaining",
                "review_rows_remaining": review_rows_remaining,
            }
        )
    upstream_retag_required_remaining_count = int(
        source_clean_gate.get("upstream_retag_required_remaining_count")
        or report.get("upstream_retag_required_remaining_count")
        or 0
    )
    if upstream_retag_required_remaining_count > 0:
        effective_issues.append(
            {
                "code": "upstream_retag_required_rows_still_in_804_gl",
                "upstream_retag_required_remaining_count": upstream_retag_required_remaining_count,
                "upstream_retag_required_remaining_sum": (
                    source_clean_gate.get("upstream_retag_required_remaining_sum")
                    or report.get("upstream_retag_required_remaining_sum")
                    or 0.0
                ),
            }
        )
    if live_decision_count == 0 and review_rows_remaining > 0:
        effective_issues.append({"code": "no_live_review_decisions_for_required_review_rows"})
    raw_validation_status = decision_validation.get("status")
    effective_status = "ok" if not effective_issues and report.get("source_clean_status") == "ok" else "review"
    payload = {
        "job": "audit-804-quitman-cash-alignment-decision-validation",
        "generated_at": iso_z(),
        "source_report": report.get("gl_path"),
        "decisions_path": report.get("decisions_path"),
        **decision_validation,
        "raw_validation_status": raw_validation_status,
        "status": effective_status,
        "source_clean_status": report.get("source_clean_status"),
        "source_clean_gate": source_clean_gate,
        "review_progress": report.get("review_progress") if isinstance(report.get("review_progress"), dict) else {},
        "effective_status": effective_status,
        "effective_issue_count": len(effective_issues),
        "effective_issues": effective_issues[:200],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gl", type=Path, default=DEFAULT_GL)
    parser.add_argument("--candidate-packet", type=Path, default=DEFAULT_CANDIDATE_PACKET)
    parser.add_argument("--transfer-report", type=Path, default=DEFAULT_TRANSFER_REPORT)
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS)
    parser.add_argument("--reviewed-template", type=Path, default=DEFAULT_REVIEWED_TEMPLATE)
    parser.add_argument("--proposed-decisions", type=Path, default=DEFAULT_PROPOSED_DECISIONS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--review-queue-csv", type=Path, default=DEFAULT_REVIEW_QUEUE_CSV)
    parser.add_argument("--group-review-queue-csv", type=Path, default=DEFAULT_GROUP_REVIEW_QUEUE_CSV)
    parser.add_argument("--candidate-decisions", type=Path, default=DEFAULT_CANDIDATE_DECISIONS)
    parser.add_argument("--decision-template", type=Path, default=DEFAULT_DECISION_TEMPLATE)
    parser.add_argument("--review-markdown", type=Path, default=DEFAULT_REVIEW_MARKDOWN)
    parser.add_argument("--decision-validation", type=Path, default=DEFAULT_DECISION_VALIDATION)
    parser.add_argument("--upstream-retag-queue-csv", type=Path, default=DEFAULT_UPSTREAM_RETAG_QUEUE_CSV)
    parser.add_argument("--import-commands", type=Path, default=DEFAULT_IMPORT_COMMANDS)
    parser.add_argument("--import-group-review-csv", action="store_true")
    args = parser.parse_args(argv)
    report = build_report(
        args.gl,
        args.candidate_packet,
        args.transfer_report,
        args.decisions,
        args.reviewed_template,
        args.proposed_decisions,
    )
    scaffold_status = write_reviewed_template_scaffold(args.reviewed_template, report)
    group_review_csv_import = None
    if args.import_group_review_csv:
        group_review_csv_import = import_group_review_csv_to_template(
            args.group_review_queue_csv,
            args.reviewed_template,
        )
        report = build_report(
            args.gl,
            args.candidate_packet,
            args.transfer_report,
            args.decisions,
            args.reviewed_template,
            args.proposed_decisions,
        )
        scaffold_status = write_reviewed_template_scaffold(args.reviewed_template, report)
    candidate_payload = build_candidate_decisions(report)
    reviewed_template_payload = read_json(args.reviewed_template)
    review_progress = build_review_progress(reviewed_template_payload, candidate_payload)
    report["review_progress"] = review_progress
    report["reviewed_template_scaffold"] = scaffold_status
    report["group_review_csv_import"] = group_review_csv_import
    report["reviewed_template_reviewed_group_count"] = review_progress["reviewed_group_count"]
    report["reviewed_template_source_group_count"] = review_progress["group_count"]
    report["reviewed_template_unreviewed_group_count"] = review_progress["unreviewed_group_count"]
    report["reviewed_template_high_priority_unreviewed_group_count"] = review_progress[
        "high_priority_unreviewed_group_count"
    ]
    report["reviewed_template_unreviewed_absolute_amount_sum"] = review_progress["unreviewed_absolute_amount_sum"]
    upstream_retag_rows = upstream_retag_queue_rows(report)
    report["upstream_retag_queue_csv"] = str(args.upstream_retag_queue_csv)
    report["upstream_retag_queue_count"] = len(upstream_retag_rows)
    report["upstream_retag_queue_amount_sum"] = round(sum(float(row.get("amount") or 0.0) for row in upstream_retag_rows), 2)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_review_queue_csv(args.review_queue_csv, report)
    write_upstream_retag_queue_csv(args.upstream_retag_queue_csv, report)
    write_group_review_queue_csv(args.group_review_queue_csv, review_progress)
    write_candidate_decisions(args.candidate_decisions, report)
    write_decision_template(args.decision_template, report)
    write_review_markdown(args.review_markdown, report)
    write_decision_validation(args.decision_validation, report)
    write_group_review_import_commands(args.import_commands)
    print(
        f"status={report['status']} review_rows={report['classification_review_count']} "
        f"reviewed_template_scaffold={scaffold_status['status']} report={args.report}"
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
