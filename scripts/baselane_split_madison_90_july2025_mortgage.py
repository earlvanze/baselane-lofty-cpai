#!/usr/bin/env python3
"""Idempotently split 90 Madison's two July-posted 2025 Citadel payments.

The July 1 payment contains the approved May NOI principal curtailment.  The
July 21 payment contains the approved June curtailment; it is not a July NOI
curtailment.  Ordinary P&I and the paid fee remain ECO responsibility, while
escrow and the two approved curtailments remain 90 Madison DAO expenses.
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
DAO_BANK_ID = "89680"
PARENT_TAG_ID = "33"
CENT = Decimal("0.01")
MARKER = "AOPS-90-CURTAILMENT"

SPECS = {
    "170408632": {
        "date": "2025-07-01",
        "amount": "-2820.60",
        "payment_month": "2025-06",
        "curtailment_month": "2025-05",
        "components": (
            ("90 Madison | ECO ordinary mortgage principal | 2025-06", "-216.59", ECO_LEGACY_PROPERTY_ID, "20"),
            ("90 Madison | ECO mortgage interest | 2025-06", "-1552.62", ECO_LEGACY_PROPERTY_ID, "11"),
            ("90 Madison | rental dwelling escrow | 2025-06", "-97.92", DAO_PROPERTY_ID, "65"),
            ("90 Madison | city/state/local tax escrow | 2025-06", "-442.47", DAO_PROPERTY_ID, "95"),
            ("90 Madison | general escrow | 2025-06", "-261.00", DAO_PROPERTY_ID, "130"),
            ("90 Madison | approved May NOI principal curtailment", "-250.00", DAO_PROPERTY_ID, "20"),
        ),
    },
    "175660215": {
        "date": "2025-07-21",
        "amount": "-3355.98",
        "payment_month": "2025-07",
        "curtailment_month": "2025-06",
        "components": (
            ("90 Madison | ECO ordinary mortgage principal | 2025-07", "-220.53", ECO_LEGACY_PROPERTY_ID, "20"),
            ("90 Madison | ECO mortgage interest | 2025-07", "-1548.68", ECO_LEGACY_PROPERTY_ID, "11"),
            ("90 Madison | rental dwelling escrow | 2025-07", "-97.92", DAO_PROPERTY_ID, "65"),
            ("90 Madison | city/state/local tax escrow | 2025-07", "-442.47", DAO_PROPERTY_ID, "95"),
            ("90 Madison | general escrow | 2025-07", "-261.00", DAO_PROPERTY_ID, "130"),
            ("90 Madison | ECO paid mortgage fee | 2025-07", "-35.38", ECO_LEGACY_PROPERTY_ID, "33"),
            ("90 Madison | approved June NOI principal curtailment", "-750.00", DAO_PROPERTY_ID, "20"),
        ),
    },
}


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def parent_note(spec: dict[str, Any]) -> str:
    return (
        "90 Madison statement-backed native mortgage split. Ordinary P&I and "
        "fees are ECO responsibility; insurance, taxes, general escrow, and "
        f"the approved {spec['curtailment_month']} NOI curtailment are DAO "
        "expenses. July 2025 NOI curtailment is $0.00."
    )


def curtailment_note(spec: dict[str, Any], amount: Decimal) -> str:
    return (
        f"{MARKER}|recognition={spec['curtailment_month']}|"
        f"amount={abs(amount):.2f} | Approved 50% NOI principal curtailment; "
        f"bank-posted {spec['date']}. July 2025 NOI curtailment is $0.00."
    )


def target_children(parent: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "amount": cents(amount),
            "date": str(parent["date"]),
            "merchantName": label,
            "propertyId": property_id,
            "tagId": tag_id,
        }
        for label, amount, property_id, tag_id in spec["components"]
    ]
    if sum((row["amount"] for row in rows), Decimal("0.00")) != cents(parent["amount"]):
        raise RuntimeError(f"components do not sum to parent {parent['id']}")
    return rows


def child_note_updates(
    parent: dict[str, Any], spec: dict[str, Any], target: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    live = active_children(parent)
    updates: list[dict[str, Any]] = []
    for wanted in target:
        if "approved" not in wanted["merchantName"].lower():
            continue
        matches = [
            row
            for row in live
            if normalized([row]) == normalized([wanted])
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"expected one curtailment child for parent {parent['id']}, got {len(matches)}"
            )
        desired = curtailment_note(spec, wanted["amount"])
        if note_text(matches[0].get("note")) != desired:
            updates.append({"id": str(matches[0]["id"]), "note": desired})
    return updates


def build_plan() -> tuple[dict[str, Any], dict[str, Any]]:
    parents = query_parents(list(SPECS))
    issues: list[str] = []
    actions: list[dict[str, Any]] = []
    targets: dict[str, list[dict[str, Any]]] = {}
    metadata_exact: dict[str, bool] = {}
    split_exact: dict[str, bool] = {}
    notes_exact: dict[str, bool] = {}
    for parent_id, spec in SPECS.items():
        parent = parents.get(parent_id)
        if not parent:
            issues.append(f"missing_parent:{parent_id}")
            continue
        if parent.get("isDeleted"):
            issues.append(f"deleted_parent:{parent_id}")
        if cents(parent.get("amount") or 0) != cents(spec["amount"]):
            issues.append(f"amount_mismatch:{parent_id}:{parent.get('amount')}")
        if str(parent.get("date") or "") != spec["date"]:
            issues.append(f"date_mismatch:{parent_id}:{parent.get('date')}")
        if str(parent.get("bankAccountId") or "") != DAO_BANK_ID:
            issues.append(f"bank_mismatch:{parent_id}:{parent.get('bankAccountId')}")

        target = target_children(parent, spec)
        targets[parent_id] = target
        metadata_exact[parent_id] = (
            str(parent.get("propertyId") or "") == DAO_PROPERTY_ID
            and str(parent.get("tagId") or "") == PARENT_TAG_ID
            and note_text(parent.get("note")) == parent_note(spec)
        )
        split_exact[parent_id] = normalized(active_children(parent)) == normalized(target)
        note_updates: list[dict[str, Any]] = []
        if split_exact[parent_id]:
            note_updates = child_note_updates(parent, spec, target)
        notes_exact[parent_id] = split_exact[parent_id] and not note_updates
        actions.append(
            {
                "parent_id": parent_id,
                "date": spec["date"],
                "amount": spec["amount"],
                "payment_month": spec["payment_month"],
                "curtailment_recognition_month": spec["curtailment_month"],
                "metadata_action": "none" if metadata_exact[parent_id] else "update",
                "split_action": (
                    "none"
                    if split_exact[parent_id]
                    else ("replace" if active_children(parent) else "create")
                ),
                "curtailment_note_action": "none" if notes_exact[parent_id] else "update_after_split",
                "components": [
                    {
                        "merchant": row["merchantName"],
                        "amount": str(row["amount"]),
                        "property_id": row["propertyId"],
                        "tag_id": row["tagId"],
                    }
                    for row in target
                ],
            }
        )
    public = {
        "scope": "90 Madison July-posted 2025 Citadel mortgage roots",
        "accounting_policy": (
            "Ordinary P&I and paid mortgage fees are ECO responsibility. "
            "Escrow and approved May/June NOI principal curtailments are 90 "
            "Madison DAO expenses. July 2025 NOI curtailment is zero."
        ),
        "issues": issues,
        "actions": actions,
    }
    private = {
        "parents": parents,
        "targets": targets,
        "metadata_exact": metadata_exact,
        "split_exact": split_exact,
        "notes_exact": notes_exact,
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
    parent_updates = []
    for parent_id, spec in SPECS.items():
        if not private["metadata_exact"][parent_id]:
            parent_updates.append(
                {
                    "id": parent_id,
                    "propertyId": DAO_PROPERTY_ID,
                    "tagId": PARENT_TAG_ID,
                    "note": parent_note(spec),
                }
            )
    update_transactions(parent_updates)
    for parent_id in SPECS:
        if not private["split_exact"][parent_id]:
            reconcile_parent_split(
                private["parents"][parent_id], private["targets"][parent_id]
            )
    refreshed = query_parents(list(SPECS))
    note_updates: list[dict[str, Any]] = []
    for parent_id, spec in SPECS.items():
        note_updates.extend(
            child_note_updates(
                refreshed[parent_id], spec, private["targets"][parent_id]
            )
        )
    update_transactions(note_updates)


def write_report(name: str, payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


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
            if public["issues"] or any(
                action[key] != "none"
                for action in public["actions"]
                for key in ("metadata_action", "split_action", "curtailment_note_action")
            ):
                raise RuntimeError(f"post-apply verification failed: {public}")
            mode = "applied_and_verified"

    payload = {"status": "ok" if not public["issues"] else "blocked", "mode": mode, "digest": plan_digest, **public}
    path = write_report(
        "madison_90_july2025_mortgage_split_"
        + ("applied.json" if args.apply else "preview.json"),
        payload,
    )
    print(json.dumps({**payload, "report": str(path)}, indent=2))
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
