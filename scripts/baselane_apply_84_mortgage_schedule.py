#!/usr/bin/env python3
"""Idempotently apply the full Wesley-funded 84 Madison mortgage schedule."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE", Path(__file__).absolute().parents[1]))
sys.path.insert(0, str(ROOT / "scripts"))

from baselane_apply_alcott_accruals_live import run_graphql  # noqa: E402


PROPERTY = "84 Madison Ave"
PROPERTY_ID = "60548"
START_BALANCE = Decimal("183617.00")
ANNUAL_RATE = Decimal("0.06875")
TERM_MONTHS = 360
MONTHLY_RATE = ANNUAL_RATE / Decimal(12)
PAYMENT = (START_BALANCE * MONTHLY_RATE / (Decimal(1) - (Decimal(1) + MONTHLY_RATE) ** Decimal(-TERM_MONTHS))).quantize(
    Decimal("0.01"), rounding=ROUND_HALF_UP
)
START_MONTH = "2025-10"
END_MONTH = "2026-10"
FLOOD_PREMIUM = Decimal("1843.00")
STATEMENT_MONTHLY_PAYMENT = Decimal("2225.42")
REPORT = ROOT / "reports" / "84_madison_mortgage_schedule_live_apply.json"
APPLY_ENV = "BASELANE_84_MORTGAGE_SCHEDULE_APPLY"
APPLY_DIGEST_ENV = "BASELANE_84_MORTGAGE_SCHEDULE_DIGEST"
MORTGAGE_PREFIX = "AOPS-84-MORTGAGE-PMT"
FLOOD_PREFIX = "AOPS-84-FLOOD-INSURANCE"
TAX_INSURANCE_PREFIX = "AOPS-84-TAXES-INSURANCE"
WESLEY_ADVANCE_PREFIX = "AOPS-84-WESLEY-MORTGAGE-ADVANCE"


def month_range(start: str, end: str) -> list[str]:
    year, month = map(int, start.split("-"))
    end_year, end_month = map(int, end.split("-"))
    result: list[str] = []
    while (year, month) <= (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return result


def month_end(month: str) -> str:
    year, number = map(int, month.split("-"))
    return date(year, number, calendar.monthrange(year, number)[1]).isoformat()


def schedule_targets() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    targets: list[dict[str, Any]] = []
    amortization: list[dict[str, str]] = []
    balance = START_BALANCE
    for payment_number, month in enumerate(month_range(START_MONTH, END_MONTH), 1):
        interest = (balance * MONTHLY_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        principal = (PAYMENT - interest).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        ending_balance = (balance - principal).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        amortization.append({
            "month": month,
            "payment_number": str(payment_number),
            "starting_balance": f"{balance:.2f}",
            "payment": f"{PAYMENT:.2f}",
            "principal": f"{principal:.2f}",
            "interest": f"{interest:.2f}",
            "ending_balance": f"{ending_balance:.2f}",
        })
        basis = (
            f"PMT schedule: starting balance $183,617.00 before October 2025 payment; "
            f"6.875% fixed; 360 months; payment {payment_number}; monthly P&I ${PAYMENT:.2f}; "
            f"ending balance ${ending_balance:.2f}. Accounting-only schedule entry; no Baselane bank transfer represented."
        )
        for component, amount, tag_id, label in (
            ("principal", principal, "20", "Mortgage Principal"),
            ("interest", interest, "11", "Mortgage Interest"),
        ):
            marker = f"{MORTGAGE_PREFIX}|{component}|{PROPERTY}|{month}"
            targets.append({
                "marker": marker,
                "component": component,
                "month": month,
                "date": month_end(month),
                "amount": f"{-amount:.2f}",
                "tag_id": tag_id,
                "merchant_name": f"84 Madison Ave {label} | {month}",
                "note": f"{marker} | {basis}",
            })
        balance = ending_balance

    flood_months = month_range("2025-11", "2026-10")
    regular = Decimal("153.58")
    for index, month in enumerate(flood_months):
        amount = regular if index < len(flood_months) - 1 else FLOOD_PREMIUM - regular * Decimal(len(flood_months) - 1)
        marker = f"{FLOOD_PREFIX}|{PROPERTY}|{month}"
        targets.append({
            "marker": marker,
            "component": "flood_insurance",
            "month": month,
            "date": month_end(month),
            "amount": f"{-amount:.2f}",
            "tag_id": "8",
            "merchant_name": f"84 Madison Ave Flood Insurance | {month}",
            "note": (
                f"{marker} | $1,843.00 annual flood premium recognized November 2025 through October 2026. "
                "Source: Rushmore statement dated 2025-12-02, transaction activity showing $1,843.00 flood-insurance "
                "disbursement on 2025-11-18. Accounting-only monthly accrual; no Baselane bank transfer represented."
            ),
        })

    expense_by_month: dict[str, Decimal] = defaultdict(Decimal)
    for target in targets:
        expense_by_month[target["month"]] += -Decimal(target["amount"])
    for month in month_range(START_MONTH, END_MONTH):
        amount = (STATEMENT_MONTHLY_PAYMENT - expense_by_month[month]).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        marker = f"{TAX_INSURANCE_PREFIX}|{PROPERTY}|{month}"
        targets.append({
            "marker": marker,
            "component": "taxes_insurance",
            "month": month,
            "date": month_end(month),
            "amount": f"{-amount:.2f}",
            "tag_id": "15",
            "merchant_name": f"84 Madison Ave Taxes & Insurance | {month}",
            "note": (
                f"{marker} | Statement-billed taxes and insurance paid by Wesley Babcock outside Baselane. "
                f"Amount is the balancing portion of the ${STATEMENT_MONTHLY_PAYMENT:.2f} regular monthly "
                f"statement payment after the user-directed ${PAYMENT:.2f} DAO P&I schedule and separately "
                "recognized flood-insurance accrual. Creditor: Wesley Babcock; not ECO Systems LLC. "
                "Accounting-only accrual; no Baselane bank transfer represented."
            ),
        })

    expense_by_month: dict[str, Decimal] = defaultdict(Decimal)
    for target in targets:
        expense_by_month[target["month"]] += -Decimal(target["amount"])
    for month in month_range(START_MONTH, END_MONTH):
        amount = expense_by_month[month].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        marker = f"{WESLEY_ADVANCE_PREFIX}|{PROPERTY}|{month}"
        targets.append({
            "marker": marker,
            "component": "wesley_mortgage_advance",
            "month": month,
            "date": month_end(month),
            "amount": f"{amount:.2f}",
            "tag_id": "25",
            "merchant_name": f"Wesley Babcock Mortgage Advance | 84 Madison Ave | {month}",
            "note": (
                f"{marker} | Accounting-only payable recognition matching the full ${STATEMENT_MONTHLY_PAYMENT:.2f} "
                "84 Madison regular monthly statement payment, including principal, interest, taxes, and insurance. "
                "Wesley Babcock paid the obligation outside Baselane. Creditor: Wesley Babcock; not ECO Systems LLC. "
                "No Baselane bank transfer represented."
            ),
        })
    return targets, amortization


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def query_transactions() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    query = """
    query Transactions($input: SortsAndFilters) {
      transactions(input: $input) {
        total
        data { id amount date merchantName propertyId tagId bankAccountId note isManual hidden isDeleted }
      }
    }
    """
    while True:
        result = run_graphql({
            "operationName": "Transactions",
            "variables": {"input": {
                "sort": {"direction": "DESC", "field": "date"},
                "filter": {"propertyId": PROPERTY_ID, "isHidden": False, "isDeleted": False},
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


def matches(target: dict[str, Any], row: dict[str, Any]) -> bool:
    return all((
        Decimal(str(row.get("amount") or 0)).quantize(Decimal("0.01")) == Decimal(target["amount"]),
        str(row.get("date") or "") == target["date"],
        str(row.get("propertyId") or "") == PROPERTY_ID,
        str(row.get("tagId") or "") == target["tag_id"],
        note_text(row.get("note")) == target["note"],
        row.get("bankAccountId") is None,
    ))


def reporting_month(value: str | None = None) -> str:
    if value:
        month = value
    else:
        cutoff = os.environ.get("BASELANE_REPORTING_CUTOFF_DATE", "").strip()
        month = cutoff[:7] if cutoff else date.today().strftime("%Y-%m")
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError as exc:
        raise ValueError(f"invalid reporting month {month!r}; expected YYYY-MM") from exc
    if not START_MONTH <= month <= END_MONTH:
        raise ValueError(
            f"reporting month {month!r} is outside the supported schedule "
            f"{START_MONTH} through {END_MONTH}"
        )
    return month


def delete_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deleted: list[dict[str, Any]] = []
    for offset in range(0, len(rows), 25):
        result = run_graphql({
            "operationName": "DeleteFuture84MortgageSchedule",
            "variables": {"input": [
                {"id": str(row["id"]), "isDeleted": True, "isReviewedByUser": True}
                for row in rows[offset:offset + 25]
            ]},
            "query": """
            mutation DeleteFuture84MortgageSchedule($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) { id isDeleted amount date propertyId tagId note }
            }
            """,
        })["data"]["updateTransactions"]
        deleted.extend(result)
    return deleted


def create_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    for offset in range(0, len(targets), 5):
        batch = targets[offset:offset + 5]
        fields = []
        for index, target in enumerate(batch):
            fields.append(
                f"r{index}: createTransaction(input: {{"
                f"merchantName: {json.dumps(target['merchant_name'])} "
                f"note: {json.dumps(target['note'])} "
                f"tagId: {json.dumps(target['tag_id'])} propertyId: {json.dumps(PROPERTY_ID)} "
                "unitId: null entityId: null bankAccountId: null "
                f"date: {json.dumps(target['date'])} amount: {target['amount']} isReviewedByUser: true"
                "}) { id amount date propertyId tagId bankAccountId note isManual }"
            )
        data = run_graphql({
            "operationName": "Create84MortgageSchedule",
            "variables": {},
            "query": "mutation Create84MortgageSchedule {\n" + "\n".join(fields) + "\n}",
        })["data"]
        created.extend(data[f"r{index}"] for index in range(len(batch)))
    return created


def update_targets(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    inputs = [{
        "id": str(row["id"]),
        "amount": float(Decimal(target["amount"])),
        "merchantName": target["merchant_name"],
        "note": target["note"],
        "tagId": target["tag_id"],
        "propertyId": PROPERTY_ID,
        "unitId": None,
    } for target, row in items]
    updated: list[dict[str, Any]] = []
    for offset in range(0, len(inputs), 25):
        result = run_graphql({
            "operationName": "Update84MortgageSchedule",
            "variables": {"input": inputs[offset:offset + 25]},
            "query": """
            mutation Update84MortgageSchedule($input: [UpdateTransaction!]) {
              updateTransactions(input: $input) { id amount date propertyId tagId bankAccountId note isManual }
            }
            """,
        })["data"]["updateTransactions"]
        updated.extend(result)
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--through-month")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()

    through_month = reporting_month(args.through_month)
    all_targets, amortization = schedule_targets()
    targets = [target for target in all_targets if target["month"] <= through_month]
    future_targets = {target["marker"]: target for target in all_targets if target["month"] > through_month}
    live = query_transactions()
    by_marker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in live:
        marker = note_text(row.get("note")).split(" | ", 1)[0]
        if marker.startswith((MORTGAGE_PREFIX, FLOOD_PREFIX, TAX_INSURANCE_PREFIX, WESLEY_ADVANCE_PREFIX)):
            by_marker[marker].append(row)

    duplicates = {marker: rows for marker, rows in by_marker.items() if len(rows) > 1}
    if duplicates:
        raise RuntimeError(f"duplicate schedule markers: {sorted(duplicates)}")
    future_live: list[dict[str, Any]] = []
    future_identity_failures: list[str] = []
    for marker, target in future_targets.items():
        rows = by_marker.get(marker) or []
        if not rows:
            continue
        row = rows[0]
        if matches(target, row):
            future_live.append(row)
        else:
            future_identity_failures.append(marker)
    create = [target for target in targets if not by_marker.get(target["marker"])]
    update = [
        (target, by_marker[target["marker"]][0])
        for target in targets
        if by_marker.get(target["marker"]) and not matches(target, by_marker[target["marker"]][0])
    ]
    skip = [
        target for target in targets
        if by_marker.get(target["marker"]) and matches(target, by_marker[target["marker"]][0])
    ]

    created: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    mutation_plan = {
        "through_month": through_month,
        "create": create,
        "update": [
            {"target": target, "live_id": str(row.get("id") or "")}
            for target, row in update
        ],
        "delete": [
            {
                "id": str(row.get("id") or ""),
                "amount": str(row.get("amount") or ""),
                "date": str(row.get("date") or ""),
                "property_id": str(row.get("propertyId") or ""),
                "tag_id": str(row.get("tagId") or ""),
                "marker": note_text(row.get("note")).split(" | ", 1)[0],
            }
            for row in future_live
        ],
    }
    payload_digest = hashlib.sha256(
        json.dumps(mutation_plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    apply_authorized = (
        os.environ.get(APPLY_ENV) == "1"
        and os.environ.get(APPLY_DIGEST_ENV) == payload_digest
    )
    if args.apply and future_identity_failures:
        raise RuntimeError(
            "future live schedule rows failed exact identity verification: "
            f"{future_identity_failures}"
        )
    if args.apply and not apply_authorized:
        raise RuntimeError(
            f"--apply requires {APPLY_ENV}=1 and {APPLY_DIGEST_ENV}={payload_digest}"
        )
    if args.apply:
        created = create_targets(create)
        updated = update_targets(update)
        deleted = delete_targets(future_live)
        verified = query_transactions()
        verified_by_marker = {note_text(row.get("note")).split(" | ", 1)[0]: row for row in verified}
        failures = [target["marker"] for target in targets if not matches(target, verified_by_marker.get(target["marker"], {}))]
        future_remaining = sorted(set(future_targets).intersection(verified_by_marker))
        if future_remaining:
            failures.extend(f"future_row_not_deleted:{marker}" for marker in future_remaining)
    else:
        failures = []

    report = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "mode": "apply" if args.apply else "dry_run",
        "through_month": through_month,
        "property": PROPERTY,
        "property_id": PROPERTY_ID,
        "starting_balance": f"{START_BALANCE:.2f}",
        "annual_interest_rate": f"{ANNUAL_RATE:.5f}",
        "term_months": TERM_MONTHS,
        "monthly_pi_payment": f"{PAYMENT:.2f}",
        "statement_monthly_payment": f"{STATEMENT_MONTHLY_PAYMENT:.2f}",
        "annual_flood_insurance": f"{FLOOD_PREMIUM:.2f}",
        "planned_counts": {
            "create": len(create),
            "update": len(update),
            "delete_future": len(future_live),
            "skip": len(skip),
        },
        "created_count": len(created),
        "updated_count": len(updated),
        "deleted_future_count": len(deleted),
        "future_identity_failures": future_identity_failures,
        "payload_digest": payload_digest,
        "apply_authorized": apply_authorized,
        "approval_command": (
            f"{APPLY_ENV}=1 {APPLY_DIGEST_ENV}={payload_digest} "
            f"python3 scripts/{Path(__file__).name} --through-month {through_month} --apply"
        ),
        "verification_failures": failures,
        "amortization": amortization,
        "targets": targets,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "mode", "starting_balance", "annual_interest_rate", "term_months", "monthly_pi_payment",
        "statement_monthly_payment", "annual_flood_insurance", "planned_counts", "created_count", "updated_count",
        "deleted_future_count", "future_identity_failures", "payload_digest", "verification_failures",
    )}, indent=2))
    if failures:
        raise RuntimeError(f"schedule verification failed: {failures}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
