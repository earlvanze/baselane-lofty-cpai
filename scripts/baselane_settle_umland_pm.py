#!/usr/bin/env python3
"""Guarded Umland -> ECO PM-fee cash settlement with native audit splits."""

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
SOURCE_ACCOUNT = 95650
ECO_ACCOUNT = 29732
PROPERTY_ID = "83184"
FLOAT = Decimal("500.00")

SPEC: dict[str, Any] = {
    "key": "Umland_to_ECO_PM",
    "from": SOURCE_ACCOUNT,
    "to": ECO_ACCOUNT,
    "amount": "1182.49",
    "property_id": PROPERTY_ID,
    "bookkeeping_note": (
        "Umland ECO PM and earned DAO fee settlement through February 2026"
    ),
    "note": (
        "22164 Umland Circle: unpaid 15% PM accruals for Dec25 $417.72, "
        "Jan26 $298.15, and Feb26 $404.12, plus Jan26 DAO LLC fee $62.50. "
        "DAO fee is earned: ECO paid Corporate Filings LLC $45 on 2026-03-10 "
        "and $45 on 2026-03-13. Internal only."
    ),
    "parent_label": "Umland-ECO | PM + DAO fee | through 2026-02",
    "components": [
        ["Umland-ECO | PM cash | 2025-12", "417.72"],
        ["Umland-ECO | PM cash | 2026-01", "298.15"],
        ["Umland-ECO | DAO LLC fee cash | 2026-01", "62.50"],
        ["Umland-ECO | PM cash | 2026-02", "404.12"],
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
    issues: list[str] = []
    if set(opening) != {SOURCE_ACCOUNT, ECO_ACCOUNT}:
        issues.append("Umland operations or ECO 2624 is unavailable")
    if opening.get(SOURCE_ACCOUNT, Decimal("-1")) < amount:
        issues.append("settlement exceeds Umland available balance")
    ending_source = opening.get(SOURCE_ACCOUNT, Decimal("0")) - amount
    if ending_source < FLOAT:
        issues.append("settlement would leave Umland below the $500 floor")

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
        "scope": "Umland -> ECO unpaid PM cash settlement",
        "issues": issues,
        "cash_flow_invariant": {
            "internal_only": True,
            "tag_id": "24",
            "category": "Transfers Between Accounts",
            "native_splits_on_both_mirrors": True,
            "minimum_umland_ending_balance": "500.00",
            "dao_llc_fee_requires_underlying_cost_evidence": True,
        },
        "opening_available_balances": {
            "Umland": format(opening.get(SOURCE_ACCOUNT, Decimal("0")), ".2f"),
            "ECO": format(opening.get(ECO_ACCOUNT, Decimal("0")), ".2f"),
        },
        "forecast_available_balances": {
            "Umland": format(ending_source, ".2f"),
            "ECO": format(
                opening.get(ECO_ACCOUNT, Decimal("0")) + amount,
                ".2f",
            ),
        },
        "transfer": {
            "direction": "Umland -> ECO",
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
    dry_path = write_report("umland_pm_settlement_dry_run.json", dry)
    if args.audit_only:
        audit = ensure_native_audit(SPEC, plan)
        payload = {"status": "existing_cash_rows_audited", **audit}
        path = write_report("umland_pm_settlement_audit_only.json", payload)
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
    if ending.get(SOURCE_ACCOUNT, Decimal("-1")) < FLOAT:
        raise RuntimeError("post-transfer Umland balance is below $500")
    applied = {
        "status": "submitted_and_verified",
        "digest": plan_digest,
        **public,
        "ending_available_balances": {
            "Umland": format(ending[SOURCE_ACCOUNT], ".2f"),
            "ECO": format(ending[ECO_ACCOUNT], ".2f"),
        },
        "receipt": receipt,
        "native_audit": audit,
    }
    path = write_report("umland_pm_settlement_applied.json", applied)
    print(json.dumps({**applied, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
