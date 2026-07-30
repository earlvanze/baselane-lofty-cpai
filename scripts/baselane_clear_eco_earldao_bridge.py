#!/usr/bin/env python3
"""Clear the July 2026 ECO/EARLDAO bridge without inventing a DAO allocation.

The existing 2026-07-11 ECO -> EARLDAO transfer contains a verified $13,000
ECO bridge advance. EARLDAO returned $10,000 on 2026-07-17 and another
$10,000 on 2026-07-24. This workflow:

* labels the 2026-07-17 transfer as the first $10,000 bridge repayment; and
* splits the 2026-07-24 transfer into the final $3,000 bridge repayment and a
  separate $7,000 EARLDAO-funded ECO clearing advance pending DAO allocation.

All rows remain category 24 (Transfers Between Accounts). No cash is moved.
The $7,000 component is intentionally not assigned to a property until
transaction-level support identifies the benefiting DAO(s).
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
EARLDAO_PROPERTY_ID = "94512"
TRANSFER_TAG_ID = "24"

ROWS = {
    "316163372": {
        "amount": Decimal("-10000.00"),
        "date": "2026-07-17",
        "bank_account_id": "131545",
        "side": "EARLDAO",
        "tranche": "first",
    },
    "316163381": {
        "amount": Decimal("10000.00"),
        "date": "2026-07-17",
        "bank_account_id": "38968",
        "side": "ECO",
        "tranche": "first",
    },
    "319772383": {
        "amount": Decimal("-10000.00"),
        "date": "2026-07-24",
        "bank_account_id": "131545",
        "side": "EARLDAO",
        "tranche": "second",
    },
    "319772392": {
        "amount": Decimal("10000.00"),
        "date": "2026-07-24",
        "bank_account_id": "38968",
        "side": "ECO",
        "tranche": "second",
    },
}


def query_rows() -> dict[str, dict[str, Any]]:
    fields = "\n".join(
        f"""
        t{index}: transactionById(id: "{transaction_id}") {{
          id amount date merchantName propertyId tagId bankAccountId note
          isDeleted isSplit parentId
          splitTransactions {{
            id amount date merchantName propertyId tagId parentId isDeleted
          }}
        }}
        """
        for index, transaction_id in enumerate(ROWS)
    )
    data = graphql(
        {
            "operationName": "EcoEarldaoBridgeRows",
            "variables": {},
            "query": f"query EcoEarldaoBridgeRows {{ {fields} }}",
        }
    )["data"]
    return {
        str(row["id"]): row
        for row in data.values()
        if row is not None
    }


def validate(rows: dict[str, dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for transaction_id, expected in ROWS.items():
        row = rows.get(transaction_id)
        if not row:
            issues.append(f"missing transaction {transaction_id}")
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
            TRANSFER_TAG_ID,
            False,
            None,
        )
        if actual != wanted:
            issues.append(
                f"transaction {transaction_id} changed: "
                f"expected {wanted}, found {actual}"
            )
    return issues


def first_tranche_updates() -> list[dict[str, Any]]:
    return [
        {
            "id": transaction_id,
            "merchantName": (
                "EARLDAO -> ECO | ECO bridge repayment | tranche 1 | "
                "2026-07-17 | $10,000"
            ),
            "propertyId": EARLDAO_PROPERTY_ID,
            "tagId": TRANSFER_TAG_ID,
            "note": (
                "First repayment of ECO's verified $13,000 bridge advance to "
                "EARLDAO dated 2026-07-11. Remaining bridge principal after "
                "this transfer: $3,000. Internal transfer; not revenue."
            ),
        }
        for transaction_id, expected in ROWS.items()
        if expected["tranche"] == "first"
    ]


def second_tranche_updates() -> list[dict[str, Any]]:
    return [
        {
            "id": transaction_id,
            "merchantName": (
                "EARLDAO -> ECO | bridge payoff + clearing advance | "
                "2026-07-24 | $10,000"
            ),
            "propertyId": EARLDAO_PROPERTY_ID,
            "tagId": TRANSFER_TAG_ID,
            "note": (
                "Existing internal transfer split into the final $3,000 "
                "repayment of ECO's 2026-07-11 bridge and a separate $7,000 "
                "EARLDAO-funded ECO clearing advance pending transaction-level "
                "allocation to benefiting DAO(s). Not ECO revenue."
            ),
        }
        for transaction_id, expected in ROWS.items()
        if expected["tranche"] == "second"
    ]


def desired_second_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    sign = Decimal("-1") if cents(parent["amount"]) < 0 else Decimal("1")
    children = [
        {
            "amount": cents(sign * Decimal("3000.00")),
            "date": str(parent["date"]),
            "merchantName": (
                "EARLDAO -> ECO | final ECO bridge repayment | $3,000"
            ),
            "propertyId": EARLDAO_PROPERTY_ID,
            "tagId": TRANSFER_TAG_ID,
        },
        {
            "amount": cents(sign * Decimal("7000.00")),
            "date": str(parent["date"]),
            "merchantName": (
                "EARLDAO -> ECO | clearing advance pending DAO allocation | "
                "$7,000"
            ),
            "propertyId": EARLDAO_PROPERTY_ID,
            "tagId": TRANSFER_TAG_ID,
        },
    ]
    if sum((row["amount"] for row in children), Decimal("0")) != cents(
        parent["amount"]
    ):
        raise RuntimeError("repayment components do not sum to parent")
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


def expected_updates() -> dict[str, dict[str, Any]]:
    return {
        str(row["id"]): row
        for row in first_tranche_updates() + second_tranche_updates()
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


def verify() -> dict[str, Any]:
    rows = query_rows()
    issues = validate(rows)
    for transaction_id, expected in expected_updates().items():
        actual = rows.get(transaction_id)
        if not actual:
            continue
        for field in ("merchantName", "propertyId", "tagId"):
            if str(actual.get(field) or "") != str(expected[field]):
                issues.append(
                    f"{transaction_id} {field} is not settled: "
                    f"{actual.get(field)!r}"
                )
    for transaction_id, expected in ROWS.items():
        if expected["tranche"] != "second":
            continue
        parent = rows.get(transaction_id)
        if not parent:
            continue
        target = desired_second_children(parent)
        if normalized_children(
            parent.get("splitTransactions") or []
        ) != normalized_children(target):
            issues.append(f"{transaction_id} native split does not match")
    return {
        "status": "verified" if not issues else "verification_failed",
        "issues": issues,
        "cash_movement_created": False,
        "bridge_principal_after": "0.00",
        "unallocated_clearing_advance": "7000.00",
        "rows": [public_row(row) for row in rows.values()],
    }


def plan(rows: dict[str, dict[str, Any]], issues: list[str]) -> dict[str, Any]:
    return {
        "scope": "ECO/EARLDAO July 2026 bridge clearing",
        "status": "dry_run",
        "issues": issues,
        "cash_movement_created": False,
        "bridge": {
            "original_principal": "13000.00",
            "first_repayment": "10000.00",
            "final_repayment": "3000.00",
            "ending_principal": "0.00",
        },
        "separate_earldao_funded_eco_clearing_advance": {
            "amount": "7000.00",
            "property_allocation": "pending transaction-level support",
            "is_eco_revenue": False,
        },
        "source_rows": [
            public_row(rows[transaction_id])
            for transaction_id in sorted(rows, key=int)
        ],
        "metadata_updates": list(expected_updates().values()),
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    if args.verify:
        result = verify()
        path = write_report("eco_earldao_bridge_clear_verify.json", result)
        print(json.dumps({**result, "report": str(path)}, indent=2))
        return 0 if result["status"] == "verified" else 2

    rows = query_rows()
    issues = validate(rows)
    public = plan(rows, issues)
    plan_digest = digest(public)
    dry_run = {"digest": plan_digest, **public}
    dry_path = write_report("eco_earldao_bridge_clear_dry_run.json", dry_run)
    if not args.apply:
        print(json.dumps({**dry_run, "report": str(dry_path)}, indent=2))
        return 0 if not issues else 2
    if args.digest != plan_digest:
        raise RuntimeError(f"live digest is {plan_digest}; exact --digest required")
    if issues:
        raise RuntimeError(f"refusing apply with issues: {issues}")

    update_rows(first_tranche_updates())
    update_rows(second_tranche_updates())
    for transaction_id, expected in ROWS.items():
        if expected["tranche"] != "second":
            continue
        parent = query_parent(transaction_id)
        target = desired_second_children(parent)
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
    path = write_report("eco_earldao_bridge_clear_applied.json", applied)
    print(json.dumps({**applied, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
