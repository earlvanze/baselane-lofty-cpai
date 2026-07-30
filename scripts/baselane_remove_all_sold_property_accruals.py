#!/usr/bin/env python3
"""Delete synthetic tax, insurance, and DAO-fee accruals for sold properties.

The sold-property source of truth is the exported Yhome Transition
Reconciliation sheet, supplemented by explicit sales confirmed after the
sheet was last refreshed.  Only Baselane manual rows carrying an exact AOPS
accrual marker are eligible.  Cash transactions and Web2/Web3 settlement rows
are never selected.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql


ROOT = Path("/home/digit/.openclaw/workspace")
DEFAULT_SHEET = ROOT / "reports" / "yhome_transition_reconciliation.csv"
DEFAULT_REPORT = ROOT / "reports" / "baselane_sold_property_accrual_deletion.20260729.json"
APPLY_ENV = "BASELANE_SOLD_ACCRUAL_DELETE_APPLY"
APPLY_DIGEST_ENV = "BASELANE_SOLD_ACCRUAL_DELETE_DIGEST"
EXPLICITLY_CONFIRMED_SOLD = ("8708 Willard Ave, Cleveland, OH 44102",)
SEARCH_PREFIXES = (
    "AOPS-OHIL-ACCRUAL",
    "AOPS-PAU-ACCRUAL",
    "AOPS-PNL-ACCRUAL",
    "AOPS-MONTHLY-ACCRUAL",
)
ELIGIBLE_KINDS = {"taxes", "insurance", "dao", "dao_eco"}
MARKER_RE = re.compile(
    r"^(AOPS-(?:OHIL|PAU|PNL|MONTHLY)-ACCRUAL)"
    r"\|(taxes|insurance|dao|dao_eco)\|([^|]+)\|(\d{4}-\d{2})\|"
)
STREET_SUFFIXES = {
    "ave", "avenue", "blvd", "boulevard", "cir", "circle", "ct", "court",
    "dr", "drive", "ln", "lane", "pl", "place", "rd", "road", "st", "street",
}
TOKEN_ALIASES = {
    "avenue": "ave", "boulevard": "blvd", "circle": "cir", "court": "ct",
    "drive": "dr", "lane": "ln", "place": "pl", "road": "rd", "street": "st",
    "east": "e", "north": "n", "south": "s", "west": "w",
}


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def money(value: Any) -> str:
    return f"{Decimal(str(value or 0)):.2f}"


def address_key(value: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", value.lower())
    normalized = [TOKEN_ALIASES.get(token, token) for token in tokens]
    for index, token in enumerate(normalized):
        if token in {TOKEN_ALIASES.get(item, item) for item in STREET_SUFFIXES}:
            return " ".join(normalized[: index + 1])
    return " ".join(normalized)


def sold_properties(sheet_path: Path) -> dict[str, str]:
    sold: dict[str, str] = {}
    with sheet_path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.reader(handle):
            if len(row) > 1 and "sold" in str(row[1]).lower():
                sold[address_key(row[0])] = row[0]
    for property_name in EXPLICITLY_CONFIRMED_SOLD:
        sold[address_key(property_name)] = property_name
    return sold


def query_marker_rows() -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    query = """
    query Transactions($input: SortsAndFilters) {
      transactions(input: $input) {
        total
        data {
          id amount date propertyId tagId merchantName note isManual isDeleted
          parentId bankAccountId
        }
      }
    }
    """
    for search in SEARCH_PREFIXES:
        page = 1
        seen = 0
        while True:
            result = run_graphql({
                "operationName": "Transactions",
                "variables": {"input": {
                    "sort": {"direction": "ASC", "field": "date"},
                    "filter": {
                        "search": search,
                        "isHidden": False,
                        "isDeleted": False,
                    },
                    "page": page,
                    "pageLimit": 250,
                }},
                "query": query,
            })["data"]["transactions"]
            rows = result.get("data") or []
            for row in rows:
                by_id[str(row["id"])] = row
            seen += len(rows)
            if not rows or seen >= int(result.get("total") or 0):
                break
            page += 1
    return list(by_id.values())


def build_targets(
    rows: list[dict[str, Any]], sold: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    targets: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        note = note_text(row.get("note"))
        match = MARKER_RE.match(note)
        if not match or match.group(2) not in ELIGIBLE_KINDS:
            continue
        marker_property = match.group(3)
        key = address_key(marker_property)
        if key not in sold:
            continue
        identity_issues = []
        if not row.get("isManual"):
            identity_issues.append("not_manual")
        if row.get("isDeleted"):
            identity_issues.append("already_deleted")
        if row.get("parentId") is not None:
            identity_issues.append("split_child")
        if row.get("bankAccountId") is not None:
            identity_issues.append("bank_cash_row")
        public = {
            "id": str(row["id"]),
            "sold_property": sold[key],
            "marker_property": marker_property,
            "kind": match.group(2),
            "month": match.group(4),
            "date": str(row.get("date") or "")[:10],
            "amount": money(row.get("amount")),
            "property_id": str(row.get("propertyId") or ""),
            "tag_id": str(row.get("tagId") or ""),
            "merchant_name": str(row.get("merchantName") or ""),
            "marker": note,
        }
        if identity_issues:
            rejected.append({**public, "identity_issues": identity_issues})
        else:
            targets.append(public)
    targets.sort(key=lambda item: (
        address_key(item["sold_property"]), item["month"], item["kind"], item["id"]
    ))
    return targets, rejected


def summarize(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "row_count": 0,
            "dao_balance_increase": Decimal("0"),
            "eco_balance_decrease": Decimal("0"),
            "by_kind": defaultdict(Decimal),
        }
    )
    for row in targets:
        item = grouped[row["sold_property"]]
        amount = Decimal(row["amount"])
        item["row_count"] += 1
        item["by_kind"][row["kind"]] += amount
        if row["kind"] == "dao_eco":
            item["eco_balance_decrease"] += amount
        else:
            item["dao_balance_increase"] += -amount
    return [
        {
            "property": property_name,
            "row_count": item["row_count"],
            "dao_balance_increase": money(item["dao_balance_increase"]),
            "eco_balance_decrease": money(item["eco_balance_decrease"]),
            "by_kind": {
                kind: money(amount)
                for kind, amount in sorted(item["by_kind"].items())
            },
        }
        for property_name, item in sorted(grouped.items(), key=lambda pair: address_key(pair[0]))
    ]


def delete_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    mutation = """
    mutation UpdateTransaction($input: [UpdateTransaction!]) {
      updateTransactions(input: $input) {
        id isDeleted amount date propertyId tagId note
      }
    }
    """
    for index in range(0, len(targets), 50):
        batch = targets[index:index + 50]
        returned = run_graphql({
            "operationName": "UpdateTransaction",
            "variables": {"input": [
                {"id": row["id"], "isDeleted": True, "isReviewedByUser": True}
                for row in batch
            ]},
            "query": mutation,
        })["data"]["updateTransactions"]
        expected_ids = {row["id"] for row in batch}
        confirmed_ids = {
            str(row.get("id")) for row in returned if row.get("isDeleted")
        }
        if confirmed_ids != expected_ids:
            raise RuntimeError(
                f"Baselane deletion response mismatch: expected={sorted(expected_ids)} "
                f"confirmed={sorted(confirmed_ids)}"
            )
        applied.extend(returned)
    return applied


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sheet", type=Path, default=DEFAULT_SHEET)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    sold = sold_properties(args.sheet)
    targets, rejected = build_targets(query_marker_rows(), sold)
    digest = hashlib.sha256(
        json.dumps(targets, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    status = "no_op" if not targets else "ready"
    applied: list[dict[str, Any]] = []
    if rejected:
        status = "blocked_identity_mismatch"
    elif args.apply and targets:
        if (
            os.environ.get(APPLY_ENV) != "1"
            or os.environ.get(APPLY_DIGEST_ENV) != digest
        ):
            status = "blocked_approval_gate"
        else:
            applied = delete_targets(targets)
            status = "applied"

    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scope": (
            "All months; exact manual AOPS tax, insurance, DAO-fee expense, "
            "and paired ECO DAO-fee revenue accruals for confirmed sold properties."
        ),
        "sold_property_count": len(sold),
        "sold_properties": sorted(sold.values(), key=address_key),
        "target_count": len(targets),
        "targets": targets,
        "rejected": rejected,
        "summary": summarize(targets),
        "payload_digest": digest,
        "applied_count": len(applied),
        "approval_command": (
            f"{APPLY_ENV}=1 {APPLY_DIGEST_ENV}={digest} "
            f"python3 scripts/{Path(__file__).name} --apply"
        ),
        "controls": {
            "cash_rows_eligible": False,
            "split_children_eligible": False,
            "web3_yhome_settlement_rows_eligible": False,
            "pm_fee_accruals_eligible": False,
            "explicitly_confirmed_sold": list(EXPLICITLY_CONFIRMED_SOLD),
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "status": status,
        "sold_property_count": len(sold),
        "target_count": len(targets),
        "rejected_count": len(rejected),
        "payload_digest": digest,
        "summary": report["summary"],
        "report": str(args.report),
    }, indent=2))
    return 0 if status in {"ready", "applied", "no_op"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
