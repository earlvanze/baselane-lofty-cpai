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
    result["properties"] = indexed
    result["status"] = "ok"
    return result


def cash_for_property(authority: dict[str, Any], property_name: Any) -> dict[str, Any]:
    key = normalize_property(property_name)
    row = (authority.get("properties") or {}).get(key)
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
        return {
            **common,
            "status": "property_missing",
            "amount": None,
            "issues": ["live_dao_bank_property_missing"],
        }
    return {
        **common,
        "status": "ok",
        "amount": money(row.get("dao_bank_total")),
        "operations_balance": money(row.get("operations_balance")),
        "documented_security_principal": money(row.get("documented_security_principal")),
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
    output.update(
        {
            # ECO Net DAO Funds is the accounting entitlement represented by
            # the full property GL, including open accruals. Bank cash is
            # independent custody evidence and must not replace or invalidate it.
            "eco_operating_cash": gl_value,
            "eco_operating_cash_status": (
                "ok" if gl_value is not None and output.get("eco_gl_column_e_status") == "ok" else "missing_gl_source"
            ),
            "eco_operating_cash_source_mode": output.get("eco_gl_column_e_source_mode"),
            "eco_operating_cash_source": output.get("eco_gl_column_e_source"),
            "eco_operating_cash_as_of_date": output.get("eco_gl_column_e_as_of_date"),
            "eco_operating_cash_balance_scope": output.get("eco_gl_column_e_scope"),
            "physical_bank_cash": cash.get("amount"),
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
                round(float(cash["amount"]) - float(gl_value), 2)
                if cash.get("amount") is not None and gl_value is not None
                else None
            ),
        }
    )
    return output
