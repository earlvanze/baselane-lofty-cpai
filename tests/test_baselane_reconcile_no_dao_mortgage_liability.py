import json
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from baselane_reconcile_no_dao_mortgage_liability import build_report, main  # noqa: E402


def fixture():
    config = {
        "policy": {"cash_cutoff": "2026-07-31", "basis": "test"},
        "properties": {"Test": {
            "dao_bank_account_ids": ["dao"],
            "payment_events": [{"installment_months": ["2026-07"], "parent_id": "p", "date": "2026-07-01", "components": {"principal": "40", "interest": "60", "escrow_general": "25", "eco_fee": "5"}, "source": "statement"}],
            "confirmed_reimbursements": [{"id": "r", "date": "2026-07-02", "amount": "105", "applied_to_eco_responsibility": "105", "memo": "P&I and fee"}],
            "candidate_reimbursements": [{"id": "c", "date": "2026-07-03", "amount": "20", "memo": "", "reason": "unlabeled"}],
            "other_open_dao_payables_to_eco": {"pm": "10"},
        }},
    }
    rows = {
        "p": {"id": "p", "date": "2026-07-01", "amount": -130, "bankAccountId": "dao", "splitTransactions": [
            {"amount": -40, "tagId": "20"}, {"amount": -60, "tagId": "11"},
            {"amount": -25, "tagId": "130"}, {"amount": -5, "tagId": "24"},
        ]},
        "r": {"id": "r", "date": "2026-07-02", "amount": 105, "note": {"text": "P&I and fee"}},
        "c": {"id": "c", "date": "2026-07-03", "amount": 20},
    }
    return config, rows


def test_waterfall_keeps_escrow_out_of_eco_responsibility():
    config, rows = fixture()
    report = build_report(config, "Test", rows)
    assert report["summary"]["gross_pi_due_from_eco"] == "100.00"
    assert report["summary"]["gross_lender_fees_due_from_eco"] == "5.00"
    assert report["summary"]["restricted_dao_escrow_paid"] == "25.00"
    assert report["summary"]["open_mortgage_due_from_eco"] == "0.00"
    assert report["summary"]["net_after_explicit_cross_entity_ap"] == "-10.00"


def test_unlabeled_candidate_never_reduces_confirmed_balance():
    config, rows = fixture()
    del rows["r"]
    config["properties"]["Test"]["confirmed_reimbursements"] = []
    report = build_report(config, "Test", rows)
    assert report["summary"]["open_mortgage_due_from_eco"] == "105.00"
    assert report["candidate_reimbursements"][0]["status"] == "present_unallocated"


def test_wrong_bank_fails_closed():
    config, rows = fixture()
    rows["p"]["bankAccountId"] = "eco"
    report = build_report(config, "Test", rows)
    assert report["status"] == "blocked"
    assert report["blockers"][0]["reason"] == ["unexpected_bank_account"]


def test_all_configured_builds_portfolio_report(tmp_path):
    config, rows = fixture()
    config["properties"]["Second"] = copy.deepcopy(config["properties"]["Test"])
    config_path = tmp_path / "config.json"
    rows_path = tmp_path / "rows.json"
    report_path = tmp_path / "report.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    rows_path.write_text(json.dumps(rows), encoding="utf-8")

    rc = main([
        "--all-configured", "--config", str(config_path),
        "--offline-transactions", str(rows_path), "--report", str(report_path),
    ])

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert rc == 0
    assert report["status"] == "review"
    assert report["property_count"] == 2
    assert set(report["properties"]) == {"Test", "Second"}


def test_alawa_policy_uses_full_annual_dao_admin_fee():
    root = Path(__file__).resolve().parents[1]
    config = json.loads(
        (root / "config" / "no_dao_mortgage_liability_reconciliation.json").read_text(
            encoding="utf-8"
        )
    )
    policy = config["policy"]["dao_llc_admin_fee"]
    alawa_ap = config["properties"]["85-104 Alawa Pl"]["other_open_dao_payables_to_eco"]

    assert policy["annual_charge_to_dao"] == "750.00"
    assert policy["monthly_accrual"] == "62.50"
    assert policy["annual_eco_payable_to_lofty"] == "200.00"
    assert policy["eco_filing_cost_reference"] == "125.00"
    assert alawa_ap["annual_dao_llc_admin_fee"] == "750.00"
    assert "dao_llc_fee" not in alawa_ap
