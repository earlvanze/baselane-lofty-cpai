#!/usr/bin/env python3
"""Reclassify two Umland-to-Earl water transfers as an EARLDAO receivable."""

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
REPORT = ROOT / "reports" / "baselane_umland_7000_earldao_receivable.20260729.json"
APPLY_ENV = "BASELANE_UMLAND_7000_RECEIVABLE_APPLY"
APPLY_DIGEST_ENV = "BASELANE_UMLAND_7000_RECEIVABLE_DIGEST"
UMLAND_PROPERTY_ID = "83184"
UMLAND_BANK_ACCOUNT_ID = "119345"
OTHER_LOAN_PAYMENTS_TAG_ID = "34"
NOTE = (
    "EARLDAO direct obligation to Beagle LFTY0454 DAO LLC. This cash transfer "
    "was not payment of a 22164 Umland Circle obligation and is a $7,000 "
    "receivable from EARLDAO across the two July 2026 transfers. Reclassified "
    "2026-07-29; existing cash movement preserved."
)

TARGETS: dict[str, dict[str, Any]] = {
    "307135073": {
        "date": "2026-07-01",
        "amount": -5000.00,
        "merchant": "Earl Vanze Co  |  INTERNAL_TRANSFER",
    },
    "312497885": {
        "date": "2026-07-10",
        "amount": -2000.00,
        "merchant": "Earl Vanze Co  |  INTERNAL_TRANSFER",
    },
}


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def query_candidates() -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for search in ("Water Bill Payment Reimbursement", "EARLDAO direct obligation"):
        response = run_graphql({
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "ASC", "field": "date"},
                    "filter": {
                        "search": search,
                        "isHidden": False,
                        "isDeleted": False,
                    },
                    "page": 1,
                    "pageLimit": 100,
                }
            },
            "query": """
            query Transactions($input: SortsAndFilters) {
              transactions(input: $input) {
                data {
                  id amount date merchantName propertyId tagId bankAccountId
                  note isManual isDeleted
                }
              }
            }
            """,
        })
        for row in response["data"]["transactions"].get("data") or []:
            rows[str(row.get("id") or "")] = row
    return list(rows.values())


def read_live_targets() -> list[dict[str, Any]]:
    by_id = {str(row.get("id") or ""): row for row in query_candidates()}
    verified: list[dict[str, Any]] = []
    for transaction_id, expected in TARGETS.items():
        row = by_id.get(transaction_id)
        if row is None:
            raise RuntimeError(f"Missing live transaction {transaction_id}")
        actual = {
            "date": str(row.get("date") or ""),
            "amount": round(float(row.get("amount") or 0), 2),
            "merchant": str(row.get("merchantName") or ""),
            "property_id": (
                str(row.get("propertyId")) if row.get("propertyId") is not None else None
            ),
            "tag_id": str(row.get("tagId") or ""),
            "bank_account_id": str(row.get("bankAccountId") or ""),
            "note": note_text(row.get("note")),
            "is_manual": bool(row.get("isManual")),
            "is_deleted": bool(row.get("isDeleted")),
        }
        if (
            actual["date"] != expected["date"]
            or actual["amount"] != expected["amount"]
            or actual["merchant"] != expected["merchant"]
            or actual["property_id"] not in (UMLAND_PROPERTY_ID, None)
            or actual["tag_id"] != OTHER_LOAN_PAYMENTS_TAG_ID
            or actual["bank_account_id"] != UMLAND_BANK_ACCOUNT_ID
            or actual["is_manual"]
            or actual["is_deleted"]
        ):
            raise RuntimeError(f"Live identity mismatch for {transaction_id}: {actual}")
        verified.append({
            "id": transaction_id,
            **actual,
            "target_property_id": None,
            "target_note": NOTE,
            "already_applied": actual["property_id"] is None and actual["note"] == NOTE,
        })
    return verified


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()

    before = read_live_targets()
    digest_input = [
        {
            "id": item["id"],
            "date": item["date"],
            "amount": item["amount"],
            "merchant": item["merchant"],
            "property_id": item["property_id"],
            "target_property_id": item["target_property_id"],
            "target_note": item["target_note"],
        }
        for item in before
    ]
    digest = hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    status = "ready"
    mutation_results: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    if args.apply:
        if os.environ.get(APPLY_ENV) != "1" or os.environ.get(APPLY_DIGEST_ENV) != digest:
            status = "blocked"
        else:
            pending = [
                {
                    "id": item["id"],
                    "propertyId": None,
                    "note": NOTE,
                    "isReviewedByUser": True,
                }
                for item in before
                if not item["already_applied"]
            ]
            if pending:
                response = run_graphql({
                    "operationName": "UpdateTransaction",
                    "variables": {"input": pending},
                    "query": """
                    mutation UpdateTransaction($input: [UpdateTransaction!]) {
                      updateTransactions(input: $input) {
                        id amount date merchantName propertyId tagId bankAccountId
                        note isManual isDeleted
                      }
                    }
                    """,
                })
                mutation_results = response["data"]["updateTransactions"]
            after = read_live_targets()
            wrong = [item for item in after if not item["already_applied"]]
            if wrong:
                raise RuntimeError(f"Post-apply verification failed: {wrong}")
            status = "applied"

    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scope": "Two existing Beagle bank-feed rows; property assignment and note only.",
        "accounting_treatment": (
            "$7,000 EARLDAO obligation to Beagle; excluded from Umland property GL."
        ),
        "historical_opening_cash": "0.00",
        "creates_cash": False,
        "creates_manual_rows": False,
        "before": before,
        "payload_digest": digest,
        "mutation_results": mutation_results,
        "after": after,
        "approval_command": (
            f"{APPLY_ENV}=1 {APPLY_DIGEST_ENV}={digest} "
            f"python3 scripts/{Path(__file__).name} --apply"
        ),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "status": status,
        "target_count": len(before),
        "pending_count": sum(not item["already_applied"] for item in before),
        "payload_digest": digest,
        "report": str(args.report),
    }, indent=2))
    return 0 if status in {"ready", "applied"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
