#!/usr/bin/env python3
"""Guarded partial Ohio-to-ECO reimbursement of July 29 property-tax bills."""

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

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "oh_tax_reimbursements.20260729.json"
ECO = 29732
SPECS = [
    {
        "key": "allendale_tax_reimbursement",
        "from": 105859,
        "to": ECO,
        "amount": "870.64",
        "property_id": "83239",
        "floor": "500.00",
        "parent_label": "Allendale-ECO | 2026 H1 tax reimbursement | partial",
        "bookkeeping_note": "Allendale partial reimbursement of 2026-07-29 ECO-paid H1 taxes",
        "note": "Internal reimbursement; preserves the $500 non-coownership operating floor.",
        "components": [
            ["Allendale-ECO | property tax | 2026 H1 | partial", "859.34"],
            ["Allendale-ECO | tax payment service fee", "11.30"],
        ],
    },
    {
        "key": "frederick_tax_reimbursement",
        "from": 115460,
        "to": ECO,
        "amount": "438.93",
        "property_id": "81782",
        "floor": "500.00",
        "parent_label": "Frederick-ECO | 2026 H1 tax reimbursement | partial",
        "bookkeeping_note": "Frederick partial reimbursement of 2026-07-29 ECO-paid H1 taxes",
        "note": "Internal reimbursement; preserves the $500 non-coownership operating floor.",
        "components": [
            ["Frederick-ECO | property tax | 2026 H1 | partial", "425.82"],
            ["Frederick-ECO | tax payment service fee", "13.11"],
        ],
    },
    {
        "key": "nash_tax_reimbursement",
        "from": 119576,
        "to": ECO,
        "amount": "505.98",
        "property_id": "96348",
        "floor": "742.70",
        "parent_label": "Nash-ECO | 2026 H1 tax reimbursement | partial",
        "bookkeeping_note": "Nash partial reimbursement of 2026-07-29 ECO-paid H1 taxes",
        "note": (
            "Internal reimbursement; preserves the $500 non-coownership operating "
            "floor plus the documented $242.70 next ULD obligation."
        ),
        "components": [
            ["Nash-ECO | property tax | 2026 H1 | partial", "495.28"],
            ["Nash-ECO | tax payment service fee", "10.70"],
        ],
    },
]


def balances() -> dict[int, Decimal]:
    wanted = {ECO, *(int(spec["from"]) for spec in SPECS)}
    return {
        int(row["transfer_account_id"]): Decimal(str(row["available_balance"]))
        for row in list_active_transfer_accounts(graphql)
        if int(row["transfer_account_id"]) in wanted
    }


def build() -> tuple[dict, list[dict]]:
    opening = balances()
    issues: list[str] = []
    plans: list[dict] = []
    ending = dict(opening)
    for spec in SPECS:
        source = int(spec["from"])
        amount = Decimal(spec["amount"])
        floor = Decimal(spec["floor"])
        if source not in opening or ECO not in opening:
            issues.append(f"{spec['key']}: transfer account unavailable")
            continue
        if opening[source] == floor:
            # Idempotency guard: the source account already reflects this exact
            # guarded reimbursement. Never create a second transfer merely
            # because a receiving-side Baselane mirror was delayed.
            continue
        if opening[source] - amount != floor:
            issues.append(
                f"{spec['key']}: live ending {opening[source] - amount:.2f} "
                f"does not equal protected amount {floor:.2f}"
            )
        plan = build_transfer_plan(
            from_transfer_account_id=source,
            to_transfer_account_id=ECO,
            amount=spec["amount"],
            bookkeeping_note=spec["bookkeeping_note"],
            property_id=spec["property_id"],
            tag_id=24,
            same_day=True,
        )
        plans.append(plan)
        ending[source] -= amount
        ending[ECO] += amount
    public = {
        "scope": "Ohio July 29 property-tax reimbursements funded above protected cash",
        "issues": issues,
        "opening_balances": {str(k): f"{v:.2f}" for k, v in opening.items()},
        "forecast_balances": {str(k): f"{v:.2f}" for k, v in ending.items()},
        "transfers": [
            {
                **spec,
                "live_status": (
                    "already_applied"
                    if opening.get(int(spec["from"])) == Decimal(spec["floor"])
                    else "planned"
                ),
            }
            for spec in SPECS
        ],
        "held": {
            "1456 W 85th": "No unrestricted cash above $500.",
            "1278 E 187th": "No operating cash above $500; security principal excluded.",
            "1518 Dille": "Exactly $500 operating float.",
            "428 Cross": "Preserved for $500 float plus next $165.71 ULD payment.",
            "1432 Sara": "Excluded from the July 29 tax-payment batch.",
        },
    }
    return public, plans


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()
    public, plans = build()
    digest = hashlib.sha256(
        json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if not args.apply:
        print(json.dumps({"status": "dry_run", "digest": digest, **public}, indent=2))
        return 0 if not public["issues"] else 2
    if args.digest != digest:
        raise RuntimeError(f"live digest is {digest}; exact --digest required")
    if public["issues"]:
        raise RuntimeError(f"refusing apply with issues: {public['issues']}")
    receipts = []
    audits = []
    planned_specs = [
        spec
        for spec in SPECS
        if public["opening_balances"].get(str(spec["from"])) != spec["floor"]
    ]
    for spec, plan in zip(planned_specs, plans, strict=True):
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
        if ending[int(spec["from"])] != Decimal(spec["floor"]):
            raise RuntimeError(f"ending balance mismatch: {spec['key']}")
    payload = {
        "status": "submitted_and_verified",
        "digest": digest,
        **public,
        "ending_balances": {str(k): f"{v:.2f}" for k, v in ending.items()},
        "receipts": receipts,
        "native_audits": audits,
    }
    REPORT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**payload, "report": str(REPORT)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
