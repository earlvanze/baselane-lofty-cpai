#!/usr/bin/env python3
"""Idempotently split the 84 Madison settlement transfers on both bank mirrors."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GRAPHQL_HELPER = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
PROPERTY_84 = "60548"
TAG_TRANSFER = "24"
CENT = Decimal("0.01")


def cents(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def run_graphql(payload: dict[str, Any]) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.flush()
        proc = subprocess.run(
            ["node", str(GRAPHQL_HELPER), handle.name],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
            check=False,
        )
    if proc.returncode:
        raise RuntimeError(f"GraphQL helper failed: {proc.stderr[-1200:]}")
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    if result.get("errors"):
        raise RuntimeError(f"GraphQL errors: {json.dumps(result['errors'])[:1600]}")
    return result


def query_parents(parent_ids: list[str]) -> dict[str, dict[str, Any]]:
    parents: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(parent_ids), 5):
        batch = parent_ids[offset : offset + 5]
        fields = "\n".join(
            f"""
            t{index}: transactionById(id: "{parent_id}") {{
              id amount date merchantName propertyId tagId note isSplit isDeleted
              splitTransactions {{
                id amount date merchantName propertyId tagId parentId isDeleted
              }}
            }}
            """
            for index, parent_id in enumerate(batch)
        )
        data = run_graphql(
            {
                "operationName": "SettlementParents",
                "variables": {},
                "query": f"query SettlementParents {{ {fields} }}",
            }
        )["data"]
        parents.update({str(row["id"]): row for row in data.values() if row})
    return parents


def active_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in parent.get("splitTransactions") or [] if not row.get("isDeleted")]


def expected_children(
    *,
    parent_amount: Decimal,
    parent_date: str,
    property_id: str,
    components: list[tuple[str, Decimal]],
) -> list[dict[str, Any]]:
    sign = Decimal("-1") if parent_amount < 0 else Decimal("1")
    rows = [
        {
            "amount": cents(sign * amount),
            "date": parent_date,
            "merchantName": label,
            "propertyId": property_id,
            "tagId": TAG_TRANSFER,
        }
        for label, amount in components
    ]
    if sum((row["amount"] for row in rows), Decimal("0")) != parent_amount:
        raise ValueError(f"components do not sum to parent amount {parent_amount}")
    return rows


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
    )


def row_key(row: dict[str, Any]) -> tuple[Decimal, str, str, str, str]:
    return normalized([row])[0]


def split_parent(parent_id: str, children: list[dict[str, Any]]) -> dict[str, Any]:
    return run_graphql(
        {
            "operationName": "createOrUpdateSplitTx",
            "variables": {
                "parentTransactionId": parent_id,
                "splitType": "AMOUNT",
                "transactionSplitInputs": [
                    {
                        **row,
                        "amount": float(row["amount"]),
                        "propertyUnitId": None,
                    }
                    for row in children
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
    )["data"]["createOrUpdateSplitTx"]


def reconcile_parent_split(
    parent: dict[str, Any],
    target: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep one exact target child and mark all superseded children deleted."""
    existing = active_children(parent)
    by_key: dict[tuple[Decimal, str, str, str, str], list[dict[str, Any]]] = {}
    for child in existing:
        by_key.setdefault(row_key(child), []).append(child)

    inputs: list[dict[str, Any]] = []
    kept_ids: set[str] = set()
    for target_row in target:
        candidates = sorted(
            by_key.get(row_key(target_row), []),
            key=lambda row: int(str(row.get("id") or "0")),
            reverse=True,
        )
        row = dict(target_row)
        if candidates:
            row["id"] = str(candidates[0]["id"])
            kept_ids.add(str(candidates[0]["id"]))
        inputs.append(row)

    for child in existing:
        child_id = str(child.get("id") or "")
        if child_id in kept_ids:
            continue
        inputs.append(
            {
                "id": child_id,
                "amount": cents(child.get("amount") or 0),
                "date": str(child.get("date") or parent.get("date") or ""),
                "merchantName": str(child.get("merchantName") or ""),
                "propertyId": str(child.get("propertyId") or ""),
                "tagId": str(child.get("tagId") or ""),
                "isDelete": True,
            }
        )
    return split_parent(str(parent["id"]), inputs)


def update_transfer_metadata(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not inputs:
        return []
    return run_graphql(
        {
            "operationName": "UpdateTransaction",
            "variables": {"input": inputs},
            "query": """
            mutation UpdateTransaction($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) {
                id amount date merchantName propertyId tagId note
              }
            }
            """,
        }
    )["data"]["updateTransactions"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Allow an existing split to be replaced when its audited target changed.",
    )
    args = parser.parse_args()

    schedules: dict[str, list[tuple[str, Decimal]]] = {}
    property_by_parent: dict[str, str] = {}

    eco_7000 = [
        (
            "84-ECO | thru Feb26 net | DAO fees 437.50; Cleaning 775; Repairs 241.25; "
            "Loan 400; rent/reimb offsets 1590.32",
            Decimal("263.43"),
        ),
        (
            "84-ECO | Mar26 net | Cleaning 60; PM 796.09; Repairs 845; DAO fee 62.50; "
            "reimb/legal offsets 565.60",
            Decimal("1197.99"),
        ),
        (
            "84-ECO | Apr26 | Cleaning 435; PM 583.68; DAO fee 62.50",
            Decimal("1081.18"),
        ),
        (
            "84-ECO | May26 net | Cleaning 506.25; PM 721.32; Repairs 426.25; "
            "Supplies 39.46; DAO fee 62.50; legal offset 405",
            Decimal("1350.78"),
        ),
        (
            "84-ECO | Jun26 | Cleaning 640; PM 679.49; DAO fee 62.50",
            Decimal("1381.99"),
        ),
        ("84-ECO | 2026-07-24 overpayment later returned", Decimal("1724.63")),
    ]
    for parent_id in ("319780204", "319780208"):
        schedules[parent_id] = eco_7000
        property_by_parent[parent_id] = PROPERTY_84

    shared_84_88 = [
        (
            "84-88 | May26 net | Linens 385.42; Waste 87.81; Software 62.11; "
            "Streaming 10.54; less pre-May credit 170.56; cross-acct offset 39.59",
            Decimal("335.73"),
        ),
        (
            "84-88 | Jun26 | Linens 379.20; Waste 87.81; Software 58.11; "
            "Streaming 3.25",
            Decimal("528.37"),
        ),
        (
            "84-88 | Jul1-14 | Linens 185.49; Waste 87.81; Software 28.24; "
            "Streaming 7.29",
            Decimal("308.83"),
        ),
        ("84-88 | Jul17 | Linens | Morgan Linen", Decimal("93.57")),
        ("84-88 | Jul24 | Linens | Morgan Linen", Decimal("92.41")),
    ]
    for parent_id in ("321083688", "321083692"):
        schedules[parent_id] = shared_84_88
        property_by_parent[parent_id] = PROPERTY_84

    shared_86_84 = [
        ("86-84 | 2026-03 shared supplies", Decimal("22.70")),
        ("86-84 | 2026-04 shared supplies", Decimal("33.09")),
    ]
    for parent_id in ("321081476", "321081472"):
        schedules[parent_id] = shared_86_84
        property_by_parent[parent_id] = PROPERTY_84

    spectrum_84_90 = [
        ("84-90 | 2026-04 | Spectrum | source: Phone/Cable/Internet", Decimal("38.85")),
        ("84-90 | 2026-05 | Spectrum | source: Phone/Cable/Internet", Decimal("38.83")),
        ("84-90 | 2026-05 | Spectrum | source: Utilities", Decimal("20.00")),
        ("84-90 | 2026-06 | Spectrum | source: Utilities", Decimal("38.83")),
        ("84-90 | 2026-06 | Spectrum | source: Phone/Cable/Internet", Decimal("12.50")),
        ("84-90 | 2026-07 | Spectrum | source: Phone/Cable/Internet", Decimal("35.82")),
    ]
    for parent_id in ("321140607", "321140626"):
        schedules[parent_id] = spectrum_84_90
        property_by_parent[parent_id] = "60548"

    spectrum_86_90 = [
        ("86-90 | 2026-01 | Spectrum | source: Phone/Cable/Internet", Decimal("37.50")),
        ("86-90 | 2026-02 | Spectrum | source: Phone/Cable/Internet", Decimal("37.50")),
        ("86-90 | 2026-04 | Spectrum | source: Phone/Cable/Internet", Decimal("38.85")),
        ("86-90 | 2026-05 | Spectrum | source: Phone/Cable/Internet", Decimal("38.83")),
        ("86-90 | 2026-05 | Spectrum | source: Utilities", Decimal("20.00")),
        ("86-90 | 2026-06 | Spectrum | source: Utilities", Decimal("38.83")),
        ("86-90 | 2026-06 | Spectrum | source: Phone/Cable/Internet", Decimal("12.50")),
        ("86-90 | 2026-07 | Spectrum | source: Phone/Cable/Internet", Decimal("35.82")),
    ]
    for parent_id in ("321141265", "321141269"):
        schedules[parent_id] = spectrum_86_90
        property_by_parent[parent_id] = "63162"

    spectrum_88_90 = [
        ("88-90 | 2026-04 | Spectrum | source: Phone/Cable/Internet", Decimal("38.85")),
        ("88-90 | 2026-05 | Spectrum | source: Phone/Cable/Internet", Decimal("38.83")),
        ("88-90 | 2026-05 | Spectrum | source: Utilities", Decimal("20.00")),
        ("88-90 | 2026-06 | Spectrum | source: Utilities", Decimal("38.83")),
        ("88-90 | 2026-06 | Spectrum | source: Phone/Cable/Internet", Decimal("12.50")),
        ("88-90 | 2026-07 | Spectrum | source: Phone/Cable/Internet", Decimal("35.82")),
    ]
    for parent_id in ("321141981", "321141986"):
        schedules[parent_id] = spectrum_88_90
        property_by_parent[parent_id] = "31499"

    parents = query_parents(list(schedules))
    missing = sorted(set(schedules) - set(parents))
    if missing:
        raise RuntimeError(f"missing parent transactions: {missing}")

    plans: list[dict[str, Any]] = []
    for parent_id, components in schedules.items():
        parent = parents[parent_id]
        target = expected_children(
            parent_amount=cents(parent["amount"]),
            parent_date=str(parent["date"]),
            property_id=property_by_parent[parent_id],
            components=components,
        )
        existing = active_children(parent)
        existing_differs = bool(existing) and normalized(existing) != normalized(target)
        if existing_differs and not args.replace_existing:
            action = "replace_requires_flag"
        elif existing_differs:
            action = "replace"
        else:
            action = "already_exact" if existing else "create"
        plans.append(
            {
                "parent_id": parent_id,
                "parent_amount": str(cents(parent["amount"])),
                "action": action,
                "components": [
                    {"amount": str(row["amount"]), "label": row["merchantName"]}
                    for row in target
                ],
                "_target": target,
            }
        )

    metadata_updates = []
    eco_note = (
        "84 Madison 7000 settlement through Jun 2026 split by obligation month "
        "including 1724 63 overpayment returned Jul 27"
    )
    for parent_id in ("319780204", "319780208"):
        parent = parents[parent_id]
        note = parent.get("note")
        current_note = note.get("text") if isinstance(note, dict) else note
        if (
            str(parent.get("propertyId") or "") != PROPERTY_84
            or str(parent.get("tagId") or "") != TAG_TRANSFER
            or str(current_note or "") != eco_note
        ):
            metadata_updates.append(
                {
                    "id": parent_id,
                    "propertyId": PROPERTY_84,
                    "tagId": TAG_TRANSFER,
                    "note": eco_note,
                }
            )

    shared_84_88_note = (
        "84 to 88 shared-cost settlement through Jul 24 2026. Every split remains "
        "Transfers Between Accounts; expense categories are audit labels only. "
        "May net 335.73 = gross 545.88 less pre-May 84 credit 170.56 and 88-to-84 "
        "cross-account offset 39.59 (66.95 paid by 84 less 27.36 reimbursed)."
    )
    for parent_id in ("321083688", "321083692"):
        parent = parents[parent_id]
        note = parent.get("note")
        current_note = note.get("text") if isinstance(note, dict) else note
        if (
            str(parent.get("propertyId") or "") != PROPERTY_84
            or str(parent.get("tagId") or "") != TAG_TRANSFER
            or str(current_note or "") != shared_84_88_note
        ):
            metadata_updates.append(
                {
                    "id": parent_id,
                    "propertyId": PROPERTY_84,
                    "tagId": TAG_TRANSFER,
                    "note": shared_84_88_note,
                }
            )

    public_plan = [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in plans
    ]
    if not args.apply:
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "metadata_updates": metadata_updates,
                    "splits": public_plan,
                },
                indent=2,
            )
        )
        return 0

    update_transfer_metadata(metadata_updates)
    applied = []
    for row in plans:
        if row["action"] == "create":
            split_parent(row["parent_id"], row["_target"])
            applied.append(row["parent_id"])
        elif row["action"] == "replace":
            reconcile_parent_split(parents[row["parent_id"]], row["_target"])
            applied.append(row["parent_id"])
        elif row["action"] == "replace_requires_flag":
            raise RuntimeError(
                f"parent {row['parent_id']} has a different live split; "
                "rerun with --replace-existing after reviewing the dry run"
            )

    verified = query_parents(list(schedules))
    for parent_id, components in schedules.items():
        parent = verified[parent_id]
        target = expected_children(
            parent_amount=cents(parent["amount"]),
            parent_date=str(parent["date"]),
            property_id=property_by_parent[parent_id],
            components=components,
        )
        if normalized(active_children(parent)) != normalized(target):
            raise RuntimeError(f"post-apply verification failed for {parent_id}")
    for parent_id in ("319780204", "319780208"):
        parent = verified[parent_id]
        note = parent.get("note")
        current_note = note.get("text") if isinstance(note, dict) else note
        if str(parent.get("propertyId") or "") != PROPERTY_84 or str(parent.get("tagId") or "") != TAG_TRANSFER:
            raise RuntimeError(f"metadata verification failed for {parent_id}")
        if str(current_note or "") != eco_note:
            raise RuntimeError(f"note verification failed for {parent_id}")
    for parent_id in ("321083688", "321083692"):
        parent = verified[parent_id]
        note = parent.get("note")
        current_note = note.get("text") if isinstance(note, dict) else note
        if str(parent.get("propertyId") or "") != PROPERTY_84 or str(parent.get("tagId") or "") != TAG_TRANSFER:
            raise RuntimeError(f"metadata verification failed for {parent_id}")
        if str(current_note or "") != shared_84_88_note:
            raise RuntimeError(f"note verification failed for {parent_id}")

    print(
        json.dumps(
            {
                "status": "applied",
                "split_parents_applied": applied,
                "metadata_rows_updated": [row["id"] for row in metadata_updates],
                "verified_parent_count": len(verified),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
