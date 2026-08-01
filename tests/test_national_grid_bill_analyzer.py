import importlib.util
import sys
from datetime import date
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/national_grid_bill_analyzer.py"
MONTHLY_CRON = Path(__file__).resolve().parents[1] / "scripts/baselane_financials_monthly_cron.sh"
SPEC = importlib.util.spec_from_file_location("national_grid_bill_analyzer", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


THRESHOLDS = {
    "charge_increase_percent": 10.0,
    "charge_increase_dollars": 25.0,
    "payment_stable_percent": 2.0,
    "payment_stable_dollars": 2.0,
    "deferred_balance_increase_dollars": 25.0,
    "supplier_rate_increase_percent": 10.0,
    "stale_after_days": 62,
}


def test_flags_fixed_payment_rising_charges_and_deferred_balance_for_apg():
    bills = [
        MODULE.Bill(
            "88 Madison Ave", "feb.pdf", "national_grid_pdf", account="4251273206",
            bill_date="2026-02-17", current_charges=500.0, payment_received=377.0,
            deferred_balance=1500.0, supplier="American Power & Gas, LLC",
            electric_supply_rate=0.20,
        ),
        MODULE.Bill(
            "88 Madison Ave", "mar.pdf", "national_grid_pdf", account="4251273206",
            bill_date="2026-03-17", current_charges=758.19, payment_received=377.0,
            deferred_balance=2138.29, supplier="American Power & Gas, LLC",
            electric_supply_rate=0.2941,
        ),
    ]

    result = MODULE.analyze_property(
        "88 Madison Ave", "NY/88 Madison Ave Public", bills, THRESHOLDS, date(2026, 4, 1)
    )

    fixed_payment = [a for a in result["anomalies"] if a["type"] == "fixed_payment_rising_charges_deferred_balance"]
    assert result["status"] == "critical"
    assert fixed_payment == [{
        "type": "fixed_payment_rising_charges_deferred_balance",
        "severity": "critical",
        "account": "4251273206",
        "bill_date": "2026-03-17",
        "supplier": "American Power & Gas, LLC",
        "payment_previous": 377.0,
        "payment_current": 377.0,
        "charges_previous": 500.0,
        "charges_current": 758.19,
        "deferred_previous": 1500.0,
        "deferred_current": 2138.29,
        "deferred_delta": 638.29,
        "apg_disputed_variable_rate_pattern": True,
    }]


def test_flags_stale_bill_evidence_without_inventing_charge_anomaly():
    bills = [MODULE.Bill("724 3rd Ave", "old.csv", "arcadia_csv", bill_date="2024-04-02", current_charges=100)]
    result = MODULE.analyze_property(
        "724 3rd Ave", "NY/724 3rd Ave Public", bills, THRESHOLDS, date(2026, 8, 1)
    )
    assert result["status"] == "review"
    assert [item["type"] for item in result["anomalies"]] == ["stale_or_missing_bill_evidence"]


def test_january_2025_scope_uses_prior_bill_as_baseline_but_excludes_older_anomalies():
    bills = [
        MODULE.Bill("84 Madison Ave", "nov.pdf", "national_grid_pdf", account="1", bill_date="2024-11-15", current_charges=50),
        MODULE.Bill("84 Madison Ave", "dec.pdf", "national_grid_pdf", account="1", bill_date="2024-12-15", current_charges=100),
        MODULE.Bill("84 Madison Ave", "jan.pdf", "national_grid_pdf", account="1", bill_date="2025-01-15", current_charges=150),
    ]
    result = MODULE.analyze_property(
        "84 Madison Ave", "NY/84 Madison Ave Public", bills, THRESHOLDS, date(2025, 2, 1), "2025-01"
    )
    increases = [item for item in result["anomalies"] if item["type"] == "current_charges_increase"]
    assert result["bill_count"] == 1
    assert [item["bill_date"] for item in increases] == ["2025-01-15"]


def test_apg_portfolio_review_masks_accounts_and_keeps_statement_timeline():
    bills = [
        MODULE.Bill("88 Madison Ave", "feb.pdf", "national_grid_pdf", account="4251273206", bill_date="2026-02-17", current_charges=500, payment_received=377, deferred_balance=1500, supplier="American Power & Gas, LLC"),
        MODULE.Bill("88 Madison Ave", "mar.pdf", "national_grid_pdf", account="4251273206", bill_date="2026-03-17", current_charges=758.19, payment_received=377, deferred_balance=2138.29, supplier="American Power & Gas, LLC"),
    ]
    prop = MODULE.analyze_property("88 Madison Ave", "NY/88 Madison Ave Public", bills, THRESHOLDS, date(2026, 4, 1), "2025-01")
    review = MODULE.build_apg_portfolio_review([prop], "2025-01")
    account = review["accounts"][0]
    assert review["account_count"] == 1
    assert review["fixed_payment_divergence_count"] == 1
    assert account["account"].startswith("acct-")
    assert "4251273206" not in account["account"]
    assert account["first_observable_fixed_payment_divergence"] == "2026-03-17"
    assert [item["bill_date"] for item in account["timeline"]] == ["2026-02-17", "2026-03-17"]


def test_monthly_close_runs_analyzer_and_passes_report_to_update_driver():
    text = MONTHLY_CRON.read_text(encoding="utf-8")
    assert 'CURRENT_STEP="national_grid_bill_analyzer"' in text
    assert 'NATIONAL_GRID_STATEMENT_AUDIT_FROM_MONTH="${NATIONAL_GRID_STATEMENT_AUDIT_FROM_MONTH:-2025-01}"' in text
    assert '"$PY" "$NATIONAL_GRID_ANALYZER_SCRIPT"' in text
    assert 'MONTHLY_LOFTY_ARGS+=(--utility-anomaly-report "$NATIONAL_GRID_ANALYZER_REPORT")' in text
    assert '"national_grid_bill_analyzer": os.environ.get("BASELANE_MONTHLY_NATIONAL_GRID_ANALYZER_STATUS")' in text
