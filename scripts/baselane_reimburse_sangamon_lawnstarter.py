#!/usr/bin/env python3
"""Guarded Sangamon -> ECO reimbursement for three ECO-paid LawnStarter charges."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from baselane_settle_madison_pm_mortgage import STATE_PATH, ensure_native_audit, graphql
from baselane_mcp.transfers import (
    build_transfer_plan,
    execute_transfer,
    list_active_transfer_accounts,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports"
MASTER_LEDGER = Path(
    "/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"
)
SOURCE_ACCOUNT = 94231
RESERVE_ACCOUNT = 102779
ECO_ACCOUNT = 29732
PROPERTY_ID = "83181"
PORTFOLIO_FLOOR = Decimal("500.00")

SPEC: dict[str, Any] = {
    "key": "Sangamon_to_ECO_LawnStarter_2026_05_06",
    "from": SOURCE_ACCOUNT,
    "to": ECO_ACCOUNT,
    "amount": "245.97",
    "property_id": PROPERTY_ID,
    "bookkeeping_note": (
        "8143 Sangamon reimbursement to ECO for LawnStarter "
        "2026-05-27, 2026-06-08, 2026-06-24"
    ),
    "note": (
        "8143 S Sangamon St: reimburse three ECO-paid LawnStarter charges. "
        "Internal transfer only; native components preserve charge date/category audit."
    ),
    "parent_label": "Sangamon-ECO | LawnStarter reimbursement | 2026-05/06",
    "components": [
        ["Sangamon-ECO | LawnStarter | 2026-05-27", "127.99"],
        ["Sangamon-ECO | LawnStarter | 2026-06-08", "58.99"],
        ["Sangamon-ECO | LawnStarter | 2026-06-24", "58.99"],
    ],
}


def cents(value: Any) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def balances() -> dict[int, Decimal]:
    wanted = {SOURCE_ACCOUNT, RESERVE_ACCOUNT, ECO_ACCOUNT}
    return {
        int(row["transfer_account_id"]): cents(row["available_balance"])
        for row in list_active_transfer_accounts(graphql)
        if int(row["transfer_account_id"]) in wanted
    }


def source_evidence() -> dict[str, Any]:
    expected = {
        ("May 27, 2026", Decimal("-127.99")),
        ("June 08, 2026", Decimal("-58.99")),
        ("June 24, 2026", Decimal("-58.99")),
    }
    matches: list[dict[str, str]] = []
    with MASTER_LEDGER.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            key = (str(row.get("Date") or ""), cents(row.get("Amount")))
            if (
                key in expected
                and "Sangamon" in str(row.get("Property") or "")
                and "LAWNSTARTER" in (
                    str(row.get("Merchant") or "")
                    + str(row.get("Description") or "")
                ).upper()
                and str(row.get("Account") or "").startswith(
                    "ECO Systems, LLC-ECO Systems Operations-2624"
                )
            ):
                matches.append(
                    {
                        "date": key[0],
                        "amount": format(key[1], ".2f"),
                        "account": str(row.get("Account") or ""),
                        "merchant": str(row.get("Merchant") or ""),
                        "property": str(row.get("Property") or "").strip(),
                    }
                )
    matches.sort(key=lambda row: (row["date"], row["amount"]))
    found = {(row["date"], Decimal(row["amount"])) for row in matches}
    return {
        "status": "verified" if found == expected and len(matches) == 3 else "mismatch",
        "row_count": len(matches),
        "amount": format(
            -sum((Decimal(row["amount"]) for row in matches), Decimal("0")), ".2f"
        ),
        "rows": matches,
    }


def build_public() -> tuple[dict[str, Any], dict[str, Any]]:
    opening = balances()
    evidence = source_evidence()
    amount = Decimal(SPEC["amount"])
    source_ending = opening.get(SOURCE_ACCOUNT, Decimal("0")) - amount
    combined_ending = source_ending + opening.get(RESERVE_ACCOUNT, Decimal("0"))
    issues: list[str] = []
    if set(opening) != {SOURCE_ACCOUNT, RESERVE_ACCOUNT, ECO_ACCOUNT}:
        issues.append("Sangamon operations/reserves or ECO 2624 is unavailable")
    if evidence["status"] != "verified" or evidence["amount"] != SPEC["amount"]:
        issues.append("canonical GL does not contain exactly the three supported charges")
    if source_ending < Decimal("500.00"):
        issues.append("Sangamon operations would end below the $500 operating float")
    if combined_ending < PORTFOLIO_FLOOR:
        issues.append("Sangamon combined bank cash would end below the $500 floor")

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
        "scope": "8143 S Sangamon -> ECO LawnStarter reimbursement",
        "issues": issues,
        "source_evidence": evidence,
        "cash_flow_invariant": {
            "internal_only": True,
            "tag_id": "24",
            "category": "Transfers Between Accounts",
            "combined_cash_floor": format(PORTFOLIO_FLOOR, ".2f"),
            "reserve_policy": (
                "Sangamon is not a mortgage co-ownership; retain at least $500. "
                "Additional cash remains in the DAO for the active back-porch project."
            ),
        },
        "opening_available_balances": {
            "Sangamon_operations": format(
                opening.get(SOURCE_ACCOUNT, Decimal("0")), ".2f"
            ),
            "Sangamon_reserves": format(
                opening.get(RESERVE_ACCOUNT, Decimal("0")), ".2f"
            ),
            "ECO_2624": format(opening.get(ECO_ACCOUNT, Decimal("0")), ".2f"),
        },
        "forecast_available_balances": {
            "Sangamon_operations": format(source_ending, ".2f"),
            "Sangamon_reserves": format(
                opening.get(RESERVE_ACCOUNT, Decimal("0")), ".2f"
            ),
            "Sangamon_combined": format(combined_ending, ".2f"),
            "ECO_2624": format(
                opening.get(ECO_ACCOUNT, Decimal("0")) + amount, ".2f"
            ),
        },
        "transfer": {
            "direction": "Sangamon -> ECO",
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
    dry_path = write_report("sangamon_lawnstarter_reimbursement.dry.json", dry)
    if args.audit_only:
        audit = ensure_native_audit(SPEC, plan)
        payload = {"status": "existing_cash_rows_audited", **audit}
        path = write_report("sangamon_lawnstarter_reimbursement.audit.json", payload)
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
    if audit.get("status") != "verified_native_splits":
        raise RuntimeError(f"native split mirrors not verified: {audit}")
    ending = balances()
    applied = {
        "status": "submitted_and_verified",
        "digest": plan_digest,
        **public,
        "ending_available_balances": {
            "Sangamon_operations": format(ending[SOURCE_ACCOUNT], ".2f"),
            "Sangamon_reserves": format(ending[RESERVE_ACCOUNT], ".2f"),
            "Sangamon_combined": format(
                ending[SOURCE_ACCOUNT] + ending[RESERVE_ACCOUNT], ".2f"
            ),
            "ECO_2624": format(ending[ECO_ACCOUNT], ".2f"),
        },
        "receipt": receipt,
        "native_audit": audit,
    }
    path = write_report("sangamon_lawnstarter_reimbursement.applied.json", applied)
    print(json.dumps({**applied, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
