import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from baselane_reconcile_9_country_club import (  # noqa: E402
    OPERATIONS_ACCOUNT_ID,
    PROPERTY,
    TAG_MORTGAGE_INTEREST,
    TAG_PROPERTY_TAXES,
    TAG_TAXES,
    mortgage_reconciliation_issues,
    property_hold_report,
    scheduled_targets,
    update_decision,
)


def row(**updates):
    value = {
        "id": "1",
        "amount": Decimal("0"),
        "date": "2026-07-01",
        "bankAccountId": None,
        "tagId": None,
        "note": None,
    }
    value.update(updates)
    return value


def settlement_targets(rows, kind):
    return [
        target
        for target in scheduled_targets(rows)
        if target["kind"] == kind
    ]


def test_two_june_lender_debits_for_one_statement_month_block_mutations():
    rows = [
        row(
            id="292462381",
            amount=Decimal("-2875"),
            date="2026-06-02",
            bankAccountId=OPERATIONS_ACCOUNT_ID,
            tagId=TAG_MORTGAGE_INTEREST,
            merchantName="UBS BANK USA | LOAN PAYMT",
        ),
        row(
            id="296257769",
            amount=Decimal("-2875"),
            date="2026-06-10",
            bankAccountId=OPERATIONS_ACCOUNT_ID,
            tagId=TAG_MORTGAGE_INTEREST,
            merchantName="UBS BANK USA | LOAN PAYMT",
        ),
        row(
            id="may",
            amount=Decimal("-2875"),
            date="2026-05-21",
            bankAccountId=OPERATIONS_ACCOUNT_ID,
            tagId=TAG_MORTGAGE_INTEREST,
            merchantName="UBS BANK USA | LOAN PAYMT",
        ),
        row(
            id="july",
            amount=Decimal("-2875"),
            date="2026-07-01",
            bankAccountId=OPERATIONS_ACCOUNT_ID,
            tagId=TAG_MORTGAGE_INTEREST,
            merchantName="UBS BANK USA | LOAN PAYMT",
        ),
    ]

    issues = mortgage_reconciliation_issues(rows)
    june_issue = next(
        issue
        for issue in issues
        if issue["code"] == "lender_bank_statement_month_mismatch"
        and issue["month"] == "2026-06"
    )
    assert june_issue["bank_net_lender_debits"] == "5750.00"
    assert june_issue["lender_statement_recognized_interest"] == "2875.00"
    targets = settlement_targets(rows, "mortgage_settlement")
    assert targets == []


def test_stone_cash_out_is_not_treated_as_a_lender_settlement():
    rows = [
        row(
            id="may",
            amount=Decimal("-2875"),
            date="2026-05-21",
            bankAccountId=OPERATIONS_ACCOUNT_ID,
            tagId=TAG_MORTGAGE_INTEREST,
            merchantName="UBS BANK USA | LOAN PAYMT",
        ),
        row(
            id="june",
            amount=Decimal("-2875"),
            date="2026-06-10",
            bankAccountId=OPERATIONS_ACCOUNT_ID,
            tagId=TAG_MORTGAGE_INTEREST,
            merchantName="UBS BANK USA | LOAN PAYMT",
        ),
        row(
            id="july",
            amount=Decimal("-2875"),
            date="2026-07-01",
            bankAccountId=OPERATIONS_ACCOUNT_ID,
            tagId=TAG_MORTGAGE_INTEREST,
            merchantName="UBS BANK USA | LOAN PAYMT",
        ),
        row(
            id="315063504",
            amount=Decimal("-2875"),
            date="2026-07-15",
            bankAccountId=OPERATIONS_ACCOUNT_ID,
            tagId=TAG_MORTGAGE_INTEREST,
            merchantName="Stone Manor Hospitality LLC | TRANSFER_O",
            note="August 2026 mortgage interest",
        ),
    ]

    issues = mortgage_reconciliation_issues(rows)
    owner_issue = next(
        issue
        for issue in issues
        if issue["code"] == "owner_cash_misclassified_as_lender_mortgage_cash"
    )
    assert owner_issue["transaction_ids"] == ["315063504"]
    targets = settlement_targets(rows, "mortgage_settlement")
    assert targets == []


def test_clean_statement_backed_lender_rows_create_exact_cash_references():
    rows = [
        row(
            id=transaction_id,
            amount=Decimal("-2875"),
            date=posted,
            bankAccountId=OPERATIONS_ACCOUNT_ID,
            tagId=TAG_MORTGAGE_INTEREST,
            merchantName="UBS BANK USA | LOAN PAYMT",
        )
        for transaction_id, posted in (
            ("may", "2026-05-21"),
            ("june", "2026-06-10"),
            ("july", "2026-07-01"),
        )
    ]

    assert mortgage_reconciliation_issues(rows) == []
    targets = settlement_targets(rows, "mortgage_settlement")
    assert [target["sourceTransactionId"] for target in targets] == [
        "may",
        "june",
        "july",
    ]
    assert all(
        f"payment={target['sourceTransactionId']}" in target["note"]
        for target in targets
    )


def test_unreferenced_legacy_mortgage_journal_blocks_rebuild():
    rows = [
        row(
            id=transaction_id,
            amount=Decimal("-2875"),
            date=posted,
            bankAccountId=OPERATIONS_ACCOUNT_ID,
            tagId=TAG_MORTGAGE_INTEREST,
            merchantName="UBS BANK USA | LOAN PAYMT",
        )
        for transaction_id, posted in (
            ("may", "2026-05-21"),
            ("june", "2026-06-10"),
            ("july", "2026-07-01"),
        )
    ]
    rows.append(
        row(
            id="400",
            amount=Decimal("2875"),
            date="2026-05-21",
            tagId=TAG_MORTGAGE_INTEREST,
            note=(
                "AOPS-9CC-RECON|mortgage_settlement|"
                f"{PROPERTY}|2026-05|2875.00 | no exact cash reference"
            ),
        )
    )

    issues = mortgage_reconciliation_issues(rows)
    legacy = next(
        issue
        for issue in issues
        if issue["code"] == "unreferenced_legacy_mortgage_settlement_journals"
    )
    assert legacy["transaction_ids"] == ["400"]
    assert settlement_targets(rows, "mortgage_settlement") == []


def test_tax_target_clears_new_cash_without_recreating_prior_clearing():
    rows = [
        row(
            id="feb-cash",
            amount=Decimal("-262.45"),
            date="2026-02-28",
            bankAccountId=OPERATIONS_ACCOUNT_ID,
            tagId=TAG_TAXES,
        ),
        row(
            id="feb-settlement",
            amount=Decimal("262.45"),
            date="2026-02-28",
            tagId=TAG_TAXES,
            note={
                "text": (
                    "AOPS-9CC-RECON|tax_settlement|"
                    f"{PROPERTY}|2026-02|262.45 | prior clearing"
                )
            },
        ),
        row(
            id="july-cash",
            amount=Decimal("-8316.80"),
            date="2026-07-18",
            bankAccountId=OPERATIONS_ACCOUNT_ID,
            tagId=TAG_PROPERTY_TAXES,
        ),
    ]

    targets = settlement_targets(rows, "tax_settlement")
    assert len(targets) == 1
    assert targets[0]["amount"] == Decimal("8316.80")
    assert targets[0]["sourceTransactionIds"] == ["july-cash"]


def test_update_decision_does_not_rewrite_equivalent_live_values():
    target = {
        "id": "100",
        "values": {"propertyId": "91341", "unitId": None, "tagId": "80"},
        "reason": "test",
    }
    current = {
        "id": 100,
        "propertyId": 91341,
        "unitId": None,
        "tagId": "80",
    }

    decision = update_decision({"100": current}, target)
    assert decision["missing"] is False
    assert decision["requires_update"] is False

    current["tagId"] = "24"
    decision = update_decision({"100": current}, target)
    assert decision["requires_update"] is True


def test_property_hold_report_holds_only_the_conflicted_property(tmp_path):
    source_report = tmp_path / "nine-country-club.json"
    report = property_hold_report(
        {
            "generated_at": "2026-08-02T13:50:00Z",
            "digest": "a" * 64,
            "mutation_allowed": False,
            "reconciliation_issues": [
                {"code": "lender_bank_statement_month_mismatch"}
            ],
        },
        source_report=source_report,
    )

    assert report["status"] == "held"
    assert report["held_property_count"] == 1
    assert report["held_properties"][0]["property_name"] == "9 Country Club Ln N"
    assert report["held_properties"][0]["issue_codes"] == [
        "lender_bank_statement_month_mismatch"
    ]
    assert report["held_properties"][0]["live_state_verified"] is True


def test_property_hold_report_clears_after_verified_reconciliation(tmp_path):
    report = property_hold_report(
        {
            "generated_at": "2026-08-02T13:50:00Z",
            "digest": "b" * 64,
            "mutation_allowed": True,
            "reconciliation_issues": [],
        },
        source_report=tmp_path / "nine-country-club.json",
    )

    assert report["status"] == "ok"
    assert report["held_property_count"] == 0
    assert report["held_properties"] == []
