#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from baselane_apply_alcott_accruals_live import run_graphql


ROOT = Path(os.environ.get("WORKSPACE_ROOT") or Path.cwd()).resolve()
PROPERTY_ID = "91341"
PROPERTY = "9 Country Club Ln N"
OPERATIONS_ACCOUNT_ID = "133098"

TAG_MORTGAGE_INTEREST = "11"
TAG_MORTGAGE_FEES = "34"
TAG_PROPERTY_MANAGEMENT = "80"
TAG_INSURANCE = "65"
TAG_TAXES = "95"
TAG_PROPERTY_TAXES = "15"
TAG_TRANSFERS = "24"

PM_PAYMENT_ID = "240295549"
JUNE_INSURANCE_PAYMENT_ID = "302557345"
JUNE_INSURANCE_ACCRUAL_ID = "313980406"
JULY_MORTGAGE_PARENT_ID = "306737335"
JULY_MORTGAGE_FEE_CHILD_ID = "314558401"
TAX_PAYMENT_ID = "247839471"
NBT_VERIFICATION_DEBIT_IDS = ("311540949", "311541005")

MORTGAGE_START_MONTH = "2025-09"
MORTGAGE_END_MONTH = "2026-07"
MORTGAGE_INTEREST = Decimal("2875.00")

# The April and July UBS statements show YTD interest of $11,500 and $20,125,
# respectively.  The $8,625 delta is exactly one $2,875 payment for each of May,
# June, and July.  This is an evidence constraint, not a transaction-ID guess.
LENDER_STATEMENT_RECOGNIZED_INTEREST_BY_MONTH = {
    "2026-05": MORTGAGE_INTEREST,
    "2026-06": MORTGAGE_INTEREST,
    "2026-07": MORTGAGE_INTEREST,
}
LENDER_STATEMENT_EVIDENCE = {
    "april_statement_ytd_interest": "11500.00",
    "july_statement_ytd_interest": "20125.00",
    "may_through_july_delta": "8625.00",
}
OWNER_MORTGAGE_COUNTERPARTY = "stone manor hospitality"
LENDER_MORTGAGE_COUNTERPARTY = "ubs bank usa"

SETTLEMENT_MARKER_RE = re.compile(
    r"^AOPS-9CC-RECON\|(?P<kind>mortgage_settlement|tax_settlement)\|"
    r"9 Country Club Ln N\|(?P<month>20\d{2}-\d{2})\|"
    r"(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)"
)


def cents(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def note_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def transaction_id_sort_key(value: object) -> tuple[int, int | str]:
    text = str(value or "")
    return (0, int(text)) if text.isdigit() else (1, text)


def existing_settlement_amounts(
    rows: list[dict[str, Any]], marker_kind: str
) -> dict[str, Decimal]:
    """Return posted accounting clearings by service month."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        match = SETTLEMENT_MARKER_RE.match(note_text(row.get("note")))
        if not match or match.group("kind") != marker_kind:
            continue
        amount = cents(row.get("amount"))
        if amount > 0:
            totals[match.group("month")] += amount
    return totals


def pending_cash_settlements(
    rows: list[dict[str, Any]],
    *,
    tag_ids: set[str],
    marker_kind: str,
) -> list[tuple[str, str, Decimal, tuple[str, ...]]]:
    """Return posting month, date, unsettled amount, and source cash IDs."""
    cash_by_month: dict[str, Decimal] = defaultdict(Decimal)
    dates_by_month: dict[str, str] = {}
    ids_by_month: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if str(row.get("bankAccountId") or "") != OPERATIONS_ACCOUNT_ID:
            continue
        if str(row.get("tagId") or "") not in tag_ids:
            continue
        month = str(row.get("date") or "")[:7]
        if not re.fullmatch(r"20\d{2}-\d{2}", month):
            continue
        cash_by_month[month] += cents(row.get("amount"))
        dates_by_month[month] = max(
            dates_by_month.get(month, ""), str(row.get("date") or "")
        )
        if cents(row.get("amount")) < 0 and row.get("id") is not None:
            ids_by_month[month].append(str(row["id"]))

    existing = existing_settlement_amounts(rows, marker_kind)
    pending = []
    for month, net_cash in sorted(cash_by_month.items()):
        amount = max(Decimal("0"), -net_cash - existing.get(month, Decimal("0")))
        if amount > 0:
            pending.append(
                (month, dates_by_month[month], amount, tuple(ids_by_month[month]))
            )
    return pending


def settlement_posting_month_amounts(
    rows: list[dict[str, Any]], marker_kind: str
) -> dict[str, Decimal]:
    """Return positive accounting clearings by their P&L posting month."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        match = SETTLEMENT_MARKER_RE.match(note_text(row.get("note")))
        amount = cents(row.get("amount"))
        month = str(row.get("date") or "")[:7]
        if (
            match
            and match.group("kind") == marker_kind
            and amount > 0
            and re.fullmatch(r"20\d{2}-\d{2}", month)
        ):
            totals[month] += amount
    return totals


def unreferenced_settlement_posting_month_amounts(
    rows: list[dict[str, Any]], marker_kind: str
) -> dict[str, Decimal]:
    """Return legacy aggregate journals that do not identify their cash row."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        note = note_text(row.get("note"))
        match = SETTLEMENT_MARKER_RE.match(note)
        amount = cents(row.get("amount"))
        month = str(row.get("date") or "")[:7]
        if (
            match
            and match.group("kind") == marker_kind
            and amount > 0
            and re.fullmatch(r"20\d{2}-\d{2}", month)
            and not re.search(
                r"\b(?:payment|transaction|tx)\s*[:=]\s*\d+\b",
                note,
                re.I,
            )
        ):
            totals[month] += amount
    return totals


def referenced_cash_ids(rows: list[dict[str, Any]], marker_kind: str) -> set[str]:
    result: set[str] = set()
    for row in rows:
        match = SETTLEMENT_MARKER_RE.match(note_text(row.get("note")))
        if not match or match.group("kind") != marker_kind:
            continue
        result.update(
            re.findall(
                r"\b(?:payment|transaction|tx)\s*[:=]\s*(\d+)\b",
                note_text(row.get("note")),
                re.I,
            )
        )
    return result


def mortgage_cash_role(row: dict[str, Any]) -> str | None:
    """Classify an actual mortgage-tagged bank row by economic counterparty."""
    if str(row.get("bankAccountId") or "") != OPERATIONS_ACCOUNT_ID:
        return None
    if str(row.get("tagId") or "") != TAG_MORTGAGE_INTEREST:
        return None
    if cents(row.get("amount")) == 0:
        return None
    text = " ".join(
        str(value or "")
        for value in (
            row.get("merchantName"),
            row.get("description"),
            note_text(row.get("note")),
        )
    ).lower()
    if OWNER_MORTGAGE_COUNTERPARTY in text:
        return "owner_reimbursement_or_advance"
    if LENDER_MORTGAGE_COUNTERPARTY in text:
        return "lender_payment"
    return "unclassified_mortgage_cash"


def mortgage_reconciliation_issues(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return evidence conflicts that must hold all 9CC live mutations."""
    issues: list[dict[str, Any]] = []
    owner_cash_ids: list[str] = []
    unclassified_cash_ids: list[str] = []
    lender_net_by_month: dict[str, Decimal] = defaultdict(Decimal)

    for row in rows:
        role = mortgage_cash_role(row)
        if role is None:
            continue
        row_id = str(row.get("id") or "")
        amount = cents(row.get("amount"))
        month = str(row.get("date") or "")[:7]
        if role == "owner_reimbursement_or_advance" and amount < 0:
            owner_cash_ids.append(row_id)
        elif role == "unclassified_mortgage_cash" and amount < 0:
            unclassified_cash_ids.append(row_id)
        elif role == "lender_payment" and re.fullmatch(r"20\d{2}-\d{2}", month):
            lender_net_by_month[month] += amount

    if owner_cash_ids:
        issues.append({
            "code": "owner_cash_misclassified_as_lender_mortgage_cash",
            "message": (
                "Stone Manor cash-outs are reimbursements or advances and cannot "
                "also settle the UBS mortgage accrual."
            ),
            "transaction_ids": sorted(owner_cash_ids, key=transaction_id_sort_key),
        })
    if unclassified_cash_ids:
        issues.append({
            "code": "unclassified_mortgage_cash_counterparty",
            "message": "Mortgage-tagged bank debits require a verified lender or owner role.",
            "transaction_ids": sorted(
                unclassified_cash_ids, key=transaction_id_sort_key
            ),
        })

    for month, recognized in LENDER_STATEMENT_RECOGNIZED_INTEREST_BY_MONTH.items():
        bank_paid = max(Decimal("0"), -lender_net_by_month.get(month, Decimal("0")))
        if bank_paid == recognized:
            continue
        issues.append({
            "code": "lender_bank_statement_month_mismatch",
            "month": month,
            "bank_net_lender_debits": format(bank_paid, ".2f"),
            "lender_statement_recognized_interest": format(recognized, ".2f"),
            "difference": format(bank_paid - recognized, ".2f"),
            "message": (
                "The DAO bank chronology does not match the lender-recognized monthly "
                "interest. Do not assign an unsupported service period."
            ),
            "evidence": LENDER_STATEMENT_EVIDENCE,
        })

    legacy_settlements = []
    for row in rows:
        note = note_text(row.get("note"))
        match = SETTLEMENT_MARKER_RE.match(note)
        if (
            match
            and match.group("kind") == "mortgage_settlement"
            and cents(row.get("amount")) > 0
            and not _cash_reference_ids_for_note(note)
        ):
            legacy_settlements.append(str(row.get("id") or ""))
    if legacy_settlements:
        issues.append({
            "code": "unreferenced_legacy_mortgage_settlement_journals",
            "message": (
                "Legacy positive mortgage journals mix owner reimbursements with lender "
                "payments and must be rebuilt from exact cash references."
            ),
            "transaction_ids": sorted(
                legacy_settlements, key=transaction_id_sort_key
            ),
        })
    return issues


def property_hold_report(
    reconciliation_report: dict[str, Any],
    *,
    source_report: Path,
) -> dict[str, Any]:
    """Project this audit into the monthly pipeline's property-hold schema."""
    issues = reconciliation_report.get("reconciliation_issues")
    if not isinstance(issues, list):
        issues = []
    held = bool(issues) or reconciliation_report.get("mutation_allowed") is not True
    held_properties = []
    if held:
        held_properties.append({
            "property": PROPERTY,
            "property_name": PROPERTY,
            "property_id": PROPERTY_ID,
            "hold_scope": "lofty_financial_summary_and_owner_email",
            "hold_reason": (
                "Mortgage cash chronology and lender-statement evidence do not "
                "reconcile; do not publish or mutate guessed financial values."
            ),
            "issue_count": len(issues),
            "issue_codes": [
                str(issue.get("code") or "unknown")
                for issue in issues
                if isinstance(issue, dict)
            ],
            "mutation_allowed": False,
            "live_state_verified": True,
            "source_digest": reconciliation_report.get("digest"),
            "source_report": str(source_report),
        })
    return {
        "schema_version": 1,
        "generated_at": reconciliation_report.get("generated_at"),
        "status": "held" if held else "ok",
        "source": "baselane_reconcile_9_country_club",
        "source_report": str(source_report),
        "source_digest": reconciliation_report.get("digest"),
        "live_state_verified": True,
        "held_property_count": len(held_properties),
        "held_properties": held_properties,
    }


def _cash_reference_ids_for_note(note: str) -> set[str]:
    return set(
        re.findall(
            r"\b(?:payment|transaction|tx)\s*[:=]\s*(\d+)\b",
            note,
            re.I,
        )
    )


def mortgage_service_period(row: dict[str, Any]) -> str:
    row_id = str(row.get("id") or "")
    text = " ".join(
        str(value or "")
        for value in (row.get("merchantName"), row.get("description"), note_text(row.get("note")))
    )
    named = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})\b",
        text,
        re.I,
    )
    if named:
        month_number = {
            name: index
            for index, name in enumerate(
                (
                    "january", "february", "march", "april", "may", "june",
                    "july", "august", "september", "october", "november", "december",
                ),
                start=1,
            )
        }[named.group(1).lower()]
        return f"{int(named.group(2)):04d}-{month_number:02d}"
    posting_month = str(row.get("date") or "")[:7]
    if not re.fullmatch(r"20\d{2}-\d{2}", posting_month):
        raise ValueError(f"mortgage cash row {row_id or '<unknown>'} has no valid date")
    return posting_month


def pending_mortgage_cash_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select exact cash debits not yet represented by positive P&L journals."""
    cash_by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    net_cash_by_month: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        if mortgage_cash_role(row) != "lender_payment":
            continue
        month = str(row.get("date") or "")[:7]
        if not re.fullmatch(r"20\d{2}-\d{2}", month):
            continue
        amount = cents(row.get("amount"))
        net_cash_by_month[month] += amount
        if amount < 0:
            cash_by_month[month].append(row)

    existing_by_month = settlement_posting_month_amounts(
        rows, "mortgage_settlement"
    )
    legacy_by_month = unreferenced_settlement_posting_month_amounts(
        rows, "mortgage_settlement"
    )
    referenced = referenced_cash_ids(rows, "mortgage_settlement")
    result: list[dict[str, Any]] = []
    for month, net_cash in sorted(net_cash_by_month.items()):
        pending = max(
            Decimal("0"),
            -net_cash - existing_by_month.get(month, Decimal("0")),
        )
        if pending == 0:
            continue

        candidates = sorted(
            (
                row
                for row in cash_by_month[month]
                if str(row.get("id") or "") not in referenced
            ),
            key=lambda row: (
                str(row.get("date") or ""),
                transaction_id_sort_key(row.get("id")),
            ),
        )
        # Legacy aggregate journals do not carry payment IDs.  They represented
        # the latest same-month debit, matching the historical allocator.
        legacy_amount = legacy_by_month.get(month, Decimal("0"))
        for row in reversed(candidates):
            if legacy_amount <= 0:
                break
            value = -cents(row.get("amount"))
            if value > legacy_amount:
                raise ValueError(
                    f"ambiguous partial legacy mortgage settlement for {month}: "
                    f"remaining={legacy_amount:.2f}, payment={row.get('id')} amount={value:.2f}"
                )
            legacy_amount -= value
            referenced.add(str(row.get("id") or ""))
        if legacy_amount != 0:
            raise ValueError(
                f"unmatched legacy mortgage settlement for {month}: {legacy_amount:.2f}"
            )

        selected_total = Decimal("0")
        for row in candidates:
            if str(row.get("id") or "") in referenced:
                continue
            value = -cents(row.get("amount"))
            if selected_total + value > pending:
                raise ValueError(
                    f"ambiguous mortgage cash remainder for {month}: "
                    f"pending={pending:.2f}, payment={row.get('id')} amount={value:.2f}"
                )
            result.append(row)
            selected_total += value
            if selected_total == pending:
                break
        if selected_total != pending:
            raise ValueError(
                f"mortgage cash remainder lacks exact source rows for {month}: "
                f"pending={pending:.2f}, selected={selected_total:.2f}"
            )
    return result


def month_range(start: str, end: str) -> list[str]:
    year, month = map(int, start.split("-"))
    end_year, end_month = map(int, end.split("-"))
    result = []
    while (year, month) <= (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return result


def query_transactions(filter_values: dict[str, Any]) -> list[dict[str, Any]]:
    query = """
    query Transactions($input: SortsAndFilters) {
      transactions(input: $input) {
        total
        data {
          id amount date merchantName description propertyId unitId tagId bankAccountId note
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
                "filter": {**filter_values, "isHidden": False, "isDeleted": False},
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


def query_bank_balances() -> dict[str, Decimal]:
    data = run_graphql({
        "operationName": "BankAccounts",
        "variables": {},
        "query": "query BankAccounts { bankAccounts { id currentBalance availableBalance } }",
    })["data"]["bankAccounts"]
    return {
        str(row["id"]): cents(row.get("currentBalance"))
        for row in data
        if str(row.get("id")) in {OPERATIONS_ACCOUNT_ID, "165515"}
    }


def update_transaction(transaction_id: str, values: dict[str, Any]) -> dict[str, Any]:
    payload = {"id": transaction_id, **values, "isReviewedByUser": True}
    return run_graphql({
        "operationName": "UpdateTransaction",
        "variables": {"input": [payload]},
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id amount date propertyId tagId bankAccountId note isManual
          }
        }
        """,
    })["data"]["updateTransactions"][0]


def create_transaction(target: dict[str, Any]) -> dict[str, Any]:
    values = {
        "merchantName": target["merchantName"],
        "note": target["note"],
        "tagId": target["tagId"],
        "propertyId": PROPERTY_ID,
        "unitId": None,
        "entityId": None,
        "date": target["date"],
        "bankAccountId": None,
        "amount": float(target["amount"]),
        "isReviewedByUser": True,
    }
    return run_graphql({
        "operationName": "createTransaction",
        "variables": values,
        "query": """
        mutation createTransaction(
          $merchantName: String!, $note: String!, $tagId: ID, $propertyId: ID,
          $unitId: ID, $entityId: Int, $date: String!, $bankAccountId: ID,
          $amount: Float!, $isReviewedByUser: Boolean
        ) {
          createTransaction(input: {
            merchantName: $merchantName note: $note tagId: $tagId
            propertyId: $propertyId unitId: $unitId entityId: $entityId
            date: $date bankAccountId: $bankAccountId amount: $amount
            isReviewedByUser: $isReviewedByUser
          }) { id amount date propertyId tagId bankAccountId note isManual }
        }
        """,
    })["data"]["createTransaction"]


def normalized_update_value(field: str, value: Any) -> Any:
    if field == "amount":
        return format(cents(value), ".2f")
    if field == "note":
        return note_text(value)
    if field in {"propertyId", "unitId", "tagId", "bankAccountId"}:
        return None if value is None or str(value) == "" else str(value)
    return value


def update_decision(
    rows_by_id: dict[str, dict[str, Any]], target: dict[str, Any]
) -> dict[str, Any]:
    current = rows_by_id.get(str(target["id"]))
    fields = tuple(target["values"])
    current_values = {
        field: (current.get(field) if current is not None else None)
        for field in fields
    }
    normalized_current = {
        field: normalized_update_value(field, value)
        for field, value in current_values.items()
    }
    normalized_target = {
        field: normalized_update_value(field, value)
        for field, value in target["values"].items()
    }
    return {
        **target,
        "missing": current is None,
        "requires_update": current is not None
        and normalized_current != normalized_target,
        "current": current_values,
        "normalized_current": normalized_current,
        "normalized_target": normalized_target,
    }


def split_july_mortgage() -> dict[str, Any]:
    children = [
        {
            "amount": -2875.00,
            "tagId": TAG_MORTGAGE_INTEREST,
            "propertyId": PROPERTY_ID,
            "merchantName": "UBS Bank USA - monthly mortgage interest",
            "date": "2026-07-01",
        },
        {
            "amount": -150.25,
            "tagId": TAG_MORTGAGE_FEES,
            "propertyId": PROPERTY_ID,
            "merchantName": "UBS Bank USA - mortgage fees",
            "date": "2026-07-01",
        },
    ]
    return run_graphql({
        "operationName": "createOrUpdateSplitTx",
        "variables": {
            "parentTransactionId": JULY_MORTGAGE_PARENT_ID,
            "splitType": "AMOUNT",
            "transactionSplitInputs": children,
        },
        "query": """
        mutation createOrUpdateSplitTx(
          $parentTransactionId: ID!, $splitType: SplitType!,
          $transactionSplitInputs: [TransactionSplitInput!]!
        ) {
          createOrUpdateSplitTx(input: {
            parentTransactionId: $parentTransactionId
            transactionSplitInputs: $transactionSplitInputs
            splitType: $splitType
          }) {
            id
            splitTransactions { id tagId propertyId amount merchantName date parentId }
          }
        }
        """,
    })["data"]["createOrUpdateSplitTx"]


def scheduled_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    existing_notes = {note_text(row.get("note")) for row in rows}
    mortgage_issues = mortgage_reconciliation_issues(rows)

    for month in month_range(MORTGAGE_START_MONTH, MORTGAGE_END_MONTH):
        marker = f"AOPS-PNL-ACCRUAL|mortgage_interest|{PROPERTY}|{month}|2875.00"
        if any(marker in note for note in existing_notes):
            continue
        targets.append({
            "kind": "mortgage_accrual",
            "merchantName": f"Mortgage Interest Accrual | {PROPERTY} | {month}",
            "date": "2026-07-01" if month == "2026-07" else f"{month}-28",
            "amount": -MORTGAGE_INTEREST,
            "tagId": TAG_MORTGAGE_INTEREST,
            "note": marker + " | Monthly interest-only UBS obligation. Accounting/manual accrual only, no bank transfer.",
        })

    for cash_row in ([] if mortgage_issues else pending_mortgage_cash_rows(rows)):
        payment_id = str(cash_row["id"])
        service_period = mortgage_service_period(cash_row)
        amount = -cents(cash_row.get("amount"))
        marker = (
            f"AOPS-9CC-RECON|mortgage_settlement|{PROPERTY}|"
            f"{service_period}|{amount:.2f}"
        )
        if any(marker in note for note in existing_notes):
            continue
        posting_month = str(cash_row.get("date") or "")[:7]
        if service_period > posting_month:
            explanation = "Offsets bank-paid prepaid mortgage interest"
        else:
            explanation = "Offsets bank-paid mortgage interest against its exact monthly accrual"
        targets.append({
            "kind": "mortgage_settlement",
            "merchantName": (
                f"Mortgage Interest Cash Settlement | {PROPERTY} | {service_period}"
            ),
            "date": str(cash_row["date"]),
            "amount": amount,
            "tagId": TAG_MORTGAGE_INTEREST,
            "note": (
                f"{marker} | service_period={service_period}|payment={payment_id}|"
                f"bill={amount:.2f} | {explanation}; no bank transfer."
            ),
            "sourceTransactionId": payment_id,
            "sourceSnapshot": {
                key: cash_row.get(key)
                for key in (
                    "amount", "date", "merchantName", "description", "propertyId",
                    "tagId", "bankAccountId", "note", "parentId",
                )
            },
        })

    pm_cash = defaultdict(Decimal)
    pm_dates: dict[str, str] = {}
    for row in rows:
        is_fixed_pm_row = str(row.get("id")) == PM_PAYMENT_ID
        if str(row.get("bankAccountId") or "") != OPERATIONS_ACCOUNT_ID:
            continue
        if str(row.get("tagId") or "") != TAG_PROPERTY_MANAGEMENT and not is_fixed_pm_row:
            continue
        month = str(row.get("date") or "")[:7]
        pm_cash[month] += cents(row.get("amount"))
        pm_dates[month] = max(pm_dates.get(month, ""), str(row.get("date") or ""))
    for month, net_amount in sorted(pm_cash.items()):
        amount = max(Decimal("0"), -net_amount)
        if amount <= 0:
            continue
        marker = f"AOPS-9CC-RECON|pm_settlement_cash|{PROPERTY}|{month}|{amount:.2f}"
        if any(marker in note for note in existing_notes):
            continue
        targets.append({
            "kind": "pm_settlement",
            "merchantName": f"PM Fee Cash Settlement | {PROPERTY} | {month}",
            "date": pm_dates[month],
            "amount": amount,
            "tagId": TAG_PROPERTY_MANAGEMENT,
            "note": marker + " | Offsets cash paid to ECO against cumulative PM fee accruals; no bank transfer.",
        })

    for month, cash_date, amount, payment_ids in pending_cash_settlements(
        rows,
        tag_ids={TAG_TAXES, TAG_PROPERTY_TAXES},
        marker_kind="tax_settlement",
    ):
        tax_marker = f"AOPS-9CC-RECON|tax_settlement|{PROPERTY}|{month}|{amount:.2f}"
        if any(tax_marker in note for note in existing_notes):
            continue
        targets.append({
            "kind": "tax_settlement",
            "merchantName": f"Property Tax Cash Settlement | {PROPERTY} | {month}",
            "date": cash_date,
            "amount": amount,
            "tagId": TAG_TAXES,
            "note": (
                tax_marker
                + " | "
                + "|".join(f"payment={payment_id}" for payment_id in payment_ids)
                + f"|bill={amount:.2f} | Offsets the DAO-bank property-tax payment "
                "against accrued taxes; no bank transfer."
            ),
            "sourceTransactionIds": list(payment_ids),
        })
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit and idempotently reconcile live Baselane rows for 9 Country Club Ln N.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--digest")
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "baselane_9_country_club_reconciliation.json")
    parser.add_argument(
        "--hold-report",
        type=Path,
        default=ROOT / "reports" / "baselane_property_reconciliation_holds.json",
    )
    args = parser.parse_args()

    property_rows = query_transactions({"propertyId": PROPERTY_ID})
    pm_search_rows = query_transactions({"search": "PM Fees Jan 2026"})
    rows_by_id = {str(row["id"]): row for row in property_rows + pm_search_rows}
    target_rows_by_id = {str(row["id"]): row for row in property_rows}
    for row in pm_search_rows:
        if str(row.get("id")) == PM_PAYMENT_ID:
            target_rows_by_id[str(row["id"])] = row
    targets = scheduled_targets(list(target_rows_by_id.values()))
    reconciliation_issues = mortgage_reconciliation_issues(
        list(target_rows_by_id.values())
    )
    balances = query_bank_balances()

    update_targets = [
        {
            "id": PM_PAYMENT_ID,
            "values": {"propertyId": PROPERTY_ID, "unitId": None, "tagId": TAG_PROPERTY_MANAGEMENT},
            "reason": "January 2026 PM cash payment belonged to 9 Country Club and settles its PM accrual",
        },
        {
            "id": JUNE_INSURANCE_PAYMENT_ID,
            "values": {"propertyId": PROPERTY_ID, "unitId": None, "tagId": TAG_INSURANCE},
            "reason": "June New York Central $918 debit is the monthly dwelling insurance premium",
        },
        {
            "id": JUNE_INSURANCE_ACCRUAL_ID,
            "values": {
                "amount": 0.0,
                "propertyId": PROPERTY_ID,
                "unitId": None,
                "tagId": TAG_INSURANCE,
                "note": "AOPS-9CC-RECON|void_insurance_accrual|9 Country Club Ln N|2026-06|1552.91 | Voided because the $918 New York Central premium cleared the DAO bank on 2026-06-23.",
            },
            "reason": "remove duplicate and stale-rate June insurance accrual",
        },
        {
            "id": JULY_MORTGAGE_FEE_CHILD_ID,
            "values": {"propertyId": PROPERTY_ID, "unitId": None, "tagId": TAG_MORTGAGE_FEES},
            "reason": "classify the $150.25 UBS remainder as other loan payment/mortgage fees",
        },
        *[
            {
                "id": transaction_id,
                "values": {"propertyId": PROPERTY_ID, "unitId": None, "tagId": TAG_TRANSFERS},
                "reason": "pair the NBT account-verification debit with its equal transfer credit",
            }
            for transaction_id in NBT_VERIFICATION_DEBIT_IDS
        ],
    ]

    july_parent = rows_by_id.get(JULY_MORTGAGE_PARENT_ID)
    split_required = bool(july_parent and not july_parent.get("isSplit"))
    updates = [update_decision(rows_by_id, target) for target in update_targets]
    digest_payload = {
        "property_id": PROPERTY_ID,
        "split_required": split_required,
        "reconciliation_issues": reconciliation_issues,
        "updates": updates,
        "create_targets": [
            {**row, "amount": format(row["amount"], ".2f")} for row in targets
        ],
    }
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    if args.apply and args.digest != digest:
        raise SystemExit(f"apply requires exact live dry-run digest: {digest}")
    if args.apply and reconciliation_issues:
        raise SystemExit(
            "9 Country Club live mutations are blocked by mortgage reconciliation "
            f"issues: {[issue['code'] for issue in reconciliation_issues]}"
        )

    actions: list[dict[str, Any]] = []
    if args.apply:
        if split_required:
            actions.append({"kind": "split", "result": split_july_mortgage()})
        for update in updates:
            if update["missing"]:
                actions.append({"kind": "update", "id": update["id"], "status": "missing"})
                continue
            if not update["requires_update"]:
                actions.append(
                    {"kind": "update", "id": update["id"], "status": "unchanged"}
                )
                continue
            result = update_transaction(update["id"], update["values"])
            actions.append({"kind": "update", "id": update["id"], "reason": update["reason"], "result": result})
        for target in targets:
            actions.append({"kind": "create", "target_kind": target["kind"], "result": create_transaction(target)})

    mortgage_accrual_total = MORTGAGE_INTEREST * len(month_range(MORTGAGE_START_MONTH, MORTGAGE_END_MONTH))
    mortgage_settlement_total = None
    if not reconciliation_issues:
        mortgage_settlement_total = sum(
            (
                target["amount"]
                for target in targets
                if target["kind"] == "mortgage_settlement"
            ),
            Decimal("0"),
        )
        mortgage_settlement_total += sum(
            (
                cents(row.get("amount"))
                for row in property_rows
                if "AOPS-9CC-RECON|mortgage_settlement|"
                in note_text(row.get("note"))
            ),
            Decimal("0"),
        )
    pm_settlement_total = sum((target["amount"] for target in targets if target["kind"] == "pm_settlement"), Decimal("0"))
    pm_settlement_total += sum(
        (cents(row.get("amount")) for row in property_rows if "AOPS-9CC-RECON|pm_settlement_cash|" in note_text(row.get("note"))),
        Decimal("0"),
    )

    report = {
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": (
            "blocked_reconciliation"
            if reconciliation_issues
            else ("applied" if args.apply else "dry_run")
        ),
        "digest": digest,
        "property": PROPERTY,
        "property_id": PROPERTY_ID,
        "bank_balances": {key: float(value) for key, value in balances.items()},
        "physical_cash": float(sum(balances.values(), Decimal("0"))),
        "split_required": split_required,
        "mutation_allowed": not reconciliation_issues,
        "reconciliation_issue_count": len(reconciliation_issues),
        "reconciliation_issues": reconciliation_issues,
        "updates": updates,
        "update_required_count": sum(
            1 for update in updates if update["requires_update"]
        ),
        "create_target_count": len(targets),
        "create_targets": [{**row, "amount": float(row["amount"])} for row in targets],
        "mortgage": {
            "loan_type": "interest_only",
            "principal_balance": 1200000.00,
            "rate": 0.02875,
            "monthly_interest": float(MORTGAGE_INTEREST),
            "scheduled_months": month_range(MORTGAGE_START_MONTH, MORTGAGE_END_MONTH),
            "scheduled_interest": float(mortgage_accrual_total),
            "cash_settlements": (
                float(mortgage_settlement_total)
                if mortgage_settlement_total is not None
                else None
            ),
            "net_accrual_less_settlements": (
                float(mortgage_accrual_total - mortgage_settlement_total)
                if mortgage_settlement_total is not None
                else None
            ),
            "outstanding_interest_payable": (
                float(max(Decimal("0"), mortgage_accrual_total - mortgage_settlement_total))
                if mortgage_settlement_total is not None
                else None
            ),
            "prepaid_interest": (
                float(max(Decimal("0"), mortgage_settlement_total - mortgage_accrual_total))
                if mortgage_settlement_total is not None
                else None
            ),
        },
        "pm_cash_settlements": float(pm_settlement_total),
        "actions": actions,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    hold_report = property_hold_report(report, source_report=args.report)
    args.hold_report.parent.mkdir(parents=True, exist_ok=True)
    args.hold_report.write_text(
        json.dumps(hold_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": report["status"],
        "create_target_count": report["create_target_count"],
        "split_required": split_required,
        "physical_cash": report["physical_cash"],
        "mortgage_outstanding": report["mortgage"]["outstanding_interest_payable"],
        "reconciliation_issue_count": report["reconciliation_issue_count"],
        "report": str(args.report),
        "hold_report": str(args.hold_report),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
