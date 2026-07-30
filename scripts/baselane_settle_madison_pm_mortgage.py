#!/usr/bin/env python3
"""Guarded ECO <-> 86/88/90 Madison PM/mortgage cash settlement."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_mcp.transfers import (  # noqa: E402
    build_transfer_plan,
    execute_transfer,
    list_active_transfer_accounts,
    run_graphql_via_cdp,
)


BRIDGE = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
REPORT_DIR = ROOT / "reports"
STATE_PATH = REPORT_DIR / "baselane_transfer_state.json"
FLOAT = Decimal("500.00")
CENT = Decimal("0.01")

ACCOUNT = {
    "ECO": 29732,
    "86": 70125,
    "88": 70936,
    "90": 70935,
}

SPECS = [
    {
        "key": "88_to_ECO",
        "from": ACCOUNT["88"],
        "to": ACCOUNT["ECO"],
        "amount": "5450.97",
        "property_id": "31499",
        "bookkeeping_note": (
            "Madison 88 ECO mortgage through July 2026 PM through June 2026"
        ),
        "note": (
            "88 Madison: mortgage net thru Jul26 $1,887.25 = $1,082.78 duplicate "
            "P&I net + $847.79 escrow overfund - $43.32 ECO late fee; PM cash thru "
            "Jun26 $3,563.72. Total $5,450.97. Unpaid PM accrual $7,191.62 remains. "
            "Internal only."
        ),
        "parent_label": "88-ECO | mortgage thru Jul + PM thru Jun | settlement",
        "components": [
            [
                "88-ECO | P&I duplicate less 2026-07 unpaid P&I | net",
                "1082.78",
            ],
            ["88-ECO | escrow overfund refund | 2025-01", "247.25"],
            ["88-ECO | escrow overfund refund | 2025-03", "200.18"],
            ["88-ECO | escrow overfund refund | 2025-04", "200.18"],
            [
                "88-ECO | 2025-05 escrow less 2025-08 ECO late fee",
                "156.86",
            ],
            ["88-ECO | PM cash | 2025-09 | remaining", "870.97"],
            ["88-ECO | PM cash | 2026-03", "2344.53"],
            ["88-ECO | PM cash | 2026-04 | partial", "348.22"],
        ],
    },
    {
        "key": "90_to_ECO",
        "from": ACCOUNT["90"],
        "to": ACCOUNT["ECO"],
        "amount": "4895.31",
        "property_id": "31525",
        "bookkeeping_note": (
            "Madison 90 ECO PM through June 2026 mortgage through July 2026"
        ),
        "note": (
            "90 Madison: PM thru Jun26 $9,658.30 - ECO mortgage thru Jul26 "
            "$4,762.99 = $4,895.31. Mortgage: $3,538.42 P&I gap + $1,450 extra "
            "principal + $35.58 ECO fee - $261.01 escrow. Dec24 $950 curtailment "
            "had matching ECO advance. Internal only."
        ),
        "parent_label": "90-ECO | PM thru Jun less mortgage thru Jul | residual",
        "components": [
            [
                "90-ECO | PM cash | 2026-04 | remaining after mortgage offset",
                "755.28",
            ],
            ["90-ECO | PM cash | 2026-05", "1964.07"],
            ["90-ECO | PM cash | 2026-06", "2175.96"],
        ],
    },
    {
        "key": "ECO_to_86",
        "from": ACCOUNT["ECO"],
        "to": ACCOUNT["86"],
        "amount": "357.31",
        "property_id": "63162",
        "bookkeeping_note": (
            "ECO Madison 86 mortgage through July 2026 PM through June 2026"
        ),
        "note": (
            "86 Madison: ECO mortgage thru Jul26 $4,908.42 - PM thru Jun26 "
            "$4,551.11 = $357.31 to 86. Mortgage: $21,013.38 P&I - $12,841.51 "
            "labeled reimbursements - $2,700 support - $563.45 escrow overfund. "
            "Jul26 P&I residual. Internal only."
        ),
        "parent_label": "ECO-86 | mortgage thru Jul less PM thru Jun | residual",
        "components": [
            [
                "ECO-86 | mortgage P&I | 2026-07 | remaining after PM offset",
                "357.31",
            ],
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


def balances() -> dict[int, Decimal]:
    return {
        int(row["transfer_account_id"]): Decimal(str(row["available_balance"]))
        for row in list_active_transfer_accounts(graphql)
        if int(row["transfer_account_id"]) in set(ACCOUNT.values())
    }


def cents(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(value.get("text") or "").split())
    return " ".join(str(value or "").split())


def query_recent_transfer_rows(bank_account_id: str | None = None) -> list[dict[str, Any]]:
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
                        "tagId": "24",
                        "bankAccountId": bank_account_id,
                        "propertyId": None,
                        "unitId": None,
                        "isDeleted": False,
                        "isDocumentUploaded": None,
                    },
                    "page": 1,
                    "pageLimit": 250,
                }
            },
            "query": """
            query Transactions($input: SortsAndFilters) {
              transactions(input: $input) {
                total
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
            "operationName": "MadisonSettlementParent",
            "variables": {"id": parent_id},
            "query": """
            query MadisonSettlementParent($id: ID!) {
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


def update_parent_metadata(
    parent_ids: list[str],
    *,
    label: str,
    property_id: str,
    note: str,
) -> None:
    response = graphql(
        {
            "operationName": "UpdateTransaction",
            "variables": {
                "input": [
                    {
                        "id": parent_id,
                        "merchantName": label,
                        "propertyId": property_id,
                        "tagId": "24",
                        "note": note,
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
    )
    rows = response["data"]["updateTransactions"]
    if len(rows) != len(parent_ids):
        raise RuntimeError("Baselane did not update both mirrored transfer parents")


def normalized_children(rows: list[dict[str, Any]]) -> list[tuple[Decimal, str, str, str, str]]:
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


def expected_children(
    parent: dict[str, Any],
    *,
    property_id: str,
    components: list[list[str]],
) -> list[dict[str, Any]]:
    parent_amount = cents(parent["amount"])
    sign = Decimal("-1") if parent_amount < 0 else Decimal("1")
    rows = [
        {
            "amount": cents(sign * Decimal(amount)),
            "date": str(parent["date"]),
            "merchantName": label,
            "propertyId": property_id,
            "tagId": "24",
        }
        for label, amount in components
    ]
    if sum((row["amount"] for row in rows), Decimal("0")) != parent_amount:
        raise RuntimeError(
            f"native split components do not sum to parent {parent['id']}"
        )
    return rows


def reconcile_parent_split(
    parent: dict[str, Any],
    target: list[dict[str, Any]],
) -> dict[str, Any]:
    existing = [
        row
        for row in parent.get("splitTransactions") or []
        if not row.get("isDeleted")
    ]
    by_key: dict[tuple[Decimal, str, str, str, str], list[dict[str, Any]]] = {}
    for child in existing:
        by_key.setdefault(normalized_children([child])[0], []).append(child)

    inputs: list[dict[str, Any]] = []
    kept: set[str] = set()
    for target_row in target:
        key = normalized_children([target_row])[0]
        candidates = sorted(
            by_key.get(key, []),
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
                "tagId": str(child.get("tagId") or "24"),
                "isDelete": True,
            }
        )

    response = graphql(
        {
            "operationName": "createOrUpdateSplitTx",
            "variables": {
                "parentTransactionId": str(parent["id"]),
                "splitType": "AMOUNT",
                "transactionSplitInputs": [
                    {
                        **row,
                        "amount": float(row["amount"]),
                        "propertyUnitId": None,
                    }
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
                splitTransactions {
                  id amount date merchantName propertyId tagId parentId isDeleted
                }
              }
            }
            """,
        }
    )
    return response["data"]["createOrUpdateSplitTx"]


def ensure_native_audit(
    spec: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    accounts = {
        int(row["transfer_account_id"]): str(row["bank_account_id"])
        for row in list_active_transfer_accounts(graphql)
    }
    expected_bank_ids = {
        accounts[int(spec["from"])],
        accounts[int(spec["to"])],
    }
    exact: list[dict[str, Any]] = []
    for attempt in range(4):
        # Query each bank mirror separately. A workspace-wide page can be
        # saturated by recent split children before both parents appear.
        rows = [
            row
            for bank_account_id in sorted(expected_bank_ids)
            for row in query_recent_transfer_rows(bank_account_id)
        ]
        exact = [
            row
            for row in rows
            if (
                not row.get("isDeleted")
                and not row.get("parentId")
                and str(row.get("bankAccountId") or "") in expected_bank_ids
                and str(row.get("date") or "") == str(plan["transfer_date"])
                and abs(cents(row.get("amount") or 0)) == Decimal(plan["amount"])
                and note_text(row.get("note"))
                in {
                    note_text(plan["bookkeeping_note"]),
                    note_text(spec["note"]),
                }
            )
        ]
        matched_banks = {str(row.get("bankAccountId")) for row in exact}
        component_labels = {str(label) for label, _amount in spec["components"]}
        for bank_account_id in expected_bank_ids - matched_banks:
            candidate_parent_ids = {
                str(row.get("parentId"))
                for row in rows
                if (
                    str(row.get("bankAccountId") or "") == bank_account_id
                    and str(row.get("date") or "") == str(plan["transfer_date"])
                    and str(row.get("merchantName") or "") in component_labels
                    and row.get("parentId")
                    and not row.get("isDeleted")
                )
            }
            for parent_id in sorted(candidate_parent_ids):
                parent = query_parent(parent_id)
                if (
                    str(parent.get("bankAccountId") or "") == bank_account_id
                    and str(parent.get("date") or "") == str(plan["transfer_date"])
                    and abs(cents(parent.get("amount") or 0))
                    == Decimal(plan["amount"])
                    and normalized_children(parent.get("splitTransactions") or [])
                    == normalized_children(
                        expected_children(
                            parent,
                            property_id=str(spec["property_id"]),
                            components=spec["components"],
                        )
                    )
                ):
                    exact.append(parent)
                    break
        if len(exact) == 2 and {
            str(row.get("bankAccountId")) for row in exact
        } == expected_bank_ids:
            break
        if attempt < 3:
            time.sleep(2)
    if len(exact) != 2 or {
        str(row.get("bankAccountId")) for row in exact
    } != expected_bank_ids:
        return {
            "status": "pending_bank_mirrors",
            "matched_parent_ids": [str(row.get("id")) for row in exact],
        }

    parent_ids = [str(row["id"]) for row in exact]
    update_parent_metadata(
        parent_ids,
        label=spec["parent_label"],
        property_id=str(spec["property_id"]),
        note=spec["note"],
    )

    # A one-component residual is already fully represented by its parent.
    # Baselane-native splitting is material only when two or more components
    # need to remain independently auditable.
    if len(spec["components"]) == 1:
        return {
            "status": "verified_single_component_parents",
            "parent_ids": parent_ids,
            "all_rows_tag_id": "24",
        }

    split_ids: list[str] = []
    for parent_id in parent_ids:
        parent = query_parent(parent_id)
        target = expected_children(
            parent,
            property_id=str(spec["property_id"]),
            components=spec["components"],
        )
        if normalized_children(parent.get("splitTransactions") or []) != normalized_children(target):
            reconcile_parent_split(parent, target)
        verified = query_parent(parent_id)
        if normalized_children(verified.get("splitTransactions") or []) != normalized_children(target):
            raise RuntimeError(
                f"native transfer split verification failed for {parent_id}"
            )
        split_ids.append(parent_id)
    return {
        "status": "verified_native_splits",
        "parent_ids": split_ids,
        "component_count_per_parent": len(spec["components"]),
        "all_rows_tag_id": "24",
    }


def build_public(
    selected_keys: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    opening = balances()
    issues: list[str] = []
    if set(opening) != set(ACCOUNT.values()):
        issues.append("one or more Madison/ECO internal accounts are unavailable")

    plans: list[dict[str, Any]] = []
    running = dict(opening)
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
        if spec["key"] not in selected_keys:
            continue
        amount = Decimal(plan["amount"])
        if running.get(spec["from"], Decimal("-1")) < amount:
            issues.append(f"{spec['key']} exceeds source available balance")
        running[spec["from"]] = running.get(spec["from"], Decimal("0")) - amount
        running[spec["to"]] = running.get(spec["to"], Decimal("0")) + amount

    for dao in ("86", "88", "90"):
        if running.get(ACCOUNT[dao], Decimal("-1")) < FLOAT:
            issues.append(f"{dao} would end below the $500 operations float")

    public = {
        "scope": "ECO <-> 86/88/90 Madison PM fee and mortgage cash settlement",
        "selected_legs": sorted(selected_keys),
        "cash_flow_invariant": {
            "tag_id": "24",
            "category": "Transfers Between Accounts",
            "internal_only": True,
            "component_labels_are_audit_only": True,
            "native_split_policy": (
                "Create Baselane-native splits on both bank mirrors when a "
                "transfer has multiple components; leave single-component "
                "residuals as labeled parents."
            ),
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
                "tag_id": "24",
                "parent_label": spec["parent_label"],
                "bookkeeping_note": spec["bookkeeping_note"],
                "note": spec["note"],
                "components": [
                    {"label": label, "amount": amount, "tag_id": "24"}
                    for label, amount in spec["components"]
                ],
                "confirmation_token": plan["confirmation_token"],
            }
            for spec, plan in zip(SPECS, plans)
        ],
    }
    return public, plans


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
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help=(
            "Reconcile labels/native splits on an already-created transfer "
            "without submitting cash."
        ),
    )
    parser.add_argument("--digest")
    parser.add_argument(
        "--only",
        action="append",
        choices=[spec["key"] for spec in SPECS],
        help=(
            "Submit only the named exact-plan leg. May be repeated. "
            "The digest still confirms the complete Madison plan."
        ),
    )
    args = parser.parse_args()
    if args.apply and args.audit_only:
        parser.error("--apply and --audit-only are mutually exclusive")

    selected_keys = set(args.only or [spec["key"] for spec in SPECS])
    public, plans = build_public(selected_keys)
    plan_digest = digest(public)
    dry = {"status": "dry_run", "digest": plan_digest, **public}
    dry_path = write_report("madison_pm_mortgage_settlement_dry_run.json", dry)
    if args.audit_only:
        audit_rows = []
        for spec, plan in zip(SPECS, plans):
            if spec["key"] not in selected_keys:
                continue
            audit_rows.append(
                {
                    "key": spec["key"],
                    **ensure_native_audit(spec, plan),
                }
            )
        payload = {
            "status": "existing_cash_rows_audited",
            "selected_legs": sorted(selected_keys),
            "native_audit_rows": audit_rows,
        }
        report = write_report(
            "madison_pm_mortgage_settlement_audit_only.json",
            payload,
        )
        print(json.dumps({**payload, "report": str(report)}, indent=2))
        return 0
    if not args.apply:
        print(json.dumps({**dry, "report": str(dry_path)}, indent=2))
        return 0 if not public["issues"] else 2
    if args.digest != plan_digest:
        raise RuntimeError(f"live digest is {plan_digest}; exact --digest required")
    if public["issues"]:
        raise RuntimeError(f"refusing apply with issues: {public['issues']}")

    receipts = []
    audit_rows = []
    for spec, plan in zip(SPECS, plans):
        if spec["key"] not in selected_keys:
            continue
        result = execute_transfer(
            plan=plan,
            confirmation_token=plan["confirmation_token"],
            graphql_runner=graphql,
            state_path=STATE_PATH,
        )
        receipts.append({"key": spec["key"], **result})
        audit_rows.append(
            {
                "key": spec["key"],
                **ensure_native_audit(spec, plan),
            }
        )

    ending = balances()
    verified = {
        key: format(ending.get(value, Decimal("0")), ".2f")
        for key, value in ACCOUNT.items()
    }
    for dao in ("86", "88", "90"):
        if ending.get(ACCOUNT[dao], Decimal("-1")) < FLOAT:
            raise RuntimeError(f"post-transfer {dao} available balance is below $500")

    applied = {
        "status": (
            "submitted_and_verified"
            if len(selected_keys) == len(SPECS)
            else "selected_legs_submitted_and_verified"
        ),
        "digest": plan_digest,
        **public,
        "selected_legs": sorted(selected_keys),
        "ending_available_balances": verified,
        "receipts": receipts,
        "native_audit_rows": audit_rows,
    }
    report_name = (
        "madison_pm_mortgage_settlement_applied.json"
        if len(selected_keys) == len(SPECS)
        else "madison_pm_mortgage_settlement_partial_apply.json"
    )
    path = write_report(report_name, applied)
    print(json.dumps({**applied, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
