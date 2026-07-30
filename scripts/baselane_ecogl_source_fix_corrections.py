#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FIELDS = [
    "id",
    "fix_status",
    "property",
    "date",
    "amount",
    "merchant",
    "description",
    "current_label",
    "verified_category",
    "historical_evidence_status",
    "historical_suggested_category",
    "historical_support_count",
    "historical_conflict_count",
    "historical_category_counts",
    "context_candidate_status",
    "context_candidate_category",
    "context_candidate_reason",
    "document_support_count",
    "document_checked_file_count",
    "document_limit_reached",
    "document_category_counts",
    "document_roots",
    "document_examples",
    "email_invoice_evidence_required",
    "payment_rail",
    "payee_tokens",
    "email_invoice_search_query",
    "email_invoice_expected_window",
    "local_mail_invoice_status",
    "local_mail_invoice_match_count",
    "local_mail_invoice_checked_file_count",
    "local_mail_invoice_matches",
    "gws_mail_invoice_status",
    "gws_mail_invoice_match_count",
    "gws_mail_invoice_matches",
    "gws_mail_invoice_errors",
    "evidence_status",
    "operator_category_to_set_in_baselane",
    "operator_note",
    "baselane_match_key",
    "next_action",
]


def evidence_status(historical_status: object, context_status: object) -> str:
    historical_text = str(historical_status or "")
    context_text = str(context_status or "")
    if context_text.startswith("automation_safe_") or context_text in {
        "context_only_exact_amount_notes",
        "context_only_notes",
        "context_only_same_merchant",
        "conflicting_context",
    }:
        return context_text
    return historical_text or context_text or "unknown"


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


def verifier_by_id(verifier: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("id")): row
        for row in verifier.get("results") or []
        if isinstance(row, dict) and row.get("id")
    }


def build_report(evidence_path: Path, verifier_path: Path) -> dict[str, Any]:
    evidence = read_json(evidence_path)
    verifier = read_json(verifier_path)
    verifier_rows = verifier_by_id(verifier)
    rows: list[dict[str, Any]] = []
    for row in evidence.get("rows") or []:
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("id") or "")
        verifier_row = verifier_rows.get(row_id, {})
        history = row.get("historical_category_evidence") if isinstance(row.get("historical_category_evidence"), dict) else {}
        context_candidate = row.get("context_candidate") if isinstance(row.get("context_candidate"), dict) else {}
        document_evidence = row.get("document_category_evidence") if isinstance(row.get("document_category_evidence"), dict) else {}
        email_evidence = row.get("email_invoice_evidence") if isinstance(row.get("email_invoice_evidence"), dict) else {}
        local_mail_evidence = row.get("local_mail_invoice_evidence") if isinstance(row.get("local_mail_invoice_evidence"), dict) else {}
        gws_mail_evidence = row.get("gws_mail_invoice_evidence") if isinstance(row.get("gws_mail_invoice_evidence"), dict) else {}
        receipt_evidence = row.get("email_receipt_category_evidence") if isinstance(row.get("email_receipt_category_evidence"), dict) else {}
        verified_category = str(verifier_row.get("verified_category") or "")
        fix_status = str(verifier_row.get("status") or "not_verified")
        suggested_category = ""
        if verified_category:
            suggested_category = verified_category
        elif history.get("automation_safe") is True:
            suggested_category = str(history.get("suggested_category") or "")
        elif str(context_candidate.get("status") or "").startswith("automation_safe_"):
            suggested_category = str(context_candidate.get("category") or "")
        elif receipt_evidence.get("status") == "automation_safe_email_receipt":
            suggested_category = str(receipt_evidence.get("category") or "")
        rows.append(
            {
                "id": row_id,
                "fix_status": fix_status,
                "property": row.get("property"),
                "date": row.get("date"),
                "amount": row.get("amount"),
                "merchant": row.get("merchant"),
                "description": row.get("description"),
                "current_label": row.get("current_label"),
                "verified_category": verified_category,
                "historical_evidence_status": history.get("status"),
                "historical_suggested_category": history.get("suggested_category"),
                "historical_support_count": history.get("support_count"),
                "historical_conflict_count": history.get("conflict_count"),
                "historical_category_counts": json.dumps(history.get("category_counts") or {}, sort_keys=True),
                "context_candidate_status": context_candidate.get("status"),
                "context_candidate_category": context_candidate.get("category"),
                "context_candidate_reason": context_candidate.get("reason"),
                "document_support_count": document_evidence.get("support_count"),
                "document_checked_file_count": document_evidence.get("checked_file_count"),
                "document_limit_reached": document_evidence.get("limit_reached"),
                "document_category_counts": json.dumps(document_evidence.get("category_counts") or {}, sort_keys=True),
                "document_roots": json.dumps(document_evidence.get("property_document_roots") or []),
                "document_examples": json.dumps(document_evidence.get("examples") or []),
                "email_invoice_evidence_required": email_evidence.get("required") is True,
                "payment_rail": email_evidence.get("payment_rail") or "",
                "payee_tokens": json.dumps(email_evidence.get("payee_tokens") or []),
                "email_invoice_search_query": email_evidence.get("search_query") or "",
                "email_invoice_expected_window": json.dumps(email_evidence.get("expected_window") or {}, sort_keys=True),
                "local_mail_invoice_status": local_mail_evidence.get("status") or "",
                "local_mail_invoice_match_count": local_mail_evidence.get("match_count"),
                "local_mail_invoice_checked_file_count": local_mail_evidence.get("checked_file_count"),
                "local_mail_invoice_matches": json.dumps(local_mail_evidence.get("matches") or []),
                "gws_mail_invoice_status": gws_mail_evidence.get("status") or "",
                "gws_mail_invoice_match_count": gws_mail_evidence.get("match_count"),
                "gws_mail_invoice_matches": json.dumps(gws_mail_evidence.get("matches") or []),
                "gws_mail_invoice_errors": json.dumps(gws_mail_evidence.get("errors") or []),
                "email_receipt_category_evidence_status": receipt_evidence.get("status") or "",
                "email_receipt_category_evidence_category": receipt_evidence.get("category") or "",
                "email_receipt_category_evidence_reason": receipt_evidence.get("reason") or "",
                "email_receipt_category_evidence": json.dumps(receipt_evidence),
                "evidence_status": evidence_status(history.get("status"), context_candidate.get("status")),
                "operator_category_to_set_in_baselane": suggested_category,
                "operator_note": "",
                "baselane_match_key": "|".join(
                    str(value or "")
                    for value in (row.get("property"), row.get("date"), row.get("amount"), row.get("merchant"))
                ),
                "next_action": (
                    "Already categorized in latest Baselane export; rerun weekly cron."
                    if fix_status == "verified_fixed"
                    else "Set exact category in Baselane source row, export again, then rerun weekly cron."
                ),
            }
        )
    input_statuses = {
        "evidence": evidence.get("status"),
        "verifier": verifier.get("status"),
    }
    input_error = any(status in {"missing", "unreadable"} for status in input_statuses.values())
    remaining_count = sum(1 for row in rows if row.get("fix_status") != "verified_fixed")
    return {
        "generated_at": iso_z(),
        "status": "ok" if remaining_count == 0 and not input_error else "review",
        "row_count": len(rows),
        "remaining_count": remaining_count,
        "input_statuses": input_statuses,
        "evidence": str(evidence_path),
        "verifier": str(verifier_path),
        "policy": "Worksheet only; does not mutate Baselane, public docs, Lofty PM, Telegram, or email.",
        "next_action": (
            "Rerun weekly cron; all source-fix rows verify as categorized."
            if remaining_count == 0
            else "Use this worksheet to apply exact Baselane source categories for remaining rows, then rerun weekly cron."
        ),
        "rows": rows,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in FIELDS})


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ECO GL Source-Fix Corrections",
        "",
        f"- Status: `{report['status']}`",
        f"- Rows: `{report['row_count']}`",
        f"- Remaining: `{report['remaining_count']}`",
        f"- Policy: {report['policy']}",
        f"- Next action: {report['next_action']}",
        "",
        "## Remaining Rows",
        "",
    ]
    remaining = [row for row in report.get("rows") or [] if row.get("fix_status") != "verified_fixed"]
    for row in remaining:
        lines.append(
            f"- `{row.get('id')}` — {row.get('property')} — {row.get('date')} — {row.get('amount')} — "
            f"{row.get('merchant')} — evidence `{row.get('evidence_status')}`"
        )
    if not remaining:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a no-mutation Baselane source-fix correction worksheet.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--verifier", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()

    root = args.root
    evidence_path = args.evidence or root / "reports" / "baselane_ecogl_source_fix_evidence.json"
    verifier_path = args.verifier or root / "reports" / "baselane_ecogl_source_fix_verifier.json"
    report_path = args.report or root / "reports" / "baselane_ecogl_source_fix_corrections.json"
    csv_path = args.csv or root / "reports" / "baselane_ecogl_source_fix_corrections.csv"
    markdown_path = args.markdown or root / "reports" / "baselane_ecogl_source_fix_corrections.md"
    report = build_report(evidence_path, verifier_path)
    write_json(report_path, report)
    write_csv(csv_path, report["rows"])
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "row_count": report["row_count"], "remaining_count": report["remaining_count"]}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
