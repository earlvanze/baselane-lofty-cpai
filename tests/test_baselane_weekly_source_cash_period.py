from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "baselane_weekly_unprocessed_pass.sh"


def test_weekly_source_cash_report_is_month_keyed_and_regenerated_for_cf_month():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "baselane_weekly_source_cash_balance_${CF_MONTH//-/_}_report.json" in text
    assert 'run_weekly_source_cash_balance_audit || WEEKLY_SOURCE_CASH_AUDIT_RC="$?"' in text
    assert '--month "$CF_MONTH"' in text
    assert '--report "$WEEKLY_SOURCE_CASH_REPORT"' in text
    assert '--source-cash-mode "$WEEKLY_SOURCE_CASH_MODE"' in text


def test_weekly_regenerates_property_ledgers_from_clean_reporting_source_before_source_cash():
    text = SCRIPT.read_text(encoding="utf-8")

    refresh = text.index('reporting_property_ledger_refresh_args=(')
    source_cash = text.index('run_weekly_source_cash_balance_audit || WEEKLY_SOURCE_CASH_AUDIT_RC="$?"')
    assert refresh < source_cash
    assert 'baselane_reporting_property_ledger_refresh.py' in text
    assert '--source "$BASELANE_CF_REPORTING_LEDGER_PATH"' in text[refresh:source_cash]
    assert 'BASELANE_WEEKLY_PROPERTY_LEDGER_APPLY="${BASELANE_WEEKLY_PROPERTY_LEDGER_APPLY:-1}"' in text
    assert 'reporting_property_ledger_refresh_args+=(--apply)' in text[refresh:source_cash]


def test_weekly_source_cash_consumers_do_not_read_shared_daily_report():
    text = SCRIPT.read_text(encoding="utf-8")

    assert text.count('--source-cash-report "$WEEKLY_SOURCE_CASH_REPORT"') == 2
    assert 'source_cash_report_path = Path(os.environ["WEEKLY_SOURCE_CASH_REPORT"])' in text
    assert 'source_cash_report_month == os.environ["CF_MONTH"]' in text
    assert 'review_reasons.append("source_cash_balance_report_not_current")' in text
    assert "baselane_daily_source_cash_balance_report.json" not in text


def test_weekly_source_cash_nonblocking_no_matches_are_diagnostic_only():
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'source_cash_report.get("blocking_no_match_count")' in text
    assert 'source_cash_report.get("blocking_no_match_properties_bounded")' in text
    assert "source_cash_balance_blocking_no_match_count=" in text
    assert 'review_reasons.append(f"source_cash_balance_no_match_count=' not in text
