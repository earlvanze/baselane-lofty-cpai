#!/usr/bin/env python3
"""Reconcile Bowmanville's approved EARLDAO lending position.

Bowmanville is the lender. Most interest and the May 2026 principal redemption
were paid from EARLDAO directly to Lofty, so Baselane contains the EARLDAO cash
expense but no Bowmanville bank mirror. This script adds bankless, net-zero
pass-through accounting on Bowmanville, accrues the three genuinely unpaid
months, and decomposes the $7,003.30 redemption into $7,000 principal plus a
$3.30 non-principal operating-reserve top-up. It never moves cash.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path("/home/digit/.openclaw/workspace")
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_apply_alcott_accruals_live import run_graphql  # noqa: E402
from baselane_mcp.transfers import list_active_transfer_accounts  # noqa: E402
from baselane_settle_madison_pm_mortgage import (  # noqa: E402
    cents,
    normalized_children,
    query_parent,
    reconcile_parent_split,
)

REPORT = ROOT / "reports" / "bowmanville_earldao_note_reconciliation.20260729.json"
BOWMANVILLE_PROPERTY_ID = "83241"
EARLDAO_PROPERTY_ID = "94512"
EARLDAO_BANK_ID = "131545"
PRINCIPAL_PARENT_ID = "284916805"
MARKER_PREFIX = "BOWMANVILLE-EARLDAO|"

PAID_INTEREST = [
    ("2025-10", "2025-12-17", Decimal("412.00"), "221195348"),
    ("2025-11", "2025-12-17", Decimal("412.00"), "221195348"),
    ("2026-01", "2026-01-21", Decimal("412.00"), "233529322"),
    ("2026-03", "2026-03-18", Decimal("412.00"), "255803758"),
    ("2026-04", "2026-05-18", Decimal("412.00"), "284917001"),
    ("2026-05", "2026-05-19", Decimal("412.00"), "284998151"),
    ("2026-06", "2026-06-01", Decimal("342.00"), "291164888"),
    ("2026-07", "2026-07-01", Decimal("342.00"), "306770512"),
]
UNPAID_INTEREST = [
    ("2025-09", "2025-09-30"),
    ("2025-12", "2025-12-31"),
    ("2026-02", "2026-02-28"),
]

SOURCE_EXPECTATIONS = {
    "221195348": ("-824.00", "2025-12-17", "12"),
    "233529322": ("-412.00", "2026-01-21", "12"),
    "255803758": ("-412.00", "2026-03-18", "12"),
    "284917001": ("-412.00", "2026-05-18", "12"),
    "284998151": ("-412.00", "2026-05-19", "12"),
    "291164888": ("-342.00", "2026-06-01", "12"),
    "306770512": ("-342.00", "2026-07-01", "12"),
}


def money(value: Any) -> str:
    return f"{Decimal(str(value or 0)).quantize(Decimal('0.01')):.2f}"


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(value.get("text") or "").split())
    return " ".join(str(value or "").split())


def query_transaction(transaction_id: str) -> dict[str, Any]:
    result = run_graphql(
        {
            "operationName": "BowmanvilleEvidence",
            "variables": {"id": transaction_id},
            "query": """
            query BowmanvilleEvidence($id: ID!) {
              transactionById(id: $id) {
                id amount date merchantName note propertyId tagId bankAccountId
                isManual isDeleted parentId
              }
            }
            """,
        }
    )["data"]["transactionById"]
    if not result:
        raise RuntimeError(f"Baselane transaction {transaction_id} not found")
    return result


def source_evidence() -> tuple[list[dict[str, Any]], list[str]]:
    rows = [query_transaction(transaction_id) for transaction_id in SOURCE_EXPECTATIONS]
    issues: list[str] = []
    for row in rows:
        expected = SOURCE_EXPECTATIONS[str(row["id"])]
        actual = (
            money(row.get("amount")),
            str(row.get("date") or ""),
            str(row.get("tagId") or ""),
        )
        if actual != expected:
            issues.append(f"source_changed:{row['id']}:{actual}")
        if (
            str(row.get("propertyId") or "") != EARLDAO_PROPERTY_ID
            or str(row.get("bankAccountId") or "") != EARLDAO_BANK_ID
            or row.get("parentId")
            or row.get("isDeleted")
        ):
            issues.append(f"source_scope_changed:{row['id']}")
    return rows, issues


def target_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month, row_date, amount, source_id in PAID_INTEREST:
        common = (
            f"Interest for {month} was paid externally from EARLDAO cash "
            f"transaction {source_id} directly to Lofty for Bowmanville. "
            "Bankless pass-through recognition only; no new cash."
        )
        rows.extend(
            [
                {
                    "marker": f"{MARKER_PREFIX}paid-pass-through|{month}|income",
                    "date": row_date,
                    "amount": amount,
                    "merchantName": f"EARLDAO interest income | Bowmanville | {month}",
                    "propertyId": BOWMANVILLE_PROPERTY_ID,
                    "tagId": "2",
                    "note": f"{MARKER_PREFIX}paid-pass-through|{month}|income | {common}",
                },
                {
                    "marker": f"{MARKER_PREFIX}paid-pass-through|{month}|distribution",
                    "date": row_date,
                    "amount": -amount,
                    "merchantName": f"Interest sent to Lofty | Bowmanville | {month}",
                    "propertyId": BOWMANVILLE_PROPERTY_ID,
                    "tagId": "25",
                    "note": f"{MARKER_PREFIX}paid-pass-through|{month}|distribution | {common}",
                },
            ]
        )
    for month, row_date in UNPAID_INTEREST:
        amount = Decimal("412.00")
        common = (
            f"Unpaid 12% annual interest on Bowmanville's $41,200 EARLDAO "
            f"principal for {month}. Accounting accrual only; no bank transfer."
        )
        rows.extend(
            [
                {
                    "marker": f"{MARKER_PREFIX}unpaid-interest|{month}|earldao",
                    "date": row_date,
                    "amount": -amount,
                    "merchantName": f"Bowmanville interest payable | EARLDAO | {month}",
                    "propertyId": EARLDAO_PROPERTY_ID,
                    "tagId": "12",
                    "note": f"{MARKER_PREFIX}unpaid-interest|{month}|earldao | {common}",
                },
                {
                    "marker": f"{MARKER_PREFIX}unpaid-interest|{month}|bowmanville",
                    "date": row_date,
                    "amount": amount,
                    "merchantName": f"EARLDAO interest receivable | Bowmanville | {month}",
                    "propertyId": BOWMANVILLE_PROPERTY_ID,
                    "tagId": "2",
                    "note": f"{MARKER_PREFIX}unpaid-interest|{month}|bowmanville | {common}",
                },
            ]
        )
    principal_common = (
        "External pass-through recognition for EARLDAO cash transaction "
        "284916805 paid directly to Lofty; no new cash."
    )
    rows.extend(
        [
            {
                "marker": f"{MARKER_PREFIX}principal-redemption|2026-05|receipt",
                "date": "2026-05-18",
                "amount": Decimal("7000.00"),
                "merchantName": "EARLDAO principal redemption | Bowmanville | $7,000",
                "propertyId": BOWMANVILLE_PROPERTY_ID,
                "tagId": "23",
                "note": f"{MARKER_PREFIX}principal-redemption|2026-05|receipt | {principal_common}",
            },
            {
                "marker": f"{MARKER_PREFIX}principal-redemption|2026-05|distribution",
                "date": "2026-05-18",
                "amount": Decimal("-7000.00"),
                "merchantName": "Principal sent to Lofty | Bowmanville | $7,000",
                "propertyId": BOWMANVILLE_PROPERTY_ID,
                "tagId": "25",
                "note": f"{MARKER_PREFIX}principal-redemption|2026-05|distribution | {principal_common}",
            },
            {
                "marker": f"{MARKER_PREFIX}principal-redemption|2026-05|topup-in",
                "date": "2026-05-18",
                "amount": Decimal("3.30"),
                "merchantName": "EARLDAO non-principal OR top-up | Bowmanville | $3.30",
                "propertyId": BOWMANVILLE_PROPERTY_ID,
                "tagId": "25",
                "note": f"{MARKER_PREFIX}principal-redemption|2026-05|topup-in | {principal_common}",
            },
            {
                "marker": f"{MARKER_PREFIX}principal-redemption|2026-05|topup-out",
                "date": "2026-05-18",
                "amount": Decimal("-3.30"),
                "merchantName": "OR top-up sent to Lofty | Bowmanville | $3.30",
                "propertyId": BOWMANVILLE_PROPERTY_ID,
                "tagId": "25",
                "note": f"{MARKER_PREFIX}principal-redemption|2026-05|topup-out | {principal_common}",
            },
        ]
    )
    return rows


def query_managed_rows() -> list[dict[str, Any]]:
    response = run_graphql(
        {
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "ASC", "field": "date"},
                    "filter": {
                        "search": "BOWMANVILLE-EARLDAO",
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
                  id amount date merchantName note propertyId tagId bankAccountId
                  isManual isDeleted
                }
              }
            }
            """,
        }
    )
    return list(response["data"]["transactions"]["data"])


def marker_map(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    expected = {row["marker"] for row in target_rows()}
    found: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    for row in rows:
        matches = [marker for marker in expected if marker in note_text(row.get("note"))]
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
        and str(row.get("date") or "") == target["date"]
        and str(row.get("merchantName") or "") == target["merchantName"]
        and str(row.get("propertyId") or "") == target["propertyId"]
        and str(row.get("tagId") or "") == target["tagId"]
        and row.get("bankAccountId") is None
        and bool(row.get("isManual"))
        and note_text(row.get("note")) == target["note"]
    )


def desired_principal_children() -> list[dict[str, Any]]:
    return [
        {
            "amount": Decimal("-7000.00"),
            "date": "2026-05-18",
            "merchantName": "Bowmanville EARLDAO principal redemption | $7,000",
            "propertyId": EARLDAO_PROPERTY_ID,
            "tagId": "22",
        },
        {
            "amount": Decimal("-3.30"),
            "date": "2026-05-18",
            "merchantName": "Bowmanville OR top-up outside principal | $3.30",
            "propertyId": EARLDAO_PROPERTY_ID,
            "tagId": "25",
        },
    ]


def validate_principal_parent(parent: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    actual = (
        money(parent.get("amount")),
        str(parent.get("date") or ""),
        str(parent.get("propertyId") or ""),
        str(parent.get("bankAccountId") or ""),
        bool(parent.get("isDeleted")),
        parent.get("parentId"),
    )
    expected = ("-7003.30", "2026-05-18", EARLDAO_PROPERTY_ID, EARLDAO_BANK_ID, False, None)
    if actual != expected:
        issues.append(f"principal_parent_changed:{actual}")
    return issues


def account_snapshot() -> list[dict[str, Any]]:
    return [
        row
        for row in list_active_transfer_accounts(run_graphql)
        if any(
            token in str(row.get("nickname") or "").lower()
            for token in ("bowmanville", "earldao")
        )
    ]


def build_plan() -> dict[str, Any]:
    source, issues = source_evidence()
    principal = query_parent(PRINCIPAL_PARENT_ID)
    issues.extend(validate_principal_parent(principal))
    current, marker_issues = marker_map(query_managed_rows())
    issues.extend(marker_issues)
    actions = []
    for target in target_rows():
        row = current.get(target["marker"])
        action = "skip" if row and target_matches(target, row) else ("update" if row else "create")
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
    split_matches = (
        normalized_children(principal.get("splitTransactions") or [])
        == normalized_children(desired_principal_children())
    )
    return {
        "scope": "254 Bowmanville / EARLDAO lending note",
        "as_of": "2026-07-28",
        "issues": issues,
        "governance": {
            "approval_date": "2025-08-05",
            "approval_id": "3157918842",
            "approved_commitment": "44000.00",
            "terms": "12% APY; Bowmanville is lender and EARLDAO is borrower",
        },
        "principal": {
            "documented_opening_web3_value": "41200.00",
            "principal_redeemed_2026_05_18": "7000.00",
            "nonprincipal_or_topup": "3.30",
            "ending_principal": "34200.00",
            "native_split_action": "skip" if split_matches else "reconcile",
        },
        "interest": {
            "paid_periods": [month for month, *_ in PAID_INTEREST],
            "unpaid_periods": [month for month, _ in UNPAID_INTEREST],
            "unpaid_total": "1236.00",
        },
        "cash": {
            "accounts": account_snapshot(),
            "cash_movement_created": False,
            "cash_transfer_due_now": "0.00",
            "reason": (
                "Bowmanville is sold/inactive and has a zero-balance operations "
                "account; policy requires bankless accruals instead of a new transfer."
            ),
        },
        "source_payments": source,
        "accounting_actions": actions,
    }


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
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
                "amount",
                "merchantName",
                "note",
                "tagId",
                "propertyId",
                "unitId",
                "isReviewedByUser",
            )
        },
    }
    result = run_graphql(
        {
            "operationName": "UpdateBowmanvilleReconciliation",
            "variables": {"input": [update]},
            "query": """
            mutation UpdateBowmanvilleReconciliation($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) { id }
            }
            """,
        }
    )["data"]["updateTransactions"]
    if len(result) != 1 or str(result[0]["id"]) != transaction_id:
        raise RuntimeError(f"Baselane did not confirm update {transaction_id}")


def apply_targets() -> None:
    current, issues = marker_map(query_managed_rows())
    if issues:
        raise RuntimeError(f"managed-row issues: {issues}")
    for target in target_rows():
        row = current.get(target["marker"])
        if row and target_matches(target, row):
            continue
        if row:
            update_transaction(str(row["id"]), target)
        else:
            create_transaction(target)
    parent = query_parent(PRINCIPAL_PARENT_ID)
    target = desired_principal_children()
    if normalized_children(parent.get("splitTransactions") or []) != normalized_children(target):
        reconcile_parent_split(parent, target)


def verify() -> dict[str, Any]:
    plan = build_plan()
    issues = list(plan["issues"])
    if any(row["action"] != "skip" for row in plan["accounting_actions"]):
        issues.append("managed_accounting_rows_do_not_match_target_state")
    if plan["principal"]["native_split_action"] != "skip":
        issues.append("principal_native_split_does_not_match_target_state")
    return {
        **plan,
        "status": "verified" if not issues else "verification_failed",
        "issues": issues,
    }


def write_report(payload: dict[str, Any]) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        report = verify()
        write_report(report)
        print(json.dumps({**report, "report": str(REPORT)}, indent=2, default=str))
        return 0 if report["status"] == "verified" else 2
    plan = build_plan()
    plan_digest = digest(plan)
    dry = {"status": "dry_run", "digest": plan_digest, **plan}
    write_report(dry)
    if not args.apply:
        print(json.dumps({**dry, "report": str(REPORT)}, indent=2, default=str))
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
    print(json.dumps({**report, "report": str(REPORT)}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
