from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import baselane_export_ledger as export


def eco_row(*, transaction_id: str, target: str, amount: float = 62.5) -> dict:
    prefix = "ECO Systems LLC DAO Registration Fee Revenue | "
    return {
        "id": transaction_id,
        "date": "2026-07-31",
        "amount": amount,
        "merchantName": f"{prefix}{target} | July 2026",
        "description": f"{prefix}{target} | July 2026",
        "propertyId": "eco",
        "tagId": "revenue",
        "note": {
            "text": (
                f"AOPS-MONTHLY-ACCRUAL|dao_eco|{target}|2026-07|62.50 | "
                "Accounting/manual accrual only, no bank transfer."
            )
        },
    }


def test_eco_accrual_target_requires_exact_balanced_evidence():
    row = {
        "Amount": 62.5,
        "Merchant": "ECO Systems LLC DAO Registration Fee Revenue | 1518 Dille Rd | July 2026",
        "Description": "ECO Systems LLC DAO Registration Fee Revenue | 1518 Dille Rd | July 2026",
        "Type": "Revenue",
        "Category": "Fees & Other Revenue",
        "Notes": "AOPS-MONTHLY-ACCRUAL|dao_eco|1518 Dille Rd|2026-07|62.50 | manual",
    }

    assert export.eco_accrual_target_property(row) == "1518 Dille Rd"
    row["Amount"] = 61.5
    assert export.eco_accrual_target_property(row) == ""


def test_export_retains_verified_eco_counterparts_but_excludes_coolwood(tmp_path):
    transactions = [
        eco_row(transaction_id="4", target="1456 W 85th St, Cleveland, OH 44102"),
        eco_row(transaction_id="3", target="22164 Umland Cir, Jenner, CA 95450"),
        eco_row(transaction_id="2", target="1 Coolwood Dr."),
        {
            "id": "1",
            "date": "2026-07-30",
            "amount": 100,
            "merchantName": "Tenant",
            "description": "Rent",
            "propertyId": "1456",
            "tagId": None,
            "note": None,
        },
    ]

    def fake_gql(operation_name, _query, variables=None):
        if operation_name == "PropertyList":
            return {
                "property": [
                    {"id": "eco", "name": "Mining, Sales, Consulting, and PM", "address": ""},
                    {"id": "cool", "name": "1 Coolwood Dr.", "address": ""},
                    {"id": "1456", "name": "1456 W 85th St.", "address": ""},
                ]
            }
        if operation_name == "TagList":
            return {"tag": [{"type": "Revenue", "subType": [{"id": "revenue", "name": "Fees & Other Revenue"}]}]}
        if operation_name == "Transactions":
            assert variables["input"]["sort"] == {"field": "id", "direction": "DESC"}
            return {"transactions": {"total": len(transactions), "data": transactions}}
        raise AssertionError(operation_name)

    tracker = tmp_path / "tracker"
    reports = tmp_path / "reports"
    tracker.mkdir()
    reports.mkdir()
    out_path = tracker / "ECO Systems General Ledger.csv"
    guard_path = reports / "guard.json"

    assert export.run_export(
        app_check="test",
        bsession="test",
        tracker_dir=tracker,
        reports_dir=reports,
        out_path=out_path,
        guard_report_path=guard_path,
        min_rows=0,
        max_rows=10,
        gql_func=fake_gql,
    ) == 0

    with out_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["Property"] for row in rows] == [
        "1456 W 85th St, Cleveland, OH 44102",
        "22164 Umland Cir, Jenner, CA 95450",
        "1456 W 85th St.",
    ]
    guard = json.loads(guard_path.read_text(encoding="utf-8"))
    assert guard["included_eco_accrual_counterpart_rows"] == 2
    assert guard["dropped_excluded_eco_accrual_target_rows"] == 1
    assert guard["output_rows"] == 3


def test_scheduled_export_uses_stable_fast_pagination_and_excludes_coolwood():
    source = (SCRIPTS / "baselane_export_human_paced.js").read_text(encoding="utf-8")

    assert "BASELANE_PAGE_LIMIT || 500" in source
    assert "sort: {direction: 'DESC', field: 'id'}" in source
    assert "'1 Coolwood Dr.'" in source
