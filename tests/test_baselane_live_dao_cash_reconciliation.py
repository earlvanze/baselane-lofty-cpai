import csv
import sys
from datetime import date
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import baselane_live_dao_cash_reconciliation as reconciliation


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
        row("manual-1", -900, "Repairs", Type="Manual"),
    ]

    result = reconciliation.build_eco_intercompany_subledger(rows, date(2026, 7, 31))
    item = result["86 Madison Ave"]

    assert item["eco_intercompany_net_position"] == "-550.00"
    assert item["dao_accounts_payable_to_eco"] == "550.00"
    assert item["eco_accounts_receivable_from_dao"] == "550.00"
    assert item["gross_eco_advances"] == "1550.00"
    assert item["gross_dao_cash_credits"] == "1000.00"
    assert item["included_row_count"] == 3
    assert {row["classification"] for row in item["excluded_rows"]} == {
        "eco_earned_revenue",
        "credit_card_payoff_duplicate",
        "eco_no_dao_mortgage_obligation",
        "accounting_only_or_manual_row",
    }
