#!/usr/bin/env python3
"""Idempotently settle Sara principal already collected by ECO into EARLDAO.

The cash was included in ECO Reserves -> EARLDAO's existing 2026-07-11
$16,000 internal transfer.  This workflow relabels the four historical Sara
payments and decomposes both mirrors of that existing transfer into:

* four Sara principal components totaling $3,000; and
* a separate $13,000 ECO bridge advance to EARLDAO.

No new cash transfer is created.  All bridge-transfer parents and children
remain category 24 so downstream cash-flow statements exclude them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from baselane_settle_madison_pm_mortgage import (
    cents,
    graphql,
    normalized_children,
    query_parent,
    reconcile_parent_split,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports"
SARA_PROPERTY_ID = "83188"
EARLDAO_PROPERTY_ID = "94512"
TAG_PRINCIPAL = "22"
TAG_TRANSFER = "24"

SARA_COMPONENTS = [
    {
        "date": "2025-10-04",
        "amount": Decimal("105.75"),
        "post_principal": Decimal("12894.25"),
        "sara_id": "196963147",
        "eco_id": "196963150",
    },
    {
        "date": "2025-11-09",
        "amount": Decimal("894.25"),
        "post_principal": Decimal("12000.00"),
        "sara_id": "208475919",
        "eco_id": "208475921",
    },
    {
        "date": "2025-12-08",
        "amount": Decimal("1000.00"),
        "post_principal": Decimal("11000.00"),
        "sara_id": "218128680",
        "eco_id": "218128688",
    },
    {
        "date": "2026-01-06",
        "amount": Decimal("1000.00"),
        "post_principal": Decimal("10000.00"),
        "sara_id": "227790655",
        "eco_id": "227790659",
    },
]

BRIDGE_PARENTS = {
    "312564386": {
        "amount": Decimal("-16000.00"),
        "date": "2026-07-11",
        "bank_account_id": "116478",
        "side": "ECO Reserves",
    },
    "312564388": {
        "amount": Decimal("16000.00"),
        "date": "2026-07-11",
        "bank_account_id": "131545",
        "side": "EARLDAO",
    },
}


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "id",
            "amount",
            "date",
            "merchantName",
            "propertyId",
            "tagId",
            "bankAccountId",
            "isSplit",
            "parentId",
            "isDeleted",
        )
    }


def query_rows() -> dict[str, dict[str, Any]]:
    ids = [
        value
        for component in SARA_COMPONENTS
        for value in (component["sara_id"], component["eco_id"])
    ] + list(BRIDGE_PARENTS)
    rows: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(ids), 5):
        batch = ids[offset : offset + 5]
        fields = "\n".join(
            f"""
            t{index}: transactionById(id: "{transaction_id}") {{
              id amount date merchantName propertyId tagId bankAccountId note
              isManual hidden isDeleted isSplit parentId
              splitTransactions {{
                id amount date merchantName propertyId tagId parentId isDeleted
              }}
            }}
            """
            for index, transaction_id in enumerate(batch)
        )
        data = graphql(
            {
                "operationName": "SaraEarldaoSettlementRows",
                "variables": {},
                "query": f"query SaraEarldaoSettlementRows {{ {fields} }}",
            }
        )["data"]
        for row in data.values():
            if row:
                rows[str(row["id"])] = row
    return rows


def validate(rows: dict[str, dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for component in SARA_COMPONENTS:
        for key, expected_amount, expected_bank in (
            ("sara_id", -component["amount"], "123831"),
            ("eco_id", component["amount"], "38968"),
        ):
            transaction_id = component[key]
            row = rows.get(transaction_id)
            if not row:
                issues.append(f"missing transaction {transaction_id}")
                continue
            expected = (
                cents(expected_amount),
                component["date"],
                expected_bank,
                False,
                None,
            )
            actual = (
                cents(row.get("amount") or 0),
                str(row.get("date") or ""),
                str(row.get("bankAccountId") or ""),
                bool(row.get("isDeleted")),
                row.get("parentId"),
            )
            if actual != expected:
                issues.append(
                    f"transaction {transaction_id} changed: "
                    f"expected {expected}, found {actual}"
                )

    for transaction_id, expected in BRIDGE_PARENTS.items():
        row = rows.get(transaction_id)
        if not row:
            issues.append(f"missing bridge transaction {transaction_id}")
            continue
        actual = (
            cents(row.get("amount") or 0),
            str(row.get("date") or ""),
            str(row.get("bankAccountId") or ""),
            str(row.get("tagId") or ""),
            bool(row.get("isDeleted")),
            row.get("parentId"),
        )
        wanted = (
            expected["amount"],
            expected["date"],
            expected["bank_account_id"],
            TAG_TRANSFER,
            False,
            None,
        )
        if actual != wanted:
            issues.append(
                f"bridge transaction {transaction_id} changed: "
                f"expected {wanted}, found {actual}"
            )
    return issues


def desired_historical_metadata() -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    for component in SARA_COMPONENTS:
        amount = format(component["amount"], ".2f")
        post = format(component["post_principal"], ".2f")
        updates.append(
            {
                "id": component["sara_id"],
                "merchantName": (
                    f"Sara -> EARLDAO principal | {component['date']} | ${amount}"
                ),
                "propertyId": SARA_PROPERTY_ID,
                "tagId": TAG_PRINCIPAL,
                "note": (
                    f"1432 Sara Ave EARLDAO loan principal payment collected by "
                    f"ECO as agent; payment ${amount}; post-payment principal "
                    f"${post}. No duplicate principal row."
                ),
            }
        )
        updates.append(
            {
                "id": component["eco_id"],
                "merchantName": (
                    f"ECO agent receipt for EARLDAO | Sara principal | "
                    f"{component['date']} | ${amount}"
                ),
                "propertyId": SARA_PROPERTY_ID,
                "tagId": TAG_TRANSFER,
                "note": (
                    f"EARLDAO principal collected by ECO as agent from 1432 Sara "
                    f"Ave; payment ${amount}; post-payment principal ${post}; "
                    f"liability to EARLDAO, not ECO revenue."
                ),
            }
        )
    return updates


def desired_bridge_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    sign = Decimal("-1") if cents(parent["amount"]) < 0 else Decimal("1")
    children = [
        {
            "amount": cents(sign * component["amount"]),
            "date": str(parent["date"]),
            "merchantName": (
                f"ECO-EARLDAO | Sara principal collected "
                f"{component['date']} | ${component['amount']:.2f}"
            ),
            "propertyId": SARA_PROPERTY_ID,
            "tagId": TAG_TRANSFER,
        }
        for component in SARA_COMPONENTS
    ]
    children.append(
        {
            "amount": cents(sign * Decimal("13000.00")),
            "date": str(parent["date"]),
            "merchantName": (
                "ECO-EARLDAO | ECO bridge advance | 2026-07-11 | $13,000"
            ),
            "propertyId": EARLDAO_PROPERTY_ID,
            "tagId": TAG_TRANSFER,
        }
    )
    if sum((row["amount"] for row in children), Decimal("0")) != cents(
        parent["amount"]
    ):
        raise RuntimeError("bridge components do not sum to the parent")
    return children


def update_rows(updates: list[dict[str, Any]]) -> None:
    response = graphql(
        {
            "operationName": "UpdateTransaction",
            "variables": {"input": updates},
            "query": """
            mutation UpdateTransaction($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) {
                id merchantName propertyId tagId note
              }
            }
            """,
        }
    )
    returned = response["data"]["updateTransactions"]
    if {str(row["id"]) for row in returned} != {
        str(row["id"]) for row in updates
    }:
        raise RuntimeError("Baselane did not confirm every metadata update")


def bridge_parent_updates() -> list[dict[str, Any]]:
    return [
        {
            "id": transaction_id,
            "merchantName": (
                "ECO-EARLDAO | Sara principal settlement + ECO bridge | "
                "2026-07-11"
            ),
            "propertyId": EARLDAO_PROPERTY_ID,
            "tagId": TAG_TRANSFER,
            "note": (
                "Existing $16,000 internal transfer decomposed into $3,000 of "
                "Sara principal already collected by ECO as agent and a separate "
                "$13,000 ECO bridge advance to EARLDAO. No new cash movement."
            ),
        }
        for transaction_id in BRIDGE_PARENTS
    ]


def build_public(rows: dict[str, dict[str, Any]], issues: list[str]) -> dict[str, Any]:
    historical = desired_historical_metadata()
    return {
        "scope": "1432 Sara principal -> EARLDAO existing-cash settlement",
        "status": "dry_run",
        "issues": issues,
        "cash_movement_created": False,
        "accounting_invariants": {
            "sara_principal_total": "3000.00",
            "eco_bridge_total": "13000.00",
            "existing_transfer_total": "16000.00",
            "historical_sara_category": {
                "id": TAG_PRINCIPAL,
                "name": "Other Loan Principal Payments",
            },
            "all_eco_and_earldao_transfer_rows_category": {
                "id": TAG_TRANSFER,
                "name": "Transfers Between Accounts",
            },
            "eco_agent_receipts_are_not_revenue": True,
            "no_duplicate_sara_principal_rows": True,
        },
        "source_rows": [
            public_row(rows[transaction_id])
            for transaction_id in sorted(rows, key=int)
        ],
        "historical_updates": historical,
        "existing_bridge_parent_updates": bridge_parent_updates(),
        "existing_bridge_components": [
            {
                "label": (
                    f"Sara principal collected {component['date']}"
                ),
                "amount": format(component["amount"], ".2f"),
                "property_id": SARA_PROPERTY_ID,
                "tag_id": TAG_TRANSFER,
            }
            for component in SARA_COMPONENTS
        ]
        + [
            {
                "label": "ECO bridge advance to EARLDAO",
                "amount": "13000.00",
                "property_id": EARLDAO_PROPERTY_ID,
                "tag_id": TAG_TRANSFER,
            }
        ],
    }


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_report(filename: str, payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / filename
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def verify() -> dict[str, Any]:
    rows = query_rows()
    issues = validate(rows)
    expected_updates = {
        str(row["id"]): row
        for row in desired_historical_metadata() + bridge_parent_updates()
    }
    for transaction_id, expected in expected_updates.items():
        actual = rows.get(transaction_id)
        if not actual:
            continue
        for field in ("merchantName", "propertyId", "tagId"):
            if str(actual.get(field) or "") != str(expected[field]):
                issues.append(
                    f"{transaction_id} {field} is not settled: "
                    f"{actual.get(field)!r}"
                )
    for transaction_id in BRIDGE_PARENTS:
        parent = rows.get(transaction_id)
        if not parent:
            continue
        target = desired_bridge_children(parent)
        if normalized_children(
            parent.get("splitTransactions") or []
        ) != normalized_children(target):
            issues.append(
                f"{transaction_id} native split does not match settlement"
            )
    return {
        "status": "verified" if not issues else "verification_failed",
        "issues": issues,
        "cash_movement_created": False,
        "rows": [public_row(row) for row in rows.values()],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    if args.verify:
        result = verify()
        path = write_report(
            "sara_earldao_principal_settlement_verify.json", result
        )
        print(json.dumps({**result, "report": str(path)}, indent=2))
        return 0 if result["status"] == "verified" else 2

    rows = query_rows()
    issues = validate(rows)
    public = build_public(rows, issues)
    plan_digest = digest(public)
    dry_run = {"digest": plan_digest, **public}
    dry_path = write_report(
        "sara_earldao_principal_settlement_dry_run.json", dry_run
    )
    if not args.apply:
        print(json.dumps({**dry_run, "report": str(dry_path)}, indent=2))
        return 0 if not issues else 2
    if args.digest != plan_digest:
        raise RuntimeError(f"live digest is {plan_digest}; exact --digest required")
    if issues:
        raise RuntimeError(f"refusing apply with issues: {issues}")

    update_rows(desired_historical_metadata())
    update_rows(bridge_parent_updates())
    for transaction_id in BRIDGE_PARENTS:
        parent = query_parent(transaction_id)
        target = desired_bridge_children(parent)
        if normalized_children(
            parent.get("splitTransactions") or []
        ) != normalized_children(target):
            reconcile_parent_split(parent, target)

    verified = verify()
    if verified["status"] != "verified":
        raise RuntimeError(
            f"post-apply verification failed: {verified['issues']}"
        )
    applied = {
        **public,
        "status": "applied_and_verified",
        "digest": plan_digest,
        "verification": verified,
    }
    path = write_report(
        "sara_earldao_principal_settlement_applied.json", applied
    )
    print(json.dumps({**applied, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
