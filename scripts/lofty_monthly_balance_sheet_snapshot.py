#!/usr/bin/env python3
"""Render the canonical investor-review balance-sheet cash snapshot."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


OBLIGATION_LABELS = {
    "dao": "DAO fees",
    "general_escrow": "General escrow",
    "insurance": "Insurance",
    "insurance_escrow": "Insurance escrow",
    "interest": "Interest",
    "legal": "Legal fees",
    "mortgage_interest": "Mortgage interest",
    "pm": "PM fee",
    "pm_dao": "PM and DAO fees",
    "principal": "Principal",
    "retained_capital": "Retained capital",
    "tax_escrow": "Tax escrow",
    "taxes": "Taxes",
}
OBLIGATION_ORDER = (
    "pm",
    "legal",
    "dao",
    "pm_dao",
    "taxes",
    "tax_escrow",
    "insurance",
    "insurance_escrow",
    "mortgage_interest",
    "interest",
    "principal",
    "general_escrow",
    "retained_capital",
)


def parse_money(value: Any) -> float | None:
    try:
        return float(Decimal(str(value).replace("$", "").replace(",", "")).quantize(Decimal("0.01")))
    except (InvalidOperation, TypeError, ValueError):
        return None


def format_money(value: Any) -> str:
    amount = parse_money(value)
    if amount is None:
        return "Pending reconciliation"
    prefix = "-$" if amount < 0 else "$"
    return f"{prefix}{abs(amount):,.2f}"


def has_verified_intercompany_position(summary: dict[str, Any]) -> bool:
    status = str(summary.get("intercompany_payable_status") or "")
    source_mode = str(summary.get("intercompany_source_mode") or "")
    payable = parse_money(summary.get("dao_accounts_payable_to_eco"))
    receivable = parse_money(summary.get("eco_accounts_receivable_from_dao"))
    if payable is None or receivable is None:
        return False
    if payable < 0 or receivable < 0 or payable != receivable:
        return False
    if status == "ok":
        return True
    if status == "verified_payable_from_id_bearing_cash_rollforward":
        return source_mode == "id_bearing_eco_account_intercompany_rollforward"
    if status == "ok_no_open_position":
        return payable == 0 and receivable == 0
    if status == "positive_activity_requires_custody_reconciliation":
        return (
            source_mode == "id_bearing_eco_account_intercompany_rollforward"
            and payable == 0
            and receivable == 0
        )
    return (
        status == "reconciliation_pending"
        and source_mode == "id_bearing_eco_account_activity_trace"
        and payable == 0
        and receivable == 0
    )


def reconciled_obligation_breakdown(
    summary: dict[str, Any],
) -> list[tuple[str, float]] | None:
    total = parse_money(summary.get("open_accrued_obligations"))
    if summary.get("open_accrued_obligations_status") != "ok" or total is None or total < 0:
        return None
    raw = summary.get("open_accrued_obligations_by_kind")
    if not isinstance(raw, dict):
        return [] if total == 0 else None

    parsed: dict[str, float] = {}
    for raw_kind, raw_amount in raw.items():
        kind = str(raw_kind or "").strip()
        amount = parse_money(raw_amount)
        if not kind or amount is None or amount < 0:
            return None
        if amount:
            parsed[kind] = amount
    if abs(round(sum(parsed.values()), 2) - total) > 0.01:
        return None

    order = {kind: index for index, kind in enumerate(OBLIGATION_ORDER)}
    return sorted(
        parsed.items(),
        key=lambda item: (order.get(item[0], len(order)), item[0]),
    )


def reconciled_counterparty_balances(
    summary: dict[str, Any],
    key: str,
) -> list[dict[str, Any]] | None:
    raw = summary.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        counterparty = str(item.get("counterparty") or "").strip()
        category = str(item.get("category") or "").strip()
        amount = parse_money(item.get("amount"))
        if not counterparty or not category or amount is None or amount < 0:
            return None
        if amount:
            result.append({**item, "amount": amount})
    return result


def lofty_reserve_description(value: Any) -> str:
    reserve = parse_money(value)
    if reserve is None:
        return "pending reconciliation"
    if reserve < 0:
        return "a reserve deficit, not cash owed to ECO"
    if reserve > 0:
        return "cash held separately by Lofty, not by ECO"
    return "no separate Lofty reserve balance"


def position_date_label(summary: dict[str, Any], run_month: str | None = None) -> str:
    month = str(run_month or summary.get("as_of_month") or "").strip()
    # The close cutoff is the investor-facing date authority. Source dates are
    # validated against it before publication; choosing an arbitrary same-month
    # source date here previously allowed a July 14 snapshot to leak into the
    # July 31 close.
    try:
        cutoff = date.fromisoformat(str(summary.get("reporting_cutoff_date") or ""))
    except ValueError:
        cutoff = None
    if cutoff is not None and (not month or cutoff.strftime("%Y-%m") == month):
        return f"{cutoff:%B} {cutoff.day}"
    for key in ("physical_bank_cash_as_of_date", "eco_operating_cash_as_of_date"):
        try:
            parsed = date.fromisoformat(str(summary.get(key) or ""))
        except ValueError:
            continue
        if not month or parsed.strftime("%Y-%m") == month:
            return f"{parsed:%B} {parsed.day}"
    try:
        year, month_number = (int(part) for part in month.split("-", 1))
        last_day = calendar.monthrange(year, month_number)[1]
        parsed = date(year, month_number, last_day)
        return f"{parsed:%B} {parsed.day}"
    except (TypeError, ValueError):
        return "current"


def _property_label(summary: dict[str, Any]) -> str:
    label = str(summary.get("property_name") or "DAO").split(",", 1)[0].strip()
    return label.removesuffix(" Public") or "DAO"


def render_balance_sheet_lines(summary: dict[str, Any]) -> list[str]:
    physical_cash = parse_money(summary.get("physical_bank_cash"))
    total_spendable = parse_money(summary.get("total_dao_spendable_cash"))
    eco_spendable = parse_money(summary.get("eco_held_unrestricted_cash"))
    obligations = parse_money(summary.get("open_accrued_obligations"))
    lines = []
    if summary.get("physical_bank_cash_status") == "ok" and physical_cash is not None:
        lines.append(f"- Baselane bank cash: {format_money(physical_cash)}")
    elif summary.get("physical_bank_cash_status") != "property_missing":
        lines.append("- Baselane bank cash: Pending reconciliation")
    reserve_override = summary.get("lofty_curr_maintenance_reserve_reporting_override")
    reserve_override_pending = bool(
        isinstance(reserve_override, dict)
        and reserve_override.get("live_correction_status")
        not in {None, "", "verified_live"}
    )
    reserve_label = (
        "Lofty operating-reserve reporting value (live correction pending)"
        if reserve_override_pending
        else "Lofty operating-reserve ledger"
    )
    reserve_description = (
        "approved close value; authenticated live readback is retained separately for correction"
        if reserve_override_pending
        else lofty_reserve_description(summary.get("lofty_curr_maintenance_reserve"))
    )
    lines.extend(
        [
            f"- ECO-held {_property_label(summary)} cash: "
            f"{format_money(eco_spendable) if summary.get('eco_held_unrestricted_cash_status') == 'ok' else 'Pending reconciliation'}",
            "- Recorded unpaid obligations: "
            f"{format_money(obligations) if summary.get('open_accrued_obligations_status') == 'ok' else 'Pending reconciliation'}",
        ]
    )

    payables = reconciled_counterparty_balances(
        summary, "dao_accounts_payable_by_counterparty"
    )
    receivables = reconciled_counterparty_balances(
        summary, "dao_accounts_receivable_by_counterparty"
    )
    accrued_payables = (
        [
            item
            for item in payables
            if item.get("cash_effect") == "included_in_recorded_unpaid_obligations"
        ]
        if payables is not None
        else None
    )
    if accrued_payables is not None:
        for item in accrued_payables:
            kind = str(item.get("category") or "")
            label = OBLIGATION_LABELS.get(kind, kind.replace("_", " ").title())
            lines.append(
                f"  - Due to {item['counterparty']} — {label}: "
                f"{format_money(item['amount'])}"
            )
    else:
        breakdown = reconciled_obligation_breakdown(summary)
        if breakdown is None and obligations not in (None, 0):
            lines.append("  - Counterparty/category detail: Pending reconciliation")
        elif breakdown:
            for kind, amount in breakdown:
                label = OBLIGATION_LABELS.get(kind, kind.replace("_", " ").title())
                lines.append(f"  - Counterparty pending — {label}: {format_money(amount)}")

    lines.extend(
        [
            "- Spendable Baselane/ECO cash after recorded obligations (before Lofty OR): "
            f"{format_money(total_spendable) if summary.get('total_dao_spendable_cash_status') == 'ok' else 'Pending reconciliation'}",
            "- ECO Net DAO Funds (spendable cash held by ECO): "
            f"{format_money(eco_spendable) if summary.get('eco_held_unrestricted_cash_status') == 'ok' else 'Pending reconciliation'}",
            "",
            "Separate from spendable cash:",
            f"- {reserve_label}: "
            f"{format_money(summary.get('lofty_curr_maintenance_reserve'))} — "
            f"{reserve_description}.",
        ]
    )

    lofty_reserve = parse_money(summary.get("lofty_curr_maintenance_reserve"))
    if lofty_reserve is not None and lofty_reserve < 0:
        lines.append(f"  - DAO A/P — Due to Lofty operating reserve: {format_money(-lofty_reserve)}")

    if payables is not None:
        other_payables = [
            item
            for item in payables
            if item.get("cash_effect") != "included_in_recorded_unpaid_obligations"
        ]
        for item in other_payables:
            category = str(item.get("category") or "balance").replace("_", " ")
            lines.append(
                f"- DAO A/P — Due to {item['counterparty']} ({category}): "
                f"{format_money(item['amount'])}"
            )
    if receivables is not None:
        for item in receivables:
            category = str(item.get("category") or "balance").replace("_", " ")
            lines.append(
                f"- DAO A/R — Due from {item['counterparty']} ({category}): "
                f"{format_money(item['amount'])}"
            )

    if payables is None and "dao_accounts_payable_to_eco" in summary:
        if has_verified_intercompany_position(summary):
            payable_amount = parse_money(summary.get("dao_accounts_payable_to_eco"))
            if payable_amount:
                lines.append(f"- DAO A/P — Due to ECO: {format_money(payable_amount)}")
        else:
            lines.append("- DAO A/P — Due to ECO: Pending reconciliation")

    if summary.get("mortgage_escrow_reconciliation_required"):
        if summary.get("restricted_mortgage_escrow_status") == "ok":
            lines.append(
                "- Mortgage-servicer escrow for taxes and insurance (restricted): "
                f"{format_money(summary.get('restricted_mortgage_escrow'))}"
            )
    for item in (summary.get("property_cash_summary_adjustments") or {}).get("cash_position_lines") or []:
        if not isinstance(item, dict):
            continue
        source = f" ({item.get('source')})" if item.get("source") else ""
        lines.append(f"- {item.get('metric')}: {format_money(item.get('amount'))}{source}")
    return lines


def render_balance_sheet_snapshot(
    summary: dict[str, Any],
    run_month: str | None = None,
) -> str:
    lines = [
        f"Actual {position_date_label(summary, run_month)} position:",
        "",
        *render_balance_sheet_lines(summary),
        "",
        "Baselane/ECO spendable cash and the Lofty operating-reserve ledger are shown separately; "
        "neither should be read as an approved distribution.",
        "If anything looks wrong, please DM @earlvanze on Discord or email "
        "ecosystemspm@gmail.com.",
    ]
    return "\n".join(lines)
