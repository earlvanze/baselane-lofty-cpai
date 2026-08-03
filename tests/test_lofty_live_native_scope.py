from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lofty_live_native_scope import (  # noqa: E402
    enrich_targets_from_active_roster,
    load_active_roster_scope,
    partition_current_manager_targets,
    split_native_live_targets,
    validate_full_reporting_scope,
)
from lofty_capture_live_financial_guards import (  # noqa: E402
    listing_cash_flow_projection_override,
    verify_live_distribution,
)
from lofty_monthly_publish_to_pm import (  # noqa: E402
    authoritative_roster_lifecycle_guards,
    build_runtime_map,
    property_id_candidates,
    runtime_map_scope_guard,
)


def test_roster_scope_preserves_32_physical_and_30_reporting_targets(tmp_path: Path) -> None:
    roster = tmp_path / "roster.json"
    roster.write_text(
        json.dumps(
            {
                "status": "ok",
                "authoritative_active_property_count": 32,
                "authoritative_reporting_target_count": 30,
            }
        ),
        encoding="utf-8",
    )

    scope = load_active_roster_scope(roster)

    assert scope["status"] == "ok"
    assert scope["physical_property_count"] == 32
    assert scope["portfolio_reporting_target_count"] == 30
    assert validate_full_reporting_scope(scope, 30, targeted=False) == []
    assert validate_full_reporting_scope(scope, 20, targeted=False) == [
        "active reporting scope mismatch: selected=20, authoritative=30"
    ]
    assert validate_full_reporting_scope(scope, 1, targeted=True) == []


def test_no_native_id_is_nonblocking_for_accounting_and_reporting() -> None:
    native, unavailable = split_native_live_targets(
        [
            {"property_name": "Native", "lofty_property_id": "ABC123"},
            {"property_name": "Ohio 3 Property Package", "lofty_property_id": None},
        ]
    )

    assert [record["property_name"] for record in native] == ["Native"]
    assert unavailable[0]["status"] == "unavailable_no_live_property_id"
    assert unavailable[0]["accounting_and_investor_reporting_included"] is True
    assert unavailable[0]["nonblocking_scope"] == "native_lofty_listing_actions_only"


def test_current_manager_partition_distinguishes_portfolio_and_live_action_scope() -> None:
    targets = []
    for index in range(20):
        targets.append(
            {
                "property_name": f"Ready {index}",
                "lofty_property_id": f"READY-{index}",
                "lofty_live_mutation_available": True,
            }
        )
    for index in range(6):
        targets.append(
            {
                "property_name": f"Unavailable {index}",
                "lofty_property_id": f"UNAVAILABLE-{index}",
                "lofty_live_mutation_available": False,
            }
        )
    for index in range(4):
        targets.append({"property_name": f"No ID {index}", "lofty_property_id": None})

    partition = partition_current_manager_targets(targets)

    assert len(targets) == 30
    assert len(partition["known_id"]) == 26
    assert len(partition["captureable"]) == 26
    assert len(partition["actionable"]) == 20
    assert len(partition["manager_unavailable"]) == 0
    assert len(partition["mutation_unavailable"]) == 6
    assert len(partition["no_id"]) == 4
    assert all(record["live_capture_guard_applicable"] is False for record in partition["mutation_unavailable"])
    assert all(record["live_capture_guard_applicable"] is False for record in partition["no_id"])


def test_authenticated_manager_ids_override_roster_availability_hint() -> None:
    targets = [
        {
            "property_name": "Freshly available",
            "lofty_property_id": "A",
            "lofty_live_mutation_available": False,
        },
        {
            "property_name": "Freshly unavailable",
            "lofty_property_id": "B",
            "lofty_live_mutation_available": True,
        },
    ]

    partition = partition_current_manager_targets(
        targets,
        live_property_ids={"A"},
        mutation_ready_property_ids={"A"},
    )

    assert [record["lofty_property_id"] for record in partition["actionable"]] == ["A"]
    assert [record["lofty_property_id"] for record in partition["manager_unavailable"]] == ["B"]


def test_active_roster_overlay_replaces_stale_ids_and_preserves_no_id(tmp_path: Path) -> None:
    property_a = tmp_path / "A"
    property_b = tmp_path / "B"
    scope = {
        "records": [
            {
                "property_name": "A",
                "property_path": str(property_a),
                "lofty_property_id": "CURRENT-A",
                "lofty_live_mutation_available": True,
            },
            {
                "property_name": "B",
                "property_path": str(property_b),
                "lofty_property_id": None,
                "lofty_live_mutation_available": False,
            },
        ]
    }
    targets = [
        {"property_name": "A", "property_path": str(property_a), "lofty_property_id": "STALE-A"},
        {"property_name": "B", "property_path": str(property_b), "lofty_property_id": "STALE-B"},
    ]

    enriched, unmatched = enrich_targets_from_active_roster(targets, scope)

    assert unmatched == []
    assert enriched[0]["lofty_property_id"] == "CURRENT-A"
    assert enriched[1]["lofty_property_id"] is None


def test_publish_runtime_map_does_not_block_target_without_native_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LOFTY_SKIP_PROPERTY_SIBLING_RESOLUTION", "1")
    property_path = tmp_path / "Ohio 3 Property Package"
    snapshot = property_path / "Public" / "00 - README & Property Snapshot"
    snapshot.mkdir(parents=True)
    (snapshot / "UPDATES.md").write_text("# Updates\n", encoding="utf-8")
    (snapshot / "FINANCIALS.md").write_text("# Financials\n", encoding="utf-8")

    properties, records = build_runtime_map(
        [{"property_path": str(property_path), "status": "existing"}],
        [],
        tmp_path / "payloads",
        "2026-07",
        False,
    )

    assert properties == []
    assert records[0]["status"] == "unavailable_no_live_property_id"
    assert not records[0]["status"].startswith("blocked_")
    assert records[0]["accounting_and_investor_reporting_included"] is True


def test_publish_property_ids_use_authoritative_active_roster(tmp_path: Path) -> None:
    roster = tmp_path / "roster.json"
    roster.write_text(
        json.dumps(
            {
                "status": "ok",
                "records": [
                    {
                        "property_name": "Native Property",
                        "property_path": str(tmp_path / "Native Property"),
                        "lofty_property_id": "CURRENT-ID",
                    },
                    {
                        "property_name": "Ohio 3 Property Package",
                        "property_path": str(tmp_path / "Ohio 3-Property Package"),
                        "lofty_property_id": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(
        json.dumps(
            {
                "properties": [
                    {
                        "name": "Ohio 3 Property Package",
                        "editHref": "/property-owners/edit/STALE-ID",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    candidates = property_id_candidates(portfolio, None, roster)

    assert {candidate["property_id"] for candidate in candidates} == {"CURRENT-ID"}
    assert all(candidate["source"] == "active_roster" for candidate in candidates)


def test_canonical_runtime_map_requires_exact_reporting_target_coverage(tmp_path: Path) -> None:
    packet = tmp_path / "candidate.json"
    packet.write_text(json.dumps({"property_count": 30}), encoding="utf-8")
    runtime_map = tmp_path / "baselane_financials_monthly_lofty_pm_runtime_map.json"

    complete_path, complete_issue, expected = runtime_map_scope_guard(runtime_map, 30, packet, None)
    subset_path, subset_issue, _ = runtime_map_scope_guard(runtime_map, 20, packet, None)

    assert complete_path == runtime_map
    assert complete_issue is None
    assert expected == 30
    assert subset_path.name.endswith(".targeted-subset-blocked.json")
    assert "reporting_targets=20" in str(subset_issue)


def test_authoritative_roster_supersedes_stale_selling_lifecycle_guard(tmp_path: Path) -> None:
    roster = tmp_path / "roster.json"
    roster.write_text(
        json.dumps(
            {
                "status": "ok",
                "authoritative_reporting_target_count": 1,
                "records": [
                    {
                        "property_name": "918 Frederick Blvd",
                        "property_path": str(tmp_path / "918 Frederick Blvd, Akron, OH 44320"),
                        "physical_addresses": ["918 Frederick Blvd, Akron, OH 44320"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    stale_guard = {
        "source": "yhome_transition_reconciliation",
        "property_name": "918 Frederick Blvd. Akron, OH 44320",
        "normalized_property": "918 frederick blvd akron oh 44320",
        "yhome_column_b": "ECO (Selling)",
    }

    retained, conflicts = authoritative_roster_lifecycle_guards(roster, [stale_guard])

    assert retained == []
    assert conflicts == [stale_guard]


def test_live_financial_guard_uses_month_scoped_listing_projection_override(tmp_path: Path) -> None:
    financials = tmp_path / "FINANCIALS.md"
    financials.write_text(
        """# Financials

| Metric | Amount |
|---|---:|
| Recurring Net Operating Cashflow | $868.88 |
| Projected Annual Cash Flow Basis | $10,426.56 |
""",
        encoding="utf-8",
    )
    policy_path = tmp_path / "lofty_listing_update_policy.json"
    policy = {
        "projected_annual_cash_flow_overrides": [
            {
                "address": "5541 S Peoria St, Chicago, IL 60621",
                "run_month": "2026-07",
                "projected_annual_cash_flow": 13004.04,
                "approved_at": "2026-07-27",
            }
        ]
    }
    projection_override = listing_cash_flow_projection_override(
        policy,
        "5541 S Peoria St, Chicago, IL 60621",
        "2026-07",
        policy_path=policy_path,
    )

    result = verify_live_distribution(
        financials,
        {
            "cash_flow": 13004.04,
            "coc": 4.75,
            "projected_rental_yield": 4.75,
            "is_occupied": True,
            "total_investment": 273650,
        },
        "5541 S Peoria St, Chicago, IL 60621",
        projection_override,
    )

    assert result["ok"] is True
    assert result["expected"] == 13004.04
    assert result["local_expected"] == 10426.56
    assert result["expected_source"] == "listing_update_policy_override"
    assert result["expected_coc"] == 4.75


def test_zero_listing_projection_override_preserves_disabled_distribution(tmp_path: Path) -> None:
    financials = tmp_path / "FINANCIALS.md"
    financials.write_text(
        """# Financials

| Metric | Amount |
|---|---:|
| Net Operating Cashflow | $1,532.78 |
""",
        encoding="utf-8",
    )
    policy = {
        "projected_annual_cash_flow_overrides": [
            {
                "address": "1456 W 85th St, Cleveland, OH 44102",
                "run_month": "2026-07",
                "projected_annual_cash_flow": 0,
            }
        ]
    }
    projection_override = listing_cash_flow_projection_override(
        policy,
        "1456 W 85th St, Cleveland, OH 44102",
        "2026-07",
    )

    result = verify_live_distribution(
        financials,
        {
            "cash_flow": 0,
            "coc": 0,
            "projected_rental_yield": 0,
            "is_occupied": False,
            "total_investment": 212100,
        },
        "1456 W 85th St, Cleveland, OH 44102",
        projection_override,
    )

    assert result["ok"] is True
    assert result["expected"] == 0
    assert result["local_expected"] == 18393.36
    assert result["distribution_disabled"] is True


def test_listing_projection_override_does_not_leak_to_another_month() -> None:
    policy = {
        "projected_annual_cash_flow_overrides": [
            {
                "address": "5541 S Peoria St, Chicago, IL 60621",
                "run_month": "2026-07",
                "projected_annual_cash_flow": 13004.04,
            }
        ]
    }

    assert (
        listing_cash_flow_projection_override(
            policy,
            "5541 S Peoria St, Chicago, IL 60621",
            "2026-08",
        )
        is None
    )
