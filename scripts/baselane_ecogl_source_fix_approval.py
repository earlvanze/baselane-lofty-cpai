#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import baselane_ecogl_source_fix_correction_validator as correction_validator


APPROVAL_SCOPE = "baselane_ecogl_source_fix_category_approval"
INVALID_EMPTY_CATEGORIES = {"", "uncategorized", "uncategorized expense"}
BASELANE_CATEGORY_ALIASES = {
    "Cleaning & Janitorial": "Cleaning & Maintenance",
    "Gardening & Landscaping": "Cleaning & Maintenance",
    "Landscaping": "Cleaning & Maintenance",
    "Remodeling": "Repairs",
    "Water & Sewer": "Utilities",
}
AUTO_APPROVAL_STATUS = "automation_safe_exact_history"
PUBLIC_DOCUMENT_AUTO_APPROVAL_STATUS = "automation_safe_public_document"
EMAIL_RECEIPT_AUTO_APPROVAL_STATUS = "automation_safe_email_receipt"
SAME_MERCHANT_AUTO_APPROVAL_STATUS = "automation_safe_same_merchant_history"
NATIVE_SPLIT_AUTO_APPROVAL_STATUS = "automation_safe_native_split"
GOVERNMENT_REGISTRATION_AUTO_APPROVAL_STATUS = "automation_safe_government_registration"
HEMLANE_LIVE_AUTO_APPROVAL_STATUS = "automation_safe_hemlane_live_transaction"
MATERIAL_FIELDS = [
    "id",
    "fix_status",
    "property",
    "date",
    "amount",
    "merchant",
    "description",
    "current_label",
    "historical_evidence_status",
    "historical_suggested_category",
    "historical_category_counts",
    "context_candidate_status",
    "context_candidate_category",
    "context_candidate_reason",
    "document_support_count",
    "document_checked_file_count",
    "document_limit_reached",
    "document_category_counts",
    "email_invoice_evidence_required",
    "email_invoice_search_query",
    "local_mail_invoice_status",
    "local_mail_invoice_match_count",
    "gws_mail_invoice_status",
    "gws_mail_invoice_match_count",
    "baselane_match_key",
    "email_receipt_category_evidence",
]


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def row_digest(row: dict[str, Any]) -> str:
    material = {field: str(row.get(field) or "").strip() for field in MATERIAL_FIELDS}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def approval_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    if data.get("approval_scope") != APPROVAL_SCOPE:
        return []
    entries = data.get("approvals")
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def read_csv_rows(path: Path) -> tuple[list[dict[str, str]], list[str], list[str]]:
    if not path.is_file():
        return [], [], [f"missing_csv:{path}"]
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            return [{key: str(value or "") for key, value in row.items()} for row in reader], list(reader.fieldnames or []), []
    except Exception as exc:  # noqa: BLE001
        return [], [], [f"unreadable_csv:{exc}"]


def approval_category(entry: dict[str, Any], fallback: str = "") -> str:
    return str(entry.get("category_to_set") or entry.get("operator_category_to_set_in_baselane") or fallback or "").strip()


def baselane_category(category: str) -> str:
    category = str(category or "").strip()
    return BASELANE_CATEGORY_ALIASES.get(category, category)


def is_empty_category(category: str) -> bool:
    return category.strip().lower() in INVALID_EMPTY_CATEGORIES


def json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        data = json.loads(value)
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def evidence_profile(row: dict[str, Any]) -> dict[str, Any]:
    historical_status = str(row.get("historical_evidence_status") or "unknown")
    context_status = str(row.get("context_candidate_status") or "unknown")
    category_counts = json_object(row.get("historical_category_counts"))
    document_counts = json_object(row.get("document_category_counts"))
    email_invoice_required = str(row.get("email_invoice_evidence_required") or "").lower() == "true"
    local_mail_invoice_match_count = count(row.get("local_mail_invoice_match_count"))
    gws_mail_invoice_match_count = count(row.get("gws_mail_invoice_match_count"))
    email_source_match_count = local_mail_invoice_match_count + gws_mail_invoice_match_count
    email_receipt_status = str(row.get("email_receipt_category_evidence_status") or row.get("email_receipt_evidence_status") or "")
    email_receipt_category = str(row.get("email_receipt_category_evidence_category") or row.get("email_receipt_evidence_category") or "").strip()
    historical_category = str(row.get("historical_suggested_category") or "").strip()
    context_category = str(row.get("context_candidate_category") or "").strip()
    candidate_category = context_category or email_receipt_category
    support_count = count(row.get("historical_support_count"))
    conflict_count = count(row.get("historical_conflict_count"))
    if not support_count and category_counts:
        support_count = sum(count(value) for value in category_counts.values())
    if not conflict_count and len(category_counts) > 1:
        conflict_count = len(category_counts)

    if historical_status == "conflicting_support" or context_status == "conflicting_context" or conflict_count > 1:
        recommendation = "blocked_conflicting_support"
        confidence = "blocked"
        evidence_needed = "Resolve conflicting same-property/same-merchant category history before setting Baselane source category."
    elif historical_status == "no_support" and context_status == "no_deterministic_candidate" and email_invoice_required and email_source_match_count:
        recommendation = "review_email_invoice_evidence"
        confidence = "weak"
        evidence_needed = "Review matched email invoice/receipt artifact and set the exact Baselane category from that source evidence."
    elif historical_status == "no_support" and context_status == "no_deterministic_candidate" and email_invoice_required:
        recommendation = "blocked_email_invoice_required"
        confidence = "blocked"
        evidence_needed = "Find the matching invoice/receipt in email; do not infer category from payment rail or person-payment merchant alone."
    elif historical_status == "no_support" and context_status == "no_deterministic_candidate":
        recommendation = "blocked_no_support"
        confidence = "blocked"
        evidence_needed = "Find source documentation or a deterministic recurring pattern; do not infer category from merchant alone."
    elif context_status in {
        AUTO_APPROVAL_STATUS,
        PUBLIC_DOCUMENT_AUTO_APPROVAL_STATUS,
        EMAIL_RECEIPT_AUTO_APPROVAL_STATUS,
        SAME_MERCHANT_AUTO_APPROVAL_STATUS,
        NATIVE_SPLIT_AUTO_APPROVAL_STATUS,
        GOVERNMENT_REGISTRATION_AUTO_APPROVAL_STATUS,
        HEMLANE_LIVE_AUTO_APPROVAL_STATUS,
    }:
        recommendation = "ready_for_deterministic_auto_approval"
        confidence = "strong"
        if context_status == EMAIL_RECEIPT_AUTO_APPROVAL_STATUS:
            evidence_needed = "Auto-approvable: matched email receipt confirms payment with property, amount, and service keyword; category is deterministic."
        elif context_status == PUBLIC_DOCUMENT_AUTO_APPROVAL_STATUS:
            evidence_needed = "Auto-approvable: public Dropbox source document evidence supports the category with no conflicts."
        elif context_status == SAME_MERCHANT_AUTO_APPROVAL_STATUS:
            evidence_needed = "Auto-approvable: same-property/same-merchant source history repeatedly supports one category with no conflicts."
        elif context_status == NATIVE_SPLIT_AUTO_APPROVAL_STATUS:
            evidence_needed = "Auto-approvable: vendor is governed by a Baselane-native split rule with deterministic target category."
        elif context_status == GOVERNMENT_REGISTRATION_AUTO_APPROVAL_STATUS:
            evidence_needed = "Auto-approvable: government registration merchant deterministically supports the category with no conflicts."
        elif context_status == HEMLANE_LIVE_AUTO_APPROVAL_STATUS:
            evidence_needed = "Auto-approvable: live Hemlane completed rent transaction matches property, amount, and date window with no ambiguity."
        else:
            evidence_needed = "Auto-approvable only when exact historical evidence, context candidate, and worksheet category all match with no conflicts."
    elif email_receipt_status == EMAIL_RECEIPT_AUTO_APPROVAL_STATUS and email_receipt_category:
        recommendation = "ready_for_deterministic_auto_approval"
        confidence = "strong"
        evidence_needed = "Auto-approvable: matched email receipt confirms payment with property, amount, and service keyword; category is deterministic."
        candidate_category = email_receipt_category
    elif candidate_category:
        recommendation = "review_weak_support"
        confidence = "weak"
        evidence_needed = "Get another independent same-property/same-merchant/same-amount source row or source document before approval."
    else:
        recommendation = "blocked_insufficient_evidence"
        confidence = "blocked"
        evidence_needed = "Add deterministic source evidence before selecting a Baselane category."

    return {
        "autonomy_recommendation": recommendation,
        "automation_confidence": confidence,
        "candidate_category": candidate_category,
        "evidence_needed": evidence_needed,
        "evidence_summary": {
            "historical_status": historical_status,
            "historical_support_count": support_count,
            "historical_conflict_count": conflict_count,
            "historical_category_counts": category_counts,
            "context_status": context_status,
            "context_candidate_category": context_category,
            "document_support_count": count(row.get("document_support_count")),
            "document_checked_file_count": count(row.get("document_checked_file_count")),
            "document_category_counts": document_counts,
            "document_limit_reached": str(row.get("document_limit_reached") or "").lower() == "true",
            "email_invoice_evidence_required": email_invoice_required,
            "payment_rail": row.get("payment_rail") or "",
            "payee_tokens": row.get("payee_tokens") or "",
            "email_invoice_search_query": row.get("email_invoice_search_query") or "",
            "email_invoice_expected_window": row.get("email_invoice_expected_window") or "",
            "local_mail_invoice_status": row.get("local_mail_invoice_status") or "",
            "local_mail_invoice_match_count": local_mail_invoice_match_count,
            "local_mail_invoice_checked_file_count": count(row.get("local_mail_invoice_checked_file_count")),
            "gws_mail_invoice_status": row.get("gws_mail_invoice_status") or "",
            "gws_mail_invoice_match_count": gws_mail_invoice_match_count,
            "email_receipt_status": email_receipt_status,
            "email_receipt_category": email_receipt_category,
        },
    }


def deterministic_auto_approval_reason(row: dict[str, Any], category: str, allowed: set[str]) -> str:
    category = baselane_category(category)
    if is_empty_category(category) or category not in allowed:
        return ""
    historical_status = str(row.get("historical_evidence_status") or "")
    context_status = str(row.get("context_candidate_status") or "")
    historical_category = baselane_category(str(row.get("historical_suggested_category") or "").strip())
    context_category = baselane_category(str(row.get("context_candidate_category") or "").strip())
    support_count = count(row.get("historical_support_count"))
    conflict_count = count(row.get("historical_conflict_count"))
    category_counts = json_object(row.get("historical_category_counts"))
    if not support_count and category_counts:
        support_count = sum(count(value) for value in category_counts.values())
    positive_categories = {
        baselane_category(str(key).strip()): count(value)
        for key, value in category_counts.items()
        if str(key).strip() and count(value) > 0
    }
    if not conflict_count and len(positive_categories) > 1:
        conflict_count = len(positive_categories)
    if context_status == AUTO_APPROVAL_STATUS:
        if historical_status != "strong_support":
            return ""
        if historical_category != category or context_category != category:
            return ""
        if support_count < 2 or conflict_count != 0:
            return ""
        if positive_categories and set(positive_categories) != {category}:
            return ""
        return (
            f"Deterministic auto-approval: {support_count} exact historical source rows support "
            f"{category!r}; no conflicting category evidence."
        )
    if context_status == PUBLIC_DOCUMENT_AUTO_APPROVAL_STATUS:
        if context_category != category:
            return ""
        if historical_status == "conflicting_support" or conflict_count > 1:
            return ""
        return f"Deterministic auto-approval: public Dropbox source document evidence supports {category!r}."
    email_receipt_status = str(row.get("email_receipt_category_evidence_status") or row.get("email_receipt_evidence_status") or "")
    email_receipt_category = baselane_category(
        str(row.get("email_receipt_category_evidence_category") or row.get("email_receipt_evidence_category") or "").strip()
    )
    if email_receipt_status == EMAIL_RECEIPT_AUTO_APPROVAL_STATUS:
        if email_receipt_category != category:
            return ""
        if historical_status == "conflicting_support" or conflict_count > 1:
            return ""
        return f"Deterministic auto-approval: matched email receipt confirms payment and service keyword for {category!r}."
    if context_status == SAME_MERCHANT_AUTO_APPROVAL_STATUS:
        if context_category != category:
            return ""
        if historical_status == "conflicting_support" or conflict_count > 1:
            return ""
        return f"Deterministic auto-approval: same-property/same-merchant source history repeatedly supports {category!r}."
    if context_status == NATIVE_SPLIT_AUTO_APPROVAL_STATUS:
        if context_category != category:
            return ""
        if historical_status == "conflicting_support" or conflict_count > 1:
            return ""
        return f"Deterministic auto-approval: Baselane-native split rule supports {category!r}."
    if context_status == GOVERNMENT_REGISTRATION_AUTO_APPROVAL_STATUS:
        if context_category != category:
            return ""
        if historical_status == "conflicting_support" or conflict_count > 1:
            return ""
        return f"Deterministic auto-approval: government registration merchant rule supports {category!r}."
    if context_status == HEMLANE_LIVE_AUTO_APPROVAL_STATUS:
        if context_category != category:
            return ""
        if category != "Rents":
            return ""
        if historical_status == "conflicting_support" or conflict_count > 1:
            return ""
        return "Deterministic auto-approval: live Hemlane completed rent transaction supports 'Rents'."
    return ""


def build_report(root: Path, corrections_report_path: Path, corrections_csv_path: Path, approval_path: Path) -> dict[str, Any]:
    corrections_report = read_json(corrections_report_path)
    correction_rows = [
        row
        for row in corrections_report.get("rows") or []
        if isinstance(row, dict) and row.get("id") and row.get("fix_status") != "verified_fixed"
    ]
    csv_rows, csv_fieldnames, csv_errors = read_csv_rows(corrections_csv_path)
    csv_by_id = {str(row.get("id") or ""): row for row in csv_rows if row.get("id")}
    allowed = correction_validator.allowed_categories(root)
    existing = read_json(approval_path)
    existing_entries = approval_entries(existing)
    current_digests = {str(row.get("id")): row_digest(row) for row in correction_rows}
    existing_by_id: dict[str, dict[str, Any]] = {}
    duplicate_approval_ids: set[str] = set()
    stale_approvals: list[dict[str, Any]] = []
    for entry in existing_entries:
        entry_id = str(entry.get("id") or "")
        if not entry_id:
            stale_approvals.append({"id": entry_id, "reason": "missing_id"})
            continue
        if entry_id in existing_by_id:
            duplicate_approval_ids.add(entry_id)
        expected_digest = current_digests.get(entry_id)
        if not expected_digest:
            stale_approvals.append({"id": entry_id, "reason": "not_in_current_source_fix_rows"})
            continue
        if str(entry.get("row_digest") or "") != expected_digest:
            stale_approvals.append({"id": entry_id, "reason": "row_digest_mismatch"})
            continue
        existing_by_id[entry_id] = entry

    approvals: list[dict[str, Any]] = []
    for row in correction_rows:
        row_id = str(row.get("id"))
        digest = current_digests[row_id]
        existing_entry = existing_by_id.get(row_id, {})
        csv_row = csv_by_id.get(row_id, {})
        requested_category = approval_category(existing_entry, csv_row.get("operator_category_to_set_in_baselane", ""))
        category = baselane_category(requested_category)
        profile = evidence_profile(row)
        auto_approval_reason = deterministic_auto_approval_reason(row, category, allowed)
        auto_approved = bool(auto_approval_reason) and row_id not in duplicate_approval_ids
        approved = existing_entry.get("approved") is True or auto_approved
        if approved and (is_empty_category(category) or category not in allowed):
            validation_status = "invalid_approval"
            validation_reason = "approved=true requires category_to_set to be a valid Baselane category"
        elif auto_approved:
            validation_status = "ready_auto_approved"
            validation_reason = auto_approval_reason
        elif approved:
            validation_status = "ready_approved"
            validation_reason = "approved category can be copied into generated approved corrections CSV"
        else:
            validation_status = "pending_approval"
            validation_reason = "set approved=true with a valid category_to_set only after source evidence review"
        approvals.append(
            {
                "id": row_id,
                "row_digest": digest,
                "approved": approved,
                "auto_approved": auto_approved,
                "requested_category_to_set": requested_category,
                "category_to_set": category,
                "category_alias_applied": category != requested_category,
                "approval_note": auto_approval_reason or str(existing_entry.get("approval_note") or ""),
                "validation_status": validation_status,
                "validation_reason": validation_reason,
                **profile,
                "property": row.get("property"),
                "date": row.get("date"),
                "amount": row.get("amount"),
                "merchant": row.get("merchant"),
                "description": row.get("description"),
                "baselane_match_key": row.get("baselane_match_key"),
                "historical_evidence_status": row.get("historical_evidence_status"),
                "historical_suggested_category": row.get("historical_suggested_category"),
                "historical_category_counts": row.get("historical_category_counts"),
                "context_candidate_status": row.get("context_candidate_status"),
                "context_candidate_category": row.get("context_candidate_category"),
                "context_candidate_reason": row.get("context_candidate_reason"),
                "document_support_count": row.get("document_support_count"),
                "document_checked_file_count": row.get("document_checked_file_count"),
                "document_limit_reached": row.get("document_limit_reached"),
                "document_category_counts": row.get("document_category_counts"),
                "document_roots": row.get("document_roots"),
                "document_examples": row.get("document_examples"),
                "email_invoice_evidence_required": row.get("email_invoice_evidence_required"),
                "payment_rail": row.get("payment_rail"),
                "payee_tokens": row.get("payee_tokens"),
                "email_invoice_search_query": row.get("email_invoice_search_query"),
                "email_invoice_expected_window": row.get("email_invoice_expected_window"),
                "local_mail_invoice_status": row.get("local_mail_invoice_status"),
                "local_mail_invoice_match_count": row.get("local_mail_invoice_match_count"),
                "local_mail_invoice_checked_file_count": row.get("local_mail_invoice_checked_file_count"),
                "local_mail_invoice_matches": row.get("local_mail_invoice_matches"),
                "gws_mail_invoice_status": row.get("gws_mail_invoice_status"),
                "gws_mail_invoice_match_count": row.get("gws_mail_invoice_match_count"),
                "gws_mail_invoice_matches": row.get("gws_mail_invoice_matches"),
                "gws_mail_invoice_errors": row.get("gws_mail_invoice_errors"),
                "allowed_categories": sorted(allowed),
            }
        )
    invalid_count = sum(1 for entry in approvals if entry["validation_status"] == "invalid_approval")
    pending_count = sum(1 for entry in approvals if entry["validation_status"] == "pending_approval")
    approved_count = sum(1 for entry in approvals if entry["validation_status"] in {"ready_approved", "ready_auto_approved"})
    auto_approved_count = sum(1 for entry in approvals if entry["validation_status"] == "ready_auto_approved")
    recommendation_counts: dict[str, int] = {}
    for entry in approvals:
        recommendation = str(entry.get("autonomy_recommendation") or "unknown")
        recommendation_counts[recommendation] = recommendation_counts.get(recommendation, 0) + 1
    structural_issue_count = len(csv_errors) + len(duplicate_approval_ids) + len(stale_approvals)
    status = "ok" if pending_count == 0 and invalid_count == 0 and structural_issue_count == 0 else "review"
    if not correction_rows and corrections_report.get("status") == "ok":
        status = "ok"
    return {
        "generated_at": iso_z(),
        "status": status,
        "approval_scope": APPROVAL_SCOPE,
        "policy": "Approval artifact only; does not mutate Baselane, public docs, Lofty PM, Telegram, or email.",
        "instructions": "Rows with exact deterministic historical support are auto-approved; all weak, conflicting, or unsupported rows require approved=true and a valid category_to_set after source evidence review. Stale row digests are blocked.",
        "corrections_report": str(corrections_report_path),
        "corrections_csv": str(corrections_csv_path),
        "approval_file": str(approval_path),
        "allowed_category_count": len(allowed),
        "row_count": len(approvals),
        "approved_count": approved_count,
        "auto_approved_count": auto_approved_count,
        "pending_count": pending_count,
        "invalid_count": invalid_count,
        "autonomy_recommendation_counts": dict(sorted(recommendation_counts.items())),
        "structural_issue_count": structural_issue_count,
        "csv_errors": csv_errors,
        "duplicate_approval_ids": sorted(duplicate_approval_ids),
        "stale_approvals": stale_approvals,
        "approved_corrections_csv_status": "ready" if status == "ok" else ("partial_ready" if approved_count else "pending"),
        "next_action": (
            "Use the generated approved corrections CSV to update Baselane source rows, export again, then rerun weekly cron."
            if status == "ok"
            else (
                f"Use the {approved_count} validated approved correction(s) in reports/baselane_ecogl_source_fix_approved_corrections.csv; "
                f"{pending_count} row(s) remain blocked pending stronger source evidence."
                if approved_count
                else "Update this approval JSON; do not edit generated correction reports directly."
            )
        ),
        "approvals": approvals,
    }


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_approved_csv(path: Path, corrections_csv_path: Path, report: dict[str, Any]) -> None:
    rows, fieldnames, errors = read_csv_rows(corrections_csv_path)
    if errors:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return
    approved_by_id = {
        str(entry.get("id")): str(entry.get("category_to_set") or "").strip()
        for entry in report.get("approvals") or []
        if entry.get("validation_status") in {"ready_approved", "ready_auto_approved"}
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            row_id = str(row.get("id") or "")
            if row_id not in approved_by_id:
                continue
            row["operator_category_to_set_in_baselane"] = approved_by_id[row_id]
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ECO GL Source-Fix Approval",
        "",
        f"- Status: `{report['status']}`",
        f"- Rows: `{report['row_count']}`",
        f"- Approved: `{report['approved_count']}`",
        f"- Pending: `{report['pending_count']}`",
        f"- Invalid: `{report['invalid_count']}`",
        f"- Evidence classes: `{report.get('autonomy_recommendation_counts') or {}}`",
        f"- Structural issues: `{report['structural_issue_count']}`",
        f"- Policy: {report['policy']}",
        f"- Next action: {report['next_action']}",
        "",
        "## Pending / Invalid Rows",
        "",
    ]
    flagged = [
        entry
        for entry in report.get("approvals") or []
        if entry.get("validation_status") not in {"ready_approved", "ready_auto_approved"}
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in flagged:
        grouped.setdefault(str(entry.get("autonomy_recommendation") or "unknown"), []).append(entry)
    for recommendation, entries in sorted(grouped.items()):
        lines.extend(["", f"### {recommendation}", ""])
        for entry in entries:
            candidate = str(entry.get("candidate_category") or "")
            candidate_suffix = f" — candidate `{candidate}`" if candidate else ""
            lines.append(
                f"- `{entry.get('id')}` — {entry.get('property')} — {entry.get('date')} — {entry.get('amount')} — "
                f"{entry.get('merchant')} — `{entry.get('validation_status')}`{candidate_suffix}"
            )
            lines.append(f"  - Evidence needed: {entry.get('evidence_needed')}")
            if count(entry.get("document_checked_file_count")) and not count(entry.get("document_support_count")):
                limit = " (scan limit reached)" if str(entry.get("document_limit_reached")).lower() == "true" else ""
                lines.append(
                    f"  - Public-doc scan: checked `{entry.get('document_checked_file_count')}` file(s), "
                    f"matched category evidence `0`{limit}."
                )
            if str(entry.get("email_invoice_evidence_required") or "").lower() == "true":
                lines.append(f"  - Email invoice query: `{entry.get('email_invoice_search_query') or 'manual search required'}`")
                lines.append(
                    f"  - Local mail evidence: `{entry.get('local_mail_invoice_status') or 'unknown'}` "
                    f"({entry.get('local_mail_invoice_match_count') or 0} match)."
                )
                lines.append(
                    f"  - Gmail evidence: `{entry.get('gws_mail_invoice_status') or 'unknown'}` "
                    f"({entry.get('gws_mail_invoice_match_count') or 0} match)."
                )
    if not flagged:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and validate durable ECO GL source-fix category approvals.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--corrections-report", type=Path)
    parser.add_argument("--corrections-csv", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--approved-csv", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--validation-report", type=Path)
    parser.add_argument("--validation-csv", type=Path)
    parser.add_argument("--validation-markdown", type=Path)
    args = parser.parse_args()

    root = args.root
    corrections_report = args.corrections_report or root / "reports" / "baselane_ecogl_source_fix_corrections.json"
    corrections_csv = args.corrections_csv or root / "reports" / "baselane_ecogl_source_fix_corrections.csv"
    approval = args.approval or root / "reports" / "baselane_ecogl_source_fix_approval.json"
    approved_csv = args.approved_csv or root / "reports" / "baselane_ecogl_source_fix_approved_corrections.csv"
    markdown = args.markdown or root / "reports" / "baselane_ecogl_source_fix_approval.md"
    validation_report_path = args.validation_report or root / "reports" / "baselane_ecogl_source_fix_correction_validation.json"
    validation_csv_path = args.validation_csv or root / "reports" / "baselane_ecogl_source_fix_correction_validation.csv"
    validation_markdown_path = args.validation_markdown or root / "reports" / "baselane_ecogl_source_fix_correction_validation.md"
    report = build_report(root, corrections_report, corrections_csv, approval)
    write_json(approval, report)
    write_approved_csv(approved_csv, corrections_csv, report)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(render_markdown(report), encoding="utf-8")
    validation_report = correction_validator.build_report(root, approved_csv, approval)
    correction_validator.write_json(validation_report_path, validation_report)
    correction_validator.write_csv(validation_csv_path, validation_report["records"])
    validation_markdown_path.parent.mkdir(parents=True, exist_ok=True)
    validation_markdown_path.write_text(correction_validator.render_markdown(validation_report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "row_count": report["row_count"], "approved_count": report["approved_count"], "pending_count": report["pending_count"], "invalid_count": report["invalid_count"]}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
