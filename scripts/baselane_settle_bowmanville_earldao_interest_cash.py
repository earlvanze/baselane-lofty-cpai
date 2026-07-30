#!/usr/bin/env python3
"""Pay Bowmanville's two oldest unpaid interest months to EARLDAO in cash."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path("/home/digit/.openclaw/workspace")
sys.path.insert(0, str(ROOT / "skills" / "baselane-mcp" / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from baselane_mcp.transfers import (  # noqa: E402
    TransferValidationError,
    build_transfer_plan,
    execute_transfer,
    list_active_transfer_accounts,
)
from baselane_settle_madison_pm_mortgage import (  # noqa: E402
    cents,
    graphql,
    note_text,
    query_parent,
    query_recent_transfer_rows,
    update_parent_metadata,
)


EARLDAO_TRANSFER_ID = 105899
BOWMANVILLE_TRANSFER_ID = 102917
EARLDAO_BANK_ID = "131545"
BOWMANVILLE_BANK_ID = "127859"
BOWMANVILLE_PROPERTY_ID = "83241"
TAG_ID = "24"
TRANSFER_DATE = "2026-07-29"
STATE = ROOT / "reports" / "bowmanville_earldao_interest_cash_state.json"
REPORT = (
    ROOT / "reports" / "bowmanville_earldao_interest_cash_settlement.20260729.json"
)

MONTHS = [
    {
        "month": "2025-09",
        "amount": "412.00",
        "label": "EARLDAO-Bowmanville | interest settlement | 2025-09",
        "note": "BOWMANVILLE-EARLDAO cash settlement 2025-09 interest internal only",
    },
    {
        "month": "2025-12",
        "amount": "412.00",
        "label": "EARLDAO-Bowmanville | interest settlement | 2025-12",
        "note": "BOWMANVILLE-EARLDAO cash settlement 2025-12 interest internal only",
    },
]


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def write_report(payload: dict[str, Any]) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def accounts() -> dict[int, dict[str, Any]]:
    return {
        int(row["transfer_account_id"]): row
        for row in list_active_transfer_accounts(graphql)
        if int(row["transfer_account_id"])
        in {EARLDAO_TRANSFER_ID, BOWMANVILLE_TRANSFER_ID}
    }


def specs() -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], list[str]]:
    live = accounts()
    issues: list[str] = []
    for transfer_id, name, bank_id in (
        (EARLDAO_TRANSFER_ID, "EARLDAO Operations", EARLDAO_BANK_ID),
        (BOWMANVILLE_TRANSFER_ID, "Bowmanville Operations", BOWMANVILLE_BANK_ID),
    ):
        row = live.get(transfer_id)
        if not row:
            issues.append(f"{name} is not an eligible internal Baselane account")
        elif str(row.get("bank_account_id")) != bank_id:
            issues.append(f"{name} bank account identity changed")

    result: list[dict[str, Any]] = []
    for item in MONTHS:
        plan = build_transfer_plan(
            from_transfer_account_id=BOWMANVILLE_TRANSFER_ID,
            to_transfer_account_id=EARLDAO_TRANSFER_ID,
            amount=item["amount"],
            bookkeeping_note=item["note"],
            property_id=BOWMANVILLE_PROPERTY_ID,
            tag_id=TAG_ID,
            transfer_date=TRANSFER_DATE,
            same_day=True,
        )
        result.append({**item, "plan": plan})

    opening = money(live.get(BOWMANVILLE_TRANSFER_ID, {}).get("available_balance"))
    total = sum((money(row["amount"]) for row in result), Decimal("0"))
    if opening < total:
        issues.append(
            f"Bowmanville Operations has {opening:.2f}; {total:.2f} is required"
        )
    return result, live, issues


def public_plan(
    rows: list[dict[str, Any]],
    live: dict[int, dict[str, Any]],
    issues: list[str],
) -> dict[str, Any]:
    opening = money(live.get(BOWMANVILLE_TRANSFER_ID, {}).get("available_balance"))
    destination_opening = money(
        live.get(EARLDAO_TRANSFER_ID, {}).get("available_balance")
    )
    total = sum((money(row["amount"]) for row in rows), Decimal("0"))
    return {
        "scope": "Bowmanville lender interest cash settlement",
        "transfer_date": TRANSFER_DATE,
        "status": "dry_run",
        "policy": {
            "internal_only": True,
            "one_transfer_per_interest_month": True,
            "all_cash_mirrors_tag_id": TAG_ID,
            "no_external_recipient": True,
            "no_partial_month_payment": True,
        },
        "issues": issues,
        "source": {
            "transfer_account_id": BOWMANVILLE_TRANSFER_ID,
            "bank_account_id": BOWMANVILLE_BANK_ID,
            "opening_available_balance": f"{opening:.2f}",
            "projected_ending_available_balance": f"{opening - total:.2f}",
        },
        "destination": {
            "transfer_account_id": EARLDAO_TRANSFER_ID,
            "bank_account_id": EARLDAO_BANK_ID,
            "opening_available_balance": f"{destination_opening:.2f}",
            "projected_ending_available_balance": f"{destination_opening + total:.2f}",
        },
        "total": f"{total:.2f}",
        "transfers": [
            {
                "month": row["month"],
                "amount": row["amount"],
                "label": row["label"],
                "bookkeeping_note": row["note"],
                "confirmation_token": row["plan"]["confirmation_token"],
            }
            for row in rows
        ],
        "remaining_unpaid_interest": {
            "month": "2026-02",
            "amount": "412.00",
            "treatment": "paired accrual only; no cash transfer",
        },
    }


def exact_mirrors(spec: dict[str, Any]) -> list[dict[str, Any]]:
    expected_banks = {EARLDAO_BANK_ID, BOWMANVILLE_BANK_ID}
    rows = [
        row
        for bank_id in expected_banks
        for row in query_recent_transfer_rows(bank_id)
    ]
    return [
        row
        for row in rows
        if (
            not row.get("isDeleted")
            and not row.get("parentId")
            and str(row.get("bankAccountId") or "") in expected_banks
            and str(row.get("date") or "") == TRANSFER_DATE
            and abs(cents(row.get("amount") or 0)) == money(spec["amount"])
            and note_text(row.get("note")) in {spec["note"], spec["label"]}
        )
    ]


def audit(spec: dict[str, Any]) -> dict[str, Any]:
    matched: list[dict[str, Any]] = []
    for attempt in range(6):
        matched = exact_mirrors(spec)
        if (
            len(matched) == 2
            and {str(row.get("bankAccountId")) for row in matched}
            == {EARLDAO_BANK_ID, BOWMANVILLE_BANK_ID}
        ):
            break
        if attempt < 5:
            time.sleep(2)
    if len(matched) != 2:
        return {
            "month": spec["month"],
            "status": "pending_bank_mirrors",
            "matched_parent_ids": [str(row["id"]) for row in matched],
        }

    ids = [str(row["id"]) for row in matched]
    update_parent_metadata(
        ids,
        label=spec["label"],
        property_id=BOWMANVILLE_PROPERTY_ID,
        note=spec["note"],
    )
    verified = [query_parent(transaction_id) for transaction_id in ids]
    issues = [
        str(row["id"])
        for row in verified
        if (
            str(row.get("tagId")) != TAG_ID
            or str(row.get("propertyId")) != BOWMANVILLE_PROPERTY_ID
            or str(row.get("merchantName")) != spec["label"]
            or note_text(row.get("note")) != spec["note"]
            or abs(cents(row.get("amount") or 0)) != money(spec["amount"])
        )
    ]
    if issues:
        raise RuntimeError(f"transfer mirror metadata mismatch: {issues}")
    return {
        "month": spec["month"],
        "status": "verified_mirrored_parents",
        "parent_ids": ids,
        "tag_id": TAG_ID,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    rows, live, issues = specs()
    plan = public_plan(rows, live, issues)
    plan_digest = digest(plan)
    dry = {"digest": plan_digest, **plan}

    if args.verify:
        audits = [audit(row) for row in rows]
        ending = accounts()
        result = {
            "status": (
                "verified"
                if all(row["status"] == "verified_mirrored_parents" for row in audits)
                else "verification_pending"
            ),
            "audits": audits,
            "ending_available_balances": {
                "EARLDAO Operations": f"{money(ending[EARLDAO_TRANSFER_ID]['available_balance']):.2f}",
                "Bowmanville Operations": f"{money(ending[BOWMANVILLE_TRANSFER_ID]['available_balance']):.2f}",
            },
            "remaining_unpaid_interest": plan["remaining_unpaid_interest"],
        }
        write_report(result)
        print(json.dumps({**result, "report": str(REPORT)}, indent=2))
        return 0 if result["status"] == "verified" else 2

    write_report(dry)
    if not args.apply:
        print(json.dumps({**dry, "report": str(REPORT)}, indent=2))
        return 0 if not issues else 2
    if args.digest != plan_digest:
        raise RuntimeError(f"live digest is {plan_digest}; exact --digest required")
    if issues:
        raise RuntimeError(f"refusing apply with issues: {issues}")

    receipts: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    for row in rows:
        try:
            outcome = execute_transfer(
                plan=row["plan"],
                confirmation_token=row["plan"]["confirmation_token"],
                graphql_runner=graphql,
                state_path=STATE,
            )
        except TransferValidationError as exc:
            message = str(exc)
            normalized = message.casefold()
            challenge = (
                "otp"
                if (
                    "otp for user" in normalized
                    or "bank otp has not been completed" in normalized
                )
                else None
            )
            failure = {
                "status": (
                    "authentication_challenge" if challenge else "rejected"
                ),
                "challenge_type": challenge,
                "failed_month": row["month"],
                "cash_movement_created": bool(receipts),
                "completed_receipts": receipts,
                "next_action": (
                    "Run the documented Fold 7 MFA handoff, then repeat the exact apply."
                    if challenge
                    else "Resolve the Baselane validation error before retrying."
                ),
            }
            write_report(failure)
            print(json.dumps({**failure, "report": str(REPORT)}, indent=2))
            return 3
        receipts.append(
            {
                "month": row["month"],
                "status": outcome["status"],
                "idempotent": outcome["idempotent"],
                "receipt": outcome.get("receipt"),
            }
        )
        audits.append(audit(row))

    ending = accounts()
    result = {
        "status": "submitted_and_verified",
        "digest": plan_digest,
        "total": plan["total"],
        "receipts": receipts,
        "audits": audits,
        "ending_available_balances": {
            "EARLDAO Operations": f"{money(ending[EARLDAO_TRANSFER_ID]['available_balance']):.2f}",
            "Bowmanville Operations": f"{money(ending[BOWMANVILLE_TRANSFER_ID]['available_balance']):.2f}",
        },
        "remaining_unpaid_interest": plan["remaining_unpaid_interest"],
    }
    if not all(
        row["status"] == "verified_mirrored_parents" for row in audits
    ):
        result["status"] = "submitted_bank_mirrors_pending"
    write_report(result)
    print(json.dumps({**result, "report": str(REPORT)}, indent=2))
    return 0 if result["status"] == "submitted_and_verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
