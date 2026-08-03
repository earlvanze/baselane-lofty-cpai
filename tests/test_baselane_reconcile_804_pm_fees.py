import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import baselane_reconcile_804_pm_fees as mod  # noqa: E402


def test_804_pm_schedule_and_double_entry():
    rows = mod.targets()
    assert len(rows) == 20
    assert len({row["marker"] for row in rows}) == 20
    assert sum((mod.money(item["gross"]) for item in mod.RENT_EVIDENCE), Decimal("0")) == Decimal("34617.00")
    assert sum((mod.pm_fee(item["gross"]) for item in mod.RENT_EVIDENCE), Decimal("0")) == Decimal("4500.21")
    assert sum((mod.money(row["amount"]) for row in rows), Decimal("0")) == Decimal("0.00")


def test_804_pm_sides_use_distinct_entities_and_categories():
    rows = mod.targets()
    dao = [row for row in rows if row["side"] == "dao_expense"]
    eco = [row for row in rows if row["side"] == "eco_revenue"]
    assert {row["propertyId"] for row in dao} == {mod.PROPERTY_ID}
    assert {row["tagId"] for row in dao} == {mod.TAG_PROPERTY_MANAGEMENT}
    assert all(mod.money(row["amount"]) < 0 for row in dao)
    assert {row["propertyId"] for row in eco} == {mod.ECO_PROPERTY_ID}
    assert {row["tagId"] for row in eco} == {mod.TAG_FEES_OTHER_REVENUE}
    assert all(mod.money(row["amount"]) > 0 for row in eco)


def test_804_march_late_receipt_is_not_april_revenue():
    march = next(row for row in mod.RENT_EVIDENCE if row["month"] == "2026-03")
    assert march["receipt_date"] == "2026-04-20"
    assert march["corroborating_ids"] == ("274510087", "274510117")
    march_targets = [row for row in mod.targets() if row["month"] == "2026-03"]
    assert {row["date"] for row in march_targets} == {"2026-03-28"}
    assert {abs(mod.money(row["amount"])) for row in march_targets} == {Decimal("455.00")}
