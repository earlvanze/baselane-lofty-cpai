import importlib.util
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "coownership_reserve_policy", ROOT / "scripts" / "coownership_reserve_policy.py"
)
assert SPEC and SPEC.loader
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


def row(amount, note, *, category="Property Management"):
    return {
        "Property": "Example House",
        "ISODate": "2026-07-28",
        "Amount": str(amount),
        "Type": "Manual",
        "Category": category,
        "Notes": note,
    }


def test_explicit_non_pm_settlement_clears_only_matching_obligation():
    rows = [
        row(-300, "AOPS-MONTHLY-ACCRUAL|taxes|Example House|2026-07|300.00"),
        row(-100, "AOPS-MONTHLY-ACCRUAL|dao|Example House|2026-07|100.00"),
        row(250, "AOPS-TAX-SETTLEMENT|Example House|2026-H1|250.00|bill=250.00"),
    ]
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-150.00")


def test_counterparty_revenue_does_not_clear_dao_obligation():
    rows = [
        row(-62.5, "AOPS-MONTHLY-ACCRUAL|dao|Example House|2026-07|62.50"),
        row(62.5, "AOPS-MONTHLY-ACCRUAL|dao_eco|Example House|2026-07|62.50"),
    ]
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-62.50")


def test_zero_value_void_marker_uses_encoded_accrual_amount():
    rows = [
        row(-918, "AOPS-MONTHLY-ACCRUAL|insurance|Example House|2026-06|918.00"),
        row(0, "AOPS-RECON|void_insurance_accrual|Example House|2026-06|918.00 | paid"),
    ]
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("0.00")
