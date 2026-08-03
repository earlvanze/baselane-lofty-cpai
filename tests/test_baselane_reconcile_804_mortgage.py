import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from baselane_reconcile_804_mortgage import (  # noqa: E402
    NATHANIEL_ADVANCE_MONTHS,
    components,
    manual_targets,
    month_range,
    schedule,
)


def test_schedule_starts_with_first_post_refinance_installment() -> None:
    assert month_range()[0] == "2025-11"
    assert "2025-09" not in schedule()
    assert "2025-10" not in schedule()
    assert schedule()["2025-11"]["principal"] == Decimal("294.15")
    assert schedule()["2025-11"]["interest"] == Decimal("2089.76")


def test_every_month_decomposes_to_observed_2700_payment() -> None:
    for month in month_range():
        rows = components(month, negative=True, tx_date=f"{month}-01")
        assert sum(Decimal(row["amount"]) for row in rows) == Decimal("-2700.00")


def test_only_missing_cash_months_create_balanced_nathaniel_accruals() -> None:
    targets = manual_targets()
    assert len(targets) == 6 * len(NATHANIEL_ADVANCE_MONTHS)
    for month in NATHANIEL_ADVANCE_MONTHS:
        month_rows = [row for row in targets if row["marker"].endswith(f"|{month}")]
        assert sum(Decimal(row["amount"]) for row in month_rows) == Decimal("0.00")
        assert sum(row["component"] == "payable" for row in month_rows) == 1


def test_february_is_cash_reimbursement_not_open_payable() -> None:
    assert "2026-02" not in NATHANIEL_ADVANCE_MONTHS
    assert not any(row["marker"].endswith("|2026-02") for row in manual_targets())
