import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from baselane_ledger_revenue_policy import (  # noqa: E402
    is_categoryless_known_rent_revenue,
    is_short_term_rent_revenue,
)
from coownership_reserve_policy import financial_bucket  # noqa: E402


def row(**overrides):
    value = {
        "Amount": "100.00",
        "Merchant": "",
        "Description": "",
        "Notes": "",
        "Type": "",
        "Category": "",
        "Sub-category": "",
    }
    value.update(overrides)
    return value


def test_known_platform_matches_merchant_description_or_notes() -> None:
    assert is_categoryless_known_rent_revenue(row(Merchant="Airbnb Payments"))
    assert is_categoryless_known_rent_revenue(row(Description="BOOKINGCOM payout"))
    assert is_categoryless_known_rent_revenue(row(Notes="Deposit from Hospitable, Inc."))


def test_fallback_requires_positive_amount_and_blank_category() -> None:
    assert not is_categoryless_known_rent_revenue(row(Merchant="Booking.com", Amount="-25.00"))
    assert not is_categoryless_known_rent_revenue(
        row(Merchant="Airbnb", Category="Fees & Other Revenue")
    )
    assert not is_categoryless_known_rent_revenue(row(Merchant="Unrelated deposit"))


def test_explicit_and_export_defect_short_term_rows_share_one_policy() -> None:
    assert is_short_term_rent_revenue(row(Category="Short Term Rents"))
    categoryless = row(Description="Hostshare deposit")
    assert is_short_term_rent_revenue(categoryless)
    assert financial_bucket(categoryless) == "rents"
