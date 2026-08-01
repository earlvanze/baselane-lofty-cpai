from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


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


def manual_accrual_kind(row: dict[str, Any]) -> str | None:
    """Return the AOPS accrual kind encoded in a journal note."""
    match = re.match(r"\s*AOPS-[^|]+\|([^|]+)\|", str(row.get("Notes") or ""), re.I)
    return match.group(1).strip().lower() if match else None


def is_pm_manual_accrual(row: dict[str, Any]) -> bool:
    return manual_accrual_kind(row) in {"pm", "pm_dao"}


def is_pm_cash_settlement(row: dict[str, Any]) -> bool:
    """Identify an actual DAO-to-ECO PM payment, not its accrual journal."""
    if money(row.get("Amount")) >= 0:
        return False
    transfer_classification = normalize_property(
        " ".join(
            str(row.get(key) or "")
            for key in ("Type", "Category", "Sub-category")
        )
    )
    if "transfer" not in transfer_classification:
        return False
    if is_manual_accrual_row(row):
        return False
    text = normalize_property(" ".join(
        str(row.get(key) or "")
        for key in ("Merchant", "Description", "Notes")
    ))
    return "pm cash" in text or "pm fee" in text or "pm fees" in text


def explicit_pm_unpaid_balance(row: dict[str, Any]) -> Decimal | None:
    """Read an audited remaining-PM balance from a settlement memo, if present."""
    if not is_pm_cash_settlement(row):
        return None
    match = re.search(
        r"unpaid\s+pm\s+accrual\s*\$?([0-9][0-9,]*(?:\.\d{1,2})?)\s+remains",
        str(row.get("Notes") or ""),
        re.I,
    )
    return money(match.group(1)) if match else None


ACCRUED_OBLIGATION_KINDS = {
    "dao",
    "insurance",
    "interest",
    "legal",
    "mortgage_interest",
    "pm",
    "pm_dao",
    "principal",
    "retained_capital",
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
    kind = manual_accrual_kind(row) or ""
    target = SETTLEMENT_TO_OBLIGATION_KIND.get(kind)
    amount = money(row.get("Amount"))
    if note.upper().startswith("AOPS-TAX-SETTLEMENT|"):
        target = "taxes"
    if kind.startswith("void_") and kind.endswith("_accrual"):
        target = kind[len("void_") : -len("_accrual")]
        if amount <= 0:
            marker = re.search(r"\|(?:20\d{2}-\d{2}|[^|]+)\|([0-9][0-9,]*(?:\.\d{1,2})?)\s*(?:\||$)", note)
            amount = money(marker.group(1)) if marker else Decimal("0")
    if not target or amount <= 0:
        return None
    return target, amount


def outstanding_manual_accrual_liability(
    rows: Iterable[dict[str, Any]],
    property_name: str,
    cutoff: date,
) -> Decimal:
    """Return unpaid manual accruals after explicit PM cash settlements.

    Baselane records AOPS accruals as non-cash journals.  Counting those rows
    alongside a later DAO-to-ECO payment deducts the same liability twice.
    Only negative accrual journals are liabilities; positive settlement
    journals and actual PM cash transfers reduce that liability.
    """
    accrued: dict[str, Decimal] = {
        kind: Decimal("0") for kind in ACCRUED_OBLIGATION_KINDS
    }
    settled: dict[str, Decimal] = {
        kind: Decimal("0") for kind in ACCRUED_OBLIGATION_KINDS
    }
    audited_pm_unpaid: Decimal | None = None
    for row in rows:
        if not row_matches_property(row, property_name):
            continue
        posted = row_date(row)
        if posted is None or posted > cutoff or is_reset_row(row) or is_or_replenishment_row(row):
            continue
        amount = money(row.get("Amount"))
        kind = manual_accrual_kind(row) or ""
        if is_manual_accrual_row(row) and amount < 0 and kind in ACCRUED_OBLIGATION_KINDS:
            accrued["pm" if kind == "pm_dao" else kind] += -amount
        elif (manual_settlement := manual_accrual_settlement(row)) is not None:
            settlement_kind, settlement_amount = manual_settlement
            if settlement_kind in settled:
                settled[settlement_kind] += settlement_amount
        elif is_pm_cash_settlement(row):
            settled["pm"] -= amount
            explicit_balance = explicit_pm_unpaid_balance(row)
            if explicit_balance is not None:
                audited_pm_unpaid = explicit_balance

    # A PM payment settles only PM accruals.  It must never erase an unpaid
    # legal or DAO-fee journal simply because the rows share a property.
    # Composite settlements may apply cash to historic periods whose original
    # accrual journals predate the normalized ledger.  When the settlement
    # explicitly states its remaining PM balance, that audited balance is the
    # authoritative current liability rather than an incomplete ledger rollup.
    unpaid_total = Decimal("0")
    for kind, accrued_amount in accrued.items():
        if kind == "pm" and audited_pm_unpaid is not None:
            unpaid_total += audited_pm_unpaid
        else:
            unpaid_total += max(accrued_amount - settled.get(kind, Decimal("0")), Decimal("0"))
    return -round_money(unpaid_total)


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
