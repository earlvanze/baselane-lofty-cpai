#!/usr/bin/env python3
"""Remove duplicate Citadel rows that were incorrectly attributed to 86 Madison.

Citadel services the 90 Madison mortgage; 86 Madison is serviced by Onity.
These bounded rows are residue from a deleted 2026-03 mortgage-split redo:

* orphan split children whose deleted Citadel component parent remains hidden;
* standalone copies of those erroneous children.

The real Citadel bank parents remain in Baselane, so these rows duplicate
accounting activity and must be deleted rather than relabeled to 90 Madison.
This manual-only repair requires an exact dry-run digest and verifies that bank
balances do not change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_mcp.transfers import (  # noqa: E402
    list_active_transfer_accounts,
    run_graphql_via_cdp,
)


BRIDGE = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
REPORT = ROOT / "reports" / "mislabeled_citadel_86_rows_repair.json"
CENT = Decimal("0.01")
CITADEL_PROPERTY_ID = "31525"
MSC_PROPERTY_ID = "37648"
MADISON_90_BANK_ID = "89680"
ECO_LEGACY_BANK_ID = "38968"

# id, date, amount, parent id, merchant name
ORPHAN_CHILDREN = [
    ("258418564", "2025-01-14", "-715.49", "250924516", "P+I - 86 Madison Ave"),
    ("258418634", "2025-03-10", "-715.49", "250924499", "P+I - 86 Madison Ave"),
    ("258418677", "2025-07-01", "-715.49", "250924475", "P+I - 86 Madison Ave"),
    ("258418744", "2025-07-21", "-715.49", "250924430", "P+I - 86 Madison Ave"),
    ("258418758", "2025-08-04", "-752.34", "250927443", "P+I - 86 Madison Ave"),
    ("258418760", "2025-08-04", "-715.49", "250924418", "P+I - 86 Madison Ave"),
    ("258418785", "2025-09-11", "-715.49", "250924377", "P+I - 86 Madison Ave"),
    ("258418787", "2025-09-11", "-752.34", "250927265", "P+I - 86 Madison Ave"),
    ("258418789", "2025-10-09", "-693.55", "250927209", "P+I - 86 Madison Ave"),
    ("258418819", "2025-10-09", "-693.55", "250924358", "P+I - 86 Madison Ave"),
    ("258421206", "2025-11-06", "-693.55", "250927140", "P+I - 86 Madison Ave"),
    ("258421220", "2025-11-06", "-693.55", "250924345", "P+I - 86 Madison Ave"),
    ("258421226", "2025-12-15", "-693.55", "250927118", "P+I - 86 Madison Ave"),
    ("258420979", "2026-01-14", "-834.61", "250924299", "P+I - 86 Madison Ave"),
    ("258420980", "2026-01-14", "-696.07", "250924299", "Escrow - 86 Madison Ave"),
]

# id, date, amount, merchant name. These bankless manual rows duplicate the
# bad nested children above. Some retain their source-parent note and others
# are identical copies created by the same failed redo.
STANDALONE_COPIES = [
    ("258418312", "2025-01-14", "-715.49", "86 Madison Ave P+I"),
    ("258421153", "2025-01-14", "-715.49", "86 Madison Ave P+I"),
    ("258424090", "2025-01-14", "-715.49", "86 Madison Ave P+I"),
    ("258418377", "2025-03-10", "-715.49", "86 Madison Ave P+I"),
    ("258421497", "2025-03-10", "-715.49", "86 Madison Ave P+I"),
    ("258424390", "2025-03-10", "-715.49", "86 Madison Ave P+I"),
    ("258418383", "2025-07-01", "-715.49", "86 Madison Ave P+I"),
    ("258421541", "2025-07-01", "-715.49", "86 Madison Ave P+I"),
    ("258424580", "2025-07-01", "-715.49", "86 Madison Ave P+I"),
    ("258418391", "2025-07-21", "-715.49", "86 Madison Ave P+I"),
    ("258420986", "2025-07-21", "-715.49", "86 Madison Ave P+I"),
    ("258421589", "2025-07-21", "-715.49", "86 Madison Ave P+I"),
    ("258418392", "2025-08-04", "-752.34", "86 Madison Ave P+I"),
    ("258418393", "2025-08-04", "-715.49", "86 Madison Ave P+I"),
    ("258418422", "2025-12-15", "-693.55", "86 Madison Ave P+I"),
    ("258418448", "2025-12-15", "-693.55", "86 Madison Ave P+I"),
    ("258421300", "2025-12-15", "-693.55", "86 Madison Ave P+I"),
    ("258421466", "2025-12-15", "-693.55", "86 Madison Ave P+I"),
    ("258422039", "2025-12-15", "-693.55", "86 Madison Ave P+I"),
    ("258422287", "2025-12-15", "-693.55", "86 Madison Ave P+I"),
]


def graphql(payload: dict[str, Any]) -> dict[str, Any]:
    return run_graphql_via_cdp(
        payload,
        bridge_path=BRIDGE,
        workspace_root=ROOT,
        timeout=120,
    )


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(value.get("text") or "").split())
    return " ".join(str(value or "").split())


def transaction(transaction_id: str) -> dict[str, Any]:
    response = graphql(
        {
            "operationName": "CitadelRepairTransaction",
            "variables": {"id": transaction_id},
            "query": """
              query CitadelRepairTransaction($id: ID!) {
                transactionById(id: $id) {
                  id amount date merchantName propertyId tagId bankAccountId note
                  isManual hidden isDeleted isSplit parentId
                }
              }
            """,
        }
    )
    row = response["data"]["transactionById"]
    if not row:
        raise RuntimeError(f"missing Baselane transaction {transaction_id}")
    return row


def current_balances() -> dict[str, str]:
    wanted_bank_ids = {MADISON_90_BANK_ID, ECO_LEGACY_BANK_ID}
    return {
        str(row["bank_account_id"]): str(row["available_balance"])
        for row in list_active_transfer_accounts(graphql)
        if str(row["bank_account_id"]) in wanted_bank_ids
    }


def bounded_targets() -> list[dict[str, Any]]:
    rows = [
        {
            "id": row_id,
            "date": date,
            "amount": amount,
            "parent_id": parent_id,
            "merchant_name": merchant_name,
            "kind": "orphan_child",
        }
        for row_id, date, amount, parent_id, merchant_name in ORPHAN_CHILDREN
    ]
    rows.extend(
        {
            "id": row_id,
            "date": date,
            "amount": amount,
            "parent_id": None,
            "merchant_name": merchant_name,
            "kind": "standalone_copy",
        }
        for row_id, date, amount, merchant_name in STANDALONE_COPIES
    )
    return rows


def action_digest(targets: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(targets, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_target(
    expected: dict[str, Any],
    row: dict[str, Any],
    parent_cache: dict[str, dict[str, Any]],
) -> list[str]:
    issues: list[str] = []
    row_id = expected["id"]
    if row.get("isDeleted"):
        return issues
    if str(row.get("date") or "") != expected["date"]:
        issues.append(f"date_mismatch:{row_id}")
    if money(row.get("amount") or 0) != money(expected["amount"]):
        issues.append(f"amount_mismatch:{row_id}")
    if str(row.get("merchantName") or "") != expected["merchant_name"]:
        issues.append(f"merchant_mismatch:{row_id}")
    if str(row.get("propertyId") or "") != MSC_PROPERTY_ID:
        issues.append(f"property_mismatch:{row_id}")
    if not row.get("isManual"):
        issues.append(f"not_manual:{row_id}")

    if expected["kind"] == "orphan_child":
        parent_id = expected["parent_id"]
        if str(row.get("parentId") or "") != parent_id:
            issues.append(f"parent_mismatch:{row_id}")
        if str(row.get("bankAccountId") or "") not in {
            MADISON_90_BANK_ID,
            ECO_LEGACY_BANK_ID,
        }:
            issues.append(f"bank_mismatch:{row_id}")
        if expected["merchant_name"].startswith("P+I"):
            marker = note_text(row.get("note"))
            valid_marker = (
                "AOPS-ORIGINAL-PROPERTY|86 Madison Ave" in marker
                or (
                    parent_id == "250924299"
                    and marker == "Hidden during redo of mortgage splits"
                )
            )
            if not valid_marker:
                issues.append(f"marker_mismatch:{row_id}")
        parent = parent_cache.setdefault(parent_id, transaction(parent_id))
        if not parent.get("isDeleted") or not parent.get("hidden"):
            issues.append(f"parent_not_deleted_hidden:{parent_id}")
        if "CITADEL SERV PMT" not in str(parent.get("merchantName") or ""):
            issues.append(f"parent_not_citadel:{parent_id}")
    else:
        if row.get("parentId") is not None:
            issues.append(f"standalone_has_parent:{row_id}")
        if row.get("bankAccountId") is not None:
            issues.append(f"standalone_has_bank:{row_id}")
        same_bad_child = any(
            target["kind"] == "orphan_child"
            and target["date"] == expected["date"]
            and money(target["amount"]) == money(expected["amount"])
            for target in bounded_targets()
        )
        if not same_bad_child:
            issues.append(f"no_matching_orphan_basis:{row_id}")
    return issues


def delete_rows(ids: list[str]) -> list[dict[str, Any]]:
    response = graphql(
        {
            "operationName": "DeleteMislabeledCitadelRows",
            "variables": {
                "input": [
                    {
                        "id": row_id,
                        "isDeleted": True,
                        "isReviewedByUser": True,
                    }
                    for row_id in ids
                ]
            },
            "query": """
              mutation DeleteMislabeledCitadelRows(
                $input: [UpdateTransaction!]
              ) {
                updateTransactions(input: $input) { id isDeleted }
              }
            """,
        }
    )
    return list(response["data"]["updateTransactions"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--require-action-digest")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)

    targets = bounded_targets()
    digest = action_digest(targets)
    if args.apply and args.require_action_digest != digest:
        raise RuntimeError(
            f"apply requires current action digest; current digest is {digest}"
        )

    balances_before = current_balances()
    parent_cache: dict[str, dict[str, Any]] = {}
    live_rows: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    pending_ids: list[str] = []
    for expected in targets:
        row = transaction(expected["id"])
        live_rows[expected["id"]] = row
        issues.extend(validate_target(expected, row, parent_cache))
        if not row.get("isDeleted"):
            pending_ids.append(expected["id"])

    results: list[dict[str, Any]] = []
    verification: dict[str, Any] = {}
    if args.apply and not issues and pending_ids:
        results = delete_rows(pending_ids)
        failed_result_ids = sorted(
            row_id
            for row_id in pending_ids
            if not any(
                str(result.get("id")) == row_id and result.get("isDeleted")
                for result in results
            )
        )
        remaining_ids = sorted(
            row_id
            for row_id in pending_ids
            if not transaction(row_id).get("isDeleted")
        )
        balances_after = current_balances()
        verification = {
            "failed_result_ids": failed_result_ids,
            "remaining_active_ids": remaining_ids,
            "balances_before": balances_before,
            "balances_after": balances_after,
            "balances_unchanged": balances_after == balances_before,
        }
        if failed_result_ids or remaining_ids:
            issues.append("post_apply_transaction_verification_failed")
        if balances_after != balances_before:
            issues.append("bank_balance_changed")
    else:
        verification = {
            "balances_before": balances_before,
            "already_deleted_count": len(targets) - len(pending_ids),
        }

    report = {
        "mode": "apply" if args.apply else "dry_run",
        "status": "ok" if not issues else "blocked",
        "accounting_basis": {
            "citadel_property": "90 Madison Ave",
            "citadel_property_id": CITADEL_PROPERTY_ID,
            "86_servicer": "Onity Mortgage / MortgageQuestions.com",
            "treatment": (
                "delete duplicate/mislabeled residue; do not relabel it because "
                "the real Citadel bank transactions already remain in Baselane"
            ),
        },
        "action_digest": digest,
        "target_count": len(targets),
        "pending_count": len(pending_ids),
        "issues": sorted(set(issues)),
        "targets": targets,
        "results": results,
        "verification": verification,
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
                "target_count": report["target_count"],
                "pending_count": report["pending_count"],
                "action_digest": digest,
                "issue_count": len(report["issues"]),
            },
            indent=2,
        )
    )
    return 0 if not issues else 2


if __name__ == "__main__":
    raise SystemExit(main())
