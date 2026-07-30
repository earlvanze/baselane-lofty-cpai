#!/usr/bin/env python3
"""Deterministic 30/360 reconciliation of the signed 1432 Sara EARLDAO note.

This is a loan subledger calculation, not a Baselane cash mutation.  It uses
the signed note terms and the live-verified cash events.  Interest-designated
payments reduce accrued interest; principal-designated payments reduce
principal on their original dates even though ECO remitted the collected
principal to EARLDAO later as disclosed agent cash.

The 2025-09-20 $51.63 EARLDAO -> Sara transfer is included as an additional
principal advance because both live mirrors say "EARLDAO Loan".  It is also
reported as a documentation exception because the signed note states a
$13,000 loan and requires written amendments.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any


getcontext().prec = 28
ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports"
CENT = Decimal("0.01")
RATE = Decimal("0.12")
ORIGINAL_PRINCIPAL = Decimal("13000.00")
LOAN_DATE = date(2025, 6, 9)
MATURITY_DATE = date(2026, 6, 9)


@dataclass(frozen=True)
class Event:
    event_date: date
    kind: str
    amount: Decimal
    transaction_ids: tuple[str, ...]
    evidence: str


EVENTS = (
    Event(date(2025, 9, 10), "interest_payment", Decimal("390.00"),
          ("189577527", "189577528"), "Live paired Sara -> EARLDAO cash."),
    Event(date(2025, 9, 20), "principal_advance", Decimal("51.63"),
          ("192584831", "192584838"),
          'Live paired EARLDAO -> Sara cash; both mirrors note "EARLDAO Loan".'),
    Event(date(2025, 10, 4), "interest_payment", Decimal("130.00"),
          ("196963420", "196963424"), "Live paired Sara -> EARLDAO cash."),
    Event(date(2025, 10, 4), "principal_payment", Decimal("105.75"),
          ("196963147", "196963150"),
          "Sara paid ECO as EARLDAO collection agent; remitted 2026-07-11."),
    Event(date(2025, 11, 9), "interest_payment", Decimal("130.00"),
          ("208475577", "208475575"), "Live paired Sara -> EARLDAO cash."),
    Event(date(2025, 11, 9), "principal_payment", Decimal("894.25"),
          ("208475919", "208475921"),
          "Sara paid ECO as EARLDAO collection agent; remitted 2026-07-11."),
    Event(date(2025, 12, 2), "interest_payment", Decimal("120.00"),
          ("215945626", "215945800", "215945796"),
          "$130 receipt less paired $10 interest overpayment return."),
    Event(date(2025, 12, 8), "principal_payment", Decimal("1000.00"),
          ("218128680", "218128688"),
          "Sara paid ECO as EARLDAO collection agent; remitted 2026-07-11."),
    Event(date(2026, 1, 6), "interest_payment", Decimal("120.00"),
          ("227790566",), "Live Sara cash payment received by EARLDAO."),
    Event(date(2026, 1, 6), "principal_payment", Decimal("1000.00"),
          ("227790655", "227790659"),
          "Sara paid ECO as EARLDAO collection agent; remitted 2026-07-11."),
    Event(date(2026, 2, 24), "interest_payment", Decimal("100.00"),
          ("245803528", "245803520"), "Live paired Sara -> EARLDAO cash."),
)


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def day_count_30_360(start: date, end: date) -> int:
    """Contractual 30-day-month/360-year day count."""
    return (
        (end.year - start.year) * 360
        + (end.month - start.month) * 30
        + (min(end.day, 30) - min(start.day, 30))
    )


def calculate(as_of: date, include_supplemental_advance: bool) -> dict[str, Any]:
    if as_of < MATURITY_DATE:
        raise ValueError("This matured-note report requires as_of >= 2026-06-09")

    principal = ORIGINAL_PRINCIPAL
    accrued_interest = Decimal("0")
    last_date = LOAN_DATE
    ledger: list[dict[str, Any]] = []

    events = [
        event for event in EVENTS
        if include_supplemental_advance or event.kind != "principal_advance"
    ]
    # Interest cash is applied before same-day principal, matching the
    # transaction labels and the note's accrued-interest prepayment clause.
    order = {"interest_payment": 0, "principal_payment": 1, "principal_advance": 2}
    events.sort(key=lambda event: (event.event_date, order[event.kind]))

    for event in events:
        days = day_count_30_360(last_date, event.event_date)
        interest = principal * RATE * Decimal(days) / Decimal(360)
        accrued_interest += interest
        principal_before = principal
        accrued_before = accrued_interest

        if event.kind == "interest_payment":
            accrued_interest -= event.amount
        elif event.kind == "principal_payment":
            principal -= event.amount
        elif event.kind == "principal_advance":
            principal += event.amount
        else:
            raise ValueError(f"Unsupported event kind: {event.kind}")

        ledger.append({
            "date": event.event_date.isoformat(),
            "kind": event.kind,
            "amount": f"{event.amount:.2f}",
            "transaction_ids": list(event.transaction_ids),
            "days_since_prior_event_30_360": days,
            "principal_before": f"{money(principal_before):.2f}",
            "interest_accrued_since_prior_event": f"{money(interest):.2f}",
            "accrued_interest_before_action": f"{money(accrued_before):.2f}",
            "principal_after": f"{money(principal):.2f}",
            "accrued_interest_after": f"{money(accrued_interest):.2f}",
            "evidence": event.evidence,
        })
        last_date = event.event_date

    days_to_maturity = day_count_30_360(last_date, MATURITY_DATE)
    final_contract_interest = (
        principal * RATE * Decimal(days_to_maturity) / Decimal(360)
    )
    accrued_interest += final_contract_interest
    maturity_interest = accrued_interest
    matured_base = principal + maturity_interest

    default_days = day_count_30_360(MATURITY_DATE, as_of)
    default_interest = matured_base * RATE * Decimal(default_days) / Decimal(360)
    total_interest_due = maturity_interest + default_interest
    total_debt = principal + total_interest_due

    return {
        "as_of": as_of.isoformat(),
        "include_2025_09_20_supplemental_advance": include_supplemental_advance,
        "terms": {
            "signed_principal": "13000.00",
            "loan_date": LOAN_DATE.isoformat(),
            "maturity_date": MATURITY_DATE.isoformat(),
            "annual_rate": "12.00%",
            "day_count": "30-day month / 360-day year",
            "post_maturity_rate": "12.00% on matured unpaid principal and interest",
        },
        "verified_cash": {
            "interest_paid": "990.00",
            "principal_paid_via_eco_agent": "3000.00",
            "supplemental_advance": (
                "51.63" if include_supplemental_advance else "0.00"
            ),
        },
        "event_ledger": ledger,
        "maturity": {
            "days_from_last_event_30_360": days_to_maturity,
            "interest_from_last_event": f"{money(final_contract_interest):.2f}",
            "principal": f"{money(principal):.2f}",
            "unpaid_interest": f"{money(maturity_interest):.2f}",
            "matured_base": f"{money(matured_base):.2f}",
        },
        "as_of_balance": {
            "default_days_30_360": default_days,
            "principal": f"{money(principal):.2f}",
            "pre_maturity_interest_due": f"{money(maturity_interest):.2f}",
            "post_maturity_default_interest": f"{money(default_interest):.2f}",
            "total_interest_due": f"{money(total_interest_due):.2f}",
            "total_debt": f"{money(total_debt):.2f}",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", default="2026-07-29")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPORT_DIR / "sara_earldao_note_reconciliation.20260729.json",
    )
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of)

    recognized = calculate(as_of, include_supplemental_advance=True)
    exception_scenario = calculate(as_of, include_supplemental_advance=False)
    report = {
        "status": "reconciled_with_documentation_exception",
        "accounting_conclusion": (
            "Recognize the live $51.63 EARLDAO-to-Sara transfer as additional "
            "loan principal because both cash mirrors label it EARLDAO Loan. "
            "Obtain written ratification because the signed note states $13,000."
        ),
        "recognized_balance": recognized,
        "exception_scenario_if_51_63_is_proven_not_to_be_principal": (
            exception_scenario
        ),
        "difference_caused_by_51_63": {
            "principal": "51.63",
            "interest_through_as_of": f"{money(Decimal(recognized['as_of_balance']['total_interest_due']) - Decimal(exception_scenario['as_of_balance']['total_interest_due'])):.2f}",
            "total_debt": f"{money(Decimal(recognized['as_of_balance']['total_debt']) - Decimal(exception_scenario['as_of_balance']['total_debt'])):.2f}",
        },
        "controls": {
            "new_cash_movement": False,
            "baselane_mutation": False,
            "interest_payments_live_verified_through": "2026-07-29",
            "no_interest_cash_after": "2026-02-24",
            "eco_agent_principal_remitted_to_earldao": "3000.00",
        },
        "open_documentation_item": (
            "Ratify the 2025-09-20 $51.63 supplemental advance or document its "
            "non-loan purpose; do not silently omit paired lender cash."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
