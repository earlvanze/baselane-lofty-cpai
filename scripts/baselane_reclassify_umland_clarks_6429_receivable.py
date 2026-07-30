#!/usr/bin/env python3
"""Reclassify the unsupported Umland-to-Clarks transfer as a receivable."""

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
REPORT = ROOT / "reports" / "baselane_umland_clarks_6429_receivable.20260729.json"
APPLY_ENV = "BASELANE_UMLAND_CLARKS_RECEIVABLE_APPLY"
APPLY_DIGEST_ENV = "BASELANE_UMLAND_CLARKS_RECEIVABLE_DIGEST"
TRANSACTION_ID = "174466095"
PROPERTY_ID = "83184"
BANK_ACCOUNT_ID = "119345"
RECEIVABLE_TAG_ID = "34"
NOTE = (
    "Receivable from Clarks Corner Investments, LLC to Beagle LFTY0454 DAO "
    "LLC. No tax, insurance, HOA, or operating-expense reimbursement support "
    "was identified for this $6,429.07 transfer. Not an owner distribution. "
    "Reclassified 2026-07-29; existing cash movement preserved."
)


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def query_candidates() -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for search in ("Withdrawal from Lofty less June rents", "Receivable from Clarks Corner"):
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


def read_live_target() -> dict[str, Any]:
    row = next(
        (item for item in query_candidates() if str(item.get("id") or "") == TRANSACTION_ID),
        None,
    )
    if row is None:
        raise RuntimeError(f"Missing live transaction {TRANSACTION_ID}")
    actual = {
        "id": TRANSACTION_ID,
        "date": str(row.get("date") or ""),
        "amount": round(float(row.get("amount") or 0), 2),
        "merchant": str(row.get("merchantName") or ""),
        "property_id": str(row.get("propertyId") or ""),
        "tag_id": str(row.get("tagId") or ""),
        "bank_account_id": str(row.get("bankAccountId") or ""),
        "note": note_text(row.get("note")),
        "is_manual": bool(row.get("isManual")),
        "is_deleted": bool(row.get("isDeleted")),
    }
    if (
        actual["date"] != "2025-07-16"
        or actual["amount"] != -6429.07
        or actual["merchant"] != "Clarks Corner Investments, LLC  |  TRANSFER_OUT"
        or actual["property_id"] != PROPERTY_ID
        or actual["tag_id"] not in ("25", RECEIVABLE_TAG_ID)
        or actual["bank_account_id"] != BANK_ACCOUNT_ID
        or actual["is_manual"]
        or actual["is_deleted"]
    ):
        raise RuntimeError(f"Live identity mismatch: {actual}")
    return {
        **actual,
        "target_tag_id": RECEIVABLE_TAG_ID,
        "target_note": NOTE,
        "already_applied": actual["tag_id"] == RECEIVABLE_TAG_ID and actual["note"] == NOTE,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    before = read_live_target()
    digest_input = {
        "id": before["id"],
        "date": before["date"],
        "amount": before["amount"],
        "merchant": before["merchant"],
        "property_id": before["property_id"],
        "tag_id": before["tag_id"],
        "target_tag_id": before["target_tag_id"],
        "target_note": before["target_note"],
    }
    digest = hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    status = "ready"
    mutation_results: list[dict[str, Any]] = []
    after: dict[str, Any] | None = None
    if args.apply:
        if os.environ.get(APPLY_ENV) != "1" or os.environ.get(APPLY_DIGEST_ENV) != digest:
            status = "blocked"
        else:
            if not before["already_applied"]:
                response = run_graphql({
                    "operationName": "UpdateTransaction",
                    "variables": {
                        "input": [{
                            "id": TRANSACTION_ID,
                            "propertyId": PROPERTY_ID,
                            "tagId": RECEIVABLE_TAG_ID,
                            "note": NOTE,
                            "isReviewedByUser": True,
                        }]
                    },
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
            after = read_live_target()
            if not after["already_applied"]:
                raise RuntimeError(f"Post-apply verification failed: {after}")
            status = "applied"

    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "accounting_treatment": (
            "$6,429.07 receivable due from Clarks Corner to Beagle; not a "
            "distribution or supported reimbursement."
        ),
        "supported_clarks_reimbursements_preserved": {
            "property_tax": "1020.18",
            "insurance": "367.25",
            "hoa_opex": "300.00",
            "total": "1687.43",
        },
        "creates_cash": False,
        "creates_manual_rows": False,
        "before": before,
        "payload_digest": digest,
        "mutation_results": mutation_results,
        "after": after,
    }
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": status,
        "pending_count": 0 if before["already_applied"] else 1,
        "payload_digest": digest,
        "report": str(REPORT),
    }, indent=2))
    return 0 if status in {"ready", "applied"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
