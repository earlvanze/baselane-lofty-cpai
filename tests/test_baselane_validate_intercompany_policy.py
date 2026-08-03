from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from baselane_validate_intercompany_policy import build_report  # noqa: E402


def test_build_report_validates_rules_against_source_evidence(tmp_path: Path):
    source = tmp_path / "source.csv"
    fieldnames = [
        "ISODate",
        "Account",
        "Property",
        "Pending",
        "Type",
        "BaselaneId",
        "Amount",
        "Category",
    ]
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "ISODate": "2026-07-31",
                "Account": "ECO Systems, LLC-2624",
                "Property": "",
                "Pending": "False",
                "Type": "Transaction",
                "BaselaneId": "cash-1",
                "Amount": "-500.00",
                "Category": "Repairs",
            }
        )
        writer.writerow(
            {
                "ISODate": "2026-07-31",
                "Account": "Property DAO LLC-Operations",
                "Property": "326 South Alcott Street",
                "Pending": "False",
                "Type": "Transaction",
                "BaselaneId": "mirror-1",
                "Amount": "500.00",
                "Category": "Transfers Between Accounts",
            }
        )
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effective_date": "2026-08-03",
                "rules": [
                    {
                        "baselane_id": "cash-1",
                        "expected_date": "2026-07-31",
                        "expected_amount": "-500.00",
                        "expected_account": "ECO Systems, LLC-2624",
                        "expected_property": "",
                        "property": "326 South Alcott Street",
                        "action": "include",
                        "classification": "approved_property_retag_from_blank_property",
                        "rationale": "Exact test correction.",
                        "evidence_baselane_ids": ["mirror-1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_report(source, policy, date(2026, 7, 31))

    assert report["status"] == "ok"
    assert report["rule_count"] == 1
    assert report["verified_payable_property_count"] == 1
    assert report["included_cash_row_count"] == 1
