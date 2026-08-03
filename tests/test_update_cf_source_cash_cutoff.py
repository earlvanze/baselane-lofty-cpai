import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_cf_statements.py"


def load_module():
    spec = importlib.util.spec_from_file_location("update_cf_source_cash_cutoff", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def transaction(day: str, amount: float, notes: str = ""):
    return {
        "_date": datetime.fromisoformat(day),
        "_amount": amount,
        "Notes": notes,
    }


def test_month_end_balance_excludes_next_month_transaction():
    module = load_module()
    rows = [
        transaction("2026-07-31", 100.00, "AOPS-PNL-ACCRUAL|synthetic|property|2026-07|100.00"),
        transaction("2026-08-01", -70.99, "LawnStarter"),
    ]

    closed = module.source_cash_balance_for_month(rows, 2026, 7, "as_of_month_end")
    current = module.source_cash_balance_for_month(rows, 2026, 7, "full_column_e")

    assert closed["expected"] == 100.00
    assert closed["included_count"] == 1
    assert closed["as_of_date"] == "2026-07-31"
    assert current["expected"] == 29.01
    assert current["included_count"] == 2


def test_operational_cutoff_retains_target_month_aops_rows_only():
    module = load_module()
    rows = [
        transaction("2026-07-30", 10.00, "ordinary"),
        transaction("2026-07-31", 20.00, "ordinary"),
        transaction("2026-07-31", 30.00, "AOPS-PNL-ACCRUAL|synthetic|property|2026-07|30.00"),
        transaction("2026-08-01", 40.00, "AOPS-PNL-ACCRUAL|synthetic|property|2026-08|40.00"),
    ]

    filtered = module.filter_through_date(
        rows,
        date(2026, 7, 30),
        accounting_month=(2026, 7),
    )

    assert [row["_amount"] for row in filtered] == [10.00, 30.00]
