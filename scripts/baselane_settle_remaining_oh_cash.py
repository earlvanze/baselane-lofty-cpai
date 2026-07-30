#!/usr/bin/env python3
"""Settle safely funded Ohio accrual cash and void Sara's duplicate PM row."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql
from baselane_settle_madison_pm_mortgage import (
    STATE_PATH,
    ensure_native_audit,
    graphql,
)
from baselane_mcp.transfers import (
    build_transfer_plan,
    execute_transfer,
    list_active_transfer_accounts,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports"
ECO_ACCOUNT = 29732
SARA_DUPLICATE_PM_ID = "313978482"

SPECS: list[dict[str, Any]] = [
    {
        "key": "E187_to_ECO_accrual_cash",
        "from": 111697,
        "to": ECO_ACCOUNT,
        "amount": "1722.13",
        "property_id": "93597",
        "target_ending": "711.43",
        "bookkeeping_note": "1278 E 187th funded DAO fee and tax accrual settlement",
        "note": (
            "1278 E 187th: collect funded Jan-Jun 2026 DAO fees and tax "
            "accruals plus a partial historical ECO operating reimbursement. "
            "Hemlane PM excluded. The $749 security deposit remains segregated "
            "and operations retains $711.43, above the $500 float. Internal only."
        ),
        "parent_label": "1278-ECO | accrued fees + taxes + reimbursement | through 2026-06",
        "components": [
            *[
                [f"1278-ECO | DAO LLC fee cash | 2026-{month:02d}", "62.50"]
                for month in range(1, 7)
            ],
            *[
                [f"1278-ECO | property tax accrual cash | 2026-{month:02d}", "146.33"]
                for month in range(1, 7)
            ],
            [
                "1278-ECO | historical operating reimbursement | through 2026-06 | partial",
                "469.15",
            ],
        ],
    },
    {
        "key": "Nash_to_ECO_accrual_cash",
        "from": 119576,
        "to": ECO_ACCOUNT,
        "amount": "376.63",
        "property_id": "96348",
        "target_ending": "1248.68",
        "bookkeeping_note": "Nash funded DAO fee and partial tax accrual settlement",
        "note": (
            "566 Nash St: collect funded Jan-Jun 2026 DAO LLC/admin fees and "
            "partially fund January 2026 property-tax accrual. Hemlane PM "
            "excluded. The $1,890 security deposit remains segregated; "
            "operations retains $1,248.68, covering the next $242.70 ULD "
            "payment plus the $500 float. Internal only."
        ),
        "parent_label": "Nash-ECO | DAO fees + partial tax accrual | through 2026-06",
        "components": [
            *[
                [f"Nash-ECO | DAO LLC fee cash | 2026-{month:02d}", "62.50"]
                for month in range(1, 7)
            ],
            ["Nash-ECO | property tax accrual cash | 2026-01 | partial", "1.63"],
        ],
    },
]


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def balances() -> dict[int, Decimal]:
    wanted = {ECO_ACCOUNT, *(int(spec["from"]) for spec in SPECS)}
    return {
        int(row["transfer_account_id"]): Decimal(str(row["available_balance"]))
        for row in list_active_transfer_accounts(graphql)
        if int(row["transfer_account_id"]) in wanted
    }


def sara_duplicate() -> dict[str, Any]:
    result = run_graphql(
        {
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "ASC", "field": "date"},
                    "filter": {
                        "search": "PM Fee Accrual | 1432 Sara Ave. | June 2026",
                        "isHidden": False,
                        "isDeleted": False,
                    },
                    "page": 1,
                    "pageLimit": 100,
                }
            },
            "query": """
            query Transactions($input: SortsAndFilters) {
              transactions(input: $input) {
                data { id amount date propertyId tagId merchantName note isManual }
              }
            }
            """,
        }
    )["data"]["transactions"]["data"]
    rows = [row for row in result if str(row.get("id")) == SARA_DUPLICATE_PM_ID]
    if len(rows) != 1:
        raise RuntimeError("Sara duplicate PM accrual identity is missing or ambiguous")
    row = rows[0]
    actual = (
        Decimal(str(row.get("amount"))),
        str(row.get("date")),
        str(row.get("propertyId")),
        str(row.get("tagId")),
        bool(row.get("isManual")),
    )
    expected = (Decimal("-46.50"), "2026-06-28", "83188", "80", True)
    marker = "AOPS-MONTHLY-ACCRUAL|pm|1432 Sara Ave.|2026-06|46.50"
    if actual != expected or marker not in note_text(row.get("note")):
        raise RuntimeError(f"Sara duplicate PM accrual changed: actual={actual!r}")
    return {
        "id": SARA_DUPLICATE_PM_ID,
        "amount": "-46.50",
        "reason": "June rent was deposited net after Hemlane's direct PM split.",
    }


def build_public() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    opening = balances()
    duplicate = sara_duplicate()
    issues: list[str] = []
    plans: list[dict[str, Any]] = []
    forecast = dict(opening)
    transfers: list[dict[str, Any]] = []
    for spec in SPECS:
        source = int(spec["from"])
        amount = Decimal(spec["amount"])
        target = Decimal(spec["target_ending"])
        ending = forecast.get(source, Decimal()) - amount
        if ending != target:
            issues.append(
                f"{spec['key']} ending would be {ending:.2f}, not {target:.2f}"
            )
        if ending < Decimal("500.00"):
            issues.append(f"{spec['key']} would breach the active-property float")
        plan = build_transfer_plan(
            from_transfer_account_id=source,
            to_transfer_account_id=ECO_ACCOUNT,
            amount=spec["amount"],
            bookkeeping_note=spec["bookkeeping_note"],
            property_id=spec["property_id"],
            tag_id=24,
            same_day=True,
        )
        plans.append(plan)
        forecast[source] = ending
        forecast[ECO_ACCOUNT] = forecast.get(ECO_ACCOUNT, Decimal()) + amount
        transfers.append(
            {
                "key": spec["key"],
                "direction": "DAO -> ECO",
                "amount": spec["amount"],
                "property_id": spec["property_id"],
                "parent_label": spec["parent_label"],
                "bookkeeping_note": spec["bookkeeping_note"],
                "note": spec["note"],
                "components": [
                    {"label": label, "amount": value, "tag_id": "24"}
                    for label, value in spec["components"]
                ],
                "confirmation_token": plan["confirmation_token"],
            }
        )
    public = {
        "scope": "remaining safely funded Ohio cash settlements",
        "issues": issues,
        "sara_pm_void": duplicate,
        "held_accounts": {
            "1432 Sara Ave": "Retain $519.03 for recurring EARLDAO obligation and float.",
            "428 Cross St": "Retain $568.33 for next $165.71 ULD payment and float.",
        },
        "cash_flow_invariant": {
            "internal_only": True,
            "tag_id": "24",
            "category": "Transfers Between Accounts",
            "pm_cash_included": "0.00",
            "security_principal_moved": "0.00",
        },
        "opening_balances": {str(k): format(v, ".2f") for k, v in opening.items()},
        "forecast_balances": {str(k): format(v, ".2f") for k, v in forecast.items()},
        "transfers": transfers,
    }
    return public, plans


def digest(public: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_report(name: str, payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def void_sara_pm() -> list[dict[str, Any]]:
    result = run_graphql(
        {
            "operationName": "UpdateTransaction",
            "variables": {
                "input": [
                    {
                        "id": SARA_DUPLICATE_PM_ID,
                        "amount": 0,
                        "note": (
                            "ECO-VOID-DUPLICATE-PM|1432 Sara Ave.|2026-06|"
                            "313978482 | Original -46.50 manual accrual voided. "
                            "Hemlane already withheld/remitted PM before depositing "
                            "net rent, so no additional PM accrual or payment is due."
                        ),
                        "isReviewedByUser": True,
                    }
                ]
            },
            "query": """
            mutation UpdateTransaction($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) {
                id amount date propertyId tagId note isReviewedByUser
              }
            }
            """,
        }
    )["data"]["updateTransactions"]
    if (
        len(result) != 1
        or str(result[0].get("id")) != SARA_DUPLICATE_PM_ID
        or Decimal(str(result[0].get("amount"))) != Decimal("0")
    ):
        raise RuntimeError(f"Sara PM void response mismatch: {result!r}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()

    public, plans = build_public()
    plan_digest = digest(public)
    dry = {"status": "dry_run", "digest": plan_digest, **public}
    dry_path = write_report("remaining_oh_cash_settlement_dry_run.json", dry)
    if not args.apply:
        print(json.dumps({**dry, "report": str(dry_path)}, indent=2))
        return 0 if not public["issues"] else 2
    if args.digest != plan_digest:
        raise RuntimeError(f"live digest is {plan_digest}; exact --digest required")
    if public["issues"]:
        raise RuntimeError(f"refusing apply with issues: {public['issues']}")

    voided = void_sara_pm()
    receipts = []
    audits = []
    for spec, plan in zip(SPECS, plans, strict=True):
        receipts.append(
            execute_transfer(
                plan=plan,
                confirmation_token=plan["confirmation_token"],
                graphql_runner=graphql,
                state_path=STATE_PATH,
            )
        )
        audits.append(ensure_native_audit(spec, plan))
    ending = balances()
    for spec in SPECS:
        if ending.get(int(spec["from"])) != Decimal(spec["target_ending"]):
            raise RuntimeError(f"post-transfer balance mismatch for {spec['key']}")
    applied = {
        "status": "submitted_and_verified",
        "digest": plan_digest,
        **public,
        "sara_pm_void_response": voided,
        "ending_balances": {str(k): format(v, ".2f") for k, v in ending.items()},
        "receipts": receipts,
        "native_audits": audits,
    }
    path = write_report("remaining_oh_cash_settlement_applied.json", applied)
    print(json.dumps({**applied, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
