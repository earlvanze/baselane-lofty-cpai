#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql


ROOT = Path(__file__).absolute().parents[1]
REPORT = ROOT / "reports" / "baselane_88_duplicate_pm_accrual_void.json"
APPLY_ENV = "BASELANE_88_DUPLICATE_PM_VOID_APPLY"
APPLY_DIGEST_ENV = "BASELANE_88_DUPLICATE_PM_VOID_DIGEST"
PROPERTY_ID = "31499"
TARGETS = {
    "254362127": {"date": "2025-08-01", "amount": -3455.31, "label": "July 2025 legacy PM duplicate"},
    "254362207": {"date": "2025-10-01", "amount": -1338.63, "label": "September 2025 legacy PM conflict"},
}


def query_rows() -> list[dict[str, Any]]:
    query = """
        query Transactions($input: SortsAndFilters) {
          transactions(input: $input) { total data { id amount date propertyId merchantName description note isManual } }
        }
        """
    rows = []
    page = 1
    while True:
        result = run_graphql({
            "operationName": "Transactions",
            "variables": {"input": {
                "sort": {"direction": "DESC", "field": "date"},
                "filter": {"propertyId": PROPERTY_ID, "isHidden": False, "isDeleted": False},
                "page": page, "pageLimit": 1000,
            }},
            "query": query,
        })["data"]["transactions"]
        batch = result.get("data") or []
        rows.extend(batch)
        if not batch or len(rows) >= int(result.get("total") or 0):
            return rows
        page += 1


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Void two legacy 88 Madison PM accruals superseded by schedule-backed AOPS rows.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()

    rows_by_id = {str(row.get("id") or ""): row for row in query_rows()}
    verified = []
    for transaction_id, expected in TARGETS.items():
        row = rows_by_id.get(transaction_id)
        if not row:
            raise RuntimeError(f"missing live target {transaction_id}")
        actual = (str(row.get("date") or ""), round(float(row.get("amount") or 0), 2), str(row.get("propertyId") or ""))
        wanted = (expected["date"], expected["amount"], PROPERTY_ID)
        if actual != wanted:
            raise RuntimeError(f"live identity mismatch for {transaction_id}: expected={wanted} actual={actual}")
        verified.append({"id": transaction_id, **expected, "merchant": row.get("merchantName"), "old_note": note_text(row.get("note"))})
    digest = hashlib.sha256(json.dumps(verified, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    applied = []
    status = "ready"
    if args.apply:
        if os.environ.get(APPLY_ENV) != "1" or os.environ.get(APPLY_DIGEST_ENV) != digest:
            status = "blocked"
        else:
            updates = []
            for row in verified:
                marker = f"ECO-VOID-DUPLICATE-PM|88 Madison Ave|{row['id']}"
                updates.append({
                    "id": row["id"], "amount": 0,
                    "note": f"{marker} | Voided legacy accountless PM accrual superseded by schedule-backed AOPS PM accrual. Original amount {row['amount']:.2f}. No cash movement.",
                    "isReviewedByUser": True,
                })
            applied = run_graphql({
                "operationName": "UpdateTransaction", "variables": {"input": updates},
                "query": "mutation UpdateTransaction($input: [UpdateTransaction!]) { updateTransactions(input: $input) { id amount date propertyId note } }",
            })["data"]["updateTransactions"]
            if {str(row.get("id")) for row in applied if float(row.get("amount") or 0) == 0} != set(TARGETS):
                raise RuntimeError(f"void response mismatch: {applied}")
            status = "applied"
    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "property": "88 Madison Ave", "property_id": PROPERTY_ID,
        "reason": "Legacy accountless PM rows duplicated or conflicted with the schedule-backed AOPS PM accrual ledger; voiding does not represent cash movement.",
        "verified_targets": verified, "payload_digest": digest, "applied": applied,
        "column_e_correction": "4793.94",
        "approval_command": f"{APPLY_ENV}=1 {APPLY_DIGEST_ENV}={digest} python3 scripts/{Path(__file__).name} --apply",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "payload_digest": digest, "target_count": len(verified), "column_e_correction": "4793.94"}, indent=2))
    return 2 if status == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
