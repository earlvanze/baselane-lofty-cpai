#!/usr/bin/env python3
"""Restore evidenced sold-property custody transactions to their correct GL scope.

This is intentionally narrow.  It fixes three existing bank-feed rows whose
property assignment prevents the sold-property ledgers from reflecting the
cash that actually moved.  It never creates cash or manual transactions.
"""

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
REPORT = ROOT / "reports" / "baselane_sold_property_custody_retags.20260729.json"
APPLY_ENV = "BASELANE_SOLD_CUSTODY_RETAG_APPLY"
APPLY_DIGEST_ENV = "BASELANE_SOLD_CUSTODY_RETAG_DIGEST"

TARGETS: dict[str, dict[str, Any]] = {
    "268401252": {
        "date": "2026-04-14",
        "amount": -75790.61,
        "merchant": (
            "Wire to Lofty Holding 26931 Shoreview Avenue DAO LLC | "
            "26931 Shoreview Ave Net Sale Proceeds"
        ),
        "from_property_id": "37648",
        "to_property_id": "83242",
        "reason": "Actual Shoreview net-sale-proceeds remittance to Lofty.",
    },
    "241104602": {
        "date": "2026-02-11",
        "amount": 10708.64,
        "merchant": "Lofty Holding 614 E 97th Street DAO LLC  |  TRANSFER_O",
        "from_property_id": "37648",
        "to_property_id": "91166",
        "reason": "Actual reimbursement from 97th net sale proceeds to ECO.",
    },
    "305988656": {
        "date": "2026-06-29",
        "amount": -80928.95,
        "merchant": (
            "Returned due to: RTN IMAD 20260518MMQFMP2K023292 PER | "
            "Return incoming wire"
        ),
        "from_property_id": "87267",
        "to_property_id": None,
        "reason": (
            "Return of the unassigned duplicate 146th wire; matching incoming "
            "wire 284902593 is also unassigned."
        ),
    },
}


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def query_candidates(search: str) -> list[dict[str, Any]]:
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
    return response["data"]["transactions"].get("data") or []


def read_live_targets() -> list[dict[str, Any]]:
    live_by_id: dict[str, dict[str, Any]] = {}
    for target in TARGETS.values():
        for row in query_candidates(target["merchant"]):
            live_by_id[str(row.get("id") or "")] = row

    verified: list[dict[str, Any]] = []
    for transaction_id, expected in TARGETS.items():
        row = live_by_id.get(transaction_id)
        if row is None:
            raise RuntimeError(f"Missing live transaction {transaction_id}")
        actual = {
            "date": str(row.get("date") or ""),
            "amount": round(float(row.get("amount") or 0), 2),
            "merchant": str(row.get("merchantName") or ""),
            "property_id": (
                str(row.get("propertyId")) if row.get("propertyId") is not None else None
            ),
            "is_deleted": bool(row.get("isDeleted")),
        }
        wanted_identity = {
            "date": expected["date"],
            "amount": expected["amount"],
            "merchant": expected["merchant"],
            "property_id": expected["from_property_id"],
            "is_deleted": False,
        }
        already_applied_identity = dict(
            wanted_identity, property_id=expected["to_property_id"]
        )
        if actual not in (wanted_identity, already_applied_identity):
            raise RuntimeError(
                f"Live identity mismatch for {transaction_id}: "
                f"expected={wanted_identity} or {already_applied_identity}; actual={actual}"
            )
        verified.append({
            "id": transaction_id,
            **actual,
            "target_property_id": expected["to_property_id"],
            "reason": expected["reason"],
            "tag_id": str(row.get("tagId") or ""),
            "bank_account_id": str(row.get("bankAccountId") or ""),
            "note": note_text(row.get("note")),
            "already_applied": actual["property_id"] == expected["to_property_id"],
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
            "reason": item["reason"],
        }
        for item in before
    ]
    digest = hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    status = "ready"
    mutation_results: list[dict[str, Any]] = []
    if args.apply:
        if os.environ.get(APPLY_ENV) != "1" or os.environ.get(APPLY_DIGEST_ENV) != digest:
            status = "blocked"
        else:
            pending = [
                {
                    "id": item["id"],
                    "propertyId": item["target_property_id"],
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
            wrong = [
                item for item in after
                if item["property_id"] != item["target_property_id"]
            ]
            if wrong:
                raise RuntimeError(f"Post-apply verification failed: {wrong}")
            status = "applied"
    else:
        after = []

    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scope": "Three existing bank-feed rows; property/tag assignment only.",
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
