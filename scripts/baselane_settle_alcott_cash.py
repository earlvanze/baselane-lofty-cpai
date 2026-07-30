#!/usr/bin/env python3
"""Guarded Alcott -> ECO cash settlement and HVAC-transfer metadata repair."""

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
    query_recent_transfer_rows,
    update_parent_metadata,
)
from baselane_mcp.transfers import (
    build_transfer_plan,
    execute_transfer,
    list_active_transfer_accounts,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports"
SOURCE_ACCOUNT = 84290
SOURCE_BANK = "106362"
ECO_ACCOUNT = 29732
ECO_BANK = "38968"
PROPERTY_ID = "77356"
PROTECTED_MINIMUM = Decimal("2750.00")  # $2,250 deposits + $500 operating float

SPEC: dict[str, Any] = {
    "key": "Alcott_to_ECO_operating_advance",
    "from": SOURCE_ACCOUNT,
    "to": ECO_ACCOUNT,
    "amount": "1806.76",
    "property_id": PROPERTY_ID,
    "bookkeeping_note": "Alcott partial repayment of ECO operating advances",
    "note": (
        "326-332 S Alcott: partial FIFO repayment of ECO-funded 2026-01 "
        "turnover/remodeling costs. DAO retains $2,250 tenant security deposits "
        "plus $500 operating float. Internal only."
    ),
    "parent_label": "Alcott-ECO | 2026-01 remodeling advance | partial",
    "components": [
        ["Alcott-ECO | remodeling advance | 2026-01 | partial", "1806.76"],
    ],
}

HVAC_REPAIRS = [
    {
        "amount": Decimal("3000.00"),
        "label": "Alcott-ECO | HVAC replacement reimbursement | 2026-06-19",
        "note": (
            "Reimbursement of ECO 2624 payment to Alirio Aramis Le on "
            "2026-06-19 for Alcott AC replacement. Internal only."
        ),
    },
    {
        "amount": Decimal("1780.00"),
        "label": "Alcott-ECO | HVAC repairs reimbursement | 2026-06-22",
        "note": (
            "Reimbursement of ECO 2624 payment to Alirio Aramis Le on "
            "2026-06-22 for Alcott HVAC repairs. Internal only."
        ),
    },
]


def balances() -> dict[int, Decimal]:
    wanted = {SOURCE_ACCOUNT, ECO_ACCOUNT}
    return {
        int(row["transfer_account_id"]): Decimal(str(row["available_balance"]))
        for row in list_active_transfer_accounts(graphql)
        if int(row["transfer_account_id"]) in wanted
    }


def repair_hvac_metadata(*, apply: bool) -> list[dict[str, Any]]:
    rows = [
        row
        for bank_id in (SOURCE_BANK, ECO_BANK)
        for row in query_recent_transfer_rows(bank_id)
        if (
            not row.get("isDeleted")
            and not row.get("parentId")
            and str(row.get("date") or "") == "2026-07-10"
            and str(row.get("bankAccountId") or "") == bank_id
        )
    ]
    result: list[dict[str, Any]] = []
    for repair in HVAC_REPAIRS:
        matches = [
            row
            for row in rows
            if abs(Decimal(str(row.get("amount") or 0))) == repair["amount"]
        ]
        matched_banks = {str(row.get("bankAccountId") or "") for row in matches}
        if len(matches) != 2 or matched_banks != {SOURCE_BANK, ECO_BANK}:
            raise RuntimeError(
                f"expected both HVAC mirrors for {repair['amount']}; "
                f"found ids {[row.get('id') for row in matches]}"
            )
        ids = [str(row["id"]) for row in matches]
        if apply:
            update_parent_metadata(
                ids,
                label=repair["label"],
                property_id=PROPERTY_ID,
                note=repair["note"],
            )
        result.append(
            {
                "amount": format(repair["amount"], ".2f"),
                "parent_ids": ids,
                "label": repair["label"],
                "status": "updated" if apply else "validated",
                "tag_id": "24",
            }
        )
    return result


def build_public() -> tuple[dict[str, Any], dict[str, Any]]:
    opening = balances()
    amount = Decimal(SPEC["amount"])
    issues: list[str] = []
    if set(opening) != {SOURCE_ACCOUNT, ECO_ACCOUNT}:
        issues.append("Alcott operations or ECO 2624 is unavailable")
    ending_source = opening.get(SOURCE_ACCOUNT, Decimal("0")) - amount
    # This source account excludes the separate $2,250 security account.
    if ending_source < Decimal("500.00"):
        issues.append("settlement would leave Alcott operations below $500")
    if (
        ending_source + Decimal("2250.00")
        < PROTECTED_MINIMUM
    ):
        issues.append("settlement would breach the total protected minimum")

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
        "scope": "Alcott -> ECO partial operating-advance repayment",
        "issues": issues,
        "cash_flow_invariant": {
            "internal_only": True,
            "tag_id": "24",
            "category": "Transfers Between Accounts",
            "security_deposits_retained": "2250.00",
            "operating_float_retained": "500.00",
            "protected_total": "2750.00",
        },
        "opening_available_balances": {
            "Alcott_operations": format(
                opening.get(SOURCE_ACCOUNT, Decimal("0")), ".2f"
            ),
            "ECO": format(opening.get(ECO_ACCOUNT, Decimal("0")), ".2f"),
        },
        "forecast_available_balances": {
            "Alcott_operations": format(ending_source, ".2f"),
            "Alcott_total_including_security": format(
                ending_source + Decimal("2250.00"), ".2f"
            ),
            "ECO": format(
                opening.get(ECO_ACCOUNT, Decimal("0")) + amount, ".2f"
            ),
        },
        "transfer": {
            "direction": "Alcott -> ECO",
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

    if args.audit_only:
        plan = build_transfer_plan(
            from_transfer_account_id=SOURCE_ACCOUNT,
            to_transfer_account_id=ECO_ACCOUNT,
            amount=SPEC["amount"],
            bookkeeping_note=SPEC["bookkeeping_note"],
            property_id=PROPERTY_ID,
            tag_id=24,
            same_day=True,
        )
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        stored = (state.get("transfers") or {}).get(plan["confirmation_token"])
        if not stored or stored.get("status") != "completed":
            raise RuntimeError("completed Alcott transfer is absent from state")
        repaired = repair_hvac_metadata(apply=True)
        audit = ensure_native_audit(SPEC, plan)
        ending = balances()
        payload = {
            "status": "completed_transfer_audited",
            "transfer": SPEC,
            "receipt": stored.get("receipt"),
            "existing_hvac_reimbursements": repaired,
            "native_audit": audit,
            "ending_available_balances": {
                "Alcott_operations": format(ending[SOURCE_ACCOUNT], ".2f"),
                "Alcott_total_including_security": format(
                    ending[SOURCE_ACCOUNT] + Decimal("2250.00"), ".2f"
                ),
                "ECO": format(ending[ECO_ACCOUNT], ".2f"),
            },
        }
        path = write_report("alcott_cash_settlement_applied.json", payload)
        print(json.dumps({**payload, "report": str(path)}, indent=2))
        return 0

    public, plan = build_public()
    hvac = repair_hvac_metadata(apply=False)
    public["existing_hvac_reimbursements"] = hvac
    plan_digest = digest(public)
    dry = {"status": "dry_run", "digest": plan_digest, **public}
    dry_path = write_report("alcott_cash_settlement_dry_run.json", dry)
    if not args.apply:
        print(json.dumps({**dry, "report": str(dry_path)}, indent=2))
        return 0 if not public["issues"] else 2
    if args.digest != plan_digest:
        raise RuntimeError(f"live digest is {plan_digest}; exact --digest required")
    if public["issues"]:
        raise RuntimeError(f"refusing apply with issues: {public['issues']}")

    repaired = repair_hvac_metadata(apply=True)
    receipt = execute_transfer(
        plan=plan,
        confirmation_token=plan["confirmation_token"],
        graphql_runner=graphql,
        state_path=STATE_PATH,
    )
    audit = ensure_native_audit(SPEC, plan)
    ending = balances()
    if ending.get(SOURCE_ACCOUNT, Decimal("-1")) < Decimal("500.00"):
        raise RuntimeError("post-transfer Alcott operations is below $500")
    applied = {
        "status": "submitted_and_verified",
        "digest": plan_digest,
        **public,
        "existing_hvac_reimbursements": repaired,
        "ending_available_balances": {
            "Alcott_operations": format(ending[SOURCE_ACCOUNT], ".2f"),
            "Alcott_total_including_security": format(
                ending[SOURCE_ACCOUNT] + Decimal("2250.00"), ".2f"
            ),
            "ECO": format(ending[ECO_ACCOUNT], ".2f"),
        },
        "receipt": receipt,
        "native_audit": audit,
    }
    path = write_report("alcott_cash_settlement_applied.json", applied)
    print(json.dumps({**applied, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
