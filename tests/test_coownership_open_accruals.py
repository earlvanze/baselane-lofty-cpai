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


def row(
    amount,
    note,
    *,
    category="Property Management",
    iso_date="2026-07-28",
    account="",
    merchant="",
    description="",
    transaction_type="Manual",
    baselane_id="",
):
    return {
        "Property": "Example House",
        "ISODate": iso_date,
        "Amount": str(amount),
        "Type": transaction_type,
        "Category": category,
        "Account": account,
        "Merchant": merchant,
        "Description": description,
        "Notes": note,
        "BaselaneId": baselane_id,
    }


def test_explicit_non_pm_settlement_clears_only_matching_obligation():
    rows = [
        row(
            -300,
            "AOPS-MONTHLY-ACCRUAL|taxes|Example House|2026-01|300.00",
            iso_date="2026-01-28",
        ),
        row(-100, "AOPS-MONTHLY-ACCRUAL|dao|Example House|2026-07|100.00"),
        row(250, "AOPS-TAX-SETTLEMENT|Example House|2026-H1|250.00|bill=250.00"),
    ]
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-150.00")


def test_actual_tax_cash_clears_only_accrued_tax_through_payment_month():
    rows = [
        row(
            -300,
            "AOPS-MONTHLY-ACCRUAL|taxes|Example House|2026-01|300.00",
            iso_date="2026-01-28",
        ),
        row(
            -300,
            "AOPS-MONTHLY-ACCRUAL|taxes|Example House|2026-02|300.00",
            iso_date="2026-02-28",
        ),
        row(
            -450,
            "County tax payment",
            category="Taxes",
            iso_date="2026-07-18",
            account="Example House DAO Operations",
            merchant="COUNTY TAX PMT",
            transaction_type="Operating Expenses",
            baselane_id="200",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-150.00")


def test_short_term_occupancy_tax_does_not_clear_property_tax_accrual():
    rows = [
        row(
            -300,
            "AOPS-MONTHLY-ACCRUAL|taxes|Example House|2026-02|300.00",
            iso_date="2026-02-28",
        ),
        row(
            -288.26,
            "County occupancy tax payment",
            category="Short Term Occupancy Taxes",
            iso_date="2026-02-12",
            account="Example House DAO Operations",
            merchant="SONOMA CNTY TAX",
            transaction_type="Operating Expenses",
            baselane_id="200",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 2, 28)
    ) == policy.Decimal("-300.00")


def test_entity_registration_tax_category_clears_legal_not_property_tax():
    rows = [
        row(
            -103.75,
            "AOPS-PNL-ACCRUAL|legal|Example House|2026-03|103.75",
            category="Legal Fees",
            iso_date="2026-03-28",
        ),
        row(
            -103.75,
            "AOPS-MONTHLY-ACCRUAL|taxes|Example House|2026-03|103.75",
            iso_date="2026-03-28",
        ),
        row(
            -103.75,
            "Entity annual filing",
            category="Tax Licenses & Registrations",
            iso_date="2026-03-10",
            account="ECO Systems, LLC-ECO Systems Operations-2624",
            merchant="WYOMING SECRETARY OF ST",
            transaction_type="Operating Expenses",
            baselane_id="200",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability_by_kind(
        rows, "Example House", date(2026, 3, 31)
    ) == {"taxes": policy.Decimal("103.75")}


def test_dao_registration_note_is_legal_not_property_tax():
    payment = row(
        -750,
        "DAO LLC Registration in Hawaii",
        category="Tax Licenses & Registrations",
        iso_date="2026-01-16",
        account="ECO Systems, LLC-ECO Systems Operations-2624",
        merchant="DCCA ELECTRONIC PYMT",
        transaction_type="Operating Expenses",
    )

    assert policy.actual_obligation_cash_settlement(payment) == (
        "legal",
        policy.Decimal("750.00"),
    )


def test_plural_dao_fee_cash_settles_dao_accrual_once_across_transfer_mirrors():
    rows = [
        row(
            -62.50,
            "AOPS-MONTHLY-ACCRUAL|dao|Example House|2026-04|62.50",
            iso_date="2026-04-28",
        ),
        row(
            -62.50,
            "April 2026 DAO LLC Fees",
            category="Accounting & Tax Fees",
            iso_date="2026-05-22",
            account="Example House DAO Operations",
            merchant="Example House DAO LLC | TRANSFER_O",
            description="ECO Systems, LLC | INTERNAL_TRANSFER",
            transaction_type="Operating Expenses",
            baselane_id="200",
        ),
        row(
            62.50,
            "April 2026 DAO LLC Fees",
            category="Transfers Between Accounts",
            iso_date="2026-05-22",
            account="ECO Systems, LLC-ECO Systems Operations-2624",
            merchant="Example House DAO LLC | INTERNAL_TRANSFER",
            description="Example House DAO LLC | INTERNAL_TRANSFER",
            transaction_type="Transfers & Other",
            baselane_id="201",
        ),
    ]

    assert policy.actual_obligation_cash_settlement(rows[1]) == (
        "dao",
        policy.Decimal("62.50"),
    )
    assert policy.actual_obligation_cash_settlement(rows[2]) is None
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 5, 31)
    ) == policy.Decimal("0.00")


def test_canonical_property_ledger_ignores_positive_composite_transfer_mirror():
    rows = [
        row(
            -62.50,
            "AOPS-MONTHLY-ACCRUAL|dao|Example House|2026-06|62.50",
            iso_date="2026-06-28",
        ),
        row(
            -1381.99,
            "Settlement through June 2026",
            category="Transfers Between Accounts",
            iso_date="2026-07-24",
            merchant="Example-ECO | Jun26 | Cleaning 640; PM 679.49; DAO fee 62.50",
            description="ECO Systems, LLC | INTERNAL_TRANSFER",
            transaction_type="Transfers & Other",
            baselane_id="200",
        ),
        row(
            1381.99,
            "Settlement through June 2026",
            category="Transfers Between Accounts",
            iso_date="2026-07-24",
            merchant="Example-ECO | Jun26 | Cleaning 640; PM 679.49; DAO fee 62.50",
            description="Example House DAO LLC | INTERNAL_TRANSFER",
            transaction_type="Transfers & Other",
            baselane_id="201",
        ),
        row(
            -62.50,
            "AOPS-MONTHLY-ACCRUAL|dao|Example House|2026-07|62.50",
            iso_date="2026-07-28",
        ),
    ]

    assert policy.actual_obligation_cash_settlement(rows[1]) == (
        "dao",
        policy.Decimal("62.50"),
    )
    assert policy.actual_obligation_cash_settlement(rows[2]) is None
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-62.50")


def test_composite_transfer_applies_only_itemized_dao_fee_component():
    payment = row(
        -263.43,
        "Monthly net settlement",
        category="Transfers Between Accounts",
        iso_date="2026-07-24",
        account="Example House DAO Operations",
        merchant=(
            "Example-ECO | net | DAO fees 437.50; Cleaning 775; Repairs 241.25; "
            "rent/reimb offsets 1590.32"
        ),
        description="ECO Systems, LLC | INTERNAL_TRANSFER",
        transaction_type="Transfers & Other",
    )

    assert policy.actual_obligation_cash_settlement(payment) == (
        "dao",
        policy.Decimal("437.50"),
    )


def test_pm_transfer_does_not_inherit_dao_fee_from_shared_audit_note():
    payment = row(
        -404.12,
        (
            "Unpaid PM accrual Feb26 $404.12, plus Jan26 DAO LLC fee $62.50. "
            "ECO paid Corporate Filings LLC $45. Internal only."
        ),
        category="Transfers Between Accounts",
        iso_date="2026-07-27",
        account="Example House DAO Operations",
        merchant="Example-ECO | PM cash | 2026-02",
        description="ECO Systems, LLC | INTERNAL_TRANSFER",
        transaction_type="Transfers & Other",
    )

    assert policy.actual_obligation_cash_settlement(payment) is None


def test_noi_distribution_applies_only_explicit_dao_fee_component():
    payment = row(
        -864.66,
        "May 2025 NOI + $62.50 DAO LLC Fees",
        category="Owner Contributions/Distributions",
        iso_date="2025-06-14",
        account="Example House DAO Operations",
        merchant="Example House DAO LLC | TRANSFER_O",
        description="Example House DAO LLC | TRANSFER_O",
        transaction_type="Transfers & Other",
    )

    assert policy.actual_obligation_cash_settlement(payment) == (
        "dao",
        policy.Decimal("62.50"),
    )


def test_unitemized_noi_distribution_does_not_clear_dao_fees():
    payment = row(
        -446.49,
        "50% NOI + DAO LLC Fees",
        category="Owner Contributions/Distributions",
        iso_date="2025-02-03",
        account="Example House DAO Operations",
        merchant="Example House DAO LLC | TRANSFER_O",
        description="Example House DAO LLC | TRANSFER_O",
        transaction_type="Transfers & Other",
    )

    assert policy.actual_obligation_cash_settlement(payment) is None


def test_tax_reimbursement_with_a_stated_month_clears_fifo_through_that_month():
    rows = [
        row(
            -300,
            "AOPS-MONTHLY-ACCRUAL|taxes|Example House|2026-01|300.00",
            iso_date="2026-01-28",
        ),
        row(
            -300,
            "AOPS-MONTHLY-ACCRUAL|taxes|Example House|2026-02|300.00",
            iso_date="2026-02-28",
        ),
        row(
            -450,
            "February 2026 property tax reimbursement",
            category="Taxes",
            iso_date="2026-02-15",
            account="Example House DAO Operations",
            merchant="COUNTY TAX PMT",
            transaction_type="Operating Expenses",
            baselane_id="200",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 2, 28)
    ) == policy.Decimal("-150.00")


def test_tax_settlement_journal_supersedes_the_cash_payment_it_allocates():
    rows = [
        row(
            -300,
            "AOPS-MONTHLY-ACCRUAL|taxes|Example House|2026-01|300.00",
            iso_date="2026-01-28",
        ),
        row(
            -300,
            "AOPS-MONTHLY-ACCRUAL|taxes|Example House|2026-02|300.00",
            iso_date="2026-02-28",
        ),
        row(
            -450,
            "County tax payment",
            category="Taxes",
            iso_date="2026-07-27",
            account="Example House DAO Operations",
            merchant="COUNTY TAX PMT",
            transaction_type="Operating Expenses",
            baselane_id="200",
        ),
        row(
            300,
            "AOPS-TAX-SETTLEMENT|Example House|2026-H1|300.00|"
            "bill=450.00|payment=200 | Clears the accrued portion of the bill.",
            category="Taxes",
            iso_date="2026-07-27",
            baselane_id="300",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-300.00")


def test_insurance_cash_uses_the_explicit_service_period():
    rows = [
        row(
            -918,
            "AOPS-MONTHLY-ACCRUAL|insurance|Example House|2025-10|918.00",
            category="Rental Dwelling",
            iso_date="2025-10-28",
        ),
        row(
            -918,
            "Proper Insurance October 2025",
            category="Rental Dwelling",
            iso_date="2025-11-18",
            account="Example House DAO Operations",
            merchant="INSURANCE REIMBURSEMENT",
            transaction_type="Operating Expenses",
            baselane_id="200",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("0.00")


def test_matching_mortgage_journal_and_cash_clear_the_obligation_once():
    rows = [
        row(
            -2875,
            "AOPS-PNL-ACCRUAL|mortgage_interest|Example House|2026-01|2875.00",
            category="Mortgage Interest Payments",
            iso_date="2026-01-28",
        ),
        row(
            -2875,
            "AOPS-PNL-ACCRUAL|mortgage_interest|Example House|2026-02|2875.00",
            category="Mortgage Interest Payments",
            iso_date="2026-02-28",
        ),
        row(
            -2875,
            "Mortgage Interest Payment",
            category="Mortgage Interest Payments",
            iso_date="2026-01-12",
            account="Example House DAO Operations",
            merchant="MORTGAGE LENDER",
            transaction_type="Loan Payments & Capex",
            baselane_id="200",
        ),
        row(
            2875,
            "AOPS-RECON|mortgage_settlement|Example House|2026-01|2875.00 | "
            "Offsets bank-paid mortgage interest; no bank transfer.",
            category="Mortgage Interest Payments",
            iso_date="2026-01-12",
            baselane_id="300",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-2875.00")


def test_noah_outside_payment_clears_mortgage_accrual_once():
    rows = [
        row(
            -2875,
            "AOPS-PNL-ACCRUAL|mortgage_interest|9 Country Club Ln N|2026-03|2875.00",
            category="Mortgage Interest Payments",
            iso_date="2026-03-28",
        ),
        row(
            2875,
            "AOPS-9CC-NOAH-MORTGAGE-ADVANCE|9 Country Club Ln N|2026-03 | "
            "Accounting-only payable recognition; no bank transfer represented.",
            category="Owner Contributions/Distributions",
            iso_date="2026-03-28",
        ),
    ]
    for entry in rows:
        entry["Property"] = "9 Country Club Ln N"

    liability = policy.outstanding_manual_accrual_liability_by_kind(
        rows, "9 Country Club Ln N", date(2026, 7, 31)
    )
    assert liability.get("mortgage_interest", policy.Decimal("0")) == policy.Decimal(
        "0.00"
    )


def test_stone_reimbursement_does_not_clear_mortgage_accrual_again():
    rows = [
        row(
            -2875,
            "AOPS-PNL-ACCRUAL|mortgage_interest|9 Country Club Ln N|2026-03|2875.00",
            category="Mortgage Interest Payments",
            iso_date="2026-03-28",
        ),
        row(
            2875,
            "AOPS-9CC-NOAH-MORTGAGE-ADVANCE|9 Country Club Ln N|2026-03 | "
            "Accounting-only payable recognition; no bank transfer represented.",
            category="Owner Contributions/Distributions",
            iso_date="2026-03-28",
        ),
        row(
            -2875,
            "Mortgage Interest Payment Reimbursement",
            category="Mortgage Interest Payments",
            iso_date="2026-04-07",
            account="9 Country Club DAO Operations",
            merchant="Stone Manor Hospitality LLC | TRANSFER_O",
            transaction_type="Loan Payments & Capex",
            baselane_id="200",
        ),
    ]
    for entry in rows:
        entry["Property"] = "9 Country Club Ln N"

    liability = policy.outstanding_manual_accrual_liability_by_kind(
        rows, "9 Country Club Ln N", date(2026, 7, 31)
    )
    assert liability.get("mortgage_interest", policy.Decimal("0")) == policy.Decimal(
        "0.00"
    )


def test_stone_future_advance_does_not_clear_current_mortgage_accrual():
    rows = [
        row(
            -2875,
            "AOPS-PNL-ACCRUAL|mortgage_interest|9 Country Club Ln N|2026-07|2875.00",
            category="Mortgage Interest Payments",
            iso_date="2026-07-28",
        ),
        row(
            -2875,
            "August 2026 mortgage interest",
            category="Mortgage Interest Payments",
            iso_date="2026-07-15",
            account="9 Country Club DAO Operations",
            merchant="Stone Manor Hospitality LLC | TRANSFER_O",
            transaction_type="Loan Payments & Capex",
            baselane_id="315063504",
        ),
    ]
    for entry in rows:
        entry["Property"] = "9 Country Club Ln N"

    liability = policy.outstanding_manual_accrual_liability_by_kind(
        rows, "9 Country Club Ln N", date(2026, 7, 31)
    )
    assert liability.get("mortgage_interest") == policy.Decimal("2875.00")


def test_generic_internal_transfer_does_not_clear_an_obligation():
    rows = [
        row(-300, "AOPS-MONTHLY-ACCRUAL|taxes|Example House|2026-07|300.00"),
        row(
            -300,
            "Move cash between accounts",
            category="Transfers Between Accounts",
            account="Example House DAO Operations",
            merchant="EXAMPLE HOUSE DAO SAVINGS | INTERNAL_TRANSFER",
            description="INTERNAL_TRANSFER",
            transaction_type="Transfers & Other",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-300.00")


def test_entity_filing_expense_does_not_clear_property_tax_accrual():
    rows = [
        row(-300, "AOPS-MONTHLY-ACCRUAL|taxes|Example House|2026-07|300.00"),
        row(
            -63,
            "Annual Filing Fee and Renewal Service",
            category="City, State, & Local Taxes",
            account="Example House DAO Operations",
            merchant="CORPORATE FILINGS LLC",
            transaction_type="Operating Expenses",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-300.00")


def test_dao_fee_purpose_overrides_a_coarse_tax_category():
    payment = row(
        -62.5,
        "DAO LLC Fee",
        category="City, State, & Local Taxes",
        account="Example House DAO Operations",
        merchant="ECO SYSTEMS LLC",
        transaction_type="Operating Expenses",
    )
    rows = [
        row(-62.5, "AOPS-MONTHLY-ACCRUAL|dao|Example House|2026-07|62.50"),
        row(-300, "AOPS-MONTHLY-ACCRUAL|taxes|Example House|2026-07|300.00"),
        payment,
    ]

    assert policy.actual_obligation_cash_settlement(payment) == (
        "dao",
        policy.Decimal("62.50"),
    )
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-300.00")


def test_returned_mortgage_debit_does_not_settle_the_accrual():
    rows = [
        row(
            -2875,
            "AOPS-PNL-ACCRUAL|mortgage_interest|Example House|2026-05|2875.00",
            category="Mortgage Interest Payments",
            iso_date="2026-05-28",
        ),
        row(
            -2875,
            "Mortgage Interest Payment Reversed",
            category="Mortgage Interest Payments",
            iso_date="2026-05-22",
            account="Example House DAO Operations",
            merchant="MORTGAGE LENDER",
            transaction_type="Loan Payments & Capex",
            baselane_id="200",
        ),
        row(
            2875,
            "Mortgage Interest Payment Reversed",
            category="Mortgage Interest Payments",
            iso_date="2026-05-22",
            account="Example House DAO Operations",
            merchant="MORTGAGE LENDER",
            description="Returned received ACH transaction",
            transaction_type="Loan Payments & Capex",
            baselane_id="201",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-2875.00")


def test_future_mortgage_pay_period_does_not_clear_current_accrual():
    rows = [
        row(
            -2875,
            "AOPS-PNL-ACCRUAL|mortgage_interest|Example House|2026-07|2875.00",
            category="Mortgage Interest Payments",
            iso_date="2026-07-28",
        ),
        row(
            -2875,
            "August 20 mortgage interest",
            category="Mortgage Interest Payments",
            iso_date="2026-07-15",
            account="Example House DAO Operations",
            merchant="MORTGAGE LENDER",
            transaction_type="Loan Payments & Capex",
            baselane_id="200",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-2875.00")


def test_unlabeled_mortgage_cash_clears_fifo_only_through_posting_month():
    rows = [
        row(
            -2875,
            "AOPS-PNL-ACCRUAL|mortgage_interest|Example House|2026-01|2875.00",
            category="Mortgage Interest Payments",
            iso_date="2026-01-28",
        ),
        row(
            -2875,
            "AOPS-PNL-ACCRUAL|mortgage_interest|Example House|2026-02|2875.00",
            category="Mortgage Interest Payments",
            iso_date="2026-02-28",
        ),
        row(
            -5750,
            "Mortgage catch-up payment",
            category="Mortgage Interest Payments",
            iso_date="2026-02-15",
            account="Example House DAO Operations",
            merchant="MORTGAGE LENDER",
            transaction_type="Loan Payments & Capex",
            baselane_id="200",
        ),
        row(
            -2875,
            "AOPS-PNL-ACCRUAL|mortgage_interest|Example House|2026-03|2875.00",
            category="Mortgage Interest Payments",
            iso_date="2026-03-28",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 3, 31)
    ) == policy.Decimal("-2875.00")


def test_counterparty_revenue_does_not_clear_dao_obligation():
    rows = [
        row(-62.5, "AOPS-MONTHLY-ACCRUAL|dao|Example House|2026-07|62.50"),
        row(62.5, "AOPS-MONTHLY-ACCRUAL|dao_eco|Example House|2026-07|62.50"),
    ]
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-62.50")


def test_legacy_direct_pm_marker_remains_an_open_dao_obligation():
    legacy = row(
        -500.18,
        "AOPS-PM-FEE|85-104 Alawa Pl|2026-06|500.18 | monthly PM fee",
    )
    legacy["Property"] = "85-104 Alawa Pl"

    assert policy.manual_accrual_kind(legacy) == "pm"
    assert policy.outstanding_manual_accrual_liability(
        [legacy], "85-104 Alawa Pl", date(2026, 7, 31)
    ) == policy.Decimal("-500.18")


def test_legacy_and_paired_pm_rows_roll_forward_without_counterparty_netting():
    legacy = row(
        -500.18,
        "AOPS-PM-FEE|85-104 Alawa Pl|2026-06|500.18 | monthly PM fee",
    )
    dao = row(
        -962.67,
        "AOPS-PM-FEE|pm_dao|85-104 Alawa Pl|2026-07|962.67 | monthly PM fee",
    )
    eco = row(
        962.67,
        "AOPS-PM-FEE|pm_eco|85-104 Alawa Pl|2026-07|962.67 | reciprocal ECO revenue",
    )
    for candidate in (legacy, dao, eco):
        candidate["Property"] = "85-104 Alawa Pl"

    assert policy.outstanding_manual_accrual_liability(
        [legacy, dao, eco], "85-104 Alawa Pl", date(2026, 7, 31)
    ) == policy.Decimal("-1462.85")


def test_accounting_only_mortgage_schedule_is_not_an_open_cash_obligation():
    schedule = row(
        -1206.23,
        "AOPS-84-MORTGAGE-PMT|principal|84 Madison Ave|2026-07 | "
        "Accounting-only schedule entry; no Baselane bank transfer represented.",
        category="Mortgage Principal Payments",
    )
    schedule["Property"] = "84 Madison Ave"

    assert policy.outstanding_manual_accrual_liability(
        [schedule], "84 Madison Ave", date(2026, 7, 31)
    ) == policy.Decimal("0.00")


def test_negative_accrual_clearing_cash_row_settles_matching_legal_period():
    rows = [
        row(-300, "AOPS-PNL-ACCRUAL|legal|Example House|2026-07|300.00"),
        row(
            -100,
            "AOPS-ACCRUAL-CLEARING|legal|Example House|2026-07|tx:123 | "
            "Actual legal cash payment charged against standard legal accrual.",
            category="Legal Fees",
            account="Example House DAO Operations",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-200.00")


def test_standard_legal_cash_clears_prior_accruals_fifo():
    rows = [
        row(
            -62.5,
            "AOPS-PNL-ACCRUAL|legal|Example House|2026-01|62.50",
            category="Legal Fees",
            iso_date="2026-01-28",
        ),
        row(
            -62.5,
            "AOPS-PNL-ACCRUAL|legal|Example House|2026-02|62.50",
            category="Legal Fees",
            iso_date="2026-02-28",
        ),
        row(
            -125,
            "Annual registered-agent invoice",
            category="Legal & Other Professional Fees",
            iso_date="2026-02-15",
            account="Example House DAO Operations",
            merchant="REGISTERED AGENT",
            transaction_type="Operating Expenses",
            baselane_id="200",
        ),
        row(
            -62.5,
            "AOPS-PNL-ACCRUAL|legal|Example House|2026-03|62.50",
            category="Legal Fees",
            iso_date="2026-03-28",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 3, 31)
    ) == policy.Decimal("-62.50")


def test_operating_expense_pm_payment_from_dao_to_eco_clears_pay_period():
    payment = row(
        -500,
        "July PM Fees 25 percent",
        iso_date="2026-07-31",
        account="Example House DAO Operations",
        merchant="ECO Systems, LLC | INTERNAL_TRANSFER",
        description="ECO Systems, LLC | INTERNAL_TRANSFER",
        transaction_type="Operating Expenses",
    )
    rows = [
        row(-500, "AOPS-PNL-ACCRUAL|pm|Example House|2026-07|500.00"),
        payment,
    ]

    assert policy.pm_cash_settlement_amount(payment) == policy.Decimal("500.00")
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("0.00")


def test_eco_to_dao_pm_refund_does_not_clear_the_dao_obligation():
    refund = row(
        -500,
        "July PM fee overpayment return",
        iso_date="2026-07-31",
        account="ECO Systems, LLC-ECO Systems Operations",
        merchant="Example House DAO LLC | INTERNAL_TRANSFER",
        description="Example House DAO LLC | INTERNAL_TRANSFER",
        transaction_type="Transfers & Other",
        category="Transfers Between Accounts",
    )
    rows = [
        row(-500, "AOPS-PNL-ACCRUAL|pm|Example House|2026-07|500.00"),
        refund,
    ]

    assert policy.pm_cash_settlement_amount(refund) == policy.Decimal("0.00")
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-500.00")


def test_itemized_composite_transfer_applies_only_its_pm_component():
    payment = row(
        -1197.99,
        "84 Madison settlement through June 2026",
        iso_date="2026-07-24",
        account="Example House DAO Operations",
        merchant="84-ECO | Mar26 net | Cleaning 60; PM 796.09; Repairs 845",
        description="ECO Systems, LLC | INTERNAL_TRANSFER",
        transaction_type="Transfers & Other",
        category="Transfers Between Accounts",
    )
    rows = [
        row(
            -796.09,
            "AOPS-PNL-ACCRUAL|pm|Example House|2026-03|796.09",
            iso_date="2026-03-28",
        ),
        payment,
    ]

    assert policy.pm_cash_settlement_amount(payment) == policy.Decimal("796.09")
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("0.00")


def test_incidental_pm_text_does_not_reclassify_escrow_refund_as_pm_cash():
    refund = row(
        -500,
        "Mortgage and escrow reconciliation; PM fee discussed in the audit memo.",
        iso_date="2026-07-27",
        account="Example House DAO Operations",
        merchant="88-ECO | escrow overfund refund | 2026-06",
        description="ECO Systems, LLC | INTERNAL_TRANSFER",
        transaction_type="Transfers & Other",
        category="Transfers Between Accounts",
    )
    rows = [
        row(-500, "AOPS-PNL-ACCRUAL|pm|Example House|2026-06|500.00"),
        refund,
    ]

    assert policy.pm_cash_settlement_amount(refund) == policy.Decimal("0.00")
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-500.00")


def test_composite_note_does_not_reclassify_a_dao_fee_cash_row_as_pm():
    payment = row(
        -62.50,
        "Unpaid PM accruals for Jan $298.15, plus Jan DAO LLC fee $62.50.",
        iso_date="2026-07-27",
        account="Example House DAO Operations",
        merchant="Example-ECO | DAO LLC fee cash | 2026-01",
        description="ECO Systems, LLC | INTERNAL_TRANSFER",
        category="Transfers Between Accounts",
        transaction_type="Transfers & Other",
    )

    assert policy.pm_cash_settlement_amount(payment) == policy.Decimal("0.00")


def test_explicit_pm_anchor_rolls_forward_only_later_pay_periods():
    anchor_note = (
        "88 Madison: mortgage net thru Jul26 $1,887.25; PM cash thru Jun26 "
        "$3,563.72. Unpaid PM accrual $7,191.62 remains."
    )
    rows = [
        row(
            -10000,
            "AOPS-PNL-ACCRUAL|pm|Example House|2026-06|10000.00",
            iso_date="2026-06-28",
            baselane_id="100",
        ),
        row(
            -1082.78,
            anchor_note,
            iso_date="2026-07-27",
            account="Example House DAO Operations",
            merchant="88-ECO | P&I duplicate less unpaid P&I | net",
            description="ECO Systems, LLC | INTERNAL_TRANSFER",
            category="Transfers Between Accounts",
            transaction_type="Transfers & Other",
            baselane_id="910",
        ),
        row(
            -348.22,
            anchor_note,
            iso_date="2026-07-27",
            account="Example House DAO Operations",
            merchant="88-ECO | PM cash | 2026-04 | partial",
            description="ECO Systems, LLC | INTERNAL_TRANSFER",
            category="Transfers Between Accounts",
            transaction_type="Transfers & Other",
            baselane_id="917",
        ),
        row(
            -3291.24,
            "AOPS-PNL-ACCRUAL|pm_dao|Example House|2026-07|3291.24",
            baselane_id="1000",
        ),
        row(
            -3254.25,
            "July 2026 PM cash settlement",
            iso_date="2026-07-31",
            account="Example House DAO Operations",
            merchant="ECO Systems, LLC | INTERNAL_TRANSFER",
            description="ECO Systems, LLC | INTERNAL_TRANSFER",
            category="Transfers Between Accounts",
            transaction_type="Transfers & Other",
            baselane_id="1001",
        ),
        row(
            -36.99,
            "July 2026 PM fee true up",
            iso_date="2026-07-31",
            account="Example House DAO Operations",
            merchant="ECO Systems, LLC | INTERNAL_TRANSFER",
            description="ECO Systems, LLC | INTERNAL_TRANSFER",
            category="Transfers Between Accounts",
            transaction_type="Transfers & Other",
            baselane_id="1002",
        ),
    ]

    expected = policy.Decimal("-7191.62")
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == expected
    assert policy.outstanding_manual_accrual_liability(
        list(reversed(rows)), "Example House", date(2026, 7, 31)
    ) == expected


def test_zero_pm_anchor_then_july_partial_payment_leaves_july_remainder():
    rows = [
        row(
            -9658.30,
            "AOPS-PNL-ACCRUAL|pm|Example House|2026-06|9658.30",
            iso_date="2026-06-28",
            baselane_id="100",
        ),
        row(
            -4895.31,
            "90 Madison: PM thru Jun26 $9,658.30 - ECO mortgage thru Jul26 "
            "$4,762.99 = $4,895.31.",
            iso_date="2026-07-27",
            account="Example House DAO Operations",
            merchant="90-ECO | PM cash | 2026-06",
            description="ECO Systems, LLC | INTERNAL_TRANSFER",
            category="Transfers Between Accounts",
            transaction_type="Transfers & Other",
            baselane_id="200",
        ),
        row(
            -1929.29,
            "AOPS-PNL-ACCRUAL|pm_dao|Example House|2026-07|1929.29",
            baselane_id="300",
        ),
        row(
            -1000,
            "July PM Fees 25 Percent Partial Payment 1 of 2",
            iso_date="2026-07-31",
            account="Example House DAO Operations",
            merchant="ECO Systems, LLC | INTERNAL_TRANSFER",
            description="ECO Systems, LLC | INTERNAL_TRANSFER",
            category="Transfers Between Accounts",
            transaction_type="Transfers & Other",
            baselane_id="400",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-929.29")


def test_pay_period_overpayment_does_not_spill_into_a_later_month():
    rows = [
        row(
            -548.66,
            "AOPS-PNL-ACCRUAL|pm|Example House|2026-05|548.66",
            iso_date="2026-05-28",
        ),
        row(
            -721.32,
            "May 2026 PM cash",
            iso_date="2026-07-24",
            account="Example House DAO Operations",
            merchant="84-ECO | May26 | PM 721.32",
            description="ECO Systems, LLC | INTERNAL_TRANSFER",
            category="Transfers Between Accounts",
            transaction_type="Transfers & Other",
        ),
        row(-1066.72, "AOPS-PNL-ACCRUAL|pm|Example House|2026-07|1066.72"),
        row(
            -1066.65,
            "July PM Fees 15 percent",
            iso_date="2026-07-31",
            account="Example House DAO Operations",
            merchant="ECO Systems, LLC | INTERNAL_TRANSFER",
            description="ECO Systems, LLC | INTERNAL_TRANSFER",
            category="Property Management",
            transaction_type="Operating Expenses",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-0.07")
    assert policy.pm_pay_period_cash_credit(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("172.66")


def test_matching_aops_pm_settlement_does_not_duplicate_actual_cash():
    rows = [
        row(
            -500,
            "AOPS-PNL-ACCRUAL|pm|Example House|2026-06|500.00",
            iso_date="2026-06-28",
            baselane_id="100",
        ),
        row(
            -500,
            "June 2026 PM cash",
            iso_date="2026-07-01",
            account="Example House DAO Operations",
            merchant="ECO Systems, LLC | INTERNAL_TRANSFER",
            description="ECO Systems, LLC | INTERNAL_TRANSFER",
            category="Transfers Between Accounts",
            transaction_type="Transfers & Other",
            baselane_id="200",
        ),
        row(
            500,
            "AOPS-9CC-RECON|pm_settlement_cash|Example House|2026-07|500.00 | "
            "Offsets cash paid to ECO against cumulative PM fee accruals; "
            "no bank transfer.",
            iso_date="2026-07-01",
            baselane_id="300",
        ),
        row(
            -400,
            "AOPS-PNL-ACCRUAL|pm|Example House|2026-07|400.00",
            iso_date="2026-07-28",
            baselane_id="400",
        ),
    ]

    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("-400.00")
    assert policy.pm_pay_period_cash_credit(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("0.00")


def test_pay_period_parsing_does_not_treat_day_range_as_two_digit_year():
    posted = date(2026, 1, 2)

    assert policy.parse_pay_period("PM Fee Dec 20-21 2025", posted) == "2025-12"
    assert policy.pay_period_range_end("PM Fees Nov-Dec 2025", posted) == "2025-12"


def test_future_stay_range_uses_the_cash_deposit_accrual_month():
    payment = row(
        -3750,
        "ECO Systems Half of 20 percent PM Fee for April deposit for Aug-Nov Stay",
        iso_date="2026-07-31",
        account="Example House DAO Operations",
        merchant="ECO Systems, LLC | INTERNAL_TRANSFER",
        description="ECO Systems, LLC | INTERNAL_TRANSFER",
        category="Transfers Between Accounts",
        transaction_type="Transfers & Other",
    )

    assert policy.pm_cash_settlement_allocation(payment) == ("2026-04", "2026-04")


def test_804_pm_and_nathaniel_advance_markers_restrict_cash_without_double_counting_counterparts():
    rows = [
        row(
            -416,
            "AOPS-804-PM-FEE|pm_dao|804 S Quitman St|2025-06 | DAO PM expense",
        ),
        row(
            416,
            "AOPS-804-PM-FEE|pm_eco|804 S Quitman St|2025-06 | ECO PM revenue",
        ),
        row(
            -295.87,
            "AOPS-804-NATHANIEL-MORTGAGE-ADVANCE|principal|804 S Quitman St|2025-12",
            category="Mortgage Principal Payments",
        ),
        row(
            -2088.04,
            "AOPS-804-NATHANIEL-MORTGAGE-ADVANCE|interest|804 S Quitman St|2025-12",
            category="Mortgage Interest",
        ),
        row(
            -144.48,
            "AOPS-804-NATHANIEL-MORTGAGE-ADVANCE|tax_escrow|804 S Quitman St|2025-12",
            category="Property Taxes",
        ),
        row(
            -153.47,
            "AOPS-804-NATHANIEL-MORTGAGE-ADVANCE|insurance_escrow|804 S Quitman St|2025-12",
            category="Insurance",
        ),
        row(
            -18.14,
            "AOPS-804-NATHANIEL-MORTGAGE-ADVANCE|general_escrow|804 S Quitman St|2025-12",
            category="Retained Capital",
        ),
        row(
            2700,
            "AOPS-804-NATHANIEL-MORTGAGE-ADVANCE|payable|804 S Quitman St|2025-12",
            category="Owner Contributions/Distributions",
        ),
    ]
    for candidate in rows:
        candidate["Property"] = "804 S Quitman St"

    assert policy.outstanding_manual_accrual_liability(
        rows, "804 S Quitman St", date(2026, 7, 31)
    ) == policy.Decimal("-3116.00")


def test_zero_value_void_marker_uses_encoded_accrual_amount():
    rows = [
        row(-918, "AOPS-MONTHLY-ACCRUAL|insurance|Example House|2026-06|918.00"),
        row(0, "AOPS-RECON|void_insurance_accrual|Example House|2026-06|918.00 | paid"),
    ]
    assert policy.outstanding_manual_accrual_liability(
        rows, "Example House", date(2026, 7, 31)
    ) == policy.Decimal("0.00")


def test_combined_reserve_counts_lofty_or_toward_floor():
    position = policy.combined_reserve_position("2500", "1000", "3000")

    assert position["combined_reserve_liquidity"] == policy.Decimal("3500.00")
    assert position["combined_surplus_above_floor"] == policy.Decimal("500.00")
    assert position["combined_shortfall_to_floor"] == policy.Decimal("0.00")
    assert position["sendable_eco_cash"] == policy.Decimal("500.00")


def test_combined_reserve_send_is_capped_by_eco_cash():
    position = policy.combined_reserve_position("1000", "4000", "3000")

    assert position["combined_reserve_liquidity"] == policy.Decimal("5000.00")
    assert position["combined_surplus_above_floor"] == policy.Decimal("2000.00")
    assert position["sendable_eco_cash"] == policy.Decimal("1000.00")


def test_combined_reserve_reports_shortfall_without_negative_transfer():
    position = policy.combined_reserve_position("-500", "2500", "3000")

    assert position["combined_reserve_liquidity"] == policy.Decimal("2000.00")
    assert position["combined_shortfall_to_floor"] == policy.Decimal("1000.00")
    assert position["sendable_eco_cash"] == policy.Decimal("0.00")
