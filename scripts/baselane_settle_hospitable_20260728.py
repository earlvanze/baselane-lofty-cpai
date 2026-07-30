#!/usr/bin/env python3
"""Settle the July 28 Hospitable allocation into 88 Madison and fund 90's repair."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from baselane_mcp.transfers import (
    build_transfer_plan,
    execute_transfer,
    list_active_transfer_accounts,
)
from baselane_settle_madison_pm_mortgage import (
    STATE_PATH,
    ensure_native_audit,
    graphql,
)


# Keep runtime artifacts on the ext4 OpenClaw workspace even though the scripts
# directory is a compatibility symlink into the older Dropbox tree.
ROOT = Path(__file__).absolute().parents[1]
REPORTS = ROOT / "reports"
TARGET_88 = 70936
TARGET_90 = 70935
EARLDAO = 105899
ECO = 29732

SPECS: list[dict[str, Any]] = [
    {
        "key": "eco_to_earldao_liquidity_bridge",
        "from": ECO,
        "to": EARLDAO,
        "amount": "135.59",
        "property_id": "31525",
        "bookkeeping_note": "ECO liquidity bridge to EARLDAO for 90 Madison July funding",
        "note": (
            "Exact liquidity bridge enabling EARLDAO to fund 90 Madison while "
            "preserving EARLDAO as the lender of record."
        ),
        "parent_label": "ECO-EARLDAO | 90 Jul liquidity bridge | 135.59",
        "components": [["ECO-EARLDAO | 90 Jul liquidity bridge", "135.59"]],
    },
    {
        "key": "earldao_to_90_repair_funding",
        "from": EARLDAO,
        "to": TARGET_90,
        "amount": "1031.81",
        "property_id": "31525",
        "bookkeeping_note": "90 Madison July repair funding after Hospitable reimbursement",
        "note": (
            "EARLDAO capital advance to 90 Madison: $490.00 remaining funding "
            "for the approved $730.00 maintenance and repairs payment after the "
            "live balance fell to $240.00, $500.00 post-payment operating float, "
            "plus $41.81 needed to reimburse 88 Madison for 90's July 28 "
            "Hospitable share."
        ),
        "parent_label": "EARLDAO-90 | Jul repair and float | 1031.81",
        "components": [
            ["EARLDAO-90 | Jul repair payment shortfall", "490.00"],
            ["EARLDAO-90 | Post-payment operating float", "500.00"],
            ["EARLDAO-90 | Hospitable reimbursement funding", "41.81"],
        ],
    },
    {
        "key": "umland_to_88_hospitable",
        "from": 95650,
        "to": TARGET_88,
        "amount": "13.93",
        "property_id": "83184",
        "bookkeeping_note": "Umland reimbursement to 88 Madison for 2026-07-28 Hospitable",
        "note": "22164 Umland Circle share of the July 28, 2026 Hospitable charge paid by 88 Madison.",
        "parent_label": "Umland-88 | Hospitable | 2026-07-28",
        "components": [["Umland-88 | Hospitable | 2026-07-28", "13.93"]],
    },
    {
        "key": "pillar_to_88_hospitable",
        "from": 149359,
        "to": TARGET_88,
        "amount": "13.93",
        "property_id": "109641",
        "bookkeeping_note": "27 Pillar reimbursement to 88 Madison for 2026-07-28 Hospitable",
        "note": "27 Pillar Ln share of the July 28, 2026 Hospitable charge paid by 88 Madison.",
        "parent_label": "Pillar-88 | Hospitable | 2026-07-28",
        "components": [["Pillar-88 | Hospitable | 2026-07-28", "13.93"]],
    },
    {
        "key": "country_club_to_88_hospitable",
        "from": 107185,
        "to": TARGET_88,
        "amount": "13.93",
        "property_id": "91341",
        "bookkeeping_note": "9 Country Club reimbursement to 88 Madison for 2026-07-28 Hospitable",
        "note": "9 Country Club Ln N share of the July 28, 2026 Hospitable charge paid by 88 Madison.",
        "parent_label": "9CC-88 | Hospitable | 2026-07-28",
        "components": [["9CC-88 | Hospitable | 2026-07-28", "13.93"]],
    },
    {
        "key": "84_to_88_hospitable",
        "from": 65485,
        "to": TARGET_88,
        "amount": "27.87",
        "property_id": "60548",
        "bookkeeping_note": "84 Madison reimbursement to 88 Madison for 2026-07-28 Hospitable",
        "note": "84 Madison Ave share of the July 28, 2026 Hospitable charge paid by 88 Madison.",
        "parent_label": "84-88 | Hospitable | 2026-07-28",
        "components": [["84-88 | Hospitable | 2026-07-28", "27.87"]],
    },
    {
        "key": "86_to_88_hospitable",
        "from": 70125,
        "to": TARGET_88,
        "amount": "27.87",
        "property_id": "63162",
        "bookkeeping_note": "86 Madison reimbursement to 88 Madison for 2026-07-28 Hospitable",
        "note": "86 Madison Ave share of the July 28, 2026 Hospitable charge paid by 88 Madison.",
        "parent_label": "86-88 | Hospitable | 2026-07-28",
        "components": [["86-88 | Hospitable | 2026-07-28", "27.87"]],
    },
    {
        "key": "90_to_88_hospitable",
        "from": TARGET_90,
        "to": TARGET_88,
        "amount": "41.81",
        "property_id": "31525",
        "bookkeeping_note": "90 Madison reimbursement to 88 Madison for 2026-07-28 Hospitable",
        "note": "90 Madison Ave share of the July 28, 2026 Hospitable charge paid by 88 Madison.",
        "parent_label": "90-88 | Hospitable | 2026-07-28",
        "components": [["90-88 | Hospitable | 2026-07-28", "41.81"]],
    },
]


def live_balances() -> dict[int, Decimal]:
    return {
        int(row["transfer_account_id"]): Decimal(str(row["available_balance"]))
        for row in list_active_transfer_accounts(graphql)
    }


def build() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    opening = live_balances()
    forecast = dict(opening)
    plans: list[dict[str, Any]] = []
    issues: list[str] = []
    for spec in SPECS:
        source = int(spec["from"])
        destination = int(spec["to"])
        amount = Decimal(spec["amount"])
        if source not in forecast or destination not in forecast:
            issues.append(f"missing_transfer_account:{spec['key']}")
            continue
        if forecast[source] < amount:
            issues.append(f"insufficient_funds:{spec['key']}")
        forecast[source] -= amount
        forecast[destination] += amount
        plans.append(
            build_transfer_plan(
                from_transfer_account_id=source,
                to_transfer_account_id=destination,
                amount=spec["amount"],
                bookkeeping_note=spec["bookkeeping_note"],
                property_id=spec["property_id"],
                tag_id=24,
                same_day=True,
            )
        )
    if forecast.get(TARGET_90) != Decimal("1230.00"):
        issues.append(
            f"90_ending_balance_not_1230:{forecast.get(TARGET_90, Decimal('0')):.2f}"
        )
    public = {
        "scope": "2026-07-28 Hospitable reimbursements and 90 Madison repair funding",
        "issues": issues,
        "invariants": {
            "internal_transfers_only": True,
            "tag_id": "24",
            "category": "Transfers Between Accounts",
            "90_pre_payment_balance": "1230.00",
            "90_repair_payment": "730.00",
            "90_post_payment_float": "500.00",
        },
        "opening_balances": {
            str(key): f"{opening[key]:.2f}"
            for key in sorted({int(s["from"]) for s in SPECS} | {TARGET_88, TARGET_90})
        },
        "forecast_balances": {
            str(key): f"{forecast[key]:.2f}"
            for key in sorted({int(s["from"]) for s in SPECS} | {TARGET_88, TARGET_90})
        },
        "transfers": [
            {
                "key": spec["key"],
                "from_transfer_account_id": spec["from"],
                "to_transfer_account_id": spec["to"],
                "amount": spec["amount"],
                "property_id": spec["property_id"],
                "label": spec["parent_label"],
                "confirmation_token": plan["confirmation_token"],
            }
            for spec, plan in zip(SPECS, plans)
        ],
        "unsettled_payable": {
            "property": "22 W Main St",
            "property_id": "97162",
            "amount": "13.93",
            "payable_to": "88 Madison Ave",
            "reason": "No active eligible Baselane internal-transfer account.",
        },
    }
    return public, plans


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_report(name: str, payload: dict[str, Any]) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()

    public, plans = build()
    plan_digest = digest(public)
    if not args.apply:
        payload = {"status": "dry_run", "digest": plan_digest, **public}
        path = write_report("hospitable_20260728_cash_settlement.dry-run.json", payload)
        print(json.dumps({**payload, "report": str(path)}, indent=2))
        return 0 if not public["issues"] else 2
    if args.digest != plan_digest:
        raise RuntimeError(f"live digest is {plan_digest}; exact --digest required")
    if public["issues"]:
        raise RuntimeError(f"refusing apply with issues: {public['issues']}")

    receipts: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    for spec, plan in zip(SPECS, plans):
        receipts.append(
            execute_transfer(
                plan=plan,
                confirmation_token=plan["confirmation_token"],
                graphql_runner=graphql,
                state_path=STATE_PATH,
            )
        )
        audits.append({"key": spec["key"], **ensure_native_audit(spec, plan)})

    ending = live_balances()
    if ending[TARGET_90] != Decimal("1230.00"):
        raise RuntimeError(f"90 Madison ended at {ending[TARGET_90]:.2f}, not 1230.00")
    payload = {
        "status": "submitted_and_verified",
        "digest": plan_digest,
        **public,
        "ending_balances": {
            str(key): f"{ending[key]:.2f}"
            for key in sorted({int(s["from"]) for s in SPECS} | {TARGET_88, TARGET_90})
        },
        "receipts": receipts,
        "audits": audits,
    }
    path = write_report("hospitable_20260728_cash_settlement.applied.json", payload)
    print(json.dumps({**payload, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
