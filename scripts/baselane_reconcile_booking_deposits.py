#!/usr/bin/env python3
"""Idempotently property-tag and split verified Booking.com bank deposits.

The source evidence is the Booking.com weekly payout statement and matching
Hospitable reservation financials. All rows remain Short Term Rents so the
native split improves property attribution without changing P&L classification.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_mcp.transfers import run_graphql_via_cdp  # noqa: E402


BRIDGE = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
REPORT = ROOT / "reports" / "baselane_booking_deposit_reconciliation.20260729.json"
CENT = Decimal("0.01")
TAG_SHORT_TERM_RENTS = "37"

PROPERTY = {
    "84 Madison": "60548",
    "86 Madison": "63162",
    "22164 Umland": "83184",
}

SPECS: list[dict[str, Any]] = [
    {
        "id": "321438252",
        "reference": "ST-E4L7X9U",
        "date": "2026-07-28",
        "amount": "835.00",
        "property": "86 Madison",
        "note": (
            "Booking.com payout ST-E4L7X9U | reservation 6149146610 | "
            "86.2 Madison | stay 2026-07-27 to 2026-07-31 | gross $835.00. "
            "Booking commission remains a separate expense."
        ),
    },
    {
        "id": "320842176",
        "reference": "ST-X3Q5W4M",
        "date": "2026-07-27",
        "amount": "757.00",
        "property": "84 Madison",
        "note": (
            "Booking.com payout ST-X3Q5W4M | reservation 6014430659 | "
            "84.2 Madison | stay 2026-07-25 to 2026-07-29 | gross $757.00. "
            "Booking commission remains a separate expense."
        ),
    },
    {
        "id": "317155456",
        "reference": "ST-F6X1F0V",
        "date": "2026-07-20",
        "amount": "3488.72",
        "note": (
            "Booking.com weekly payout ST-F6X1F0V5J8Q1 dated 2026-07-20. "
            "Native split follows the attached Booking.com payout statement; "
            "all components remain Short Term Rents."
        ),
        "components": [
            {
                "amount": "2478.72",
                "merchantName": "Booking.com | 6650259757 | Umland | reservation",
                "property": "22164 Umland",
            },
            {
                "amount": "488.00",
                "merchantName": "Booking.com | 5011338048 | 84.2 | reservation",
                "property": "84 Madison",
            },
            {
                "amount": "423.00",
                "merchantName": "Booking.com | 5841816877 | 86.1 | reservation",
                "property": "86 Madison",
            },
            {
                "amount": "182.00",
                "merchantName": "Booking.com | 5911686529 | 86.1 | reservation",
                "property": "86 Madison",
            },
            {
                "amount": "-83.00",
                "merchantName": "Booking.com | 5911686529 | 86.1 | refund",
                "property": "86 Madison",
            },
        ],
    },
]


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(value.get("text") or "").split())
    return " ".join(str(value or "").split())


def graphql(payload: dict[str, Any]) -> dict[str, Any]:
    return run_graphql_via_cdp(
        payload,
        bridge_path=BRIDGE,
        workspace_root=ROOT,
        timeout=120,
    )


def query_parent(transaction_id: str) -> dict[str, Any]:
    result = graphql(
        {
            "operationName": "BookingDepositParent",
            "variables": {"id": transaction_id},
            "query": """
            query BookingDepositParent($id: ID!) {
              transactionById(id: $id) {
                id amount date merchantName propertyId tagId bankAccountId note
                isSplit isDeleted parentId pending
                splitTransactions {
                  id amount date merchantName propertyId tagId parentId isDeleted
                }
              }
            }
            """,
        }
    )
    row = result["data"]["transactionById"]
    if not row:
        raise RuntimeError(f"Baselane transaction {transaction_id} was not found")
    return row


def active_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in parent.get("splitTransactions") or []
        if not row.get("isDeleted")
    ]


def child_key(row: dict[str, Any]) -> tuple[Decimal, str, str, str, str]:
    return (
        money(row.get("amount") or 0),
        str(row.get("date") or ""),
        str(row.get("merchantName") or ""),
        str(row.get("propertyId") or ""),
        str(row.get("tagId") or ""),
    )


def target_children(spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "amount": money(component["amount"]),
            "date": spec["date"],
            "merchantName": component["merchantName"],
            "propertyId": PROPERTY[component["property"]],
            "tagId": TAG_SHORT_TERM_RENTS,
        }
        for component in spec["components"]
    ]
    if sum((row["amount"] for row in rows), Decimal("0")) != money(spec["amount"]):
        raise RuntimeError(f"split components do not sum for {spec['reference']}")
    return rows


def normalized(rows: list[dict[str, Any]]) -> list[tuple[Decimal, str, str, str, str]]:
    return sorted(child_key(row) for row in rows if not row.get("isDeleted"))


def verify_source(parent: dict[str, Any], spec: dict[str, Any]) -> None:
    checks = {
        "id": (str(parent.get("id")), spec["id"]),
        "amount": (money(parent.get("amount") or 0), money(spec["amount"])),
        "date": (str(parent.get("date") or ""), spec["date"]),
        "tagId": (str(parent.get("tagId") or ""), TAG_SHORT_TERM_RENTS),
        "merchantName": (str(parent.get("merchantName") or ""), "Booking.com"),
        "bankAccountId": (str(parent.get("bankAccountId") or ""), "38968"),
        "parentId": (str(parent.get("parentId") or ""), ""),
        "isDeleted": (bool(parent.get("isDeleted")), False),
        "pending": (bool(parent.get("pending")), False),
    }
    failures = [
        f"{field}: live={actual!r} expected={expected!r}"
        for field, (actual, expected) in checks.items()
        if actual != expected
    ]
    if failures:
        raise RuntimeError(
            f"immutable live preflight failed for {spec['reference']}: "
            + "; ".join(failures)
        )


def update_parent(parent: dict[str, Any], spec: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "id": spec["id"],
        "merchantName": "Booking.com",
        "tagId": TAG_SHORT_TERM_RENTS,
        "note": spec["note"],
    }
    if spec.get("property"):
        values["propertyId"] = PROPERTY[spec["property"]]
    result = graphql(
        {
            "operationName": "UpdateBookingDeposit",
            "variables": {"input": [values]},
            "query": """
            mutation UpdateBookingDeposit($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) {
                id amount date merchantName propertyId tagId note
              }
            }
            """,
        }
    )["data"]["updateTransactions"]
    if len(result) != 1:
        raise RuntimeError(f"parent update failed for {spec['reference']}")


def reconcile_split(parent: dict[str, Any], targets: list[dict[str, Any]]) -> None:
    existing = active_children(parent)
    by_key: dict[tuple[Decimal, str, str, str, str], list[dict[str, Any]]] = {}
    for row in existing:
        by_key.setdefault(child_key(row), []).append(row)

    kept: set[str] = set()
    inputs: list[dict[str, Any]] = []
    for target in targets:
        row = dict(target)
        matches = sorted(
            by_key.get(child_key(target), []),
            key=lambda item: int(str(item["id"])),
            reverse=True,
        )
        if matches:
            row["id"] = str(matches[0]["id"])
            kept.add(str(matches[0]["id"]))
        inputs.append(row)

    for child in existing:
        child_id = str(child["id"])
        if child_id not in kept:
            inputs.append(
                {
                    "id": child_id,
                    "amount": money(child.get("amount") or 0),
                    "date": str(child.get("date") or parent["date"]),
                    "merchantName": str(child.get("merchantName") or ""),
                    "propertyId": str(child.get("propertyId") or ""),
                    "tagId": str(child.get("tagId") or TAG_SHORT_TERM_RENTS),
                    "isDelete": True,
                }
            )

    graphql(
        {
            "operationName": "createOrUpdateSplitTx",
            "variables": {
                "parentTransactionId": str(parent["id"]),
                "splitType": "AMOUNT",
                "transactionSplitInputs": [
                    {**row, "amount": float(row["amount"]), "propertyUnitId": None}
                    for row in inputs
                ],
            },
            "query": """
            mutation createOrUpdateSplitTx(
              $parentTransactionId: ID!
              $splitType: SplitType!
              $transactionSplitInputs: [TransactionSplitInput!]!
            ) {
              createOrUpdateSplitTx(input: {
                parentTransactionId: $parentTransactionId
                transactionSplitInputs: $transactionSplitInputs
                splitType: $splitType
              }) {
                id
              }
            }
            """,
        }
    )


def desired(parent: dict[str, Any], spec: dict[str, Any]) -> bool:
    if str(parent.get("tagId") or "") != TAG_SHORT_TERM_RENTS:
        return False
    if note_text(parent.get("note")) != spec["note"]:
        return False
    if spec.get("property"):
        return (
            str(parent.get("propertyId") or "") == PROPERTY[spec["property"]]
            and not parent.get("isSplit")
        )
    targets = target_children(spec)
    return bool(parent.get("isSplit")) and normalized(active_children(parent)) == normalized(targets)


def run(apply: bool) -> dict[str, Any]:
    before: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for spec in SPECS:
        parent = query_parent(spec["id"])
        verify_source(parent, spec)
        before.append((spec, parent))

    if apply:
        for spec, parent in before:
            if desired(parent, spec):
                continue
            update_parent(parent, spec)
            if spec.get("components"):
                refreshed = query_parent(spec["id"])
                targets = target_children(spec)
                if normalized(active_children(refreshed)) != normalized(targets):
                    reconcile_split(refreshed, targets)

    verified: list[dict[str, Any]] = []
    for spec in SPECS:
        parent = query_parent(spec["id"])
        verify_source(parent, spec)
        ok = desired(parent, spec)
        if apply and not ok:
            raise RuntimeError(f"post-write verification failed for {spec['reference']}")
        verified.append(
            {
                "reference": spec["reference"],
                "transaction_id": spec["id"],
                "amount": spec["amount"],
                "date": spec["date"],
                "property": spec.get("property"),
                "status": "verified" if ok else "change_required",
                "components": [
                    {
                        "amount": str(money(row["amount"])),
                        "merchantName": row["merchantName"],
                        "propertyId": str(row["propertyId"]),
                        "tagId": str(row["tagId"]),
                    }
                    for row in active_children(parent)
                ],
            }
        )

    report = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "applied": apply,
        "classification": {
            "tag_id": TAG_SHORT_TERM_RENTS,
            "category": "Short Term Rents",
            "gross_revenue": True,
            "booking_commission_netting": False,
        },
        "transactions": verified,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply property tags/native split after immutable live preflight.",
    )
    args = parser.parse_args()
    print(json.dumps(run(args.apply), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
