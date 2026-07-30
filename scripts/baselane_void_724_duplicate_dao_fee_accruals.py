#!/usr/bin/env python3
"""Safely void two duplicate 724 ECO-side DAO fee accrual rows."""

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
REPORT = ROOT / "reports" / "baselane_724_duplicate_dao_fee_accrual_void.json"
APPLY_ENV = "BASELANE_724_DUPLICATE_DAO_FEE_VOID_APPLY"
APPLY_DIGEST_ENV = "BASELANE_724_DUPLICATE_DAO_FEE_VOID_DIGEST"
PROPERTY_ID = "37648"
TARGETS = {
    "320834264": {"date": "2025-07-28", "amount": 62.50, "month": "2025-07"},
    "320834271": {"date": "2025-08-28", "amount": 62.50, "month": "2025-08"},
}


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def query_rows() -> list[dict[str, Any]]:
    result = run_graphql({
        "operationName": "Transactions",
        "variables": {"input": {
            "sort": {"direction": "ASC", "field": "date"},
            "filter": {
                "search": "ECO Systems LLC DAO Registration Fee Revenue | 724 3rd Ave",
                "isHidden": False,
                "isDeleted": False,
            },
            "page": 1,
            "pageLimit": 1000,
        }},
        "query": """
        query Transactions($input: SortsAndFilters) {
          transactions(input: $input) {
            data { id amount date propertyId tagId merchantName note isManual }
          }
        }
        """,
    })["data"]["transactions"]
    return result.get("data") or []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()

    rows_by_id = {str(row["id"]): row for row in query_rows()}
    verified = []
    for transaction_id, expected in TARGETS.items():
        row = rows_by_id.get(transaction_id)
        if row is None:
            raise RuntimeError(f"Missing live duplicate target {transaction_id}")
        actual = (
            str(row.get("date") or ""),
            round(float(row.get("amount") or 0), 2),
            str(row.get("propertyId") or ""),
            str(row.get("tagId") or ""),
            bool(row.get("isManual")),
        )
        wanted = (expected["date"], expected["amount"], PROPERTY_ID, "2", True)
        if actual != wanted:
            raise RuntimeError(
                f"Live identity mismatch for {transaction_id}: expected={wanted} actual={actual}"
            )
        expected_marker = (
            f"AOPS-PNL-ACCRUAL|dao_eco|724 3rd Ave|{expected['month']}|62.50"
        )
        if expected_marker not in note_text(row.get("note")):
            raise RuntimeError(f"Unexpected marker for duplicate target {transaction_id}")
        verified.append({
            "id": transaction_id,
            **expected,
            "merchantName": row.get("merchantName"),
            "old_note": note_text(row.get("note")),
        })

    digest = hashlib.sha256(
        json.dumps(verified, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    applied = []
    status = "ready"
    if args.apply:
        if os.environ.get(APPLY_ENV) != "1" or os.environ.get(APPLY_DIGEST_ENV) != digest:
            status = "blocked"
        else:
            updates = [{
                "id": row["id"],
                "amount": 0,
                "note": (
                    f"ECO-VOID-DUPLICATE-DAO-FEE|724 3rd Ave|{row['month']}|{row['id']} | "
                    "Voided duplicate ECO-side DAO registration fee accrual created during "
                    f"live retry. Original amount {row['amount']:.2f}. No cash movement."
                ),
                "isReviewedByUser": True,
            } for row in verified]
            applied = run_graphql({
                "operationName": "UpdateTransaction",
                "variables": {"input": updates},
                "query": """
                mutation UpdateTransaction($input: [UpdateTransaction!]) {
                  updateTransactions(input: $input) { id amount date propertyId tagId note }
                }
                """,
            })["data"]["updateTransactions"]
            if {
                str(row["id"]) for row in applied
                if round(float(row.get("amount") or 0), 2) == 0
            } != set(TARGETS):
                raise RuntimeError(f"Void response mismatch: {applied}")
            status = "applied"

    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "property": "724 3rd Ave",
        "reason": "Remove two duplicate ECO-side accruals while preserving the valid 12-month schedule.",
        "verified_targets": verified,
        "payload_digest": digest,
        "applied": applied,
        "approval_command": (
            f"{APPLY_ENV}=1 {APPLY_DIGEST_ENV}={digest} "
            f"python3 scripts/{Path(__file__).name} --apply"
        ),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": status,
        "target_count": len(verified),
        "payload_digest": digest,
        "report": str(args.report),
    }, indent=2))
    return 2 if status == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
