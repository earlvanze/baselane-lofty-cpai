import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lofty_monthly_approve_safe_candidates import candidate_text_issues
from lofty_monthly_review_candidate_packet import (
    ledger_cash_flow_snapshot,
    monthly_financial_summary,
    render_financials_cash_position,
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
            "lofty_curr_maintenance_reserve": 1000,
            "eco_held_cash_gross": 3000,
            "open_accrued_obligations": 500,
            "open_accrued_obligations_status": "ok",
            "eco_held_unrestricted_cash": 2500,
            "eco_held_unrestricted_cash_status": "ok",
            "dao_accounts_payable_to_eco": 750,
            "intercompany_payable_status": "ok",
            "eco_gl_column_e_sum": 12294.38,
            "eco_gl_column_e_status": "ok",
            "eco_gl_column_e_row_count": 12,
            "physical_bank_cash": 2000,
            "physical_bank_cash_status": "ok",
            "physical_bank_cash_as_of_date": "2026-07-30",
        },
        "2026-07",
    )

    assert "| ECO Net DAO Funds (spendable cash held by ECO) | $2,500.00 |" in text
    assert "| Less: Accrued but Unpaid Obligations | $500.00 |" in text
    assert "| DAO A/P — Due to ECO for verified advances | $750.00 |" in text
    assert "Column E" not in text
    assert "Full property-ledger accounting position" not in text
    assert "| Cash in this DAO's own Baselane bank account | $2,000.00 |" in text
    assert "**ECO Net DAO Funds** is verified nonnegative spendable cash" in text
    assert candidate_text_issues(text, "financial") == []


def test_unknown_mortgage_escrow_is_disclosed_without_blocking_verified_cash() -> None:
    text = render_financials_cash_position(
        {
            "lofty_curr_maintenance_reserve": 1000,
            "eco_held_cash_gross": 3000,
            "open_accrued_obligations": 500,
            "open_accrued_obligations_status": "ok",
            "eco_held_unrestricted_cash": 2500,
            "eco_held_unrestricted_cash_status": "ok",
            "mortgage_escrow_reconciliation_required": True,
            "restricted_mortgage_escrow": None,
            "restricted_mortgage_escrow_status": "reconciliation_pending",
        },
        "2026-07",
    )

    assert "Balance not reported by Baselane" in text
    assert "Held separately by the servicer and excluded from spendable cash" in text
    assert "Pending reconciliation" not in text
    assert candidate_text_issues(text, "financial") == []


def test_safety_scan_accepts_plain_english_cash_breakout(tmp_path) -> None:
    path = tmp_path / "FINANCIALS.md"
    path.write_text(
        """# Financials

## Cash Flow Snapshot (2026-07)

## Monthly Cash Position (2026-07)

Only ECO Net DAO Funds is spendable ECO-held cash.

| What this number means | Amount | Explanation |
|---|---:|---|
| Lofty maintenance reserve balance | $1,000.00 | Held separately |
| ECO Net DAO Funds (spendable cash held by ECO) | $2,500.00 | Available after restrictions |
""",
        encoding="utf-8",
    )

    assert scan_text(path, "financials", "2026-07") == []


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
        """| Lofty maintenance reserve balance | $743.09 | Held separately |
| Cash in this DAO's own Baselane bank account | $569.37 | Verified |
| Recorded unpaid obligations | $1,087.67 | Reserved |
| ECO Net DAO Funds (spendable cash held by ECO) | $0.00 | Spendable |
""",
        encoding="utf-8",
    )

    sources = cash_source_distribution_guard_sources(financials, "85-104 Alawa Pl")

    assert "total_dao_spendable_cash" not in sources
    assert "combined_operating_cash_below_maintenance_reserve" in sources
