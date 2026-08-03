import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lofty_monthly_approve_safe_candidates import candidate_text_issues
from lofty_monthly_review_candidate_packet import (
    curr_maintenance_reserve_reporting_override,
    has_verified_financial_summary,
    ledger_cash_flow_snapshot,
    monthly_financial_summary,
    render_financials_cash_position,
    render_monthly_financial_summary,
    render_owner_update_key_items,
    render_source_backed_financial_snapshot,
)
from lofty_monthly_review_safety_scan import scan_text
from lofty_financial_patch_readiness import distribution_guard_preview
from lofty_monthly_publish_to_pm import eco_operating_cash_for_distribution
from lofty_capture_live_financial_guards import cash_source_distribution_guard_sources


def test_rendered_cash_position_omits_internal_full_ledger_control() -> None:
    text = render_financials_cash_position(
        {
            "property_name": "90 Madison Ave",
            "reporting_cutoff_date": "2026-07-31",
            "lofty_curr_maintenance_reserve": 1000,
            "eco_held_cash_gross": 3000,
            "open_accrued_obligations": 500,
            "open_accrued_obligations_by_kind": {"pm": 300, "legal": 100, "dao": 100},
            "open_accrued_obligations_status": "ok",
            "eco_held_unrestricted_cash": 2500,
            "eco_held_unrestricted_cash_status": "ok",
            "total_dao_spendable_cash": 4500,
            "total_dao_spendable_cash_status": "ok",
            "dao_accounts_payable_to_eco": 750,
            "eco_accounts_receivable_from_dao": 750,
            "intercompany_payable_status": "ok",
            "eco_gl_column_e_sum": 12294.38,
            "eco_gl_column_e_status": "ok",
            "eco_gl_column_e_row_count": 12,
            "physical_bank_cash": 2000,
            "physical_bank_cash_status": "ok",
            "physical_bank_cash_as_of_date": "2026-07-31",
            "dao_accounts_payable_by_counterparty": [
                {
                    "counterparty": "ECO Systems LLC",
                    "category": "pm",
                    "amount": 300,
                    "cash_effect": "included_in_recorded_unpaid_obligations",
                },
                {
                    "counterparty": "Legal vendor",
                    "category": "legal",
                    "amount": 100,
                    "cash_effect": "included_in_recorded_unpaid_obligations",
                },
                {
                    "counterparty": "ECO Systems LLC",
                    "category": "cash_advances",
                    "amount": 750,
                    "cash_effect": "balance_sheet_only_do_not_reduce_cash_again",
                },
            ],
            "dao_accounts_receivable_by_counterparty": [],
        },
        "2026-07",
    )

    assert "Reserve-adjusted operating position" not in text
    assert "- ECO Net DAO Funds (spendable cash held by ECO): $2,500.00" in text
    assert "- Recorded unpaid obligations: $500.00" in text
    assert "  - Due to ECO Systems LLC — PM fee: $300.00" in text
    assert "  - Due to Legal vendor — Legal fees: $100.00" in text
    assert "- DAO A/P — Due to ECO Systems LLC (cash advances): $750.00" in text
    assert "ECO A/R" not in text
    assert "Column E" not in text
    assert "Full property-ledger accounting position" not in text
    assert "- Baselane bank cash: $2,000.00" in text
    assert "shown separately" in text
    assert (
        "If anything looks wrong, please DM @earlvanze on Discord or email "
        "ecosystemspm@gmail.com."
    ) in text
    assert candidate_text_issues(text, "financial") == []


def test_90_madison_balance_sheet_snapshot_matches_july_31_authority() -> None:
    text = render_financials_cash_position(
        {
            "property_name": "90 Madison Ave",
            "as_of_month": "2026-07",
            "reporting_cutoff_date": "2026-07-31",
            "physical_bank_cash": 633.96,
            "physical_bank_cash_status": "ok",
            "physical_bank_cash_as_of_date": "2026-07-31",
            "eco_held_cash_gross": 0,
            "eco_held_unrestricted_cash": 0,
            "eco_held_unrestricted_cash_status": "ok",
            "lofty_curr_maintenance_reserve": 1946.77,
            "total_dao_spendable_cash": 0,
            "total_dao_spendable_cash_status": "ok",
            "open_accrued_obligations": 1741.79,
            "open_accrued_obligations_by_kind": {
                "pm": 929.29,
                "legal": 687.50,
                "dao": 125,
            },
            "open_accrued_obligations_status": "ok",
            "dao_accounts_payable_to_eco": 0,
            "eco_accounts_receivable_from_dao": 0,
            "intercompany_payable_status": "reconciliation_pending",
            "intercompany_source_mode": "id_bearing_eco_account_activity_trace",
        },
        "2026-07",
    )

    for expected in (
        "Actual July 31 position:",
        "- Baselane bank cash: $633.96",
        "- ECO-held 90 Madison Ave cash: $0.00",
        "- Lofty operating-reserve ledger: $1,946.77 — cash held separately by Lofty, not by ECO.",
        "- Recorded unpaid obligations: $1,741.79",
        "  - Counterparty pending — PM fee: $929.29",
        "  - Counterparty pending — Legal fees: $687.50",
        "  - Counterparty pending — DAO fees: $125.00",
    ):
        assert expected in text
    assert "DAO A/P - Due to ECO" not in text
    assert "ECO A/R - Due from DAO" not in text
    assert "Reserve-adjusted operating position" not in text
    assert candidate_text_issues(text, "financial") == []


def test_negative_lofty_reserve_is_shown_once_as_lofty_payable() -> None:
    text = render_financials_cash_position(
        {
            "property_name": "25 Circle Dr",
            "as_of_month": "2026-07",
            "reporting_cutoff_date": "2026-07-31",
            "physical_bank_cash": 718.93,
            "physical_bank_cash_status": "ok",
            "physical_bank_cash_as_of_date": "2026-07-31",
            "eco_held_unrestricted_cash": 0,
            "eco_held_unrestricted_cash_status": "ok",
            "lofty_curr_maintenance_reserve": -7482.71,
            "total_dao_spendable_cash": 718.93,
            "total_dao_spendable_cash_status": "ok",
            "open_accrued_obligations": 0,
            "open_accrued_obligations_by_kind": {},
            "open_accrued_obligations_status": "ok",
        },
        "2026-07",
    )

    assert (
        "- Spendable Baselane/ECO cash after recorded obligations (before Lofty OR): $718.93"
        in text
    )
    assert "Separate from spendable cash:" in text
    assert (
        "- Lofty operating-reserve ledger: -$7,482.71 — a reserve deficit, not cash owed to ECO."
        in text
    )
    assert "  - DAO A/P — Due to Lofty operating reserve: $7,482.71" in text
    assert "Reserve-adjusted operating position" not in text
    assert "Accessible DAO funds" not in text
    assert candidate_text_issues(text, "financial") == []


def _verified_summary(**overrides) -> dict:
    summary = {
        "eco_gl_column_e_status": "ok",
        "eco_gl_column_e_as_of_date": "2026-07-31",
        "eco_held_unrestricted_cash_status": "ok",
        "eco_operating_cash_as_of_date": "2026-07-31",
        "physical_bank_cash_as_of_date": "2026-07-31",
        "reporting_cutoff_date": "2026-07-31",
        "open_accrued_obligations_status": "ok",
        "open_accrued_obligations": 0,
        "open_accrued_obligations_by_kind": {},
        "dao_accounts_payable_by_counterparty": [],
        "dao_accounts_receivable_by_counterparty": [],
        "counterparty_balances_status": "ok",
        "eco_held_unrestricted_cash": 0,
        "dao_accounts_payable_to_eco": 0,
        "eco_accounts_receivable_from_dao": 0,
        "intercompany_payable_status": "ok_no_open_position",
        "intercompany_source_mode": "id_bearing_eco_account_activity_trace",
    }
    summary.update(overrides)
    return summary


def test_zero_historical_intercompany_trace_does_not_block_verified_cash() -> None:
    summary = _verified_summary(intercompany_payable_status="reconciliation_pending")

    assert has_verified_financial_summary(summary) is True


def test_same_month_stale_cash_authority_blocks_july_31_summary() -> None:
    summary = _verified_summary(
        physical_bank_cash_as_of_date="2026-07-14",
        eco_operating_cash_as_of_date="2026-07-14",
    )

    assert has_verified_financial_summary(summary) is False


def test_verified_id_bearing_payable_is_publishable() -> None:
    summary = _verified_summary(
        intercompany_payable_status="verified_payable_from_id_bearing_cash_rollforward",
        intercompany_source_mode="id_bearing_eco_account_intercompany_rollforward",
        dao_accounts_payable_to_eco=1118.39,
        eco_accounts_receivable_from_dao=1118.39,
    )

    assert has_verified_financial_summary(summary) is True


def test_verified_payable_status_requires_exact_cash_rollforward_source() -> None:
    summary = _verified_summary(
        intercompany_payable_status="verified_payable_from_id_bearing_cash_rollforward",
        intercompany_source_mode="property_tag_diagnostic",
        dao_accounts_payable_to_eco=1118.39,
        eco_accounts_receivable_from_dao=1118.39,
    )

    assert has_verified_financial_summary(summary) is False


def test_positive_rollforward_verifies_zero_payable_without_creating_custody() -> None:
    summary = _verified_summary(
        intercompany_payable_status="positive_activity_requires_custody_reconciliation",
        intercompany_source_mode="id_bearing_eco_account_intercompany_rollforward",
        dao_accounts_payable_to_eco=0,
        eco_accounts_receivable_from_dao=0,
        eco_held_cash_gross=0,
    )

    assert has_verified_financial_summary(summary) is True


def test_pending_intercompany_trace_cannot_publish_a_nonzero_debt() -> None:
    summary = _verified_summary(
        intercompany_payable_status="reconciliation_pending",
        dao_accounts_payable_to_eco=125,
        eco_accounts_receivable_from_dao=125,
    )

    assert has_verified_financial_summary(summary) is False


def test_intercompany_position_must_be_reciprocal() -> None:
    summary = _verified_summary(
        intercompany_payable_status="ok",
        dao_accounts_payable_to_eco=125,
        eco_accounts_receivable_from_dao=0,
    )

    assert has_verified_financial_summary(summary) is False


def test_unknown_mortgage_escrow_is_omitted_without_blocking_verified_cash() -> None:
    text = render_financials_cash_position(
        {
            "lofty_curr_maintenance_reserve": 1000,
            "eco_held_cash_gross": 3000,
            "open_accrued_obligations": 500,
            "open_accrued_obligations_by_kind": {"pm": 500},
            "open_accrued_obligations_status": "ok",
            "eco_held_unrestricted_cash": 2500,
            "eco_held_unrestricted_cash_status": "ok",
            "total_dao_spendable_cash": 2500,
            "total_dao_spendable_cash_status": "ok",
            "physical_bank_cash_status": "property_missing",
            "mortgage_escrow_reconciliation_required": True,
            "restricted_mortgage_escrow": None,
            "restricted_mortgage_escrow_status": "reconciliation_pending",
        },
        "2026-07",
    )

    assert "Mortgage-servicer escrow" not in text
    assert "Baselane bank cash" not in text
    assert "Pending reconciliation" not in text
    assert candidate_text_issues(text, "financial") == []


def test_safety_scan_accepts_plain_english_cash_breakout(tmp_path) -> None:
    path = tmp_path / "FINANCIALS.md"
    path.write_text(
        """# Financials

## Cash Flow Snapshot (2026-07)

## Monthly Cash Position (2026-07)

Actual July 31 position:

- Lofty operating-reserve ledger: $1,000.00 - cash held separately by Lofty, not by ECO.
- ECO Net DAO Funds (spendable cash held by ECO): $2,500.00
If anything looks wrong, email ecosystemspm@gmail.com.
""",
        encoding="utf-8",
    )

    assert scan_text(path, "financials", "2026-07") == []


def test_safety_scan_still_blocks_noncanonical_email_address(tmp_path) -> None:
    path = tmp_path / "update.md"
    path.write_text(
        "- Lofty operating-reserve ledger: $1,000.00\n"
        "- ECO Net DAO Funds (spendable cash held by ECO): $2,500.00\n"
        "Contact private-owner@example.com.\n",
        encoding="utf-8",
    )

    issues = scan_text(path, "updates")

    assert any(item["class"] == "email" for item in issues)


def test_safety_scan_pending_lofty_reserve_requires_source_scoped_exception(tmp_path) -> None:
    path = tmp_path / "update.md"
    path.write_text(
        "- Lofty operating-reserve ledger: Pending reconciliation - no reserve source.\n"
        "- ECO Net DAO Funds (spendable cash held by ECO): $0.00\n",
        encoding="utf-8",
    )

    blocked = scan_text(path, "updates")
    allowed = scan_text(path, "updates", allow_pending_lofty_reserve=True)

    assert any(item["class"] == "missing_lofty_curr_maintenance_reserve" for item in blocked)
    assert allowed == []


def test_update_safety_scan_rejects_negative_legacy_eco_cash_name(tmp_path) -> None:
    path = tmp_path / "update.md"
    path.write_text(
        """## 2026-07-30

- Property Update (07/30/2026):
- Lofty-held reserve is $125.00.
- Spendable cash ECO owes this DAO (ECO Net DAO Funds): -$13,105.08
""",
        encoding="utf-8",
    )

    issues = scan_text(path, "updates")
    assert any(item["class"] == "missing_eco_net_dao_funds" for item in issues)


def test_str_snapshot_uses_actual_cash_flow_without_recurring_metrics(tmp_path) -> None:
    ledger = tmp_path / "ECO Systems General Ledger - 86 Madison Ave.csv"
    ledger.write_text(
        "Date,Merchant,Amount,Type,Category,Sub-category,Property,Notes\n"
        "2026-07-01,Airbnb,9491.26,Transaction,Revenue,Short Term Rents,86 Madison Ave,\n"
        "2026-07-15,July operating expenses,-7600.84,Manual,Operating Expenses,Repairs,86 Madison Ave,\n"
        "2026-07-24,Kyle Leese,-273.86,Transaction,Loan Payments & Capex,Other Loan Payments,86 Madison Ave,\n",
        encoding="utf-8",
    )
    summary = {
        "eco_gl_column_e_source": str(ledger),
        "lofty_monthly_rent": 6701.90,
        "eco_gl_column_e_row_count": 3,
    }

    snapshot = ledger_cash_flow_snapshot(summary, "2026-07")
    rendered = render_source_backed_financial_snapshot(summary, "2026-07")

    assert snapshot["has_short_term_rent_revenue"] is True
    assert snapshot["cash_flow_annualization_policy"] == "monthly_net_operating_cashflow"
    assert snapshot["noi"] == 1890.42
    assert snapshot["net_operating_cashflow"] == 1616.56
    assert "| Net Operating Cashflow | $1,616.56 |" in rendered
    assert "Scheduled Monthly Rent" not in rendered
    assert "Recurring Net Operating Cashflow" not in rendered
    owner_summary = render_owner_update_key_items(summary, "2026-07")
    assert "resulting in NOI of $1,890.42" in owner_summary
    assert "Net Operating Cashflow after these items was $1,616.56" in owner_summary
    assert "Other Expenses/Utilities" in owner_summary


def test_categoryless_short_term_split_children_are_revenue_and_disable_long_term_cap(tmp_path) -> None:
    ledger = tmp_path / "ECO Systems General Ledger - Umland.csv"
    ledger.write_text(
        "Date,Merchant,Description,Amount,Type,Category,Sub-category,Property,Notes\n"
        "2026-07-01,Booking.com,Split child,2478.72,,,,22164 Umland Cir,\n"
        "2026-07-02,Hostshare,Split child,300.00,,,,22164 Umland Cir,\n"
        "2026-07-03,Airbnb,Split child,1646.95,,,,22164 Umland Cir,\n"
        "2026-07-04,Airbnb,Split child,1790.12,,,,22164 Umland Cir,\n"
        "2026-07-05,Booking.com,Refund,-25.00,,,,22164 Umland Cir,\n",
        encoding="utf-8",
    )

    snapshot = ledger_cash_flow_snapshot(
        {"eco_gl_column_e_source": str(ledger), "lofty_monthly_rent": 1000.0},
        "2026-07",
    )

    assert snapshot["revenue"] == 6215.79
    assert snapshot["has_short_term_rent_revenue"] is True
    assert snapshot["cash_flow_annualization_policy"] == "monthly_net_operating_cashflow"


def test_month_scoped_listing_projection_override_survives_candidate_regeneration(tmp_path) -> None:
    ledger = tmp_path / "ECO Systems General Ledger - 5541 S Peoria St.csv"
    ledger.write_text(
        "Date,Merchant,Amount,Type,Category,Sub-category,Property,Notes\n"
        "2026-07-01,July rent,3584.00,Transaction,Revenue,Long Term Rents,5541 S Peoria St,\n"
        "2026-07-15,July expenses,-1621.12,Transaction,Operating Expenses,Repairs,5541 S Peoria St,\n",
        encoding="utf-8",
    )
    summary = {
        "eco_gl_column_e_source": str(ledger),
        "lofty_monthly_rent": 2490.00,
        "listing_cash_flow_projection_override": {
            "projected_annual_cash_flow": 13004.04,
            "reason": "user-confirmed listing projection",
            "evidence": "reports/approved.json",
            "approved_at": "2026-07-27",
        },
    }

    snapshot = ledger_cash_flow_snapshot(summary, "2026-07")
    rendered = render_source_backed_financial_snapshot(summary, "2026-07")

    assert snapshot["net_operating_cashflow"] == 1962.88
    assert snapshot["projected_annual_cash_flow_basis"] == 13004.04
    assert snapshot["cash_flow_annualization_policy"] == "approved_listing_projection_override"
    assert "| Projected Annual Cash Flow Basis | $13,004.04 |" in rendered
    assert "| Listing projection override reason | user-confirmed listing projection |" in rendered


def test_snapshot_excludes_eco_side_pm_accrual_from_dao_revenue(tmp_path) -> None:
    ledger = tmp_path / "ECO Systems General Ledger - 88 Madison Ave.csv"
    ledger.write_text(
        "Date,Merchant,Amount,Type,Category,Sub-category,Property,Notes\n"
        "2026-07-01,Airbnb,13164.97,Transaction,Revenue,Short Term Rents,88 Madison Ave,\n"
        "2026-07-28,ECO PM accrual,3291.24,Revenue,Fees & Other Revenue,,Mining Sales Consulting and PM,"
        "AOPS-PNL-ACCRUAL|pm_eco|88 Madison Ave|2026-07|3291.24\n"
        "2026-07-28,DAO PM accrual,-3291.24,Expense,Property Management,,88 Madison Ave,"
        "AOPS-PNL-ACCRUAL|pm|88 Madison Ave|2026-07|3291.24\n",
        encoding="utf-8",
    )
    snapshot = ledger_cash_flow_snapshot({"eco_gl_column_e_source": str(ledger)}, "2026-07")

    assert snapshot["revenue"] == 13164.97
    assert snapshot["operating_expenses"] == -3291.24
    assert snapshot["noi"] == 9873.73


def test_financials_md_is_not_a_downstream_value_source(tmp_path) -> None:
    property_dir = tmp_path / "86 Madison Ave Public"
    snapshot_dir = property_dir / "00 - README & Property Snapshot"
    statements_dir = property_dir / "07 - P&L & Owner Statements"
    snapshot_dir.mkdir(parents=True)
    statements_dir.mkdir(parents=True)
    financials = snapshot_dir / "FINANCIALS.md"
    financials.write_text(
        "| Lofty Operating Cash | $999,999.00 | Lofty `curr_maintenance_reserve` |\n"
        "| Scheduled Monthly Rent | $888,888.00 |\n",
        encoding="utf-8",
    )
    ledger = statements_dir / "ECO Systems General Ledger - 86 Madison Ave.csv"
    ledger.write_text(
        "Date,Merchant,Amount,Type,Category,Sub-category,Property,Notes\n"
        "2026-07-01,Airbnb,1000.00,Transaction,Revenue,Short Term Rents,86 Madison Ave,\n",
        encoding="utf-8",
    )

    summary = monthly_financial_summary(
        "86 Madison Ave",
        financials,
        [],
        run_month="2026-07",
    )

    assert summary["lofty_curr_maintenance_reserve"] is None
    assert summary["lofty_monthly_rent"] is None
    assert summary["eco_gl_column_e_source"] == str(ledger)


def test_colfax_july_close_uses_approved_reporting_reserve_without_hiding_live_readback(tmp_path) -> None:
    live_source = tmp_path / "lofty-live.json"
    live_source.write_text("{}\n", encoding="utf-8")
    summary = monthly_financial_summary(
        "7542 and 7656 S Colfax Ave, Chicago, IL 60649",
        tmp_path / "FINANCIALS.md",
        [
            {
                "address": "7542 and 7656 S Colfax Ave, Chicago, IL 60649",
                "curr_maintenance_reserve": 222003.0,
                "source": str(live_source),
                "source_mode": "pm_or_sdk_snapshot",
            }
        ],
        run_month="2026-07",
    )

    assert summary["lofty_curr_maintenance_reserve"] == 14325.48
    assert summary["lofty_curr_maintenance_reserve_source_mode"] == "approved_reporting_override"
    assert summary["lofty_curr_maintenance_reserve_live_snapshot"] == 222003.0
    assert summary["lofty_curr_maintenance_reserve_live_snapshot_source_file"] == str(live_source)
    assert summary["lofty_curr_maintenance_reserve_reporting_override"]["live_correction_status"] == (
        "pending_supported_backend_correction"
    )
    rendered = render_monthly_financial_summary(summary)
    assert "Lofty operating-reserve reporting value (live correction pending): $14,325.48" in rendered


def test_colfax_reporting_reserve_override_is_july_scoped() -> None:
    assert curr_maintenance_reserve_reporting_override(
        "7542 and 7656 S Colfax Ave, Chicago, IL 60649",
        "2026-07",
    )["curr_maintenance_reserve"] == 14325.48
    assert curr_maintenance_reserve_reporting_override(
        "7542 and 7656 S Colfax Ave, Chicago, IL 60649",
        "2026-08",
    ) is None


def test_distribution_guard_prefers_reconciled_spendable_cash() -> None:
    summary = {
        "lofty_curr_maintenance_reserve": 1147.19,
        "total_dao_spendable_cash": -18571.79,
        "eco_held_unrestricted_cash": -19744.68,
        "eco_gl_column_e_net_of_accruals": 5794.94,
    }

    preview = distribution_guard_preview(
        summary,
        {},
        {"cash_flow": 11573.76, "projected_annual_cash_flow": 11573.76},
        property_name="88 Madison Ave",
    )

    assert preview["requires_zero_distribution"] is True
    assert preview["combined_operating_cash"] == -17424.60
    assert "total_dao_spendable_cash" in preview["zero_distribution_sources"]
    assert eco_operating_cash_for_distribution({}, summary) == (-18571.79, "total_dao_spendable_cash")


def test_distribution_guard_uses_full_combined_three_thousand_floor() -> None:
    summary = {
        "lofty_curr_maintenance_reserve": 1500.00,
        "total_dao_spendable_cash": 1000.00,
        "eco_held_unrestricted_cash": 1000.00,
    }

    preview = distribution_guard_preview(
        summary,
        {},
        {"cash_flow": 1200.00, "projected_annual_cash_flow": 1200.00},
        property_name="88 Madison Ave",
    )

    assert preview["distribution_minimum"] == 3000.00
    assert preview["combined_operating_cash"] == 2500.00
    assert preview["requires_zero_distribution"] is True
    assert "combined_operating_cash_below_distribution_minimum" in preview["zero_distribution_sources"]


def test_verified_live_distribution_mismatch_is_corrective_ready() -> None:
    summary = {
        "lofty_curr_maintenance_reserve": 1147.19,
        "total_dao_spendable_cash": -18571.79,
        "eco_held_unrestricted_cash": -19744.68,
    }
    live_status = {
        "status": "blocked_live_distribution_mismatch",
        "live_financials_length": 371,
        "snapshot_path": "/tmp/live-FINANCIALS.md",
        "live_distribution_verify": {
            "targeted": True,
            "actual": 11573.76,
            "actual_coc": 5.98,
            "actual_projected_rental_yield": 5.98,
            "actual_is_occupied": True,
            "cash_flow_ok": False,
        },
    }

    preview = distribution_guard_preview(
        summary,
        live_status,
        {"cash_flow": 11573.76, "projected_annual_cash_flow": 11573.76},
        property_name="88 Madison Ave",
    )

    assert preview["requires_zero_distribution"] is True
    assert preview["live_distribution_mismatch"] is True
    assert preview["issues"] == []


def test_live_guard_uses_new_cash_labels_without_double_counting_accruals(tmp_path) -> None:
    financials = tmp_path / "FINANCIALS.md"
    financials.write_text(
        """- Lofty Operating Reserve: $743.09
- Baselane bank cash: $569.37
- Spendable Baselane/ECO cash after recorded obligations (before Lofty OR): $0.00
- Recorded unpaid obligations: $1,087.67
- ECO Net DAO Funds (spendable cash held by ECO): $0.00
""",
        encoding="utf-8",
    )

    sources = cash_source_distribution_guard_sources(financials, "85-104 Alawa Pl")

    assert "total_dao_spendable_cash" in sources
    assert "combined_operating_cash_below_maintenance_reserve" in sources
