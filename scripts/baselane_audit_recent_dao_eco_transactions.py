#!/usr/bin/env python3
"""Read-only audit of recent Baselane DAO/ECO bank transactions.

The report includes every recent pending parent transaction plus any recent
parent transaction lacking a category or property. Native split children are
retained under their parent for auditability but are not independently treated
as bank cash movements.
"""

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

from baselane_mcp.transfers import (  # noqa: E402
    TRANSFER_ACCOUNTS_QUERY,
    run_graphql_via_cdp,
)


BRIDGE = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
DEFAULT_REPORT = (
    ROOT / "reports" / "baselane_recent_dao_eco_transaction_audit.current.json"
)

TRANSACTIONS_QUERY = """
query Transactions($input: SortsAndFilters) {
  transactions(input: $input) {
    total
    data {
      id amount date time merchantName description name pending
      bankAccountId propertyId unitId tagId note
      isSplit parentId hidden isDeleted isExternal isManual
      isReviewedByUser originalTransaction
      splitTransactions {
        id amount date merchantName propertyId unitId tagId parentId isDeleted
      }
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


def flatten_accounts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for parent in (payload.get("data") or {}).get("bankAccounts") or []:
        if not isinstance(parent, dict):
            continue
        rows.append({**parent, "_parentAccountName": parent.get("accountName")})
        for child in parent.get("subAccounts") or []:
            if isinstance(child, dict):
                rows.append(
                    {
                        **child,
                        "_parentAccountName": parent.get("accountName"),
                    }
                )
    return rows


def account_label(row: dict[str, Any]) -> str:
    parent = str(row.get("_parentAccountName") or "").strip()
    own = str(row.get("nickName") or row.get("accountName") or "").strip()
    if parent and own and parent != own:
        return f"{parent} — {own}"
    return own or parent


def is_dao_or_eco_account(row: dict[str, Any]) -> bool:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("_parentAccountName", "accountName", "nickName")
    ).lower()
    if "earldao" in text or "earl vanze" in text:
        return False
    return "dao" in text or "eco systems" in text


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(value.get("text") or "").split())
    return " ".join(str(value or "").split())


def money(value: Any) -> str:
    return f"{Decimal(str(value or 0)):.2f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--start-date",
        default=(dt.date.today() - dt.timedelta(days=7)).isoformat(),
    )
    parser.add_argument("--page-limit", type=int, default=250)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    cutoff = dt.date.fromisoformat(args.start_date)
    if args.page_limit < 1 or args.page_limit > 500:
        raise SystemExit("--page-limit must be between 1 and 500")

    accounts_payload = graphql(
        {
            "operationName": "BankAccountsActive",
            "variables": {
                "isConnectedAccount": None,
                "accountStatus": None,
                "isTransferable": None,
            },
            "query": TRANSFER_ACCOUNTS_QUERY,
        }
    )
    accounts = [
        row
        for row in flatten_accounts(accounts_payload)
        if row.get("isExternal") is False and is_dao_or_eco_account(row)
    ]
    account_by_id = {str(row["id"]): row for row in accounts}

    properties = graphql(
        {
            "operationName": "PropertyList",
            "variables": {},
            "query": "query PropertyList { property { id name address } }",
        }
    )["data"]["property"]
    property_by_id = {
        str(row["id"]): str(row.get("name") or row.get("address") or "")
        for row in properties
    }

    tag_payload = graphql(
        {
            "operationName": "TagList",
            "variables": {},
            "query": "query TagList { tag { type subType { id name } } }",
        }
    )["data"]["tag"]
    tag_by_id: dict[str, str] = {}
    for group in tag_payload:
        for subtype in group.get("subType") or []:
            tag_by_id[str(subtype["id"])] = (
                f"{group.get('type') or ''} / {subtype.get('name') or ''}".strip(" /")
            )

    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = graphql(
            {
                "operationName": "Transactions",
                "variables": {
                    "input": {
                        "sort": {"direction": "DESC", "field": "date"},
                        "filter": {
                            "isHidden": False,
                            "search": "",
                            "isDeleted": False,
                        },
                        "page": page,
                        "pageLimit": args.page_limit,
                    }
                },
                "query": TRANSACTIONS_QUERY,
            }
        )["data"]["transactions"]
        batch = result.get("data") or []
        rows.extend(batch)
        dated = [
            dt.date.fromisoformat(str(row["date"])[:10])
            for row in batch
            if row.get("date")
        ]
        if (
            not batch
            or len(rows) >= int(result.get("total") or 0)
            or (dated and min(dated) < cutoff)
        ):
            break
        page += 1

    recent = []
    for row in rows:
        if row.get("parentId") or row.get("isDeleted"):
            continue
        row_date = dt.date.fromisoformat(str(row["date"])[:10])
        if row_date < cutoff:
            continue
        account_id = str(row.get("bankAccountId") or "")
        account = account_by_id.get(account_id)
        if not account:
            continue
        missing_category = not bool(row.get("tagId"))
        missing_property = not bool(row.get("propertyId"))
        pending = bool(row.get("pending"))
        if not (pending or missing_category or missing_property):
            continue
        children = [
            {
                "id": str(child.get("id") or ""),
                "amount": money(child.get("amount")),
                "merchant": str(child.get("merchantName") or ""),
                "property_id": str(child.get("propertyId") or ""),
                "property": property_by_id.get(
                    str(child.get("propertyId") or ""), ""
                ),
                "tag_id": str(child.get("tagId") or ""),
                "category": tag_by_id.get(str(child.get("tagId") or ""), ""),
            }
            for child in row.get("splitTransactions") or []
            if not child.get("isDeleted")
        ]
        recent.append(
            {
                "id": str(row.get("id") or ""),
                "date": str(row.get("date") or ""),
                "amount": money(row.get("amount")),
                "direction": (
                    "outflow"
                    if Decimal(str(row.get("amount") or 0)) < 0
                    else "inflow"
                ),
                "pending": pending,
                "account_id": account_id,
                "account": account_label(account),
                "merchant": str(
                    row.get("merchantName")
                    or row.get("description")
                    or row.get("name")
                    or ""
                ),
                "property_id": str(row.get("propertyId") or ""),
                "property": property_by_id.get(
                    str(row.get("propertyId") or ""), ""
                ),
                "tag_id": str(row.get("tagId") or ""),
                "category": tag_by_id.get(str(row.get("tagId") or ""), ""),
                "note": note_text(row.get("note")),
                "missing_category": missing_category,
                "missing_property": missing_property,
                "is_split": bool(row.get("isSplit")),
                "children": children,
            }
        )

    report = {
        "status": "ok",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "start_date": cutoff.isoformat(),
        "account_count": len(accounts),
        "scanned_transaction_count": len(rows),
        "candidate_count": len(recent),
        "pending_count": sum(1 for row in recent if row["pending"]),
        "untagged_outflow_count": sum(
            1
            for row in recent
            if row["direction"] == "outflow"
            and (row["missing_category"] or row["missing_property"])
        ),
        "booking_candidate_count": sum(
            1 for row in recent if "booking" in row["merchant"].lower()
        ),
        "transactions": sorted(
            recent, key=lambda row: (row["date"], int(row["id"])), reverse=True
        ),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "status",
        "start_date",
        "account_count",
        "scanned_transaction_count",
        "candidate_count",
        "pending_count",
        "untagged_outflow_count",
        "booking_candidate_count",
    )}, indent=2))


if __name__ == "__main__":
    main()
