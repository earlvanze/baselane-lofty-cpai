#!/usr/bin/env python3
"""Reconcile 804 S Quitman permanent-loan PITI through July 2026.

The September 2025 refinance first produced a November 2025 installment. Cash
payments already in the DAO bank account are decomposed with Baselane-native
splits. Months paid outside Baselane are recognized as an expense/asset mix
with an equal payable to Nathaniel Gipson. This script never moves cash.
"""

from __future__ import annotations

import argparse
import calendar
import fcntl
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from baselane_apply_alcott_accruals_live import run_graphql  # noqa: E402


PROPERTY = "804 S Quitman St"
PROPERTY_ID = "57369"
START_BALANCE = Decimal("358500.00")
ANNUAL_RATE = Decimal("0.06995")
MONTHLY_RATE = ANNUAL_RATE / Decimal(12)
MONTHLY_PI = Decimal("2383.91")
TAX_ESCROW = Decimal("144.48")
INSURANCE_ESCROW = Decimal("153.47")
GENERAL_ESCROW = Decimal("18.14")
TOTAL_PAYMENT = Decimal("2700.00")
START_MONTH = "2025-11"
END_MONTH = "2026-07"

TAG_INTEREST = "11"
TAG_INSURANCE = "8"
TAG_TAX = "15"
TAG_PRINCIPAL = "20"
TAG_ADVANCE = "25"
TAG_GENERAL_ESCROW = "130"

# Exact, ID-bearing Baselane cash evidence. February is a reimbursement of the
# same month's outside payment, so splitting that cash row recognizes PITI and
# leaves no February payable outstanding.
CASH_PARENT_BY_MONTH = {
    "2025-11": "206413737",
    "2026-02": "240737933",
    "2026-03": "248909334",
    "2026-04": "263412404",
    "2026-05": "277715631",
}

# No matching debit exists anywhere in the refreshed Baselane export. Per the
# 2026-08-02 operator instruction these were funded by Nathaniel outside the
# DAO bank account and remain payable to him until cash settlement is proven.
NATHANIEL_ADVANCE_MONTHS = {"2025-12", "2026-01", "2026-06", "2026-07"}

MARKER_PREFIX = "AOPS-804-NATHANIEL-MORTGAGE-ADVANCE"
REPORT = ROOT / "reports" / "baselane_804_mortgage_reconciliation.json"
PIPELINE_LOCK = ROOT / "scripts" / ".baselane_source_pipeline.lock"


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def month_range(start: str = START_MONTH, end: str = END_MONTH) -> list[str]:
    year, month = map(int, start.split("-"))
    end_year, end_month = map(int, end.split("-"))
    result: list[str] = []
    while (year, month) <= (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return result


def month_end(month: str) -> str:
    year, number = map(int, month.split("-"))
    return date(year, number, calendar.monthrange(year, number)[1]).isoformat()


def schedule() -> dict[str, dict[str, Decimal]]:
    result: dict[str, dict[str, Decimal]] = {}
    balance = START_BALANCE
    for month in month_range():
        interest = money(balance * MONTHLY_RATE)
        scheduled_principal = money(MONTHLY_PI - interest)
        result[month] = {
            "principal": scheduled_principal,
            "interest": interest,
            "tax_escrow": TAX_ESCROW,
            "insurance_escrow": INSURANCE_ESCROW,
            "general_escrow": GENERAL_ESCROW,
            "total": TOTAL_PAYMENT,
            "starting_balance": balance,
            "ending_balance": money(balance - scheduled_principal),
        }
        balance = result[month]["ending_balance"]
    return result


def components(month: str, *, negative: bool, tx_date: str) -> list[dict[str, Any]]:
    row = schedule()[month]
    sign = Decimal("-1") if negative else Decimal("1")
    items = (
        ("principal", row["principal"], TAG_PRINCIPAL, "Mortgage Principal"),
        ("interest", row["interest"], TAG_INTEREST, "Mortgage Interest"),
        ("tax_escrow", row["tax_escrow"], TAG_TAX, "Mortgage Escrow - Property Taxes"),
        ("insurance_escrow", row["insurance_escrow"], TAG_INSURANCE, "Mortgage Escrow - Insurance"),
        ("general_escrow", row["general_escrow"], TAG_GENERAL_ESCROW, "Mortgage Escrow - General/Unapplied"),
    )
    return [
        {
            "component": key,
            "amount": str(money(sign * amount)),
            "tagId": tag_id,
            "propertyId": PROPERTY_ID,
            "merchantName": f"804 S Quitman {label} | {month}",
            "date": tx_date,
        }
        for key, amount, tag_id, label in items
    ]


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def query_parent(transaction_id: str) -> dict[str, Any] | None:
    result = run_graphql({
        "operationName": "TransactionById",
        "variables": {"id": transaction_id},
        "query": """
        query TransactionById($id: ID!) {
          transactionById(id: $id) {
            id amount date propertyId tagId bankAccountId merchantName note isDeleted hidden
            splitTransactions {
              id amount date propertyId tagId merchantName isDeleted
            }
          }
        }
        """,
    })["data"]
    return result.get("transactionById")


def query_property_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = run_graphql({
            "operationName": "Transactions",
            "variables": {"input": {
                "sort": {"direction": "DESC", "field": "date"},
                "filter": {"propertyId": PROPERTY_ID, "isHidden": False, "isDeleted": False},
                "page": page,
                "pageLimit": 1000,
            }},
            "query": """
            query Transactions($input: SortsAndFilters) {
              transactions(input: $input) {
                total
                data { id amount date propertyId tagId bankAccountId merchantName note isManual isDeleted hidden }
              }
            }
            """,
        })["data"]["transactions"]
        batch = result.get("data") or []
        rows.extend(batch)
        if not batch or len(rows) >= int(result.get("total") or 0):
            return rows
        page += 1


def active_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in parent.get("splitTransactions") or [] if not row.get("isDeleted")]


def normalized(rows: list[dict[str, Any]]) -> list[tuple[str, str, Decimal, str, str]]:
    return sorted(
        (
            str(row.get("tagId") or ""),
            str(row.get("propertyId") or ""),
            money(row.get("amount") or 0),
            str(row.get("merchantName") or ""),
            str(row.get("date") or ""),
        )
        for row in rows
    )


def manual_targets() -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for month in sorted(NATHANIEL_ADVANCE_MONTHS):
        tx_date = month_end(month)
        basis = (
            "Permanent-loan PITI funded outside Baselane by Nathaniel Gipson; DAO is the obligor. "
            "No matching Baselane cash debit exists. Accounting-only recognition; no cash movement. "
            "Closing disclosure fixes monthly P&I at $2,383.91 and estimated escrow at $297.95; "
            "$18.14 recurring payment excess remains general escrow/unapplied pending servicer evidence."
        )
        for item in components(month, negative=True, tx_date=tx_date):
            marker = f"{MARKER_PREFIX}|{item['component']}|{PROPERTY}|{month}"
            targets.append({
                **item,
                "marker": marker,
                "note": f"{marker} | {basis}",
            })
        marker = f"{MARKER_PREFIX}|payable|{PROPERTY}|{month}"
        targets.append({
            "component": "payable",
            "amount": str(TOTAL_PAYMENT),
            "tagId": TAG_ADVANCE,
            "propertyId": PROPERTY_ID,
            "merchantName": f"Nathaniel Gipson Mortgage Advance | 804 S Quitman | {month}",
            "date": tx_date,
            "marker": marker,
            "note": (
                f"{marker} | Matching payable recognition for the full $2,700.00 installment funded by "
                "Nathaniel Gipson outside Baselane. Creditor is Nathaniel Gipson, not ECO Systems LLC. "
                "Accounting-only recognition; no cash movement."
            ),
        })
    return targets


def manual_matches(target: dict[str, Any], row: dict[str, Any]) -> bool:
    return all((
        money(row.get("amount") or 0) == money(target["amount"]),
        str(row.get("date") or "") == target["date"],
        str(row.get("propertyId") or "") == PROPERTY_ID,
        str(row.get("tagId") or "") == target["tagId"],
        str(row.get("merchantName") or "") == target["merchantName"],
        note_text(row.get("note")) == target["note"],
        row.get("bankAccountId") is None,
    ))


def build_plan() -> dict[str, Any]:
    split_actions: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for month, transaction_id in CASH_PARENT_BY_MONTH.items():
        parent = query_parent(transaction_id)
        if not parent or parent.get("isDeleted") or parent.get("hidden"):
            blockers.append({"month": month, "parent_id": transaction_id, "reason": "missing_or_inactive_parent"})
            continue
        if money(parent.get("amount") or 0) != -TOTAL_PAYMENT or str(parent.get("propertyId") or "") != PROPERTY_ID:
            blockers.append({
                "month": month,
                "parent_id": transaction_id,
                "reason": "parent_identity_mismatch",
                "live_amount": str(parent.get("amount")),
                "live_property_id": str(parent.get("propertyId")),
            })
            continue
        target = components(month, negative=True, tx_date=str(parent["date"]))
        if normalized(active_children(parent)) == normalized(target):
            split_actions.append({"month": month, "parent_id": transaction_id, "action": "already_split"})
        elif active_children(parent):
            blockers.append({
                "month": month,
                "parent_id": transaction_id,
                "reason": "unexpected_existing_split",
                "children": active_children(parent),
            })
        else:
            split_actions.append({"month": month, "parent_id": transaction_id, "action": "split", "target": target})

    by_marker: dict[str, list[dict[str, Any]]] = {}
    for row in query_property_rows():
        marker = note_text(row.get("note")).split(" | ", 1)[0]
        if marker.startswith(MARKER_PREFIX):
            by_marker.setdefault(marker, []).append(row)

    manual_create: list[dict[str, Any]] = []
    manual_skip: list[str] = []
    for target in manual_targets():
        rows = by_marker.get(target["marker"]) or []
        if len(rows) > 1:
            blockers.append({"marker": target["marker"], "reason": "duplicate_manual_marker", "ids": [r["id"] for r in rows]})
        elif not rows:
            manual_create.append(target)
        elif manual_matches(target, rows[0]):
            manual_skip.append(target["marker"])
        else:
            blockers.append({"marker": target["marker"], "reason": "manual_identity_mismatch", "live": rows[0]})

    return {
        "split_actions": split_actions,
        "manual_create": manual_create,
        "manual_skip": manual_skip,
        "blockers": blockers,
    }


def plan_digest(plan: dict[str, Any]) -> str:
    payload = {
        "split_actions": [row for row in plan["split_actions"] if row["action"] == "split"],
        "manual_create": plan["manual_create"],
        "blockers": plan["blockers"],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def mutation_split(parent_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return run_graphql({
        "operationName": "createOrUpdateSplitTx",
        "variables": {
            "parentTransactionId": parent_id,
            "splitType": "AMOUNT",
            "transactionSplitInputs": [{
                "tagId": row["tagId"],
                "propertyId": PROPERTY_ID,
                "propertyUnitId": None,
                "date": row["date"],
                "amount": float(row["amount"]),
                "merchantName": row["merchantName"],
            } for row in rows],
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
          }) { id splitTransactions { id amount tagId propertyId merchantName date isDeleted } }
        }
        """,
    })["data"]["createOrUpdateSplitTx"]


def mutation_create_manual(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    for offset in range(0, len(rows), 5):
        batch = rows[offset:offset + 5]
        fields: list[str] = []
        for index, row in enumerate(batch):
            fields.append(
                f"r{index}: createTransaction(input: {{"
                f"merchantName: {json.dumps(row['merchantName'])} "
                f"note: {json.dumps(row['note'])} "
                f"tagId: {json.dumps(row['tagId'])} propertyId: {json.dumps(PROPERTY_ID)} "
                "unitId: null entityId: null bankAccountId: null "
                f"date: {json.dumps(row['date'])} amount: {row['amount']} isReviewedByUser: true"
                "}) { id amount date propertyId tagId bankAccountId note isManual }"
            )
        data = run_graphql({
            "operationName": "Create804MortgageAccruals",
            "variables": {},
            "query": "mutation Create804MortgageAccruals {\n" + "\n".join(fields) + "\n}",
        })["data"]
        created.extend(data[f"r{index}"] for index in range(len(batch)))
    return created


@contextmanager
def exclusive_pipeline_lock():
    PIPELINE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with PIPELINE_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--require-plan-digest")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)

    plan = build_plan()
    digest = plan_digest(plan)
    applied: dict[str, Any] | None = None
    verify: dict[str, Any] | None = None
    if args.apply:
        if not args.require_plan_digest:
            parser.error("--apply requires --require-plan-digest")
        with exclusive_pipeline_lock():
            plan = build_plan()
            digest = plan_digest(plan)
            if digest != args.require_plan_digest:
                raise SystemExit(f"plan digest changed: expected {args.require_plan_digest}, current {digest}")
            if plan["blockers"]:
                raise SystemExit(f"live blockers prevent apply: {json.dumps(plan['blockers'], indent=2)}")
            applied = {"splits": [], "manual_created": []}
            for action in plan["split_actions"]:
                if action["action"] == "split":
                    applied["splits"].append(mutation_split(action["parent_id"], action["target"]))
            applied["manual_created"] = mutation_create_manual(plan["manual_create"])
            verify = build_plan()

    post_actions = [] if verify is None else [row for row in verify["split_actions"] if row["action"] == "split"]
    status = "blocked" if plan["blockers"] else "needs_apply"
    if verify is not None:
        status = "ok" if not verify["blockers"] and not post_actions and not verify["manual_create"] else "failed"
    elif not any(row["action"] == "split" for row in plan["split_actions"]) and not plan["manual_create"]:
        status = "ok"

    amortization = {
        month: {key: str(value) for key, value in values.items()}
        for month, values in schedule().items()
    }
    report = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "status": status,
        "mode": "apply" if args.apply else "dry_run",
        "property": PROPERTY,
        "property_id": PROPERTY_ID,
        "scope": {"start_month": START_MONTH, "end_month": END_MONTH},
        "cash_parent_by_month": CASH_PARENT_BY_MONTH,
        "nathaniel_advance_months": sorted(NATHANIEL_ADVANCE_MONTHS),
        "accounting_basis": {
            "monthly_pi": str(MONTHLY_PI),
            "monthly_tax_escrow": str(TAX_ESCROW),
            "monthly_insurance_escrow": str(INSURANCE_ESCROW),
            "monthly_general_escrow_unapplied": str(GENERAL_ESCROW),
            "total_monthly_payment": str(TOTAL_PAYMENT),
            "outstanding_payable_to_nathaniel": str(TOTAL_PAYMENT * len(NATHANIEL_ADVANCE_MONTHS)),
        },
        "plan_digest": digest,
        "plan": plan,
        "applied": applied,
        "verify": verify,
        "amortization": amortization,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": status,
        "mode": report["mode"],
        "plan_digest": digest,
        "split_needed": sum(row["action"] == "split" for row in plan["split_actions"]),
        "manual_rows_needed": len(plan["manual_create"]),
        "blocker_count": len(plan["blockers"]),
        "outstanding_payable_to_nathaniel": report["accounting_basis"]["outstanding_payable_to_nathaniel"],
        "report": str(args.report),
    }, indent=2))
    return 0 if status in {"ok", "needs_apply"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
