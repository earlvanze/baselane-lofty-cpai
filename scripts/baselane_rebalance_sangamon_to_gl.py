#!/usr/bin/env python3
"""Move Sangamon bank cash above its authoritative property-GL balance to ECO."""

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


# Keep operational state on the canonical ext4 workspace.  ``resolve()`` would
# follow the Windows compatibility symlink used by an older OpenClaw checkout.
ROOT = Path("/home/digit/.openclaw/workspace")
REPORT_DIR = ROOT / "reports"
MASTER_LEDGER = Path(
    "/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"
)
SOURCE_INDEX = ROOT / "reports/baselane_source_transaction_index.csv"
OPERATIONS_ACCOUNT = 94231
RESERVE_ACCOUNT = 102779
ECO_ACCOUNT = 29732
PROPERTY_ID = "83181"
PROPERTY_TOKEN = "Sangamon"

SPEC: dict[str, Any] = {
    "key": "Sangamon_to_ECO_exact_GL_rebalance_20260729",
    "from": RESERVE_ACCOUNT,
    "to": ECO_ACCOUNT,
    "amount": "3020.65",
    "property_id": PROPERTY_ID,
    "bookkeeping_note": (
        "8143 Sangamon excess cash to ECO custody; bank total equals property GL 2026-07-29"
    ),
    "note": (
        "8143 S Sangamon St exact cash-to-GL rebalance through 2026-07-29. "
        "Internal transfer only; no reserve add-on. Both mirrors and native "
        "components remain property-tagged Transfers Between Accounts."
    ),
    "parent_label": "Sangamon-ECO | exact cash-to-GL rebalance | 2026-07-29",
    "components": [
        ["Sangamon-ECO | LawnStarter | 2026-05-27", "127.99"],
        ["Sangamon-ECO | LawnStarter | 2026-06-08", "58.99"],
        ["Sangamon-ECO | LawnStarter | 2026-06-24", "58.99"],
        ["Sangamon-ECO | residual cash-to-GL settlement | thru 2026-07-29", "2774.68"],
    ],
}


def cents(value: Any) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def balances() -> dict[int, Decimal]:
    wanted = {OPERATIONS_ACCOUNT, RESERVE_ACCOUNT, ECO_ACCOUNT}
    return {
        int(row["transfer_account_id"]): cents(row["available_balance"])
        for row in list_active_transfer_accounts(graphql)
        if int(row["transfer_account_id"]) in wanted
    }


def gl_balance() -> Decimal:
    total = Decimal("0")
    with MASTER_LEDGER.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if PROPERTY_TOKEN not in str(row.get("Property") or ""):
                continue
            # ECO's positive company-fee counterpart is revenue belonging to ECO,
            # not property cash. The property-side negative DAO fee remains in
            # Sangamon's GL; including both would overstate Sangamon by $62.50
            # per accrued month after the master exporter adds ECO mirror rows.
            if "AOPS-OHIL-ACCRUAL|dao_eco|" in str(row.get("Notes") or ""):
                continue
            total += cents(row.get("Amount"))
    return total.quantize(Decimal("0.01"))


def lawnstarter_evidence() -> dict[str, Any]:
    expected = {
        ("2026-05-27", Decimal("-127.99")),
        ("2026-06-08", Decimal("-58.99")),
        ("2026-06-24", Decimal("-58.99")),
    }
    matches: list[dict[str, str]] = []
    with SOURCE_INDEX.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            key = (str(row.get("ISODate") or ""), cents(row.get("Amount")))
            if (
                key in expected
                and str(row.get("PropertyId") or "") == PROPERTY_ID
                and "LAWN" in (
                    str(row.get("Merchant") or "")
                    + str(row.get("Description") or "")
                ).upper()
                and str(row.get("BankAccountId") or "") == "38968"
            ):
                matches.append(
                    {
                        "date": key[0],
                        "amount": format(key[1], ".2f"),
                        "transaction_id": str(row.get("BaselaneId") or ""),
                    }
                )
    found = {(row["date"], Decimal(row["amount"])) for row in matches}
    return {
        "status": "verified" if found == expected and len(matches) == 3 else "mismatch",
        "row_count": len(matches),
        "gross_reimbursement": format(
            -sum((Decimal(row["amount"]) for row in matches), Decimal("0")), ".2f"
        ),
        "rows": sorted(matches, key=lambda row: row["date"]),
    }


def build_public() -> tuple[dict[str, Any], dict[str, Any]]:
    opening = balances()
    ledger = gl_balance()
    amount = Decimal(SPEC["amount"])
    dao_opening = opening.get(OPERATIONS_ACCOUNT, Decimal("0")) + opening.get(
        RESERVE_ACCOUNT, Decimal("0")
    )
    excess = (dao_opening - ledger).quantize(Decimal("0.01"))
    evidence = lawnstarter_evidence()
    issues: list[str] = []
    if set(opening) != {OPERATIONS_ACCOUNT, RESERVE_ACCOUNT, ECO_ACCOUNT}:
        issues.append("Sangamon operations/reserves or ECO 2624 is unavailable")
    if excess != amount:
        issues.append(
            f"live bank excess {excess:.2f} does not match guarded amount {amount:.2f}"
        )
    if opening.get(RESERVE_ACCOUNT, Decimal("0")) < amount:
        issues.append("Sangamon reserves cannot fund the exact excess")
    if evidence["status"] != "verified":
        issues.append("three LawnStarter component rows are not uniquely verified")
    if sum((Decimal(value) for _label, value in SPEC["components"]), Decimal("0")) != amount:
        issues.append("native components do not sum to the transfer")

    plan = build_transfer_plan(
        from_transfer_account_id=RESERVE_ACCOUNT,
        to_transfer_account_id=ECO_ACCOUNT,
        amount=SPEC["amount"],
        bookkeeping_note=SPEC["bookkeeping_note"],
        property_id=PROPERTY_ID,
        tag_id=24,
        same_day=True,
    )
    public = {
        "scope": "8143 S Sangamon exact bank-cash to property-GL rebalance",
        "issues": issues,
        "source_evidence": evidence,
        "cash_flow_invariant": {
            "internal_only": True,
            "tag_id": "24",
            "category": "Transfers Between Accounts",
            "property_id_on_both_mirrors_and_all_components": PROPERTY_ID,
            "reserve_add_on": "0.00",
            "authoritative_target": "full Sangamon property GL sum",
        },
        "authoritative_gl_balance": format(ledger, ".2f"),
        "opening_available_balances": {
            "Sangamon_operations": format(opening.get(OPERATIONS_ACCOUNT, 0), ".2f"),
            "Sangamon_reserves": format(opening.get(RESERVE_ACCOUNT, 0), ".2f"),
            "Sangamon_combined": format(dao_opening, ".2f"),
            "ECO_2624": format(opening.get(ECO_ACCOUNT, 0), ".2f"),
        },
        "forecast_available_balances": {
            "Sangamon_operations": format(opening.get(OPERATIONS_ACCOUNT, 0), ".2f"),
            "Sangamon_reserves": format(
                opening.get(RESERVE_ACCOUNT, Decimal("0")) - amount, ".2f"
            ),
            "Sangamon_combined": format(dao_opening - amount, ".2f"),
            "ECO_2624": format(
                opening.get(ECO_ACCOUNT, Decimal("0")) + amount, ".2f"
            ),
        },
        "transfer": {
            "direction": "Sangamon reserves -> ECO 2624",
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
    parser.add_argument("--digest")
    args = parser.parse_args()

    public, plan = build_public()
    plan_digest = digest(public)
    dry = {"status": "dry_run", "digest": plan_digest, **public}
    dry_path = write_report("sangamon_exact_gl_rebalance.dry.json", dry)
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
    ending_combined = ending[OPERATIONS_ACCOUNT] + ending[RESERVE_ACCOUNT]
    if ending_combined != Decimal(public["authoritative_gl_balance"]):
        raise RuntimeError(
            f"post-transfer Sangamon cash {ending_combined:.2f} "
            f"does not equal GL {public['authoritative_gl_balance']}"
        )
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
    path = write_report("sangamon_exact_gl_rebalance.applied.json", applied)
    print(json.dumps({**applied, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
