#!/usr/bin/env python3
"""Idempotently split 88 Madison's August 2025 mortgage payment.

The source statement shows a $2,002.59 regular payment plus a $43.32 late
charge.  The regular payment's escrow components follow the servicer's
statement allocation and sum to $919.81.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

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
PARENT_ID = "179396726"
PROPERTY_ID = "37648"
PARENT_TAG_ID = "33"
NOTE = (
    "88 Madison August 2025 statement-backed mortgage split: regular payment "
    "$2,002.59 (P&I $1,082.78; escrow $919.81) plus paid late charge $43.32. "
    "Late charge is ECO-reimbursable; escrow is DAO responsibility."
)
COMPONENTS = [
    ("88 Madison | mortgage principal | 2025-08", Decimal("-358.21"), "20"),
    ("88 Madison | mortgage interest | 2025-08", Decimal("-724.57"), "11"),
    ("88 Madison | general escrow | 2025-08", Decimal("-37.42"), "130"),
    ("88 Madison | city/state/local tax escrow | 2025-08", Decimal("-480.38"), "95"),
    ("88 Madison | flood escrow | 2025-08", Decimal("-154.17"), "70"),
    ("88 Madison | rental dwelling escrow | 2025-08", Decimal("-127.33"), "65"),
    ("88 Madison | PMI escrow | 2025-08", Decimal("-120.51"), "133"),
    ("88 Madison | paid mortgage late charge | 2025-08", Decimal("-43.32"), "109"),
]


def target_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "amount": cents(amount),
            "date": str(parent["date"]),
            "merchantName": label,
            "propertyId": PROPERTY_ID,
            "tagId": tag_id,
        }
        for label, amount, tag_id in COMPONENTS
    ]
    if sum((row["amount"] for row in rows), Decimal("0")) != cents(parent["amount"]):
        raise RuntimeError("statement components do not sum to the live parent")
    return rows


def build_plan() -> tuple[dict[str, Any], dict[str, Any]]:
    parent = query_parents([PARENT_ID]).get(PARENT_ID)
    issues: list[str] = []
    if not parent:
        issues.append(f"missing parent {PARENT_ID}")
        public = {"issues": issues, "parent_id": PARENT_ID}
        return public, {}
    if parent.get("isDeleted"):
        issues.append(f"parent {PARENT_ID} is deleted")
    if cents(parent.get("amount") or 0) != Decimal("-2045.91"):
        issues.append(f"unexpected parent amount {parent.get('amount')}")
    if str(parent.get("date") or "") != "2025-08-05":
        issues.append(f"unexpected parent date {parent.get('date')}")

    target = target_children(parent)
    metadata_exact = (
        str(parent.get("propertyId") or "") == PROPERTY_ID
        and str(parent.get("tagId") or "") == PARENT_TAG_ID
        and str((parent.get("note") or {}).get("text") if isinstance(parent.get("note"), dict) else parent.get("note") or "")
        == NOTE
    )
    split_exact = normalized(active_children(parent)) == normalized(target)
    public = {
        "scope": "88 Madison August 2025 mortgage payment",
        "source": "official servicer statement",
        "issues": issues,
        "parent": {
            "id": PARENT_ID,
            "date": str(parent.get("date") or ""),
            "amount": str(cents(parent.get("amount") or 0)),
            "property_id": PROPERTY_ID,
            "tag_id": PARENT_TAG_ID,
            "metadata_action": "already_exact" if metadata_exact else "update",
            "split_action": "already_exact" if split_exact else ("replace" if active_children(parent) else "create"),
            "note": NOTE,
        },
        "components": [
            {
                "label": row["merchantName"],
                "amount": str(row["amount"]),
                "property_id": row["propertyId"],
                "tag_id": row["tagId"],
            }
            for row in target
        ],
    }
    return public, {"parent": parent, "target": target, "metadata_exact": metadata_exact, "split_exact": split_exact}


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
    parser.add_argument("--digest")
    args = parser.parse_args()

    public, private = build_plan()
    plan_digest = digest(public)
    dry = {"status": "dry_run", "digest": plan_digest, **public}
    dry_path = write_report("88_aug2025_mortgage_split_dry_run.json", dry)
    if not args.apply:
        print(json.dumps({**dry, "report": str(dry_path)}, indent=2))
        return 0 if not public["issues"] else 2
    if args.digest != plan_digest:
        raise RuntimeError(f"live digest is {plan_digest}; exact --digest required")
    if public["issues"]:
        raise RuntimeError(f"refusing apply with issues: {public['issues']}")

    if not private["metadata_exact"]:
        update_transactions(
            [
                {
                    "id": PARENT_ID,
                    "propertyId": PROPERTY_ID,
                    "tagId": PARENT_TAG_ID,
                    "note": NOTE,
                }
            ]
        )
    if not private["split_exact"]:
        reconcile_parent_split(private["parent"], private["target"])

    verified, _ = build_plan()
    if (
        verified["issues"]
        or verified["parent"]["metadata_action"] != "already_exact"
        or verified["parent"]["split_action"] != "already_exact"
    ):
        raise RuntimeError(f"post-apply verification failed: {verified}")
    applied = {"status": "applied_and_verified", "digest": plan_digest, **verified}
    path = write_report("88_aug2025_mortgage_split_applied.json", applied)
    print(json.dumps({**applied, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
