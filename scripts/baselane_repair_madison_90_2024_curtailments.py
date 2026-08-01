#!/usr/bin/env python3
"""Repair the exact live Nov/Dec 2024 90 Madison curtailment chain.

The November Citadel root combines ordinary ECO P&I, DAO escrow, and the
approved October NOI principal curtailment.  The December $950 debit is the
approved November curtailment.  Positive ECO-side receipts are transfers, not
tax or insurance income/contra-expense rows.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from baselane_clean_madison_intercompany_cash import (
    active_children,
    cents,
    normalized,
    query_parents,
    reconcile_parent_split,
    update_transactions,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports"
PIPELINE_LOCK = ROOT / "scripts" / ".baselane_source_pipeline.lock"
DAO_PROPERTY_ID = "31525"
ECO_LEGACY_PROPERTY_ID = "37648"
TRANSFER_TAG_ID = "24"
MORTGAGE_PRINCIPAL_TAG_ID = "20"
PARENT_TAG_ID = "33"
MARKER = "AOPS-90-CURTAILMENT"

NOVEMBER_PARENT_ID = "121017123"
DECEMBER_CURTAILMENT_ID = "126615478"

NOVEMBER_COMPONENTS = (
    ("90 Madison | ECO ordinary mortgage principal | 2024-11", "-164.02", ECO_LEGACY_PROPERTY_ID, "20"),
    ("90 Madison | ECO mortgage interest | 2024-11", "-1605.19", ECO_LEGACY_PROPERTY_ID, "11"),
    ("90 Madison | rental dwelling escrow | 2024-11", "-97.92", DAO_PROPERTY_ID, "65"),
    ("90 Madison | city/state/local tax escrow | 2024-11", "-492.52", DAO_PROPERTY_ID, "95"),
    ("90 Madison | general escrow | 2024-11", "-210.95", DAO_PROPERTY_ID, "130"),
    ("90 Madison | approved 2024-10 NOI principal curtailment", "-1700.00", DAO_PROPERTY_ID, "20"),
)

RECEIPT_UPDATES = {
    "120348904": {
        "amount": "1700.00",
        "label": "90 Madison | transfer to ECO for approved 2024-10 principal curtailment",
        "note": (
            "AOPS-90-MORTGAGE-TRANSFER|component=principal-curtailment|"
            "recognition=2024-10|amount=1700.00 | Cash transfer into ECO; "
            "expense is represented once in Citadel root 121017123."
        ),
    },
    "120348028": {
        "amount": "492.52",
        "label": "90 Madison | transfer to ECO for city/state/local tax escrow",
        "note": (
            "AOPS-90-MORTGAGE-TRANSFER|component=tax-escrow|"
            "payment=2024-11|amount=492.52 | Cash transfer into ECO; expense "
            "is represented once in Citadel root 121017123."
        ),
    },
    "120347855": {
        "amount": "97.92",
        "label": "90 Madison | transfer to ECO for rental dwelling escrow",
        "note": (
            "AOPS-90-MORTGAGE-TRANSFER|component=insurance-escrow|"
            "payment=2024-11|amount=97.92 | Cash transfer into ECO; expense "
            "is represented once in Citadel root 121017123."
        ),
    },
}


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def november_parent_note() -> str:
    return (
        "90 Madison statement-backed native mortgage split for 2024-11. "
        "Ordinary P&I is ECO responsibility; escrow and the approved 2024-10 "
        "NOI principal curtailment are DAO expenses."
    )


def november_curtailment_note() -> str:
    return (
        f"{MARKER}|recognition=2024-10|amount=1700.00 | Approved 50% NOI "
        "principal curtailment; bank-posted 2024-11-12. Ordinary mortgage "
        "P&I remains ECO responsibility."
    )


def december_curtailment_note() -> str:
    return (
        f"{MARKER}|recognition=2024-11|amount=950.00 | Approved 50% NOI "
        "principal curtailment; bank-posted 2024-12-16. Ordinary mortgage "
        "P&I remains ECO responsibility."
    )


def query_exact_rows(ids: list[str]) -> dict[str, dict[str, Any]]:
    # query_parents returns all fields needed for both roots and standalone rows.
    return query_parents(ids)


def target_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "amount": cents(amount),
            "date": str(parent["date"]),
            "merchantName": label,
            "propertyId": property_id,
            "tagId": tag_id,
        }
        for label, amount, property_id, tag_id in NOVEMBER_COMPONENTS
    ]
    if sum((row["amount"] for row in rows), Decimal("0.00")) != cents(parent["amount"]):
        raise RuntimeError("November components do not sum to the bank root")
    return rows


def child_note_update(parent: dict[str, Any], target: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wanted = next(row for row in target if "approved" in row["merchantName"].lower())
    matches = [row for row in active_children(parent) if normalized([row]) == normalized([wanted])]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one November curtailment child, found {len(matches)}"
        )
    desired = november_curtailment_note()
    if note_text(matches[0].get("note")) == desired:
        return []
    return [{"id": str(matches[0]["id"]), "note": desired}]


def build_plan() -> tuple[dict[str, Any], dict[str, Any]]:
    ids = [NOVEMBER_PARENT_ID, DECEMBER_CURTAILMENT_ID, *RECEIPT_UPDATES]
    rows = query_exact_rows(ids)
    issues: list[str] = []
    for row_id in ids:
        if row_id not in rows:
            issues.append(f"missing:{row_id}")
            continue
        row = rows[row_id]
        if row.get("isDeleted"):
            issues.append(f"deleted:{row_id}")

    parent = rows.get(NOVEMBER_PARENT_ID)
    target: list[dict[str, Any]] = []
    parent_metadata_exact = False
    split_exact = False
    child_note_exact = False
    if parent:
        if cents(parent.get("amount")) != Decimal("-4270.60"):
            issues.append(f"amount_mismatch:{NOVEMBER_PARENT_ID}:{parent.get('amount')}")
        if str(parent.get("date") or "") != "2024-11-12":
            issues.append(f"date_mismatch:{NOVEMBER_PARENT_ID}:{parent.get('date')}")
        if str(parent.get("bankAccountId") or "") != "38968":
            issues.append(f"bank_mismatch:{NOVEMBER_PARENT_ID}:{parent.get('bankAccountId')}")
        target = target_children(parent)
        parent_metadata_exact = (
            str(parent.get("propertyId") or "") == DAO_PROPERTY_ID
            and str(parent.get("tagId") or "") == PARENT_TAG_ID
            and note_text(parent.get("note")) == november_parent_note()
        )
        split_exact = normalized(active_children(parent)) == normalized(target)
        child_note_exact = split_exact and not child_note_update(parent, target)

    standalone_actions: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    december = rows.get(DECEMBER_CURTAILMENT_ID)
    if december:
        if cents(december.get("amount")) != Decimal("-950.00"):
            issues.append(
                f"amount_mismatch:{DECEMBER_CURTAILMENT_ID}:{december.get('amount')}"
            )
        if str(december.get("date") or "") != "2024-12-16":
            issues.append(
                f"date_mismatch:{DECEMBER_CURTAILMENT_ID}:{december.get('date')}"
            )
        if str(december.get("bankAccountId") or "") != "89680":
            issues.append(
                f"bank_mismatch:{DECEMBER_CURTAILMENT_ID}:{december.get('bankAccountId')}"
            )
        desired = {
            "id": DECEMBER_CURTAILMENT_ID,
            "propertyId": DAO_PROPERTY_ID,
            "tagId": MORTGAGE_PRINCIPAL_TAG_ID,
            "merchantName": "90 Madison | approved 2024-11 NOI principal curtailment",
            "note": december_curtailment_note(),
            "isReviewedByUser": True,
        }
        exact = (
            str(december.get("propertyId") or "") == desired["propertyId"]
            and str(december.get("tagId") or "") == desired["tagId"]
            and str(december.get("merchantName") or "") == desired["merchantName"]
            and note_text(december.get("note")) == desired["note"]
        )
        standalone_actions.append(
            {"id": DECEMBER_CURTAILMENT_ID, "kind": "curtailment", "action": "none" if exact else "update"}
        )
        if not exact:
            updates.append(desired)

    for row_id, spec in RECEIPT_UPDATES.items():
        row = rows.get(row_id)
        if not row:
            continue
        if cents(row.get("amount")) != cents(spec["amount"]):
            issues.append(f"amount_mismatch:{row_id}:{row.get('amount')}")
        desired = {
            "id": row_id,
            "propertyId": DAO_PROPERTY_ID,
            "tagId": TRANSFER_TAG_ID,
            "merchantName": spec["label"],
            "note": spec["note"],
            "isReviewedByUser": True,
        }
        exact = (
            str(row.get("propertyId") or "") == desired["propertyId"]
            and str(row.get("tagId") or "") == desired["tagId"]
            and str(row.get("merchantName") or "") == desired["merchantName"]
            and note_text(row.get("note")) == desired["note"]
        )
        standalone_actions.append(
            {"id": row_id, "kind": "transfer_receipt", "action": "none" if exact else "update"}
        )
        if not exact:
            updates.append(desired)

    public = {
        "scope": "exact live Nov/Dec 2024 90 Madison mortgage chain",
        "accounting_policy": (
            "Ordinary P&I is ECO responsibility. DAO escrow and approved "
            "principal curtailments are expenses exactly once. ECO-side "
            "receipts are transfers and cannot be tax/insurance rows."
        ),
        "issues": issues,
        "november_root": {
            "id": NOVEMBER_PARENT_ID,
            "metadata_action": "none" if parent_metadata_exact else "update",
            "split_action": (
                "none"
                if split_exact
                else ("replace" if parent and active_children(parent) else "create")
            ),
            "curtailment_note_action": "none" if child_note_exact else "update_after_split",
            "components": [
                {
                    "merchant": row["merchantName"],
                    "amount": str(row["amount"]),
                    "property_id": row["propertyId"],
                    "tag_id": row["tagId"],
                }
                for row in target
            ],
        },
        "standalone_actions": standalone_actions,
    }
    private = {
        "parent": parent,
        "target": target,
        "parent_metadata_exact": parent_metadata_exact,
        "split_exact": split_exact,
        "updates": updates,
    }
    return public, private


def digest(public: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@contextmanager
def pipeline_lock(enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    with PIPELINE_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def apply(private: dict[str, Any]) -> None:
    parent_updates: list[dict[str, Any]] = []
    if not private["parent_metadata_exact"]:
        parent_updates.append(
            {
                "id": NOVEMBER_PARENT_ID,
                "propertyId": DAO_PROPERTY_ID,
                "tagId": PARENT_TAG_ID,
                "note": november_parent_note(),
                "isReviewedByUser": True,
            }
        )
    update_transactions([*parent_updates, *private["updates"]])
    if not private["split_exact"]:
        reconcile_parent_split(private["parent"], private["target"])
    refreshed = query_parents([NOVEMBER_PARENT_ID])[NOVEMBER_PARENT_ID]
    update_transactions(child_note_update(refreshed, private["target"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()
    if args.apply and not args.digest:
        parser.error("--apply requires --digest")

    with pipeline_lock(args.apply):
        public, private = build_plan()
        plan_digest = digest(public)
        mode = "preview"
        if args.apply:
            if args.digest != plan_digest:
                raise RuntimeError(
                    f"live digest changed: expected {args.digest}, current {plan_digest}"
                )
            if public["issues"]:
                raise RuntimeError(f"refusing apply with issues: {public['issues']}")
            apply(private)
            public, _ = build_plan()
            pending = (
                public["november_root"]["metadata_action"] != "none"
                or public["november_root"]["split_action"] != "none"
                or public["november_root"]["curtailment_note_action"] != "none"
                or any(row["action"] != "none" for row in public["standalone_actions"])
            )
            if public["issues"] or pending:
                raise RuntimeError(f"post-apply verification failed: {public}")
            mode = "applied_and_verified"

    report = {
        "status": "ok" if not public["issues"] else "blocked",
        "mode": mode,
        "digest": plan_digest,
        **public,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / (
        "madison_90_2024_curtailment_repair_"
        + ("applied.json" if args.apply else "preview.json")
    )
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "report": str(path)}, indent=2))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
