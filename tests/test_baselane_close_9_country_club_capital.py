import sys
from decimal import Decimal
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from baselane_close_9_country_club_capital import (  # noqa: E402
    MORTGAGE_TAG_ID,
    NOAH_PREFIX,
    PROPERTY_ID,
    is_noah_reimbursement,
    noah_position,
    plan_digest,
)


def bank_row(transaction_id: str, posted: str, note: str) -> dict:
    return {
        "id": transaction_id,
        "amount": Decimal("-2875.00"),
        "date": posted,
        "merchantName": "Stone Manor Hospitality LLC | TRANSFER_O",
        "propertyId": PROPERTY_ID,
        "tagId": MORTGAGE_TAG_ID,
        "bankAccountId": "133098",
        "note": note,
    }


def advance_row(transaction_id: str, month: str) -> dict:
    return {
        "id": transaction_id,
        "amount": Decimal("2875.00"),
        "date": f"{month}-28",
        "merchantName": "Mortgage Interest Paid by Noah Simon",
        "propertyId": PROPERTY_ID,
        "tagId": "25",
        "bankAccountId": None,
        "note": f"{NOAH_PREFIX}|9 Country Club Ln N|{month}",
    }


def test_future_stone_advance_is_not_a_noah_reimbursement():
    settled = bank_row("1", "2026-06-01", "Mortgage Interest Payment")
    future = bank_row("2", "2026-07-15", "August 2026 mortgage interest")

    assert is_noah_reimbursement(settled) is True
    assert is_noah_reimbursement(future) is False


def test_noah_position_excludes_future_stone_advance():
    position = noah_position(
        [
            advance_row("10", "2026-03"),
            bank_row("20", "2026-06-01", "Mortgage Interest Payment"),
            bank_row("30", "2026-07-15", "August 2026 mortgage interest"),
        ]
    )

    assert position["interest_paid_by_noah_total"] == "2875.00"
    assert position["reimbursement_total"] == "2875.00"
    assert position["interest_payable"] == "0.00"


def test_plan_digest_is_stable_for_decimal_material():
    payload = {"targets": [{"amount": Decimal("2875"), "id": "1"}]}
    assert plan_digest(payload) == plan_digest(payload)
    assert len(plan_digest(payload)) == 64
