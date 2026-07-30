#!/usr/bin/env python3
"""Idempotently accrue Willard's unpaid EARLDAO interest through 2026-07-28.

This creates paired bankless accounting rows only. Willard has no active
dedicated DAO bank account, so pooled ECO/Hemlane/Aligned cash is never moved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql
from baselane_live_dao_cash_reconciliation import graphql, note_text
from baselane_mcp.transfers import list_active_transfer_accounts

ROOT = Path("/home/digit/.openclaw/workspace")
REPORT = ROOT / "reports" / "willard_earldao_interest_reconciliation.20260729.json"
WILLARD_PROPERTY_ID = "81779"
EARLDAO_PROPERTY_ID = "94512"
MARKER_PREFIX = "WILLARD-EARLDAO|interest|"
MONTHLY_INTEREST = Decimal("110.00")
MONTHS = [
    ("2025-11", "2025-11-30"),
    ("2025-12", "2025-12-31"),
    ("2026-01", "2026-01-31"),
    ("2026-02", "2026-02-28"),
    ("2026-03", "2026-03-31"),
    ("2026-04", "2026-04-30"),
    ("2026-05", "2026-05-31"),
    ("2026-06", "2026-06-30"),
    ("2026-07", "2026-07-28"),
]


def money(value: Any) -> str:
    return f"{Decimal(str(value or 0)).quantize(Decimal('0.01')):.2f}"


def targets() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month, row_date in MONTHS:
        common = (
            "12% simple interest on Willard's documented $11,000 net "
            f"EARLDAO principal for {month}; $110 monthly; no compounding. "
            "Accounting accrual only; no bank transfer."
        )
        rows.extend(
            [
                {
                    "marker": f"{MARKER_PREFIX}{month}|borrower",
                    "date": row_date,
                    "amount": -MONTHLY_INTEREST,
                    "merchantName": f"EARLDAO interest accrual | 8708 Willard | {month}",
                    "propertyId": WILLARD_PROPERTY_ID,
                    "tagId": "12",
                    "note": f"{MARKER_PREFIX}{month}|borrower | {common}",
                },
                {
                    "marker": f"{MARKER_PREFIX}{month}|lender",
                    "date": row_date,
                    "amount": MONTHLY_INTEREST,
                    "merchantName": f"Willard interest receivable | EARLDAO | {month}",
                    "propertyId": EARLDAO_PROPERTY_ID,
                    "tagId": "2",
                    "note": f"{MARKER_PREFIX}{month}|lender | {common}",
                },
            ]
        )
    return rows


def query_marker_rows() -> list[dict[str, Any]]:
    result = run_graphql(
        {
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "ASC", "field": "date"},
                    "filter": {
                        "search": "WILLARD-EARLDAO",
                        "isHidden": False,
                        "isDeleted": False,
                    },
                    "page": 1,
                    "pageLimit": 250,
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
        }
    )["data"]["transactions"]["data"]
    return [row for row in result if MARKER_PREFIX in note_text(row.get("note"))]


def marker_map(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    expected = {row["marker"] for row in targets()}
    found: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    for row in rows:
        matches = [
            marker for marker in expected if marker in note_text(row.get("note"))
        ]
        if len(matches) != 1:
            issues.append(f"unrecognized_or_ambiguous_managed_row:{row.get('id')}")
            continue
        marker = matches[0]
        if marker in found:
            issues.append(f"duplicate_managed_marker:{marker}")
        found[marker] = row
    return found, issues


def target_matches(target: dict[str, Any], row: dict[str, Any]) -> bool:
    return (
        money(row.get("amount")) == money(target["amount"])
        and str(row.get("date") or "")[:10] == target["date"]
        and str(row.get("merchantName") or "") == target["merchantName"]
        and str(row.get("propertyId") or "") == target["propertyId"]
        and str(row.get("tagId") or "") == target["tagId"]
        and row.get("bankAccountId") is None
        and bool(row.get("isManual"))
        and note_text(row.get("note")) == target["note"]
    )


def dedicated_willard_accounts() -> list[dict[str, Any]]:
    return [
        row
        for row in list_active_transfer_accounts(graphql)
        if "willard" in str(row.get("nickname") or "").lower()
    ]


def build_plan() -> dict[str, Any]:
    current, issues = marker_map(query_marker_rows())
    actions = []
    for target in targets():
        row = current.get(target["marker"])
        action = "skip" if row and target_matches(target, row) else (
            "update" if row else "create"
        )
        actions.append(
            {
                "action": action,
                "existing_id": str(row["id"]) if row else None,
                **{
                    key: money(value) if key == "amount" else value
                    for key, value in target.items()
                },
            }
        )
    accounts = dedicated_willard_accounts()
    if accounts:
        issues.append("active_dedicated_willard_account_requires_cash_review")
    return {
        "scope": "8708 Willard / EARLDAO unpaid interest",
        "as_of": "2026-07-28",
        "issues": issues,
        "governance": {
            "approval_date": "2025-05-27",
            "terms": "12% APR; interest-only; one-year balloon; non-dilutive",
            "maturity": "2026-05-30",
        },
        "principal": {
            "gross_earldao_receivable": "15000.00",
            "willard_reciprocal_receivable": "4000.00",
            "documented_net_servicing_balance": "11000.00",
        },
        "interest": {
            "last_cash_payment_date": "2025-11-05",
            "last_cash_payment_amount": "800.00",
            "last_cash_payment_coverage": "2025-06 through 2025-10",
            "monthly_amount": "110.00",
            "months": [month for month, _ in MONTHS],
            "gross_unpaid": money(MONTHLY_INTEREST * len(MONTHS)),
        },
        "cash": {
            "active_dedicated_willard_accounts": accounts,
            "cash_transfer_created": False,
            "cash_transfer_due_now": "0.00",
            "reason": (
                "No active dedicated Willard DAO bank account exists. Pooled "
                "ECO/Hemlane/Aligned balances are not a permissible source."
            ),
        },
        "accounting_actions": actions,
    }


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def values(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "merchantName": target["merchantName"],
        "note": target["note"],
        "tagId": target["tagId"],
        "propertyId": target["propertyId"],
        "unitId": None,
        "entityId": None,
        "date": target["date"],
        "bankAccountId": None,
        "amount": float(target["amount"]),
        "isReviewedByUser": True,
    }


def create_transaction(target: dict[str, Any]) -> None:
    run_graphql(
        {
            "operationName": "createTransaction",
            "variables": values(target),
            "query": """
            mutation createTransaction(
              $merchantName: String!, $note: String!, $tagId: ID,
              $propertyId: ID, $unitId: ID, $entityId: Int, $date: String!,
              $bankAccountId: ID, $amount: Float!,
              $isReviewedByUser: Boolean
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


def update_transaction(transaction_id: str, target: dict[str, Any]) -> None:
    row = values(target)
    update = {
        "id": transaction_id,
        **{
            key: row[key]
            for key in (
                "amount", "merchantName", "note", "tagId", "propertyId",
                "unitId", "isReviewedByUser",
            )
        },
    }
    result = graphql(
        {
            "operationName": "UpdateWillardEarldaoInterest",
            "variables": {"input": [update]},
            "query": """
            mutation UpdateWillardEarldaoInterest($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) { id }
            }
            """,
        }
    )["data"]["updateTransactions"]
    if len(result) != 1 or str(result[0]["id"]) != transaction_id:
        raise RuntimeError(f"Baselane did not confirm update {transaction_id}")


def apply_targets() -> None:
    current, issues = marker_map(query_marker_rows())
    if issues:
        raise RuntimeError(f"managed-row issues: {issues}")
    for target in targets():
        row = current.get(target["marker"])
        if row and target_matches(target, row):
            continue
        if row:
            update_transaction(str(row["id"]), target)
        else:
            create_transaction(target)


def verify() -> dict[str, Any]:
    plan = build_plan()
    issues = list(plan["issues"])
    if any(row["action"] != "skip" for row in plan["accounting_actions"]):
        issues.append("managed_accounting_rows_do_not_match_target_state")
    return {
        **plan,
        "status": "verified" if not issues else "verification_failed",
        "issues": issues,
    }


def write_report(payload: dict[str, Any]) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        report = verify()
        write_report(report)
        print(json.dumps({**report, "report": str(REPORT)}, indent=2))
        return 0 if report["status"] == "verified" else 2

    plan = build_plan()
    plan_digest = digest(plan)
    dry = {"status": "dry_run", "digest": plan_digest, **plan}
    write_report(dry)
    if not args.apply:
        print(json.dumps({**dry, "report": str(REPORT)}, indent=2))
        return 0 if not plan["issues"] else 2
    if args.digest != plan_digest:
        raise RuntimeError(f"live digest is {plan_digest}; exact --digest required")
    if plan["issues"]:
        raise RuntimeError(f"refusing apply with issues: {plan['issues']}")

    apply_targets()
    report = verify()
    if report["status"] != "verified":
        raise RuntimeError(f"post-apply verification failed: {report['issues']}")
    report["status"] = "applied_and_verified"
    report["digest"] = plan_digest
    write_report(report)
    print(json.dumps({**report, "report": str(REPORT)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
