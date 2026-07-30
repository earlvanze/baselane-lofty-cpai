#!/usr/bin/env python3
"""Settle monthly reserve retention from each DAO checking account to savings."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))

from baselane_mcp.transfers import build_transfer_plan, execute_transfer, list_active_transfer_accounts  # noqa: E402
from baselane_settle_madison_pm_mortgage import (  # noqa: E402
    cents,
    graphql,
    note_text,
    query_parent,
    query_recent_transfer_rows,
    update_parent_metadata,
)


TAG_ID = "24"
STATE = ROOT / "reports/baselane_monthly_reserve_retention_transfer_state.json"
DEFAULT_ACCRUAL_REPORT = ROOT / "reports/baselane_monthly_accruals_202607.json"
DEFAULT_REPORT = ROOT / "reports/baselane_monthly_reserve_retention_transfer_plan.json"
DEFAULT_APPLIED_REPORT = ROOT / "reports/baselane_monthly_reserve_retention_transfer_applied.json"
CENT = Decimal("0.01")

ACCOUNT_PAIRS = {
    "84 Madison Ave": {
        "property_id": "60548",
        "operations": "84 Madison Ave Operations",
        "reserves": "84 Madison Ave Reserves",
    },
    "86 Madison Ave": {
        "property_id": "63162",
        "operations": "86 Madison Ave Operations",
        "reserves": "86 Madison Ave Reserves",
    },
    "88 Madison Ave": {
        "property_id": "89681",
        "operations": "88 Madison Ave Operations",
        "reserves": "88 Madison Ave Reserves",
    },
    "90 Madison Ave": {
        "property_id": "31525",
        "operations": "90 Madison Ave Operations",
        "reserves": "90 Madison Ave Reserves",
    },
    "85-104 Alawa Pl": {
        "property_id": "102389",
        "operations": "85-104 Alawa Pl Operations",
        "reserves": "85-104 Alawa Pl Reserves",
    },
    "326-332 S Alcott St, Denver, CO 80219": {
        "property_id": "77356",
        "operations": "326-332 S Alcott Operations",
        "reserves": "326-332 S Alcott Reserves",
    },
    "22164 Umland Cir, Jenner, CA 95450": {
        "property_id": "83184",
        "operations": "22164 Umland Circle Operations",
        "reserves": "22164 Umland Circle Reserves",
    },
    "917 Pawnee Ave, Memphis, TN 38109": {
        "property_id": "117274",
        "operations": "917 Pawnee Ave Operations",
        "reserves": "917 Pawnee Ave Reserves",
    },
}


def money(value: Any) -> Decimal:
    return Decimal(str(value or "0")).quantize(CENT)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def account_label(account: dict[str, Any]) -> str:
    return " ".join(
        str(account.get(key) or "").strip()
        for key in ("account_name", "nickname")
    ).strip()


def exact_account(accounts: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    matches = [account for account in accounts if label.casefold() in account_label(account).casefold()]
    return matches[0] if len(matches) == 1 else None


def completed_tokens() -> set[str]:
    if not STATE.is_file():
        return set()
    payload = json.loads(STATE.read_text(encoding="utf-8"))
    return {
        str(token)
        for token, record in (payload.get("transfers") or {}).items()
        if isinstance(record, dict) and record.get("status") == "completed"
    }


def retention_rows(report: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    month_reports = report.get("month_reports") or []
    if len(month_reports) != 1:
        raise RuntimeError("monthly accrual report must contain exactly one month")
    month_report = month_reports[0]
    month = str(month_report.get("target_month") or report.get("target_month") or "")
    rows = [
        row
        for row in month_report.get("or_replenishment") or []
        if isinstance(row, dict) and money(row.get("amount")) > 0
    ]
    return month, rows


def build_specs(
    report: dict[str, Any],
    accounts: list[dict[str, Any]],
    *,
    already_completed: set[str],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    month, requirements = retention_rows(report)
    specs: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    for requirement in requirements:
        property_name = str(requirement.get("property") or "")
        config = ACCOUNT_PAIRS.get(property_name)
        if not config:
            holds.append({"property": property_name, "reason": "account_pair_not_configured"})
            continue
        source = exact_account(accounts, config["operations"])
        destination = exact_account(accounts, config["reserves"])
        if source is None or destination is None:
            holds.append(
                {
                    "property": property_name,
                    "reason": "exact_live_account_pair_unavailable",
                    "operations_match": source is not None,
                    "reserves_match": destination is not None,
                }
            )
            continue
        amount = money(requirement["amount"])
        source_balance = money(source.get("available_balance"))
        if source_balance < amount:
            holds.append(
                {
                    "property": property_name,
                    "reason": "insufficient_checking_available_balance",
                    "required_amount": format(amount, ".2f"),
                    "available_balance": format(source_balance, ".2f"),
                }
            )
            continue
        note = f"AOPS-RESERVE-CASH|{property_name}|{month}|{amount:.2f}"
        label = f"OR Replenishment | {property_name} | {month}"
        plan = build_transfer_plan(
            from_transfer_account_id=source["transfer_account_id"],
            to_transfer_account_id=destination["transfer_account_id"],
            amount=amount,
            bookkeeping_note=note,
            property_id=config["property_id"],
            tag_id=TAG_ID,
            same_day=True,
        )
        if plan["confirmation_token"] in already_completed:
            continue
        specs.append(
            {
                "property": property_name,
                "property_id": config["property_id"],
                "month": month,
                "amount": format(amount, ".2f"),
                "label": label,
                "note": note,
                "source": source,
                "destination": destination,
                "plan": plan,
            }
        )
    return month, specs, holds


def public_plan(month: str, specs: list[dict[str, Any]], holds: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "status": "ready" if specs and not holds else ("partial" if specs else "held"),
        "month": month,
        "policy": {
            "direction": "same DAO checking -> same DAO savings",
            "category": "Transfers Between Accounts",
            "cash_only": True,
            "exact_account_pair_required": True,
            "missing_or_insufficient_account_nonblocking": True,
        },
        "transfer_count": len(specs),
        "transfer_total": format(sum((money(spec["amount"]) for spec in specs), Decimal("0")), ".2f"),
        "hold_count": len(holds),
        "holds": holds,
        "transfers": [
            {
                "property": spec["property"],
                "property_id": spec["property_id"],
                "amount": spec["amount"],
                "source_transfer_account_id": spec["source"]["transfer_account_id"],
                "source_account": account_label(spec["source"]),
                "source_available_balance": spec["source"]["available_balance"],
                "destination_transfer_account_id": spec["destination"]["transfer_account_id"],
                "destination_account": account_label(spec["destination"]),
                "confirmation_token": spec["plan"]["confirmation_token"],
            }
            for spec in specs
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {"digest": hashlib.sha256(canonical.encode()).hexdigest(), **payload}


def audit_mirrors(spec: dict[str, Any]) -> dict[str, Any]:
    expected_bank_ids = {
        str(spec["source"]["bank_account_id"]),
        str(spec["destination"]["bank_account_id"]),
    }
    exact: list[dict[str, Any]] = []
    for attempt in range(4):
        rows = [
            row
            for bank_id in sorted(expected_bank_ids)
            for row in query_recent_transfer_rows(bank_id)
        ]
        exact = [
            row
            for row in rows
            if (
                not row.get("isDeleted")
                and not row.get("parentId")
                and str(row.get("bankAccountId") or "") in expected_bank_ids
                and str(row.get("date") or "") == spec["plan"]["transfer_date"]
                and abs(cents(row.get("amount") or 0)) == money(spec["amount"])
                and note_text(row.get("note")) in {spec["note"], spec["label"]}
            )
        ]
        if len(exact) == 2 and {str(row.get("bankAccountId")) for row in exact} == expected_bank_ids:
            break
        if attempt < 3:
            time.sleep(2)
    if len(exact) != 2:
        return {"status": "pending_bank_mirrors", "matched_parent_ids": [str(row.get("id")) for row in exact]}
    parent_ids = [str(row["id"]) for row in exact]
    update_parent_metadata(
        parent_ids,
        label=spec["label"],
        property_id=spec["property_id"],
        note=spec["note"],
    )
    verified = [query_parent(parent_id) for parent_id in parent_ids]
    if any(
        str(row.get("tagId")) != TAG_ID
        or str(row.get("propertyId")) != spec["property_id"]
        or str(row.get("merchantName")) != spec["label"]
        or note_text(row.get("note")) != spec["note"]
        for row in verified
    ):
        raise RuntimeError(f"{spec['property']} reserve transfer mirror verification failed")
    return {"status": "verified_mirrored_parents", "parent_ids": parent_ids}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accrual-report", type=Path, default=DEFAULT_ACCRUAL_REPORT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--applied-report", type=Path, default=DEFAULT_APPLIED_REPORT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()

    report = json.loads(args.accrual_report.read_text(encoding="utf-8"))
    if report.get("status") != "ok":
        raise RuntimeError("monthly accrual report is not status=ok")
    accounts = list_active_transfer_accounts(graphql)
    month, specs, holds = build_specs(report, accounts, already_completed=completed_tokens())
    plan = public_plan(month, specs, holds)
    write_json(args.report, plan)
    if not args.apply:
        print(json.dumps({"report": str(args.report), **plan}, indent=2))
        return 0
    if args.digest != plan["digest"]:
        raise RuntimeError(f"live digest is {plan['digest']}; exact --digest required")

    receipts = []
    audits = []
    for spec in specs:
        receipt = execute_transfer(
            plan=spec["plan"],
            confirmation_token=spec["plan"]["confirmation_token"],
            graphql_runner=graphql,
            state_path=STATE,
        )
        receipts.append({"property": spec["property"], **receipt})
        audits.append({"property": spec["property"], **audit_mirrors(spec)})
    applied = {
        **plan,
        "status": "submitted_and_verified" if all(row["status"] == "verified_mirrored_parents" for row in audits) else "submitted_pending_mirrors",
        "receipts": receipts,
        "audits": audits,
    }
    write_json(args.applied_report, applied)
    print(json.dumps({"report": str(args.applied_report), **applied}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
