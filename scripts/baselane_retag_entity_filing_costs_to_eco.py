#!/usr/bin/env python3
"""Inventory entity filing costs and retag only explicitly approved rows to ECO.

A filing merchant alone does not establish that a cost belongs to ECO.
Ordinary costs covered by a matching accrued/collected ECO service fee may be
retagged, but extraordinary or back-filing costs without that coverage remain
DAO/property expenses or reimbursable to ECO. Live application therefore
requires a reviewed eligibility manifest containing exact transaction IDs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
GRAPHQL_MODULE = ROOT / "scripts" / "baselane_apply_alcott_accruals_live.py"
ECO_PROPERTY_ID = "37648"
ECO_PROPERTY_NAME = "Mining, Sales, Consulting, and PM"

SEARCH_TERMS = (
    "CORPORATE FILINGS",
    "WYOMING SECRETARY",
    "WY SECRETARY",
    "REGISTERED AGENTS",
    "CO SECRETARY STATE",
    "ILSOS",
    "NYS DOS",
)

MERCHANT_MARKERS = (
    "CORPORATE FILINGS LLC",
    "WYOMING SECRETARY OF ST",
    "WY SECRETARY OF STA",
    "REGISTERED AGENTS INC",
    "CO SECRETARY STATE FEE",
    "ILSOS ",
    "NYS DOS ",
)


def load_graphql_runner():
    spec = importlib.util.spec_from_file_location("baselane_live", GRAPHQL_MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {GRAPHQL_MODULE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run_graphql


def note_text(note: Any) -> str:
    if isinstance(note, dict):
        return str(note.get("text") or "")
    return str(note or "")


def query_transactions(run_graphql, search: str) -> list[dict[str, Any]]:
    payload = {
        "operationName": "Transactions",
        "variables": {
            "input": {
                "sort": {"direction": "DESC", "field": "date"},
                "filter": {
                    "search": search,
                    "isHidden": False,
                    "isDeleted": False,
                },
                "page": 1,
                "pageLimit": 1000,
            }
        },
        "query": """
        query Transactions($input: SortsAndFilters) {
          transactions(input: $input) {
            total
            data {
              id amount date merchantName description bankAccountId
              propertyId tagId note isManual hidden isDeleted
            }
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["transactions"].get("data") or []


def query_bank_account(run_graphql, bank_account_id: str) -> dict[str, Any]:
    payload = {
        "operationName": "BankAccount",
        "variables": {"id": bank_account_id},
        "query": """
        query BankAccount($id: ID!) {
          bankAccount(id: $id) {
            id accountName nickName accountNumber institutionName
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["bankAccount"]


def is_actual_filing_cost(row: dict[str, Any]) -> bool:
    merchant = str(row.get("merchantName") or "").upper()
    note = note_text(row.get("note"))
    return (
        float(row.get("amount") or 0) < 0
        and any(marker in merchant for marker in MERCHANT_MARKERS)
        and "INTERNAL_TRANSFER" not in merchant
        and "AOPS-" not in note
        and not row.get("hidden")
        and not row.get("isDeleted")
    )


def inventory(run_graphql) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for term in SEARCH_TERMS:
        for row in query_transactions(run_graphql, term):
            if is_actual_filing_cost(row):
                rows[str(row["id"])] = row
    return sorted(rows.values(), key=lambda row: (str(row.get("date") or ""), int(row["id"])))


def update_properties(run_graphql, row_ids: list[str]) -> list[dict[str, Any]]:
    payload = {
        "operationName": "UpdateTransaction",
        "variables": {
            "input": [{
                "id": row_id,
                "propertyId": ECO_PROPERTY_ID,
                "unitId": None,
            } for row_id in row_ids]
        },
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id propertyId tagId amount date merchantName bankAccountId note
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["updateTransactions"]


def load_approved_ids(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict):
        values = payload.get("approved_transaction_ids")
    else:
        values = None
    if not isinstance(values, list) or any(not isinstance(value, (str, int)) for value in values):
        raise ValueError(
            "Eligibility manifest must be a JSON list of transaction IDs or an "
            "object with an approved_transaction_ids list"
        )
    return {str(value) for value in values}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply property changes live")
    parser.add_argument(
        "--eligibility-manifest",
        type=Path,
        help="Reviewed JSON list of transaction IDs covered by an accrued/collected ECO fee",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.apply and args.eligibility_manifest is None:
        parser.error("--apply requires --eligibility-manifest; merchant matching alone is not sufficient")

    run_graphql = load_graphql_runner()
    rows = inventory(run_graphql)
    discovered_candidates = [
        row for row in rows
        if str(row.get("propertyId") or "") != ECO_PROPERTY_ID
    ]
    approved_ids = (
        load_approved_ids(args.eligibility_manifest)
        if args.eligibility_manifest is not None
        else set()
    )
    known_ids = {str(row["id"]) for row in rows}
    unknown_approved_ids = sorted(approved_ids - known_ids)
    if args.apply and unknown_approved_ids:
        parser.error(
            "Eligibility manifest contains transaction IDs not found in the live filing-cost "
            f"inventory: {', '.join(unknown_approved_ids)}"
        )
    candidates = (
        [
            row for row in discovered_candidates
            if str(row["id"]) in approved_ids
        ]
        if args.eligibility_manifest is not None
        else discovered_candidates
    )

    bank_accounts: dict[str, dict[str, Any]] = {}
    # Account lookups are useful in dry-run audit reports but consume enough
    # short-lived app-check requests to jeopardize a later live mutation.
    if not args.apply:
        for bank_id in sorted({str(row["bankAccountId"]) for row in candidates if row.get("bankAccountId")}):
            bank_accounts[bank_id] = query_bank_account(run_graphql, bank_id)

    applied: list[dict[str, Any]] = []
    if args.apply:
        for start in range(0, len(candidates), 25):
            batch = candidates[start:start + 25]
            applied.extend(update_properties(
                run_graphql,
                [str(row["id"]) for row in batch],
            ))

    remaining = (
        [
            row for row in inventory(run_graphql)
            if str(row["id"]) in approved_ids
            and str(row.get("propertyId") or "") != ECO_PROPERTY_ID
        ]
        if args.apply
        else candidates
    )

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    report_path = args.report or REPORTS / f"baselane_entity_filing_costs_to_eco.{stamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "mode": "apply" if args.apply else "dry_run",
        "policy": (
            "Merchant matching is inventory only. A row may be assigned to ECO only "
            "when a reviewed manifest confirms that an accrued/collected ECO fee covers "
            "it; unmatched back-filing and extraordinary costs stay with the DAO/property "
            "or remain reimbursable to ECO. Only propertyId is changed."
        ),
        "eco_property": {"id": ECO_PROPERTY_ID, "name": ECO_PROPERTY_NAME},
        "matched_count": len(rows),
        "already_eco_count": sum(
            str(row.get("propertyId") or "") == ECO_PROPERTY_ID for row in rows
        ),
        "discovered_candidate_count": len(discovered_candidates),
        "eligibility_manifest": (
            str(args.eligibility_manifest) if args.eligibility_manifest is not None else None
        ),
        "approved_id_count": len(approved_ids),
        "unknown_approved_ids": unknown_approved_ids,
        "candidate_count": len(candidates),
        "applied_count": len(applied),
        "remaining_count": len(remaining),
        "candidate_total": round(sum(abs(float(row["amount"])) for row in candidates), 2),
        "bank_accounts": bank_accounts,
        "candidates": [
            {
                **{key: row.get(key) for key in (
                    "id", "date", "amount", "merchantName", "bankAccountId",
                    "propertyId", "tagId",
                )},
                "note": note_text(row.get("note")),
            }
            for row in candidates
        ],
        "applied": applied,
        "remaining": [str(row["id"]) for row in remaining],
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(json.dumps({
        "mode": report["mode"],
        "matched_count": report["matched_count"],
        "already_eco_count": report["already_eco_count"],
        "discovered_candidate_count": report["discovered_candidate_count"],
        "approved_id_count": report["approved_id_count"],
        "candidate_count": report["candidate_count"],
        "candidate_total": report["candidate_total"],
        "applied_count": report["applied_count"],
        "remaining_count": report["remaining_count"],
        "report": str(report_path),
    }, indent=2))
    return 0 if not remaining or not args.apply else 1


if __name__ == "__main__":
    raise SystemExit(main())
