#!/usr/bin/env python3
"""Shared revenue classification for Baselane ledger exports."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


KNOWN_CATEGORYLESS_RENT_PLATFORM_PATTERNS = (
    "airbnb",
    "booking com",
    "bookingcom",
    "hostshare",
    "evolve vacation",
    "vrbo",
    "hospitable inc",
)


def normalize_text(value: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())


def _value(row: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in row:
            return row.get(key)
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def _positive_amount(value: object) -> bool:
    raw = str(value or "").strip().replace("$", "").replace(",", "")
    if raw.startswith("(") and raw.endswith(")"):
        raw = f"-{raw[1:-1]}"
    try:
        return Decimal(raw) > 0
    except (InvalidOperation, ValueError):
        return False


def has_explicit_category(row: Mapping[str, Any]) -> bool:
    return bool(
        str(_value(row, "Category") or "").strip()
        or str(_value(row, "Sub-category", "Subcategory", "Sub_category") or "").strip()
    )


def is_categoryless_known_rent_revenue(
    row: Mapping[str, Any],
    amount: object | None = None,
) -> bool:
    """Recognize known rent-platform split children whose export lost category fields."""
    if has_explicit_category(row):
        return False
    if not _positive_amount(_value(row, "Amount") if amount is None else amount):
        return False
    transaction_text = normalize_text(
        " ".join(
            str(_value(row, field) or "")
            for field in ("Merchant", "Description", "Notes")
        )
    )
    return any(pattern in transaction_text for pattern in KNOWN_CATEGORYLESS_RENT_PLATFORM_PATTERNS)


def is_short_term_rent_revenue(row: Mapping[str, Any], amount: object | None = None) -> bool:
    category_text = normalize_text(
        " ".join(
            str(_value(row, field) or "")
            for field in ("Category", "Sub-category", "Subcategory", "Sub_category")
        )
    )
    if "short term rent" in category_text:
        return _positive_amount(_value(row, "Amount") if amount is None else amount)
    return is_categoryless_known_rent_revenue(row, amount)
