#!/usr/bin/env python3
"""Capture live ECO-tagged activity held in scoped DAO bank accounts."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE", Path(__file__).absolute().parents[1]))
sys.path.insert(0, str(ROOT / "scripts"))

from baselane_apply_alcott_accruals_live import run_graphql  # noqa: E402


ECO_PROPERTY_ID = "37648"
DEFAULT_REPORT = ROOT / "reports" / "scoped_dao_live_eco_allocation_20260714.json"
DEFAULT_CSV = ROOT / "reports" / "scoped_dao_live_eco_allocation_evidence_20260714.csv"
MONEY = Decimal("0.01")

BANK_ACCOUNTS = {
    "88616": ("86 Madison Ave", "Snow Leopard LFTY0439 DAO LLC"),
    "89681": ("88 Madison Ave", "Heron LFTY0314 DAO LLC"),
    "89680": ("90 Madison Ave", "Strawberry LFTY402 DAO LLC"),
    "56668": ("724 3rd Ave", "Grape LFTY403 DAO LLC"),
    "102389": ("85-104 Alawa Pl", "Poodle LFTY0452 DAO LLC"),
}


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def query_transactions(bank_account_id: str) -> list[dict[str, Any]]:
    query = """
    query Transactions($input: SortsAndFilters) {
      transactions(input: $input) {
        total
        data {
          id amount date merchantName description propertyId tagId bankAccountId
          note isManual hidden isDeleted
        }
      }
    }
    """
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = run_graphql({
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "DESC", "field": "date"},
                    "filter": {
                        "bankAccountId": bank_account_id,
                        "propertyId": ECO_PROPERTY_ID,
                        "isHidden": False,
                        "isDeleted": False,
                    },
                    "page": page,
                    "pageLimit": 1000,
                }
            },
            "query": query,
        })["data"]["transactions"]
        batch = result.get("data") or []
        rows.extend(batch)
        if not batch or len(rows) >= int(result.get("total") or 0):
            return rows
        page += 1


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "date", "baselane_id", "bank_account_id", "bank_property", "bank_owner",
        "amount", "property_id", "tag_id", "merchant", "description", "notes",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", type=date.fromisoformat, default=date(2026, 7, 14))
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args()

    evidence: list[dict[str, Any]] = []
    net_by_owner: dict[str, Decimal] = defaultdict(Decimal)
    for bank_account_id, (bank_property, bank_owner) in BANK_ACCOUNTS.items():
        for tx in query_transactions(bank_account_id):
            tx_date = str(tx.get("date") or "")
            if not tx_date.startswith("2026-") or date.fromisoformat(tx_date) > args.cutoff:
                continue
            amount = Decimal(str(tx.get("amount") or 0)).quantize(MONEY)
            net_by_owner[bank_owner] += amount
            evidence.append({
                "date": tx_date,
                "baselane_id": str(tx.get("id") or ""),
                "bank_account_id": bank_account_id,
                "bank_property": bank_property,
                "bank_owner": bank_owner,
                "amount": f"{amount:.2f}",
                "property_id": str(tx.get("propertyId") or ""),
                "tag_id": str(tx.get("tagId") or ""),
                "merchant": str(tx.get("merchantName") or ""),
                "description": str(tx.get("description") or ""),
                "notes": note_text(tx.get("note")),
            })

    evidence.sort(key=lambda row: (row["date"], row["bank_owner"], row["baselane_id"]))
    accounts = []
    for bank_account_id, (bank_property, bank_owner) in BANK_ACCOUNTS.items():
        net = net_by_owner[bank_owner].quantize(MONEY)
        accounts.append({
            "bank_account_id": bank_account_id,
            "bank_property": bank_property,
            "bank_owner": bank_owner,
            "net_eco_allocated_cash": f"{net:.2f}",
            "settlement_direction": "ECO Systems LLC owes DAO" if net < 0 else "DAO owes ECO Systems LLC",
            "settlement_amount": f"{abs(net):.2f}",
        })

    report = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "cutoff": args.cutoff.isoformat(),
        "eco_property_id": ECO_PROPERTY_ID,
        "method": (
            "Live Baselane rows allocated to ECO Systems LLC propertyId 37648 within scoped DAO-owned bank accounts. "
            "Negative net cash means ECO-funded activity was paid by the DAO bank account and ECO owes the DAO; "
            "positive net cash means the DAO account holds ECO funds and owes ECO."
        ),
        "transaction_count": len(evidence),
        "accounts": accounts,
        "transactions": evidence,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(args.csv, evidence)
    print(json.dumps({
        "report": str(args.report),
        "csv": str(args.csv),
        "transaction_count": len(evidence),
        "accounts": accounts,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
