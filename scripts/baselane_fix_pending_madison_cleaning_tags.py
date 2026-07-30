#!/usr/bin/env python3
"""Idempotently correct the July 2026 pending Madison cleaning categories."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_mcp.transfers import run_graphql_via_cdp  # noqa: E402


BRIDGE = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
REPORT = ROOT / "reports" / "baselane_fix_pending_madison_cleaning_tags.json"
CLEANING_TAG_ID = "52"
TARGETS = {
    "321921425": {
        "date": "2026-07-29",
        "amount": Decimal("-230.00"),
        "bank_account_id": "88616",
        "property_id": "63162",
        "note": "May-June Deep Cleanings Ingrid",
        "allowed_old_tag_ids": {"140"},
    },
    "321921262": {
        "date": "2026-07-29",
        "amount": Decimal("-320.00"),
        "bank_account_id": "89681",
        "property_id": "31499",
        "note": "May-June Deep Cleanings Ingrid",
        "allowed_old_tag_ids": {"145"},
    },
}

QUERY = """
query Transactions($input: SortsAndFilters) {
  transactions(input: $input) {
    data {
      id amount date merchantName bankAccountId propertyId unitId tagId note
      pending parentId isDeleted isReviewedByUser
    }
  }
}
""".strip()


def graphql(payload: dict[str, Any]) -> dict[str, Any]:
    return run_graphql_via_cdp(
        payload,
        bridge_path=BRIDGE,
        workspace_root=ROOT,
        timeout=120,
    )


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(value.get("text") or "").split())
    return " ".join(str(value or "").split())


def fetch() -> dict[str, dict[str, Any]]:
    response = graphql(
        {
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "DESC", "field": "date"},
                    "filter": {
                        "isHidden": False,
                        "isDeleted": False,
                        "search": "",
                    },
                    "page": 1,
                    "pageLimit": 250,
                }
            },
            "query": QUERY,
        }
    )
    return {
        str(row["id"]): row
        for row in response["data"]["transactions"]["data"]
        if str(row.get("id") or "") in TARGETS
    }


def validate(row: dict[str, Any], target: dict[str, Any]) -> None:
    actual = {
        "date": str(row.get("date") or "")[:10],
        "amount": Decimal(str(row.get("amount") or 0)).quantize(Decimal("0.01")),
        "bank_account_id": str(row.get("bankAccountId") or ""),
        "property_id": str(row.get("propertyId") or ""),
        "note": note_text(row.get("note")),
    }
    expected = {key: target[key] for key in actual}
    if actual != expected:
        raise RuntimeError(f"immutable preflight mismatch: {actual!r} != {expected!r}")
    if row.get("parentId") or row.get("isDeleted") or not row.get("pending"):
        raise RuntimeError(f"transaction state is not an active pending parent: {row!r}")
    tag_id = str(row.get("tagId") or "")
    if tag_id != CLEANING_TAG_ID and tag_id not in target["allowed_old_tag_ids"]:
        raise RuntimeError(f"unexpected source tag {tag_id!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    before = fetch()
    if set(before) != set(TARGETS):
        raise RuntimeError(f"target discovery mismatch: {sorted(before)}")
    for tx_id, target in TARGETS.items():
        validate(before[tx_id], target)

    updates = [
        {
            "id": tx_id,
            "tagId": CLEANING_TAG_ID,
            "isReviewedByUser": True,
        }
        for tx_id, row in before.items()
        if str(row.get("tagId") or "") != CLEANING_TAG_ID
    ]
    if args.apply and updates:
        graphql(
            {
                "operationName": "UpdatePendingMadisonCleaningTags",
                "variables": {"input": updates},
                "query": """
mutation UpdatePendingMadisonCleaningTags($input: [UpdateTransaction!]) {
  updateTransactions(input: $input) {
    id amount date bankAccountId propertyId tagId note pending
  }
}
""".strip(),
            }
        )

    after = fetch()
    for tx_id, target in TARGETS.items():
        validate(after[tx_id], target)
        if args.apply and str(after[tx_id].get("tagId") or "") != CLEANING_TAG_ID:
            raise RuntimeError(f"verification failed for {tx_id}")

    report = {
        "status": "verified" if args.apply else "dry_run",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "applied": args.apply,
        "target_tag_id": CLEANING_TAG_ID,
        "updates_needed": len(updates),
        "transactions": [
            {
                "id": tx_id,
                "amount": f"{target['amount']:.2f}",
                "date": target["date"],
                "property_id": target["property_id"],
                "old_tag_id": str(before[tx_id].get("tagId") or ""),
                "new_tag_id": str(after[tx_id].get("tagId") or ""),
                "pending": bool(after[tx_id].get("pending")),
            }
            for tx_id, target in TARGETS.items()
        ],
    }
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
