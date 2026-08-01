from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_coownership_gl_policy as policy


def row(*, merchant: str, amount: str, notes: str) -> dict[str, str]:
    return {
        "Merchant": merchant,
        "Description": "CITADEL SERV PMT | MTGE PAYMT",
        "Category": "Loan Payments & Capex",
        "Sub-category": "Mortgage Principal Payments",
        "Type": "Manual",
        "Amount": amount,
        "Notes": notes,
    }


def test_exact_90_madison_curtailment_is_approved():
    candidate = row(
        merchant="90 Madison | approved 2024-10 NOI principal curtailment",
        amount="-1700",
        notes=(
            "AOPS-90-CURTAILMENT|recognition=2024-10|amount=1700.00 | "
            "Approved 50% NOI principal curtailment."
        ),
    )

    assert policy.is_approved_dao_principal_curtailment("90 Madison Ave", candidate, -1700.0)


def test_positive_transfer_receipt_is_not_an_approved_principal_expense():
    candidate = row(
        merchant="90 Madison | transfer to ECO for approved 2024-10 principal curtailment",
        amount="1700",
        notes=(
            "AOPS-90-MORTGAGE-TRANSFER|component=principal-curtailment|"
            "recognition=2024-10|amount=1700.00"
        ),
    )

    assert not policy.is_approved_dao_principal_curtailment("90 Madison Ave", candidate, 1700.0)


def test_wrong_amount_or_ordinary_principal_is_not_approved():
    candidate = row(
        merchant="90 Madison | ordinary mortgage principal",
        amount="-164.02",
        notes="Ordinary mortgage P&I remains ECO responsibility.",
    )

    assert not policy.is_approved_dao_principal_curtailment("90 Madison Ave", candidate, -164.02)
