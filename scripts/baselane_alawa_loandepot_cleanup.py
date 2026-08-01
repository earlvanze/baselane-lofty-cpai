#!/usr/bin/env python3
"""Idempotently clean up 85-104 Alawa LoanDepot Baselane splits.

Uses the existing Baselane CDP GraphQL helper so writes happen through the
same authenticated browser session used by the daily sync workflow.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GRAPHQL_HELPER = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
PIPELINE_LOCK = ROOT / "scripts" / ".baselane_source_pipeline.lock"

ALAWA_PROPERTY_ID = "73461"
MINING_PROPERTY_ID = "37648"
TAG_MORTGAGE_INTEREST = "11"
TAG_MORTGAGE_PRINCIPAL = "20"
TAG_MORTGAGE_PAYMENT = "33"
TAG_INSURANCE = "8"
TAG_TAXES = "15"
TAG_GENERAL_ESCROW = "130"
TAG_CITY_STATE_LOCAL_TAXES = "95"
NO_DAO_MORTGAGE_DEBT_TAG_IDS = {TAG_MORTGAGE_INTEREST, TAG_MORTGAGE_PRINCIPAL, TAG_MORTGAGE_PAYMENT, TAG_GENERAL_ESCROW}

BAD_NOTES = {"*SPLIT*", "hidden after mortgage component split"}
NO_PROPERTY_FIX = object()

TARGET_SPLITS: dict[str, list[dict[str, Any]]] = {
    "289871689": [
        {
            "amount": -1486.16,
            "tagId": TAG_MORTGAGE_INTEREST,
            "propertyId": MINING_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Interest",
            "date": "2026-05-29",
        },
        {
            "amount": -1357.55,
            "tagId": TAG_MORTGAGE_PRINCIPAL,
            "propertyId": MINING_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Principal",
            "date": "2026-05-29",
        },
        {
            "amount": -280.45,
            "tagId": TAG_TAXES,
            "propertyId": ALAWA_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Escrow - Property Taxes",
            "date": "2026-05-29",
        },
        {
            "amount": -187.88,
            "tagId": TAG_INSURANCE,
            "propertyId": ALAWA_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Escrow - Insurance",
            "date": "2026-05-29",
        },
        {
            "amount": -300.66,
            "tagId": TAG_GENERAL_ESCROW,
            "propertyId": MINING_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Escrow - General",
            "date": "2026-05-29",
        },
    ],
    "304093755": [
        {
            "amount": -1482.91,
            "tagId": TAG_MORTGAGE_INTEREST,
            "propertyId": MINING_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Interest",
            "date": "2026-06-26",
        },
        {
            "amount": -1360.80,
            "tagId": TAG_MORTGAGE_PRINCIPAL,
            "propertyId": MINING_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Principal",
            "date": "2026-06-26",
        },
        {
            "amount": -280.45,
            "tagId": TAG_TAXES,
            "propertyId": ALAWA_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Escrow - Property Taxes",
            "date": "2026-06-26",
        },
        {
            "amount": -187.88,
            "tagId": TAG_INSURANCE,
            "propertyId": ALAWA_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Escrow - Insurance",
            "date": "2026-06-26",
        },
        {
            "amount": -300.66,
            "tagId": TAG_GENERAL_ESCROW,
            "propertyId": MINING_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Escrow - General",
            "date": "2026-06-26",
        },
    ],
    "321949940": [
        {
            "amount": -1479.65,
            "tagId": TAG_MORTGAGE_INTEREST,
            "propertyId": MINING_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Interest",
            "date": "2026-07-29",
        },
        {
            "amount": -1364.06,
            "tagId": TAG_MORTGAGE_PRINCIPAL,
            "propertyId": MINING_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Principal",
            "date": "2026-07-29",
        },
        {
            "amount": -280.45,
            "tagId": TAG_TAXES,
            "propertyId": ALAWA_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Escrow - Property Taxes",
            "date": "2026-07-29",
        },
        {
            "amount": -187.88,
            "tagId": TAG_INSURANCE,
            "propertyId": ALAWA_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Escrow - Insurance",
            "date": "2026-07-29",
        },
        {
            "amount": -300.66,
            "tagId": TAG_GENERAL_ESCROW,
            "propertyId": MINING_PROPERTY_ID,
            "merchantName": "85-104 Alawa Pl Mortgage Escrow - General",
            "date": "2026-07-29",
        },
    ],
}

LEGACY_COMPONENT_BY_ABS_AMOUNT = {
    Decimal("1319.12"): (MINING_PROPERTY_ID, TAG_MORTGAGE_PRINCIPAL),
    Decimal("1524.59"): (MINING_PROPERTY_ID, TAG_MORTGAGE_INTEREST),
    Decimal("141.56"): (ALAWA_PROPERTY_ID, TAG_CITY_STATE_LOCAL_TAXES),
    Decimal("58.58"): (ALAWA_PROPERTY_ID, TAG_INSURANCE),
    Decimal("135.74"): (MINING_PROPERTY_ID, TAG_GENERAL_ESCROW),
}


def decimal_amount(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def note_text(note: Any) -> str:
    if isinstance(note, dict):
        return str(note.get("text") or "")
    return str(note or "")


def run_graphql(payload: dict[str, Any]) -> dict[str, Any]:
    if not GRAPHQL_HELPER.exists():
        raise FileNotFoundError(f"missing GraphQL helper: {GRAPHQL_HELPER}")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.flush()
        payload_path = handle.name
    try:
        env = os.environ.copy()
        proc = subprocess.run(
            ["node", str(GRAPHQL_HELPER), payload_path],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    finally:
        Path(payload_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"GraphQL helper rc={proc.returncode}")
    data = json.loads(proc.stdout)
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return data


def query_transactions() -> list[dict[str, Any]]:
    payload = {
        "operationName": "Transactions",
        "variables": {
            "input": {
                "sort": {"direction": "DESC", "field": "date"},
                "filter": {"search": "loanDepot", "isHidden": False, "isDeleted": False},
                "page": 1,
                "pageLimit": 100,
            }
        },
        "query": """
        query Transactions($input: SortsAndFilters) {
          transactions(input: $input) {
            total
            data {
              id
              amount
              date
              merchantName
              propertyId
              tagId
              note
              isSplit
              parentId
              hidden
              isDeleted
            }
          }
        }
        """,
    }
    result = run_graphql(payload)
    return result["data"]["transactions"]["data"]


def query_parent(parent_id: str) -> dict[str, Any] | None:
    payload = {
        "operationName": "TransactionById",
        "variables": {"id": str(parent_id)},
        "query": """
        query TransactionById($id: ID!) {
          transactionById(id: $id) {
            id
            amount
            date
            merchantName
            propertyId
            tagId
            note
            isSplit
            parentId
            hidden
            isDeleted
            splitTransactions {
              id
              amount
              date
              merchantName
              propertyId
              unitId
              tagId
              note
              parentId
              hidden
              isDeleted
            }
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["transactionById"]


def active_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    return [child for child in parent.get("splitTransactions") or [] if not child.get("isDeleted")]


def normalized_components(rows: list[dict[str, Any]]) -> list[tuple[str, str, Decimal, str]]:
    return sorted(
        (
            str(row.get("tagId") or ""),
            str(row.get("propertyId") or ""),
            decimal_amount(row.get("amount") or "0"),
            str(row.get("merchantName") or ""),
        )
        for row in rows
    )


def split_matches(parent: dict[str, Any], target: list[dict[str, Any]]) -> bool:
    children = active_children(parent)
    if len(children) != len(target):
        return False
    return normalized_components(children) == normalized_components(target)


def duplicate_child_ids(parent: dict[str, Any], target: list[dict[str, Any]]) -> list[str]:
    remaining = list(normalized_components(target))
    extras: list[str] = []
    for child in active_children(parent):
        component = normalized_components([child])[0]
        if component in remaining:
            remaining.remove(component)
        else:
            extras.append(str(child["id"]))
    return extras if not remaining else []


def mutation_split(parent_id: str, splits: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "operationName": "createOrUpdateSplitTx",
        "variables": {
            "parentTransactionId": str(parent_id),
            "splitType": "AMOUNT",
            "transactionSplitInputs": [
                {
                    "tagId": str(split["tagId"]),
                    "propertyId": str(split["propertyId"]) if split.get("propertyId") is not None else None,
                    "propertyUnitId": None,
                    "date": str(split["date"]),
                    "amount": float(split["amount"]),
                    "merchantName": str(split["merchantName"]),
                }
                for split in splits
            ],
        },
        "query": """
        mutation createOrUpdateSplitTx(
          $parentTransactionId: ID!
          $splitType: SplitType!
          $transactionSplitInputs: [TransactionSplitInput!]!
        ) {
          createOrUpdateSplitTx(
            input: {
              parentTransactionId: $parentTransactionId
              transactionSplitInputs: $transactionSplitInputs
              splitType: $splitType
            }
          ) {
            id
            splitTransactions {
              id
              amount
              tagId
              propertyId
              merchantName
              date
            }
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["createOrUpdateSplitTx"]


def mutation_update_transactions(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not inputs:
        return []
    payload = {
        "operationName": "UpdateTransaction",
        "variables": {"input": inputs},
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id
            note
            propertyId
            tagId
            amount
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["updateTransactions"]


def mutation_update_notes(transaction_ids: list[str]) -> list[dict[str, Any]]:
    return mutation_update_transactions([{"id": str(tx_id), "note": ""} for tx_id in transaction_ids])


def expected_component(row: dict[str, Any]) -> tuple[str, str] | None:
    merchant = " ".join(str(row.get("merchantName") or "").lower().split())
    if "mortgage interest" in merchant:
        return MINING_PROPERTY_ID, TAG_MORTGAGE_INTEREST
    if "mortgage principal" in merchant:
        return MINING_PROPERTY_ID, TAG_MORTGAGE_PRINCIPAL
    if "escrow - property taxes" in merchant:
        return ALAWA_PROPERTY_ID, TAG_TAXES
    if "escrow - insurance" in merchant:
        return ALAWA_PROPERTY_ID, TAG_INSURANCE
    if "escrow - general" in merchant:
        return MINING_PROPERTY_ID, TAG_GENERAL_ESCROW
    if merchant == "loandepot" and row.get("parentId"):
        return LEGACY_COMPONENT_BY_ABS_AMOUNT.get(abs(decimal_amount(row.get("amount") or "0")))
    return None


def plan_digest(plan: dict[str, Any]) -> str:
    payload = {
        "split_actions": plan["split_actions"],
        "note_ids": plan["note_ids"],
        "property_fix_inputs": plan["property_fix_inputs"],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def exclusive_pipeline_lock():
    PIPELINE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with PIPELINE_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def build_plan() -> dict[str, Any]:
    parents = {parent_id: query_parent(parent_id) for parent_id in TARGET_SPLITS}
    loan_depot_rows = query_transactions()

    split_actions: list[dict[str, Any]] = []
    note_ids: set[str] = set()
    property_issues: list[dict[str, Any]] = []

    for parent_id, target in TARGET_SPLITS.items():
        parent = parents[parent_id]
        if not parent:
            split_actions.append({"parent_id": parent_id, "action": "missing_parent"})
            continue
        target_total = sum(decimal_amount(item["amount"]) for item in target)
        parent_amount = decimal_amount(parent.get("amount") or "0")
        if target_total != parent_amount:
            split_actions.append(
                {
                    "parent_id": parent_id,
                    "action": "invalid_target_total",
                    "target_total": str(target_total),
                    "parent_amount": str(parent_amount),
                }
            )
            continue
        if split_matches(parent, target):
            split_actions.append({"parent_id": parent_id, "action": "already_split"})
        elif extras := duplicate_child_ids(parent, target):
            split_actions.append(
                {
                    "parent_id": parent_id,
                    "action": "delete_duplicate_children",
                    "transaction_ids": sorted(extras, key=int),
                }
            )
        else:
            split_actions.append({"parent_id": parent_id, "action": "split", "target": target})
        if note_text(parent.get("note")) in BAD_NOTES:
            note_ids.add(str(parent["id"]))
        for child in active_children(parent):
            if note_text(child.get("note")) in BAD_NOTES:
                note_ids.add(str(child["id"]))

    for row in loan_depot_rows:
        if str(row.get("parentId") or "") in TARGET_SPLITS:
            continue
        tag_id = str(row.get("tagId") or "")
        property_id = str(row.get("propertyId") or "")
        expected = expected_component(row)
        if expected and (property_id, tag_id) != expected:
            target_property_id, target_tag_id = expected
            property_issues.append(
                {
                    "id": row.get("id"),
                    "date": row.get("date"),
                    "amount": row.get("amount"),
                    "tagId": tag_id,
                    "propertyId": property_id,
                    "targetPropertyId": target_property_id,
                    "targetTagId": target_tag_id,
                    "merchantName": row.get("merchantName"),
                }
            )
        if note_text(row.get("note")) in BAD_NOTES:
            note_ids.add(str(row["id"]))

    return {
        "split_actions": split_actions,
        "note_ids": sorted(note_ids, key=int),
        "property_issues": property_issues,
        "property_fix_inputs": sorted(
            (
                {
                    "id": str(item["id"]),
                    "propertyId": item.get("targetPropertyId"),
                    "tagId": item.get("targetTagId"),
                    "isReviewedByUser": True,
                }
                for item in property_issues
            ),
            key=lambda item: int(item["id"]),
        ),
        "property_fix_ids": sorted({str(item["id"]) for item in property_issues}, key=int),
    }


def apply_plan(plan: dict[str, Any]) -> dict[str, Any]:
    applied: dict[str, Any] = {"splits": [], "duplicate_deletes": [], "notes": [], "errors": []}
    for action in plan["split_actions"]:
        if action["action"] == "delete_duplicate_children":
            try:
                applied["duplicate_deletes"].extend(
                    mutation_update_transactions(
                        [
                            {"id": tx_id, "isDeleted": True, "isReviewedByUser": True}
                            for tx_id in action["transaction_ids"]
                        ]
                    )
                )
            except Exception as exc:
                applied["errors"].append({"parent_id": action["parent_id"], "error": str(exc)})
            continue
        if action["action"] != "split":
            continue
        try:
            applied["splits"].append(mutation_split(action["parent_id"], action["target"]))
        except Exception as exc:
            applied["errors"].append({"parent_id": action["parent_id"], "error": str(exc)})
    if plan["note_ids"]:
        try:
            applied["notes"] = mutation_update_notes(plan["note_ids"])
        except Exception as exc:
            applied["errors"].append({"note_cleanup": str(exc)})
    if plan.get("property_fix_inputs"):
        try:
            applied["property_fixes"] = mutation_update_transactions(plan["property_fix_inputs"])
        except Exception as exc:
            applied["errors"].append({"property_cleanup": str(exc)})
    return applied


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="apply Baselane split/note fixes")
    mode.add_argument("--dry-run", action="store_true", help="show planned fixes without writes")
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "baselane_alawa_loandepot_cleanup_report.json")
    parser.add_argument("--json", action="store_true", help="print JSON report")
    parser.add_argument("--require-plan-digest", help="exact digest emitted by the live preview")
    args = parser.parse_args(argv)

    if not args.apply and not args.dry_run:
        args.dry_run = True

    plan = build_plan()
    digest = plan_digest(plan)
    applied = None
    if args.apply:
        if not args.require_plan_digest:
            parser.error("--apply requires --require-plan-digest")
        with exclusive_pipeline_lock():
            plan = build_plan()
            digest = plan_digest(plan)
            if digest != args.require_plan_digest:
                raise SystemExit(
                    f"plan digest changed: expected {args.require_plan_digest}, current {digest}"
                )
            applied = apply_plan(plan)
            verify = build_plan()
    else:
        verify = None

    status = "ok"
    if applied and applied.get("errors"):
        status = "failed"
    elif verify and (
        any(action["action"] in {"split", "delete_duplicate_children"} for action in verify["split_actions"])
        or verify["note_ids"]
        or verify["property_issues"]
    ):
        status = "needs_review"
    elif (
        any(action["action"] in {"split", "delete_duplicate_children"} for action in plan["split_actions"])
        or plan["property_issues"]
    ):
        status = "needs_review"

    report = {
        "status": status,
        "mode": "apply" if args.apply else "dry_run",
        "plan_digest": digest,
        "plan": plan,
        "applied": applied,
        "verify": verify,
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{status}: wrote {args.report}")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
