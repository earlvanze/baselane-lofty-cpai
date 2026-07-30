#!/usr/bin/env python3
"""Settle 22 W Main's July 28 Hospitable share from ECO Systems to 88 Madison."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

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


ROOT = Path(__file__).absolute().parents[1]
REPORTS = ROOT / "reports"
ECO = 29732
MADISON_88 = 70936
AMOUNT = Decimal("13.93")

SPEC = {
    "key": "eco_to_88_for_22_w_main_hospitable",
    "from": ECO,
    "to": MADISON_88,
    "amount": "13.93",
    "property_id": "97162",
    "bookkeeping_note": "ECO coverage of 22 W Main 2026-07-28 Hospitable share to 88 Madison",
    "note": (
        "ECO Systems direct settlement to 88 Madison for 22 W Main St's "
        "allocated share of the July 28, 2026 Hospitable charge."
    ),
    "parent_label": "ECO-88 | 22 W Main Hospitable | 2026-07-28",
    "components": [["ECO-88 | 22 W Main Hospitable | 2026-07-28", "13.93"]],
}


def balances() -> dict[int, Decimal]:
    return {
        int(row["transfer_account_id"]): Decimal(str(row["available_balance"]))
        for row in list_active_transfer_accounts(graphql)
    }


def build() -> tuple[dict, dict]:
    opening = balances()
    issues: list[str] = []
    if ECO not in opening or MADISON_88 not in opening:
        issues.append("required_transfer_account_missing")
    elif opening[ECO] < AMOUNT:
        issues.append("eco_insufficient_funds")
    plan = build_transfer_plan(
        from_transfer_account_id=ECO,
        to_transfer_account_id=MADISON_88,
        amount=AMOUNT,
        bookkeeping_note=SPEC["bookkeeping_note"],
        property_id=SPEC["property_id"],
        tag_id=24,
        same_day=True,
    )
    public = {
        "scope": "22 W Main 2026-07-28 Hospitable settlement",
        "issues": issues,
        "opening_balances": {
            "eco": f"{opening.get(ECO, Decimal('0')):.2f}",
            "88_madison": f"{opening.get(MADISON_88, Decimal('0')):.2f}",
        },
        "transfer": {
            "from": ECO,
            "to": MADISON_88,
            "amount": "13.93",
            "property_id": "97162",
            "tag_id": "24",
            "label": SPEC["parent_label"],
            "confirmation_token": plan["confirmation_token"],
        },
    }
    return public, plan


def digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_report(name: str, payload: dict) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()

    public, plan = build()
    plan_digest = digest(public)
    if not args.apply:
        payload = {"status": "dry_run", "digest": plan_digest, **public}
        path = write_report("22_w_main_hospitable_settlement.dry-run.json", payload)
        print(json.dumps({**payload, "report": str(path)}, indent=2))
        return 0 if not public["issues"] else 2
    if args.digest != plan_digest:
        raise RuntimeError(f"live digest is {plan_digest}; exact --digest required")
    if public["issues"]:
        raise RuntimeError(f"refusing apply with issues: {public['issues']}")

    receipt = execute_transfer(
        plan=plan,
        confirmation_token=plan["confirmation_token"],
        graphql_runner=graphql,
        state_path=STATE_PATH,
    )
    audit = ensure_native_audit(SPEC, plan)
    ending = balances()
    payload = {
        "status": "submitted_and_verified",
        "digest": plan_digest,
        **public,
        "ending_balances": {
            "eco": f"{ending[ECO]:.2f}",
            "88_madison": f"{ending[MADISON_88]:.2f}",
        },
        "receipt": receipt,
        "audit": audit,
    }
    path = write_report("22_w_main_hospitable_settlement.applied.json", payload)
    print(json.dumps({**payload, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
