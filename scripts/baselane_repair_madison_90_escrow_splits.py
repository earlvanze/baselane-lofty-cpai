#!/usr/bin/env python3
"""Restore two native 90 Madison escrow children and remove manual copies.

This manual-only repair is limited to the January and March 2025 Citadel bank
roots identified by the NY mortgage graph audit. It requires an exact preview
digest and verifies both split arithmetic and unchanged bank balances.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_mcp.transfers import (  # noqa: E402
    list_active_transfer_accounts,
    run_graphql_via_cdp,
)


BRIDGE = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
REPORT = ROOT / "reports" / "madison_90_escrow_split_repair.json"
PIPELINE_LOCK = ROOT / "scripts" / ".baselane_source_pipeline.lock"
PROPERTY_ID = "31525"
BANK_ID = "38968"
ESCROW_TAG_ID = "27"
CENT = Decimal("0.01")

REPAIRS = (
    {
        "date": "2025-01-14",
        "parent_id": "131782750",
        "parent_amount": "-2570.60",
        "p_and_i_id": "258417852",
        "p_and_i_amount": "-1769.21",
        "restore_id": "258417853",
        "delete_ids": ("258421135", "258424014"),
    },
    {
        "date": "2025-03-10",
        "parent_id": "143479097",
        "parent_amount": "-3020.60",
        "p_and_i_id": "258418622",
        "p_and_i_amount": "-2219.21",
        "restore_id": "258418623",
        "delete_ids": ("258421200", "258424369"),
    },
)


def graphql(payload: dict[str, Any]) -> dict[str, Any]:
    return run_graphql_via_cdp(
        payload,
        bridge_path=BRIDGE,
        workspace_root=ROOT,
        timeout=120,
    )


def transaction(row_id: str) -> dict[str, Any] | None:
    result = graphql(
        {
            "operationName": "Madison90EscrowRepairTransaction",
            "variables": {"id": row_id},
            "query": """
              query Madison90EscrowRepairTransaction($id: ID!) {
                transactionById(id: $id) {
                  id amount date merchantName propertyId tagId bankAccountId
                  isManual hidden isDeleted isSplit parentId
                }
              }
            """,
        }
    )
    return result["data"]["transactionById"]


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT)


def current_balance() -> str:
    for account in list_active_transfer_accounts(graphql):
        if str(account["bank_account_id"]) == BANK_ID:
            return str(account["available_balance"])
    raise RuntimeError(f"missing live Baselane bank account {BANK_ID}")


@contextmanager
def pipeline_lock(enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    PIPELINE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with PIPELINE_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def expected_actions() -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for repair in REPAIRS:
        actions.append(
            {
                "action": "restore_native_escrow_child",
                "id": repair["restore_id"],
                "parent_id": repair["parent_id"],
                "date": repair["date"],
                "amount": "-801.39",
                "property_id": PROPERTY_ID,
                "tag_id": ESCROW_TAG_ID,
            }
        )
        actions.extend(
            {
                "action": "delete_redundant_manual_escrow",
                "id": row_id,
                "preserve_id": repair["restore_id"],
                "date": repair["date"],
                "amount": "-801.39",
                "property_id": PROPERTY_ID,
                "tag_id": ESCROW_TAG_ID,
            }
            for row_id in repair["delete_ids"]
        )
    return actions


def action_digest(actions: list[dict[str, Any]]) -> str:
    payload = json.dumps(actions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_live_state() -> tuple[list[str], dict[str, dict[str, Any]]]:
    issues: list[str] = []
    rows: dict[str, dict[str, Any]] = {}
    for repair in REPAIRS:
        ids = (
            repair["parent_id"],
            repair["p_and_i_id"],
            repair["restore_id"],
            *repair["delete_ids"],
        )
        for row_id in ids:
            row = transaction(row_id)
            if row is None:
                issues.append(f"missing:{row_id}")
            else:
                rows[row_id] = row

        if any(row_id not in rows for row_id in ids):
            continue
        parent = rows[repair["parent_id"]]
        p_and_i = rows[repair["p_and_i_id"]]
        restore = rows[repair["restore_id"]]
        if not parent.get("isSplit") or not parent.get("hidden") or parent.get("isDeleted"):
            issues.append(f"parent_state_mismatch:{repair['parent_id']}")
        if money(parent.get("amount")) != money(repair["parent_amount"]):
            issues.append(f"parent_amount_mismatch:{repair['parent_id']}")
        if p_and_i.get("isDeleted") or str(p_and_i.get("parentId") or "") != repair["parent_id"]:
            issues.append(f"p_and_i_state_mismatch:{repair['p_and_i_id']}")
        if money(p_and_i.get("amount")) != money(repair["p_and_i_amount"]):
            issues.append(f"p_and_i_amount_mismatch:{repair['p_and_i_id']}")
        if str(restore.get("parentId") or "") != repair["parent_id"]:
            issues.append(f"native_escrow_parent_mismatch:{repair['restore_id']}")
        if money(restore.get("amount")) != Decimal("-801.39"):
            issues.append(f"native_escrow_amount_mismatch:{repair['restore_id']}")
        if str(restore.get("propertyId") or "") != PROPERTY_ID:
            issues.append(f"native_escrow_property_mismatch:{repair['restore_id']}")
        if str(restore.get("bankAccountId") or "") != BANK_ID:
            issues.append(f"native_escrow_bank_mismatch:{repair['restore_id']}")
        for row_id in repair["delete_ids"]:
            row = rows[row_id]
            if row.get("parentId") or row.get("bankAccountId"):
                issues.append(f"manual_copy_not_bankless:{row_id}")
            if money(row.get("amount")) != Decimal("-801.39"):
                issues.append(f"manual_copy_amount_mismatch:{row_id}")
            if str(row.get("propertyId") or "") != PROPERTY_ID:
                issues.append(f"manual_copy_property_mismatch:{row_id}")
    return sorted(set(issues)), rows


def mutate() -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    for repair in REPAIRS:
        inputs.append(
            {
                "id": repair["restore_id"],
                "isDeleted": False,
                "tagId": ESCROW_TAG_ID,
                "propertyId": PROPERTY_ID,
                "isReviewedByUser": True,
            }
        )
        inputs.extend(
            {
                "id": row_id,
                "isDeleted": True,
                "isReviewedByUser": True,
            }
            for row_id in repair["delete_ids"]
        )
    result = graphql(
        {
            "operationName": "RepairMadison90EscrowSplits",
            "variables": {"input": inputs},
            "query": """
              mutation RepairMadison90EscrowSplits($input: [UpdateTransaction!]) {
                updateTransactions(input: $input) {
                  id amount propertyId tagId bankAccountId parentId isDeleted
                }
              }
            """,
        }
    )
    return list(result["data"]["updateTransactions"])


def verify_after_apply(balance_before: str) -> list[str]:
    failures: list[str] = []
    for repair in REPAIRS:
        parent = transaction(repair["parent_id"])
        p_and_i = transaction(repair["p_and_i_id"])
        escrow = transaction(repair["restore_id"])
        if not parent or not p_and_i or not escrow:
            failures.append(f"post_apply_missing_split:{repair['parent_id']}")
            continue
        if escrow.get("isDeleted"):
            failures.append(f"native_escrow_not_restored:{repair['restore_id']}")
        if str(escrow.get("tagId") or "") != ESCROW_TAG_ID:
            failures.append(f"native_escrow_tag_mismatch:{repair['restore_id']}")
        child_sum = money(p_and_i.get("amount")) + money(escrow.get("amount"))
        if child_sum != money(parent.get("amount")):
            failures.append(f"split_sum_mismatch:{repair['parent_id']}:{child_sum}")
        for row_id in repair["delete_ids"]:
            row = transaction(row_id)
            if row and not row.get("isDeleted"):
                failures.append(f"manual_copy_still_active:{row_id}")
    balance_after = current_balance()
    if balance_after != balance_before:
        failures.append(f"bank_balance_changed:{balance_before}:{balance_after}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--require-action-digest")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()
    if args.apply and not args.require_action_digest:
        parser.error("--apply requires --require-action-digest")

    actions = expected_actions()
    digest = action_digest(actions)
    if args.apply and args.require_action_digest != digest:
        raise SystemExit(
            f"action digest changed: expected {args.require_action_digest}, current {digest}"
        )

    mutation_results: list[dict[str, Any]] = []
    verification_failures: list[str] = []
    with pipeline_lock(args.apply):
        issues, _ = validate_live_state()
        balance_before = current_balance()
        if args.apply and not issues:
            mutation_results = mutate()
            verification_failures = verify_after_apply(balance_before)

    report = {
        "status": "ok" if not issues and not verification_failures else "blocked",
        "mode": "apply" if args.apply else "preview",
        "accounting_basis": (
            "Preserve each real Citadel bank root with one native escrow child; "
            "remove the two bankless manual copies for the same date and amount."
        ),
        "action_digest": digest,
        "action_count": len(actions),
        "actions": actions,
        "issues": issues,
        "balance_before": balance_before,
        "mutation_results": mutation_results,
        "verification_failures": verification_failures,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "mode": report["mode"],
                "action_digest": digest,
                "action_count": len(actions),
                "issue_count": len(issues),
                "verification_failure_count": len(verification_failures),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
