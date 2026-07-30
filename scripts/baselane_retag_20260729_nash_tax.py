#!/usr/bin/env python3
"""Idempotently retag the posted 2026-07-29 Nash tax payment in Baselane."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path("/home/digit/.openclaw/workspace")
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_mcp.transfers import run_graphql_via_cdp  # noqa: E402

SOURCE = ROOT / "reports" / "baselane_source_transaction_index.csv"
TRANSACTION_ID = "321912249"
EXPECTED = {
    "ISODate": "2026-07-29",
    "Amount": "-1070.12",
    "Merchant": "OPC*SUMMIT REAL",
    "Property": "566 Nash St",
    "PropertyId": "96348",
}
TARGET_TAG_ID = "93"
TARGET_CATEGORY = "Property Taxes"


def load_row() -> dict[str, str]:
    with SOURCE.open(newline="", encoding="utf-8-sig") as handle:
        matches = [
            row for row in csv.DictReader(handle)
            if row.get("BaselaneId") == TRANSACTION_ID
        ]
    if len(matches) != 1:
        raise SystemExit(f"expected one current source row, found {len(matches)}")
    row = matches[0]
    for key, expected in EXPECTED.items():
        if row.get(key) != expected:
            raise SystemExit(
                f"source guard failed for {key}: {row.get(key)!r} != {expected!r}"
            )
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()

    row = load_row()
    plan = {
        "transaction_id": TRANSACTION_ID,
        "property_id": EXPECTED["PropertyId"],
        "from_tag_id": row["TagId"],
        "from_category": row["Category"],
        "to_tag_id": TARGET_TAG_ID,
        "to_category": TARGET_CATEGORY,
    }
    digest = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if row["TagId"] == TARGET_TAG_ID and row["Category"] == TARGET_CATEGORY:
        print(json.dumps({"status": "already_applied", "digest": digest, **plan}, indent=2))
        return 0
    if not args.apply:
        print(json.dumps({"status": "dry_run", "digest": digest, **plan}, indent=2))
        return 0
    if args.digest != digest:
        raise SystemExit("apply requires the exact dry-run digest")

    payload = {
        "operationName": "UpdateTransactions",
        "query": (
            "mutation UpdateTransactions($input: [UpdateTransaction!]) { "
            "updateTransactions(input: $input) { id tagId propertyId merchantName amount } }"
        ),
        "variables": {
            "input": [{
                "id": TRANSACTION_ID,
                "tagId": TARGET_TAG_ID,
                "propertyId": EXPECTED["PropertyId"],
                "isReviewedByUser": True,
            }]
        },
    }
    result = run_graphql_via_cdp(
        payload,
        bridge_path=ROOT / "scripts" / "baselane_graphql_via_cdp.js",
        workspace_root=ROOT,
    )
    updated = (result.get("data") or {}).get("updateTransactions") or []
    if len(updated) != 1 or str(updated[0].get("tagId")) != TARGET_TAG_ID:
        raise SystemExit(json.dumps({"status": "verification_failed", "result": result}))
    print(json.dumps({"status": "applied", "digest": digest, "result": updated[0]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
