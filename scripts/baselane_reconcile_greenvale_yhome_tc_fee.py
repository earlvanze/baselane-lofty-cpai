#!/usr/bin/env python3
"""Reclassify the settled Greenvale/Yhome transaction-coordination fee.

This workflow changes bookkeeping metadata only. It never creates a transaction
or moves cash. The exact source rows and amounts are fixed so the workflow fails
closed if Baselane no longer matches the reviewed evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from baselane_apply_monthly_accruals_live import query_transactions, run_graphql


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "reports" / "greenvale_yhome_tc_fee_reconciliation.20260729.json"
ECO_PROPERTY_ID = "37648"

TARGETS: dict[str, dict[str, Any]] = {
    "250628621": {
        "amount": -3072.16,
        "date": "2026-01-23",
        "merchant_contains": "Greenvale TC Fee",
        "propertyId": ECO_PROPERTY_ID,
        "tagId": "79",
        "note": (
            "Greenvale transaction-coordination fee paid by ECO to Earl Vanze Co; "
            "advanced pending Yhome recovery and recovered 2026-02-26. ECO-only "
            "consulting expense; no DAO cash movement. Original note: Transfer to Gemini."
        ),
    },
    "250628280": {
        "amount": 3072.16,
        "date": "2026-02-26",
        "merchant_contains": "Reimbursement from Yhome for Greenvale TC Fee",
        "propertyId": ECO_PROPERTY_ID,
        "tagId": "2",
        "note": (
            "Yhome-paid Greenvale transaction-coordination fee. Governance authorized "
            "payment to ECO only from recovered Yhome funds; settles ECO's 2026-01-23 "
            "advance and creates no DAO cash movement. Original split: Greenvale TC Fee "
            "$3072.16 + Bannbury Taxes $4885.62 + 27 Pillar Taxes $5289.53."
        ),
    },
}


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def digest() -> str:
    payload = json.dumps(TARGETS, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def live_rows() -> dict[str, dict[str, Any]]:
    rows = query_transactions("Greenvale TC Fee", 100)
    return {str(row.get("id")): row for row in rows if str(row.get("id")) in TARGETS}


def source_matches(transaction_id: str, row: dict[str, Any]) -> bool:
    target = TARGETS[transaction_id]
    return (
        round(float(row.get("amount") or 0), 2) == target["amount"]
        and str(row.get("date") or "") == target["date"]
        and target["merchant_contains"].lower() in str(row.get("merchantName") or "").lower()
    )


def target_matches(transaction_id: str, row: dict[str, Any]) -> bool:
    target = TARGETS[transaction_id]
    return (
        source_matches(transaction_id, row)
        and str(row.get("propertyId") or "") == target["propertyId"]
        and str(row.get("tagId") or "") == target["tagId"]
        and note_text(row.get("note")) == target["note"]
    )


def update(row: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    values = {
        "id": str(row["id"]),
        "amount": target["amount"],
        "merchantName": str(row.get("merchantName") or ""),
        "note": target["note"],
        "tagId": target["tagId"],
        "propertyId": target["propertyId"],
        "unitId": None,
    }
    payload = {
        "operationName": "UpdateTransaction",
        "variables": {"input": [values]},
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id amount date merchantName propertyId tagId note isManual
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["updateTransactions"][0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--confirm-digest")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    expected_digest = digest()
    if args.apply and args.confirm_digest != expected_digest:
        raise SystemExit(f"--confirm-digest must equal {expected_digest}")

    before = live_rows()
    missing = sorted(set(TARGETS) - set(before))
    source_errors = sorted(
        transaction_id
        for transaction_id, row in before.items()
        if not source_matches(transaction_id, row)
    )
    if missing or source_errors:
        raise RuntimeError(f"live source guard failed: missing={missing}, changed={source_errors}")

    actions: list[dict[str, Any]] = []
    for transaction_id, target in TARGETS.items():
        row = before[transaction_id]
        actions.append(
            {
                "id": transaction_id,
                "action": "noop" if target_matches(transaction_id, row) else "update_metadata",
                "before": {
                    "amount": row.get("amount"),
                    "date": row.get("date"),
                    "propertyId": row.get("propertyId"),
                    "tagId": row.get("tagId"),
                    "note": note_text(row.get("note")),
                },
                "target": target,
            }
        )

    applied: list[dict[str, Any]] = []
    if args.apply:
        for action in actions:
            if action["action"] == "update_metadata":
                applied.append(update(before[action["id"]], TARGETS[action["id"]]))

    after = live_rows() if args.apply or args.verify else before
    verified = all(target_matches(transaction_id, after[transaction_id]) for transaction_id in TARGETS)
    report = {
        "status": "verified" if verified else ("preview" if not args.apply else "verification_failed"),
        "target_digest": expected_digest,
        "cash_movement_created": False,
        "economic_result": {
            "eco_advance": 3072.16,
            "yhome_recovery": 3072.16,
            "open_cash_balance": 0.0,
            "dao_cash_impact": 0.0,
        },
        "actions": actions,
        "applied": applied,
        "verified": verified,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if (not args.apply or verified) else 1


if __name__ == "__main__":
    raise SystemExit(main())
