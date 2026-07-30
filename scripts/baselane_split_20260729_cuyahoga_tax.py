#!/usr/bin/env python3
"""Idempotently split the posted 2026-07-29 Cuyahoga tax debit by property."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal

from baselane_settle_madison_pm_mortgage import (
    graphql,
    normalized_children,
    query_parent,
    reconcile_parent_split,
)

PARENT_ID = "321910372"
EXPECTED_AMOUNT = Decimal("-5063.89")
COMPONENTS = [
    ("1456 W 85th | 2026 H1 property tax", "-2399.80", "81428", "93"),
    ("1456 W 85th | tax payment service fee", "-1.40", "81428", "15"),
    ("1278 E 187th | 2026 H1 property tax", "-877.97", "93597", "93"),
    ("1278 E 187th | tax payment service fee", "-0.51", "93597", "15"),
    ("1518 Dille | 2026 H1 property tax", "-1783.17", "83240", "93"),
    ("1518 Dille | tax payment service fee", "-1.04", "83240", "15"),
]


def target(parent: dict) -> list[dict]:
    rows = [
        {
            "amount": Decimal(amount),
            "date": str(parent["date"]),
            "merchantName": label,
            "propertyId": property_id,
            "tagId": tag_id,
        }
        for label, amount, property_id, tag_id in COMPONENTS
    ]
    if sum((row["amount"] for row in rows), Decimal("0")) != EXPECTED_AMOUNT:
        raise RuntimeError("configured Cuyahoga split does not sum to parent")
    return rows


def update_parent() -> None:
    response = graphql(
        {
            "operationName": "UpdateTransaction",
            "variables": {
                "input": [{
                    "id": PARENT_ID,
                    "merchantName": "Cuyahoga County taxes | 1456 + 1278 + Dille | 2026 H1",
                    "note": (
                        "AOPS-CUYAHOGA-TAX-SPLIT|2026-07-29|5063.89 | "
                        "Native property/category split from official payment receipts."
                    ),
                }]
            },
            "query": """
            mutation UpdateTransaction($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) { id merchantName note }
            }
            """,
        }
    )
    if len(response["data"]["updateTransactions"]) != 1:
        raise RuntimeError("Cuyahoga parent metadata update failed")


def inspect() -> tuple[dict, list[dict], bool]:
    parent = query_parent(PARENT_ID)
    if (
        Decimal(str(parent.get("amount"))) != EXPECTED_AMOUNT
        or str(parent.get("date")) != "2026-07-29"
        or str(parent.get("bankAccountId")) != "38968"
        or parent.get("isDeleted")
        or parent.get("parentId")
    ):
        raise RuntimeError(f"Cuyahoga parent identity changed: {parent!r}")
    rows = target(parent)
    done = normalized_children(parent.get("splitTransactions") or []) == normalized_children(rows)
    return parent, rows, done


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()
    digest = hashlib.sha256(
        json.dumps(COMPONENTS, separators=(",", ":")).encode()
    ).hexdigest()
    parent, rows, done = inspect()
    plan = {
        "status": "already_applied" if done else "create_native_split",
        "digest": digest,
        "parent_id": PARENT_ID,
        "amount": str(EXPECTED_AMOUNT),
        "components": COMPONENTS,
    }
    if not args.apply:
        print(json.dumps(plan, indent=2))
        return 0
    if args.digest != digest:
        raise RuntimeError("apply requires exact dry-run digest")
    if not done:
        reconcile_parent_split(parent, rows)
    update_parent()
    _parent, verify_rows, verified = inspect()
    if not verified:
        raise RuntimeError("Cuyahoga native split verification failed")
    print(json.dumps({**plan, "status": "applied_and_verified"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
