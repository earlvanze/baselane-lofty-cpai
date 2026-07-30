#!/usr/bin/env python3
"""Correct and verify Willard's 2025-11-05 EARLDAO payment split.

The actual cash was $800. Contemporaneous reporting establishes $16,000 net
principal in July 2025 and $11,000 on November 5, so the native split must
carry offsetting $5,000 principal components, not $7,000. The $800 is exactly
five months of interest at 12% on $16,000 ($160/month).
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

ROOT = Path("/home/digit/.openclaw/workspace")
REPORT_DIR = ROOT / "reports"
CASH_PARENT_ID = "207162700"
CASH_MIRROR_ID = "207162697"
WILLARD_PROPERTY_ID = "81779"
EARLDAO_PROPERTY_ID = "94512"


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(value.get("text") or "").split())
    return " ".join(str(value or "").split())


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "id", "amount", "date", "merchantName", "propertyId", "tagId",
            "bankAccountId", "isSplit", "parentId", "isDeleted", "note",
        )
    }


def query_full(transaction_id: str) -> dict[str, Any]:
    response = graphql({
        "operationName": "WillardEarldaoCorrectionRow",
        "variables": {"id": transaction_id},
        "query": """
        query WillardEarldaoCorrectionRow($id: ID!) {
          transactionById(id: $id) {
            id amount date merchantName propertyId tagId bankAccountId note
            isSplit isDeleted parentId
            splitTransactions {
              id amount date merchantName propertyId tagId bankAccountId note
              isSplit isDeleted parentId
            }
          }
        }
        """,
    })
    row = response["data"]["transactionById"]
    if not row:
        raise RuntimeError(f"Baselane transaction {transaction_id} was not found")
    return row


def query_rows() -> tuple[dict[str, Any], dict[str, Any]]:
    return query_full(CASH_PARENT_ID), query_full(CASH_MIRROR_ID)


def desired_children(_parent: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "amount": Decimal("-800.00"),
            "date": "2025-11-05",
            "merchantName": "Willard -> EARLDAO | interest Jun-Oct 2025 | $800",
            "propertyId": WILLARD_PROPERTY_ID,
            "tagId": "12",
        },
        {
            "amount": Decimal("5000.00"),
            "date": "2025-11-05",
            "merchantName": "EARLDAO | Willard principal receipt allocation | $5,000",
            "propertyId": EARLDAO_PROPERTY_ID,
            "tagId": "23",
        },
        {
            "amount": Decimal("-5000.00"),
            "date": "2025-11-05",
            "merchantName": "Willard -> EARLDAO | principal payment allocation | $5,000",
            "propertyId": WILLARD_PROPERTY_ID,
            "tagId": "22",
        },
    ]


def parent_updates() -> list[dict[str, Any]]:
    return [
        {
            "id": CASH_PARENT_ID,
            "merchantName": (
                "Willard -> EARLDAO | $800 interest cash + $5,000 principal "
                "allocation | 2025-11-05"
            ),
            "propertyId": WILLARD_PROPERTY_ID,
            "tagId": "12",
            "note": (
                "Actual cash moved: $800 interest for June-October 2025. "
                "Native split also contains offsetting +/-$5,000 noncash "
                "principal subledger components, reducing documented net "
                "principal from $16,000 to Lofty's published $11,000. "
                "No additional principal cash moved in this parent."
            ),
        },
        {
            "id": CASH_MIRROR_ID,
            "merchantName": "EARLDAO <- Willard | interest Jun-Oct 2025 | $800",
            "propertyId": EARLDAO_PROPERTY_ID,
            "tagId": "2",
            "note": (
                "EARLDAO cash receipt of $800 interest for June-October 2025: "
                "five months at $160/month on Willard's documented $16,000 "
                "net principal."
            ),
        },
    ]


def desired_child_notes() -> dict[tuple[str, str, str], str]:
    return {
        ("-800.00", WILLARD_PROPERTY_ID, "12"): (
            "Actual Willard cash interest paid to EARLDAO for June-October "
            "2025: five months at $160/month, equal to 12% annual interest "
            "on the documented $16,000 net principal."
        ),
        ("5000.00", EARLDAO_PROPERTY_ID, "23"): (
            "Noncash principal receipt allocation inside the $800 cash "
            "interest parent. July 2025 reporting established $16,000 net "
            "principal and Lofty's 2025-11-05 update published $11,000 due; "
            "therefore the supported principal reduction is $5,000."
        ),
        ("-5000.00", WILLARD_PROPERTY_ID, "22"): (
            "Noncash Willard principal payment allocation inside the $800 "
            "cash interest parent. Reduces documented net principal from "
            "$16,000 to $11,000; this component creates no additional cash."
        ),
    }


def update_transactions(updates: list[dict[str, Any]]) -> None:
    response = graphql({
        "operationName": "UpdateTransaction",
        "variables": {"input": updates},
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id merchantName propertyId tagId note
          }
        }
        """,
    })
    returned = response["data"]["updateTransactions"]
    if {str(row["id"]) for row in returned} != {str(row["id"]) for row in updates}:
        raise RuntimeError("Baselane did not confirm every requested update")


def validate_source(parent: dict[str, Any], mirror: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    actual_parent = (
        cents(parent.get("amount") or 0), str(parent.get("date") or ""),
        str(parent.get("bankAccountId") or ""), parent.get("parentId"),
        bool(parent.get("isDeleted")),
    )
    expected_parent = (Decimal("-800.00"), "2025-11-05", "114841", None, False)
    if actual_parent != expected_parent:
        issues.append(f"cash parent changed: {actual_parent}")
    actual_mirror = (
        cents(mirror.get("amount") or 0), str(mirror.get("date") or ""),
        str(mirror.get("bankAccountId") or ""), mirror.get("parentId"),
        bool(mirror.get("isDeleted")),
    )
    expected_mirror = (Decimal("800.00"), "2025-11-05", "131545", None, False)
    if actual_mirror != expected_mirror:
        issues.append(f"cash mirror changed: {actual_mirror}")
    return issues


def plan_payload(
    parent: dict[str, Any], mirror: dict[str, Any], issues: list[str]
) -> dict[str, Any]:
    return {
        "scope": "8708 Willard / EARLDAO 2025-11-05 split correction",
        "status": "dry_run",
        "issues": issues,
        "cash_movement_created": False,
        "actual_cash": {"amount": "800.00", "purpose": "interest Jun-Oct 2025"},
        "supported_principal_sequence": {
            "net_principal_july_2025": "16000.00",
            "principal_reduction_2025_11_05": "5000.00",
            "published_balance_2025_11_05": "11000.00",
        },
        "interest_crosscheck": {
            "annual_rate": "12.00%",
            "monthly_interest_on_16000": "160.00",
            "months": ["2025-06", "2025-07", "2025-08", "2025-09", "2025-10"],
            "total": "800.00",
        },
        "source": {"cash_parent": public_row(parent), "cash_mirror": public_row(mirror)},
        "desired_parent_updates": parent_updates(),
        "desired_native_split": [
            {**row, "amount": format(row["amount"], ".2f")}
            for row in desired_children(parent)
        ],
        "controls": {
            "corrects_7000_to_5000": True,
            "corrects_interest_property_from_eco_to_willard": True,
            "corrects_interest_tag_from_baselane_bank_interest": True,
            "native_children_sum_to_cash_parent": True,
            "no_new_transfer": True,
        },
    }


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_report(name: str, payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def apply_child_notes(parent: dict[str, Any]) -> None:
    wanted = desired_child_notes()
    updates: list[dict[str, Any]] = []
    for child in parent.get("splitTransactions") or []:
        if child.get("isDeleted"):
            continue
        key = (
            format(cents(child.get("amount") or 0), ".2f"),
            str(child.get("propertyId") or ""),
            str(child.get("tagId") or ""),
        )
        if key in wanted:
            updates.append({"id": str(child["id"]), "note": wanted[key]})
    if len(updates) != 3:
        raise RuntimeError(f"expected three split children, found {len(updates)}")
    update_transactions(updates)


def verify() -> dict[str, Any]:
    parent, mirror = query_rows()
    issues = validate_source(parent, mirror)
    expected_updates = {row["id"]: row for row in parent_updates()}
    for row in (parent, mirror):
        expected = expected_updates[str(row["id"])]
        for field in ("merchantName", "propertyId", "tagId"):
            if str(row.get(field) or "") != str(expected[field]):
                issues.append(f"{row['id']} {field} does not match")
        if note_text(row.get("note")) != note_text(expected["note"]):
            issues.append(f"{row['id']} note does not match")
    if normalized_children(
        parent.get("splitTransactions") or []
    ) != normalized_children(desired_children(parent)):
        issues.append("native split does not match supported $5,000 plan")
    expected_notes = desired_child_notes()
    observed: set[tuple[str, str, str]] = set()
    for child in parent.get("splitTransactions") or []:
        if child.get("isDeleted"):
            continue
        key = (
            format(cents(child.get("amount") or 0), ".2f"),
            str(child.get("propertyId") or ""),
            str(child.get("tagId") or ""),
        )
        if key in expected_notes:
            observed.add(key)
            if note_text(child.get("note")) != note_text(expected_notes[key]):
                issues.append(f"split child {child['id']} note does not match")
    if observed != set(expected_notes):
        issues.append("not all expected split children were found")
    return {
        "status": "verified" if not issues else "verification_failed",
        "issues": issues,
        "cash_movement_created": False,
        "net_principal_after_correction": "11000.00",
        "cash_parent": public_row(parent),
        "cash_mirror": public_row(mirror),
        "native_children": [
            public_row(row) for row in parent.get("splitTransactions") or []
            if not row.get("isDeleted")
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        result = verify()
        path = write_report("willard_earldao_split_correction_verify.20260729.json", result)
        print(json.dumps({**result, "report": str(path)}, indent=2))
        return 0 if result["status"] == "verified" else 2
    parent, mirror = query_rows()
    issues = validate_source(parent, mirror)
    plan = plan_payload(parent, mirror, issues)
    plan_digest = digest(plan)
    dry_run = {"digest": plan_digest, **plan}
    path = write_report("willard_earldao_split_correction_dry_run.20260729.json", dry_run)
    if not args.apply:
        print(json.dumps({**dry_run, "report": str(path)}, indent=2))
        return 0 if not issues else 2
    if args.digest != plan_digest:
        raise RuntimeError(f"live digest is {plan_digest}; exact --digest required")
    if issues:
        raise RuntimeError(f"refusing apply with issues: {issues}")
    update_transactions(parent_updates())
    parent = query_parent(CASH_PARENT_ID)
    if normalized_children(
        parent.get("splitTransactions") or []
    ) != normalized_children(desired_children(parent)):
        reconcile_parent_split(parent, desired_children(parent))
    apply_child_notes(query_full(CASH_PARENT_ID))
    result = verify()
    if result["status"] != "verified":
        raise RuntimeError(f"post-apply verification failed: {result['issues']}")
    applied = {
        **plan, "status": "applied_and_verified", "digest": plan_digest,
        "verification": result,
    }
    applied_path = write_report("willard_earldao_split_correction_apply.20260729.json", applied)
    print(json.dumps({**applied, "report": str(applied_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
