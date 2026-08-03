#!/usr/bin/env python3
"""Close 9 Country Club monthly ECO GL balances and track Noah's interest payable."""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import hashlib
import json
import os
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql
from baselane_reconcile_9_country_club import mortgage_reconciliation_issues


ROOT = Path(os.environ.get("WORKSPACE_ROOT") or Path.cwd()).resolve()
PROPERTY = "9 Country Club Ln N"
PROPERTY_ID = "91341"
OWNER_TAG_ID = "25"
TRANSFER_TAG_ID = "24"
MORTGAGE_TAG_ID = "11"
START_MONTH = "2025-07"
CLOSED_END_MONTH = "2026-06"
NOAH_ADVANCE_END_MONTH = "2026-03"
NOAH_ADVANCE_START_MONTH = "2025-09"
NOAH_PREFIX = "AOPS-9CC-NOAH-MORTGAGE-ADVANCE"
VOID_PREFIX = "AOPS-9CC-VOID-PRE-RESPONSIBILITY-INTEREST"
OBSOLETE_INTEREST_MONTHS = {"2025-07", "2025-08"}
RESET_PREFIX = "ECO-DAO-9CC-CAPITAL"
MONTHLY_MORTGAGE = Decimal("2875.00")
REPORT = ROOT / "reports" / "baselane_9_country_club_capital_close.json"


def cents(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def digest_material(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(cents(value), ".2f")
    if isinstance(value, dict):
        return {key: digest_material(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [digest_material(item) for item in value]
    return value


def plan_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            digest_material(payload),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def month_range(start: str, end: str) -> list[str]:
    year, month = (int(part) for part in start.split("-"))
    end_year, end_month = (int(part) for part in end.split("-"))
    result = []
    while (year, month) <= (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return result


def month_end(month: str) -> str:
    year, number = (int(part) for part in month.split("-"))
    return dt.date(year, number, calendar.monthrange(year, number)[1]).isoformat()


def query_transactions(include_deleted: bool = False) -> list[dict[str, Any]]:
    query = """
    query Transactions($input: SortsAndFilters) {
      transactions(input: $input) {
        total
        data {
          id amount date merchantName description propertyId tagId bankAccountId note
          isManual hidden isDeleted isSplit parentId
        }
      }
    }
    """
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = run_graphql({
            "operationName": "Transactions",
            "variables": {"input": {
                "sort": {"direction": "ASC", "field": "date"},
                "filter": {
                    "propertyId": PROPERTY_ID,
                    "isHidden": False,
                    **({} if include_deleted else {"isDeleted": False}),
                },
                "page": page,
                "pageLimit": 1000,
            }},
            "query": query,
        })["data"]["transactions"]
        batch = result.get("data") or []
        rows.extend(batch)
        if not batch or len(rows) >= int(result.get("total") or 0):
            return rows
        page += 1


def create_transaction(target: dict[str, Any]) -> dict[str, Any]:
    return run_graphql({
        "operationName": "createTransaction",
        "variables": {
            "merchantName": target["merchant_name"],
            "note": target["note"],
            "tagId": OWNER_TAG_ID,
            "propertyId": PROPERTY_ID,
            "unitId": None,
            "entityId": None,
            "date": target["date"],
            "bankAccountId": None,
            "amount": float(target["amount"]),
            "isReviewedByUser": True,
        },
        "query": """
        mutation createTransaction(
          $merchantName: String!, $note: String!, $tagId: ID, $propertyId: ID,
          $unitId: ID, $entityId: Int, $date: String!, $bankAccountId: ID,
          $amount: Float!, $isReviewedByUser: Boolean
        ) {
          createTransaction(input: {
            merchantName: $merchantName note: $note tagId: $tagId propertyId: $propertyId
            unitId: $unitId entityId: $entityId date: $date bankAccountId: $bankAccountId
            amount: $amount isReviewedByUser: $isReviewedByUser
          }) { id amount date propertyId tagId bankAccountId note isManual }
        }
        """,
    })["data"]["createTransaction"]


def update_transaction(row: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    result = run_graphql({
        "operationName": "Update9CCCapitalClose",
        "variables": {"input": [{
            "id": str(row["id"]),
            "amount": float(target["amount"]),
            "merchantName": target["merchant_name"],
            "note": target["note"],
            "tagId": OWNER_TAG_ID,
            "propertyId": PROPERTY_ID,
            "unitId": None,
            "isReviewedByUser": True,
        }]},
        "query": """
        mutation Update9CCCapitalClose($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id amount date propertyId tagId bankAccountId note isManual
          }
        }
        """,
    })["data"]["updateTransactions"]
    return result[0]


def noah_interest_targets() -> list[dict[str, Any]]:
    targets = []
    for month in month_range(NOAH_ADVANCE_START_MONTH, NOAH_ADVANCE_END_MONTH):
        marker = f"{NOAH_PREFIX}|{PROPERTY}|{month}"
        targets.append({
            "kind": "noah_mortgage_interest_paid_outside_dao",
            "month": month,
            "marker": marker,
            "date": f"{month}-28",
            "amount": MONTHLY_MORTGAGE,
            "merchant_name": f"Mortgage Interest Paid by Noah Simon | {PROPERTY} | {month}",
            "note": (
                f"{marker} | Accounting-only payable recognition matching the ${MONTHLY_MORTGAGE:.2f} "
                "DAO UBS mortgage-interest obligation paid by Noah Simon outside the DAO bank account. "
                "Creditor: Noah Simon; ordinary reimbursement payable, not an EARLDAO loan or equity "
                "conversion and no governance vote required. No Baselane bank transfer represented."
            ),
        })
    return targets


def marker_row(target: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((row for row in rows if target["marker"] in note_text(row.get("note"))), None)


def shadow_row(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"shadow:{target['marker']}",
        "amount": target["amount"],
        "date": target["date"],
        "merchantName": target["merchant_name"],
        "propertyId": PROPERTY_ID,
        "tagId": OWNER_TAG_ID,
        "bankAccountId": None,
        "note": target["note"],
        "isManual": True,
        "hidden": False,
        "isDeleted": False,
    }


def is_reset(row: dict[str, Any]) -> bool:
    return note_text(row.get("note")).startswith(RESET_PREFIX + "|")


def is_noah_special(row: dict[str, Any]) -> bool:
    note = note_text(row.get("note"))
    if note.startswith(NOAH_PREFIX + "|"):
        return True
    if not note.startswith("AOPS-PNL-ACCRUAL|mortgage_interest|"):
        return False
    month = str(row.get("date") or "")[:7]
    return NOAH_ADVANCE_START_MONTH <= month <= NOAH_ADVANCE_END_MONTH


def is_obsolete_interest(row: dict[str, Any]) -> bool:
    month = str(row.get("date") or "")[:7]
    if month not in OBSOLETE_INTEREST_MONTHS:
        return False
    note = note_text(row.get("note"))
    return note.startswith(NOAH_PREFIX + "|") or note.startswith(
        "AOPS-PNL-ACCRUAL|mortgage_interest|9 Country Club Ln N|"
    )


def void_obsolete_interest(row: dict[str, Any]) -> dict[str, Any]:
    original_note = note_text(row.get("note"))
    month = str(row.get("date") or "")[:7]
    result = run_graphql({
        "operationName": "Void9CCPreResponsibilityInterest",
        "variables": {"input": [{
            "id": str(row["id"]),
            "amount": 0.0,
            "merchantName": f"VOID - {row.get('merchantName') or '9CC mortgage interest'}",
            "note": (
                f"{VOID_PREFIX}|{PROPERTY}|{month}|{row['id']} | Voided because July and August 2025 "
                "mortgage interest was not a DAO obligation. No bank transfer represented. "
                f"Original note: {original_note}"
            ),
            "tagId": str(row.get("tagId") or OWNER_TAG_ID),
            "propertyId": PROPERTY_ID,
            "unitId": None,
            "isReviewedByUser": True,
        }]},
        "query": """
        mutation Void9CCPreResponsibilityInterest($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id amount date propertyId tagId bankAccountId note isManual
          }
        }
        """,
    })["data"]["updateTransactions"]
    return result[0]


def calculate_close_targets(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    monthly: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for row in rows:
        month = str(row.get("date") or "")[:7]
        if not (START_MONTH <= month <= CLOSED_END_MONTH) or is_reset(row):
            continue
        value = cents(row.get("amount"))
        monthly[month]["all_in"] += value
        if is_noah_special(row):
            monthly[month]["noah_activity"] += value
        elif str(row.get("tagId") or "") == TRANSFER_TAG_ID:
            monthly[month]["transfers"] += value
        else:
            monthly[month]["economic"] += value

    retained = Decimal("0")
    principal = Decimal("0")
    targets: list[dict[str, Any]] = []
    rollforward: list[dict[str, str]] = []
    for month in month_range(START_MONTH, CLOSED_END_MONTH):
        values = monthly[month]
        economic = cents(values["economic"])
        components: dict[str, Decimal] = {}
        if economic >= 0:
            principal_payment = min(economic, principal)
            principal -= principal_payment
            if principal_payment:
                components["eco_principal"] = -principal_payment
            retained_addition = economic - principal_payment
            retained += retained_addition
            if retained_addition:
                components["retained_earnings"] = -retained_addition
        else:
            need = -economic
            retained_used = min(need, retained)
            retained -= retained_used
            if retained_used:
                components["retained_earnings"] = retained_used
            principal_advance = need - retained_used
            principal += principal_advance
            if principal_advance:
                components["eco_principal"] = principal_advance
        if values["transfers"]:
            components["transfer_clearing"] = -values["transfers"]

        expected_reset = -cents(values["all_in"])
        component_total = cents(sum(components.values(), Decimal("0")))
        residual = cents(expected_reset - component_total)
        if residual:
            components["reconciliation_residual"] = residual

        for component, value in components.items():
            value = cents(value)
            if not value:
                continue
            marker = f"{RESET_PREFIX}|{PROPERTY}|{month}|{component}"
            label = component.replace("_", " ").title()
            targets.append({
                "kind": component,
                "month": month,
                "marker": marker,
                "date": month_end(month),
                "amount": value,
                "merchant_name": f"ECO Systems LLC | {label} | {PROPERTY} | {month}",
                "note": (
                    f"{marker} | Amount: {value:.2f}. Accounting-only monthly capital reconciliation. "
                    f"Economic cash: {economic:.2f}. Transfers and other: {values['transfers']:.2f}. "
                    f"Noah mortgage activity: {values['noah_activity']:.2f}. Ending retained earnings: "
                    f"{retained:.2f}. Ending ECO principal: {principal:.2f}. No bank transfer represented."
                ),
            })
        rollforward.append({
            "month": month,
            "all_in_before_close": f"{cents(values['all_in']):.2f}",
            "economic_cash": f"{economic:.2f}",
            "transfers": f"{cents(values['transfers']):.2f}",
            "noah_mortgage_activity": f"{cents(values['noah_activity']):.2f}",
            "required_close": f"{expected_reset:.2f}",
            "ending_retained_earnings": f"{retained:.2f}",
            "ending_eco_principal": f"{principal:.2f}",
        })
    return targets, rollforward


def noah_position(rows: list[dict[str, Any]]) -> dict[str, Any]:
    advances = []
    reimbursements = []
    for row in rows:
        note = note_text(row.get("note"))
        if note.startswith(NOAH_PREFIX + "|"):
            advances.append((dt.date.fromisoformat(str(row["date"])), cents(row["amount"]), str(row["id"])))
        if is_noah_reimbursement(row):
            reimbursements.append((dt.date.fromisoformat(str(row["date"])), -cents(row["amount"]), str(row["id"])))

    events = [(date, value, "interest_paid_by_noah", row_id) for date, value, row_id in advances]
    events += [(date, -value, "reimbursement", row_id) for date, value, row_id in reimbursements]
    events.sort(key=lambda item: (item[0], 0 if item[2] == "reimbursement" else 1, item[3]))
    balance = Decimal("0")
    event_report = []
    for date, movement, kind, row_id in events:
        balance += movement
        event_report.append({
            "date": date.isoformat(), "kind": kind, "transaction_id": row_id,
            "movement": f"{movement:.2f}",
            "principal_balance": f"{balance:.2f}",
        })
    balance = cents(balance)
    return {
        "interest_paid_by_noah_total": f"{sum((value for _, value, _ in advances), Decimal('0')):.2f}",
        "reimbursement_total": f"{sum((value for _, value, _ in reimbursements), Decimal('0')):.2f}",
        "interest_payable": f"{balance:.2f}",
        "monthly_interest_payment": f"{MONTHLY_MORTGAGE:.2f}",
        "months_outstanding": int(balance / MONTHLY_MORTGAGE),
        "creditor": "Noah Simon",
        "classification": "ordinary_mortgage_interest_payable",
        "cash_transfer_required": balance > 0,
        "governance_required": False,
        "events": event_report,
    }


def is_noah_reimbursement(row: dict[str, Any]) -> bool:
    """Identify settled Noah reimbursements without consuming future advances."""
    merchant = str(row.get("merchantName") or "").lower()
    posted = str(row.get("date") or "")
    return bool(
        row.get("bankAccountId") is not None
        and str(row.get("tagId") or "") == MORTGAGE_TAG_ID
        and cents(row.get("amount")) < 0
        and "stone manor hospitality" in merchant
        and posted <= month_end(CLOSED_END_MONTH)
    )


def target_plan(
    targets: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    plan = []
    for target in targets:
        existing = marker_row(target, rows)
        requires_update = bool(
            existing
            and any((
                cents(existing.get("amount")) != cents(target["amount"]),
                str(existing.get("date") or "") != target["date"],
                str(existing.get("tagId") or "") != OWNER_TAG_ID,
                str(existing.get("propertyId") or "") != PROPERTY_ID,
                existing.get("bankAccountId") is not None,
                note_text(existing.get("note")) != target["note"],
            ))
        )
        plan.append({
            "action": (
                "create" if existing is None else ("update" if requires_update else "unchanged")
            ),
            "target": target,
            "current": (
                {
                    key: existing.get(key)
                    for key in (
                        "id", "amount", "date", "merchantName", "propertyId",
                        "tagId", "bankAccountId", "note",
                    )
                }
                if existing is not None
                else None
            ),
        })
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    args = parser.parse_args()

    rows = query_transactions()
    deleted_pre_july = query_transactions(include_deleted=True)
    deleted_ids = sorted(
        str(row["id"]) for row in deleted_pre_july
        if row.get("isDeleted") and str(row.get("date") or "") < "2025-07-01"
        and note_text(row.get("note")).startswith("AOPS-PNL-ACCRUAL|")
    )

    advance_targets = noah_interest_targets()
    obsolete_interest_rows = [row for row in rows if is_obsolete_interest(row)]
    effective_rows = [
        row for row in rows
        if not note_text(row.get("note")).startswith(NOAH_PREFIX + "|")
        and not is_obsolete_interest(row)
    ]
    effective_rows.extend(shadow_row(target) for target in advance_targets)
    close_targets, rollforward = calculate_close_targets(effective_rows)
    targets = advance_targets + close_targets
    reconciliation_issues = mortgage_reconciliation_issues(rows)
    mutation_plan = target_plan(targets, rows)
    digest_payload = {
        "property_id": PROPERTY_ID,
        "reconciliation_issues": reconciliation_issues,
        "obsolete_interest_rows": [
            {
                key: row.get(key)
                for key in (
                    "id", "amount", "date", "merchantName", "propertyId",
                    "tagId", "bankAccountId", "note",
                )
            }
            for row in obsolete_interest_rows
        ],
        "mutation_plan": mutation_plan,
    }
    digest = plan_digest(digest_payload)

    if args.apply and args.digest != digest:
        raise SystemExit(f"apply requires exact live dry-run digest: {digest}")
    if args.apply and reconciliation_issues:
        raise SystemExit(
            "9 Country Club capital close is blocked by mortgage reconciliation "
            f"issues: {[issue['code'] for issue in reconciliation_issues]}"
        )

    created = []
    updated = []
    voided = []
    if args.apply:
        for row in obsolete_interest_rows:
            voided.append(void_obsolete_interest(row))
        for target in targets:
            existing = marker_row(target, rows)
            if existing is None:
                created.append(create_transaction(target))
            elif any((
                cents(existing.get("amount")) != cents(target["amount"]),
                str(existing.get("date") or "") != target["date"],
                str(existing.get("tagId") or "") != OWNER_TAG_ID,
                str(existing.get("propertyId") or "") != PROPERTY_ID,
                existing.get("bankAccountId") is not None,
                note_text(existing.get("note")) != target["note"],
            )):
                updated.append(update_transaction(existing, target))

    if args.apply:
        verified_rows = query_transactions()
    else:
        verified_rows = [
            row for row in rows
            if not is_reset(row)
            and not note_text(row.get("note")).startswith(NOAH_PREFIX + "|")
            and not is_obsolete_interest(row)
        ]
        verified_rows.extend(shadow_row(target) for target in targets)
    monthly_totals = {}
    for month in month_range(START_MONTH, CLOSED_END_MONTH):
        monthly_totals[month] = f"{cents(sum(
            (cents(row.get('amount')) for row in verified_rows if str(row.get('date') or '')[:7] == month),
            Decimal('0'),
        )):.2f}"
    monthly_failures = {month: total for month, total in monthly_totals.items() if cents(total) != 0}
    position = noah_position(verified_rows)
    report = {
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "mode": (
            "blocked_reconciliation"
            if reconciliation_issues
            else ("apply" if args.apply else "dry_run")
        ),
        "digest": digest,
        "mutation_allowed": not reconciliation_issues,
        "reconciliation_issue_count": len(reconciliation_issues),
        "reconciliation_issues": reconciliation_issues,
        "mutation_plan": digest_material(mutation_plan),
        "property": PROPERTY,
        "property_id": PROPERTY_ID,
        "launch_date": "2025-08-15",
        "accrual_start_month": START_MONTH,
        "closed_end_month": CLOSED_END_MONTH,
        "deleted_pre_july_accrual_ids": deleted_ids,
        "target_count": len(targets),
        "created_count": len(created),
        "updated_count": len(updated),
        "voided_obsolete_interest_count": len(voided),
        "voided_obsolete_interest_ids": [str(row.get("id")) for row in voided],
        "monthly_totals_after_close": monthly_totals,
        "monthly_verification_failures": monthly_failures,
        "rollforward": rollforward,
        "noah_position": position,
    }
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "mode": report["mode"], "targets": len(targets), "created": len(created),
        "updated": len(updated), "voided": len(voided), "monthly_failures": monthly_failures,
        "ending_retained_earnings": rollforward[-1]["ending_retained_earnings"],
        "ending_eco_principal": rollforward[-1]["ending_eco_principal"],
        "noah_position": position,
    }, indent=2))
    if monthly_failures and not reconciliation_issues:
        raise RuntimeError(f"9CC monthly close verification failed: {monthly_failures}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
