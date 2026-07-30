#!/usr/bin/env python3
"""Remove the four erroneous June 2026 tax accruals for sold properties."""

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
REPORT = ROOT / "reports" / "baselane_sold_property_june_tax_accrual_removal.json"
APPLY_ENV = "BASELANE_SOLD_PROPERTY_TAX_ACCRUAL_REMOVE_APPLY"
APPLY_DIGEST_ENV = "BASELANE_SOLD_PROPERTY_TAX_ACCRUAL_REMOVE_DIGEST"
TARGETS = {
    "313980093": {
        "property": "1315 E 114th St, Cleveland, OH 44106",
        "property_id": "93244",
        "amount": -210.92,
    },
    "313980094": {
        "property": "13806 Coit Rd, Cleveland, OH 44110",
        "property_id": "83233",
        "amount": -196.63,
    },
    "313980142": {
        "property": "3024 W 103rd St, Cleveland, OH 44111",
        "property_id": "89647",
        "amount": -228.07,
    },
    "313980244": {
        "property": "9919 S Oglesby Ave, Chicago, IL 60617",
        "property_id": "80590",
        "amount": -104.48,
    },
}


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def query_live_rows() -> list[dict[str, Any]]:
    result = run_graphql({
        "operationName": "Transactions",
        "variables": {"input": {
            "sort": {"direction": "ASC", "field": "date"},
            "filter": {
                "search": "AOPS-OHIL-ACCRUAL|taxes",
                "isHidden": False,
                "isDeleted": False,
            },
            "page": 1,
            "pageLimit": 500,
        }},
        "query": """
        query Transactions($input: SortsAndFilters) {
          transactions(input: $input) {
            data { id amount date propertyId tagId merchantName note isManual isDeleted }
          }
        }
        """,
    })["data"]["transactions"]
    return result.get("data") or []


def verified_targets() -> list[dict[str, Any]]:
    rows_by_id = {str(row.get("id") or ""): row for row in query_live_rows()}
    verified: list[dict[str, Any]] = []
    for transaction_id, expected in TARGETS.items():
        row = rows_by_id.get(transaction_id)
        if row is None:
            raise RuntimeError(f"Missing live tax accrual target {transaction_id}")
        marker = f"AOPS-OHIL-ACCRUAL|taxes|{expected['property']}|2026-06"
        actual = {
            "date": str(row.get("date") or ""),
            "amount": round(float(row.get("amount") or 0), 2),
            "property_id": str(row.get("propertyId") or ""),
            "is_manual": bool(row.get("isManual")),
            "is_deleted": bool(row.get("isDeleted")),
        }
        wanted = {
            "date": "2026-06-28",
            "amount": expected["amount"],
            "property_id": expected["property_id"],
            "is_manual": True,
            "is_deleted": False,
        }
        if actual != wanted or marker not in note_text(row.get("note")):
            raise RuntimeError(
                f"Live identity mismatch for {transaction_id}: expected={wanted} actual={actual}"
            )
        verified.append({
            "id": transaction_id,
            "property": expected["property"],
            "date": actual["date"],
            "amount": actual["amount"],
            "marker": marker,
            "merchant_name": row.get("merchantName"),
            "note": note_text(row.get("note")),
        })
    return verified


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()

    verified = verified_targets()
    digest = hashlib.sha256(
        json.dumps(verified, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    applied: list[dict[str, Any]] = []
    status = "ready"
    if args.apply:
        if os.environ.get(APPLY_ENV) != "1" or os.environ.get(APPLY_DIGEST_ENV) != digest:
            status = "blocked"
        else:
            applied = run_graphql({
                "operationName": "UpdateTransaction",
                "variables": {"input": [
                    {"id": item["id"], "isDeleted": True, "isReviewedByUser": True}
                    for item in verified
                ]},
                "query": """
                mutation UpdateTransaction($input: [UpdateTransaction!]) {
                  updateTransactions(input: $input) { id isDeleted amount date propertyId note }
                }
                """,
            })["data"]["updateTransactions"]
            if {str(row.get("id") or "") for row in applied if row.get("isDeleted")} != set(TARGETS):
                raise RuntimeError(f"Deletion response mismatch: {applied}")
            status = "applied"

    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scope": "June 2026 only; no closed-month tax accruals were changed.",
        "reason": "User confirmed all four properties were already sold and owe no taxes.",
        "verified_targets": verified,
        "payload_digest": digest,
        "applied": applied,
        "approval_command": (
            f"{APPLY_ENV}=1 {APPLY_DIGEST_ENV}={digest} "
            f"python3 scripts/{Path(__file__).name} --apply"
        ),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": status,
        "target_count": len(verified),
        "payload_digest": digest,
        "report": str(args.report),
    }, indent=2))
    return 0 if status in {"ready", "applied"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
