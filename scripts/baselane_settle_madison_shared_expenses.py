#!/usr/bin/env python3
"""Guarded 86/90 -> 88 Madison shared-expense cash settlement.

The bank transfers and every native split child stay in category 24
(`Transfers Between Accounts`).  Month/category labels provide the audit trail
without turning reimbursement cash rows into duplicate P&L expenses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_mcp.transfers import (  # noqa: E402
    build_transfer_plan,
    execute_transfer,
    list_active_transfer_accounts,
    run_graphql_via_cdp,
)


BRIDGE = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
REPORT_DIR = ROOT / "reports"
STATE_PATH = REPORT_DIR / "baselane_madison_shared_expense_transfer_state.json"
FLOAT = Decimal("500.00")
CENT = Decimal("0.01")
TAG_TRANSFER = "24"

ACCOUNT = {
    "86": 70125,
    "88": 70936,
    "90": 70935,
}

SPECS = [
    {
        "key": "86_to_88",
        "from": ACCOUNT["86"],
        "to": ACCOUNT["88"],
        "amount": "1074.62",
        "property_id": "63162",
        "bookkeeping_note": (
            "Madison shared expenses 86 reimburses 88 through partial April 2026"
        ),
        "parent_label": "86-88 | shared expenses | through partial 2026-04",
        "note": (
            "86 Madison reimburses 88 Madison for 88-paid shared operating "
            "expenses after the March 6, 2026 catch-up: all March plus the "
            "cash-funded portion of April. Internal transfer only; all parent "
            "and split rows remain Transfers Between Accounts. Remaining "
            "unpaid 86-to-88 balance after this transfer: $1,965.43."
        ),
        "components": [
            ["86-88 | 2026-03 | Morgan Linen Service", "289.82"],
            ["86-88 | 2026-03 | County Waste", "67.56"],
            ["86-88 | 2026-03 | software", "60.11"],
            ["86-88 | 2026-03 | streaming", "3.25"],
            ["86-88 | 2026-03 | shared supplies", "32.48"],
            ["86-88 | 2026-04 | Morgan Linen Service | partial", "453.34"],
            ["86-88 | 2026-04 | County Waste", "87.81"],
            ["86-88 | 2026-04 | software | partial", "28.24"],
            ["86-88 | 2026-04 | streaming | partial", "6.75"],
            ["86-88 | 2026-04 | shared supplies", "45.26"],
        ],
    },
    {
        "key": "90_to_88",
        "from": ACCOUNT["90"],
        "to": ACCOUNT["88"],
        "amount": "923.48",
        "property_id": "31525",
        "bookkeeping_note": (
            "Madison shared expenses 90 reimburses 88 through partial April 2026"
        ),
        "parent_label": "90-88 | shared expenses | through partial 2026-04",
        "note": (
            "90 Madison reimburses 88 Madison for 88-paid shared operating "
            "expenses after the March 6, 2026 catch-up: all March plus the "
            "cash-funded portion of April. Internal transfer only; all parent "
            "and split rows remain Transfers Between Accounts. Remaining "
            "unpaid 90-to-88 balance after this transfer: $2,250.76."
        ),
        "components": [
            ["90-88 | 2026-03 | Morgan Linen Service", "289.82"],
            ["90-88 | 2026-03 | County Waste", "67.57"],
            ["90-88 | 2026-03 | software", "90.16"],
            ["90-88 | 2026-03 | streaming", "3.24"],
            ["90-88 | 2026-03 | shared supplies", "32.47"],
            ["90-88 | 2026-04 | Morgan Linen Service | partial", "282.81"],
            ["90-88 | 2026-04 | County Waste", "87.82"],
            ["90-88 | 2026-04 | software | partial", "42.36"],
            ["90-88 | 2026-04 | streaming | partial", "6.74"],
            ["90-88 | 2026-04 | shared supplies | partial", "20.49"],
        ],
    },
]


def graphql(payload: dict[str, Any]) -> dict[str, Any]:
    return run_graphql_via_cdp(
        payload,
        bridge_path=BRIDGE,
        workspace_root=ROOT,
        timeout=120,
    )


def cents(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(value.get("text") or "").split())
    return " ".join(str(value or "").split())


def balances() -> tuple[dict[int, Decimal], dict[int, str]]:
    rows = list_active_transfer_accounts(graphql)
    return (
        {
            int(row["transfer_account_id"]): cents(row["available_balance"])
            for row in rows
            if int(row["transfer_account_id"]) in set(ACCOUNT.values())
        },
        {
            int(row["transfer_account_id"]): str(row["bank_account_id"])
            for row in rows
            if int(row["transfer_account_id"]) in set(ACCOUNT.values())
        },
    )


def query_recent_rows(bank_account_id: str) -> list[dict[str, Any]]:
    response = graphql(
        {
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"field": "date", "direction": "DESC"},
                    "filter": {
                        "isHidden": False,
                        "search": "",
                        "isCategorized": None,
                        "tagId": TAG_TRANSFER,
                        "bankAccountId": bank_account_id,
                        "propertyId": None,
                        "unitId": None,
                        "isDeleted": False,
                        "isDocumentUploaded": None,
                    },
                    "page": 1,
                    "pageLimit": 300,
                }
            },
            "query": """
            query Transactions($input: SortsAndFilters) {
              transactions(input: $input) {
                data {
                  id amount date merchantName propertyId tagId bankAccountId
                  note isSplit parentId isDeleted
                }
              }
            }
            """,
        }
    )
    return list(response["data"]["transactions"]["data"])


def query_parent(parent_id: str) -> dict[str, Any]:
    response = graphql(
        {
            "operationName": "MadisonSharedExpenseParent",
            "variables": {"id": parent_id},
            "query": """
            query MadisonSharedExpenseParent($id: ID!) {
              transactionById(id: $id) {
                id amount date merchantName propertyId tagId bankAccountId note
                isSplit isDeleted parentId
                splitTransactions {
                  id amount date merchantName propertyId tagId parentId isDeleted
                }
              }
            }
            """,
        }
    )
    parent = response["data"]["transactionById"]
    if not parent:
        raise RuntimeError(f"Baselane transaction {parent_id} was not found")
    return parent


def active_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in parent.get("splitTransactions") or []
        if not row.get("isDeleted")
    ]


def normalized(rows: list[dict[str, Any]]) -> list[tuple[Decimal, str, str, str, str]]:
    return sorted(
        (
            cents(row.get("amount") or 0),
            str(row.get("date") or ""),
            str(row.get("merchantName") or ""),
            str(row.get("propertyId") or ""),
            str(row.get("tagId") or ""),
        )
        for row in rows
        if not row.get("isDeleted")
    )


def expected_children(parent: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    sign = Decimal("-1") if cents(parent["amount"]) < 0 else Decimal("1")
    rows = [
        {
            "amount": cents(sign * Decimal(amount)),
            "date": str(parent["date"]),
            "merchantName": label,
            "propertyId": str(spec["property_id"]),
            "tagId": TAG_TRANSFER,
        }
        for label, amount in spec["components"]
    ]
    if sum((row["amount"] for row in rows), Decimal("0")) != cents(parent["amount"]):
        raise RuntimeError(f"components do not sum to parent {parent['id']}")
    return rows


def update_parent_metadata(parent_ids: list[str], spec: dict[str, Any]) -> None:
    rows = graphql(
        {
            "operationName": "UpdateTransaction",
            "variables": {
                "input": [
                    {
                        "id": parent_id,
                        "merchantName": spec["parent_label"],
                        "propertyId": str(spec["property_id"]),
                        "tagId": TAG_TRANSFER,
                        "note": spec["note"],
                    }
                    for parent_id in parent_ids
                ]
            },
            "query": """
            mutation UpdateTransaction($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) {
                id merchantName propertyId tagId note
              }
            }
            """,
        }
    )["data"]["updateTransactions"]
    if len(rows) != len(parent_ids):
        raise RuntimeError("Baselane did not update both mirrored transfer parents")


def reconcile_split(parent: dict[str, Any], target: list[dict[str, Any]]) -> None:
    existing = active_children(parent)
    by_key: dict[tuple[Decimal, str, str, str, str], list[dict[str, Any]]] = {}
    for child in existing:
        by_key.setdefault(normalized([child])[0], []).append(child)
    kept: set[str] = set()
    inputs: list[dict[str, Any]] = []
    for target_row in target:
        candidates = sorted(
            by_key.get(normalized([target_row])[0], []),
            key=lambda row: int(str(row.get("id") or "0")),
            reverse=True,
        )
        row = dict(target_row)
        if candidates:
            row["id"] = str(candidates[0]["id"])
            kept.add(str(candidates[0]["id"]))
        inputs.append(row)
    for child in existing:
        child_id = str(child.get("id") or "")
        if child_id in kept:
            continue
        inputs.append(
            {
                "id": child_id,
                "amount": cents(child.get("amount") or 0),
                "date": str(child.get("date") or parent["date"]),
                "merchantName": str(child.get("merchantName") or ""),
                "propertyId": str(child.get("propertyId") or ""),
                "tagId": str(child.get("tagId") or TAG_TRANSFER),
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


def ensure_native_audit(
    spec: dict[str, Any],
    plan: dict[str, Any],
    bank_ids: dict[int, str],
) -> dict[str, Any]:
    expected_banks = {bank_ids[int(spec["from"])], bank_ids[int(spec["to"])]}
    exact: list[dict[str, Any]] = []
    for attempt in range(5):
        rows = [
            row
            for bank_id in sorted(expected_banks)
            for row in query_recent_rows(bank_id)
        ]
        exact = [
            row
            for row in rows
            if (
                not row.get("parentId")
                and not row.get("isDeleted")
                and str(row.get("bankAccountId") or "") in expected_banks
                and str(row.get("date") or "") == str(plan["transfer_date"])
                and abs(cents(row.get("amount") or 0)) == Decimal(plan["amount"])
                and note_text(row.get("note"))
                in {note_text(plan["bookkeeping_note"]), note_text(spec["note"])}
            )
        ]
        if len(exact) == 2 and {
            str(row.get("bankAccountId")) for row in exact
        } == expected_banks:
            break
        if attempt < 4:
            time.sleep(2)
    if len(exact) != 2:
        return {
            "status": "pending_bank_mirrors",
            "matched_parent_ids": [str(row.get("id")) for row in exact],
        }
    parent_ids = [str(row["id"]) for row in exact]
    update_parent_metadata(parent_ids, spec)
    for parent_id in parent_ids:
        parent = query_parent(parent_id)
        target = expected_children(parent, spec)
        if normalized(active_children(parent)) != normalized(target):
            reconcile_split(parent, target)
        verified = query_parent(parent_id)
        if normalized(active_children(verified)) != normalized(target):
            raise RuntimeError(f"native split verification failed for {parent_id}")
    return {
        "status": "verified_native_splits",
        "parent_ids": parent_ids,
        "component_count_per_parent": len(spec["components"]),
        "all_rows_tag_id": TAG_TRANSFER,
    }


def build_public(
    selected: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[int, str]]:
    opening, bank_ids = balances()
    issues: list[str] = []
    if set(opening) != set(ACCOUNT.values()):
        issues.append("one or more Madison internal accounts are unavailable")
    running = dict(opening)
    plans: list[dict[str, Any]] = []
    for spec in SPECS:
        plan = build_transfer_plan(
            from_transfer_account_id=spec["from"],
            to_transfer_account_id=spec["to"],
            amount=spec["amount"],
            bookkeeping_note=spec["bookkeeping_note"],
            property_id=spec["property_id"],
            tag_id=24,
            same_day=True,
        )
        plans.append(plan)
        if spec["key"] not in selected:
            continue
        amount = Decimal(plan["amount"])
        if running.get(spec["from"], Decimal("-1")) < amount:
            issues.append(f"{spec['key']} exceeds source available balance")
        running[spec["from"]] = running.get(spec["from"], Decimal("0")) - amount
        running[spec["to"]] = running.get(spec["to"], Decimal("0")) + amount
    for dao in ("86", "90"):
        if running.get(ACCOUNT[dao], Decimal("-1")) < FLOAT:
            issues.append(f"{dao} would end below the $500 float")
    public = {
        "scope": "86/90 Madison reimbursement to 88 for 88-paid shared expenses",
        "selected_legs": sorted(selected),
        "cash_flow_invariant": {
            "tag_id": TAG_TRANSFER,
            "category": "Transfers Between Accounts",
            "internal_only": True,
            "native_splits_on_both_mirrors": True,
            "component_labels_are_audit_only": True,
        },
        "issues": issues,
        "opening_available_balances": {
            key: format(opening.get(value, Decimal("0")), ".2f")
            for key, value in ACCOUNT.items()
        },
        "forecast_available_balances": {
            key: format(running.get(value, Decimal("0")), ".2f")
            for key, value in ACCOUNT.items()
        },
        "transfers": [
            {
                "key": spec["key"],
                "direction": spec["key"].replace("_to_", " -> "),
                "amount": spec["amount"],
                "property_id": spec["property_id"],
                "tag_id": TAG_TRANSFER,
                "parent_label": spec["parent_label"],
                "bookkeeping_note": spec["bookkeeping_note"],
                "note": spec["note"],
                "components": [
                    {"label": label, "amount": amount, "tag_id": TAG_TRANSFER}
                    for label, amount in spec["components"]
                ],
                "confirmation_token": plan["confirmation_token"],
            }
            for spec, plan in zip(SPECS, plans)
            if spec["key"] in selected
        ],
    }
    return public, plans, bank_ids


def digest(public: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_report(name: str, payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--digest")
    parser.add_argument(
        "--only",
        action="append",
        choices=[spec["key"] for spec in SPECS],
    )
    args = parser.parse_args()
    if args.apply and args.audit_only:
        parser.error("--apply and --audit-only are mutually exclusive")

    selected = set(args.only or [spec["key"] for spec in SPECS])
    public, plans, bank_ids = build_public(selected)
    plan_digest = digest(public)
    dry = {"status": "dry_run", "digest": plan_digest, **public}
    dry_path = write_report("madison_shared_expense_settlement_dry_run.json", dry)

    if args.audit_only:
        audits = [
            {
                "key": spec["key"],
                **ensure_native_audit(spec, plan, bank_ids),
            }
            for spec, plan in zip(SPECS, plans)
            if spec["key"] in selected
        ]
        payload = {"status": "existing_cash_rows_audited", "audits": audits}
        path = write_report(
            "madison_shared_expense_settlement_audit_only.json", payload
        )
        print(json.dumps({**payload, "report": str(path)}, indent=2))
        return 0

    if not args.apply:
        print(json.dumps({**dry, "report": str(dry_path)}, indent=2))
        return 0 if not public["issues"] else 2
    if args.digest != plan_digest:
        raise RuntimeError(f"live digest is {plan_digest}; exact --digest required")
    if public["issues"]:
        raise RuntimeError(f"refusing apply with issues: {public['issues']}")

    receipts: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    for spec, plan in zip(SPECS, plans):
        if spec["key"] not in selected:
            continue
        result = execute_transfer(
            plan=plan,
            confirmation_token=plan["confirmation_token"],
            graphql_runner=graphql,
            state_path=STATE_PATH,
        )
        receipts.append({"key": spec["key"], **result})
        audits.append(
            {
                "key": spec["key"],
                **ensure_native_audit(spec, plan, bank_ids),
            }
        )

    ending, _bank_ids = balances()
    for dao in ("86", "90"):
        if ending.get(ACCOUNT[dao], Decimal("-1")) < FLOAT:
            raise RuntimeError(f"post-transfer {dao} is below its $500 float")
    payload = {
        "status": "submitted_and_verified",
        "digest": plan_digest,
        **public,
        "ending_available_balances": {
            key: format(ending.get(value, Decimal("0")), ".2f")
            for key, value in ACCOUNT.items()
        },
        "receipts": receipts,
        "audits": audits,
    }
    path = write_report("madison_shared_expense_settlement_applied.json", payload)
    print(json.dumps({**payload, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
