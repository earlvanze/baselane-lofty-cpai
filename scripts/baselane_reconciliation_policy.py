from __future__ import annotations

from typing import Any, Mapping


NON_CASH_CLOSE_PREFIXES = (
    "ECO-DAO-MONTH-END-RESET",
    "ECO-DAO-2026-CAPITAL",
    "ECO-DAO-9CC-CAPITAL",
    "AOPS-PNL-ACCRUAL|retained_capital",
)

NON_CASH_ACCRUAL_PREFIXES = (
    "AOPS-PNL-ACCRUAL",
)


def row_note(row: Mapping[str, Any]) -> str:
    return str(row.get("Notes") or row.get("notes") or row.get("note") or "").strip()


def is_non_cash_close_row(row: Mapping[str, Any]) -> bool:
    """Return True for accounting close rows that never evidence cash movement."""
    return row_note(row).startswith(NON_CASH_CLOSE_PREFIXES)


def is_unsettled_accrual_row(row: Mapping[str, Any]) -> bool:
    """Return True for accrual and settlement journals that are not cash movements."""
    return row_note(row).startswith(NON_CASH_ACCRUAL_PREFIXES)


def is_cash_basis_excluded_row(row: Mapping[str, Any]) -> bool:
    """Return True when a ledger row must not enter ECO Operating Cash."""
    return is_non_cash_close_row(row) or is_unsettled_accrual_row(row)
