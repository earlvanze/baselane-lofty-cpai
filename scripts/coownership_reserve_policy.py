from __future__ import annotations

import csv
import json
import re
from collections import namedtuple
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from baselane_ledger_revenue_policy import is_categoryless_known_rent_revenue


MAINTENANCE_RESERVE_TARGET = Decimal("3000.00")
FULL_REPLENISHMENT_THRESHOLD = Decimal("1500.00")
FULL_REPLENISHMENT_RATE = Decimal("1.00")
HALF_REPLENISHMENT_RATE = Decimal("0.50")
PERPETUAL_REPLENISHMENT_RATE = Decimal("0.05")
POLICY_EFFECTIVE_MONTH = "2026-03"
RETAINED_KIND = "retained_capital"
RETAINED_PREFIX = "AOPS-PNL-ACCRUAL"

POLICY_PROPERTIES: dict[str, tuple[str, ...]] = {
    "84 Madison Ave": ("84 Madison Ave",),
    "86 Madison Ave": ("86 Madison Ave",),
    "88 Madison Ave": ("88 Madison Ave",),
    "90 Madison Ave": ("90 Madison Ave",),
    "9 Country Club Ln N": ("9 Country Club Ln N", "9 Country Club Lane North"),
    "724 3rd Ave": ("724 3rd Ave", "724 3rd Avenue"),
    "85-104 Alawa Pl": ("85-104 Alawa Pl", "85-104 Alawa Place"),
    "326-332 S Alcott St, Denver, CO 80219": (
        "326-332 S Alcott St",
        "326 South Alcott Street",
    ),
    "22164 Umland Cir, Jenner, CA 95450": (
        "22164 Umland Cir",
        "22164 Umland Circle",
    ),
    "917 Pawnee Ave, Memphis, TN 38109": ("917 Pawnee Ave",),
    "Ohio 3-Property Package": (
        "Ohio 3-Property Package",
        "Ohio 3 Property Package",
    ),
}

LOCAL_FINANCIALS_ONLY_PROPERTIES = (
    "724 3rd Ave",
    "Ohio 3-Property Package",
)

PROPERTY_RESERVE_POLICIES = {
    "917 Pawnee Ave, Memphis, TN 38109": {
        "effective_month": "2026-07",
        "target": Decimal("10000.00"),
        "pre_target_rate": Decimal("0.25"),
        "perpetual_rate": Decimal("0.10"),
        "governance_status": "approved",
    },
}

# These properties remain in the canonical Dropbox/CF reporting flow but are no
# longer managed through Lofty. They cannot provide a current Lofty operating
# reserve and must not hold live-managed properties' monthly close.

RESET_MARKERS = (
    "eco-dao-month-end-reset",
    "eco-dao-2026-capital",
    "eco-dao-9cc-capital",
)

REVENUE_BUCKETS = {"rents", "repairs_reimbursement", "fees_other_revenue"}
OPERATING_EXPENSE_BUCKETS = {
    "cleaning_maintenance",
    "insurance",
    "legal_professional",
    "property_mgmt_fee",
    "software_subscriptions",
    "repairs_supplies",
    "taxes",
    "utilities",
}


def money(value: object) -> Decimal:
    raw = str(value or "0").strip().replace(",", "").replace("$", "")
    if raw.startswith("(") and raw.endswith(")"):
        raw = f"-{raw[1:-1]}"
    try:
        return Decimal(raw or "0")
    except Exception:
        return Decimal("0")


def round_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def combined_reserve_position(
    eco_held_spendable_cash: object,
    lofty_operating_reserve: object,
    reserve_floor: object = MAINTENANCE_RESERVE_TARGET,
) -> dict[str, Decimal]:
    """Calculate the reserve floor across ECO cash and Lofty OR.

    Lofty OR is DAO liquidity even though it is not cash held by ECO. A cash
    transfer out of ECO may therefore use the combined surplus, but it can
    never exceed the non-negative cash actually held by ECO.
    """
    eco_cash = round_money(money(eco_held_spendable_cash))
    lofty_or = round_money(max(money(lofty_operating_reserve), Decimal()))
    floor = round_money(max(money(reserve_floor), Decimal()))
    combined = round_money(eco_cash + lofty_or)
    combined_surplus = round_money(max(combined - floor, Decimal()))
    combined_shortfall = round_money(max(floor - combined, Decimal()))
    sendable_eco_cash = round_money(min(max(eco_cash, Decimal()), combined_surplus))
    return {
        "eco_held_spendable_cash": eco_cash,
        "lofty_operating_reserve": lofty_or,
        "combined_reserve_liquidity": combined,
        "reserve_floor": floor,
        "combined_surplus_above_floor": combined_surplus,
        "combined_shortfall_to_floor": combined_shortfall,
        "sendable_eco_cash": sendable_eco_cash,
    }


@lru_cache(maxsize=4096)
def normalize_property_text(value: str) -> str:
    text = value.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    replacements = {
        "avenue": "ave",
        "street": "st",
        "circle": "cir",
        "lane": "ln",
        "place": "pl",
        "north": "n",
        "south": "s",
    }
    for source, target in replacements.items():
        text = re.sub(rf"\b{source}\b", target, text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_property(value: object) -> str:
    return normalize_property_text(str(value or ""))


@lru_cache(maxsize=4096)
def canonical_property_text(value: str) -> str | None:
    target = normalize_property_text(value)
    if not target:
        return None
    for canonical, aliases in POLICY_PROPERTIES.items():
        for alias in (canonical, *aliases):
            candidate = normalize_property_text(str(alias or ""))
            if candidate == target or candidate in target or target in candidate:
                return canonical
    return None


def canonical_property(value: object) -> str | None:
    return canonical_property_text(str(value or ""))


def live_lofty_reserve_required_properties() -> tuple[str, ...]:
    local_only = set(LOCAL_FINANCIALS_ONLY_PROPERTIES)
    return tuple(name for name in POLICY_PROPERTIES if name not in local_only)


def row_matches_property(row: dict[str, Any], property_name: str) -> bool:
    row_canonical = canonical_property(row.get("Property"))
    requested_canonical = canonical_property(property_name)
    if row_canonical is not None or requested_canonical is not None:
        return row_canonical == requested_canonical
    row_name = normalize_property(row.get("Property"))
    requested_name = normalize_property(property_name)
    return bool(
        row_name
        and requested_name
        and (
            row_name == requested_name
            or row_name in requested_name
            or requested_name in row_name
        )
    )


def row_date(row: dict[str, Any]) -> date | None:
    raw = str(row.get("ISODate") or row.get("Date") or "").strip()
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw[:10] if fmt == "%Y-%m-%d" else raw, fmt).date()
        except ValueError:
            continue
    return None


def month_end(month: str) -> date:
    year, number = (int(part) for part in month.split("-"))
    if number == 12:
        return date(year, 12, 31)
    return date(year, number + 1, 1).fromordinal(date(year, number + 1, 1).toordinal() - 1)


def is_reset_row(row: dict[str, Any]) -> bool:
    note = str(row.get("Notes") or "").lower()
    return any(marker in note for marker in RESET_MARKERS)


def is_or_replenishment_row(row: dict[str, Any]) -> bool:
    note = str(row.get("Notes") or "").lower()
    return f"|{RETAINED_KIND}|" in note or "|or_replenishment|" in note


def is_reserve_cash_settlement(row: dict[str, Any]) -> bool:
    """Identify a cleared cash reserve transfer rather than an AOPS designation."""
    if money(row.get("Amount")) >= 0 or is_manual_accrual_row(row):
        return False
    transfer_classification = normalize_property(
        " ".join(
            str(row.get(key) or "")
            for key in ("Type", "Category", "Sub-category")
        )
    )
    if "transfer" not in transfer_classification:
        return False
    transaction_text = normalize_property(
        " ".join(
            str(row.get(key) or "")
            for key in ("Merchant", "Description", "Notes")
        )
    )
    return (
        "or replenishment" in transaction_text
        or "operating reserve replenishment" in transaction_text
        or "reserve retention" in transaction_text
    )


def is_manual_accrual_row(row: dict[str, Any]) -> bool:
    """Return whether a row is an accounting-only AOPS accrual journal."""
    return str(row.get("Notes") or "").strip().lower().startswith("aops-")


def manual_accrual_family(row: dict[str, Any]) -> str | None:
    """Return the normalized AOPS control family from a journal note."""
    match = re.match(
        r"\s*(AOPS-[^|]+)\|",
        str(row.get("Notes") or ""),
        re.I,
    )
    return match.group(1).strip().upper() if match else None


def manual_accrual_kind(row: dict[str, Any]) -> str | None:
    """Return the AOPS accrual kind encoded in a journal note."""
    note = str(row.get("Notes") or "")
    match = re.match(r"\s*AOPS-[^|]+\|([^|]+)\|", note, re.I)
    if not match:
        return None
    kind = match.group(1).strip().lower()

    # 9 Country Club records the owner's lender payment as a positive owner-
    # payable journal. Economically it clears the property's mortgage-interest
    # accrual once; a later DAO-to-owner reimbursement settles the owner payable
    # and must not clear mortgage expense a second time.
    if manual_accrual_family(row) == "AOPS-9CC-NOAH-MORTGAGE-ADVANCE":
        return "mortgage_settlement"

    # Historical direct PM accruals used
    # ``AOPS-PM-FEE|{property}|YYYY-MM|amount`` before the paired journals
    # introduced explicit ``pm_dao``/``pm_eco`` kinds.  The property token is
    # not an accrual kind; recognize the legacy shape as the DAO-side PM
    # liability so it remains open until an actual PM cash settlement clears
    # it.  Explicit modern PM control kinds retain their encoded meaning.
    if re.match(r"\s*AOPS-PM-FEE\|", note, re.I):
        if kind in {"pm", "pm_dao", "pm_eco", "pm_settlement", "pm_settlement_cash"}:
            return kind
        legacy = re.match(
            r"\s*AOPS-PM-FEE\|[^|]+\|(\d{4}-\d{2})\|",
            note,
            re.I,
        )
        if legacy:
            return "pm"
    return kind


def is_pm_manual_accrual(row: dict[str, Any]) -> bool:
    return manual_accrual_kind(row) in {"pm", "pm_dao"}


OBLIGATION_ACCRUAL_FAMILIES = {
    "AOPS-PNL-ACCRUAL",
    "AOPS-MONTHLY-ACCRUAL",
    "AOPS-OHIL-ACCRUAL",
    "AOPS-PAU-ACCRUAL",
    "AOPS-PM-FEE",
    "AOPS-804-PM-FEE",
    "AOPS-804-NATHANIEL-MORTGAGE-ADVANCE",
}

MONTH_NUMBERS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
MONTH_PATTERN = "|".join(sorted(MONTH_NUMBERS, key=len, reverse=True))


def is_obligation_accrual_row(row: dict[str, Any]) -> bool:
    """Identify an AOPS family that creates a still-payable obligation."""
    return manual_accrual_family(row) in OBLIGATION_ACCRUAL_FAMILIES


def parse_pay_period(value: object, posted: date | None = None) -> str | None:
    """Extract a deterministic YYYY-MM service period from transaction text."""
    text = str(value or "")
    iso = re.search(r"(?<!\d)(20\d{2})[-/](0[1-9]|1[0-2])(?!\d)", text)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}"

    named = re.search(
        rf"\b({MONTH_PATTERN})[\s.-]+(20\d{{2}})\b",
        text,
        re.I,
    )
    if named:
        month_number = MONTH_NUMBERS[named.group(1).lower()]
        year = int(named.group(2))
        return f"{year:04d}-{month_number:02d}"

    compact = re.search(
        rf"\b({MONTH_PATTERN})[.-]?(\d{{2}})\b",
        text,
        re.I,
    )
    if compact:
        month_number = MONTH_NUMBERS[compact.group(1).lower()]
        year = 2000 + int(compact.group(2))
        return f"{year:04d}-{month_number:02d}"

    if posted is None:
        return None
    month_only = re.search(rf"\b({MONTH_PATTERN})\b", text, re.I)
    if not month_only:
        return None
    month_number = MONTH_NUMBERS[month_only.group(1).lower()]
    year = posted.year - (1 if month_number > posted.month else 0)
    return f"{year:04d}-{month_number:02d}"


def manual_accrual_period(row: dict[str, Any]) -> str | None:
    return parse_pay_period(row.get("Notes"), row_date(row))


def pay_period_range_end(value: object, posted: date | None = None) -> str | None:
    """Return the inclusive end month for an explicit multi-month label."""
    match = re.search(
        rf"\b({MONTH_PATTERN})\b\s*(?:-|to|thru|through)\s*"
        rf"\b({MONTH_PATTERN})\b(?:[\s,.-]+(20\d{{2}}))?",
        str(value or ""),
        re.I,
    )
    if not match:
        return None
    month_number = MONTH_NUMBERS[match.group(2).lower()]
    if match.group(3):
        year = int(match.group(3))
    elif posted is not None:
        year = posted.year - (1 if month_number > posted.month else 0)
    else:
        return None
    return f"{year:04d}-{month_number:02d}"


def row_event_key(row: dict[str, Any]) -> tuple[int, str]:
    """Return a stable transaction ordering independent of input row order."""
    posted = row_date(row)
    raw_id = str(row.get("BaselaneId") or "").strip()
    if raw_id.isdigit():
        sequence = raw_id.zfill(24)
    else:
        sequence = "\x1f".join(
            str(row.get(key) or "")
            for key in ("Account", "Amount", "Merchant", "Description", "Notes")
        )
    return (posted.toordinal() if posted is not None else -1, sequence)


def _itemized_pm_component(value: object) -> Decimal:
    matches = re.findall(
        r"\bPM\s*(?:component\s*)?[:=]?\s*\$?([0-9][0-9,]*\.\d{2})\b",
        str(value or ""),
        re.I,
    )
    return sum((money(match) for match in matches), Decimal("0"))


def _itemized_dao_fee_component(value: object) -> Decimal:
    """Return the explicitly itemized DAO-fee portion of a net transfer."""
    text = str(value or "")
    amount_pattern = r"(\$[0-9][0-9,]*(?:\.\d{1,2})?|[0-9][0-9,]*\.\d{1,2})"
    matches = [
        *re.findall(
            r"\bDAO(?:\s+LLC)?\s+fees?\s*(?:cash|component|paid)?\s*[:=]?\s*"
            + amount_pattern,
            text,
            re.I,
        ),
        *re.findall(
            amount_pattern + r"\s*\bDAO(?:\s+LLC)?\s+fees?\b",
            text,
            re.I,
        ),
    ]
    return round_money(sum((money(match) for match in matches), Decimal("0")))


def pm_cash_settlement_amount(row: dict[str, Any]) -> Decimal:
    """Return DAO-to-ECO cash applied to PM, excluding other split purposes."""
    amount = money(row.get("Amount"))
    if amount >= 0 or is_manual_accrual_row(row):
        return Decimal("0")

    account = normalize_property(
        row.get("Account") or row.get("BankAccountId") or row.get("Property")
    )
    if not account or "eco systems" in account:
        return Decimal("0")

    source_raw = " ".join(
        str(row.get(key) or "") for key in ("Merchant", "Description")
    )
    source = normalize_property(source_raw)
    note = normalize_property(row.get("Notes"))
    category = normalize_property(
        " ".join(
            str(row.get(key) or "")
            for key in ("Type", "Category", "Sub-category")
        )
    )
    eco_counterparty = (
        "eco systems" in source
        or bool(re.search(r"\b(?:[a-z0-9]+\s+)?eco\b", source))
        or bool(re.search(r"\bto\s+eco\b", note))
    )
    if not eco_counterparty:
        return Decimal("0")

    itemized_component = _itemized_pm_component(source_raw)
    if itemized_component > 0:
        return round_money(min(-amount, itemized_component))

    source_has_pm = bool(re.search(r"\bpm\b", source))
    source_has_other_purpose = bool(
        re.search(
            r"\b(?:dao(?:\s+llc)?\s+fee|legal|cleaning|repair|supplies?|tax(?:es)?|"
            r"insurance|mortgage|principal|escrow|refund|late\s+fee|nsf|utilities?|"
            r"water|electric|reserve|interest)\b|\bp\s+i\b",
            source,
        )
    )
    if source_has_other_purpose and not source_has_pm and "property management" not in category:
        return Decimal("0")

    note_has_pm = bool(re.search(r"\bpm\b", note))
    note_is_composite_non_pm = bool(
        re.search(r"\b(?:mortgage|principal|escrow|refund|late\s+fee|nsf)\b", note)
    )
    if source_has_pm or "property management" in category:
        return round_money(-amount)
    if note_has_pm and not note_is_composite_non_pm and not source_has_other_purpose:
        return round_money(-amount)
    return Decimal("0")


def is_pm_cash_settlement(row: dict[str, Any]) -> bool:
    """Identify an actual DAO-to-ECO PM payment, not its accrual journal."""
    return pm_cash_settlement_amount(row) > 0


def pm_cash_settlement_period(row: dict[str, Any]) -> str | None:
    exact_period, maximum_period = pm_cash_settlement_allocation(row)
    return exact_period or maximum_period


def pm_cash_settlement_allocation(
    row: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Return exact and maximum periods for a PM cash allocation."""
    source = " ".join(
        str(row.get(key) or "") for key in ("Merchant", "Description")
    )
    note = str(row.get("Notes") or "")
    posted = row_date(row)
    for value in (source, note):
        deposit_month = re.search(
            rf"\b({MONTH_PATTERN})\s+deposit\b",
            value,
            re.I,
        )
        if deposit_month and posted is not None:
            period = parse_pay_period(deposit_month.group(1), posted)
            return period, period
    if (range_end := pay_period_range_end(source, posted)) is not None:
        return None, range_end
    if (period := parse_pay_period(source, posted)) is not None:
        return period, period
    if (range_end := pay_period_range_end(note, posted)) is not None:
        return None, range_end
    period = parse_pay_period(note, posted)
    return period, period


def explicit_pm_unpaid_balance(row: dict[str, Any]) -> Decimal | None:
    """Read an audited remaining-PM balance from a settlement memo, if present."""
    match = re.search(
        r"unpaid\s+pm\s+accrual\s*\$?([0-9][0-9,]*(?:\.\d{1,2})?)\s+remains",
        str(row.get("Notes") or ""),
        re.I,
    )
    return money(match.group(1)) if match else None


def pm_audit_anchor(row: dict[str, Any]) -> tuple[str, Decimal] | None:
    """Return an audited PM balance and its inclusive through-month."""
    note = str(row.get("Notes") or "")
    through = re.search(
        rf"\bpm(?:\s+cash)?\s+thr(?:u|ough)\s+"
        rf"((?:{MONTH_PATTERN})[\s.-]*(?:20\d{{2}}|\d{{2}})|20\d{{2}}-(?:0[1-9]|1[0-2]))",
        note,
        re.I,
    )
    if not through:
        return None
    period = parse_pay_period(through.group(1), row_date(row))
    if period is None:
        return None
    explicit_balance = explicit_pm_unpaid_balance(row)
    if explicit_balance is not None:
        return period, round_money(explicit_balance)

    normalized = normalize_property(note)
    pm_amount = re.search(
        rf"\bpm(?:\s+cash)?\s+thr(?:u|ough)\s+"
        rf"(?:{MONTH_PATTERN})[\s.-]*(?:20\d{{2}}|\d{{2}})\s*\$?[0-9]",
        note,
        re.I,
    )
    if pm_amount and "mortgage" in normalized and "=" in note:
        return period, Decimal("0.00")
    return None


ACCRUED_OBLIGATION_KINDS = {
    "dao",
    "general_escrow",
    "insurance",
    "insurance_escrow",
    "interest",
    "legal",
    "mortgage_interest",
    "pm",
    "pm_dao",
    "principal",
    "retained_capital",
    "tax_escrow",
    "taxes",
}
SETTLEMENT_TO_OBLIGATION_KIND = {
    "dao_settlement": "dao",
    "insurance_settlement": "insurance",
    "legal_settlement": "legal",
    "mortgage_settlement": "mortgage_interest",
    "pm_settlement": "pm",
    "pm_settlement_cash": "pm",
    "tax_settlement": "taxes",
    "taxes_settlement": "taxes",
}


def manual_accrual_settlement(row: dict[str, Any]) -> tuple[str, Decimal] | None:
    """Return the obligation kind and amount cleared by an explicit journal.

    Settlement journals are accounting-only rows that prevent a later cash
    payment from leaving the original accrual open.  Counterparty revenue rows
    (``dao_eco``/``pm_eco``) are deliberately not settlements.
    """
    if not is_manual_accrual_row(row):
        return None
    note = str(row.get("Notes") or "")
    family = manual_accrual_family(row)
    kind = manual_accrual_kind(row) or ""
    target = SETTLEMENT_TO_OBLIGATION_KIND.get(kind)
    amount = money(row.get("Amount"))
    if note.upper().startswith("AOPS-TAX-SETTLEMENT|"):
        target = "taxes"
    if family == "AOPS-ACCRUAL-CLEARING" and kind in ACCRUED_OBLIGATION_KINDS:
        target = "pm" if kind == "pm_dao" else kind
        amount = abs(amount)
    if kind.startswith("void_") and kind.endswith("_accrual"):
        target = kind[len("void_") : -len("_accrual")]
        if amount <= 0:
            marker = re.search(r"\|(?:20\d{2}-\d{2}|[^|]+)\|([0-9][0-9,]*(?:\.\d{1,2})?)\s*(?:\||$)", note)
            amount = money(marker.group(1)) if marker else Decimal("0")
    if not target or amount <= 0:
        return None
    return target, amount


ObligationSettlementEvent = namedtuple(
    "ObligationSettlementEvent",
    (
        "event_key",
        "posted",
        "kind",
        "amount",
        "exact_period",
        "minimum_period",
        "maximum_period",
        "source",
        "family",
        "note",
        "row_ids",
    ),
)


def _nearest_month_period(value: object, posted: date) -> str | None:
    """Parse a service month, assigning month-only labels to the nearest year."""
    text = str(value or "")
    explicit = parse_pay_period(text)
    if explicit is not None:
        return explicit
    month_only = re.search(rf"\b({MONTH_PATTERN})\b", text, re.I)
    if not month_only:
        return None
    month_number = MONTH_NUMBERS[month_only.group(1).lower()]
    posted_index = posted.year * 12 + posted.month - 1
    candidates = [
        (year * 12 + month_number - 1, year)
        for year in (posted.year - 1, posted.year, posted.year + 1)
    ]
    _, year = min(
        candidates,
        key=lambda candidate: (
            abs(candidate[0] - posted_index),
            candidate[0] > posted_index,
        ),
    )
    return f"{year:04d}-{month_number:02d}"


def _half_year_window(value: object) -> tuple[str, str] | None:
    text = str(value or "")
    match = re.search(r"\b(20\d{2})[-\s]?H([12])\b", text, re.I)
    if not match:
        match = re.search(
            r"\b(20\d{2})\s+(first|1st|second|2nd)\s+half\b",
            text,
            re.I,
        )
    if not match:
        match = re.search(
            r"\b(first|1st|second|2nd)\s+half(?:\s+of)?\s+(20\d{2})\b",
            text,
            re.I,
        )
        if match:
            half_token, year_token = match.group(1), match.group(2)
        else:
            return None
    else:
        year_token, half_token = match.group(1), match.group(2)
    half = 1 if str(half_token).lower() in {"1", "first", "1st"} else 2
    year = int(year_token)
    start_month, end_month = (1, 6) if half == 1 else (7, 12)
    return f"{year:04d}-{start_month:02d}", f"{year:04d}-{end_month:02d}"


def _named_month_range_window(
    value: object,
    posted: date,
) -> tuple[str, str] | None:
    match = re.search(
        rf"\b({MONTH_PATTERN})\b\s*(?:-|to|thru|through)\s*"
        rf"\b({MONTH_PATTERN})\b(?:[\s,.-]+(20\d{{2}}))?",
        str(value or ""),
        re.I,
    )
    if not match:
        return None
    start_month = MONTH_NUMBERS[match.group(1).lower()]
    end_month = MONTH_NUMBERS[match.group(2).lower()]
    if match.group(3):
        end_year = int(match.group(3))
    else:
        end_period = _nearest_month_period(match.group(2), posted)
        if end_period is None:
            return None
        end_year = int(end_period[:4])
    start_year = end_year - (1 if start_month > end_month else 0)
    return f"{start_year:04d}-{start_month:02d}", f"{end_year:04d}-{end_month:02d}"


def _obligation_period_window(
    value: object,
    posted: date,
) -> tuple[str | None, str | None, str | None]:
    if (half_year := _half_year_window(value)) is not None:
        return half_year[0], half_year[1], None
    if (month_range := _named_month_range_window(value, posted)) is not None:
        return month_range[0], month_range[1], None
    exact = _nearest_month_period(value, posted)
    return (exact, exact, exact) if exact is not None else (None, None, None)


def actual_obligation_cash_settlement(
    row: dict[str, Any],
) -> tuple[str, Decimal] | None:
    """Return a signed non-PM obligation settlement from an actual bank row."""
    amount = money(row.get("Amount"))
    if amount == 0 or is_manual_accrual_row(row):
        return None
    if not str(
        row.get("Account") or row.get("BankAccountId") or row.get("Property") or ""
    ).strip():
        return None

    classification = normalize_property(
        " ".join(
            str(row.get(key) or "")
            for key in ("Type", "Category", "Sub-category")
        )
    )
    account = normalize_property(
        row.get("Account") or row.get("BankAccountId") or row.get("Property")
    )
    description = normalize_property(row.get("Description"))
    source_raw = " ".join(
        str(row.get(key) or "") for key in ("Merchant", "Description")
    )
    note_raw = str(row.get("Notes") or "")
    source = normalize_property(source_raw)
    note = normalize_property(note_raw)
    purpose = f"{source} {note}".strip()

    if (
        "stone manor hospitality" in source
        and (
            "mortgage" in classification
            or "mortgage" in purpose
        )
    ):
        return None

    # Both mirrors of an internal DAO-to-ECO payment are property-tagged. The
    # canonical property ledger also replaces the bank-account name with the
    # property name, so use the transfer counterparty as a direction fallback.
    # The DAO cash-out settles the obligation; counting the positive ECO receipt
    # as a refund would immediately reopen it.
    if (
        amount > 0
        and (
            "internal transfer" in purpose
            or "transfers between accounts" in classification
        )
        and (
            "eco systems" in account
            or (
                "dao llc" in description
                and "eco systems" not in description
            )
        )
    ):
        return None

    kind: str | None = None
    source_has_dao_fee = bool(
        re.search(r"\bdao(?:\s+llc)?\s+fees?\b", source)
    )
    note_has_dao_fee = bool(
        re.search(r"\bdao(?:\s+llc)?\s+fees?\b", note)
    )
    source_has_other_transfer_purpose = bool(
        re.search(
            r"\b(?:pm|property tax|insurance|mortgage|principal|escrow|"
            r"cleaning|repairs?|supplies|reimbursement|legal offset|late fee|"
            r"nsf|utilities?|water|electric|reserve|interest|noi)\b",
            source,
        )
    )
    note_has_other_transfer_purpose = bool(
        re.search(
            r"\b(?:pm|property tax|insurance|mortgage|principal|escrow|"
            r"cleaning|repairs?|supplies|reimbursement|legal offset|late fee|"
            r"nsf|utilities?|water|electric|reserve|interest|noi)\b",
            note,
        )
    )
    dao_component = Decimal("0")
    if source_has_dao_fee:
        kind = "dao"
        dao_component = _itemized_dao_fee_component(source_raw)
    elif (
        note_has_dao_fee
        and not source_has_other_transfer_purpose
        and (
            not note_has_other_transfer_purpose
            or _itemized_dao_fee_component(note_raw) > 0
        )
    ):
        kind = "dao"
        dao_component = _itemized_dao_fee_component(note_raw)
    entity_purpose_pattern = (
        r"\b(?:annual filings?|corporate filings?|registered agent|"
        r"(?:[a-z]+\s+)?secretary\s+(?:of\s+)?(?:state|sta|st|s)|"
        r"sec of state|business (?:filing|license)|entity renewal|"
        r"(?:dao\s+)?llc (?:registration|filings?|annual report)|"
        r"foreign registration|publication service|nys dos|dcca electronic|"
        r"delaware corp and tax|ilsos)\b"
    )
    if kind is None and (
        re.search(entity_purpose_pattern, source)
        or (
            not source_has_other_transfer_purpose
            and re.search(entity_purpose_pattern, note)
        )
    ):
        kind = "legal"
    elif kind is None and "short term occupancy tax" in classification:
        # Occupancy tax is an operating expense, not evidence that a property-
        # tax accrual was paid.
        return None
    elif kind is None and "tax escrow" in classification:
        kind = "tax_escrow"
    elif kind is None and "insurance escrow" in classification:
        kind = "insurance_escrow"
    elif kind is None and "general escrow" in classification:
        kind = "general_escrow"
    elif kind is None and "mortgage interest" in classification:
        kind = "mortgage_interest"
    elif kind is None and "mortgage principal" in classification:
        kind = "principal"
    elif kind is None and re.search(
        r"\b(?:property|county|city|school)\s+tax(?:es)?\b",
        purpose,
    ):
        kind = "taxes"
    elif kind is None and "tax licenses and registrations" in classification:
        # Permits, occupancy registrations, and entity filings share this
        # Baselane category.  Only the entity-purpose branch above is evidence
        # for the standard legal accrual.
        return None
    elif kind is None and "accounting and tax fees" in classification:
        # This coarse Baselane category includes accounting and entity fees.
        # Require a more specific purpose before applying it to an accrual.
        return None
    elif kind is None and re.search(r"\btax(?:es)?\b", classification):
        kind = "taxes"
    elif kind is None and (
        "insurance" in classification or "rental dwelling" in classification
    ):
        kind = "insurance"
    elif kind is None and (
        "legal" in classification or "professional fees" in classification
    ):
        kind = "legal"

    if kind is None:
        if re.search(r"\b(?:insurance premium|dwelling insurance)\b", purpose):
            kind = "insurance"
        elif re.search(r"\bmortgage interest\b", purpose):
            kind = "mortgage_interest"
        elif re.search(r"\bmortgage principal\b", purpose):
            kind = "principal"
        elif re.search(r"\blegal (?:fee|expense|payment)\b", purpose):
            kind = "legal"

    if kind is None or kind not in ACCRUED_OBLIGATION_KINDS:
        return None
    if kind == "dao" and dao_component > 0:
        return kind, dao_component if amount < 0 else -dao_component
    return kind, round_money(-amount)


def _settlement_allocation(
    row: dict[str, Any],
    kind: str,
    source: str,
) -> tuple[str | None, str | None, str | None]:
    posted = row_date(row)
    if posted is None:
        return None, None, None
    text = " ".join(
        str(row.get(key) or "")
        for key in ("Merchant", "Description", "Notes")
    )
    minimum_period, maximum_period, exact_period = _obligation_period_window(
        text,
        posted,
    )
    normalized_note = normalize_property(row.get("Notes"))
    posted_period = posted.strftime("%Y-%m")
    if (
        source == "manual"
        and manual_accrual_family(row) == "AOPS-ACCRUAL-CLEARING"
    ):
        return None, maximum_period or posted_period, None
    if source == "manual" and (
        "cumulative" in normalized_note
        or "full monthly accrual schedule" in normalized_note
    ):
        return None, maximum_period, None
    if kind == "taxes" and maximum_period is not None:
        return minimum_period if exact_period is None else None, maximum_period, None
    if maximum_period is not None:
        return minimum_period, maximum_period, exact_period

    if source == "cash" and kind in {"dao", "legal", "mortgage_interest", "taxes"}:
        return None, posted_period, None
    return posted_period, posted_period, posted_period


def _cash_reference_ids(note: str) -> set[str]:
    return set(
        re.findall(
            r"\b(?:payment|transaction|tx)\s*[:=]\s*(\d+)\b",
            note,
            re.I,
        )
    )


def _cash_reference_bill(note: str) -> Decimal | None:
    match = re.search(
        r"\bbill\s*=\s*\$?([0-9][0-9,]*(?:\.\d{1,2})?)\b",
        note,
        re.I,
    )
    return money(match.group(1)) if match else None


def _settlement_event(
    row: dict[str, Any],
    kind: str,
    amount: Decimal,
    source: str,
) -> ObligationSettlementEvent | None:
    posted = row_date(row)
    if posted is None or amount == 0:
        return None
    minimum_period, maximum_period, exact_period = _settlement_allocation(
        row,
        kind,
        source,
    )
    row_id = str(row.get("BaselaneId") or "").strip()
    return ObligationSettlementEvent(
        event_key=row_event_key(row),
        posted=posted,
        kind=kind,
        amount=round_money(amount),
        exact_period=exact_period,
        minimum_period=minimum_period,
        maximum_period=maximum_period,
        source=source,
        family=manual_accrual_family(row),
        note=str(row.get("Notes") or ""),
        row_ids=(row_id,) if row_id else (),
    )


def _group_cash_settlement_events(
    events: Iterable[ObligationSettlementEvent],
) -> list[ObligationSettlementEvent]:
    grouped: dict[
        tuple[str, date, str | None, str | None, str | None],
        list[ObligationSettlementEvent],
    ] = {}
    for event in events:
        key = (
            event.kind,
            event.posted,
            event.exact_period,
            event.minimum_period,
            event.maximum_period,
        )
        grouped.setdefault(key, []).append(event)

    result: list[ObligationSettlementEvent] = []
    for group in grouped.values():
        amount = round_money(sum((event.amount for event in group), Decimal("0")))
        if amount == 0:
            continue
        latest = max(group, key=lambda event: event.event_key)
        result.append(
            ObligationSettlementEvent(
                event_key=latest.event_key,
                posted=latest.posted,
                kind=latest.kind,
                amount=amount,
                exact_period=latest.exact_period,
                minimum_period=latest.minimum_period,
                maximum_period=latest.maximum_period,
                source="cash",
                family=None,
                note=" | ".join(event.note for event in group if event.note),
                row_ids=tuple(
                    row_id
                    for event in group
                    for row_id in event.row_ids
                ),
            )
        )
    return sorted(result, key=lambda event: event.event_key)


def _dedupe_non_pm_cash_settlements(
    cash_events: Iterable[ObligationSettlementEvent],
    manual_events: Iterable[ObligationSettlementEvent],
) -> list[ObligationSettlementEvent]:
    """Keep journal allocations while dropping the bank rows they represent."""
    raw_cash = list(cash_events)
    manuals = sorted(manual_events, key=lambda event: event.event_key)
    represented_ids: set[str] = set()

    for manual in manuals:
        references = _cash_reference_ids(manual.note)
        represented_ids.update(
            row_id
            for event in raw_cash
            if event.kind == manual.kind
            for row_id in event.row_ids
            if row_id in references
        )
        bill = _cash_reference_bill(manual.note)
        if bill is None:
            continue
        candidates = [
            event
            for event in raw_cash
            if event.kind == manual.kind
            and event.posted == manual.posted
            and event.amount > 0
            and not represented_ids.intersection(event.row_ids)
            and event.amount == bill
        ]
        if candidates:
            represented_ids.update(candidates[-1].row_ids)

    remaining_cash = _group_cash_settlement_events(
        event
        for event in raw_cash
        if not represented_ids.intersection(event.row_ids)
    )
    represented_groups: set[int] = set()
    for manual in manuals:
        normalized_note = normalize_property(manual.note)
        if (
            manual.family == "AOPS-ACCRUAL-CLEARING"
            or "void " in normalized_note
            or not re.search(
                r"\b(?:cash|bank paid|payment|settlement|no bank transfer)\b",
                normalized_note,
            )
        ):
            continue
        selected: list[int] = []
        represented_amount = Decimal("0")
        for index in range(len(remaining_cash) - 1, -1, -1):
            event = remaining_cash[index]
            if index in represented_groups or event.kind != manual.kind or event.amount <= 0:
                continue
            days = (manual.posted - event.posted).days
            if days < 0 or days > 62:
                continue
            selected.append(index)
            represented_amount += event.amount
            if represented_amount == manual.amount:
                represented_groups.update(selected)
                break
            if represented_amount > manual.amount:
                break

    return [
        event
        for index, event in enumerate(remaining_cash)
        if index not in represented_groups
    ]


def _apply_obligation_settlement(
    event: ObligationSettlementEvent,
    accrued_by_period: dict[str, Decimal],
    open_by_period: dict[str, Decimal],
) -> None:
    if event.exact_period is not None:
        periods = [event.exact_period]
    else:
        periods = [
            period
            for period in sorted(open_by_period)
            if (event.minimum_period is None or period >= event.minimum_period)
            and (event.maximum_period is None or period <= event.maximum_period)
        ]
    if event.amount < 0:
        remaining = -event.amount
        for period in reversed(periods):
            capacity = accrued_by_period.get(period, Decimal("0")) - open_by_period.get(
                period,
                Decimal("0"),
            )
            reopened = min(max(capacity, Decimal("0")), remaining)
            open_by_period[period] = open_by_period.get(period, Decimal("0")) + reopened
            remaining -= reopened
            if remaining <= 0:
                break
        return

    remaining = event.amount
    for period in periods:
        available = open_by_period.get(period, Decimal("0"))
        applied = min(available, remaining)
        open_by_period[period] = available - applied
        remaining -= applied
        if remaining <= 0:
            break


def _outstanding_non_pm_liability_by_kind(
    rows: Iterable[dict[str, Any]],
) -> dict[str, Decimal]:
    accrued: dict[str, dict[str, Decimal]] = {
        kind: {} for kind in ACCRUED_OBLIGATION_KINDS if kind != "pm"
    }
    manual_events: list[ObligationSettlementEvent] = []
    cash_events: list[ObligationSettlementEvent] = []

    for row in rows:
        amount = money(row.get("Amount"))
        kind = manual_accrual_kind(row) or ""
        normalized_kind = "pm" if kind == "pm_dao" else kind
        if (
            is_obligation_accrual_row(row)
            and amount < 0
            and normalized_kind in accrued
        ):
            period = manual_accrual_period(row)
            if period is None:
                posted = row_date(row)
                period = posted.strftime("%Y-%m") if posted is not None else "0000-00"
            accrued[normalized_kind][period] = (
                accrued[normalized_kind].get(period, Decimal("0")) - amount
            )
            continue

        manual = manual_accrual_settlement(row)
        if manual is not None and manual[0] != "pm":
            event = _settlement_event(row, manual[0], manual[1], "manual")
            if event is not None:
                manual_events.append(event)
            continue

        cash = actual_obligation_cash_settlement(row)
        if cash is not None:
            event = _settlement_event(row, cash[0], cash[1], "cash")
            if event is not None:
                cash_events.append(event)

    open_by_kind = {
        kind: dict(accrued_by_period)
        for kind, accrued_by_period in accrued.items()
    }
    unmatched_cash = _dedupe_non_pm_cash_settlements(cash_events, manual_events)
    for event in sorted(
        [*manual_events, *unmatched_cash],
        key=lambda candidate: candidate.event_key,
    ):
        if event.kind not in open_by_kind:
            continue
        _apply_obligation_settlement(
            event,
            accrued[event.kind],
            open_by_kind[event.kind],
        )
    return {
        kind: round_money(sum(periods.values(), Decimal("0")))
        for kind, periods in open_by_kind.items()
    }


def _dedupe_pm_manual_settlements(
    settlements: Iterable[
        tuple[tuple[int, str], Decimal, str | None, str | None, str]
    ],
) -> list[tuple[tuple[int, str], Decimal, str | None, str | None, str]]:
    """Drop accounting journals exactly represented by earlier PM cash rows."""
    ordered = sorted(settlements, key=lambda candidate: candidate[0])
    represented_cash: set[int] = set()
    duplicate_manual: set[int] = set()

    for manual_index, manual in enumerate(ordered):
        if manual[4] != "manual":
            continue
        selected_cash: list[int] = []
        represented_amount = Decimal("0")
        for cash_index in range(manual_index - 1, -1, -1):
            cash = ordered[cash_index]
            if cash[4] != "cash" or cash_index in represented_cash:
                continue
            selected_cash.append(cash_index)
            represented_amount += cash[1]
            if represented_amount == manual[1]:
                duplicate_manual.add(manual_index)
                represented_cash.update(selected_cash)
                break
            if represented_amount > manual[1]:
                break

    return [
        settlement
        for index, settlement in enumerate(ordered)
        if index not in duplicate_manual
    ]


def _pm_pay_period_position(
    rows: Iterable[dict[str, Any]],
) -> tuple[Decimal, Decimal]:
    accrued_by_period: dict[str, Decimal] = {}
    unknown_accruals: list[tuple[tuple[int, str], Decimal]] = []
    settlements: list[
        tuple[tuple[int, str], Decimal, str | None, str | None, str]
    ] = []
    anchors: list[tuple[tuple[int, str], str, Decimal]] = []

    for row in rows:
        event_key = row_event_key(row)
        if (anchor := pm_audit_anchor(row)) is not None:
            anchors.append((event_key, anchor[0], anchor[1]))

        amount = money(row.get("Amount"))
        kind = manual_accrual_kind(row) or ""
        if (
            is_obligation_accrual_row(row)
            and amount < 0
            and kind in {"pm", "pm_dao"}
        ):
            period = manual_accrual_period(row)
            if period is None:
                unknown_accruals.append((event_key, -amount))
            else:
                accrued_by_period[period] = (
                    accrued_by_period.get(period, Decimal("0")) - amount
                )
            continue

        manual_settlement = manual_accrual_settlement(row)
        if manual_settlement is not None and manual_settlement[0] == "pm":
            period = manual_accrual_period(row)
            cumulative = "cumulative" in str(row.get("Notes") or "").lower()
            settlements.append(
                (
                    event_key,
                    manual_settlement[1],
                    None if cumulative else period,
                    period or (row_date(row).strftime("%Y-%m") if row_date(row) else None),
                    "manual",
                )
            )
            continue

        cash_amount = pm_cash_settlement_amount(row)
        if cash_amount > 0:
            exact_period, maximum_period = pm_cash_settlement_allocation(row)
            settlements.append(
                (
                    event_key,
                    cash_amount,
                    exact_period,
                    maximum_period
                    or (row_date(row).strftime("%Y-%m") if row_date(row) else None),
                    "cash",
                )
            )

    anchor_key: tuple[int, str] | None = None
    anchor_period: str | None = None
    anchor_balance = Decimal("0")
    unapplied_cash_credit = Decimal("0")
    if anchors:
        anchor_key, anchor_period, anchor_balance = max(
            anchors,
            key=lambda candidate: candidate[0],
        )

    if anchor_period is not None and anchor_key is not None:
        open_by_period = {
            period: amount
            for period, amount in accrued_by_period.items()
            if period > anchor_period
        }
        unknown_open = [
            [event_key, amount]
            for event_key, amount in unknown_accruals
            if event_key > anchor_key
        ]
    else:
        open_by_period = dict(accrued_by_period)
        unknown_open = [
            [event_key, amount] for event_key, amount in unknown_accruals
        ]

    for (
        event_key,
        settlement_amount,
        exact_period,
        maximum_period,
        source,
    ) in _dedupe_pm_manual_settlements(settlements):
        if anchor_key is not None and event_key <= anchor_key:
            continue
        remaining = settlement_amount
        credit_eligible = False
        if exact_period is not None:
            credit_eligible = exact_period in accrued_by_period
            if anchor_period is not None and exact_period <= anchor_period:
                applied = min(anchor_balance, remaining)
                anchor_balance -= applied
            else:
                available = open_by_period.get(exact_period, Decimal("0"))
                applied = min(available, remaining)
                open_by_period[exact_period] = available - applied
            remaining -= applied
            if source == "cash" and credit_eligible:
                unapplied_cash_credit += remaining
            continue

        if maximum_period is not None:
            credit_eligible = any(
                period <= maximum_period for period in accrued_by_period
            )
        if anchor_balance > 0:
            applied = min(anchor_balance, remaining)
            anchor_balance -= applied
            remaining -= applied
        for period in sorted(open_by_period):
            if remaining <= 0:
                break
            if maximum_period is not None and period > maximum_period:
                continue
            available = open_by_period[period]
            applied = min(available, remaining)
            open_by_period[period] = available - applied
            remaining -= applied
        for unknown in unknown_open:
            if remaining <= 0:
                break
            if unknown[0] > event_key:
                continue
            applied = min(unknown[1], remaining)
            unknown[1] -= applied
            remaining -= applied
        if source == "cash" and credit_eligible:
            unapplied_cash_credit += remaining

    return (
        round_money(
            anchor_balance
            + sum(open_by_period.values(), Decimal("0"))
            + sum((entry[1] for entry in unknown_open), Decimal("0"))
        ),
        round_money(unapplied_cash_credit),
    )


def _outstanding_pm_liability(rows: Iterable[dict[str, Any]]) -> Decimal:
    return _pm_pay_period_position(rows)[0]


def pm_pay_period_cash_credit(
    rows: Iterable[dict[str, Any]],
    property_name: str,
    cutoff: date,
) -> Decimal:
    """Return actual PM cash that remains unapplied to its stated pay period."""
    scoped_rows = []
    for row in rows:
        if not row_matches_property(row, property_name):
            continue
        posted = row_date(row)
        if (
            posted is None
            or posted > cutoff
            or is_reset_row(row)
            or is_or_replenishment_row(row)
        ):
            continue
        scoped_rows.append(row)
    return _pm_pay_period_position(scoped_rows)[1]


def outstanding_manual_accrual_liability(
    rows: Iterable[dict[str, Any]],
    property_name: str,
    cutoff: date,
) -> Decimal:
    """Return unpaid manual accruals after evidenced cash settlements.

    Baselane records AOPS accruals as non-cash journals.  Counting those rows
    alongside a later DAO-to-ECO payment deducts the same liability twice.
    Only negative accrual journals are liabilities; positive settlement
    journals and matching actual cash payments reduce that liability once.
    """
    scoped_rows: list[dict[str, Any]] = []
    for row in rows:
        if not row_matches_property(row, property_name):
            continue
        posted = row_date(row)
        if posted is None or posted > cutoff or is_reset_row(row) or is_or_replenishment_row(row):
            continue
        scoped_rows.append(row)

    # A PM payment settles only PM accruals.  It must never erase an unpaid
    # legal or DAO-fee journal simply because the rows share a property.
    # Composite settlements may apply cash to historic periods whose original
    # accrual journals predate the normalized ledger.  When the settlement
    # explicitly states its remaining PM balance, that audited balance is the
    # authoritative current liability rather than an incomplete ledger rollup.
    unpaid_total = _outstanding_pm_liability(scoped_rows)
    unpaid_total += sum(
        _outstanding_non_pm_liability_by_kind(scoped_rows).values(),
        Decimal("0"),
    )
    return -round_money(unpaid_total)


def outstanding_manual_accrual_liability_by_kind(
    rows: Iterable[dict[str, Any]],
    property_name: str,
    cutoff: date,
) -> dict[str, Decimal]:
    """Return positive open-liability balances for audit and disclosure."""
    scoped_rows = [
        row
        for row in rows
        if row_matches_property(row, property_name)
        and (posted := row_date(row)) is not None
        and posted <= cutoff
        and not is_reset_row(row)
        and not is_or_replenishment_row(row)
    ]
    result = _outstanding_non_pm_liability_by_kind(scoped_rows)
    result["pm"] = _outstanding_pm_liability(scoped_rows)
    return {
        kind: round_money(amount)
        for kind, amount in result.items()
        if amount != 0
    }


def eco_gl_net_of_accruals(
    rows: Iterable[dict[str, Any]],
    property_name: str,
    as_of_month: str,
) -> Decimal:
    """Return ECO cash after only its remaining manual-accrual liabilities."""
    cutoff = month_end(as_of_month)
    scoped_rows = list(rows)
    cash_total = Decimal("0")
    for row in scoped_rows:
        if not row_matches_property(row, property_name):
            continue
        posted = row_date(row)
        if posted is None or posted > cutoff:
            continue
        if is_reset_row(row) or is_or_replenishment_row(row) or is_manual_accrual_row(row):
            continue
        cash_total += money(row.get("Amount"))
    return round_money(cash_total + outstanding_manual_accrual_liability(scoped_rows, property_name, cutoff))


def financial_bucket(row: dict[str, Any]) -> str | None:
    if is_or_replenishment_row(row):
        return RETAINED_KIND
    # Paired accruals have DAO expenses and ECO revenue receivables. ECO-side
    # rows remain property-tagged for reconciliation, but are not DAO operating
    # revenue and must not inflate property NOI or reserve funding.
    if manual_accrual_kind(row) in {"dao_eco", "pm_eco"}:
        return "intercompany_eco_revenue"
    transaction_text = normalize_property(
        " ".join(str(row.get(key) or "") for key in ("Merchant", "Description"))
    )
    if "internal transfer" in transaction_text:
        return "inter_account_transfer"
    category_text = normalize_property(
        " ".join(str(row.get(key) or "") for key in ("Type", "Category", "Sub-category"))
    )
    if "insurance" in category_text or "rental dwelling" in category_text:
        return "insurance"
    if "revenue" in category_text or "rent" in category_text:
        return "rents"
    if is_categoryless_known_rent_revenue(row, row.get("Amount")):
        return "rents"
    if "loan payments" in category_text or "mortgage payment" in category_text:
        return "debt_service"
    if "capex" in category_text or "capital expenditure" in category_text:
        return "capex"
    if "utility" in category_text:
        return "utilities"
    if "tax" in category_text:
        return "taxes"
    if "management" in category_text or "pm fee" in category_text:
        return "property_mgmt_fee"
    if "software" in category_text or "subscription" in category_text:
        return "software_subscriptions"
    if "legal" in category_text or "professional" in category_text:
        return "legal_professional"
    if "clean" in category_text or "maintenance" in category_text:
        return "cleaning_maintenance"
    if "repair" in category_text or "supply" in category_text or "expense" in category_text:
        return "repairs_supplies"
    return None


def monthly_noi(rows: Iterable[dict[str, Any]], property_name: str, month: str) -> dict[str, Decimal]:
    revenue = Decimal("0")
    operating_expenses = Decimal("0")
    for row in rows:
        if not row_matches_property(row, property_name):
            continue
        posted = row_date(row)
        if posted is None or posted.strftime("%Y-%m") != month:
            continue
        bucket = financial_bucket(row)
        amount = money(row.get("Amount"))
        if bucket in REVENUE_BUCKETS:
            revenue += amount
        elif bucket in OPERATING_EXPENSE_BUCKETS:
            operating_expenses += amount
    return {
        "revenue": round_money(revenue),
        "operating_expenses": round_money(operating_expenses),
        "noi": round_money(revenue + operating_expenses),
    }


def monthly_reserve_cash_settlement(
    rows: Iterable[dict[str, Any]],
    property_name: str,
    month: str,
) -> Decimal:
    settled = Decimal("0")
    for row in rows:
        if not row_matches_property(row, property_name):
            continue
        posted = row_date(row)
        if posted is None or posted.strftime("%Y-%m") != month:
            continue
        if is_reserve_cash_settlement(row):
            settled -= money(row.get("Amount"))
    return round_money(settled)


def replenishment_rate(combined_reserve: Decimal) -> Decimal:
    if combined_reserve < FULL_REPLENISHMENT_THRESHOLD:
        return FULL_REPLENISHMENT_RATE
    if combined_reserve < MAINTENANCE_RESERVE_TARGET:
        return HALF_REPLENISHMENT_RATE
    return PERPETUAL_REPLENISHMENT_RATE


def property_replenishment_terms(
    property_name: str,
    combined_reserve: Decimal,
    lofty_operating_reserve: Decimal,
) -> tuple[Decimal, Decimal, str, Decimal]:
    policy = PROPERTY_RESERVE_POLICIES.get(property_name)
    if not policy:
        return (
            MAINTENANCE_RESERVE_TARGET,
            replenishment_rate(combined_reserve),
            POLICY_EFFECTIVE_MONTH,
            combined_reserve,
        )
    target = money(policy["target"])
    tier_basis = lofty_operating_reserve
    rate = money(policy["pre_target_rate"] if tier_basis < target else policy["perpetual_rate"])
    return target, rate, str(policy["effective_month"]), tier_basis


def approved_replenishment_exception(
    property_name: str,
    month: str,
    approved_exceptions: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Return an exact-month, exact-property reviewed reporting exception."""
    canonical = canonical_property(property_name)
    if canonical is None:
        return None
    for candidate in approved_exceptions or ():
        if not isinstance(candidate, dict):
            continue
        if candidate.get("month") != month:
            continue
        if canonical_property(candidate.get("property")) != canonical:
            continue
        try:
            amount = round_money(money(candidate.get("amount")))
        except Exception:
            continue
        if amount <= 0:
            continue
        return {**candidate, "amount": float(amount), "property": canonical}
    return None


def calculate_replenishment(
    rows: Iterable[dict[str, Any]],
    property_name: str,
    month: str,
    lofty_operating_reserve: Decimal | float | str,
    approved_exceptions: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    canonical = canonical_property(property_name)
    if canonical is None:
        raise ValueError(f"property is not in the co-ownership reserve policy: {property_name}")
    scoped_rows = list(rows)
    eco_balance = eco_gl_net_of_accruals(scoped_rows, canonical, month)
    lofty_balance = round_money(money(lofty_operating_reserve))
    combined = round_money(eco_balance + lofty_balance)
    reserve_target, rate, effective_month, tier_basis = property_replenishment_terms(
        canonical,
        combined,
        lofty_balance,
    )
    financials = monthly_noi(scoped_rows, canonical, month)
    positive_noi = max(financials["noi"], Decimal("0"))
    calculated_amount = round_money(positive_noi * rate) if month >= effective_month else Decimal("0")
    cash_settled = monthly_reserve_cash_settlement(scoped_rows, canonical, month)
    amount = max(calculated_amount - cash_settled, Decimal("0"))
    result = {
        "property": canonical,
        "month": month,
        "maintenance_reserve": float(reserve_target),
        "eco_gl_net_of_accruals": float(eco_balance),
        "lofty_operating_reserve": float(lofty_balance),
        "combined_reserve_basis": float(combined),
        "replenishment_tier_basis": float(tier_basis),
        "replenishment_rate": float(rate),
        "revenue": float(financials["revenue"]),
        "operating_expenses": float(financials["operating_expenses"]),
        "noi": float(financials["noi"]),
        "calculated_amount": float(calculated_amount),
        "cash_settled_amount": float(cash_settled),
        "cash_settlement_status": "settled" if amount == 0 else ("partially_settled" if cash_settled else "pending"),
        "amount": float(amount),
    }
    exception = approved_replenishment_exception(canonical, month, approved_exceptions)
    if exception:
        exception_amount = round_money(money(exception["amount"]))
        result["approved_amount"] = float(exception_amount)
        result["amount"] = float(max(exception_amount - cash_settled, Decimal("0")))
        result["cash_settlement_status"] = (
            "settled"
            if result["amount"] == 0
            else ("partially_settled" if cash_settled else "pending")
        )
        result["approved_exception"] = {
            "approval_id": str(exception.get("approval_id") or ""),
            "reason": str(exception.get("reason") or ""),
        }
    return result


def properties_from_lofty_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [payload]
    while candidates:
        current = candidates.pop()
        if isinstance(current, dict):
            properties = current.get("properties")
            if isinstance(properties, list) and properties and isinstance(properties[0], dict):
                return properties
            candidates.extend(current.values())
        elif isinstance(current, list):
            candidates.extend(current)
    return []


def load_lofty_reserves(path: Path) -> dict[str, float]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, float] = {}
    for prop in properties_from_lofty_response(payload):
        canonical = canonical_property(
            " ".join(str(prop.get(key) or "") for key in ("address", "address_line1", "assetName"))
        )
        if canonical:
            result[canonical] = float(money(prop.get("curr_maintenance_reserve")))
    return result


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return list(csv.DictReader(handle))
