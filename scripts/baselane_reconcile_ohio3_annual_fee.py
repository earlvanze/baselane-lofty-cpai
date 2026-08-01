#!/usr/bin/env python3
"""Reconcile Ohio 3's 2025 annual DAO administration fee.

The bank transfer and the fee recognition are separate facts. The existing
$750 cash component is normalized to $550 paid to ECO plus a $200 DAO credit
held by ECO. Only $550 is recognized as DAO expense / ECO revenue because
Lofty charges its separate $200 year-end filing fee directly on its ledger.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from baselane_apply_monthly_accruals_live import exclusive_apply_lock, run_graphql


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "baselane_ohio3_annual_fee_reconciliation.json"
CENT = Decimal("0.01")

ECO_PROPERTY = "37648"
OHIO3_PROPERTY = "83240"
TAG_REVENUE = "2"
TAG_TRANSFER = "24"
TAG_CONSULTING = "79"
TAG_FILING = "97"

DAO_PARENT = "222512039"
ECO_PARENT = "222512042"
FILING_DATE = "2025-11-03"
MARKER_PREFIX = "AOPS-OHIO3-ANNUAL-FEE|2025|"

FILING_ROWS = {
    "180621853": {"date": "2025-08-12", "amount": "-62.25", "bank": "114283"},
    "180621913": {"date": "2025-08-12", "amount": "-103.75", "bank": "114283"},
    "206452617": {"date": "2025-11-05", "amount": "-62.25", "bank": "117761"},
}

LEGACY_MONTHLY_ACCRUALS = {
    "313978374": {
        "date": "2026-06-28", "amount": "-62.50", "property": OHIO3_PROPERTY,
        "tag": "72", "marker": "AOPS-MONTHLY-ACCRUAL|dao|1518 Dille Rd|2026-06|62.50",
    },
    "321259927": {
        "date": "2026-06-28", "amount": "62.50", "property": ECO_PROPERTY,
        "tag": TAG_REVENUE, "marker": "AOPS-MONTHLY-ACCRUAL|dao_eco|1518 Dille Rd|2026-06|62.50",
    },
    "322454397": {
        "date": "2026-07-28", "amount": "-62.50", "property": OHIO3_PROPERTY,
        "tag": "72", "marker": "AOPS-MONTHLY-ACCRUAL|dao|1518 Dille Rd|2026-07|62.50",
    },
    "322454350": {
        "date": "2026-07-28", "amount": "62.50", "property": ECO_PROPERTY,
        "tag": TAG_REVENUE, "marker": "AOPS-MONTHLY-ACCRUAL|dao_eco|1518 Dille Rd|2026-07|62.50",
    },
}

MANUAL_TARGETS = (
    {
        "side": "dao",
        "amount": "-550.00",
        "merchantName": "ECO Systems LLC | 2025 annual DAO administration fee",
        "note": MARKER_PREFIX + "dao|550.00",
        "tagId": TAG_CONSULTING,
        "propertyId": OHIO3_PROPERTY,
    },
    {
        "side": "eco",
        "amount": "550.00",
        "merchantName": "Ohio 3 | 2025 annual DAO administration fee income",
        "note": MARKER_PREFIX + "eco|550.00",
        "tagId": TAG_REVENUE,
        "propertyId": ECO_PROPERTY,
    },
)


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def query_exact() -> dict[str, dict[str, Any]]:
    ids = [*FILING_ROWS, *LEGACY_MONTHLY_ACCRUALS, DAO_PARENT, ECO_PARENT]
    rows: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(ids), 5):
        fields = []
        for index, row_id in enumerate(ids[offset : offset + 5]):
            fields.append(
                f"""
                t{index}: transactionById(id: \"{row_id}\") {{
                  id amount date merchantName propertyId tagId bankAccountId note
                  isManual isSplit isDeleted parentId
                  splitTransactions {{
                    id amount date merchantName propertyId tagId parentId isDeleted
                  }}
                }}
                """
            )
        result = run_graphql(
            {
                "operationName": "Ohio3AnnualFeeExact",
                "variables": {},
                "query": f"query Ohio3AnnualFeeExact {{ {' '.join(fields)} }}",
            }
        )["data"]
        rows.update({str(row["id"]): row for row in result.values() if row})
    return rows


def query_markers() -> list[dict[str, Any]]:
    result = run_graphql(
        {
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"field": "date", "direction": "DESC"},
                    "filter": {"search": MARKER_PREFIX, "isHidden": False, "isDeleted": False},
                    "page": 1,
                    "pageLimit": 100,
                }
            },
            "query": """
            query Transactions($input: SortsAndFilters) {
              transactions(input: $input) {
                total
                data { id amount date merchantName propertyId tagId bankAccountId note isDeleted }
              }
            }
            """,
        }
    )["data"]["transactions"]
    return result.get("data") or []


def active_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in parent.get("splitTransactions") or [] if not row.get("isDeleted")]


def child_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        f"{money(row['amount']):.2f}",
        str(row.get("date") or ""),
        str(row.get("merchantName") or ""),
        str(row.get("propertyId") or ""),
        str(row.get("tagId") or ""),
    )


def desired_children(parent: dict[str, Any], side: str) -> list[dict[str, Any]]:
    current = active_children(parent)
    sign = Decimal("-1") if side == "dao" else Decimal("1")
    old_fee = [row for row in current if abs(money(row["amount"])) == Decimal("750.00")]
    target_fee = [row for row in current if abs(money(row["amount"])) in {Decimal("550.00"), Decimal("200.00")}]
    if old_fee and target_fee:
        raise RuntimeError(f"{side} parent has both legacy and target annual-fee children")
    if len(old_fee) not in {0, 1}:
        raise RuntimeError(f"{side} parent must have one legacy $750 child")
    if not old_fee and len(target_fee) != 2:
        raise RuntimeError(f"{side} parent has neither the legacy nor target annual-fee split")

    replaced_ids = {str(row["id"]) for row in old_fee + target_fee}
    preserved = [
        {
            "id": str(row["id"]),
            "amount": money(row["amount"]),
            "date": str(row["date"]),
            "merchantName": str(row["merchantName"]),
            "propertyId": str(row["propertyId"]),
            "tagId": str(row["tagId"]),
        }
        for row in current
        if str(row["id"]) not in replaced_ids
    ]
    labels = (
        (
            "Ohio 3 | cash paid to ECO | 2025 annual administration fee",
            "Ohio 3 | excess annual-fee cash held by ECO as DAO credit",
        )
        if side == "dao"
        else (
            "Ohio 3 | cash received by ECO | 2025 annual administration fee",
            "Ohio 3 | DAO cash held by ECO as annual-fee credit",
        )
    )
    target = preserved + [
        {
            "amount": sign * Decimal("550.00"),
            "date": str(parent["date"]),
            "merchantName": labels[0],
            "propertyId": OHIO3_PROPERTY,
            "tagId": TAG_TRANSFER,
        },
        {
            "amount": sign * Decimal("200.00"),
            "date": str(parent["date"]),
            "merchantName": labels[1],
            "propertyId": OHIO3_PROPERTY,
            "tagId": TAG_TRANSFER,
        },
    ]
    if sum((money(row["amount"]) for row in target), Decimal("0")) != money(parent["amount"]):
        raise RuntimeError(f"{side} target children do not sum to parent")
    return target


def validate(rows: dict[str, dict[str, Any]], markers: list[dict[str, Any]]) -> dict[str, Any]:
    missing = sorted(
        (set(FILING_ROWS) | set(LEGACY_MONTHLY_ACCRUALS) | {DAO_PARENT, ECO_PARENT})
        - set(rows)
    )
    if missing:
        raise RuntimeError(f"missing exact transactions: {missing}")
    for row_id, expected in FILING_ROWS.items():
        row = rows[row_id]
        if row.get("isDeleted"):
            raise RuntimeError(f"filing transaction {row_id} is deleted")
        actual = (str(row["date"]), f"{money(row['amount']):.2f}", str(row["bankAccountId"]), str(row["tagId"]))
        wanted = (expected["date"], expected["amount"], expected["bank"], TAG_FILING)
        if actual != wanted or "WYOMING SECRETARY" not in str(row["merchantName"]).upper():
            raise RuntimeError(f"filing transaction {row_id} no longer matches approved evidence")
    for parent_id, amount, bank in (
        (DAO_PARENT, "-1300.38", "152895"),
        (ECO_PARENT, "1300.38", "38968"),
    ):
        parent = rows[parent_id]
        if f"{money(parent['amount']):.2f}" != amount or str(parent["bankAccountId"]) != bank:
            raise RuntimeError(f"cash parent {parent_id} no longer matches approved evidence")
        if str(parent["date"]) != "2025-12-22" or parent.get("isDeleted"):
            raise RuntimeError(f"cash parent {parent_id} has unexpected date/status")

    for row_id, expected in LEGACY_MONTHLY_ACCRUALS.items():
        row = rows[row_id]
        actual = (
            str(row.get("date") or ""),
            f"{money(row.get('amount') or 0):.2f}",
            str(row.get("propertyId") or ""),
            str(row.get("tagId") or ""),
            bool(row.get("isManual")),
        )
        wanted = (
            expected["date"], expected["amount"], expected["property"],
            expected["tag"], True,
        )
        if actual != wanted or expected["marker"] not in note_text(row.get("note")):
            raise RuntimeError(f"legacy monthly accrual {row_id} no longer matches evidence")

    marker_by_note = {note_text(row.get("note")): row for row in markers}
    duplicate_notes = [
        target["note"]
        for target in MANUAL_TARGETS
        if sum(note_text(row.get("note")) == target["note"] for row in markers) > 1
    ]
    if duplicate_notes:
        raise RuntimeError(f"duplicate accounting markers: {duplicate_notes}")
    for target in MANUAL_TARGETS:
        existing = marker_by_note.get(target["note"])
        if existing and (
            f"{money(existing['amount']):.2f}" != target["amount"]
            or str(existing["date"]) != FILING_DATE
            or str(existing["propertyId"]) != target["propertyId"]
            or str(existing["tagId"]) != target["tagId"]
            or existing.get("bankAccountId")
        ):
            raise RuntimeError(f"existing marker conflicts with target: {target['note']}")
    return marker_by_note


def plan(rows: dict[str, dict[str, Any]], markers: list[dict[str, Any]]) -> dict[str, Any]:
    marker_by_note = validate(rows, markers)
    filing_updates = [
        {"id": row_id, "propertyId": ECO_PROPERTY, "unitId": None}
        for row_id in FILING_ROWS
        if str(rows[row_id].get("propertyId") or "") != ECO_PROPERTY
    ]
    accrual_voids = [
        {"id": row_id, "isDeleted": True, "isReviewedByUser": True}
        for row_id in LEGACY_MONTHLY_ACCRUALS
        if not rows[row_id].get("isDeleted")
    ]
    dao_children = desired_children(rows[DAO_PARENT], "dao")
    eco_children = desired_children(rows[ECO_PARENT], "eco")
    manual_creates = [target for target in MANUAL_TARGETS if target["note"] not in marker_by_note]
    bounded = {
        "filing_updates": filing_updates,
        "legacy_monthly_accrual_voids": accrual_voids,
        "split_targets": {
            DAO_PARENT: [child_key(row) for row in dao_children],
            ECO_PARENT: [child_key(row) for row in eco_children],
        },
        "manual_creates": manual_creates,
    }
    digest = hashlib.sha256(
        json.dumps(bounded, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**bounded, "digest": digest, "_dao_children": dao_children, "_eco_children": eco_children}


def reconcile_split(parent: dict[str, Any], target: list[dict[str, Any]]) -> None:
    current = active_children(parent)
    by_key = {child_key(row): row for row in current}
    kept: set[str] = set()
    inputs: list[dict[str, Any]] = []
    for row in target:
        item = dict(row)
        existing = by_key.get(child_key(row))
        if existing:
            item["id"] = str(existing["id"])
            kept.add(str(existing["id"]))
        item["amount"] = float(money(item["amount"]))
        item["propertyUnitId"] = None
        inputs.append(item)
    for row in current:
        if str(row["id"]) not in kept:
            inputs.append(
                {
                    "id": str(row["id"]),
                    "amount": float(money(row["amount"])),
                    "date": str(row["date"]),
                    "merchantName": str(row["merchantName"]),
                    "propertyId": str(row["propertyId"]),
                    "tagId": str(row["tagId"]),
                    "propertyUnitId": None,
                    "isDelete": True,
                }
            )
    run_graphql(
        {
            "operationName": "createOrUpdateSplitTx",
            "variables": {
                "parentTransactionId": str(parent["id"]),
                "transactionSplitInputs": inputs,
                "splitType": "AMOUNT",
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
              }) { id }
            }
            """,
        }
    )


def apply_plan(rows: dict[str, dict[str, Any]], action: dict[str, Any]) -> None:
    transaction_updates = action["filing_updates"] + action["legacy_monthly_accrual_voids"]
    if transaction_updates:
        run_graphql(
            {
                "operationName": "UpdateTransaction",
                "variables": {"input": transaction_updates},
                "query": """
                mutation UpdateTransaction($input: [UpdateTransaction!]) {
                  updateTransactions(input: $input) { id propertyId tagId isDeleted }
                }
                """,
            }
        )
    reconcile_split(rows[DAO_PARENT], action["_dao_children"])
    reconcile_split(rows[ECO_PARENT], action["_eco_children"])
    for target in action["manual_creates"]:
        variables = {
            "merchantName": target["merchantName"],
            "note": target["note"],
            "tagId": target["tagId"],
            "propertyId": target["propertyId"],
            "unitId": None,
            "entityId": None,
            "date": FILING_DATE,
            "bankAccountId": None,
            "amount": float(money(target["amount"])),
            "isReviewedByUser": True,
        }
        run_graphql(
            {
                "operationName": "createTransaction",
                "variables": variables,
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
                  }) { id }
                }
                """,
            }
        )


def public_plan(action: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in action.items() if not key.startswith("_")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-digest")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()

    rows = query_exact()
    markers = query_markers()
    action = plan(rows, markers)
    if args.apply and args.confirm_digest != action["digest"]:
        parser.error(f"--confirm-digest must equal {action['digest']}")

    before = public_plan(action)
    if args.apply:
        with exclusive_apply_lock(True):
            locked_rows = query_exact()
            locked_markers = query_markers()
            locked_action = plan(locked_rows, locked_markers)
            if locked_action["digest"] != args.confirm_digest:
                raise RuntimeError("live state changed after preview; digest no longer matches")
            apply_plan(locked_rows, locked_action)

    verify_rows = query_exact() if args.apply else rows
    verify_markers = query_markers() if args.apply else markers
    after = plan(verify_rows, verify_markers)
    if args.apply:
        if (
            after["filing_updates"]
            or after["legacy_monthly_accrual_voids"]
            or after["manual_creates"]
        ):
            raise RuntimeError("independent readback found unapplied filing/manual actions")
        for parent_id in (DAO_PARENT, ECO_PARENT):
            if sorted(after["split_targets"][parent_id]) != sorted(
                child_key(row) for row in active_children(verify_rows[parent_id])
            ):
                raise RuntimeError(f"independent readback split mismatch for {parent_id}")

    report = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "mode": "apply" if args.apply else "preview",
        "policy": {
            "annual_report_date": FILING_DATE,
            "eco_income_and_dao_expense": "550.00",
            "lofty_direct_ledger_fee": "200.00",
            "cash_credit_held_by_eco": "200.00",
            "cash_parent_dates_preserved": True,
        },
        "before_plan": before,
        "readback_plan": public_plan(after),
        "verified": bool(args.apply),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
