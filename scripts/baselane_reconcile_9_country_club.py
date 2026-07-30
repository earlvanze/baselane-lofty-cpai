#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql


ROOT = Path(os.environ.get("WORKSPACE_ROOT") or Path.cwd()).resolve()
PROPERTY_ID = "91341"
PROPERTY = "9 Country Club Ln N"
OPERATIONS_ACCOUNT_ID = "133098"

TAG_MORTGAGE_INTEREST = "11"
TAG_MORTGAGE_FEES = "34"
TAG_PROPERTY_MANAGEMENT = "80"
TAG_INSURANCE = "65"
TAG_TAXES = "95"
TAG_TRANSFERS = "24"

PM_PAYMENT_ID = "240295549"
JUNE_INSURANCE_PAYMENT_ID = "302557345"
JUNE_INSURANCE_ACCRUAL_ID = "313980406"
JULY_MORTGAGE_PARENT_ID = "306737335"
JULY_MORTGAGE_FEE_CHILD_ID = "314558401"
TAX_PAYMENT_ID = "247839471"
NBT_VERIFICATION_DEBIT_IDS = ("311540949", "311541005")

MORTGAGE_START_MONTH = "2025-09"
MORTGAGE_END_MONTH = "2026-07"
MORTGAGE_INTEREST = Decimal("2875.00")


def cents(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def month_range(start: str, end: str) -> list[str]:
    year, month = map(int, start.split("-"))
    end_year, end_month = map(int, end.split("-"))
    result = []
    while (year, month) <= (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return result


def query_transactions(filter_values: dict[str, Any]) -> list[dict[str, Any]]:
    query = """
    query Transactions($input: SortsAndFilters) {
      transactions(input: $input) {
        total
        data {
          id amount date merchantName description propertyId tagId bankAccountId note
          isManual hidden isDeleted isSplit parentId
        }
      }
    }
    """
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = run_graphql({
            "operationName": "Transactions",
            "variables": {"input": {
                "sort": {"direction": "ASC", "field": "date"},
                "filter": {**filter_values, "isHidden": False, "isDeleted": False},
                "page": page,
                "pageLimit": 1000,
            }},
            "query": query,
        })["data"]["transactions"]
        batch = result.get("data") or []
        rows.extend(batch)
        if not batch or len(rows) >= int(result.get("total") or 0):
            return rows
        page += 1


def query_bank_balances() -> dict[str, Decimal]:
    data = run_graphql({
        "operationName": "BankAccounts",
        "variables": {},
        "query": "query BankAccounts { bankAccounts { id currentBalance availableBalance } }",
    })["data"]["bankAccounts"]
    return {
        str(row["id"]): cents(row.get("currentBalance"))
        for row in data
        if str(row.get("id")) in {OPERATIONS_ACCOUNT_ID, "165515"}
    }


def update_transaction(transaction_id: str, values: dict[str, Any]) -> dict[str, Any]:
    payload = {"id": transaction_id, **values, "isReviewedByUser": True}
    return run_graphql({
        "operationName": "UpdateTransaction",
        "variables": {"input": [payload]},
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id amount date propertyId tagId bankAccountId note isManual
          }
        }
        """,
    })["data"]["updateTransactions"][0]


def create_transaction(target: dict[str, Any]) -> dict[str, Any]:
    values = {
        "merchantName": target["merchantName"],
        "note": target["note"],
        "tagId": target["tagId"],
        "propertyId": PROPERTY_ID,
        "unitId": None,
        "entityId": None,
        "date": target["date"],
        "bankAccountId": None,
        "amount": float(target["amount"]),
        "isReviewedByUser": True,
    }
    return run_graphql({
        "operationName": "createTransaction",
        "variables": values,
        "query": """
        mutation createTransaction(
          $merchantName: String!, $note: String!, $tagId: ID, $propertyId: ID,
          $unitId: ID, $entityId: Int, $date: String!, $bankAccountId: ID,
          $amount: Float!, $isReviewedByUser: Boolean
        ) {
          createTransaction(input: {
            merchantName: $merchantName note: $note tagId: $tagId
            propertyId: $propertyId unitId: $unitId entityId: $entityId
            date: $date bankAccountId: $bankAccountId amount: $amount
            isReviewedByUser: $isReviewedByUser
          }) { id amount date propertyId tagId bankAccountId note isManual }
        }
        """,
    })["data"]["createTransaction"]


def split_july_mortgage() -> dict[str, Any]:
    children = [
        {
            "amount": -2875.00,
            "tagId": TAG_MORTGAGE_INTEREST,
            "propertyId": PROPERTY_ID,
            "merchantName": "UBS Bank USA - monthly mortgage interest",
            "date": "2026-07-01",
        },
        {
            "amount": -150.25,
            "tagId": TAG_MORTGAGE_FEES,
            "propertyId": PROPERTY_ID,
            "merchantName": "UBS Bank USA - mortgage fees",
            "date": "2026-07-01",
        },
    ]
    return run_graphql({
        "operationName": "createOrUpdateSplitTx",
        "variables": {
            "parentTransactionId": JULY_MORTGAGE_PARENT_ID,
            "splitType": "AMOUNT",
            "transactionSplitInputs": children,
        },
        "query": """
        mutation createOrUpdateSplitTx(
          $parentTransactionId: ID!, $splitType: SplitType!,
          $transactionSplitInputs: [TransactionSplitInput!]!
        ) {
          createOrUpdateSplitTx(input: {
            parentTransactionId: $parentTransactionId
            transactionSplitInputs: $transactionSplitInputs
            splitType: $splitType
          }) {
            id
            splitTransactions { id tagId propertyId amount merchantName date parentId }
          }
        }
        """,
    })["data"]["createOrUpdateSplitTx"]


def scheduled_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    existing_notes = {note_text(row.get("note")) for row in rows}

    for month in month_range(MORTGAGE_START_MONTH, MORTGAGE_END_MONTH):
        marker = f"AOPS-PNL-ACCRUAL|mortgage_interest|{PROPERTY}|{month}|2875.00"
        if any(marker in note for note in existing_notes):
            continue
        targets.append({
            "kind": "mortgage_accrual",
            "merchantName": f"Mortgage Interest Accrual | {PROPERTY} | {month}",
            "date": "2026-07-01" if month == "2026-07" else f"{month}-28",
            "amount": -MORTGAGE_INTEREST,
            "tagId": TAG_MORTGAGE_INTEREST,
            "note": marker + " | Monthly interest-only UBS obligation. Accounting/manual accrual only, no bank transfer.",
        })

    mortgage_cash = defaultdict(Decimal)
    mortgage_dates: dict[str, str] = {}
    for row in rows:
        if str(row.get("bankAccountId") or "") != OPERATIONS_ACCOUNT_ID:
            continue
        if str(row.get("tagId") or "") != TAG_MORTGAGE_INTEREST:
            continue
        month = str(row.get("date") or "")[:7]
        mortgage_cash[month] += cents(row.get("amount"))
        mortgage_dates[month] = max(mortgage_dates.get(month, ""), str(row.get("date") or ""))
    if "2026-07" not in mortgage_cash:
        mortgage_cash["2026-07"] = -MORTGAGE_INTEREST
        mortgage_dates["2026-07"] = "2026-07-01"
    for month, net_amount in sorted(mortgage_cash.items()):
        amount = max(Decimal("0"), -net_amount)
        if amount <= 0:
            continue
        marker = f"AOPS-9CC-RECON|mortgage_settlement|{PROPERTY}|{month}|{amount:.2f}"
        if any(marker in note for note in existing_notes):
            continue
        targets.append({
            "kind": "mortgage_settlement",
            "merchantName": f"Mortgage Interest Cash Settlement | {PROPERTY} | {month}",
            "date": mortgage_dates[month],
            "amount": amount,
            "tagId": TAG_MORTGAGE_INTEREST,
            "note": marker + " | Offsets bank-paid mortgage interest against the full monthly accrual schedule; no bank transfer.",
        })

    pm_cash = defaultdict(Decimal)
    pm_dates: dict[str, str] = {}
    for row in rows:
        is_fixed_pm_row = str(row.get("id")) == PM_PAYMENT_ID
        if str(row.get("bankAccountId") or "") != OPERATIONS_ACCOUNT_ID:
            continue
        if str(row.get("tagId") or "") != TAG_PROPERTY_MANAGEMENT and not is_fixed_pm_row:
            continue
        month = str(row.get("date") or "")[:7]
        pm_cash[month] += cents(row.get("amount"))
        pm_dates[month] = max(pm_dates.get(month, ""), str(row.get("date") or ""))
    for month, net_amount in sorted(pm_cash.items()):
        amount = max(Decimal("0"), -net_amount)
        if amount <= 0:
            continue
        marker = f"AOPS-9CC-RECON|pm_settlement_cash|{PROPERTY}|{month}|{amount:.2f}"
        if any(marker in note for note in existing_notes):
            continue
        targets.append({
            "kind": "pm_settlement",
            "merchantName": f"PM Fee Cash Settlement | {PROPERTY} | {month}",
            "date": pm_dates[month],
            "amount": amount,
            "tagId": TAG_PROPERTY_MANAGEMENT,
            "note": marker + " | Offsets cash paid to ECO against cumulative PM fee accruals; no bank transfer.",
        })

    tax_row = next((row for row in rows if str(row.get("id")) == TAX_PAYMENT_ID), None)
    tax_amount = max(Decimal("0"), -cents(tax_row.get("amount"))) if tax_row else Decimal("262.45")
    tax_marker = f"AOPS-9CC-RECON|tax_settlement|{PROPERTY}|2026-02|{tax_amount:.2f}"
    if tax_amount and not any(tax_marker in note for note in existing_notes):
        targets.append({
            "kind": "tax_settlement",
            "merchantName": f"Property Tax Cash Settlement | {PROPERTY} | 2026-02",
            "date": str(tax_row.get("date") if tax_row else "2026-02-28"),
            "amount": tax_amount,
            "tagId": TAG_TAXES,
            "note": tax_marker + " | Offsets the Mount Pleasant cash payment against tax accruals; no bank transfer.",
        })
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit and idempotently reconcile live Baselane rows for 9 Country Club Ln N.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "baselane_9_country_club_reconciliation.json")
    args = parser.parse_args()

    property_rows = query_transactions({"propertyId": PROPERTY_ID})
    pm_search_rows = query_transactions({"search": "PM Fees Jan 2026"})
    rows_by_id = {str(row["id"]): row for row in property_rows + pm_search_rows}
    target_rows_by_id = {str(row["id"]): row for row in property_rows}
    for row in pm_search_rows:
        if str(row.get("id")) == PM_PAYMENT_ID:
            target_rows_by_id[str(row["id"])] = row
    targets = scheduled_targets(list(target_rows_by_id.values()))
    balances = query_bank_balances()

    updates = [
        {
            "id": PM_PAYMENT_ID,
            "values": {"propertyId": PROPERTY_ID, "unitId": None, "tagId": TAG_PROPERTY_MANAGEMENT},
            "reason": "January 2026 PM cash payment belonged to 9 Country Club and settles its PM accrual",
        },
        {
            "id": JUNE_INSURANCE_PAYMENT_ID,
            "values": {"propertyId": PROPERTY_ID, "unitId": None, "tagId": TAG_INSURANCE},
            "reason": "June New York Central $918 debit is the monthly dwelling insurance premium",
        },
        {
            "id": JUNE_INSURANCE_ACCRUAL_ID,
            "values": {
                "amount": 0.0,
                "propertyId": PROPERTY_ID,
                "unitId": None,
                "tagId": TAG_INSURANCE,
                "note": "AOPS-9CC-RECON|void_insurance_accrual|9 Country Club Ln N|2026-06|1552.91 | Voided because the $918 New York Central premium cleared the DAO bank on 2026-06-23.",
            },
            "reason": "remove duplicate and stale-rate June insurance accrual",
        },
        {
            "id": JULY_MORTGAGE_FEE_CHILD_ID,
            "values": {"propertyId": PROPERTY_ID, "unitId": None, "tagId": TAG_MORTGAGE_FEES},
            "reason": "classify the $150.25 UBS remainder as other loan payment/mortgage fees",
        },
        *[
            {
                "id": transaction_id,
                "values": {"propertyId": PROPERTY_ID, "unitId": None, "tagId": TAG_TRANSFERS},
                "reason": "pair the NBT account-verification debit with its equal transfer credit",
            }
            for transaction_id in NBT_VERIFICATION_DEBIT_IDS
        ],
    ]

    july_parent = rows_by_id.get(JULY_MORTGAGE_PARENT_ID)
    split_required = bool(july_parent and not july_parent.get("isSplit"))
    actions: list[dict[str, Any]] = []
    if args.apply:
        if split_required:
            actions.append({"kind": "split", "result": split_july_mortgage()})
        for update in updates:
            current = rows_by_id.get(update["id"])
            if current is None:
                actions.append({"kind": "update", "id": update["id"], "status": "missing"})
                continue
            result = update_transaction(update["id"], update["values"])
            actions.append({"kind": "update", "id": update["id"], "reason": update["reason"], "result": result})
        for target in targets:
            actions.append({"kind": "create", "target_kind": target["kind"], "result": create_transaction(target)})

    mortgage_accrual_total = MORTGAGE_INTEREST * len(month_range(MORTGAGE_START_MONTH, MORTGAGE_END_MONTH))
    mortgage_settlement_total = sum((target["amount"] for target in targets if target["kind"] == "mortgage_settlement"), Decimal("0"))
    mortgage_settlement_total += sum(
        (cents(row.get("amount")) for row in property_rows if "AOPS-9CC-RECON|mortgage_settlement|" in note_text(row.get("note"))),
        Decimal("0"),
    )
    pm_settlement_total = sum((target["amount"] for target in targets if target["kind"] == "pm_settlement"), Decimal("0"))
    pm_settlement_total += sum(
        (cents(row.get("amount")) for row in property_rows if "AOPS-9CC-RECON|pm_settlement_cash|" in note_text(row.get("note"))),
        Decimal("0"),
    )

    report = {
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": "applied" if args.apply else "dry_run",
        "property": PROPERTY,
        "property_id": PROPERTY_ID,
        "bank_balances": {key: float(value) for key, value in balances.items()},
        "physical_cash": float(sum(balances.values(), Decimal("0"))),
        "split_required": split_required,
        "updates": updates,
        "create_target_count": len(targets),
        "create_targets": [{**row, "amount": float(row["amount"])} for row in targets],
        "mortgage": {
            "loan_type": "interest_only",
            "principal_balance": 1200000.00,
            "rate": 0.02875,
            "monthly_interest": float(MORTGAGE_INTEREST),
            "scheduled_months": month_range(MORTGAGE_START_MONTH, MORTGAGE_END_MONTH),
            "scheduled_interest": float(mortgage_accrual_total),
            "cash_settlements": float(mortgage_settlement_total),
            "outstanding_interest_payable": float(mortgage_accrual_total - mortgage_settlement_total),
        },
        "pm_cash_settlements": float(pm_settlement_total),
        "actions": actions,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "create_target_count": report["create_target_count"],
        "split_required": split_required,
        "physical_cash": report["physical_cash"],
        "mortgage_outstanding": report["mortgage"]["outstanding_interest_payable"],
        "report": str(args.report),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
