#!/usr/bin/env python3
"""Reconcile the exact 2026-08-03 Yasbeth payment as an Umland ECO advance.

This is a dated, manual-only metadata repair. It does not move cash or create a
manual accounting row. The posted ECO-bank debit is the ID-bearing evidence of
the DAO payable to ECO.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from baselane_settle_madison_pm_mortgage import (
    cents,
    graphql,
    normalized_children,
    reconcile_parent_split,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "baselane_umland_yasbeth_20260803.json"

TRANSACTION_ID = "325006746"
PROPERTY_ID = "83184"
BANK_ACCOUNT_ID = "114283"
DATE = "2026-08-03"
AMOUNT = Decimal("-1220.00")
MERCHANT = "VENMO *Yasbeth Salinas"
PARENT_LABEL = "22164 Umland | Yasbeth | July 2026 cleaning and trash | ECO advance"
NOTE = (
    "ECO cash advance for 22164 Umland: July 2026 cleanings x4 $1,120.00 "
    "plus trash service $100.00; paid to Yasbeth Salinas via Venmo on 2026-08-03."
)


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(value.get("text") or "").split())
    return " ".join(str(value or "").split())


def desired_children() -> list[dict[str, Any]]:
    return [
        {
            "amount": Decimal("-1120.00"),
            "date": DATE,
            "merchantName": "22164 Umland | July 2026 cleanings x4 | ECO advance",
            "propertyId": PROPERTY_ID,
            "tagId": "52",
        },
        {
            "amount": Decimal("-100.00"),
            "date": DATE,
            "merchantName": "22164 Umland | July 2026 trash | ECO advance",
            "propertyId": PROPERTY_ID,
            "tagId": "102",
        },
    ]


def fetch() -> dict[str, Any]:
    response = graphql(
        {
            "operationName": "UmlandYasbethTransaction",
            "variables": {"id": TRANSACTION_ID},
            "query": """
            query UmlandYasbethTransaction($id: ID!) {
              transactionById(id: $id) {
                id amount date merchantName description bankAccountId propertyId
                tagId note pending parentId isDeleted isSplit isReviewedByUser
                splitTransactions {
                  id amount date merchantName propertyId tagId parentId isDeleted
                }
              }
            }
            """,
        }
    )
    row = (response.get("data") or {}).get("transactionById")
    if not isinstance(row, dict):
        raise RuntimeError(f"Baselane transaction {TRANSACTION_ID} was not found")
    return row


def source_issues(row: dict[str, Any]) -> list[str]:
    actual = {
        "id": str(row.get("id") or ""),
        "amount": cents(row.get("amount") or 0),
        "date": str(row.get("date") or "")[:10],
        "bank_account_id": str(row.get("bankAccountId") or ""),
        "property_id": str(row.get("propertyId") or ""),
        "parent_id": row.get("parentId"),
        "pending": bool(row.get("pending")),
        "is_deleted": bool(row.get("isDeleted")),
    }
    expected = {
        "id": TRANSACTION_ID,
        "amount": AMOUNT,
        "date": DATE,
        "bank_account_id": BANK_ACCOUNT_ID,
        "property_id": PROPERTY_ID,
        "parent_id": None,
        "pending": False,
        "is_deleted": False,
    }
    issues = [] if actual == expected else [f"immutable source mismatch: {actual!r}"]
    merchant = str(row.get("merchantName") or "")
    if merchant not in {MERCHANT, PARENT_LABEL}:
        issues.append(f"unexpected source/managed merchant label: {merchant!r}")
    active_children = [
        child for child in row.get("splitTransactions") or []
        if not child.get("isDeleted")
    ]
    if active_children and normalized_children(active_children) != normalized_children(desired_children()):
        issues.append("existing native split differs from the approved two-component allocation")
    if sum((child["amount"] for child in desired_children()), Decimal("0")) != AMOUNT:
        issues.append("desired split does not sum to the bank debit")
    return issues


def public_plan(row: dict[str, Any]) -> dict[str, Any]:
    children_match = normalized_children(row.get("splitTransactions") or []) == normalized_children(desired_children())
    parent_matches = (
        str(row.get("merchantName") or "") == PARENT_LABEL
        and str(row.get("propertyId") or "") == PROPERTY_ID
        and str(row.get("tagId") or "") == "52"
        and note_text(row.get("note")) == NOTE
        and bool(row.get("isReviewedByUser"))
    )
    return {
        "scope": "22164 Umland exact Yasbeth July-cleaning/trash ECO advance",
        "status": "dry_run",
        "issues": source_issues(row),
        "cash_movement_created": False,
        "manual_accounting_row_created": False,
        "source": {
            "transaction_id": TRANSACTION_ID,
            "date": DATE,
            "amount": format(AMOUNT, ".2f"),
            "bank_account_id": BANK_ACCOUNT_ID,
            "property_id": PROPERTY_ID,
            "merchant": MERCHANT,
        },
        "accounting_treatment": (
            "Posted ECO-bank property expense establishes Umland DAO A/P due to ECO; "
            "no separate cash or accrual row is created."
        ),
        "parent_update_required": not parent_matches,
        "native_split_update_required": not children_match,
        "components": [
            {
                "amount": format(child["amount"], ".2f"),
                "property_id": child["propertyId"],
                "tag_id": child["tagId"],
                "label": child["merchantName"],
            }
            for child in desired_children()
        ],
    }


def digest(plan: dict[str, Any]) -> str:
    material = {key: value for key, value in plan.items() if key != "status"}
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def update_parent() -> None:
    response = graphql(
        {
            "operationName": "UpdateUmlandYasbethAdvance",
            "variables": {
                "input": [
                    {
                        "id": TRANSACTION_ID,
                        "merchantName": PARENT_LABEL,
                        "propertyId": PROPERTY_ID,
                        "tagId": "52",
                        "note": NOTE,
                        "isReviewedByUser": True,
                    }
                ]
            },
            "query": """
            mutation UpdateUmlandYasbethAdvance($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) {
                id merchantName propertyId tagId note isReviewedByUser
              }
            }
            """,
        }
    )
    rows = (response.get("data") or {}).get("updateTransactions") or []
    if len(rows) != 1 or str(rows[0].get("id") or "") != TRANSACTION_ID:
        raise RuntimeError("Baselane did not confirm the Yasbeth parent update")


def write_report(payload: dict[str, Any]) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirmation-digest")
    args = parser.parse_args()

    before = fetch()
    plan = public_plan(before)
    confirmation_digest = digest(plan)
    if plan["issues"]:
        result = {**plan, "status": "blocked", "confirmation_digest": confirmation_digest}
        write_report(result)
        print(json.dumps(result, indent=2))
        return 2

    if not args.apply:
        result = {**plan, "confirmation_digest": confirmation_digest}
        write_report(result)
        print(json.dumps(result, indent=2))
        return 0

    if args.confirmation_digest != confirmation_digest:
        raise RuntimeError("confirmation digest does not match the fresh live preview")

    if plan["parent_update_required"]:
        update_parent()
    current = fetch()
    if normalized_children(current.get("splitTransactions") or []) != normalized_children(desired_children()):
        reconcile_parent_split(current, desired_children())

    after = fetch()
    verification = public_plan(after)
    remaining = list(verification["issues"])
    if verification["parent_update_required"]:
        remaining.append("parent metadata did not verify")
    if verification["native_split_update_required"]:
        remaining.append("native split did not verify")
    result = {
        **verification,
        "status": "verified" if not remaining else "verification_failed",
        "issues": remaining,
        "confirmation_digest": confirmation_digest,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "child_transaction_ids": [
            str(child.get("id") or "")
            for child in after.get("splitTransactions") or []
            if not child.get("isDeleted")
        ],
    }
    write_report(result)
    print(json.dumps(result, indent=2))
    return 0 if not remaining else 3


if __name__ == "__main__":
    raise SystemExit(main())
