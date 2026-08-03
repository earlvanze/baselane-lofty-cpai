import csv
import json
import sys
from datetime import date
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import baselane_live_dao_cash_reconciliation as reconciliation


def test_active_gl_names_includes_roster_targets_without_bank_accounts(tmp_path, monkeypatch):
    roster = tmp_path / "active-roster.json"
    roster.write_text(
        '{"reporting_targets": ['
        '{"property_name": "1456 W 85th St"},'
        '{"property_name": "2094 W 34th Pl Cleveland"}'
        ']}',
        encoding="utf-8",
    )
    monkeypatch.setattr(reconciliation, "ACTIVE_ROSTER", roster)
    monkeypatch.setattr(reconciliation, "ACTIVE_SOURCE", tmp_path / "missing.json")

    assert reconciliation.active_gl_names() == {
        "1456 W 85th St.",
        "2094 W 34th Place",
    }


def test_active_gl_names_does_not_union_stale_transfer_preview(tmp_path, monkeypatch):
    roster = tmp_path / "active-roster.json"
    roster.write_text(
        '{"reporting_targets": [{"property_name": "88 Madison Ave"}]}',
        encoding="utf-8",
    )
    preview = tmp_path / "transfer-preview.json"
    preview.write_text(
        '{"active_dao_cash_balance_rows": ['
        '{"property": "88 Madison Ave"},'
        '{"property": "3178 W 41st St, Cleveland, OH 44109"}'
        ']}',
        encoding="utf-8",
    )
    monkeypatch.setattr(reconciliation, "ACTIVE_ROSTER", roster)
    monkeypatch.setattr(reconciliation, "ACTIVE_SOURCE", preview)

    assert reconciliation.active_gl_names() == {"88 Madison Ave"}


def test_read_gl_accepts_iso_and_long_form_dates(tmp_path):
    ledger = tmp_path / "ledger.csv"
    fieldnames = ["Date", "Property", "Amount", "Notes", "Type"]
    with ledger.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "Date": "2026-07-29",
                "Property": "88 Madison Ave",
                "Amount": "100.00",
                "Notes": "",
                "Type": "Revenue",
            }
        )
        writer.writerow(
            {
                "Date": "July 28, 2026",
                "Property": "88 Madison Ave",
                "Amount": "-25.00",
                "Notes": "",
                "Type": "Operating Expenses",
            }
        )

    full, cash_basis, counts = reconciliation.read_gl(ledger, date(2026, 7, 29))

    assert full["88 Madison Ave"] == reconciliation.Decimal("75.00")
    assert cash_basis["88 Madison Ave"] == reconciliation.Decimal("75.00")
    assert counts["88 Madison Ave"] == 2


def test_intercompany_subledger_records_verified_payable_and_excludes_eco_items():
    base = {
        "ISODate": "2026-07-15",
        "Account": "ECO Systems, LLC-2624",
        "Property": "86 Madison Ave",
        "Pending": "False",
        "Type": "Transaction",
        "Sub-category": "",
        "Merchant": "",
        "Description": "",
        "Notes": "",
    }

    def row(transaction_id, amount, category, **overrides):
        return {
            **base,
            "BaselaneId": transaction_id,
            "Amount": str(amount),
            "Category": category,
            **overrides,
        }

    rows = [
        row("rent-1", 1000, "Rental Income", Merchant="Airbnb"),
        row("repair-1", -1500, "Repairs", Merchant="Contractor"),
        row("tax-1", -50, "Property Taxes", Description="escrow property tax"),
        row("pm-1", 100, "Property Management", Notes="July PM fee"),
        row("cc-1", -200, "Credit Card Payment"),
        row("mortgage-1", -300, "Mortgage Payment", Description="mortgage interest"),
        row(
            "mortgage-return-1",
            300,
            "Transfers Between Accounts",
            Merchant="CITADEL SERV PMT",
            Notes="Returned Payment",
        ),
        row(
            "mortgage-reimbursement-1",
            500,
            "Transfers Between Accounts",
            Notes="Mortgage P&I reimbursement",
        ),
        row(
            "pm-cash-1",
            100,
            "Transfers Between Accounts",
            Merchant="86-ECO | PM cash | 2026-07",
        ),
        row(
            "interest-1",
            4.50,
            "Transfers Between Accounts",
            Notes="ECO bank interest through June 2026 86 Madison source 123",
        ),
        row("manual-1", -900, "Repairs", Type="Manual"),
    ]

    result = reconciliation.build_eco_intercompany_subledger(rows, date(2026, 7, 31))
    item = result["86 Madison Ave"]

    assert item["eco_intercompany_net_position"] == "-550.00"
    assert item["status"] == "verified_payable_from_id_bearing_cash_rollforward"
    assert item["dao_accounts_payable_to_eco"] == "550.00"
    assert item["eco_accounts_receivable_from_dao"] == "550.00"
    assert item["candidate_dao_accounts_payable_to_eco"] == "550.00"
    assert item["unreconciled_net_activity"] == "-550.00"
    assert item["gross_eco_advances"] == "1550.00"
    assert item["gross_dao_cash_credits"] == "1000.00"
    assert item["included_row_count"] == 3
    assert {row["classification"] for row in item["excluded_rows"]} == {
        "eco_earned_revenue",
        "credit_card_payoff_duplicate",
        "eco_no_dao_mortgage_obligation",
        "eco_fee_cash_settlement",
        "eco_owned_bank_interest_settlement",
        "accounting_only_or_manual_row",
    }


def test_positive_id_bearing_rollforward_does_not_create_eco_custody():
    rows = [
        {
            "ISODate": "2026-07-15",
            "Account": "ECO Systems, LLC-2624",
            "Property": "90 Madison Ave",
            "Pending": "False",
            "Type": "Transaction",
            "Sub-category": "",
            "Merchant": "Airbnb",
            "Description": "",
            "Notes": "",
            "BaselaneId": "rent-90-1",
            "Amount": "1000.00",
            "Category": "Rental Income",
        }
    ]

    item = reconciliation.build_eco_intercompany_subledger(
        rows, date(2026, 7, 31)
    )["90 Madison Ave"]

    assert item["status"] == "positive_activity_requires_custody_reconciliation"
    assert item["eco_intercompany_net_position"] == "1000.00"
    assert item["eco_held_dao_cash_before_obligations"] == "0.00"
    assert item["dao_accounts_payable_to_eco"] == "0.00"
    assert item["eco_accounts_receivable_from_dao"] == "0.00"


def test_aligned_owner_cash_settlement_is_a_dao_cash_credit():
    row = {
        "ISODate": "2026-07-15",
        "Account": "ECO Systems, LLC-2624",
        "Property": "1456 W 85th St",
        "Pending": "False",
        "Type": "Transaction",
        "BaselaneId": "aligned-owner-payment-1",
        "Amount": "2016.29",
        "Category": "Transfers Between Accounts",
        "Merchant": "Aligned Properties",
        "Description": "Owner Payment",
        "Notes": "Aligned PM cash settlement; neutral transfer after detail import",
    }

    assert reconciliation.eco_intercompany_row_classification(
        row, "1456 W 85th St"
    ) == ("include", "dao_owner_cash_received_from_aligned")


def test_exact_intercompany_override_maps_generic_property_and_fails_on_drift():
    row = {
        "ISODate": "2026-01-13",
        "Account": "ECO Systems, LLC-ECO Systems Operations-2624",
        "Property": "Mining, Sales, Consulting, and PM",
        "Pending": "False",
        "Type": "Transaction",
        "BaselaneId": "230718433",
        "Amount": "1500.00",
        "Category": "Transfers Between Accounts",
        "Merchant": "PELICAN LFTY0316 DAO LLC  |  INTERNAL_TRANSFER",
        "Notes": "For 332 Turnover to Alirio via Venmo",
    }
    rule = {
        "expected_date": "2026-01-13",
        "expected_amount": "1500.00",
        "expected_account": "ECO Systems, LLC-ECO Systems Operations-2624",
        "expected_property": "Mining, Sales, Consulting, and PM",
        "property": "326 South Alcott Street",
        "action": "include",
        "classification": "approved_property_retag_from_generic_bucket",
        "rationale": "Exact test rule.",
        "evidence_reference": "Test fixture evidence",
    }

    result = reconciliation.build_eco_intercompany_subledger(
        [row], date(2026, 7, 31), {"230718433": rule}
    )
    item = result["326 South Alcott Street"]
    assert item["gross_dao_cash_credits"] == "1500.00"
    assert item["included_rows"][0]["classification"] == (
        "approved_property_retag_from_generic_bucket"
    )

    bad_rule = {**rule, "expected_amount": "1499.99"}
    with pytest.raises(RuntimeError, match="expected_amount mismatch"):
        reconciliation.build_eco_intercompany_subledger(
            [row], date(2026, 7, 31), {"230718433": bad_rule}
        )


def test_intercompany_override_policy_is_strict_and_evidence_bearing(tmp_path):
    policy = {
        "schema_version": 1,
        "effective_date": "2026-08-03",
        "rules": [
            {
                "baselane_id": "cash-1",
                "expected_date": "2026-07-31",
                "expected_amount": "500.00",
                "expected_account": "ECO Systems, LLC-2624",
                "expected_property": "",
                "property": "326 South Alcott Street",
                "action": "include",
                "classification": "approved_property_retag_from_blank_property",
                "rationale": "Exact blank-tagged transfer mirror.",
                "evidence_baselane_ids": ["mirror-1"],
            }
        ],
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")

    loaded = reconciliation.load_intercompany_transaction_overrides(path)
    assert loaded["cash-1"]["evidence_baselane_ids"] == ["mirror-1"]

    del policy["rules"][0]["expected_property"]
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing required fields: expected_property"):
        reconciliation.load_intercompany_transaction_overrides(path)


def test_intercompany_override_requires_reciprocal_or_source_evidence(tmp_path):
    policy = {
        "schema_version": 1,
        "effective_date": "2026-08-03",
        "rules": [
            {
                "baselane_id": "cash-1",
                "expected_date": "2026-07-31",
                "expected_amount": "500.00",
                "expected_account": "ECO Systems, LLC-2624",
                "expected_property": "",
                "property": "326 South Alcott Street",
                "action": "include",
                "classification": "approved_property_retag_from_blank_property",
                "rationale": "No supporting evidence.",
            }
        ],
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(RuntimeError, match="lacks reciprocal/source evidence"):
        reconciliation.load_intercompany_transaction_overrides(path)


def test_intercompany_override_mirror_must_exist_in_source():
    row = {
        "ISODate": "2026-07-31",
        "Account": "ECO Systems, LLC-2624",
        "Property": "",
        "Pending": "False",
        "Type": "Transaction",
        "BaselaneId": "cash-1",
        "Amount": "500.00",
        "Category": "Transfers Between Accounts",
    }
    rule = {
        "expected_date": "2026-07-31",
        "expected_amount": "500.00",
        "expected_account": "ECO Systems, LLC-2624",
        "expected_property": "",
        "property": "326 South Alcott Street",
        "action": "include",
        "classification": "approved_property_retag_from_blank_property",
        "rationale": "Exact test rule.",
        "evidence_baselane_ids": ["missing-mirror"],
    }

    with pytest.raises(RuntimeError, match="evidence rows missing"):
        reconciliation.build_eco_intercompany_subledger(
            [row], date(2026, 7, 31), {"cash-1": rule}
        )


def test_counterparty_disclosures_include_eco_tax_authority_and_earldao_receivable():
    rows = [
        {
            "ISODate": "2026-07-20",
            "Property": "22164 Umland Cir",
            "Pending": "False",
            "BaselaneId": "umland-loan-1",
            "Amount": "-7000.00",
            "Category": "Loan",
            "Notes": "Loan receivable from EARLDAO",
        }
    ]

    payables, receivables = reconciliation.counterparty_balance_disclosures(
        rows,
        "22164 Umland Cir",
        date(2026, 7, 31),
        {"pm": "152.36", "taxes": "399.97"},
        reconciliation.Decimal("1118.39"),
    )

    assert payables == [
        {
            "counterparty": "ECO Systems LLC",
            "relationship": "eco",
            "category": "cash advances",
            "amount": "1118.39",
            "cash_effect": "balance_sheet_only_do_not_reduce_cash_again",
        },
        {
            "counterparty": "ECO Systems LLC",
            "relationship": "eco",
            "category": "pm",
            "amount": "152.36",
            "cash_effect": "included_in_recorded_unpaid_obligations",
        },
        {
            "counterparty": "Property-tax authority",
            "relationship": "government",
            "category": "taxes",
            "amount": "399.97",
            "cash_effect": "included_in_recorded_unpaid_obligations",
        },
    ]
    assert receivables == [
        {
            "counterparty": "EARLDAO",
            "relationship": "other_dao",
            "category": "cash advance or loan receivable",
            "amount": "7000.00",
            "cash_effect": "balance_sheet_asset_not_spendable_cash",
        }
    ]


def test_spendable_cash_is_physical_cash_net_of_security_and_obligations():
    result = reconciliation.spendable_cash_position(
        dao_bank_total=reconciliation.Decimal("633.96"),
        documented_security_principal=reconciliation.Decimal("0.00"),
        eco_held_cash_gross=reconciliation.Decimal("0.00"),
        open_accrued_obligations=reconciliation.Decimal("812.50"),
        other_cash_restrictions=reconciliation.Decimal("0.00"),
    )

    assert result["dao_bank_spendable_before_obligations"] == reconciliation.Decimal("633.96")
    assert result["total_dao_spendable_cash"] == reconciliation.Decimal("0.00")
    assert result["cash_reconciliation_deficit"] == reconciliation.Decimal("178.54")


def test_security_principal_and_obligations_are_each_subtracted_once():
    result = reconciliation.spendable_cash_position(
        dao_bank_total=reconciliation.Decimal("2000.00"),
        documented_security_principal=reconciliation.Decimal("750.00"),
        eco_held_cash_gross=reconciliation.Decimal("100.00"),
        open_accrued_obligations=reconciliation.Decimal("100.00"),
        other_cash_restrictions=reconciliation.Decimal("50.00"),
    )

    assert result["gross_available_before_obligations"] == reconciliation.Decimal("1350.00")
    assert result["total_cash_restrictions"] == reconciliation.Decimal("150.00")
    assert result["total_dao_spendable_cash"] == reconciliation.Decimal("1200.00")


def test_eco_net_dao_funds_excludes_dedicated_dao_bank_cash():
    result = reconciliation.spendable_cash_position(
        dao_bank_total=reconciliation.Decimal("2000.00"),
        documented_security_principal=reconciliation.Decimal("0.00"),
        eco_held_cash_gross=reconciliation.Decimal("100.00"),
        open_accrued_obligations=reconciliation.Decimal("150.00"),
        other_cash_restrictions=reconciliation.Decimal("0.00"),
    )

    eco_net_dao_funds = min(
        reconciliation.Decimal("100.00"),
        result["total_dao_spendable_cash"],
    )

    assert result["total_dao_spendable_cash"] == reconciliation.Decimal("1950.00")
    assert eco_net_dao_funds == reconciliation.Decimal("100.00")


def test_open_accrued_obligation_position_exposes_components(monkeypatch):
    monkeypatch.setattr(
        reconciliation,
        "outstanding_manual_accrual_liability_by_kind",
        lambda rows, property_name, cutoff: {
            "taxes": reconciliation.Decimal("103.75"),
            "pm": reconciliation.Decimal("8.50"),
        },
    )

    total, by_kind = reconciliation.open_accrued_obligation_position(
        [],
        "Example House",
        date(2026, 7, 31),
    )

    assert total == reconciliation.Decimal("112.25")
    assert by_kind == {"pm": "8.50", "taxes": "103.75"}
