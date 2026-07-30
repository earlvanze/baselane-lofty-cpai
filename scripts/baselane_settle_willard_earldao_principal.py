#!/usr/bin/env python3
"""Post Willard's supported $8,000 noncash EARLDAO principal settlement.

The paired, bankless rows allocate ECO-held Willard funds to EARLDAO without
claiming that a new bank transfer occurred. The workflow is idempotent and
requires an exact dry-run digest before mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql
from baselane_live_dao_cash_reconciliation import note_text


ROOT = Path("/home/digit/.openclaw/workspace")
REPORT = ROOT / "reports" / "willard_earldao_principal_settlement.20260729.json"
WILLARD_PROPERTY_ID = "81779"
EARLDAO_PROPERTY_ID = "94512"
MARKER_PREFIX = "WILLARD-EARLDAO|principal-settlement|2026-07-29|8000.00|"


def money(value: Any) -> str:
    return f"{Decimal(str(value or 0)).quantize(Decimal('0.01')):.2f}"


def targets() -> list[dict[str, Any]]:
    common = (
        "Noncash allocation of $8,000 of ECO-held Willard funds to EARLDAO "
        "principal on 2026-07-29. Reduces the documented net servicing "
        "balance from $11,000 to $3,000. No new bank cash movement."
    )
    return [
        {
            "marker": f"{MARKER_PREFIX}borrower",
            "date": "2026-07-29",
            "amount": Decimal("-8000.00"),
            "merchantName": "Willard -> EARLDAO | principal settlement | $8,000",
            "propertyId": WILLARD_PROPERTY_ID,
            "tagId": "22",
            "note": f"{MARKER_PREFIX}borrower | {common}",
        },
        {
            "marker": f"{MARKER_PREFIX}lender",
            "date": "2026-07-29",
            "amount": Decimal("8000.00"),
            "merchantName": "EARLDAO <- Willard | principal settlement | $8,000",
            "propertyId": EARLDAO_PROPERTY_ID,
            "tagId": "23",
            "note": f"{MARKER_PREFIX}lender | {common}",
        },
    ]


def query_rows() -> list[dict[str, Any]]:
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
                  note isManual isDeleted parentId
                }
              }
            }
            """,
        }
    )["data"]["transactions"]["data"]
    return [
        row
        for row in result
        if MARKER_PREFIX in note_text(row.get("note"))
    ]


def marker_map(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
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
        and row.get("parentId") is None
        and bool(row.get("isManual"))
        and note_text(row.get("note")) == target["note"]
    )


def build_plan() -> dict[str, Any]:
    current, issues = marker_map(query_rows())
    actions: list[dict[str, Any]] = []
    for target in targets():
        row = current.get(target["marker"])
        actions.append(
            {
                "action": "skip" if row and target_matches(target, row) else (
                    "update" if row else "create"
                ),
                "existing_id": str(row["id"]) if row else None,
                **{
                    key: money(value) if key == "amount" else value
                    for key, value in target.items()
                },
            }
        )
    return {
        "scope": "8708 Willard / EARLDAO principal settlement",
        "as_of": "2026-07-29",
        "issues": issues,
        "principal": {
            "documented_net_before": "11000.00",
            "settlement": "8000.00",
            "documented_net_after": "3000.00",
        },
        "eco_net_dao_funds": {
            "sheet_value_before": "8524.76",
            "expected_after_refresh": "524.76",
        },
        "cash_movement_created": False,
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
    result = run_graphql(
        {
            "operationName": "UpdateWillardEarldaoPrincipal",
            "variables": {"input": [update]},
            "query": """
            mutation UpdateWillardEarldaoPrincipal($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) { id }
            }
            """,
        }
    )["data"]["updateTransactions"]
    if len(result) != 1 or str(result[0]["id"]) != transaction_id:
        raise RuntimeError(f"Baselane did not confirm update {transaction_id}")


def apply_targets() -> None:
    current, issues = marker_map(query_rows())
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
