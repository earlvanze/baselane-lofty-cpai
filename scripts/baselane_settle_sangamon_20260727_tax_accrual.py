#!/usr/bin/env python3
"""Clear Sangamon H1 tax accruals against its posted 2026-07-27 tax payment."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from baselane_settle_madison_pm_mortgage import graphql, query_parent


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "sangamon_20260727_tax_accrual_settlement.json"
PROPERTY_ID = "83181"
TAG_ID = "95"
PAYMENT_ID = "320996350"
PAYMENT_AMOUNT = Decimal("-3414.54")
SETTLEMENT_AMOUNT = Decimal("2276.34")
VARIANCE_EXPENSE = Decimal("1138.20")
MARKER = (
    "AOPS-TAX-SETTLEMENT|8143 S Sangamon St|2026-H1|"
    "2276.34|bill=3414.54|payment=320996350"
)
MERCHANT = "Tax Accrual Settlement | 8143 S Sangamon St | 2026 H1"
ACCRUALS = [
    ("284879958", "2026-01-28", Decimal("-379.39")),
    ("284879970", "2026-02-28", Decimal("-379.39")),
    ("284880444", "2026-03-28", Decimal("-379.39")),
    ("284880449", "2026-04-28", Decimal("-379.39")),
    ("284880455", "2026-05-28", Decimal("-379.39")),
    ("313980195", "2026-06-28", Decimal("-379.39")),
]


def cents(value: Any) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def find_existing() -> list[dict[str, Any]]:
    response = graphql(
        {
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "DESC", "field": "date"},
                    "filter": {
                        "search": MERCHANT,
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
                  id amount date merchantName propertyId tagId note
                  isManual hidden isDeleted
                }
              }
            }
            """,
        }
    )
    rows = ((response.get("data") or {}).get("transactions") or {}).get("data") or []
    return [row for row in rows if note_text(row.get("note")).startswith(MARKER)]


def validate_sources() -> dict[str, Any]:
    issues: list[str] = []
    payment = query_parent(PAYMENT_ID)
    expected_payment = {
        "amount": PAYMENT_AMOUNT,
        "date": "2026-07-27",
        "propertyId": PROPERTY_ID,
        "bankAccountId": "117744",
    }
    for field, expected in expected_payment.items():
        actual = cents(payment.get(field)) if field == "amount" else str(payment.get(field))
        if actual != expected:
            issues.append(f"payment {field} changed: expected={expected} actual={actual}")
    if payment.get("isDeleted"):
        issues.append("payment is deleted")

    accrual_rows = []
    for transaction_id, date, amount in ACCRUALS:
        row = query_parent(transaction_id)
        actual = {
            "id": str(row.get("id")),
            "date": str(row.get("date")),
            "amount": cents(row.get("amount")),
            "property_id": str(row.get("propertyId")),
            "tag_id": str(row.get("tagId")),
            "deleted": bool(row.get("isDeleted")),
        }
        if (
            actual["date"] != date
            or actual["amount"] != amount
            or actual["property_id"] != PROPERTY_ID
            or actual["tag_id"] != TAG_ID
            or actual["deleted"]
        ):
            issues.append(f"accrual source changed: {actual}")
        accrual_rows.append(
            {
                **actual,
                "amount": format(actual["amount"], ".2f"),
            }
        )
    if -sum((Decimal(row["amount"]) for row in accrual_rows), Decimal("0")) != SETTLEMENT_AMOUNT:
        issues.append("six accrual rows no longer total $2,276.34")
    return {
        "status": "verified" if not issues else "blocked",
        "issues": issues,
        "payment": {
            "id": PAYMENT_ID,
            "date": str(payment.get("date")),
            "amount": format(cents(payment.get("amount")), ".2f"),
            "property_id": str(payment.get("propertyId")),
            "bank_account_id": str(payment.get("bankAccountId")),
        },
        "accrual_rows": accrual_rows,
    }


def create() -> dict[str, Any]:
    note = (
        f"{MARKER} | Clears January-June 2026 Sangamon property-tax accruals "
        "against the DAO-paid Cook County bill posted 2026-07-27. The remaining "
        "$1,138.20 stays as current tax expense. Accounting-only settlement; "
        "no bank transfer."
    )
    response = graphql(
        {
            "operationName": "createTransaction",
            "variables": {
                "merchantName": MERCHANT,
                "note": note,
                "tagId": TAG_ID,
                "propertyId": PROPERTY_ID,
                "unitId": None,
                "entityId": None,
                "date": "2026-07-27",
                "bankAccountId": None,
                "amount": float(SETTLEMENT_AMOUNT),
                "isReviewedByUser": True,
            },
            "query": """
            mutation createTransaction(
              $merchantName: String!, $note: String!, $tagId: ID,
              $propertyId: ID, $unitId: ID, $entityId: Int, $date: String!,
              $bankAccountId: ID, $amount: Float!, $isReviewedByUser: Boolean
            ) {
              createTransaction(input: {
                merchantName: $merchantName note: $note tagId: $tagId
                propertyId: $propertyId unitId: $unitId entityId: $entityId
                date: $date bankAccountId: $bankAccountId amount: $amount
                isReviewedByUser: $isReviewedByUser
              }) {
                id amount date merchantName propertyId tagId note isManual
              }
            }
            """,
        }
    )
    return (response.get("data") or {}).get("createTransaction") or {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()

    evidence = validate_sources()
    existing = find_existing()
    if len(existing) > 1:
        raise SystemExit("duplicate Sangamon H1 tax settlement markers")
    public = {
        "scope": "Sangamon H1 2026 tax accrual settlement",
        "source_evidence": evidence,
        "settlement_amount": format(SETTLEMENT_AMOUNT, ".2f"),
        "posted_payment": format(-PAYMENT_AMOUNT, ".2f"),
        "variance_current_tax_expense": format(VARIANCE_EXPENSE, ".2f"),
        "cash_movement": "none",
        "status": "already_applied" if existing else "create",
        "existing_id": str(existing[0]["id"]) if existing else None,
    }
    digest = hashlib.sha256(
        json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = {"status": "dry_run", "digest": digest, **public}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if not args.apply:
        print(json.dumps(payload, indent=2))
        return 0 if evidence["status"] == "verified" else 2
    if args.digest != digest:
        raise SystemExit(f"exact live digest required: {digest}")
    if evidence["status"] != "verified":
        raise SystemExit(json.dumps(payload))

    created = create() if not existing else existing[0]
    if (
        cents(created.get("amount")) != SETTLEMENT_AMOUNT
        or str(created.get("propertyId")) != PROPERTY_ID
        or str(created.get("tagId")) != TAG_ID
    ):
        raise SystemExit(f"settlement verification failed: {created}")
    verify = find_existing()
    if len(verify) != 1:
        raise SystemExit(f"settlement reread failed: {verify}")
    applied = {
        **payload,
        "status": "applied_or_already_applied",
        "settlement_transaction_id": str(verify[0]["id"]),
    }
    REPORT.write_text(json.dumps(applied, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(applied, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
