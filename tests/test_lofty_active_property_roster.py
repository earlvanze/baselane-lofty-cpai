import json
import sys
from pathlib import Path

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lofty_monthly_active_roster import DEFAULT_POLICY, build_roster  # noqa: E402


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_repository_policy_requires_32_physical_properties_and_30_targets() -> None:
    policy = json.loads(DEFAULT_POLICY.read_text(encoding="utf-8"))

    assert policy["expected_counts"] == {
        "active_physical_properties": 32,
        "active_reporting_targets": 30,
    }
    assert policy["exclusion_rules"]["selling_is_active"] is True
    assert policy["live_ready_additions"][0]["address"].startswith("49 Bannbury Ln")
    assert len(policy["reporting_target_overrides"]) == 2


def test_builder_keeps_physical_coverage_separate_from_grouped_targets(tmp_path: Path) -> None:
    workbook_path = tmp_path / "schedule.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "Portfolio (Internal Name)",
            "Address",
            "PM / Sub-PM",
            "On Lofty?",
            "DAO",
            "Current Status (Occupied Units)",
            "Total Units",
        ]
    )
    sheet.append(["A", "1 Alpha St, Test, OH", "PM", "Yes", "DAO A", "Occupied", 1])
    sheet.append(["B", "2 Beta St, Test, OH", "PM", "Yes", "DAO A", "Occupied", 1])
    sheet.append(["C", "3 Gamma St, Test, OH", "PM", "Yes", "DAO B", "Selling", 1])
    sheet.append(["Sold", "4 Sold St, Test, OH", "PM", "Yes", "DAO C", "Sold", 1])
    workbook.save(workbook_path)

    package_path = tmp_path / "A Package"
    gamma_path = tmp_path / "3 Gamma St"
    delta_path = tmp_path / "5 Delta St"
    for path in (package_path, gamma_path, delta_path):
        path.mkdir()

    manager_path = tmp_path / "manager.json"
    write_json(
        manager_path,
        {
            "properties": [
                {"address": "1 Alpha St, Test, OH", "status": "ready"},
                {"address": "2 Beta St, Test, OH", "status": "ready"},
                {"address": "3 Gamma St, Test, OH", "status": "ready"},
                {"address": "5 Delta St, Test, OH", "status": "ready"},
            ]
        },
    )
    listing_policy_path = tmp_path / "listing-policy.json"
    write_json(
        listing_policy_path,
        {
            "sold_ignore_listing_updates": ["4 Sold St, Test, OH"],
            "operational_ignore_listing_updates": [],
        },
    )
    property_map_path = tmp_path / "property-map.json"
    write_json(property_map_path, {"properties": []})
    policy_path = tmp_path / "roster-policy.json"
    write_json(
        policy_path,
        {
            "version": "test",
            "effective_month": "2026-07",
            "expected_counts": {
                "active_physical_properties": 4,
                "active_reporting_targets": 3,
            },
            "schedule": {"worksheet": "Sheet", "active_marker": "Yes"},
            "property_map": str(property_map_path),
            "listing_update_policy": str(listing_policy_path),
            "live_ready_additions": [
                {
                    "address": "5 Delta St, Test, OH",
                    "dao": "DAO D",
                    "property_path": str(delta_path),
                }
            ],
            "reporting_target_overrides": [
                {
                    "dao": "DAO A",
                    "managed_name": "A Package",
                    "property_path": str(package_path),
                }
            ],
            "property_path_overrides": [
                {"address": "3 Gamma St, Test, OH", "property_path": str(gamma_path)}
            ],
        },
    )

    report = build_roster(
        policy_path,
        schedule_workbook=workbook_path,
        manager_snapshot=manager_path,
        property_map_path=property_map_path,
        listing_policy_path=listing_policy_path,
    )

    assert report["status"] == "ok"
    assert report["authoritative_active_property_count"] == 4
    assert report["authoritative_reporting_target_count"] == 3
    assert {row["address"] for row in report["excluded_physical_properties"]} == {
        "4 Sold St, Test, OH"
    }
    package = next(row for row in report["reporting_targets"] if row["managed_name"] == "A Package")
    assert package["physical_property_count"] == 2
    assert any(row["current_status"] == "Selling" for row in report["physical_properties"])
