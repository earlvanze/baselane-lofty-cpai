#!/usr/bin/env python3
"""Attach dated Baselane DAO bank balances as reconciliation evidence."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_REPORT = Path(__file__).absolute().parents[1] / "reports/baselane_live_dao_cash_reconciliation.json"
AUTHORITATIVE_SOURCE_MODE = "live_baselane_dao_bank_accounts"
PROPERTY_ALIASES = {
    "326 332 s alcott st": "326 s alcott st",
}


def normalize_property(value: Any) -> str:
    text = str(value or "").strip().casefold()
    # Candidate packets commonly carry city/state/ZIP, while Baselane account
    # nicknames use only the street address.
    text = text.split(",", 1)[0]
    text = re.sub(r"\bpublic\b", " ", text)
    text = text.replace("&", " and ")
    replacements = {
        "avenue": "ave",
        "street": "st",
        "road": "rd",
        "lane": "ln",
        "circle": "cir",
        "north": "n",
        "south": "s",
        "east": "e",
        "west": "w",
    }
    for source, target in replacements.items():
        text = re.sub(rf"\b{source}\b", target, text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    normalized = re.sub(r"\s+", " ", text).strip()
    return PROPERTY_ALIASES.get(normalized, normalized)


def money(value: Any) -> float | None:
    try:
        return float(Decimal(str(value)).quantize(Decimal("0.01")))
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_report(
    path: Path,
    *,
    today: date | None = None,
    max_age_days: int = 1,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "missing",
        "path": str(path),
        "source_mode": AUTHORITATIVE_SOURCE_MODE,
        "as_of": None,
        "generated_at": None,
        "properties": {},
        "intercompany": {},
        "issues": [],
    }
    if not path.is_file():
        result["issues"].append("live_dao_bank_report_missing")
        return result
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["status"] = "unreadable"
        result["issues"].append(f"live_dao_bank_report_unreadable:{type(exc).__name__}")
        return result
    as_of_text = str(payload.get("as_of") or "").strip()
    generated = parse_timestamp(payload.get("generated_at"))
    try:
        as_of = date.fromisoformat(as_of_text)
    except ValueError:
        as_of = None
    result["as_of"] = as_of_text or None
    result["generated_at"] = generated.isoformat() if generated else None
    if as_of is None or generated is None:
        result["status"] = "invalid"
        result["issues"].append("live_dao_bank_report_timestamp_invalid")
        return result
    current = today or datetime.now(timezone.utc).date()
    age_days = (current - as_of).days
    result["age_days"] = age_days
    if age_days < 0 or age_days > max_age_days:
        result["status"] = "stale"
        result["issues"].append(f"live_dao_bank_report_stale:{age_days}_days")
        return result
    if str(payload.get("status") or "") != "ok":
        result["status"] = "review"
        result["issues"].append(f"live_dao_bank_report_status:{payload.get('status') or 'missing'}")
        return result
    indexed: dict[str, dict[str, Any]] = {}
    duplicate_keys: set[str] = set()
    for row in payload.get("properties") or []:
        if not isinstance(row, dict):
            continue
        key = normalize_property(row.get("property"))
        balance = money(row.get("dao_bank_total"))
        if not key or balance is None:
            continue
        if key in indexed:
            duplicate_keys.add(key)
        indexed[key] = dict(row)
    if duplicate_keys:
        result["status"] = "invalid"
        result["issues"].extend(f"duplicate_live_dao_bank_property:{key}" for key in sorted(duplicate_keys))
        return result
    intercompany_index: dict[str, dict[str, Any]] = {}
    duplicate_intercompany_keys: set[str] = set()
    for row in payload.get("intercompany_subledger") or []:
        if not isinstance(row, dict):
            continue
        key = normalize_property(row.get("property"))
        if not key:
            continue
        if key in intercompany_index:
            duplicate_intercompany_keys.add(key)
        intercompany_index[key] = dict(row)
    if duplicate_intercompany_keys:
        result["status"] = "invalid"
        result["issues"].extend(
            f"duplicate_intercompany_property:{key}"
            for key in sorted(duplicate_intercompany_keys)
        )
        return result
    result["properties"] = indexed
    result["intercompany"] = intercompany_index
    result["status"] = "ok"
    return result


def cash_for_property(authority: dict[str, Any], property_name: Any) -> dict[str, Any]:
    key = normalize_property(property_name)
    row = (authority.get("properties") or {}).get(key)
    intercompany = (authority.get("intercompany") or {}).get(key) or {}
    common = {
        "property_key": key,
        "source_mode": AUTHORITATIVE_SOURCE_MODE,
        "source_path": authority.get("path"),
        "as_of": authority.get("as_of"),
    }
    if authority.get("status") != "ok":
        return {
            **common,
            "status": str(authority.get("status") or "missing"),
            "amount": None,
            "issues": list(authority.get("issues") or []),
        }
    if not isinstance(row, dict):
        has_intercompany_evidence = bool(intercompany)
        return {
            **common,
            "status": "property_missing",
            "amount": None,
            "issues": ["live_dao_bank_property_missing"],
            "eco_held_cash_gross": money(
                intercompany.get("eco_held_dao_cash_before_obligations")
            ) if has_intercompany_evidence else 0.0,
            "eco_held_restricted_cash": 0.0,
            "dao_accounts_payable_to_eco": money(
                intercompany.get("dao_accounts_payable_to_eco") or 0
            ),
            "eco_accounts_receivable_from_dao": money(
                intercompany.get("eco_accounts_receivable_from_dao") or 0
            ),
            "intercompany_payable_status": (
                "ok" if intercompany.get("status") == "ok" or not intercompany else "review"
            ),
            "intercompany_source_mode": intercompany.get("source_mode")
            or "id_bearing_eco_account_intercompany_subledger",
            "gross_eco_advances": money(intercompany.get("gross_eco_advances") or 0),
            "gross_dao_cash_credits": money(intercompany.get("gross_dao_cash_credits") or 0),
            "intercompany_monthly_breakdown": intercompany.get("monthly_breakdown") or [],
            "intercompany_category_breakdown": intercompany.get("category_breakdown") or [],
        }
    return {
        **common,
        "status": "ok",
        "amount": money(row.get("dao_bank_total")),
        "operations_balance": money(row.get("operations_balance")),
        "documented_security_principal": money(row.get("documented_security_principal")),
        # These balances must be supplied by an explicit custody/servicer
        # reconciliation.  They cannot be inferred from a property tag or the
        # cumulative property GL.
        "eco_held_cash_gross": money(row.get("eco_held_cash_gross")),
        "eco_held_restricted_cash": money(row.get("eco_held_restricted_cash")) or 0.0,
        "open_accrued_obligations": money(row.get("open_accrued_obligations")),
        # A report may supply the already-netted value, but new producers
        # should prefer gross custody plus explicit restrictions so the
        # calculation remains auditable.
        "eco_held_unrestricted_cash": money(row.get("eco_held_unrestricted_cash")),
        "eco_attributed_account_activity": money(row.get("eco_attributed_account_activity")),
        "eco_cash_reconciliation_deficit": money(row.get("eco_cash_reconciliation_deficit")),
        "eco_funded_activity_pending_reciprocal_review": money(
            row.get("eco_funded_activity_pending_reciprocal_review")
        ),
        "dao_accounts_payable_to_eco": money(
            row.get("dao_accounts_payable_to_eco", intercompany.get("dao_accounts_payable_to_eco"))
        ),
        "eco_accounts_receivable_from_dao": money(
            row.get("eco_accounts_receivable_from_dao", intercompany.get("eco_accounts_receivable_from_dao"))
        ),
        "intercompany_payable_status": row.get("intercompany_payable_status")
        or ("ok" if intercompany.get("status") == "ok" else "reconciliation_pending"),
        "intercompany_source_mode": row.get("intercompany_source_mode")
        or intercompany.get("source_mode"),
        "gross_eco_advances": money(
            row.get("gross_eco_advances", intercompany.get("gross_eco_advances"))
        ),
        "gross_dao_cash_credits": money(
            row.get("gross_dao_cash_credits", intercompany.get("gross_dao_cash_credits"))
        ),
        "intercompany_monthly_breakdown": row.get("intercompany_monthly_breakdown")
        or intercompany.get("monthly_breakdown")
        or [],
        "intercompany_category_breakdown": row.get("intercompany_category_breakdown")
        or intercompany.get("category_breakdown")
        or [],
        "restricted_mortgage_escrow": money(row.get("restricted_mortgage_escrow")),
        "restricted_mortgage_escrow_status": (
            "ok" if money(row.get("restricted_mortgage_escrow")) is not None else "reconciliation_pending"
        ),
        "operating_float": money(row.get("operating_float")),
        "protected_minimum": money(row.get("protected_minimum")),
        "account_count": len(row.get("accounts") or []),
        "matched_property": row.get("property"),
        "generated_at": authority.get("generated_at"),
        "issues": [],
    }


def apply_to_summary(
    summary: dict[str, Any],
    property_name: Any,
    authority: dict[str, Any],
) -> dict[str, Any]:
    output = dict(summary)
    cash = cash_for_property(authority, property_name)
    gl_value = money(output.get("eco_gl_column_e_sum"))
    eco_gross = cash.get("eco_held_cash_gross")
    eco_restricted = cash.get("eco_held_restricted_cash") or 0.0
    # Use the obligations from the same dated custody reconciliation whenever
    # present.  Mixing a live custody net with a separately transformed GL
    # accrual total can make gross - obligations != spendable cash.
    authority_open_accruals = cash.get("open_accrued_obligations")
    open_accruals = (
        authority_open_accruals
        if authority_open_accruals is not None
        else money(output.get("open_accrued_obligations"))
    )
    eco_source_mode = "verified_eco_cash_custody_reconciliation"
    eco_source = cash.get("source_path")
    eco_as_of = cash.get("as_of")
    eco_balance_scope = "eco_held_unrestricted_cash_only"

    # A property without a dedicated bank account can still have cash in ECO's
    # pooled account, but only ID-bearing ECO bank transactions prove that
    # custody.  Column E is an accounting control and never a cash fallback.
    if cash.get("status") == "property_missing":
        eco_source_mode = cash.get("intercompany_source_mode") or "id_bearing_eco_account_intercompany_subledger"
        eco_source = cash.get("source_path")
        eco_as_of = cash.get("as_of")
        eco_balance_scope = "transaction_backed_eco_pooled_account_custody"

    eco_unrestricted = cash.get("eco_held_unrestricted_cash")
    if eco_unrestricted is None and eco_gross is not None and open_accruals is not None:
        eco_unrestricted = max(
            0.0,
            round(float(eco_gross) - float(open_accruals) - float(eco_restricted), 2),
        )
    if eco_unrestricted is not None:
        eco_unrestricted = max(0.0, float(eco_unrestricted))
    eco_unrestricted_status = "ok" if eco_unrestricted is not None else "reconciliation_pending"
    physical_bank_cash = cash.get("amount")
    if eco_unrestricted is None:
        total_spendable = None
    elif physical_bank_cash is None and cash.get("status") == "property_missing":
        total_spendable = eco_unrestricted
    elif physical_bank_cash is not None:
        total_spendable = round(float(physical_bank_cash) + float(eco_unrestricted), 2)
    else:
        total_spendable = None
    output.update(
        {
            # Never present the full GL as cash.  The GL includes accruals and
            # accounting counterparts; ECO-held spendable cash requires a
            # separate, dated custody reconciliation.
            "eco_operating_cash": eco_unrestricted,
            "eco_operating_cash_status": eco_unrestricted_status,
            "eco_operating_cash_source_mode": eco_source_mode,
            "eco_operating_cash_source": eco_source,
            "eco_operating_cash_as_of_date": eco_as_of,
            "eco_operating_cash_balance_scope": eco_balance_scope,
            "eco_held_unrestricted_cash": eco_unrestricted,
            "eco_held_unrestricted_cash_status": eco_unrestricted_status,
            "eco_held_cash_gross": eco_gross,
            "eco_held_restricted_cash": eco_restricted,
            "eco_attributed_account_activity": cash.get("eco_attributed_account_activity"),
            "eco_cash_reconciliation_deficit": cash.get("eco_cash_reconciliation_deficit"),
            "eco_funded_activity_pending_reciprocal_review": cash.get(
                "eco_funded_activity_pending_reciprocal_review"
            ),
            "dao_accounts_payable_to_eco": cash.get("dao_accounts_payable_to_eco"),
            "eco_accounts_receivable_from_dao": cash.get("eco_accounts_receivable_from_dao"),
            "intercompany_payable_status": cash.get("intercompany_payable_status"),
            "intercompany_source_mode": cash.get("intercompany_source_mode"),
            "gross_eco_advances": cash.get("gross_eco_advances"),
            "gross_dao_cash_credits": cash.get("gross_dao_cash_credits"),
            "intercompany_monthly_breakdown": cash.get("intercompany_monthly_breakdown") or [],
            "intercompany_category_breakdown": cash.get("intercompany_category_breakdown") or [],
            "open_accrued_obligations": open_accruals,
            "open_accrued_obligations_status": (
                "ok" if open_accruals is not None else "reconciliation_pending"
            ),
            "restricted_mortgage_escrow": cash.get("restricted_mortgage_escrow"),
            "restricted_mortgage_escrow_status": cash.get("restricted_mortgage_escrow_status"),
            "total_dao_spendable_cash": total_spendable,
            "total_dao_spendable_cash_status": (
                "ok" if total_spendable is not None else "reconciliation_pending"
            ),
            "physical_bank_cash": physical_bank_cash,
            "physical_bank_cash_status": cash.get("status"),
            "physical_bank_cash_source_mode": cash.get("source_mode"),
            "physical_bank_cash_source": cash.get("source_path"),
            "physical_bank_cash_as_of_date": cash.get("as_of"),
            "physical_bank_cash_matched_property": cash.get("matched_property"),
            "eco_operations_account_balance": cash.get("operations_balance"),
            "eco_documented_security_principal": cash.get("documented_security_principal"),
            "eco_operating_float": cash.get("operating_float"),
            "eco_protected_minimum": cash.get("protected_minimum"),
            "eco_bank_account_count": cash.get("account_count"),
            "physical_bank_cash_issues": cash.get("issues"),
            "eco_bank_minus_gl_gap": (
                round(float(physical_bank_cash) - float(gl_value), 2)
                if physical_bank_cash is not None and gl_value is not None
                else None
            ),
        }
    )
    return output
