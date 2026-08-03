#!/usr/bin/env python3
"""Guarded Sangamon -> ECO settlement, capped by security deposits plus cash floor."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from baselane_settle_madison_pm_mortgage import STATE_PATH, ensure_native_audit, graphql
from baselane_mcp.transfers import build_transfer_plan, execute_transfer, list_active_transfer_accounts


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports"
RECONCILIATION_REPORT = REPORT_DIR / "sangamon_live_cash_reconciliation_20260803.json"
OPERATIONS_ACCOUNT = 94231
RESERVE_ACCOUNT = 102779
ECO_ACCOUNT = 29732
PROPERTY_ID = "83181"
CASH_FLOOR = Decimal("1000.00")


def cents(value: Any) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def balances() -> dict[int, Decimal]:
    wanted = {OPERATIONS_ACCOUNT, RESERVE_ACCOUNT, ECO_ACCOUNT}
    return {
        int(row["transfer_account_id"]): cents(row["available_balance"])
        for row in list_active_transfer_accounts(graphql)
        if int(row["transfer_account_id"]) in wanted
    }


def reconciliation() -> dict[str, Any]:
    payload = json.loads(RECONCILIATION_REPORT.read_text(encoding="utf-8"))
    # Current reconciliation schema exposes a top-level issues array; older
    # reports also emitted issue_count. Accept either representation, but never
    # proceed when an actual issue is present.
    issue_count = payload.get("issue_count")
    if payload.get("status") != "ok" or payload.get("issues") or issue_count not in (None, 0):
        raise RuntimeError("Sangamon reconciliation report is not clean")
    matches = [
        row for row in payload.get("properties", [])
        if row.get("property") == "8143 S Sangamon St."
    ]
    if len(matches) != 1:
        raise RuntimeError("Sangamon property is not unique in reconciliation report")
    return matches[0]


def build_public() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    opening = balances()
    row = reconciliation()
    issues: list[str] = []
    if set(opening) != {OPERATIONS_ACCOUNT, RESERVE_ACCOUNT, ECO_ACCOUNT}:
        issues.append("Sangamon operations/reserves or ECO 2624 is unavailable")

    security_principal = cents(row.get("documented_security_principal"))
    if security_principal != Decimal("0.00"):
        issues.append(
            "guarded 2026-08-03 evidence expected no documented security principal; "
            f"found {security_principal:.2f}"
        )
    savings = next(
        (account for account in row.get("accounts", []) if account.get("transfer_account_id") == RESERVE_ACCOUNT),
        {},
    )
    evidence = savings.get("savings_evidence") or {}
    if evidence.get("documented_security_principal") is not None:
        issues.append("savings evidence unexpectedly classifies principal as tenant security deposits")

    cash_advances = cents(row.get("dao_accounts_payable_to_eco"))
    included_eco = {
        str(item.get("category")): cents(item.get("amount"))
        for item in row.get("dao_accounts_payable_by_counterparty", [])
        if item.get("counterparty") == "ECO Systems LLC"
        and item.get("cash_effect") == "included_in_recorded_unpaid_obligations"
    }
    dao_fee = included_eco.get("dao", Decimal("0"))
    pm_fee = included_eco.get("pm", Decimal("0"))
    payable = cash_advances + dao_fee + pm_fee
    combined = opening.get(OPERATIONS_ACCOUNT, Decimal("0")) + opening.get(RESERVE_ACCOUNT, Decimal("0"))
    cash_capacity = max(Decimal("0"), combined - security_principal - CASH_FLOOR)
    amount = min(payable, cash_capacity)
    if amount != payable:
        issues.append("cash cap prevents full settlement; component allocator requires review")
    if opening.get(RESERVE_ACCOUNT, Decimal("0")) < amount:
        issues.append("Sangamon reserves cannot fund the guarded transfer")

    components = [
        ["Sangamon-ECO | verified cash advances | through 2026-07-31", format(cash_advances, ".2f")],
        ["Sangamon-ECO | DAO LLC fee payable | through 2026-07-31", format(dao_fee, ".2f")],
        ["Sangamon-ECO | PM fee payable | through 2026-07-31", format(pm_fee, ".2f")],
    ]
    if sum((Decimal(value) for _label, value in components), Decimal("0")) != amount:
        issues.append("native audit components do not equal guarded transfer amount")

    spec: dict[str, Any] = {
        "key": "Sangamon_to_ECO_verified_payables_20260803",
        "from": RESERVE_ACCOUNT,
        "to": ECO_ACCOUNT,
        "amount": format(amount, ".2f"),
        "property_id": PROPERTY_ID,
        "bookkeeping_note": "8143 Sangamon settlement of verified ECO payables through 2026-07-31",
        "note": (
            "8143 S Sangamon St settlement to ECO: verified cash advances, DAO LLC fee, "
            "and PM fee. Internal transfer only; retain documented tenant security deposits "
            "plus $1,000 across DAO bank accounts."
        ),
        "parent_label": "Sangamon-ECO | verified payable settlement | through 2026-07-31",
        "components": components,
    }
    plan = build_transfer_plan(
        from_transfer_account_id=RESERVE_ACCOUNT,
        to_transfer_account_id=ECO_ACCOUNT,
        amount=spec["amount"],
        bookkeeping_note=spec["bookkeeping_note"],
        property_id=PROPERTY_ID,
        tag_id=24,
        same_day=True,
    )
    public = {
        "scope": "8143 S Sangamon -> ECO verified payable settlement",
        "issues": issues,
        "cutoff": "2026-08-03",
        "evidence": {
            "reconciliation_report": str(RECONCILIATION_REPORT),
            "documented_security_principal": format(security_principal, ".2f"),
            "documented_reserve_principal_not_security": evidence.get("documented_reserve_principal"),
            "cash_advances_due_eco": format(cash_advances, ".2f"),
            "dao_fee_due_eco": format(dao_fee, ".2f"),
            "pm_fee_due_eco": format(pm_fee, ".2f"),
            "total_due_eco": format(payable, ".2f"),
        },
        "cash_guard": {
            "security_deposits_retained": format(security_principal, ".2f"),
            "additional_cash_floor": format(CASH_FLOOR, ".2f"),
            "available_above_guard": format(cash_capacity, ".2f"),
        },
        "opening_available_balances": {
            "Sangamon_operations": format(opening.get(OPERATIONS_ACCOUNT, 0), ".2f"),
            "Sangamon_reserves": format(opening.get(RESERVE_ACCOUNT, 0), ".2f"),
            "Sangamon_combined": format(combined, ".2f"),
            "ECO_2624": format(opening.get(ECO_ACCOUNT, 0), ".2f"),
        },
        "forecast_available_balances": {
            "Sangamon_operations": format(opening.get(OPERATIONS_ACCOUNT, 0), ".2f"),
            "Sangamon_reserves": format(opening.get(RESERVE_ACCOUNT, 0) - amount, ".2f"),
            "Sangamon_combined": format(combined - amount, ".2f"),
            "ECO_2624": format(opening.get(ECO_ACCOUNT, 0) + amount, ".2f"),
        },
        "transfer": {
            "direction": "Sangamon reserves -> ECO 2624",
            "amount": format(amount, ".2f"),
            "property_id": PROPERTY_ID,
            "tag_id": "24",
            "category": "Transfers Between Accounts",
            "parent_label": spec["parent_label"],
            "bookkeeping_note": spec["bookkeeping_note"],
            "note": spec["note"],
            "components": [
                {"label": label, "amount": value, "tag_id": "24"}
                for label, value in components
            ],
            "confirmation_token": plan["confirmation_token"],
        },
    }
    return public, plan, spec


def digest(public: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(public, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_report(name: str, payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()

    public, plan, spec = build_public()
    plan_digest = digest(public)
    dry = {"status": "dry_run", "digest": plan_digest, **public}
    dry_path = write_report("sangamon_eco_payable_settlement.dry.json", dry)
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
    audit = ensure_native_audit(spec, plan)
    if audit.get("status") != "verified_native_splits":
        raise RuntimeError(f"native split mirrors not verified: {audit}")
    ending = balances()
    ending_combined = ending[OPERATIONS_ACCOUNT] + ending[RESERVE_ACCOUNT]
    expected_combined = Decimal(public["forecast_available_balances"]["Sangamon_combined"])
    if ending_combined != expected_combined:
        raise RuntimeError(f"post-transfer Sangamon cash {ending_combined:.2f} != {expected_combined:.2f}")
    applied = {
        "status": "submitted_and_verified",
        "digest": plan_digest,
        **public,
        "ending_available_balances": {
            "Sangamon_operations": format(ending[OPERATIONS_ACCOUNT], ".2f"),
            "Sangamon_reserves": format(ending[RESERVE_ACCOUNT], ".2f"),
            "Sangamon_combined": format(ending_combined, ".2f"),
            "ECO_2624": format(ending[ECO_ACCOUNT], ".2f"),
        },
        "receipt": receipt,
        "native_audit": audit,
    }
    path = write_report("sangamon_eco_payable_settlement.applied.json", applied)
    print(json.dumps({**applied, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
