#!/usr/bin/env python3
"""Clear H1 tax accruals against posted 2026-07-29 ECO-paid tax bills.

The positive manual rows reverse the already-recognized monthly accrual balance.
The posted ECO bank debits remain the actual cash expense, so only the variance
between the H1 accrual and the bill changes each property's net position.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path("/home/digit/.openclaw/workspace")
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_mcp.transfers import run_graphql_via_cdp  # noqa: E402

TARGETS = [
    {
        "property": "1456 W 85th St.",
        "property_id": "81428",
        "amount": 2399.82,
        "bill": 2401.20,
        "merchant": "Tax Accrual Settlement | 1456 W 85th St | 2026 H1",
        "marker": "AOPS-TAX-SETTLEMENT|1456 W 85th St|2026-H1|2399.82|bill=2401.20",
    },
    {
        "property": "1278 E 187th St",
        "property_id": "93597",
        "amount": 877.98,
        "bill": 878.48,
        "merchant": "Tax Accrual Settlement | 1278 E 187th St | 2026 H1",
        "marker": "AOPS-TAX-SETTLEMENT|1278 E 187th St|2026-H1|877.98|bill=878.48",
    },
    {
        "property": "428 Cross St.",
        "property_id": "81425",
        "amount": 2216.22,
        "bill": 2027.33,
        "merchant": "Tax Accrual Settlement | 428 Cross St | 2026 H1",
        "marker": "AOPS-TAX-SETTLEMENT|428 Cross St|2026-H1|2216.22|bill=2027.33",
    },
    {
        "property": "566 Nash St",
        "property_id": "96348",
        "amount": 1019.16,
        "bill": 1080.82,
        "merchant": "Tax Accrual Settlement | 566 Nash St | 2026 H1",
        "marker": "AOPS-TAX-SETTLEMENT|566 Nash St|2026-H1|1019.16|bill=1080.82",
    },
]
TAG_ID = "95"  # City, State, & Local Taxes


def graphql(payload: dict) -> dict:
    return run_graphql_via_cdp(
        payload,
        bridge_path=ROOT / "scripts" / "baselane_graphql_via_cdp.js",
        workspace_root=ROOT,
    )


def note_text(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def find_existing(marker: str, merchant: str) -> list[dict]:
    payload = {
        "operationName": "Transactions",
        "variables": {
            "input": {
                "sort": {"direction": "DESC", "field": "date"},
                "filter": {
                    "search": merchant,
                    "isHidden": False,
                    "isDeleted": False,
                },
                "page": 1,
                "pageLimit": 100,
            }
        },
        "query": (
            "query Transactions($input: SortsAndFilters) { "
            "transactions(input: $input) { total data { "
            "id amount date merchantName propertyId tagId note isManual hidden isDeleted "
            "} } }"
        ),
    }
    result = graphql(payload)
    rows = ((result.get("data") or {}).get("transactions") or {}).get("data") or []
    return [row for row in rows if note_text(row.get("note")).startswith(marker)]


def create(target: dict) -> dict:
    values = {
        "merchantName": target["merchant"],
        "note": (
            f"{target['marker']} | Clears Jan-Jun 2026 monthly property-tax "
            f"accruals against the ${target['bill']:.2f} ECO-paid bill posted "
            "2026-07-29. Accounting-only accrual settlement; no bank transfer."
        ),
        "tagId": TAG_ID,
        "propertyId": target["property_id"],
        "unitId": None,
        "entityId": None,
        "date": "2026-07-29",
        "bankAccountId": None,
        "amount": target["amount"],
        "isReviewedByUser": True,
    }
    payload = {
        "operationName": "createTransaction",
        "variables": values,
        "query": (
            "mutation createTransaction($merchantName: String!, $note: String!, "
            "$tagId: ID, $propertyId: ID, $unitId: ID, $entityId: Int, "
            "$date: String!, $bankAccountId: ID, $amount: Float!, "
            "$isReviewedByUser: Boolean) { createTransaction(input: { "
            "merchantName: $merchantName note: $note tagId: $tagId "
            "propertyId: $propertyId unitId: $unitId entityId: $entityId "
            "date: $date bankAccountId: $bankAccountId amount: $amount "
            "isReviewedByUser: $isReviewedByUser }) { "
            "id amount date merchantName propertyId tagId note isManual } }"
        ),
    }
    return (graphql(payload).get("data") or {}).get("createTransaction") or {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()

    digest = hashlib.sha256(
        json.dumps(TARGETS, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    plan = []
    for target in TARGETS:
        matches = find_existing(target["marker"], target["merchant"])
        if len(matches) > 1:
            raise SystemExit(f"duplicate settlement marker: {target['marker']}")
        plan.append({
            **target,
            "variance": round(target["amount"] - target["bill"], 2),
            "status": "already_applied" if matches else "create",
            "existing_id": str(matches[0]["id"]) if matches else None,
        })
    if not args.apply:
        print(json.dumps({"status": "dry_run", "digest": digest, "plan": plan}, indent=2))
        return 0
    if args.digest != digest:
        raise SystemExit("apply requires the exact dry-run digest")

    created = []
    for target, action in zip(TARGETS, plan):
        if action["status"] == "create":
            row = create(target)
            if (
                str(row.get("propertyId")) != target["property_id"]
                or str(row.get("tagId")) != TAG_ID
                or round(float(row.get("amount") or 0), 2) != target["amount"]
            ):
                raise SystemExit(json.dumps({"status": "create_verification_failed", "row": row}))
            created.append(row)
    print(json.dumps({
        "status": "applied_or_already_applied",
        "digest": digest,
        "created": created,
        "plan": plan,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
