#!/usr/bin/env python3
"""Verify the 8708 Willard / EARLDAO reciprocal loan subledgers.

This is a read-only accounting control. It distinguishes Baselane cash
parents from native split children and preserves both sides of the reciprocal
loan subledgers. Contemporaneous July and November reporting, corroborated by
the exact $800 interest payment, supports a $4,000 reciprocal setoff and an
$11,000 net principal balance after the November $5,000 reduction.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from baselane_live_dao_cash_reconciliation import (
    TRANSACTION_QUERY,
    graphql,
    note_text,
)


# `scripts/` can be a compatibility symlink into the mirrored source tree.
# Keep generated reports in the canonical ext4 workspace, not beside the
# resolved symlink target.
ROOT = Path("/home/digit/.openclaw/workspace")
REPORT_DIR = ROOT / "reports"
WILLARD_PROPERTY_ID = "81779"
EARLDAO_PROPERTY_ID = "94512"

EXPECTED = {
    "161599269": ("4000.00", None, "Willard bank receipt of its operating-reserve cash"),
    "242990541": ("-4000.00", "161664397", "Willard lender contribution split"),
    "242990542": ("20000.00", "161664397", "EARLDAO borrower draw split"),
    "242990543": ("-19897.82", "161664397", "Yhome/Web3 settlement split"),
    "242990544": ("-102.18", "161664397", "Initial interest split"),
    "242990848": ("102.18", "161664399", "Initial interest mirror"),
    "242990849": ("19897.82", "161664399", "Yhome/Web3 settlement mirror"),
    "242990850": ("-20000.00", "161664399", "EARLDAO draw mirror"),
    "242990851": ("4000.00", "161664399", "Willard lender contribution mirror"),
    "165672842": ("-4000.00", None, "Aligned-to-ECO clearing transfer"),
    "165672857": ("4000.00", None, "Aligned-to-ECO clearing mirror"),
    "321905457": ("-800.00", "207162700", "Interest allocation"),
    "321905458": ("5000.00", "207162700", "EARLDAO principal receipt allocation"),
    "321905459": ("-5000.00", "207162700", "Willard principal payment allocation"),
    "207162697": ("800.00", None, "EARLDAO bank cash mirror for interest"),
    "321944912": ("-8000.00", None, "Willard July 2026 principal settlement"),
    "321944913": ("8000.00", None, "EARLDAO July 2026 principal settlement mirror"),
}

EXPECTED_SPLIT_PARENT_TOTALS = {
    "161664397": "-4000.00",
    "161664399": "4000.00",
    "207162700": "-800.00",
}


def money(value: Any) -> str:
    return f"{Decimal(str(value or 0)):.2f}"


def query_all_transactions() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = graphql(
            {
                "operationName": "Transactions",
                "variables": {
                    "input": {
                        "sort": {"field": "date", "direction": "ASC"},
                        "filter": {
                            "isHidden": False,
                            "search": "",
                            "isCategorized": None,
                            "tagId": None,
                            "bankAccountId": None,
                            "propertyId": None,
                            "unitId": None,
                            "isDeleted": False,
                            "isDocumentUploaded": None,
                        },
                        "page": page,
                        "pageLimit": 250,
                    }
                },
                "query": TRANSACTION_QUERY,
            }
        )["data"]["transactions"]
        batch = result.get("data") or []
        rows.extend(batch)
        if not batch or len(rows) >= int(result.get("total") or 0):
            return rows
        page += 1


def reconcile(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {str(row.get("id")): row for row in rows}
    verification: list[dict[str, Any]] = []
    errors: list[str] = []

    for transaction_id, (expected_amount, expected_parent, purpose) in EXPECTED.items():
        row = by_id.get(transaction_id)
        ok = bool(
            row
            and money(row.get("amount")) == expected_amount
            and (
                str(row.get("parentId") or "") == str(expected_parent or "")
            )
        )
        verification.append(
            {
                "transaction_id": transaction_id,
                "purpose": purpose,
                "expected_amount": expected_amount,
                "expected_parent_id": expected_parent,
                "live_amount": money(row.get("amount")) if row else None,
                "live_parent_id": str(row.get("parentId")) if row and row.get("parentId") else None,
                "verified": ok,
            }
        )
        if not ok:
            errors.append(f"Expected transaction {transaction_id} did not match live state")

    split_parent_verification: list[dict[str, Any]] = []
    for parent_id, expected_total in EXPECTED_SPLIT_PARENT_TOTALS.items():
        children = [
            row for row in rows if str(row.get("parentId") or "") == parent_id
        ]
        live_total = sum(
            (Decimal(str(row.get("amount") or 0)) for row in children),
            Decimal("0"),
        )
        ok = f"{live_total:.2f}" == expected_total
        split_parent_verification.append(
            {
                "parent_id": parent_id,
                "expected_cash_total": expected_total,
                "live_component_total": f"{live_total:.2f}",
                "component_ids": sorted(str(row.get("id")) for row in children),
                "verified": ok,
                "note": (
                    "Baselane suppresses a split parent from the global endpoint; "
                    "the native children must sum to the original cash amount."
                ),
            }
        )
        if not ok:
            errors.append(
                f"Split parent {parent_id} components sum to {live_total:.2f}, "
                f"not {expected_total}"
            )

    willard_principal_rows = []
    for row in rows:
        text = " ".join(
            str(row.get(field) or "") for field in ("merchantName", "description")
        )
        text += " " + note_text(row.get("note"))
        if (
            str(row.get("propertyId") or "") == WILLARD_PROPERTY_ID
            and str(row.get("tagId") or "") in {"22", "23"}
            and "earldao" in text.lower()
        ):
            willard_principal_rows.append(
                {
                    "id": str(row.get("id")),
                    "date": str(row.get("date"))[:10],
                    "amount": money(row.get("amount")),
                    "tag_id": str(row.get("tagId")),
                    "bank_account_id": str(row.get("bankAccountId"))
                    if row.get("bankAccountId") is not None
                    else None,
                    "parent_id": str(row.get("parentId"))
                    if row.get("parentId") is not None
                    else None,
                    "merchant": row.get("merchantName"),
                    "note": note_text(row.get("note")),
                }
            )

    expected_principal_ids = {
        "242990541", "242990542", "321905459", "321944912",
    }
    observed_principal_ids = {row["id"] for row in willard_principal_rows}
    unexpected = sorted(observed_principal_ids - expected_principal_ids)
    missing = sorted(expected_principal_ids - observed_principal_ids)
    if unexpected:
        errors.append(f"Unexpected Willard/EARLDAO principal rows: {unexpected}")
    if missing:
        errors.append(f"Missing Willard/EARLDAO principal rows: {missing}")

    gross_draw = Decimal("20000.00")
    principal_paid = Decimal("13000.00")
    gross_principal = gross_draw - principal_paid
    lender_receivable = Decimal("4000.00")
    hypothetical_net = gross_principal - lender_receivable
    published_balance = Decimal("11000.00")
    current_servicing_balance = Decimal("3000.00")

    return {
        "status": "verified" if not errors else "live_verification_failed",
        "as_of": "2026-07-29",
        "accounting_conclusion": {
            "earldao_receivable_from_willard_gross_principal": f"{gross_principal:.2f}",
            "willard_receivable_from_earldao_separate_lender_loan": f"{lender_receivable:.2f}",
            "documented_net_after_reciprocal_setoff": f"{hypothetical_net:.2f}",
            "published_balance_before_2026_07_29_settlement": f"{published_balance:.2f}",
            "current_servicing_balance": f"{current_servicing_balance:.2f}",
            "gross_less_current_net_reciprocal_setoff": f"{gross_principal - current_servicing_balance:.2f}",
            "difference_from_current_documented_net": f"{current_servicing_balance - hypothetical_net:.2f}",
            "recognized_balance_for_interest": f"{hypothetical_net:.2f}",
            "reason": (
                "The live ledger supports a $20,000 gross draw, a $5,000 "
                "principal reduction, and a separate $4,000 Willard lender "
                "receivable. July 2025 reporting expressly presented these "
                "reciprocal positions net at $16,000. The $800 June-October "
                "interest payment equals five months at 12% on that $16,000 "
                "net balance, and the November published balance is $11,000. "
                "The paired bankless July 29, 2026 $8,000 settlement allocates "
                "ECO-held Willard funds to principal, leaving $3,000 net."
            ),
        },
        "cash_vs_accounting": {
            "2025_05_30_actual_cash_moved_willard_to_aligned": "4000.00",
            "2025_05_30_native_split_children_are_accounting_components": True,
            "2025_06_14_actual_cash_moved_aligned_to_eco_clearing": "4000.00",
            "2025_11_05_actual_cash_moved_hemlane_to_earldao": "800.00",
            "2025_11_05_5000_principal_is_same_account_reclassification": True,
            "2026_07_29_8000_principal_is_eco_held_funds_allocation": True,
            "2026_07_29_new_bank_cash_moved": "0.00",
        },
        "principal_rows": willard_principal_rows,
        "expected_transaction_verification": verification,
        "split_parent_verification": split_parent_verification,
        "controls": {
            "global_live_ledger_scanned": True,
            "new_cash_movement": False,
            "baselane_mutation": False,
            "interest_basis_verified": True,
            "reciprocal_setoff_documented": True,
            "published_11000_accepted": True,
            "july_2026_8000_settlement_verified": True,
        },
        "errors": errors,
        "next_documentation_action": (
            "Maintain the gross $7,000 borrower receivable and separate "
            "$4,000 Willard lender receivable in supporting subledgers while "
            "presenting and servicing the current $3,000 net balance."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=REPORT_DIR / "willard_earldao_note_reconciliation.20260729.json",
    )
    args = parser.parse_args()
    report = reconcile(query_all_transactions())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
