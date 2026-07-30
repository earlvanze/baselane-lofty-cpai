#!/usr/bin/env python3
"""Guarded Circle -> ECO accrued-fee/tax cash settlement with native splits."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

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
SOURCE_ACCOUNT = 113139
ECO_ACCOUNT = 29732
PROPERTY_ID = "80460"
TARGET_ENDING = Decimal("3232.88")

SPEC: dict[str, Any] = {
    "key": "Circle_to_ECO_accrual_cash",
    "from": SOURCE_ACCOUNT,
    "to": ECO_ACCOUNT,
    "amount": "1496.77",
    "property_id": PROPERTY_ID,
    "bookkeeping_note": "Circle funded DAO fee and tax accrual cash through May 2026",
    "note": (
        "25 Circle Dr: collect funded Jan-Jun 2026 DAO LLC/admin fees and "
        "partially fund Jan-May 2026 property-tax accruals. PM excluded because "
        "Hemlane withheld/remitted PM before depositing net rent. Internal only."
    ),
    "parent_label": "Circle-ECO | DAO fee + tax accrual cash | through 2026-05",
    "components": [
        ["Circle-ECO | DAO LLC fee cash | 2026-01", "62.50"],
        ["Circle-ECO | DAO LLC fee cash | 2026-02", "62.50"],
        ["Circle-ECO | DAO LLC fee cash | 2026-03", "62.50"],
        ["Circle-ECO | DAO LLC fee cash | 2026-04", "62.50"],
        ["Circle-ECO | DAO LLC fee cash | 2026-05", "62.50"],
        ["Circle-ECO | DAO LLC fee cash | 2026-06", "62.50"],
        ["Circle-ECO | property tax accrual cash | 2026-01", "259.55"],
        ["Circle-ECO | property tax accrual cash | 2026-02", "259.55"],
        ["Circle-ECO | property tax accrual cash | 2026-03", "259.55"],
        ["Circle-ECO | property tax accrual cash | 2026-04", "259.55"],
        ["Circle-ECO | property tax accrual cash | 2026-05 | partial", "83.57"],
    ],
}


def balances() -> dict[int, Decimal]:
    wanted = {SOURCE_ACCOUNT, ECO_ACCOUNT}
    return {
        int(row["transfer_account_id"]): Decimal(str(row["available_balance"]))
        for row in list_active_transfer_accounts(graphql)
        if int(row["transfer_account_id"]) in wanted
    }


def build_public() -> tuple[dict[str, Any], dict[str, Any]]:
    opening = balances()
    amount = Decimal(SPEC["amount"])
    ending_source = opening.get(SOURCE_ACCOUNT, Decimal("0")) - amount
    issues: list[str] = []
    if set(opening) != {SOURCE_ACCOUNT, ECO_ACCOUNT}:
        issues.append("Circle operations or ECO 2624 is unavailable")
    if ending_source != TARGET_ENDING:
        issues.append(
            f"Circle ending balance would be {ending_source}, not {TARGET_ENDING}"
        )
    if ending_source < Decimal("500.00"):
        issues.append("settlement would leave Circle below the $500 floor")

    plan = build_transfer_plan(
        from_transfer_account_id=SOURCE_ACCOUNT,
        to_transfer_account_id=ECO_ACCOUNT,
        amount=SPEC["amount"],
        bookkeeping_note=SPEC["bookkeeping_note"],
        property_id=PROPERTY_ID,
        tag_id=24,
        same_day=True,
    )
    public = {
        "scope": "Circle -> ECO funded accrual cash settlement",
        "issues": issues,
        "cash_flow_invariant": {
            "internal_only": True,
            "tag_id": "24",
            "category": "Transfers Between Accounts",
            "pm_cash_included": "0.00",
            "pm_reason": "Hemlane direct split before net-rent deposit",
            "circle_ending_equals_gl_target": format(TARGET_ENDING, ".2f"),
        },
        "opening_available_balances": {
            "Circle": format(opening.get(SOURCE_ACCOUNT, Decimal("0")), ".2f"),
            "ECO": format(opening.get(ECO_ACCOUNT, Decimal("0")), ".2f"),
        },
        "forecast_available_balances": {
            "Circle": format(ending_source, ".2f"),
            "ECO": format(
                opening.get(ECO_ACCOUNT, Decimal("0")) + amount, ".2f"
            ),
        },
        "transfer": {
            "direction": "Circle -> ECO",
            "amount": SPEC["amount"],
            "property_id": PROPERTY_ID,
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


def digest(public: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_report(name: str, payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()
    if args.apply and args.audit_only:
        parser.error("--apply and --audit-only are mutually exclusive")

    public, plan = build_public()
    plan_digest = digest(public)
    dry = {"status": "dry_run", "digest": plan_digest, **public}
    dry_path = write_report("circle_cash_settlement_dry_run.json", dry)
    if args.audit_only:
        audit = ensure_native_audit(SPEC, plan)
        payload = {"status": "existing_cash_rows_audited", **audit}
        path = write_report("circle_cash_settlement_audit_only.json", payload)
        print(json.dumps({**payload, "report": str(path)}, indent=2))
        return 0
    if not args.apply:
        print(json.dumps({**dry, "report": str(dry_path)}, indent=2))
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
    if ending.get(SOURCE_ACCOUNT) != TARGET_ENDING:
        raise RuntimeError("post-transfer Circle balance does not equal GL target")
    applied = {
        "status": "submitted_and_verified",
        "digest": plan_digest,
        **public,
        "ending_available_balances": {
            "Circle": format(ending[SOURCE_ACCOUNT], ".2f"),
            "ECO": format(ending[ECO_ACCOUNT], ".2f"),
        },
        "receipt": receipt,
        "native_audit": audit,
    }
    path = write_report("circle_cash_settlement_applied.json", applied)
    print(json.dumps({**applied, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
