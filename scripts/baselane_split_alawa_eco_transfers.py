#!/usr/bin/env python3
"""Idempotently native-split the five ECO -> Alawa settlement transfers."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from baselane_alawa_loandepot_cleanup import (  # noqa: E402
    active_children,
    mutation_split,
    query_parent,
)

PIPELINE_LOCK = ROOT / "scripts" / ".baselane_source_pipeline.lock"
REPORT = ROOT / "reports" / "baselane_alawa_eco_transfer_splits.json"
TAG_TRANSFER = "24"
ALAWA_PROPERTY_ID = "73461"
ECO_PROPERTY_ID = "37648"


TARGETS: dict[str, dict[str, Any]] = {
    "228196379": {
        "date": "2026-01-07", "amount": "6000.00",
        "merchant_contains": "ECO Systems", "note_contains": "Mortgage Payment and July Rent and Water Bill",
        "splits": [
            ("5687.42", ECO_PROPERTY_ID, "ECO reimbursement | Alawa mortgage P&I | 2026-01 and 2026-02"),
            ("312.58", ALAWA_PROPERTY_ID, "ECO funding | Alawa July rent and water bill remainder"),
        ],
    },
    "256733613": {
        "date": "2026-03-21", "amount": "4000.00", "merchant_contains": "ECO Systems",
        "splits": [
            ("2843.71", ECO_PROPERTY_ID, "ECO reimbursement | Alawa mortgage P&I | 2026-04"),
            ("768.99", ALAWA_PROPERTY_ID, "ECO funding | Alawa mortgage escrow | 2026-04"),
            ("387.30", ALAWA_PROPERTY_ID, "ECO funding | Alawa unrestricted cash remainder | 2026-04"),
        ],
    },
    "274569716": {
        "date": "2026-04-28", "amount": "2000.00", "merchant_contains": "ECO Systems",
        "splits": [
            ("2000.00", ECO_PROPERTY_ID, "ECO reimbursement | Alawa mortgage P&I partial | 2026-05"),
        ],
    },
    "289087164": {
        "date": "2026-05-27", "amount": "3000.00", "merchant_contains": "ECO Systems",
        "splits": [
            ("2843.71", ECO_PROPERTY_ID, "ECO reimbursement | Alawa mortgage P&I | 2026-06"),
            ("156.29", ALAWA_PROPERTY_ID, "ECO funding | Alawa mortgage escrow partial | 2026-06"),
        ],
    },
    "304071333": {
        "date": "2026-06-26", "amount": "3000.00", "merchant_contains": "Earl Vanze Co",
        "splits": [
            ("2843.71", ECO_PROPERTY_ID, "ECO reimbursement | Alawa mortgage P&I | 2026-07"),
            ("156.29", ALAWA_PROPERTY_ID, "ECO funding | Alawa mortgage escrow partial | 2026-07"),
        ],
    },
}


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def target_rows(transaction_id: str, target: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "amount": float(amount), "tagId": TAG_TRANSFER, "propertyId": property_id,
            "merchantName": merchant, "date": target["date"],
        }
        for amount, property_id, merchant in target["splits"]
    ]


def normalized(rows: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    return sorted(
        (str(money(row.get("amount"))), str(row.get("tagId") or ""),
         str(row.get("propertyId") or ""), str(row.get("merchantName") or ""))
        for row in rows if not row.get("isDeleted")
    )


def inspect() -> dict[str, Any]:
    actions = []
    for transaction_id, target in TARGETS.items():
        row = query_parent(transaction_id)
        errors = []
        if not row or row.get("isDeleted") or row.get("hidden"):
            errors.append("missing_or_inactive")
        else:
            if money(row.get("amount")) != money(target["amount"]): errors.append("amount")
            if str(row.get("date") or "") != target["date"]: errors.append("date")
            if target["merchant_contains"].lower() not in str(row.get("merchantName") or "").lower(): errors.append("merchant")
            if target.get("note_contains") and target["note_contains"].lower() not in note_text(row.get("note")).lower(): errors.append("note")
            if str(row.get("tagId") or "") != TAG_TRANSFER: errors.append("parent_tag")
        expected = target_rows(transaction_id, target)
        if sum((money(item["amount"]) for item in expected), Decimal("0.00")) != money(target["amount"]):
            errors.append("invalid_target_sum")
        live = active_children(row) if row else []
        action = "blocked" if errors else ("already_split" if normalized(live) == normalized(expected) else "split")
        actions.append({
            "id": transaction_id, "action": action, "identity_errors": errors,
            "current_children": live, "target": expected,
        })
    digest_payload = [{"id": row["id"], "action": row["action"], "target": row["target"]} for row in actions]
    digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"actions": actions, "plan_digest": digest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--require-plan-digest")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()
    preview = inspect()
    applied = []
    if args.apply:
        if not args.require_plan_digest:
            parser.error("--apply requires --require-plan-digest")
        PIPELINE_LOCK.parent.mkdir(parents=True, exist_ok=True)
        with PIPELINE_LOCK.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            preview = inspect()
            if preview["plan_digest"] != args.require_plan_digest:
                raise SystemExit("live plan digest changed; rerun preview")
            if any(row["action"] == "blocked" for row in preview["actions"]):
                raise SystemExit("blocked identity guard; no mutations applied")
            for row in preview["actions"]:
                if row["action"] == "split":
                    applied.append(mutation_split(row["id"], row["target"]))
            verify = inspect()
    else:
        verify = None
    status = "ok"
    inspected = verify or preview
    if any(row["action"] in {"blocked", "split"} for row in inspected["actions"]):
        status = "review"
    report = {"status": status, "mode": "apply" if args.apply else "preview", "preview": preview,
              "applied": applied, "verify": verify}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "plan_digest": preview["plan_digest"],
                      "actions": [{"id": row["id"], "action": row["action"]} for row in inspected["actions"]],
                      "report": str(args.report)}, indent=2))
    return 0 if status == "ok" or not args.apply else 1


if __name__ == "__main__":
    raise SystemExit(main())
