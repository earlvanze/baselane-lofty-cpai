#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql


ROOT = Path(__file__).absolute().parents[1]
DEFAULT_REPORT = ROOT / "reports" / "baselane_ny_hi_precutoff_retag.json"
ECO_PROPERTY_ID = "37648"
ECO_PROPERTY_NAME = "Mining, Sales, Consulting, and PM"
APPLY_ENV = "BASELANE_NY_HI_PRECUTOFF_RETAG_APPLY"
APPLY_DIGEST_ENV = "BASELANE_NY_HI_PRECUTOFF_RETAG_DIGEST"

PROPERTY_CUTOFFS = {
    "84 Madison Ave": {"property_id": "60548", "first_token_sale_date": "2025-08-25", "gl_start_date": "2025-07-01"},
    "86 Madison Ave": {"property_id": "63162", "first_token_sale_date": "2024-12-06", "gl_start_date": "2024-11-01"},
    "88 Madison Ave": {"property_id": "31499", "first_token_sale_date": "2024-01-29", "gl_start_date": "2023-12-01"},
    "90 Madison Ave": {"property_id": "31525", "first_token_sale_date": "2024-05-14", "gl_start_date": "2024-04-01"},
    "724 3rd Ave": {"property_id": "33594", "first_token_sale_date": "2024-04-24", "gl_start_date": "2024-03-01"},
    "9 Country Club Ln N": {"property_id": "91341", "first_token_sale_date": "2025-08-15", "gl_start_date": "2025-07-01"},
    "85-104 Alawa Pl": {"property_id": "73461", "first_token_sale_date": "2025-03-14", "gl_start_date": "2025-02-01"},
}


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def query_property_rows(property_id: str) -> list[dict[str, Any]]:
    query = """
    query Transactions($input: SortsAndFilters) {
      transactions(input: $input) {
        total
        data {
          id amount date merchantName description name propertyId tagId bankAccountId note
          isManual hidden isDeleted isSplit parentId
        }
      }
    }
    """
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        response = run_graphql(
            {
                "operationName": "Transactions",
                "variables": {
                    "input": {
                        "sort": {"direction": "ASC", "field": "date"},
                        "filter": {"propertyId": property_id, "isHidden": False, "isDeleted": False},
                        "page": page,
                        "pageLimit": 1000,
                    }
                },
                "query": query,
            }
        )["data"]["transactions"]
        batch = response.get("data") or []
        rows.extend(batch)
        if not batch or len(rows) >= int(response.get("total") or 0):
            return rows
        page += 1


def retag_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "operationName": "UpdateTransaction",
        "variables": {
            "input": [
                {"id": str(row["id"]), "propertyId": ECO_PROPERTY_ID, "unitId": None, "isReviewedByUser": True}
                for row in rows
            ]
        },
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id propertyId unitId date amount merchantName isDeleted
          }
        }
        """,
    }


def collect_plan(selected_properties: set[str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    property_results = []
    targets = []
    for property_name, config in PROPERTY_CUTOFFS.items():
        if selected_properties and property_name not in selected_properties:
            continue
        rows = query_property_rows(config["property_id"])
        pre_cutoff = [row for row in rows if str(row.get("date") or "") < config["gl_start_date"]]
        amount = sum((Decimal(str(row.get("amount") or 0)) for row in pre_cutoff), Decimal("0"))
        evidence = [
            {
                "id": str(row.get("id") or ""),
                "date": row.get("date"),
                "amount": str(row.get("amount") or 0),
                "merchant": row.get("merchantName"),
                "description": row.get("description") or row.get("name"),
                "note": note_text(row.get("note")),
                "bank_account_id": row.get("bankAccountId"),
                "is_manual": bool(row.get("isManual")),
                "is_split": bool(row.get("isSplit")),
                "parent_id": row.get("parentId"),
            }
            for row in pre_cutoff
        ]
        property_results.append(
            {
                "property": property_name,
                **config,
                "live_row_count": len(rows),
                "pre_cutoff_row_count": len(pre_cutoff),
                "pre_cutoff_amount_sum": str(amount),
                "pre_cutoff_rows": evidence,
            }
        )
        for row in pre_cutoff:
            targets.append({**row, "source_property": property_name, "gl_start_date": config["gl_start_date"]})
    return property_results, targets


def main() -> int:
    parser = argparse.ArgumentParser(description="Retag NY/HI co-ownership rows before the approved GL cutoff to ECO Systems LLC.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--property", action="append", choices=sorted(PROPERTY_CUTOFFS))
    args = parser.parse_args()

    selected_properties = set(args.property or []) or None
    property_results, targets = collect_plan(selected_properties)
    identity = [
        {
            "id": str(row.get("id") or ""),
            "source_property": row["source_property"],
            "date": str(row.get("date") or ""),
            "amount": str(row.get("amount") or 0),
            "destination_property_id": ECO_PROPERTY_ID,
        }
        for row in targets
    ]
    digest = stable_digest(identity)
    apply_requested = bool(args.apply)
    apply_authorized = os.environ.get(APPLY_ENV) == "1" and os.environ.get(APPLY_DIGEST_ENV) == digest
    applied_rows: list[dict[str, Any]] = []
    status = "ready" if targets else "ok"
    if apply_requested:
        if not apply_authorized:
            status = "blocked"
        elif targets:
            response = run_graphql(retag_payload(targets))["data"]["updateTransactions"]
            applied_rows = response or []
            expected_ids = {str(row["id"]) for row in targets}
            applied_ids = {str(row.get("id") or "") for row in applied_rows if str(row.get("propertyId")) == ECO_PROPERTY_ID}
            if applied_ids != expected_ids:
                raise RuntimeError(f"retag response mismatch expected={sorted(expected_ids)} applied={sorted(applied_ids)}")
            status = "applied"
        else:
            status = "ok"

    verification = []
    if status == "applied":
        verified_properties, remaining = collect_plan(selected_properties)
        verification = verified_properties
        if remaining:
            raise RuntimeError(f"pre-cutoff rows remain after apply: {[row.get('id') for row in remaining]}")

    report = {
        "status": status,
        "generated_at": iso_z(),
        "policy": "NY/HI co-ownership ECO GL rows begin on the first day of the month before the first token sale.",
        "destination": {"entity": "ECO Systems LLC", "baselane_property_id": ECO_PROPERTY_ID, "baselane_property_name": ECO_PROPERTY_NAME},
        "target_count": len(targets),
        "target_amount_sum": str(sum((Decimal(str(row.get("amount") or 0)) for row in targets), Decimal("0"))),
        "payload_digest": digest,
        "property_results": property_results,
        "applied_count": len(applied_rows),
        "applied_rows": applied_rows,
        "post_apply_verification": verification,
        "approval": {
            "apply_env": APPLY_ENV,
            "digest_env": APPLY_DIGEST_ENV,
            "command": f"{APPLY_ENV}=1 {APPLY_DIGEST_ENV}={digest} python3 scripts/{Path(__file__).name} --apply",
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "target_count", "target_amount_sum", "payload_digest", "applied_count")}, indent=2))
    if status == "blocked":
        print(report["approval"]["command"])
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
