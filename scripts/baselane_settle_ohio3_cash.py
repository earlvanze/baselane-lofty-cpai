#!/usr/bin/env python3
"""Guarded Ohio-3 cash cleanup: void duplicate PM and settle Dille excess."""

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
DILLE_ACCOUNT = 94250
ECO_ACCOUNT = 29732
DILLE_PROPERTY_ID = "83240"
ALLENDALE_PROPERTY_ID = "83239"
ALLENDALE_DUPLICATE_PM_ID = "313978481"
TARGET_ENDING = Decimal("500.00")

SPEC: dict[str, Any] = {
    "key": "Dille_to_ECO_excess_cash",
    "from": DILLE_ACCOUNT,
    "to": ECO_ACCOUNT,
    "amount": "1334.98",
    "property_id": DILLE_PROPERTY_ID,
    "bookkeeping_note": "Dille partial operating-deficit cash settlement through June 2026",
    "note": (
        "1518 Dille Rd: partial settlement of the documented DAO operating "
        "cash deficit through 2026-06. Leaves a $500 operating float. PM "
        "excluded because Hemlane withheld/remitted PM before net-rent "
        "deposit. The separate $1,000 EARLDAO repair loan remains a DAO "
        "liability and is not characterized as ECO revenue. Internal only."
    ),
    "parent_label": (
        "Dille-ECO | partial operating-deficit cash settlement | through 2026-06"
    ),
    "components": [
        [
            "Dille-ECO | operating deficit reimbursement | through 2026-06 | partial",
            "1334.98",
        ],
    ],
}


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def balances() -> dict[int, Decimal]:
    wanted = {DILLE_ACCOUNT, ECO_ACCOUNT}
    return {
        int(row["transfer_account_id"]): Decimal(str(row["available_balance"]))
        for row in list_active_transfer_accounts(graphql)
        if int(row["transfer_account_id"]) in wanted
    }


def allendale_duplicate() -> dict[str, Any]:
    result = run_graphql(
        {
            "operationName": "Transactions",
            "variables": {
                "input": {
                    "sort": {"direction": "ASC", "field": "date"},
                    "filter": {
                        "search": "PM Fee Accrual | 1321 Allendale Ave | June 2026",
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
    rows = [row for row in result if str(row.get("id")) == ALLENDALE_DUPLICATE_PM_ID]
    if len(rows) != 1:
        raise RuntimeError("Allendale duplicate PM accrual identity is missing or ambiguous")
    row = rows[0]
    actual = (
        Decimal(str(row.get("amount"))),
        str(row.get("date")),
        str(row.get("propertyId")),
        str(row.get("tagId")),
        bool(row.get("isManual")),
    )
    expected = (
        Decimal("-37.05"),
        "2026-06-28",
        ALLENDALE_PROPERTY_ID,
        "80",
        True,
    )
    marker = "AOPS-MONTHLY-ACCRUAL|pm|1321 Allendale Ave|2026-06|37.05"
    if actual != expected or marker not in note_text(row.get("note")):
        raise RuntimeError(
            f"Allendale duplicate PM accrual changed: actual={actual!r}"
        )
    return {
        "id": ALLENDALE_DUPLICATE_PM_ID,
        "amount": "-37.05",
        "date": "2026-06-28",
        "property_id": ALLENDALE_PROPERTY_ID,
        "reason": (
            "Hemlane deposited $940.50 net rent after its direct PM split; "
            "the additional $37.05 manual PM accrual would double count PM."
        ),
    }


def build_public() -> tuple[dict[str, Any], dict[str, Any]]:
    opening = balances()
    duplicate = allendale_duplicate()
    amount = Decimal(SPEC["amount"])
    ending_dille = opening.get(DILLE_ACCOUNT, Decimal()) - amount
    issues: list[str] = []
    if set(opening) != {DILLE_ACCOUNT, ECO_ACCOUNT}:
        issues.append("Dille operations or ECO 2624 is unavailable")
    if ending_dille != TARGET_ENDING:
        issues.append(
            f"Dille ending balance would be {ending_dille}, not {TARGET_ENDING}"
        )
    plan = build_transfer_plan(
        from_transfer_account_id=DILLE_ACCOUNT,
        to_transfer_account_id=ECO_ACCOUNT,
        amount=SPEC["amount"],
        bookkeeping_note=SPEC["bookkeeping_note"],
        property_id=DILLE_PROPERTY_ID,
        tag_id=24,
        same_day=True,
    )
    public = {
        "scope": "Ohio 3 package accrual and cash cleanup",
        "issues": issues,
        "allendale_pm_void": duplicate,
        "cash_flow_invariant": {
            "internal_only": True,
            "tag_id": "24",
            "category": "Transfers Between Accounts",
            "pm_cash_included": "0.00",
            "earldao_loan_recharacterized_as_eco_revenue": False,
            "dille_ending_float": "500.00",
        },
        "opening_available_balances": {
            "Dille": format(opening.get(DILLE_ACCOUNT, Decimal()), ".2f"),
            "ECO": format(opening.get(ECO_ACCOUNT, Decimal()), ".2f"),
        },
        "forecast_available_balances": {
            "Dille": format(ending_dille, ".2f"),
            "ECO": format(opening.get(ECO_ACCOUNT, Decimal()) + amount, ".2f"),
        },
        "transfer": {
            "direction": "Dille -> ECO",
            "amount": SPEC["amount"],
            "property_id": DILLE_PROPERTY_ID,
            "parent_label": SPEC["parent_label"],
            "bookkeeping_note": SPEC["bookkeeping_note"],
            "note": SPEC["note"],
            "components": [
                {"label": label, "amount": value, "tag_id": "24"}
                for label, value in SPEC["components"]
            ],
            "confirmation_token": plan["confirmation_token"],
        },
    }
    return public, plan


def payload_digest(public: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_report(name: str, payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def void_allendale_pm() -> list[dict[str, Any]]:
    result = run_graphql(
        {
            "operationName": "UpdateTransaction",
            "variables": {
                "input": [
                    {
                        "id": ALLENDALE_DUPLICATE_PM_ID,
                        "amount": 0,
                        "note": (
                            "ECO-VOID-DUPLICATE-PM|1321 Allendale Ave|2026-06|"
                            "313978481 | Original -37.05 manual accrual voided. "
                            "June $940.50 was net rent after Hemlane's direct PM "
                            "split, so no additional PM accrual or cash payment is due."
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
        or str(result[0].get("id")) != ALLENDALE_DUPLICATE_PM_ID
        or Decimal(str(result[0].get("amount"))) != Decimal("0")
    ):
        raise RuntimeError(f"Allendale PM void response mismatch: {result!r}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()

    public, plan = build_public()
    digest = payload_digest(public)
    dry = {"status": "dry_run", "digest": digest, **public}
    dry_path = write_report("ohio3_cash_cleanup_dry_run.json", dry)
    if not args.apply:
        print(json.dumps({**dry, "report": str(dry_path)}, indent=2))
        return 0 if not public["issues"] else 2
    if args.digest != digest:
        raise RuntimeError(f"live digest is {digest}; exact --digest required")
    if public["issues"]:
        raise RuntimeError(f"refusing apply with issues: {public['issues']}")

    voided = void_allendale_pm()
    receipt = execute_transfer(
        plan=plan,
        confirmation_token=plan["confirmation_token"],
        graphql_runner=graphql,
        state_path=STATE_PATH,
    )
    audit = ensure_native_audit(SPEC, plan)
    ending = balances()
    if ending.get(DILLE_ACCOUNT) != TARGET_ENDING:
        raise RuntimeError("post-transfer Dille balance does not equal $500")
    applied = {
        "status": "submitted_and_verified",
        "digest": digest,
        **public,
        "allendale_pm_void_response": voided,
        "ending_available_balances": {
            "Dille": format(ending[DILLE_ACCOUNT], ".2f"),
            "ECO": format(ending[ECO_ACCOUNT], ".2f"),
        },
        "receipt": receipt,
        "native_audit": audit,
    }
    path = write_report("ohio3_cash_cleanup_applied.json", applied)
    print(json.dumps({**applied, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
